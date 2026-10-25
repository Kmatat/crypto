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
                say=lambda *a: None)
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
