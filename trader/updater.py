"""Over-the-air updates: fetch new code from GitHub, test it, restart the desk, roll back if needed.

Runs as a separate, short-lived job (systemd timer every 30 minutes, or at once when
the owner sends /update on Telegram). The desk itself runs in a sandbox that cannot
change its own files, so it only *asks* for an update by creating `data/update-requested`.

Order of events when new code exists:
  git pull (fast-forward only) -> install libraries -> run the automated tests
  -> restart the desk -> wait and confirm it is running.
Any failure rolls back to the previous version. Every outcome is reported on Telegram.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import ROOT, Settings
from .notify import Telegram

log = logging.getLogger(__name__)
SERVICE = "crypto-desk"
DATA = ROOT / "data"
STATUS = DATA / "update-status.json"
REQUEST = DATA / "update-requested"
RESTART = DATA / "restart-requested"   # consumed by the root-owned crypto-desk-restart service
LOCKFILE = "requirements.lock"
TEST_TIMEOUT = 1500   # seconds, at most; the job's time budget usually leaves less (see JOB_BUDGET)
JOB_BUDGET = 1700     # servers installed before 1.3.2 stop the whole job after 1800 s: stay inside that
RESTART_WAIT = 660    # the desk finishes its current market check before restarting (systemd allows 600 s)
DESK_STARTED = DATA / "desk-started.json"   # written by the desk at start: its version and revision
HEARTBEAT = DATA / "heartbeat.json"         # written by the desk after every market check (for the watchdog)


class Interrupted(Exception):
    """The system stopped the update job (SIGTERM), usually because it ran longer than allowed."""


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def revision(root: Path = ROOT) -> str | None:
    try:
        return git(root, "rev-parse", "HEAD")
    except (subprocess.SubprocessError, OSError):
        return None


def installed_version(root: Path = ROOT) -> str | None:
    """The release number of the code on disk (may be newer than the running desk until it restarts)."""
    try:
        return parse_version((Path(root) / "trader" / "__init__.py").read_text())
    except OSError:
        return None


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def redact(text) -> str:
    """git error text can quote the remote URL; never let a token through to Telegram or logs."""
    return re.sub(r"(github_pat_|ghp_)\S+|://[^/\s]+@", lambda m: "<token>" if m.group(1) else "://<token>@", str(text))


def test_summary(out: str) -> str:
    """What a failed test run said, short enough for a phone: the FAILED lines and the final count."""
    if out.startswith("timed out"):
        return "the tests did not finish in time on this server (" + out + ")"
    lines = [ln.strip() for ln in out.splitlines()
             if ln.startswith(("FAILED ", "ERROR ")) or (" failed" in ln and " in " in ln and ln.strip().startswith("="))]
    text = "; ".join(lines[:5]).replace("=", "").strip()
    return redact(text[:600] if text else out.strip()[-400:])


def read_status(path: Path = STATUS) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def request_update(path: Path = REQUEST) -> None:
    """Called by the desk (Telegram /update): the updater job picks this file up within seconds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"requested_at": now(), "by": "telegram"}))


def updater_installed(status_path: Path = STATUS) -> bool:
    return status_path.exists()


def git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                              timeout=120).stdout.strip()
    except subprocess.TimeoutExpired as e:  # a hung network call: report it like any other git failure
        raise subprocess.CalledProcessError(1, e.cmd, stderr=f"git {args[0]} timed out after 120 s") from None


def parse_version(source: str) -> str | None:
    """The release number declared in trader/__init__.py (any revision of it)."""
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.M)
    return m.group(1) if m else None


def version_string(root: Path = ROOT) -> str:
    """'1.3.0 (02923ca)': the release number plus the exact code revision, for messages and the notebook."""
    try:
        return f"{__version__} ({git(root, 'rev-parse', '--short', 'HEAD')})"
    except (subprocess.SubprocessError, OSError, ValueError):
        return __version__


def version_text(root: Path = ROOT, status_path: Path = STATUS) -> str:
    try:
        sha, subject, date = git(root, "log", "-1", "--format=%h%n%s%n%ci").split("\n", 2)
        lines = [f"🏷️ Running version {__version__} (revision {sha}, {date[:10]}): {subject}"]
    except (subprocess.SubprocessError, OSError, ValueError):
        lines = [f"🏷️ Running version {__version__} (not a git checkout, so the exact revision is unknown)"]
    on_disk = installed_version(root)
    if on_disk and on_disk != __version__:
        lines.append(f"⚠️ Version {on_disk} is installed but the desk is still running {__version__}: it switches at "
                     "the next restart.")
    st = read_status(status_path)
    if not st:
        lines.append("Automatic updates are not set up on this machine.")
        return "\n".join(lines)
    checked = st.get("checked_at", "")[:16].replace("T", " ")
    if st.get("in_progress"):
        lines.append(f"⏳ An update is in progress or was interrupted during the {st['in_progress'].get('step')} step "
                     "(the next check finishes or undoes it).")
    if st.get("behind"):
        label = f"version {st['incoming_version']}" if st.get("incoming_version") else "a new version"
        lines.append(f"🆕 Update available: {label} ({st['behind']} new change{'s' if st['behind'] != 1 else ''}): "
                     f"{st.get('incoming', '')}".strip())
        lines.append("Send /update to install it now." if not st.get("auto_update") else
                     "It installs automatically within about 30 minutes.")
    else:
        lines.append(f"✅ Up to date (last checked {checked} UTC).")
    if st.get("last_check_error"):
        lines.append(f"⚠️ The last check for updates failed: {st['last_check_error']}")
    if st.get("last_result"):
        lines.append(f"Last update: {st['last_result']}")
    return "\n".join(lines)


class Updater:
    def __init__(self, s: Settings, tg: Telegram | None = None, root: Path = ROOT, status_path: Path = STATUS,
                 request_path: Path = REQUEST, install_cmd: list[str] | None = None, test_cmd: list[str] | None = None,
                 restart=None, is_active=None, settle_seconds: int = 20, say=print, restart_wait: int = RESTART_WAIT,
                 started_path: Path | None = None, heartbeat_path: Path | None = None, clock=time.time):
        self.s = s
        self.restart_wait = restart_wait
        self.started_path = Path(started_path) if started_path else Path(status_path).parent / DESK_STARTED.name
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else Path(status_path).parent / HEARTBEAT.name
        self.clock = clock
        self._t0 = clock()
        self._step = ""
        self.tg = tg if tg is not None else Telegram(s.telegram_token, s.telegram_chat_id)
        self.root, self.status_path, self.request_path = Path(root), Path(status_path), Path(request_path)
        self.install_cmd = install_cmd or [sys.executable, "-m", "pip", "install", "-q", "-r", LOCKFILE]
        self.test_cmd = test_cmd or [sys.executable, "-m", "pytest", "-q", "-x", "tests"]
        self.restart = restart or self._restart_service
        self.is_active = is_active or self._service_active
        self.settle_seconds = settle_seconds
        self.poll_seconds = 5
        self.say = say

    # ---- system helpers (replaced in tests) ----
    def _restart_service(self) -> None:
        """Ask root to restart the desk by dropping a flag file; a tiny root-owned systemd path unit
        installed by deploy/setup.sh picks it up. No sudo rights are needed anywhere in this job."""
        flag = self.root / "data" / "restart-requested"
        flag.write_text(now())
        for _ in range(130):  # the desk may take up to 10 min to finish its current market check
            time.sleep(self.poll_seconds)
            if not flag.exists():
                return
        flag.unlink(missing_ok=True)
        raise OSError("the restart helper did not pick up the request (is crypto-desk-restart.path enabled?)")

    @staticmethod
    def _service_active() -> bool:
        return subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0

    def _run(self, cmd: list[str], timeout: int) -> tuple[bool, str]:
        try:
            p = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, f"timed out after {timeout}s"
        return p.returncode == 0, (p.stdout + p.stderr)[-4000:]

    # ---- status / messaging ----
    def _status(self, **fields) -> dict:
        st = read_status(self.status_path) or {}
        st.update(fields, checked_at=now(), auto_update=self.s.auto_update)
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(st, indent=1))
        return st

    def _notify(self, text: str) -> None:
        self.say(text)
        self.tg.send(text)

    def _remote_version(self, branch: str) -> str | None:
        """Release number of the code waiting on GitHub (read without installing it)."""
        try:
            return parse_version(git(self.root, "show", f"origin/{branch}:trader/__init__.py"))
        except (subprocess.SubprocessError, OSError):
            return None

    def _rollback(self, old: str, why: str) -> str:
        git(self.root, "reset", "--hard", old)
        self._run(self.install_cmd, 600)
        return f"rolled back to {old[:7]}: {why}"

    # ---- the job ----
    def run(self, force: bool = False, check_only: bool = False) -> dict:
        """Every outcome of a requested update reaches the owner, including a crash or the system stopping the job."""
        requested = self.request_path.exists()
        wanted = force or requested
        self._t0 = self.clock()
        try:
            self._watchdog()
            return self._run_job(wanted, check_only)
        except Interrupted:
            self._notify(f"⚠️ The update job was stopped by the server during the {self._step or 'check'} step, probably "
                         f"because it ran longer than allowed. Trading continues on version {__version__}; the next "
                         "check (within 30 minutes) finishes or undoes it and tells you.")
            return self._status(result="failed", last_result=f"{now()[:16]} stopped by the server during {self._step}")
        except Exception as e:  # noqa: BLE001 - never fail silently: the owner is waiting for an answer
            msg = redact(str(e) or type(e).__name__)[-300:]
            prev = read_status(self.status_path) or {}
            if wanted or prev.get("notified_error") != msg:
                self._notify(f"⚠️ The update job failed unexpectedly: {msg}\nTrading continues on version {__version__}. "
                             "(Details: `desk logs-update` on the server.)")
            return self._status(result="check_failed", last_check_error=msg, notified_error=msg)
        finally:
            if requested:
                self.request_path.unlink(missing_ok=True)

    def _progress(self, step: str, **fields) -> None:
        """Remember how far an update got, so a job stopped half-way is finished or undone by the next one."""
        self._step = step
        st = read_status(self.status_path) or {}
        self._status(in_progress={**(st.get("in_progress") or {}), **fields, "step": step, "since": now()})

    def _recover(self, prev: dict) -> None:
        """The previous job stopped half-way. If the new desk did start, the update succeeded: say so. Otherwise put
        the code back to what the running desk was started from, so code and desk agree again."""
        root = self.root
        self._step = "recovery"
        head = git(root, "rev-parse", "HEAD")
        started = read_json(self.started_path) or {}
        new, old = prev.get("new"), prev.get("old")
        if new and head == new and started.get("revision") == new:
            self._notify(f"🔄 Update to {prev.get('label', new[:7])} completed. (The job was stopped before it could "
                         "report it; the new version is confirmed running.)")
            self._status(in_progress=None, result="updated", behind=0, last_result=f"{now()[:16]} updated to "
                         f"{prev.get('label', new[:7])} (confirmed late)")
            return
        if old and head != old:
            git(root, "reset", "--hard", old)
            self._run(self.install_cmd, 600)
            if prev.get("step") in ("restart", "confirm"):
                try:
                    self.restart()
                except (subprocess.SubprocessError, OSError):
                    pass
        tries = read_status(self.status_path) or {}
        count = (tries.get("interrupted") or {}).get("count", 0) + 1 if (tries.get("interrupted") or {}).get("rev") == new else 1
        self._notify(f"⚠️ The previous update attempt ({prev.get('label', '')}) was interrupted during the "
                     f"{prev.get('step')} step and has been undone; the desk runs version {__version__}."
                     + (" Trying again now." if count < 3 else " It was interrupted three times, so automatic installs of "
                        "this version are paused; send /update to try again."))
        self._status(in_progress=None, interrupted={"rev": new, "count": count},
                     last_result=f"{now()[:16]} interrupted during {prev.get('step')}; undone")

    def _watchdog(self) -> None:
        """Runs with every update check (every 30 minutes): has the desk stopped checking the market?"""
        beat = read_json(self.heartbeat_path)
        if not beat or not beat.get("ts"):
            return
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(beat["ts"])).total_seconds() / 60
        except ValueError:
            return
        limit = max(45.0, 3 * float(beat.get("next_minutes") or 15) + 15)
        if str(beat.get("note", "")).startswith("weekly strategy lab"):
            limit = max(limit, 120.0)
        st = read_status(self.status_path) or {}
        if age <= limit:
            if st.get("stall_notified"):
                self._notify("✅ Watchdog: the desk is checking the market again.")
                self._status(stall_notified=None, stall_restarted=None)
            return
        if st.get("stall_notified") != beat["ts"]:
            active = self.is_active()
            self._notify(f"⚠️ Watchdog: the desk has not finished a market check for {age:.0f} minutes (last one at "
                         f"{beat['ts'][11:16]} UTC). " + ("Its service is running, so it is probably stuck; it will be "
                                                          "restarted if this continues." if active else
                                                          "Its service is not running; the server normally restarts it "
                                                          "within a minute. If not, run `desk start` on the server."))
            self._status(stall_notified=beat["ts"])
        elif age > 2 * limit and st.get("stall_restarted") != beat["ts"] and self.is_active():
            self._notify(f"🔁 Watchdog: restarting the desk, stuck for {age:.0f} minutes. Open positions keep their "
                         "stop-loss and take-profit on the exchange meanwhile.")
            self._status(stall_restarted=beat["ts"])
            try:
                self.restart()
            except (subprocess.SubprocessError, OSError) as e:
                self._notify(f"⚠️ Watchdog: the restart did not work: {e}")

    def _wait_for_desk(self, new_rev: str, since: str) -> tuple[bool, bool]:
        """After a restart: (running, confirmed). Confirmed = the new desk wrote its start file with the new
        revision. Desks before 1.4.1 do not write it, so then only the service state is checked."""
        deadline = self.clock() + self.restart_wait
        while self.clock() < deadline:
            st = read_json(self.started_path) or {}
            if st.get("revision") == new_rev and st.get("started_at", "") >= since:
                time.sleep(self.settle_seconds)  # a crash right after start shows up as "not active"
                return self.is_active(), True
            time.sleep(self.poll_seconds)
        time.sleep(self.settle_seconds)
        return self.is_active(), False

    def _run_job(self, wanted: bool, check_only: bool) -> dict:
        root = self.root
        prev = (read_status(self.status_path) or {}).get("in_progress")
        if prev:
            self._recover(prev)
        if git(root, "status", "--porcelain", "--untracked-files=no"):
            st = self._status(result="blocked", last_result="blocked: files were changed by hand on the server")
            if wanted:
                self._notify("⚠️ Can't update: some code files on the server were edited by hand. "
                             "Run `desk update` on the server to see the details.")
            return st
        branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
        old = git(root, "rev-parse", "HEAD")
        try:
            git(root, "fetch", "--quiet", "origin", branch)
        except subprocess.CalledProcessError as e:
            msg = redact((e.stderr or str(e)).strip().splitlines()[-1] if (e.stderr or "").strip() else str(e))[-200:]
            prev = read_status(self.status_path) or {}
            st = self._status(result="check_failed", last_check_error=msg, notified_error=prev.get("notified_error"))
            if wanted or st.get("notified_error") != msg:  # tell the owner once per distinct problem
                self._notify(f"⚠️ Can't check GitHub for updates: {msg}\nIf this keeps happening the GitHub "
                             "token may have expired - see 'Good to know' in the server guide. "
                             "Trading continues normally.")
                self._status(notified_error=msg)
            return st
        remote = git(root, "rev-parse", f"origin/{branch}")
        behind = int(git(root, "rev-list", "--count", f"HEAD..origin/{branch}") or 0)
        incoming = git(root, "log", "--format=%s", f"HEAD..origin/{branch}").replace("\n", " · ")
        new_version = self._remote_version(branch) if behind else None
        new_label = f"version {new_version} ({remote[:7]})" if new_version else f"version {remote[:7]}"

        if behind == 0:
            st = self._status(head=old[:7], remote=remote[:7], behind=0, incoming="", incoming_version=None,
                              result="up_to_date", last_check_error=None, version=__version__)
            if wanted:
                self._notify(f"✅ Already running the latest version ({version_string(root)}).")
            else:
                self.say(f"Up to date ({version_string(root)}).")
            return st

        if check_only or not (self.s.auto_update or wanted):
            st = read_status(self.status_path) or {}
            st = self._status(head=old[:7], remote=remote[:7], behind=behind, incoming=incoming,
                              incoming_version=new_version, result="available", notified=st.get("notified"),
                              last_check_error=None, version=__version__)
            if st.get("notified") != remote:  # tell the owner once per new version
                self._notify(f"🆕 New {new_label} is available, {behind} change{'s' if behind != 1 else ''}: {incoming}\n"
                             "Send /update to install it. (Automatic updates are off.)")
                self._status(notified=remote)
            return st

        st = read_status(self.status_path) or {}
        tried = st.get("interrupted") or {}
        if not wanted and tried.get("rev") == remote and tried.get("count", 0) >= 3:
            return self._status(result="paused", head=old[:7], remote=remote[:7], behind=behind, incoming=incoming,
                                incoming_version=new_version)

        # ---- apply ----
        self.say(f"Updating {version_string(root)} -> {new_label}: {incoming}")
        if wanted:
            self._notify(f"⏳ Installing {new_label}: downloading, testing, then restarting the desk. This takes a few "
                         "minutes on this server; you get the result here.")
        self._progress("download", old=old, new=remote, label=new_label)
        try:
            git(root, "pull", "--ff-only", "--quiet", "origin", branch)
        except subprocess.CalledProcessError as e:
            msg = f"download failed: {(e.stderr or str(e)).strip()[-300:]}"
            self._notify(f"❌ Update failed, still running {old[:7]}. {msg}")
            return self._status(result="failed", in_progress=None, last_result=f"{now()[:16]} {msg}")

        self._progress("install")
        ok, out = self._run(self.install_cmd, 900)
        if not ok:
            why = self._rollback(old, f"library install failed: {out[-300:]}")
            self._notify(f"❌ Update failed and was undone. Still running {old[:7]}.\n{why}")
            return self._status(result="failed", in_progress=None, last_result=f"{now()[:16]} {why}", behind=behind)

        # the free server is slow (a fraction of one processor core) and keeps trading while the tests run; the
        # tests get what is left of the job's budget after keeping enough for the restart
        self._progress("tests")
        left = JOB_BUDGET - (self.clock() - self._t0) - self.restart_wait - 120
        ok, out = self._run(self.test_cmd, int(max(300, min(TEST_TIMEOUT, left))))
        if not ok:
            summary = test_summary(out)
            why = self._rollback(old, f"the new version failed its tests: {summary}")
            self._notify(f"❌ Update rejected: the new version failed its automated tests, so the old version "
                         f"({version_string(root)}) stays.\nWhat failed: {summary}\n"
                         "(Full output: `desk logs-update` on the server.)")
            return self._status(result="failed", in_progress=None, last_result=f"{now()[:16]} {why}", behind=behind)

        self._progress("restart")
        restart_at = now()
        try:
            self.restart()
        except (subprocess.SubprocessError, OSError) as e:
            why = self._rollback(old, f"restart command failed: {e}")
            self._notify(f"❌ Update installed but the desk could not be restarted; undone. {why}")
            return self._status(result="failed", in_progress=None, last_result=f"{now()[:16]} {why}", behind=behind)
        self._progress("confirm")
        running, confirmed = self._wait_for_desk(remote, restart_at)
        if not running:
            why = self._rollback(old, "the new version did not start")
            try:
                self.restart()
            except (subprocess.SubprocessError, OSError):
                pass
            self._notify(f"❌ The new version crashed on start, so the previous version ({old[:7]}) was restored "
                         "and restarted. Details are in `desk logs` on the server.")
            return self._status(result="failed", in_progress=None, last_result=f"{now()[:16]} {why}", behind=behind)

        self._notify(f"🔄 Updated to {new_label} and restarted" + ("" if confirmed else
                     " (the service is running; send /health to confirm the new version)") + f".\nWhat changed: {incoming}")
        return self._status(head=remote[:7], remote=remote[:7], behind=0, incoming="", incoming_version=None,
                            result="updated", last_check_error=None, version=new_version or __version__,
                            in_progress=None, interrupted=None,
                            last_result=f"{now()[:16]} updated {__version__} ({old[:7]}) -> {new_label}")


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="trader update")
    ap.add_argument("--check-only", action="store_true", help="report, never install")
    ap.add_argument("--force", action="store_true", help="install even when automatic updates are off")
    args = ap.parse_args(argv)
    s = Settings.load()

    def stopped(*_):
        raise Interrupted()
    signal.signal(signal.SIGTERM, stopped)  # the system's time limit: report it instead of vanishing
    try:
        st = Updater(s).run(force=args.force, check_only=args.check_only)
    except Exception as e:  # never die with a bare traceback: the status file is what /version reads
        print(f"Update check failed: {redact(e)}")
        return 1
    print(f"Result: {st.get('result', 'up to date')}")
    return 0 if st.get("result") not in ("failed", "check_failed") else 1
