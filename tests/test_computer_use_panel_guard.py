"""Foreground panel input requires fresh app and window verification under the lock."""
import asyncio
import itertools
import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from conftest import FakeApprovals
from windows_computer_use import server


PID, PANEL, MAIN, OTHER_APP_WINDOW = 4242, 404, 7, 900
PANEL_REFUSAL = (
    f"Foreground input not sent: panel window {PANEL} could not be verified as the top window "
    "of the frontmost app (another window or app may be on top). Read the app's windows "
    "again with computer_list_windows.")
WINDOW_REFUSAL = (
    f"Foreground input not sent: window {PANEL} could not be verified as the front window "
    "(another window or app may be on top). Ask the user to bring the app "
    "to the front, or use background delivery with element_token.")
FRONT_CALLS = [
    ("bring_to_front", {"pid": PID, "window_id": PANEL}),
    ("bring_to_front", {"pid": PID, "window_id": PANEL}),
    ("bring_to_front", {"pid": PID}),
]


def win_row(hwnd=PANEL, owner=MAIN, pid=PID, **updates):
    # PANEL is an owned dialog of MAIN; MAIN (owner 0) is the app's main window.
    return {
        "hwnd": hwnd, "pid": pid, "owner": owner, "visible": True, "cloaked": False,
        "minimized": False, "bounds": (300, 200, 880, 448), "title": "Panel", "class_name": "#32770",
        **updates,
    }


def main_row():
    return win_row(MAIN, 0, title="Main")


@pytest.fixture
def panel_driver(monkeypatch, fake_win32):
    driver = AsyncMock()
    handler = server.ComputerServer(driver, FakeApprovals(), runtime=AsyncMock())
    top = [win_row(), main_row()]
    state = {"snapshots": itertools.repeat(top), "front": itertools.repeat(PANEL), "flags": {},
             "current": {}, "unlocked": []}
    action = CallToolResult(content=[TextContent(type="text", text="input sent")])
    counts = {"windows": 0, "foreground": 0}

    def check_lock():
        # Every OS read happens while the front lock is held.
        if not handler._front_lock.locked():
            state["unlocked"].append(True)

    def top_level_windows():
        check_lock()
        counts["windows"] += 1
        value = next(state["snapshots"])
        if isinstance(value, Exception):
            raise value
        if value is None:
            return None
        state["current"] = {row["hwnd"]: row for row in value}
        return [row["hwnd"] for row in value]

    def foreground_window():
        check_lock()
        counts["foreground"] += 1
        value = next(state["front"])
        if isinstance(value, Exception):
            raise value
        return value

    def window_pid(hwnd):
        row = state["current"].get(hwnd)
        return row["pid"] if row else {PANEL: PID, MAIN: PID, OTHER_APP_WINDOW: 77}.get(hwnd)

    fake_win32.top_level_windows = top_level_windows
    fake_win32.foreground_window = foreground_window
    fake_win32.window_pid = window_pid
    fake_win32.window_info = lambda hwnd: state["current"].get(hwnd)

    async def call(tool, args):
        assert handler._front_lock.locked()
        if tool == "bring_to_front":
            return CallToolResult(content=[], structured_content=state["flags"], is_error=True)
        if tool == "run_steps":
            return CallToolResult(content=[], structured_content={
                "ok": True, "steps": [{"ok": True}]})
        assert tool in ("click", "press_key", "list_windows")
        return action

    async def sleep(delay):
        assert handler._front_lock.locked()
        assert delay == 0.3

    monkeypatch.setattr(handler, "_runtime_driver", AsyncMock(return_value=driver))
    monkeypatch.setattr(server.asyncio, "sleep", AsyncMock(side_effect=sleep))
    driver.call.side_effect = call
    yield handler, driver, state, counts, action
    assert state["unlocked"] == []


def send(handler, tool):
    args = {"pid": PID, "window_id": PANEL}
    if tool == "run_steps":
        args["steps"] = [
            {"press_key": {"key": "n", "delivery_mode": "foreground"}},
            {"press_key": {"key": "return"}},
        ]
    else:
        args.update(delivery_mode="foreground")
        args.update({"x": 1, "y": 2} if tool == "click" else {"key": "n"})
    result = asyncio.run(handler.call("computer_" + tool, args))
    assert not handler._front_lock.locked()
    return result


def assert_refused(result, tool, message):
    assert result.is_error
    if tool == "run_steps":
        report = json.loads(result.content[0].text)
        assert report["failed_step"] == 0
        assert report["steps"] == [{"index": 0, "ok": False,
                                    "code": "foreground_unverified", "message": message}]
    else:
        assert result.content[0].text == message


@pytest.mark.parametrize("tool", ["click", "press_key", "run_steps"])
def test_verified_panel_sends_input_under_lock(panel_driver, tool):
    handler, driver, state, counts, action = panel_driver
    # Even dialog-focus flags must yield to a verified panel target.
    state["flags"] = {"process_activated": True, "focused": False}
    result = send(handler, tool)
    assert not result.is_error
    calls = [entry.args for entry in driver.call.await_args_list]
    assert calls[:3] == FRONT_CALLS
    assert [name for name, _ in calls[3:]] == ([tool] * 2 if tool == "run_steps" else [tool])
    if tool != "run_steps":
        assert result is action
    # One foreground read for the frontmost app, one for the exact window.
    assert counts["foreground"] == 2
    assert counts["windows"] == 2
    server.asyncio.sleep.assert_awaited_once_with(0.3)


@pytest.mark.parametrize("tool", ["click", "press_key", "run_steps"])
@pytest.mark.parametrize("failure", ["other_app", "no_app", "other_window", "offscreen",
                                     "transparent", "list_raises", "list_none", "front_raises"])
def test_panel_verification_failure_never_sends_input(panel_driver, tool, failure):
    handler, driver, state, counts, _ = panel_driver
    top = [win_row(), main_row()]
    fresh = top
    if failure == "other_app":
        state["front"] = itertools.repeat(OTHER_APP_WINDOW)
    elif failure == "no_app":
        state["front"] = itertools.repeat(0)
    elif failure == "other_window":
        fresh = [main_row(), win_row()]
    elif failure == "offscreen":
        fresh = [win_row(visible=False), main_row()]
    elif failure == "transparent":
        fresh = [win_row(cloaked=True), main_row()]
    elif failure == "list_raises":
        fresh = RuntimeError("window list failed")
    elif failure == "list_none":
        fresh = None
    elif failure == "front_raises":
        state["front"] = itertools.repeat(RuntimeError("foreground lookup failed"))
    state["snapshots"] = iter([top, fresh, fresh])
    state["flags"] = {"process_activated": True, "focused": False}
    assert_refused(send(handler, tool), tool, PANEL_REFUSAL)
    assert [entry.args for entry in driver.call.await_args_list] == FRONT_CALLS
    assert counts["windows"] >= 1


@pytest.mark.parametrize("raw", [
    [win_row(pid=77)], [win_row(owner=0)], [win_row(hwnd=999)],
    [win_row(bounds=(300, 200, 20, 20))], [win_row(visible=False)], [win_row(cloaked=True)],
    None, RuntimeError("initial window list failed"),
])
@pytest.mark.parametrize("dialog", [False, True])
def test_ineligible_panel_keeps_existing_refusals(panel_driver, raw, dialog):
    handler, driver, state, counts, _ = panel_driver
    state["snapshots"] = iter([raw])
    if dialog:
        state["flags"] = {"process_activated": True, "focused": False}
    expected = (
        "Foreground input not sent: the app is in front, but another of its windows has focus, "
        f"not window {PANEL} (usually a dialog or panel such as Go to Folder, Open or Save). "
        "Call computer_list_windows for this pid and send the input to that window_id."
    ) if dialog else WINDOW_REFUSAL
    assert_refused(send(handler, "click"), "click", expected)
    assert [entry.args for entry in driver.call.await_args_list] == (
        FRONT_CALLS[:2] + ([("list_windows", {"pid": PID})] if dialog else []))
    assert counts["foreground"] == 0
    assert counts["windows"] == 1


@pytest.mark.parametrize("first_failure", ["app", "window"])
def test_panel_verification_retries_with_fresh_snapshot(panel_driver, first_failure):
    handler, driver, state, counts, _ = panel_driver
    top = [win_row(), main_row()]
    buried = [main_row(), win_row()]
    state["snapshots"] = iter([top, buried if first_failure == "window" else top, top])
    if first_failure == "app":
        state["front"] = iter([OTHER_APP_WINDOW, PANEL, PANEL])
    result = send(handler, "press_key")
    assert not result.is_error
    assert [entry.args for entry in driver.call.await_args_list][:3] == FRONT_CALLS
    assert counts["windows"] == 3
    # Two frontmost-app reads, plus one exact-window read once the first two agree.
    assert counts["foreground"] == 3
    assert server.asyncio.sleep.await_count == 2


def test_top_window_filter_ignores_ineligible_rows(panel_driver):
    handler, _, state, _, _ = panel_driver
    top = [win_row(), main_row()]
    # Another app's window, hidden, cloaked, minimized and owned windows never count as on top.
    ignored = [win_row(90, 0, pid=77), win_row(91, 0, visible=False), win_row(92, 0, cloaked=True),
               win_row(93, 0, minimized=True), win_row(94, MAIN)]
    state["snapshots"] = iter([top, ignored + top])
    assert not send(handler, "click").is_error


def test_each_foreground_batch_step_verifies_panel_again(panel_driver):
    handler, driver, state, _, _ = panel_driver
    top = [win_row(), main_row()]
    buried = [main_row(), win_row()]
    state["snapshots"] = iter([top, top, top, buried, buried])
    result = asyncio.run(handler.call("computer_run_steps", {
        "pid": PID, "window_id": PANEL, "delivery_mode": "foreground",
        "steps": [{"press_key": {"key": "n"}}, {"press_key": {"key": "return"}}]}))
    report = json.loads(result.content[0].text)
    assert result.is_error and report["failed_step"] == 1
    assert report["steps"][0]["ok"] is True
    assert report["steps"][1]["message"] == PANEL_REFUSAL
    calls = [entry.args for entry in driver.call.await_args_list]
    assert calls[:3] == FRONT_CALLS and calls[4:] == FRONT_CALLS
    assert calls[3][0] == "run_steps"
    assert not handler._front_lock.locked()
