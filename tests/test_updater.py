"""Over-the-air updates: fetch, test, restart, roll back, notify."""
import json
import subprocess
import sys

import pytest

from trader.config import Settings
from trader.updater import Updater, read_status, request_update, version_text

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
OK_CMD = [sys.executable, "-c", "pass"]
FAIL_CMD = [sys.executable, "-c", "import sys; sys.exit(1)"]


def git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


@pytest.fixture
def repos(tmp_path):
    """origin (bare) <- server clone; a 'dev' clone publishes new commits."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", origin, dev], check=True, capture_output=True)
    (dev / "app.py").write_text("v1\n")
    git(dev, "add", "."), git(dev, "commit", "-qm", "first version"), git(dev, "push", "-q", "origin", "main")
    server = tmp_path / "server"
    subprocess.run(["git", "clone", "-q", origin, server], check=True, capture_output=True)
    (server / "data").mkdir()
    return dev, server


def publish(dev, text, message):
    (dev / "app.py").write_text(text)
    git(dev, "commit", "-qam", message)
    git(dev, "push", "-q", "origin", "main")
    return git(dev, "rev-parse", "HEAD")


def make(server, tg, *, auto=True, test_cmd=OK_CMD, active=True):
    calls = []
    u = Updater(Settings(auto_update=auto), tg=tg, root=server, status_path=server / "data" / "update-status.json",
                request_path=server / "data" / "update-requested", install_cmd=OK_CMD, test_cmd=test_cmd,
                restart=lambda: calls.append("restart"), is_active=lambda: active, settle_seconds=0,
                say=lambda *a: None, restart_wait=0)
    return u, calls


def test_up_to_date_is_quiet_unless_asked(repos):
    dev, server = repos
    tg = FakeTG()
    u, calls = make(server, tg)
    st = u.run()
    assert st["behind"] == 0 and calls == [] and tg.sent == []
    request_update(u.request_path)
    u.run()
    assert "Already running the latest version" in tg.sent[-1] and not u.request_path.exists()
    assert "Up to date" in version_text(server, u.status_path)


def test_new_code_is_pulled_tested_and_desk_restarted(repos):
    dev, server = repos
    new = publish(dev, "v2\n", "Add exchange stops")
    tg = FakeTG()
    u, calls = make(server, tg)
    st = u.run()
    assert st["result"] == "updated" and calls == ["restart"]
    assert git(server, "rev-parse", "HEAD") == new and (server / "app.py").read_text() == "v2\n"
    assert "Updated to version" in tg.sent[-1] and "Add exchange stops" in tg.sent[-1]
    assert "updated" in st["last_result"] and st["behind"] == 0


def test_failed_tests_roll_back_and_keep_the_desk_running(repos):
    dev, server = repos
    old = git(server, "rev-parse", "HEAD")
    publish(dev, "broken\n", "Bad change")
    tg = FakeTG()
    u, calls = make(server, tg, test_cmd=FAIL_CMD)
    st = u.run()
    assert st["result"] == "failed" and calls == []  # never restarted onto broken code
    assert git(server, "rev-parse", "HEAD") == old and (server / "app.py").read_text() == "v1\n"
    assert "failed its automated tests" in tg.sent[-1]
    assert st["behind"] == 1 and "rolled back" in st["last_result"]


def test_crash_on_start_restores_previous_version(repos):
    dev, server = repos
    old = git(server, "rev-parse", "HEAD")
    publish(dev, "crashy\n", "Crashy change")
    tg = FakeTG()
    u, calls = make(server, tg, active=False)
    st = u.run()
    assert calls == ["restart", "restart"] and git(server, "rev-parse", "HEAD") == old
    assert "crashed on start" in tg.sent[-1] and st["result"] == "failed"


def test_auto_update_off_notifies_once_until_update_is_requested(repos):
    dev, server = repos
    publish(dev, "v2\n", "New feature")
    tg = FakeTG()
    u, calls = make(server, tg, auto=False)
    assert u.run()["result"] == "available" and len(tg.sent) == 1 and "/update" in tg.sent[0]
    u.run()
    assert len(tg.sent) == 1 and calls == []  # not nagging every 30 minutes
    assert "Update available" in version_text(server, u.status_path) and "Send /update" in version_text(server, u.status_path)
    request_update(u.request_path)  # Telegram /update
    st = u.run()
    assert st["result"] == "updated" and calls == ["restart"] and (server / "app.py").read_text() == "v2\n"


def test_hand_edited_files_block_updates(repos):
    dev, server = repos
    publish(dev, "v2\n", "New feature")
    (server / "app.py").write_text("edited by hand\n")
    tg = FakeTG()
    u, calls = make(server, tg)
    assert u.run()["result"] == "blocked" and calls == [] and tg.sent == []  # silent unless asked
    request_update(u.request_path)
    u.run()
    assert "edited by hand" in tg.sent[-1]


def test_status_file_is_readable_by_the_desk(repos):
    dev, server = repos
    tg = FakeTG()
    u, _ = make(server, tg)
    u.run()
    st = read_status(u.status_path)
    assert st["behind"] == 0 and st["auto_update"] is True and json.loads(u.status_path.read_text()) == st
    assert read_status(server / "missing.json") is None


def test_unreachable_github_is_reported_once_and_never_crashes(repos):
    dev, server = repos
    git(server, "remote", "set-url", "origin", "https://x:github_pat_SECRET@github.invalid/nobody/nothing.git")
    tg = FakeTG()
    u, calls = make(server, tg)
    st = u.run()
    assert st["result"] == "check_failed" and calls == [] and len(tg.sent) == 1
    assert "Can't check GitHub" in tg.sent[0] and "github_pat_SECRET" not in tg.sent[0] and "SECRET" not in st["last_check_error"]
    u.run()
    assert len(tg.sent) == 1  # same problem: no repeat nag
    request_update(u.request_path)
    u.run()
    assert len(tg.sent) == 2  # asked explicitly: answer
    assert "last check for updates failed" in version_text(server, u.status_path)


def test_restart_is_requested_through_a_flag_file_not_sudo(tmp_path):
    import threading
    (tmp_path / "data").mkdir()
    u = Updater(Settings(), tg=FakeTG(), root=tmp_path, say=lambda *a: None)
    u.poll_seconds = 0.05
    flag = tmp_path / "data" / "restart-requested"

    def root_helper():  # what the root-owned crypto-desk-restart.service does
        while not flag.exists():
            pass
        flag.unlink()
    threading.Thread(target=root_helper, daemon=True).start()
    u._restart_service()  # returns once the helper consumed the request
    assert not flag.exists()

    u.poll_seconds = 0.01
    with pytest.raises(OSError, match="restart helper"):
        u._restart_service()  # nobody consumes it: fails loudly instead of hanging
    assert not flag.exists()


def test_update_messages_name_the_release_number(repos):
    from trader import __version__
    dev, server = repos
    (dev / "trader").mkdir()
    (dev / "trader" / "__init__.py").write_text('"""pkg"""\n\n__version__ = "9.9.9"\n')
    git(dev, "add", "."), git(dev, "commit", "-qm", "Release 9.9.9"), git(dev, "push", "-q", "origin", "main")
    tg = FakeTG()
    u, calls = make(server, tg, auto=False)
    st = u.run()
    assert st["incoming_version"] == "9.9.9" and "version 9.9.9" in tg.sent[-1]
    assert "Update available: version 9.9.9" in version_text(server, u.status_path)
    assert version_text(server, u.status_path).startswith(f"🏷️ Running version {__version__} (revision ")
    request_update(u.request_path)
    st = u.run()
    assert st["result"] == "updated" and "Updated to version 9.9.9" in tg.sent[-1] and st["version"] == "9.9.9"
    assert f"updated {__version__}" in st["last_result"] and "9.9.9" in st["last_result"]


# ---- 1.7.5: requests from the phone and /update share the job safely ----
def test_settings_wait_for_an_interrupted_update_to_be_undone(repos):
    """A job stopped during the tests left the untested code on disk: the settings restart must not start it."""
    from trader import remote_setup as rs
    from trader.setup_wizard import write_env
    dev, server = repos
    old = git(server, "rev-parse", "HEAD")
    new = publish(dev, "v2\n", "second version")
    git(server, "pull", "-q", "--ff-only")
    data = server / "data"
    (data / "update-status.json").write_text(json.dumps({"in_progress": {"old": old, "new": new, "step": "tests",
                                                                         "label": "version 2"}}))
    (data / "desk-started.json").write_text(json.dumps({"revision": old}))
    write_env(server / ".env", {"STRATEGY": "hourly"})
    rs.write_request(data, {"PULSE_HOURS": "12"})
    request_update(data / "update-requested", purpose="settings")
    tg = FakeTG()
    u, _ = make(server, tg, auto=False)
    heads = []
    u.restart = lambda: heads.append(git(server, "rev-parse", "HEAD"))
    u.run()
    assert heads == [old]                                             # restarted on the code the desk was tested on
    assert any("interrupted" in t and "undone" in t for t in tg.sent)
    assert not any("completed" in t for t in tg.sent)


def test_a_request_written_while_the_job_runs_is_kept_for_the_next_run(repos):
    from trader import remote_setup as rs
    from trader.setup_wizard import write_env
    _, server = repos
    data = server / "data"
    write_env(server / ".env", {"STRATEGY": "hourly"})
    rs.write_request(data, {"PULSE_HOURS": "12"})
    request_update(data / "update-requested", purpose="settings")
    u, _ = make(server, FakeTG())
    u.restart = lambda: request_update(data / "update-requested")    # the owner sends /update during the restart
    u.run()
    assert json.loads((data / "update-requested").read_text())["purpose"] == "update"
    u.restart = lambda: None
    u.run()                                                           # the next run answers it and removes it
    assert not (data / "update-requested").exists()


def test_setup_keeps_a_waiting_update_and_check_only_leaves_requests(repos):
    _, server = repos
    trigger = server / "data" / "update-requested"
    request_update(trigger)
    request_update(trigger, purpose="settings")
    assert json.loads(trigger.read_text())["purpose"] == "update"
    u, _ = make(server, FakeTG())
    u.run(check_only=True)
    assert trigger.exists()


def test_a_trigger_that_is_not_an_object_is_still_answered_and_removed(repos):
    _, server = repos
    trigger = server / "data" / "update-requested"
    trigger.write_text("1")
    tg = FakeTG()
    u, _ = make(server, tg)
    assert u.run()["result"] == "up_to_date"
    assert not trigger.exists() and any("Already running the latest version" in t for t in tg.sent)


def test_status_and_restart_files_replace_a_link_instead_of_writing_through_it(repos, tmp_path):
    _, server = repos
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep\n")
    (server / "data" / "update-status.json").symlink_to(target)
    u, _ = make(server, FakeTG())
    u.run()
    assert target.read_text() == "keep\n" and not (server / "data" / "update-status.json").is_symlink()


def test_a_settings_file_that_does_not_load_keeps_the_update_job_working(tmp_path, monkeypatch):
    from trader import config
    from trader import updater as up
    env = tmp_path / ".env"
    env.write_text("TRADING_MODE=simulation\nCYCLE_MINUTES=15.0\nTELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=1\nAUTO_UPDATE=false\n")
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(config, "ENV_PATH", env)
    monkeypatch.setattr(up, "STATUS", tmp_path / "data" / "update-status.json")
    sent = []
    monkeypatch.setattr(up.Telegram, "send", lambda self, text, **k: sent.append(text) or True)
    s = up.fallback_settings(ValueError("invalid literal for int() with base 10: '15.0'"))
    assert not s.auto_update and s.telegram_chat_id == "1" and len(sent) == 1 and "does not load" in sent[0]
    up.fallback_settings(ValueError("invalid literal for int() with base 10: '15.0'"))
    assert len(sent) == 1                                              # told once per problem
    env.write_text("CYCLE_MINUTES=15.0\n")
    assert up.fallback_settings(ValueError("x")).auto_update          # the owner's choice; on unless turned off
