"""Send-path holes: non-int window_id, Enter spellings, send chords and Unicode line breaks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from conftest import FakeApprovals
from windows_computer_use import policy, server

BS = chr(92)
SEND_CASES = [
    ("computer_hotkey", {"keys": ["alt", "s"]}),
    ("computer_hotkey", {"keys": "alt+s"}),
    ("computer_press_key", {"key": "s", "modifiers": ["alt"]}),
    ("computer_press_key", {"key": "a", "modifiers": ["enter"]}),
    ("computer_press_key", {"key": "numpadenter"}),
    ("computer_press_key", {"key": "kp_enter"}),
    ("computer_press_key", {"key": " Enter"}),
    ("computer_press_key", {"key": "\n"}),
    ("computer_press_key", {"key": "\r"}),
    ("computer_press_key", {"key": "lf"}),
    ("computer_press_key", {"key": "cr"}),
    ("computer_hotkey", {"keys": ["ctrl", "m"]}),
    ("computer_hotkey", {"keys": ["ctrl", "j"]}),
    ("computer_type_text", {"text": "hi" + chr(0x2028)}),
    ("computer_type_text", {"text": "hi" + chr(0x2029)}),
    ("computer_type_text", {"text": "hi" + chr(0x85)}),
    ("computer_set_value", {"value": "a\nb"}),
]
ALT_S = [("computer_hotkey", {"keys": ["alt", "s"]}), ("computer_hotkey", {"keys": "alt+s"}),
         ("computer_press_key", {"key": "s", "modifiers": ["alt"]})]


def reason(tool, extra, exe):
    args = {"pid": 1, "window_id": 10, **extra}
    return policy.critical_reason(tool, args, app_id="win:c:" + BS + "apps" + BS + exe,
                                  resolve=lambda target: (False, None))


@pytest.mark.parametrize("exe", ["whatsapp.exe", "outlook.exe"])
@pytest.mark.parametrize("tool, extra", SEND_CASES)
def test_send_variants_are_critical(tool, extra, exe):
    assert reason(tool, extra, exe)


@pytest.mark.parametrize("tool, extra", [case for case in SEND_CASES if case not in ALT_S])
def test_same_keys_are_not_critical_in_notepad(tool, extra):
    assert not reason(tool, extra, "notepad.exe")


@pytest.mark.parametrize("tool, extra", [
    ("computer_hotkey", {"keys": ["ctrl", "c"]}),
    ("computer_hotkey", {"keys": ["ctrl", "v"]}),
    ("computer_type_text", {"text": "hi"}),
])
def test_ordinary_input_is_not_critical_in_whatsapp(tool, extra):
    assert not reason(tool, extra, "whatsapp.exe")


def test_new_send_on_enter_app_is_critical_on_return():
    assert reason("computer_press_key", {"key": "return"}, "viber.exe")


@pytest.mark.parametrize("keys", [["control", "m"], ["control", "J"], ["option", "s"], "control+m"])
def test_driver_spellings_of_send_chords_are_critical(keys):
    assert reason("computer_hotkey", {"keys": keys}, "whatsapp.exe")


@pytest.mark.parametrize("extra", [{"x": 5}, {"y": 5}, {"element_token": "s1:3"}, {"x": 5, "y": 5}])
def test_chat_click_without_a_named_target_is_not_critical(extra):
    unnamed = lambda target: (True, "")
    send = lambda target: (True, "Send")
    args = {"pid": 1, "window_id": 10, **extra}
    assert not policy.critical_reason("computer_click", args, app_id="win:c:\apps\whatsapp.exe", resolve=unnamed)
    assert not policy.critical_reason("computer_click", args, app_id="win:c:\apps\notepad.exe", resolve=unnamed)
    if "element_token" in extra or {"x", "y"} <= extra.keys():
        assert policy.critical_reason("computer_click", args, app_id="win:c:\apps\whatsapp.exe", resolve=send)


@pytest.mark.parametrize("tool", ["computer_click", "computer_double_click", "computer_right_click"])
def test_untargeted_click_is_not_critical_in_any_app(tool):
    assert not reason(tool, {}, "whatsapp.exe")
    assert not reason(tool, {}, "notepad.exe")
    steps = {"steps": [{"tool": tool.removeprefix("computer_"), "args": {}}]}
    assert not reason("computer_run_steps", steps, "whatsapp.exe")
    assert not reason("computer_run_steps", steps, "notepad.exe")
    assert reason(tool, {"name": "Send"}, "whatsapp.exe")


@pytest.mark.parametrize("value", ["8", "0x8", 8.0, [8], {}, True])
def test_non_int_window_id_is_refused_before_approval(fake_win32, value):
    fake_win32.pids.update({8: 4242})
    driver = SimpleNamespace(binary="fake", socket="fake", close=AsyncMock(),
                             call=AsyncMock(return_value=CallToolResult(content=[])))
    approvals = FakeApprovals()
    handler = server.ComputerServer(driver, approvals, runtime=AsyncMock(return_value=("fake", "fake")))
    result = asyncio.run(handler.call("computer_window_state", {"pid": 4242, "window_id": value}))
    assert result.is_error
    approvals.approval.assert_not_awaited()
    driver.call.assert_not_awaited()
    assert server._foreign_window({"pid": 4242, "window_id": value}, 4242) == (
        "window_id must be an integer; nothing was sent.")


def _zoom_aware(target):
    if target.get("from_zoom") or target.get("capture_id"):
        return False, None
    return (True, "") if "element_token" in target else (True, "Message box")


def _click(extra, exe):
    return policy.critical_reason("computer_click", {"pid": 1, "window_id": 10, **extra},
                                  app_id="win:c:" + BS + "apps" + BS + exe, resolve=_zoom_aware)


@pytest.mark.parametrize("zoom", [{"from_zoom": True}, {"capture_id": "c1"}])
def test_zoomed_click_is_not_critical_without_a_send_name(zoom):
    assert not _click({"x": 5, "y": 5, **zoom}, "whatsapp.exe")
    assert not _click({"x": 5, "y": 5, **zoom}, "notepad.exe")


@pytest.mark.parametrize("extra", [{"element_token": "s1:3", "x": 5, "y": 5}, {"name": "Message box"},
                                   {"name": "Message box", "element_token": "s1:3"}])
def test_named_or_unnamed_locators_are_not_critical_in_chat_apps(extra):
    assert not _click(extra, "whatsapp.exe")
    assert not _click(extra, "notepad.exe")


def test_a_send_named_locator_cannot_hide_behind_a_harmless_one():
    assert _click({"element_token": "s1:3", "name": "Send"}, "whatsapp.exe")
    assert _click({"x": 5, "y": 5, "name": "Send"}, "whatsapp.exe")
    assert _click({"name": "Send"}, "notepad.exe")


def test_fully_named_x_y_click_stays_allowed_in_chat_apps():
    assert not _click({"x": 5, "y": 5}, "whatsapp.exe")


@pytest.mark.parametrize("zoom", [{"capture_id": "c1"}, {"from_zoom": True}])
@pytest.mark.parametrize("exe", ["whatsapp.exe", "notepad.exe"])
def test_zoom_xy_is_never_resolved_so_it_cannot_inherit_a_send_name(zoom, exe):
    # A resolver that would name the spot "Send" must not be consulted for zoom coordinates, which are
    # outside the latest snapshot's space; a capture_id point is still resolved and name-checked.
    send = lambda target: (True, "Send")
    args = {"pid": 1, "window_id": 10, "x": 5, "y": 5, **zoom}
    app_id = "win:c:" + BS + "apps" + BS + exe
    steps = {"pid": 1, "window_id": 10, "steps": [{"tool": "click", "args": {"x": 5, "y": 5, **zoom}}]}
    for call, payload in (("computer_click", args), ("computer_run_steps", steps)):
        assert bool(policy.critical_reason(call, payload, app_id=app_id, resolve=send)) is ("capture_id" in zoom)


@pytest.mark.parametrize("extra", [{"from_zoom": False}, {"from_zoom": 0}, {"from_zoom": "false"},
                                   {"capture_id": ""}, {"capture_id": None}])
def test_present_zoom_or_capture_key_is_not_critical_with_a_harmless_name(extra):
    named = lambda target: (True, "Message box")
    args = {"pid": 1, "window_id": 10, "x": 5, "y": 5, **extra}
    assert not policy.critical_reason("computer_click", args, app_id="win:c:\apps\whatsapp.exe", resolve=named)
    assert not policy.critical_reason("computer_click", args, app_id="win:c:\apps\notepad.exe", resolve=named)


def test_capture_id_click_is_still_name_checked_in_chat_apps():
    delete = lambda target: (True, "Delete all")
    named = lambda target: (True, "Message box")
    args = {"pid": 1, "window_id": 10, "x": 5, "y": 5, "capture_id": "c1"}
    assert policy.critical_reason("computer_click", args, app_id="win:c:\apps\notepad.exe", resolve=delete)
    assert not policy.critical_reason("computer_click", args, app_id="win:c:\apps\notepad.exe", resolve=named)
    assert policy.critical_reason("computer_click", args, app_id="win:c:\apps\whatsapp.exe", resolve=delete)
    assert not policy.critical_reason("computer_click", args, app_id="win:c:\apps\whatsapp.exe", resolve=named)
