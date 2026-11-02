"""Lazy ownership of the session lock and the stop hotkey."""
import asyncio
import datetime
import json
import os
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from windows_computer_use import server
from windows_computer_use.standalone import config
from windows_computer_use.standalone.approvals import LocalApprovals
from windows_computer_use.standalone.ownership import Ownership, SessionLock, StateDirError

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

GENERIC = ("Another Claude Code session is using computer use. "
           "It is released 60 s after its last action; try again then, or close that session.")


class FakeLock:
    held = None

    def __init__(self):
        self.mine = False

    def acquire(self):
        if FakeLock.held is not None:
            return False
        FakeLock.held = self
        self.mine = True
        return True

    def close(self):
        if self.mine:
            FakeLock.held = None
            self.mine = False


class FakeHotkey:
    registers = True
    made = []

    def __init__(self):
        self.registered = False
        self.halted_at = None
        self.stopped = False
        FakeHotkey.made.append(self)

    def start(self):
        self.registered = FakeHotkey.registers

    def stop(self):
        self.stopped = True
        self.registered = False


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    FakeLock.held = None
    FakeHotkey.registers = True
    FakeHotkey.made = []
    monkeypatch.setenv("WINDOWS_CU_DIR", str(tmp_path))
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def make(clock, tmp_path):
    created = []

    def build(**kw):
        kw = {"lock_factory": FakeLock, "hotkey_factory": FakeHotkey, "clock": lambda: clock[0],
              "idle_seconds": 60, "owner_path": tmp_path / "server.owner.json", **kw}
        owned = Ownership(**kw)
        created.append(owned)
        return owned

    yield build
    for owned in created:
        owned.close()


def wait_for(condition, seconds=5):
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        time.sleep(0.05)
    return condition()


def test_construction_takes_nothing(make):
    make()
    assert FakeLock.held is None and FakeHotkey.made == []


def test_first_acquire_takes_lock_hotkey_and_writes_owner(make, tmp_path):
    owned = make()
    assert owned.acquire() is None
    assert FakeLock.held is not None and FakeHotkey.made[0].registered
    data = json.loads((tmp_path / "server.owner.json").read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid() and data["parent_pid"] == os.getppid() and data["cwd"] == os.getcwd()
    for key in ("acquired_at", "last_used_at"):
        assert datetime.datetime.fromisoformat(data[key]).utcoffset() == datetime.timedelta(0)
    assert owned.registered is True
    owned.done()


def test_second_ownership_gets_holder_message(make):
    first, second = make(), make()
    assert first.acquire() is None
    first.done()
    message = second.acquire()
    assert message.startswith(f"Another Claude Code session is using computer use (folder: {os.getcwd()}, last action ")
    assert " s ago). It is released 60 s after its last action; try again then, or close that session." in message
    assert len(FakeHotkey.made) == 1


@pytest.mark.parametrize("content", [None, "{", "[]", '{"pid": 1}', '{"pid": 1, "cwd": "x", "last_used_at": "nope"}'])
def test_unreadable_owner_file_gives_generic_message(make, tmp_path, content):
    first, second = make(), make()
    assert first.acquire() is None
    path = tmp_path / "server.owner.json"
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")
    assert second.acquire() == GENERIC


def test_idle_release_frees_lock_for_a_waiting_session(make, clock, tmp_path):
    first, second = make(), make()
    assert first.acquire() is None
    first.done()
    assert second.acquire() is not None
    clock[0] += 61
    assert wait_for(lambda: FakeLock.held is None)
    assert not (tmp_path / "server.owner.json").exists()
    assert FakeHotkey.made[0].stopped
    assert second.acquire() is None


def test_no_release_while_a_call_is_in_flight(make, clock):
    owned = make()
    assert owned.acquire() is None
    clock[0] += 500
    time.sleep(1.5)
    assert FakeLock.held is not None
    owned.done()
    clock[0] += 61
    assert wait_for(lambda: FakeLock.held is None)


def test_reacquire_while_owned_does_not_retake(make):
    owned = make()
    assert owned.acquire() is None and owned.acquire() is None
    assert len(FakeHotkey.made) == 1
    owned.done()
    assert owned._in_flight == 1
    owned.done()


def test_unregistered_hotkey_releases_lock(make, tmp_path):
    FakeHotkey.registers = False
    owned = make()
    assert owned.acquire() == ("The stop hotkey (Ctrl+Alt+Esc) could not be registered; "
                               "computer use was not started.")
    assert FakeLock.held is None
    assert not (tmp_path / "server.owner.json").exists()
    assert owned.registered is True


def test_halt_survives_release_and_reacquire(make):
    owned = make()
    assert owned.acquire() is None
    assert owned.halted_at is None
    FakeHotkey.made[0].halted_at = 12.5
    owned.done()
    owned.release()
    assert FakeLock.held is None and owned.halted_at == 12.5
    assert owned.acquire() is None
    assert FakeHotkey.made[1].halted_at is None and owned.halted_at == 12.5


def test_halt_pressed_just_before_release_is_kept(make):
    owned = make()
    assert owned.acquire() is None
    FakeHotkey.made[0].halted_at = 3.0
    owned.release()
    assert owned.halted_at == 3.0


def test_halted_owner_keeps_the_lock_past_idle(make, clock):
    first, second = make(), make()
    assert first.acquire() is None
    FakeHotkey.made[0].halted_at = 5.0
    first.done()
    clock[0] += 500
    time.sleep(2.5)
    assert FakeLock.held is not None and not FakeHotkey.made[0].stopped
    assert second.acquire().startswith("Another Claude Code session is using computer use (folder:")
    first.close()
    assert FakeLock.held is None


class PressDuringStopHotkey(FakeHotkey):
    def stop(self):
        super().stop()
        self.halted_at = 9.0


def test_halt_press_arriving_during_release_is_kept(make):
    owned = make(hotkey_factory=PressDuringStopHotkey)
    assert owned.acquire() is None
    owned.release()
    assert owned.halted_at == 9.0


class ExplodingHotkey(FakeHotkey):
    def __init__(self):
        raise RuntimeError("no hotkey")


class StartRaisingHotkey(FakeHotkey):
    def start(self):
        raise RuntimeError("start failed")


@pytest.mark.parametrize("factory", [ExplodingHotkey, StartRaisingHotkey])
def test_hotkey_that_raises_leaves_the_lock_free(make, factory, tmp_path):
    owned = make(hotkey_factory=factory)
    assert owned.acquire() == ("The stop hotkey (Ctrl+Alt+Esc) could not be registered; "
                               "computer use was not started.")
    assert FakeLock.held is None
    assert not (tmp_path / "server.owner.json").exists()


class UnwritableLock(FakeLock):
    def acquire(self):
        raise StateDirError("C:/nowhere")


def test_unusable_state_folder_is_not_reported_as_busy(make):
    owned = make(lock_factory=UnwritableLock)
    message = owned.acquire()
    assert message == ("Computer use cannot use its state folder (C:/nowhere); "
                       "check that it exists and is writable.")
    assert FakeHotkey.made == []


@windows_only
def test_session_lock_raises_state_dir_error_under_a_regular_file(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(config, "STATE_DIR", blocker / "state")
    with pytest.raises(StateDirError):
        SessionLock().acquire()


def test_release_deletes_owner_file_only_if_it_names_our_pid(make, tmp_path):
    path = tmp_path / "server.owner.json"
    owned = make()
    assert owned.acquire() is None
    path.write_text(json.dumps({"pid": os.getpid() + 1, "cwd": "x", "last_used_at": "2026-01-01T00:00:00+00:00"}),
                    encoding="utf-8")
    owned.release()
    assert path.exists()
    assert owned.acquire() is None
    owned.release()
    assert not path.exists()


def test_done_refreshes_last_used(make, tmp_path):
    owned = make()
    assert owned.acquire() is None
    path = tmp_path / "server.owner.json"
    path.write_text(json.dumps({"pid": os.getpid(), "last_used_at": "old"}), encoding="utf-8")
    owned.done()
    assert json.loads(path.read_text(encoding="utf-8"))["cwd"] == os.getcwd()


def test_close_stops_thread_and_releases(make):
    owned = make()
    assert owned.acquire() is None
    owned.done()
    owned.close()
    assert FakeLock.held is None and not owned._thread.is_alive()


@windows_only
def test_real_lock_is_shared_between_two_ownerships(tmp_path):
    kw = {"hotkey_factory": FakeHotkey, "owner_path": tmp_path / "server.owner.json"}
    first, second = Ownership(**kw), Ownership(**kw)
    try:
        assert first.acquire() is None
        first.done()
        assert second.acquire().startswith("Another Claude Code session is using computer use")
        first.release()
        assert second.acquire() is None
    finally:
        first.close()
        second.close()


@windows_only
def test_session_lock_excludes_second_session_and_releases():
    first, second = SessionLock(), SessionLock()
    try:
        assert first.acquire()
        assert not second.acquire()
        first.close()
        assert second.acquire()
    finally:
        first.close()
        second.close()


def journal_rows():
    return [json.loads(line) for line in config.journal_path().read_text(encoding="utf-8").splitlines()]


def test_preflight_journals_the_busy_refusal(make):
    holder, other = make(), make()
    assert holder.acquire() is None
    approvals = LocalApprovals(None, other, ownership=other)
    refusal = approvals.preflight()
    assert refusal.startswith("Another Claude Code session is using computer use")
    rows = journal_rows()
    assert rows[0]["tool"] == "preflight" and rows[0]["decision"] == "deny" and rows[0]["reason"] == refusal


def test_preflight_acquires_and_done_forwards(make):
    owned = make()
    approvals = LocalApprovals(None, owned, ownership=owned)
    assert FakeLock.held is None
    assert approvals.preflight() is None and FakeLock.held is not None
    approvals.done()
    assert owned._in_flight == 0
    LocalApprovals(None, FakeHotkey()).done()


def test_startup_refusal_comes_before_ownership(make):
    owned = make()
    assert LocalApprovals("refused", owned, ownership=owned).preflight() == "refused"
    assert FakeLock.held is None


class CountingApprovals:
    accepts_confirm = True

    def __init__(self, approved=True, preflight=None):
        self.done_calls = 0
        self._preflight = preflight
        self.request = AsyncMock(return_value={"approved": approved, "error": "no"})
        self.batch_stop = AsyncMock(return_value=None)

    def preflight(self):
        return self._preflight

    def done(self):
        self.done_calls += 1


def make_server(approvals, runtime=None):
    driver = SimpleNamespace(binary="fake", socket="fake", close=AsyncMock(),
                             call=AsyncMock(return_value=CallToolResult(content=[])))
    return server.ComputerServer(driver, approvals, runtime=runtime or AsyncMock(return_value=("fake", "fake")))


def test_server_call_marks_done_on_success(monkeypatch):
    monkeypatch.setattr(server, "_merge_running_apps", AsyncMock(side_effect=lambda result: result))
    approvals = CountingApprovals()
    result = asyncio.run(make_server(approvals).call("computer_list_apps", {}))
    assert not result.is_error
    assert approvals.done_calls == 1


def test_server_call_marks_done_on_refusal_after_preflight():
    approvals = CountingApprovals(approved=False)
    result = asyncio.run(make_server(approvals).call("computer_list_apps", {}))
    assert result.is_error
    assert approvals.done_calls == 1
    invalid = CountingApprovals()
    assert asyncio.run(make_server(invalid).call("not_a_tool", {})).is_error
    assert invalid.done_calls == 1


def test_server_call_marks_done_on_exception():
    approvals = CountingApprovals()
    broken = make_server(approvals, runtime=AsyncMock(side_effect=RuntimeError("boom")))
    broken.driver = None
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(broken.call("computer_list_apps", {}))
    assert approvals.done_calls == 1


def test_server_call_without_done_still_works():
    approvals = SimpleNamespace(preflight=lambda: None, accepts_confirm=True,
                                request=AsyncMock(return_value={"approved": False, "error": "no"}),
                                batch_stop=AsyncMock(return_value=None))
    assert asyncio.run(make_server(approvals).call("computer_list_apps", {})).is_error


def test_server_call_skips_done_when_preflight_refuses():
    approvals = CountingApprovals(preflight="refused")
    assert asyncio.run(make_server(approvals).call("computer_list_apps", {})).is_error
    assert approvals.done_calls == 0
