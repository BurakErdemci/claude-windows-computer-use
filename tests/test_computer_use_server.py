"""Proxy tests use only a fake stdio driver, a fake Windows OS layer and fake approvals."""
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from conftest import NOTEPAD_ID, FakeApprovals
from windows_computer_use.driver_client import DriverClient, DriverError, DriverUnavailable
from windows_computer_use.server import ComputerServer, create_server
from windows_computer_use import driver_client, platform, server


def landed(window_id=7):
    # The Windows driver's bring_to_front report for a window that reached the foreground.
    handle = hex(window_id)
    return {"landed_on_target": True, "now_fg_hwnd": handle, "target_hwnd": handle}


def handler_for(driver, approval=None, stop=None):
    runtime = AsyncMock(return_value=(driver.binary, driver.socket))
    return ComputerServer(driver, FakeApprovals(approval, stop), runtime=runtime)


@pytest.fixture
def fake(tmp_path, monkeypatch, fake_win32):
    # At most two child processes in the reconnect test, one in all others.
    fixture = Path(__file__).parent / "fixtures" / "fake_cua_driver.py"
    transport = driver_client._transport

    def fake_transport(command, env):
        child_env = dict(env)
        if "SYSTEMROOT" in os.environ:
            child_env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        return transport([sys.executable, str(fixture), *command[1:]], child_env)

    monkeypatch.setattr(driver_client, "_transport", fake_transport)
    executable = Path(sys.executable)
    log = tmp_path / "calls.jsonl"
    socket = str(tmp_path / "fake-private-socket")
    driver = DriverClient(str(executable), socket)
    # Test-only instrumentation is not inherited by the production client.
    driver.env["FAKE_CUA_LOG"] = str(log)
    # Window 7 is a real foreground window of pid 4242 as far as the OS facts go.
    fake_win32.foreground = 7
    fake_win32.pids[7] = 4242
    real_call = driver.call

    async def call(tool, args):
        result = await real_call(tool, args)
        if tool == "bring_to_front" and not result.is_error and type(args.get("window_id")) is int:
            # The shared fake driver reports the macOS shape; translate a success to the Windows one.
            return CallToolResult(content=[], structured_content=landed(args["window_id"]))
        return result

    driver.call = call
    approval = AsyncMock(return_value={"approved": True})
    return driver, log, approval


def calls(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_image_is_forwarded_and_window_snapshot_survives_later_call(fake):
    driver, log, approval = fake

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call("computer_window_state", {"pid": 4242, "window_id": 7})
            assert result.content[0].text == "snapshot"
            assert result.content[1].data == "aGVsbG8="
            assert result.content[1].mime_type == "image/png"
            click = await handler.call("computer_click", {"pid": 4242, "window_id": 7, "x": 1, "y": 2})
            assert not click.is_error
        finally:
            await handler.close()
    asyncio.run(run())
    assert len({row["process"] for row in calls(log)}) == 1
    assert [row["tool"] for row in calls(log)] == ["get_window_state", "click"]
    params = approval.call_args.kwargs["params"]
    assert params["bundle_id"] == NOTEPAD_ID and params["app_name"] == "notepad"


@pytest.mark.parametrize("tool,args", [
    ("computer_click", {"pid": 4242, "debug_image_out": "output"}),
    ("computer_type_text", {"pid": 4242, "text": "x", "scope": "desktop"}),
    ("computer_click", {"pid": 4242, "app_name": "Trusted"}),
    ("computer_run_steps", {"pid": 4242, "steps": [{"click": {}, "expect": [{"app": "Terminal"}]}]}),
])
def test_forbidden_calls_touch_neither_driver_nor_approval(fake, tool, args):
    driver, log, approval = fake
    result = asyncio.run(handler_for(driver, approval).call(tool, args))
    assert result.is_error
    assert calls(log) == []
    approval.assert_not_called()


@pytest.mark.parametrize("answer", [False, "true", "false", 1, None])
def test_denied_approval_does_not_forward_action(fake, answer):
    driver, log, approval = fake
    approval.return_value = {"approved": answer, "error": "denied"}

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call("computer_click", {"pid": 4242, "element_token": "s0000002a:1"})
            assert result.is_error and result.content[0].text == "denied"
        finally:
            await handler.close()
    asyncio.run(run())
    assert calls(log) == []


def test_denied_discovery_never_connects(fake):
    driver, log, approval = fake
    approval.return_value = {"approved": False}
    assert asyncio.run(handler_for(driver, approval).call("computer_list_apps", {})).is_error
    assert calls(log) == []


@pytest.mark.parametrize("approved", [True, False])
def test_bring_to_front_requires_foreground_approval_before_forwarding(fake, approved):
    driver, log, approval = fake
    approval.return_value = {"approved": approved, "error": "denied"}
    args = {"pid": 4242, "window_id": 7}

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call("computer_bring_to_front", args)
            assert bool(result.is_error) is not approved
            if not approved:
                assert result.content[0].text == "denied"
        finally:
            await handler.close()
    asyncio.run(run())
    approval.assert_awaited_once()
    assert approval.call_args.args == ("computer_bring_to_front",)
    params = approval.call_args.kwargs["params"]
    assert params["foreground"] is True
    assert params["bundle_id"] == NOTEPAD_ID
    assert params["pid"] == 4242 and params["window_id"] == 7
    rows = calls(log)
    if approved:
        assert [row["tool"] for row in rows] == ["bring_to_front"]
        assert rows[-1]["args"] == args
    else:
        assert rows == []


@pytest.mark.parametrize("front,window_id,expected_tools,refusal", [
    (None, 7, ["bring_to_front", "click"], None),
    ("1", 7, ["bring_to_front", "bring_to_front"], "could not be verified as the front window"),
    ("once", 7, ["bring_to_front", "bring_to_front", "click"], None),
    (None, None, [], "Foreground input needs window_id; nothing was sent."),
])
def test_foreground_click_verifies_window_before_input(fake, front, window_id, expected_tools, refusal):
    driver, log, approval = fake
    if front is not None:
        driver.env["FAKE_CUA_FRONT_UNVERIFIED"] = front
    args = {"pid": 4242, "element_token": "s0000002a:1", "delivery_mode": "foreground"}
    if window_id is not None:
        args["window_id"] = window_id

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call("computer_click", args)
            assert bool(result.is_error) is (refusal is not None)
            if refusal:
                assert refusal in result.content[0].text
        finally:
            await handler.close()
    asyncio.run(run())
    rows = calls(log)
    assert [row["tool"] for row in rows] == expected_tools
    assert all(row["args"] == {"pid": 4242, "window_id": 7}
               for row in rows if row["tool"] == "bring_to_front")


def test_foreground_click_only_fronts_target_without_frontmost_lookup(fake, monkeypatch):
    driver, log, _ = fake
    driver.env["FAKE_CUA_ACTIVE"] = "77"
    frontmost = AsyncMock(return_value=77)
    monkeypatch.setattr(driver, "frontmost_pid", frontmost)

    async def run():
        handler = handler_for(driver)
        try:
            result = await handler.call("computer_click", {
                "pid": 4242, "window_id": 7, "element_token": "s0000002a:1",
                "delivery_mode": "foreground"})
            assert not result.is_error
        finally:
            await handler.close()
    asyncio.run(run())
    rows = calls(log)
    assert [row["tool"] for row in rows] == [
        "bring_to_front", "click"]
    assert [row["args"] for row in rows if row["tool"] == "bring_to_front"] == [
        {"pid": 4242, "window_id": 7}]
    frontmost.assert_not_called()


@pytest.mark.parametrize("action_error", [False, True])
def test_fronted_call_preserves_action_result_and_holds_lock(fake_win32, action_error):
    fake_win32.foreground = 7
    fake_win32.pids[7] = 4242
    driver = AsyncMock()
    action = CallToolResult(content=[TextContent(type="text", text="action result")], is_error=action_error)
    guard = CallToolResult(content=[], structured_content=landed())
    handler = handler_for(driver)

    async def call(tool, args):
        assert handler._front_lock.locked()
        if tool == "click":
            return action
        assert tool == "bring_to_front" and args == {"pid": 4242, "window_id": 7}
        return guard

    driver.call.side_effect = call
    refusal, result = asyncio.run(handler._fronted_call(driver, 4242, 7, "click", {"pid": 4242}))
    assert refusal is None and result is action
    assert [entry.args for entry in driver.call.await_args_list] == [
        ("bring_to_front", {"pid": 4242, "window_id": 7}),
        ("click", {"pid": 4242})]
    driver.frontmost_pid.assert_not_called()
    assert not handler._front_lock.locked()


def test_background_click_never_fronts_window(fake):
    driver, log, approval = fake
    driver.env["FAKE_CUA_FRONT_UNVERIFIED"] = "1"

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call("computer_click", {
                "pid": 4242, "window_id": 7, "element_token": "s0000002a:1",
                "delivery_mode": "background"})
            assert not result.is_error
        finally:
            await handler.close()
    asyncio.run(run())
    assert [row["tool"] for row in calls(log)] == ["click"]


@pytest.mark.parametrize("shorthand", [False, True])
def test_split_steps_stop_before_unverified_foreground_step(fake, shorthand):
    driver, log, approval = fake
    driver.env["FAKE_CUA_FRONT_UNVERIFIED"] = "1"
    foreground = {"window_id": 7, "delivery_mode": "foreground"}
    step = {"click": foreground} if shorthand else {"tool": "click", "args": foreground}
    steps = [{"type_text": {"text": "hello"}}, step]

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call("computer_run_steps", {"pid": 4242, "window_id": 7, "steps": steps})
            assert result.is_error
            report = json.loads(result.content[0].text)
            assert report["failed_step"] == 1 and report["ok"] is False
            assert report["steps"][1] == {
                "index": 1, "ok": False, "code": "foreground_unverified",
                "message": "Foreground input not sent: window 7 could not be verified as the front window "
                           "(another window or app may be on top). Ask the user to bring "
                           "the app to the front, or use background delivery with element_token."}
        finally:
            await handler.close()
    asyncio.run(run())
    rows = calls(log)
    assert [row["tool"] for row in rows] == [
        "run_steps", "bring_to_front", "bring_to_front"]
    assert rows[0]["args"]["steps"] == steps[:1]
    assert all(row["args"] == {"pid": 4242, "window_id": 7}
               for row in rows if row["tool"] == "bring_to_front")


def test_split_steps_verify_each_inherited_foreground_window(fake):
    driver, log, approval = fake

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call("computer_run_steps", {
                "pid": 4242, "window_id": 7, "delivery_mode": "foreground",
                "steps": [{"click": {}}, {"type_text": {"text": "hello"}}]})
            assert not result.is_error
        finally:
            await handler.close()
    asyncio.run(run())
    rows = calls(log)
    assert [row["tool"] for row in rows] == [
        "bring_to_front", "run_steps", "bring_to_front", "run_steps"]
    assert [row["args"] for row in rows if row["tool"] == "bring_to_front"] == [
        {"pid": 4242, "window_id": 7},
        {"pid": 4242, "window_id": 7}]


@pytest.fixture
def batch_driver(monkeypatch, fake_win32):
    driver = AsyncMock(binary="bin", socket="sock")
    handler = handler_for(driver)
    state = {"active": 77, "steps": 0, "guards": 0, "refuse": None,
             "error": None, "unreadable": False, "failed": False,
             "locked": True}
    events = []

    async def frontmost():
        assert handler._front_lock.locked()
        events.append(("frontmost", state["active"]))
        return state["active"]

    async def call(tool, args):
        assert handler._front_lock.locked() is state["locked"]
        events.append((tool, args))
        if tool == "bring_to_front":
            assert args["pid"] == 4242 and "window_id" in args
            state["guards"] += 1
            verified = state["guards"] < state["refuse"] if state["refuse"] else True
            report = {**landed(args["window_id"]), "landed_on_target": verified}
            if verified:
                state["active"] = args["pid"]
                fake_win32.foreground = args["window_id"]
                fake_win32.pids[args["window_id"]] = args["pid"]
            return CallToolResult(content=[], structured_content=report)
        if tool == "run_steps":
            state["steps"] += 1
            if state["error"] == state["steps"]:
                raise DriverError("lost")
            if state["unreadable"]:
                return CallToolResult(content=[TextContent(type="text", text="invalid")])
            return CallToolResult(content=[], structured_content={
                "ok": not state["failed"], "steps": [{"ok": not state["failed"]}]})
        assert tool == "click"
        return CallToolResult(content=[TextContent(type="text", text="clicked")])

    driver.frontmost_pid.side_effect = frontmost
    driver.call.side_effect = call
    monkeypatch.setattr(server.asyncio, "sleep", AsyncMock())
    return handler, driver, state, events


def foreground_batch():
    return {"pid": 4242, "window_id": 7, "delivery_mode": "foreground", "steps": [
        {"tool": "press_key", "args": {"key": "return"}},
        {"tool": "type_text", "args": {"text": "New name"}},
        {"tool": "press_key", "args": {"key": "return"}},
    ]}


def test_batch_holds_foreground_until_all_three_steps_finish(batch_driver):
    handler, driver, state, events = batch_driver
    result = asyncio.run(handler.call("computer_run_steps", foreground_batch()))
    assert not result.is_error
    assert json.loads(result.content[0].text) == {
        "ok": True, "executed": 3, "total": 3, "failed_step": None,
        "steps": [{"ok": True, "index": i} for i in range(3)]}
    assert [tool for tool, _ in events] == [
        "bring_to_front", "run_steps", "bring_to_front", "run_steps",
        "bring_to_front", "run_steps"]
    assert [args for tool, args in events if tool == "bring_to_front"] == [
        {"pid": 4242, "window_id": 7}] * 3
    driver.frontmost_pid.assert_not_called()
    assert state["active"] == 4242
    assert not handler._front_lock.locked()


@pytest.mark.parametrize("ending,steps,guards", [
    ("error", 2, 2),
    ("refused_second", 1, 3),
    ("refused_first", 0, 2),
    ("halted", 1, 1),
    ("unreadable", 1, 1),
    ("failed", 1, 1),
])
def test_batch_preserves_early_exit_without_fronting_another_app(batch_driver, monkeypatch, ending, steps, guards):
    handler, driver, state, events = batch_driver
    if ending == "error":
        state["error"] = 2
    elif ending.startswith("refused"):
        state["refuse"] = 2 if ending == "refused_second" else 1
    elif ending == "halted":
        monkeypatch.setattr(handler.approvals, "batch_stop", AsyncMock(side_effect=[None, "halted"]))
    else:
        state[ending] = True
    result = asyncio.run(handler.call("computer_run_steps", foreground_batch()))
    assert result.is_error
    assert state["steps"] == steps and state["guards"] == guards
    assert [args for tool, args in events if tool == "bring_to_front"] == [
        {"pid": 4242, "window_id": 7}] * guards
    if ending == "error":
        assert result.content[0].text == "The connection to the computer-use driver was lost; the action was not repeated."
    elif ending.startswith("refused"):
        report = json.loads(result.content[0].text)
        assert report["failed_step"] == steps
        assert report["steps"][-1]["code"] == "foreground_unverified"
    elif ending == "halted":
        assert json.loads(result.content[0].text)["stop_reason"] == "halted"
    elif ending == "unreadable":
        assert result.content[0].text == "The result of step 1/3 could not be read; the remaining steps were not run."
    driver.frontmost_pid.assert_not_called()
    assert not handler._front_lock.locked()


def test_batch_preserves_report_and_leaves_target_in_front(batch_driver):
    handler, _, state, events = batch_driver
    result = asyncio.run(handler.call("computer_run_steps", foreground_batch()))
    assert not result.is_error
    assert json.loads(result.content[0].text)["executed"] == 3
    assert [args for tool, args in events if tool == "bring_to_front"] == [
        {"pid": 4242, "window_id": 7}] * 3
    assert state["active"] == 4242
    assert not handler._front_lock.locked()


def test_batch_background_avoids_front_lock_and_lookup(batch_driver):
    handler, driver, state, events = batch_driver
    state["locked"] = False
    args = foreground_batch()
    del args["delivery_mode"]

    class UnusedLock:
        def locked(self):
            return False

        async def __aenter__(self):
            pytest.fail("background batch acquired the front lock")

        async def acquire(self):
            pytest.fail("background batch acquired the front lock")

    handler._front_lock = UnusedLock()
    result = asyncio.run(handler.call("computer_run_steps", args))
    assert not result.is_error
    driver.frontmost_pid.assert_not_awaited()
    assert [tool for tool, _ in events] == ["run_steps"] * 3


def test_mixed_batch_holds_lock_through_last_background_step(batch_driver):
    handler, driver, state, events = batch_driver
    args = foreground_batch()
    del args["delivery_mode"]
    args["window_id"] = 9
    args["steps"][1]["args"].update(delivery_mode="foreground", window_id=9)
    args.update(delay_ms=20, observe={"include_screenshot": True})
    result = asyncio.run(handler.call("computer_run_steps", args))
    assert not result.is_error
    assert [tool for tool, _ in events] == [
        "run_steps", "bring_to_front", "run_steps", "run_steps"]
    assert [args for tool, args in events if tool == "bring_to_front"] == [
        {"pid": 4242, "window_id": 9}]
    step_calls = [args for tool, args in events if tool == "run_steps"]
    assert "observe" not in step_calls[0] and "observe" not in step_calls[1]
    assert step_calls[2]["observe"] == {"include_screenshot": True}
    assert server.asyncio.sleep.await_count == 2
    server.asyncio.sleep.assert_awaited_with(0.02)
    driver.frontmost_pid.assert_not_called()
    assert state["active"] == 4242
    assert not handler._front_lock.locked()


def test_single_foreground_click_preserves_result_and_leaves_target_in_front(batch_driver):
    handler, driver, state, events = batch_driver
    result = asyncio.run(handler.call("computer_click", {
        "pid": 4242, "window_id": 7, "delivery_mode": "foreground", "x": 1, "y": 2}))
    assert not result.is_error and result.content[0].text == "clicked"
    assert [tool for tool, _ in events] == ["bring_to_front", "click"]
    assert events[0] == ("bring_to_front", {"pid": 4242, "window_id": 7})
    assert state["active"] == 4242
    driver.frontmost_pid.assert_not_called()
    assert not handler._front_lock.locked()


@pytest.mark.parametrize("unverified", [False, True])
def test_single_step_foreground_fallback_is_guarded(fake, unverified):
    driver, log, approval = fake
    if unverified:
        driver.env["FAKE_CUA_FRONT_UNVERIFIED"] = "1"

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call("computer_run_steps", {
                "pid": 4242, "window_id": 7, "foreground_fallback": True,
                "steps": [{"click": {}}]})
            assert bool(result.is_error) is unverified
        finally:
            await handler.close()
    asyncio.run(run())
    expected = ["bring_to_front"] if unverified else ["run_steps"]
    assert [row["tool"] for row in calls(log)] == [
        "bring_to_front", *expected]


@pytest.mark.parametrize("window_id", [None, True, False, "7", 7.0])
def test_front_guard_requires_integer_window_without_driver_call(window_id):
    driver = AsyncMock()
    assert asyncio.run(server._front_guard(driver, 4242, window_id)) == (
        "Foreground input needs window_id; nothing was sent.")
    driver.call.assert_not_awaited()


@pytest.mark.parametrize("body,structured,error,verified", [
    (json.dumps(landed()), None, False, True),
    ("invalid", landed(), False, True),
    (json.dumps(landed()), {}, False, False),
    (json.dumps(landed()), None, True, False),
    ("invalid", None, False, False),
    ("[]", None, False, False),
    ('{"landed_on_target": null}', None, False, False),
    (json.dumps({**landed(), "landed_on_target": 1}), None, False, False),
    (json.dumps({**landed(), "now_fg_hwnd": "0x8"}), None, False, False),
])
def test_front_guard_accepts_only_explicit_verified_result(monkeypatch, fake_win32, body, structured, error, verified):
    fake_win32.foreground = 7
    fake_win32.pids[7] = 4242
    driver = AsyncMock()
    driver.call.return_value = CallToolResult(
        content=[TextContent(type="text", text=body)], structured_content=structured, is_error=error)
    sleep = AsyncMock()
    monkeypatch.setattr(server.asyncio, "sleep", sleep)
    result = asyncio.run(server._front_guard(driver, 4242, 7))
    assert (result is None) is verified
    assert driver.call.await_count == (1 if verified else 2)
    for call in driver.call.await_args_list:
        assert call.args == ("bring_to_front", {"pid": 4242, "window_id": 7})
    if verified:
        sleep.assert_not_awaited()
    else:
        sleep.assert_awaited_once_with(0.3)


@pytest.mark.parametrize("structured,text,expected", [
    ({"nested": [{"process_activated": True, "focused": False}]}, "", (True, False)),
    (None, '{"nested": [{"process_activated": true, "focused": false}]}', (True, False)),
    (None, "process_activated=true, focused=false", (True, False)),
    ({"process_activated": False}, "process_activated=true, focused=false", (False, False)),
    ({"process_activated": "true", "focused": 0}, "not_focused=false", (None, None)),
    (None, "invalid", (None, None)),
])
def test_front_flags_read_boolean_values_before_text(structured, text, expected):
    result = CallToolResult(content=[TextContent(type="text", text="invalid"),
                                     TextContent(type="text", text=text)], structured_content=structured)
    assert server._front_flags(result) == expected


@pytest.fixture
def dialog_driver(monkeypatch, fake_win32):
    driver = AsyncMock(binary="bin", socket="sock")
    driver.frontmost_pid.return_value = 77
    monkeypatch.setattr(server.asyncio, "sleep", AsyncMock())
    return driver


def dialog_refusal(title="Go to Folder"):
    return (
        f'Foreground input not sent: the app is in front, but its window 8 ("{title}") has focus, '
        'not window 7. This is usually a dialog or panel such as Go to Folder, Open or Save. '
        'Send the input to window_id 8 instead.')


@pytest.mark.parametrize("flags_source", ["text", "structured", "json"])
@pytest.mark.parametrize("rows_source", ["text", "structured", "both"])
def test_dialog_refusal_names_highest_eligible_window(dialog_driver, flags_source, rows_source):
    driver = dialog_driver
    flags = {"effect": [{"process_activated": True, "focused": False}]}
    guard = CallToolResult(content=[TextContent(type="text", text=(
        json.dumps(flags) if flags_source == "json" else "process_activated=true, focused=false"))],
        structured_content=flags if flags_source == "structured" else None, is_error=True)
    rows = [None, {"window_id": 7, "z_index": 100},
            {"window_id": 9, "pid": 4242, "z_index": 2},
            {"window_id": 8, "pid": 4242, "title": "Go to Folder", "z_index": 5},
            {"window_id": 10, "pid": 99, "z_index": 100},
            {"window_id": 11, "on_current_space": False, "z_index": 100},
            {"window_id": 12, "z_index": None}, {"window_id": True, "z_index": 100},
            {"window_id": "13", "z_index": 100}, {"window_id": 14, "z_index": True}]
    windows = CallToolResult(
        content=[TextContent(type="text", text="invalid"), TextContent(type="text", text=json.dumps(
            [{"window_id": 8, "title": "duplicate", "z_index": 200}] if rows_source == "both" else rows))]
        if rows_source != "structured" else [],
        structured_content={"windows": rows} if rows_source != "text" else None)
    driver.call.side_effect = [guard, guard, windows]
    result = asyncio.run(handler_for(driver).call("computer_press_key", {
        "pid": 4242, "window_id": 7, "key": "return", "delivery_mode": "foreground"}))
    assert result.is_error and result.content[0].text == dialog_refusal()
    assert [call.args for call in driver.call.await_args_list] == [
        ("bring_to_front", {"pid": 4242, "window_id": 7}),
        ("bring_to_front", {"pid": 4242, "window_id": 7}), ("list_windows", {"pid": 4242})]


@pytest.mark.parametrize("windows", [
    RuntimeError("lookup failed"), CallToolResult(content=[]),
    CallToolResult(content=[], structured_content={"windows": [{"window_id": 7, "z_index": 4}]}),
    CallToolResult(content=[TextContent(type="text", text='[{"window_id": 8, "z_index": null}]')]),
    CallToolResult(content=[], structured_content={"windows": [
        {"window_id": 8, "pid": 99, "z_index": 9},
        {"window_id": 9, "on_current_space": False, "z_index": 10}]}),
])
def test_dialog_refusal_handles_unavailable_windows(dialog_driver, windows):
    guard = CallToolResult(content=[TextContent(type="text", text="process_activated=true, focused=false")])
    dialog_driver.call.side_effect = [guard, guard, windows]
    refusal, result = asyncio.run(handler_for(dialog_driver)._fronted_call(
        dialog_driver, 4242, 7, "press_key", {"key": "return"}))
    assert result is None
    assert refusal == (
        "Foreground input not sent: the app is in front, but another of its windows has focus, "
        "not window 7 (usually a dialog or panel such as Go to Folder, Open or Save). "
        "Call computer_list_windows for this pid and send the input to that window_id.")
    assert [call.args[0] for call in dialog_driver.call.await_args_list] == [
        "bring_to_front", "bring_to_front", "list_windows"]


@pytest.mark.parametrize("title,expected", [(None, "untitled"), ("", "untitled"), (12, "untitled"),
                                           ("x" * 90, "x" * 80)])
def test_dialog_refusal_normalizes_title(dialog_driver, title, expected):
    guard = CallToolResult(content=[], structured_content={"process_activated": True, "focused": False})
    windows = CallToolResult(content=[TextContent(type="text", text=json.dumps(
        {"windows": [{"window_id": 8, "title": title, "z_index": 1}]}))])
    dialog_driver.call.side_effect = [guard, guard, windows]
    assert asyncio.run(server._front_guard(dialog_driver, 4242, 7)) == dialog_refusal(expected)


@pytest.mark.parametrize("last_flags", ["process_activated=false, focused=false",
                                        "process_activated=true, focused=true", "unknown"])
def test_front_guard_uses_only_last_attempt_flags(dialog_driver, last_flags):
    dialog_driver.call.side_effect = [
        CallToolResult(content=[TextContent(type="text", text="process_activated=true, focused=false")]),
        CallToolResult(content=[TextContent(type="text", text=last_flags)])]
    assert asyncio.run(server._front_guard(dialog_driver, 4242, 7)) == (
        "Foreground input not sent: window 7 could not be verified as the front window "
        "(another window or app may be on top). Ask the user to bring the app "
        "to the front, or use background delivery with element_token.")
    assert [call.args[0] for call in dialog_driver.call.await_args_list] == ["bring_to_front"] * 2


def test_split_steps_stops_at_dialog_refusal(dialog_driver):
    guard = CallToolResult(content=[], structured_content={"process_activated": True, "focused": False})
    windows = CallToolResult(content=[], structured_content={"windows": [
        {"window_id": 8, "title": "Go to Folder", "z_index": 2}]})
    dialog_driver.call.side_effect = [guard, guard, windows]
    result = asyncio.run(handler_for(dialog_driver).call("computer_run_steps", {
        "pid": 4242, "window_id": 7, "delivery_mode": "foreground",
        "steps": [{"press_key": {"key": "return"}}, {"type_text": {"text": "hello"}}]}))
    report = json.loads(result.content[0].text)
    assert result.is_error and report["failed_step"] == 0
    assert report["steps"] == [{"index": 0, "ok": False, "code": "foreground_unverified",
                                "message": dialog_refusal()}]
    assert [call.args[0] for call in dialog_driver.call.await_args_list] == [
        "bring_to_front", "bring_to_front", "list_windows"]


def test_exit_is_not_retried_and_next_call_reconnects(fake):
    driver, log, approval = fake

    async def run():
        handler = handler_for(driver, approval)
        try:
            failed = await handler.call("computer_type_text", {"pid": 4242, "text": "exit-mid-call"})
            assert failed.is_error and "not repeated" in failed.content[0].text
            result = await handler.call("computer_type_text", {"pid": 4242, "text": "hello"})
            assert not result.is_error
        finally:
            await handler.close()
    asyncio.run(run())
    rows = calls(log)
    assert len([row for row in rows if row["args"].get("text") == "exit-mid-call"]) == 1
    assert len({row["process"] for row in rows}) == 2


def test_risk_foreground_and_explicit_confirmation(fake):
    driver, log, approval = fake

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call("computer_hotkey", {"pid": 4242, "keys": ["Shift", "Delete"]})
            assert not result.is_error and approval.call_args.kwargs["params"]["risky"] is True
            result = await handler.call("computer_confirm_risky_step", {"pid": 4242, "summary": "Send message"})
            assert result.content[0].text == "approved"
            assert approval.call_args.kwargs["params"]["risk_reason"] == "Send message"
            await handler.call("computer_run_steps", {"pid": 4242, "steps": [{"click": {"delivery_mode": "foreground"}}]})
            assert approval.call_args.kwargs["params"]["foreground"] is True
        finally:
            await handler.close()
    asyncio.run(run())
    assert "computer_confirm_risky_step" not in [row["tool"] for row in calls(log)]


def test_mcp_registration_preserves_error_results(fake):
    driver, log, approval = fake

    async def run():
        mcp = create_server(driver, approvals=FakeApprovals(approval),
                            runtime=AsyncMock(return_value=(driver.binary, driver.socket)))
        try:
            tools = await mcp.list_tools()
            assert {tool.name for tool in tools} == (
                set(server.policy.TOOLS) - platform.current().unsupported_tools)
            result = await mcp.call_tool("computer_click", {"args": {"pid": 4242, "window_id": 7, "x": 1, "y": 2}})
            assert result.is_error
            assert result.content[0].text == "clicked"
        finally:
            await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("missing", ["binary", "socket"])
def test_missing_configuration_returns_error(fake_win32, tmp_path, missing):
    binary = tmp_path / "cua-driver"
    binary.write_text("test driver")
    config = {"binary": str(binary), "socket": str(tmp_path / "driver.sock")}
    config[missing] = ""
    with pytest.raises(DriverUnavailable):
        DriverClient(**config)
    handler = ComputerServer(None, FakeApprovals(), runtime=AsyncMock(side_effect=DriverUnavailable("not running")))
    result = asyncio.run(handler.call("computer_click", {"pid": 4242}))
    assert result.is_error and "unavailable" in result.content[0].text


def test_environment_and_command_are_minimal(fake):
    driver, log, approval = fake
    assert driver.command[1:] == ["mcp", "--embedded", "--socket", str(log.parent / "fake-private-socket")]
    assert set(driver.env) <= {"PATH", "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
                               "ProgramData", "ALLUSERSPROFILE", "TEMP", "TMP", "USERNAME",
                               "HOMEDRIVE", "HOMEPATH", "CUA_DRIVER_EMBEDDED", "DO_NOT_TRACK",
                               "CUA_DRIVER_RS_TELEMETRY_ENABLED", "CUA_DRIVER_RS_UPDATE_CHECK", "FAKE_CUA_LOG"}
    assert driver.env["DO_NOT_TRACK"] == "1"


@pytest.mark.parametrize("structured", [False, True])
def test_app_parser_supports_text_and_structured_results(fake, structured):
    driver, log, approval = fake
    data = {"apps": [{"pid": 7, "name": "App", "bundle_id": "com.test.App"},
                     {"pid": 8, "running": False}, {"pid": 0}, {"pid": True}, {"pid": 9}]}
    result = CallToolResult(content=[] if structured else [TextContent(type="text", text=json.dumps(data))],
                            **({"structured_content": data} if structured else {}))
    driver.call = AsyncMock(return_value=result)
    assert asyncio.run(driver.list_running_apps()) == {7: ("com.test.App", "App"), 9: (None, None)}


@pytest.mark.parametrize("foreground,window_pid,expected", [
    (7, 4242, 4242),
    (0, 4242, None),
    (7, None, None),
    (7, 0, None),
    (7, -1, None),
    (7, True, None),
    (7, "4242", None),
])
def test_frontmost_pid_reads_the_foreground_window_owner(fake_win32, tmp_path, foreground, window_pid, expected):
    driver = DriverClient(sys.executable, str(tmp_path / "unused-socket"))
    fake_win32.foreground = foreground
    fake_win32.pids[7] = window_pid
    driver.call = AsyncMock()
    assert asyncio.run(driver.frontmost_pid()) == expected
    driver.call.assert_not_awaited()


def test_frontmost_pid_returns_none_on_os_error(fake_win32, tmp_path, monkeypatch):
    driver = DriverClient(sys.executable, str(tmp_path / "unused-socket"))
    def denied():
        raise OSError("denied")

    monkeypatch.setattr(fake_win32, "foreground_window", denied)
    assert asyncio.run(driver.frontmost_pid()) is None


@pytest.mark.parametrize("reply", [None, True, [], "approved", 1, RuntimeError("offline")])
def test_malformed_or_failed_approval_never_forwards(fake, reply):
    driver, log, approval = fake
    if isinstance(reply, Exception):
        approval.side_effect = reply
    else:
        approval.return_value = reply

    async def run():
        handler = handler_for(driver, approval)
        try:
            denied = await handler.call("computer_click", {"pid": 4242})
            assert denied.is_error
            confirmation = await handler.call("computer_confirm_risky_step", {"pid": 4242, "summary": "Send"})
            assert confirmation.is_error and confirmation.content[0].text.startswith("rejected:")
        finally:
            await handler.close()
    asyncio.run(run())
    assert calls(log) == []


def test_pid_remapped_during_approval_is_not_acted_on(fake, fake_win32):
    driver, log, approval = fake

    async def remap(tool, params):
        fake_win32.paths[4242] = fake_win32.paths[55]
        return {"approved": True}
    approval.side_effect = remap

    async def run():
        handler = handler_for(driver, approval)
        try:
            denied = await handler.call("computer_click", {"pid": 4242})
            assert denied.is_error and "changed after approval" in denied.content[0].text
        finally:
            await handler.close()
    asyncio.run(run())
    assert approval.call_args.kwargs["params"]["bundle_id"] == NOTEPAD_ID
    assert calls(log) == []


@pytest.mark.parametrize("change", ["gone", "frame_host", "other_app", "elevated"])
def test_missing_or_changed_pid_after_approval_refuses(fake, fake_win32, change):
    driver, log, approval = fake

    async def remap(tool, params):
        if change == "gone":
            del fake_win32.paths[4242]
        elif change == "frame_host":
            fake_win32.paths[4242] = r"C:\Windows\System32\ApplicationFrameHost.exe"
        elif change == "other_app":
            fake_win32.paths[4242] = r"C:\Windows\System32\mspaint.exe"
        else:
            fake_win32.levels[4242] = 0x3000
        return {"approved": True}
    approval.side_effect = remap
    result = asyncio.run(handler_for(driver, approval).call("computer_click", {"pid": 4242}))
    assert result.is_error
    assert calls(log) == []


@pytest.mark.parametrize("tool,args,forwarded", [
    ("computer_list_windows", {"pid": 4242}, "list_windows"),
    ("computer_clipboard_read", {"include_text": True}, "clipboard_read"),
    ("computer_clipboard_write", {"text": "hello"}, "clipboard_write"),
])
def test_opened_tools_forward_after_approval(fake, tool, args, forwarded):
    driver, log, approval = fake

    async def run():
        handler = handler_for(driver, approval)
        try:
            result = await handler.call(tool, args)
            assert not result.is_error
        finally:
            await handler.close()
    asyncio.run(run())
    rows = calls(log)
    assert rows[-1]["tool"] == forwarded
    if "pid" in args:
        assert rows[:-1] == []
        assert approval.call_args.kwargs["params"]["bundle_id"] == NOTEPAD_ID
    else:
        assert len(rows) == 1
        assert approval.call_args.kwargs["params"]["pid"] is None
    for key, value in args.items():
        assert rows[-1]["args"][key] == value
    if tool == "computer_clipboard_read":
        params = approval.call_args.kwargs["params"]
        assert params["bundle_id"] == server.policy.CLIPBOARD_BUNDLE_ID
        assert params["app_name"] == "Clipboard"
    elif tool == "computer_clipboard_write":
        assert approval.call_args.kwargs["params"]["bundle_id"] is None


@pytest.mark.parametrize("args", [
    {"bundle_id": NOTEPAD_ID},
    {"bundle_id": NOTEPAD_ID, "urls": ["https://example.com"]},
    {"bundle_id": NOTEPAD_ID, "urls": [r"C:\Users"]},
])
def test_launch_is_unavailable_before_approval_or_driver_call(fake, args):
    driver, log, approval = fake
    result = asyncio.run(handler_for(driver, approval).call("computer_launch_app", args))
    assert result.is_error
    assert result.content[0].text == "This tool is not available on this operating system yet."
    approval.assert_not_called()
    assert calls(log) == []


def test_driver_session_timeout_covers_maximum_window_read(fake, monkeypatch):
    from windows_computer_use import driver_client
    original = driver_client.ClientSession
    observed = []

    def session(*streams, **kwargs):
        observed.append(kwargs["read_timeout_seconds"])
        return original(*streams, **kwargs)

    monkeypatch.setattr(driver_client, "ClientSession", session)
    driver, log, approval = fake

    async def run():
        try:
            await driver.list_running_apps()
        finally:
            await driver.close()
    asyncio.run(run())
    assert observed == [130]


def test_public_close_permanently_prevents_transport_restart(fake, monkeypatch):
    from unittest.mock import MagicMock
    from windows_computer_use import driver_client

    driver, log, _ = fake

    async def run():
        try:
            await driver.list_running_apps()
            await driver.close()
            transport = MagicMock(side_effect=AssertionError("closed client started a transport"))
            monkeypatch.setattr(driver_client, "_transport", transport)
            for operation in (driver.list_running_apps, lambda: driver.call("click", {"pid": 4242})):
                with pytest.raises(DriverUnavailable):
                    await operation()
            transport.assert_not_called()
        finally:
            await driver.close()

    asyncio.run(run())
    assert len({row["process"] for row in calls(log)}) == 1


def test_caller_cancellation_keeps_client_reusable(fake, monkeypatch):
    from windows_computer_use import driver_client

    driver, log, _ = fake

    async def run():
        entered = asyncio.Event()
        original = driver_client.DriverClient._serve

        async def pending(queue):
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(driver, "_serve", pending)
        # One cancelled owner task, then at most one fake driver process.
        task = asyncio.create_task(driver.call("list_apps", {}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            monkeypatch.setattr(driver, "_serve", original.__get__(driver))
            assert 4242 in await driver.list_running_apps()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await driver.close()

    asyncio.run(run())
    assert len({row["process"] for row in calls(log)}) == 1


@pytest.mark.parametrize("window_id,expected", [(7, 7), (None, None), (True, None), ("7", None)])
def test_gate_params_carry_only_an_integer_window_id(fake, window_id, expected):
    driver, log, approval = fake
    args = {"pid": 4242, "x": 1, "y": 2}
    if window_id is not None:
        args["window_id"] = window_id

    async def run():
        handler = handler_for(driver, approval)
        try:
            await handler.call("computer_click", args)
        finally:
            await handler.close()
    asyncio.run(run())
    if approval.call_args is not None:
        assert approval.call_args.kwargs["params"]["window_id"] == expected
    else:
        # The policy refused the malformed id before any gate was asked.
        assert expected is None
