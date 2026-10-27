"""Owner settings and window observations must stay fresh during a session."""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from conftest import FakeApprovals
from windows_computer_use import driver_client, gate, platform, policy
from windows_computer_use.platform.windows import WindowsPlatform
from windows_computer_use.server import ComputerServer
from windows_computer_use.standalone import config, hookcheck
from windows_computer_use.standalone.approvals import LocalApprovals


APP_PREFIX = "win:c:" + chr(92) + "apps" + chr(92)


@pytest.fixture
def local(tmp_path, monkeypatch, fake_win32):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    (tmp_path / "config.json").write_text('{"enabled": true}', encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(Path(__file__).parents[1]))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("WINDOWS_CU_CLAUDE_SETTINGS", str(tmp_path / "settings.json"))
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    return LocalApprovals(None, SimpleNamespace(registered=True, halted_at=None),
                          journal=lambda: tmp_path / "journal.jsonl")


@pytest.fixture
def fake_driver(tmp_path, monkeypatch):
    # At most one child per test; each scenario closes its server in finally.
    transport = driver_client._transport
    fixture = Path(__file__).parent / "fixtures" / "fake_cua_driver.py"

    def fake_transport(command, env):
        return transport([sys.executable, str(fixture), *command[1:]], env)

    monkeypatch.setattr(driver_client, "_transport", fake_transport)
    driver = driver_client.DriverClient(sys.executable, str(tmp_path / "socket"))
    log = tmp_path / "calls.jsonl"
    driver.env["FAKE_CUA_LOG"] = str(log)
    return driver, log


def handler_for(driver, approvals):
    return ComputerServer(driver, approvals, runtime=AsyncMock(return_value=(driver.binary, driver.socket)))


def test_confirmation_is_revoked_in_running_server(local, fake_driver, fake_win32, tmp_path):
    fake_win32.paths[4242] = "c:" + chr(92) + "apps" + chr(92) + "whatsapp.exe"
    driver, log = fake_driver
    handler = handler_for(driver, local)
    assert hookcheck.confirm_hook_installed()

    async def scenario():
        try:
            args = {"pid": 4242, "keys": ["enter"], "confirm": "Send reply"}
            assert not (await handler.call("computer_hotkey", args)).is_error
            before = log.read_text(encoding="utf-8")
            (tmp_path / "settings.json").write_text('{"disableAllHooks": true}', encoding="utf-8")
            result = await handler.call("computer_hotkey", args)
            assert result.is_error and "hook is not installed" in result.content[0].text
            assert log.read_text(encoding="utf-8") == before
            result = await handler.call("computer_confirm_risky_step", {"pid": 4242, "summary": "Send reply"})
            assert result.is_error and "hook is not installed" in result.content[0].text
        finally:
            await handler.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("count", [0, 1, 2])
def test_disabled_setting_refuses_every_batch_size(local, fake_driver, tmp_path, count):
    driver, log = fake_driver
    handler = handler_for(driver, local)

    async def scenario():
        try:
            assert not (await handler.call("computer_type_text", {"pid": 4242, "text": "control"})).is_error
            before = log.read_text(encoding="utf-8")
            (tmp_path / "config.json").write_text('{"enabled": false}', encoding="utf-8")
            tool = "computer_run_steps" if count else "computer_type_text"
            args = {"steps": [{"type_text": {"text": "blocked"}} for _ in range(count)]} if count else {"text": "blocked"}
            result = await handler.call(tool, {"pid": 4242, **args})
            assert result.is_error and result.content[0].text == gate.INACTIVE_REASON
            assert log.read_text(encoding="utf-8") == before
        finally:
            await handler.close()

    asyncio.run(scenario())


def snapshot(snapshot_id="s1", label="Message box"):
    return {"snapshot_id": snapshot_id, "elements": [{
        "element_token": snapshot_id + ":1", "label": label, "depth": 1,
        "screenshot_frame": {"x": 0, "y": 0, "w": 100, "h": 100}}]}


@pytest.mark.parametrize("refresh", [
    {"snapshot_id": "s2", "tree_markdown": '- [1] Button "Send"'},
    {"snapshot_id": "s2", "elements": []},
    {"snapshot_id": "s2"},
    {"snapshot_id": "s2", "elements": [{"element_token": "s2:1", "label": "Send"}]},
])
def test_snapshot_without_frames_invalidates_old_coordinates(local, fake_win32, refresh):
    fake_win32.paths[4242] = "c:" + chr(92) + "apps" + chr(92) + "whatsapp.exe"
    reports = iter([snapshot(label="Send"), refresh])

    async def call(tool, args):
        return CallToolResult(content=[], structured_content=next(reports) if tool == "get_window_state" else {})

    driver = SimpleNamespace(binary="fake", socket="fake", call=AsyncMock(side_effect=call))
    handler = handler_for(driver, local)
    key = {"pid": 4242, "window_id": 7}

    async def scenario():
        assert not (await handler.call("computer_window_state", key)).is_error
        assert handler._element_at({**key, "x": 20, "y": 20}) == (True, "Send")
        assert policy.critical_reason("computer_click", {**key, "x": 20, "y": 20},
                                      app_id=APP_PREFIX + "whatsapp.exe", resolve=handler._element_at)
        assert not (await handler.call("computer_window_state", key)).is_error
        assert handler._element_at({**key, "x": 20, "y": 20}) == (False, None)
        # The stale point no longer inherits the old "Send" name, so it is an ordinary chat click.
        assert not policy.critical_reason("computer_click", {**key, "x": 20, "y": 20},
                                          app_id=APP_PREFIX + "whatsapp.exe", resolve=handler._element_at)
        assert not (await handler.call("computer_click", {**key, "x": 20, "y": 20})).is_error
        assert any(item.args[0] == "click" for item in driver.call.await_args_list)

    asyncio.run(scenario())


@pytest.mark.parametrize("window", [{}, {"window_id": None}, {"window_id": True}, {"window_id": "7"}, {"window_id": 9}])
def test_coordinates_require_snapshot_of_named_integer_window(window):
    handler = ComputerServer(approvals=FakeApprovals(), runtime=AsyncMock())
    handler._cache_snapshot(CallToolResult(content=[], structured_content=snapshot()), {"pid": 4242, "window_id": 7})
    assert handler._element_at({"pid": 4242, "x": 20, "y": 20, **window}) == (False, None)
    assert handler._element_at({"pid": 4242, "window_id": 7, "x": 20, "y": 20}) == (True, "Message box")


@pytest.mark.parametrize("explicit", [False, True])
def test_batch_owner_flip_stops_before_next_dispatch(fake_win32, explicit):
    async def call(tool, args):
        fake_win32.pids[7] = 44
        return CallToolResult(content=[], structured_content={"ok": True, "steps": [{"ok": True}]})

    driver = SimpleNamespace(binary="fake", socket="fake", call=AsyncMock(side_effect=call))
    handler = handler_for(driver, FakeApprovals())
    step = {"tool": "type_text", "args": {"text": "hello"}} if explicit else {"type_text": {"text": "hello"}}
    result = asyncio.run(handler.call("computer_run_steps", {"pid": 4242, "window_id": 7, "steps": [step] * 3}))
    assert result.is_error
    report = json.loads(result.content[0].text)
    assert report["executed"] == 1 and report["failed_step"] == 1 and report["total"] == 3
    assert "window 7 does not belong to pid 4242" in report["steps"][-1]["message"]
    driver.call.assert_awaited_once()


@pytest.mark.parametrize("tool", ["computer_click", "computer_double_click", "computer_right_click"])
@pytest.mark.parametrize("verified, locator", [
    ({"element_token": "s1:1"}, locator)
    for locator in ({"x": 5}, {"y": 5}, {"name": "Message box"}, {"role": "Button"}, {"nth": 0})
] + [
    ({"x": 20, "y": 20}, locator)
    for locator in ({"name": "Message box"}, {"role": "Button"}, {"nth": 0})
])
def test_chat_click_locators_are_critical_only_when_named_like_send(fake_win32, tool, locator, verified):
    args = {**verified, **locator}
    resolve = lambda target: (True, "Message box")
    assert not policy.critical_reason(tool, args, app_id=APP_PREFIX + "whatsapp.exe", resolve=resolve)
    assert not policy.critical_reason(tool, args, app_id=APP_PREFIX + "notepad.exe", resolve=resolve)
    assert policy.critical_reason(tool, {**args, "name": "Send"}, app_id=APP_PREFIX + "whatsapp.exe", resolve=resolve)
    assert policy.critical_reason(tool, {**args, "name": "Delete"}, app_id=APP_PREFIX + "notepad.exe", resolve=resolve)
    for step in ({tool.removeprefix("computer_"): args},
                 {"tool": tool.removeprefix("computer_"), "args": args}):
        checked = policy.validate_call("computer_run_steps", {"pid": 4242, "window_id": 7, "steps": [step]})
        assert isinstance(checked, policy.Validated)
        assert not policy.critical_reason("computer_run_steps", checked.args,
                                          app_id=APP_PREFIX + "whatsapp.exe", resolve=resolve)


def test_empty_settings_override_reads_user_hook_disable(local, monkeypatch, tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(user))
    monkeypatch.setenv("WINDOWS_CU_CLAUDE_SETTINGS", "")
    assert hookcheck.confirm_hook_installed()
    (user / "settings.json").write_text('{"disableAllHooks": true}', encoding="utf-8")
    assert not hookcheck.confirm_hook_installed()


@pytest.mark.parametrize("name", [
    "weixin.exe", "kakaotalk.exe", "zalo.exe", "dingtalk.exe", "lark.exe", "feishu.exe",
    "qq.exe", "pidgin.exe", "hexchat.exe", "guilded.exe", "msteams.exe",
])
def test_added_chat_apps_require_enter_confirmation(fake_win32, name):
    app_id = APP_PREFIX + name
    assert platform.current().sends_on_enter(app_id)
    assert policy.critical_reason("computer_press_key", {"key": "enter"}, app_id=app_id, resolve=lambda target: (False, None))


@pytest.mark.parametrize("name", [
    "putty.exe", "wezterm.exe", "mobaxterm.exe", "securecrt.exe", "xshell.exe", "ubuntu.exe", "kali.exe",
    "gemini.exe", "opencode.exe", "enpass.exe", "protonpass.exe", "roboform.exe", "keeper.exe",
])
def test_added_sensitive_apps_are_denied(fake_win32, name):
    assert platform.current().is_hard_denied(APP_PREFIX + name, None)


@pytest.mark.parametrize("tool, args", [
    ("computer_click", {"pid": 4242, "x": 1, "y": 2}),
    ("computer_drag", {"pid": 4242, "from_x": 1, "from_y": 2, "to_x": 3, "to_y": 4}),
    ("computer_run_steps", {"pid": 4242, "steps": [{"click": {"x": 1, "y": 2}}]}),
    ("computer_run_steps", {"pid": 4242, "steps": [{"tool": "click", "args": {"x": 1, "y": 2}}]}),
])
def test_points_without_an_int_window_id_are_refused(tool, args):
    assert isinstance(policy.validate_call(tool, args), policy.Refusal)
    assert isinstance(policy.validate_call(tool, {**args, "window_id": "7"}), policy.Refusal)
    assert isinstance(policy.validate_call(tool, {**args, "window_id": 7}), policy.Validated)
