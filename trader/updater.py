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


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def redact(text) -> str:
    """git error text can quote the remote URL; never let a token through to Telegram or logs."""
    return re.sub(r"(github_pat_|ghp_)\S+|://[^/\s]+@", lambda m: "<token>" if m.group(1) else "://<token>@", str(text))


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
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          timeout=120).stdout.strip()


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
    st = read_status(status_path)
    if not st:
        lines.append("Automatic updates are not set up on this machine.")
        return "\n".join(lines)
    checked = st.get("checked_at", "")[:16].replace("T", " ")
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
                 restart=None, is_active=None, settle_seconds: int = 20, say=print):
        self.s = s
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
        return p.returncode == 0, (p.stdout + p.stderr)[-1500:]

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
        requested = self.request_path.exists()
        try:
            return self._run_job(force or requested, check_only)
        finally:
            if requested:
                self.request_path.unlink(missing_ok=True)

    def _run_job(self, wanted: bool, check_only: bool) -> dict:
        root = self.root
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

        # ---- apply ----
        self.say(f"Updating {version_string(root)} -> {new_label}: {incoming}")
        try:
            git(root, "pull", "--ff-only", "--quiet", "origin", branch)
        except subprocess.CalledProcessError as e:
            msg = f"download failed: {(e.stderr or str(e)).strip()[-300:]}"
            self._notify(f"❌ Update failed, still running {old[:7]}. {msg}")
            return self._status(result="failed", last_result=f"{now()[:16]} {msg}")

        ok, out = self._run(self.install_cmd, 900)
        if not ok:
            why = self._rollback(old, f"library install failed: {out[-300:]}")
            self._notify(f"❌ Update failed and was undone. Still running {old[:7]}.\n{why}")
            return self._status(result="failed", last_result=f"{now()[:16]} {why}", behind=behind)

        ok, out = self._run(self.test_cmd, 900)
        if not ok:
            why = self._rollback(old, f"the new version failed its tests: {out[-400:]}")
            self._notify(f"❌ Update rejected: the new version failed its automated tests, so the old version "
                         f"({old[:7]}) stays. Details are in `desk logs` on the server.")
            return self._status(result="failed", last_result=f"{now()[:16]} {why}", behind=behind)

        try:
            self.restart()
        except (subprocess.SubprocessError, OSError) as e:
            why = self._rollback(old, f"restart command failed: {e}")
            self._notify(f"❌ Update installed but the desk could not be restarted; undone. {why}")
            return self._status(result="failed", last_result=f"{now()[:16]} {why}", behind=behind)
        time.sleep(self.settle_seconds)
        if not self.is_active():
            why = self._rollback(old, "the new version did not start")
            try:
                self.restart()
            except (subprocess.SubprocessError, OSError):
                pass
            self._notify(f"❌ The new version crashed on start, so the previous version ({old[:7]}) was restored "
                         "and restarted. Details are in `desk logs` on the server.")
            return self._status(result="failed", last_result=f"{now()[:16]} {why}", behind=behind)

        self._notify(f"🔄 Updated to {new_label} and restarted.\nWhat changed: {incoming}")
        return self._status(head=remote[:7], remote=remote[:7], behind=0, incoming="", incoming_version=None,
                            result="updated", last_check_error=None, version=new_version or __version__,
                            last_result=f"{now()[:16]} updated {__version__} ({old[:7]}) -> {new_label}")


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="trader update")
    ap.add_argument("--check-only", action="store_true", help="report, never install")
    ap.add_argument("--force", action="store_true", help="install even when automatic updates are off")
    args = ap.parse_args(argv)
    s = Settings.load()
    try:
        st = Updater(s).run(force=args.force, check_only=args.check_only)
    except Exception as e:  # never die with a bare traceback: the status file is what /version reads
        print(f"Update check failed: {redact(e)}")
        return 1
    print(f"Result: {st.get('result', 'up to date')}")
    return 0 if st.get("result") not in ("failed", "check_failed") else 1
