"""Critical target and dispatch regressions, using fakes only."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from windows_computer_use import platform, policy, server
from windows_computer_use.platform.windows import WindowsPlatform
from windows_computer_use.standalone.approvals import LocalApprovals
from conftest import FakeApprovals


@pytest.mark.parametrize("name", ["SİL", "Send1", "btnsend", "sendmessage", "Satın  al",
                                  "GONDER", "Se\u034fnd", "Send\ufe0f", "SENDButton",
                                  "Se\u115fnd", "Se\u3164nd"])
def test_critical_name_variants(name):
    assert policy.critical_name(name)


@pytest.mark.parametrize("name", ["Silver", "Playback", "Border", "Position", "Recorder", "Pencil"])
def test_short_keywords_require_whole_tokens(name):
    assert policy.critical_name(name) == ""


@pytest.mark.parametrize("target", [
    {"element_token": "s1:1", "name": "Send"},
    {"element_token": "s1:1", "target": {"name": "Delete"}},
])
def test_every_address_is_checked(monkeypatch, target):
    monkeypatch.setattr(platform, "current", lambda: WindowsPlatform(
        SimpleNamespace(set_dpi_awareness=lambda: None)))
    if "target" in target:
        result = policy.validate_call("computer_hotkey", {"pid": 77, "keys": ["tab"], **target})
        assert isinstance(result, policy.Refusal) and result.reason == "Forbidden argument: target"
        return
    assert policy.critical_reason("computer_hotkey", target, app_id=None,
                                  resolve=lambda args: (True, "Pencil"))


@pytest.mark.parametrize("target, expected", [({"name": ["Send"]}, False), ({"role": "Button"}, False),
                                              ({"nth": 0}, False), ({"name": "Send"}, True)])
@pytest.mark.parametrize("exe", ["editor.exe", "whatsapp.exe"])
def test_only_a_send_named_address_is_critical_in_send_on_enter_apps(monkeypatch, target, expected, exe):
    monkeypatch.setattr(platform, "current", lambda: WindowsPlatform(
        SimpleNamespace(set_dpi_awareness=lambda: None)))
    app = "win:c:" + chr(92) + "apps" + chr(92) + exe
    assert bool(policy.critical_reason("computer_hotkey", target, app_id=app,
                                       resolve=lambda args: (True, "Pencil"))) is expected


def test_structured_frames_and_latest_snapshot():
    handler = server.ComputerServer(approvals=FakeApprovals(), runtime=AsyncMock())
    def cache(snapshot, elements):
        handler._cache_snapshot(CallToolResult(content=[], structured_content={
            "snapshot_id": snapshot, "elements": elements,
            "tree_markdown": '- [1] Button "Pencil"'}), {"pid": 77, "window_id": 31})
    cache("s1", [
        {"element_token": "s1:1", "label": "Send", "screenshot_frame": {"x": 10, "y": 10, "w": 20, "h": 20}},
        {"element_token": "s1:2", "screenshot_frame": {"x": 0, "y": 0, "w": 100, "h": 100}},
    ])
    args = {"pid": 77, "window_id": 31, "x": 15, "y": 15}
    assert handler._element_name({**args, "element_token": "s1:1"}) == "Send"
    assert handler._element_at(args) == (True, "Send")
    assert handler._element_at({**args, "x": 50}) == (True, "")
    cache("s2", [])
    assert handler._element_at(args) == (False, None)
    assert handler._element_name({**args, "element_token": "s1:1"}) is None


@pytest.mark.parametrize("mode", ["halt", "dead"])
@pytest.mark.parametrize("shape", ["direct", "single", "split", "foreground"])
def test_stop_during_identity_or_fronting_prevents_input(monkeypatch, tmp_path, mode, shape):
    app = "win:c:" + chr(92) + "apps" + chr(92) + "editor.exe"
    hotkey = SimpleNamespace(registered=True, halted_at=None)
    approvals = LocalApprovals(None, hotkey, journal=lambda: tmp_path / "journal.jsonl")
    win = WindowsPlatform(SimpleNamespace(set_dpi_awareness=lambda: None, parent_pids=lambda: {}))
    identities = 0
    def stop():
        if mode == "halt":
            hotkey.halted_at = 1.0
        else:
            hotkey.registered = False
    async def identify(*args):
        nonlocal identities
        identities += 1
        if identities == (3 if shape == "split" else 2) and shape != "foreground":
            stop()
        return app, "Editor", None
    win.identify = identify
    monkeypatch.setattr(platform, "current", lambda: win)
    monkeypatch.setattr("windows_computer_use.standalone.config.load", lambda: {"enabled": True})
    async def front(*args):
        stop()
        return None
    monkeypatch.setattr(server, "_front_guard", front)
    driver = SimpleNamespace(binary="fake", socket="fake", call=AsyncMock(return_value=CallToolResult(
        content=[], structured_content={"ok": True, "steps": [{"ok": True}]})))
    handler = server.ComputerServer(driver, approvals, runtime=AsyncMock(return_value=("fake", "fake")))
    args = {"pid": 77, "text": "hello"}
    tool = "computer_type_text"
    if shape in {"single", "split"}:
        tool = "computer_run_steps"
        args = {"pid": 77, "steps": [{"type_text": {"text": "hello"}}] * (2 if shape == "split" else 1)}
    elif shape == "foreground":
        args.update(window_id=31, delivery_mode="foreground")
    result = asyncio.run(handler.call(tool, args))
    assert result.is_error
    assert driver.call.await_count == (1 if shape == "split" else 0)


@pytest.mark.parametrize("tree_format", [None, "markdown", "elements", "both"])
def test_window_state_caches_elements_before_filtering_reply(monkeypatch, tree_format):
    win = WindowsPlatform(SimpleNamespace(set_dpi_awareness=lambda: None, window_pid=lambda hwnd: 77,
                                          image_path=lambda pid: "editor.exe"))
    win.identify = AsyncMock(return_value=("win:c:" + chr(92) + "editor.exe", "Editor", None))
    monkeypatch.setattr(platform, "current", lambda: win)
    report = {"snapshot_id": "s1", "elements": [{"element_token": "s1:1", "label": "Send",
               "screenshot_frame": {"x": 10, "y": 10, "w": 20, "h": 20}}]}
    driver = SimpleNamespace(binary="fake", socket="fake", call=AsyncMock(return_value=CallToolResult(
        content=[TextContent(type="text", text=json.dumps(report))], structured_content=report)))
    handler = server.ComputerServer(driver, FakeApprovals(), runtime=AsyncMock(return_value=("fake", "fake")))
    args = {"pid": 77, "window_id": 31}
    if tree_format is not None:
        args["tree_format"] = tree_format
    result = asyncio.run(handler.call("computer_window_state", args))
    driver.call.assert_awaited_once_with("get_window_state", {**args, "tree_format": tree_format or "both"})
    assert not result.is_error
    expected = tree_format in {"elements", "both"}
    assert ("elements" in result.structured_content) is expected
    assert ("elements" in json.loads(result.content[0].text)) is expected
    assert handler._element_name({**args, "element_token": "s1:1"}) == "Send"
    assert handler._element_at({**args, "x": 15, "y": 15}) == (True, "Send")
    assert "elements" in report


@pytest.mark.parametrize("modifier", ["+", "ctrl", "shift"])
def test_risky_chord_cannot_be_hidden_by_an_extra_key(monkeypatch, modifier):
    monkeypatch.setattr(platform, "current", lambda: WindowsPlatform(
        SimpleNamespace(set_dpi_awareness=lambda: None)))
    assert policy.risky_reason("computer_hotkey", {"keys": ["alt", "f4", modifier]})


@pytest.mark.parametrize("alteration", ["none", "click_only", "missing_tool", "echo", "comment",
                                       "suffix", "wrong_type", "missing_type", "disabled", "extra_key"])
def test_hook_registration_is_exact_and_covers_every_confirmation(tmp_path, monkeypatch, alteration):
    from windows_computer_use.standalone import hookcheck
    prefix = hookcheck.TOOL_PREFIX
    args = list(hookcheck._HOOK["args"])
    command = "uv " + " ".join(args)
    hook = {"type": "command", "command": "uv", "args": args}
    hooks_json = {"hooks": {"PreToolUse": [{"matcher": prefix + ".*", "hooks": [hook]}]}}
    entry = hooks_json["hooks"]["PreToolUse"][0]
    user_settings = {}
    if alteration == "click_only":
        entry["matcher"] = prefix + "computer_click"
    elif alteration == "missing_tool":
        entry["matcher"] = prefix + "computer_(click|type_text|press_key|hotkey|run_steps|set_value)"
    elif alteration == "echo":
        hook.pop("args")
        hook["command"] = "echo " + command
    elif alteration == "comment":
        hook.pop("args")
        hook["command"] = "# " + command
    elif alteration == "suffix":
        hook.pop("args")
        hook["command"] = command + " || true"
    elif alteration == "wrong_type":
        hook["type"] = "http"
    elif alteration == "missing_type":
        hook.pop("type")
    elif alteration == "extra_key":
        hook["timeout"] = 30
    elif alteration == "disabled":
        user_settings["disableAllHooks"] = True
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps(hooks_json), encoding="utf-8")
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(user_settings), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert hookcheck.confirm_hook_installed([path]) is (alteration == "none")
