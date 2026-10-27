"""Critical-action classification gaps found by the fix4 audits, using fakes only."""
import json
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from conftest import FakeApprovals
from windows_computer_use import policy, server

APP = "win:c:" + chr(92) + "apps" + chr(92) + "editor.exe"

@pytest.fixture(autouse=True)
def _platform(fake_win32):
    return fake_win32


def make_handler():
    return server.ComputerServer(approvals=FakeApprovals(), runtime=AsyncMock(return_value=("bin", "sock")))


@pytest.mark.parametrize("name", ["DELETEall", "SENDmessage", "PAYnow", "BUYnow", "SHAREnow", "sEND", "gOnDeR"])
def test_mixed_case_labels_are_critical(name):
    assert policy.critical_name(name)


@pytest.mark.parametrize("name", ["Pencil", "Silver", "Settings", "Close Tab", "Display", "Border"])
def test_ordinary_labels_stay_non_critical(name):
    assert policy.critical_name(name) == ""


def cache(handler, snapshot_id, key, **report):
    handler._cache_snapshot(CallToolResult(content=[], structured_content={
        "snapshot_id": snapshot_id, **report}), key)


def element(token, label, x, y, w, h, **extra):
    return {"element_token": token, "label": label, "screenshot_frame": {"x": x, "y": y, "w": w, "h": h}, **extra}


def share_snapshot(handler, key=None):
    cache(handler, "s5", key or {"pid": 77, "window_id": 31}, elements=[
        element("s5:0", "Order list.xlsx", 0, 0, 800, 600, role="Window", depth=0),
        element("s5:1", "Share", 700, 10, 60, 30, depth=1),
        element("s5:2", "Pencil", 10, 10, 30, 30, depth=1),
    ])


def reason(handler, args, tool="computer_click"):
    return policy.critical_reason(tool, args, app_id=APP, resolve=handler._resolver(args))


def test_window_root_is_ignored_by_the_hit_test():
    handler = make_handler()
    share_snapshot(handler)
    assert handler._element_at({"pid": 77, "window_id": 31, "x": 400, "y": 300}) == (True, "")
    assert handler._element_at({"pid": 77, "window_id": 31, "x": 710, "y": 20}) == (True, "Share")


def test_xy_without_window_id_is_unknown():
    handler = make_handler()
    share_snapshot(handler)
    assert handler._element_at({"pid": 77, "x": 710, "y": 20}) == (False, None)
    assert not reason(handler, {"pid": 77, "x": 710, "y": 20})
    assert not reason(handler, {"pid": 77, "x": 20, "y": 20})
    assert not reason(handler, {"pid": 78, "x": 710, "y": 20})
    assert reason(handler, {"pid": 77, "window_id": 31, "x": 710, "y": 20})


def test_token_and_xy_are_both_checked():
    handler = make_handler()
    share_snapshot(handler)
    args = {"pid": 77, "window_id": 31, "element_token": "s5:2", "x": 710, "y": 20}
    assert reason(handler, args)
    assert not reason(handler, {**args, "x": 20, "y": 20})


def test_xy_beside_a_name_is_checked():
    handler = make_handler()
    share_snapshot(handler)
    assert reason(handler, {"pid": 77, "window_id": 31, "name": "Pencil", "x": 710, "y": 20})


def test_markdown_only_snapshot_drops_frames_and_updates_names():
    handler = make_handler()
    share_snapshot(handler)
    cache(handler, "s6", {"pid": 77, "window_id": 31}, tree_markdown='- [1] Button "Send"')
    assert handler._element_at({"pid": 77, "window_id": 31, "x": 710, "y": 20}) == (False, None)
    assert handler._element_name({"pid": 77, "window_id": 31, "element_token": "s6:1"}) == "Send"


def test_xy_over_a_snapshot_with_no_frames_is_unverified():
    handler = make_handler()
    cache(handler, "s1", {"pid": 77, "window_id": 31}, tree_markdown='- [1] Button "Send"')
    assert handler._element_at({"pid": 77, "window_id": 31, "x": 5, "y": 5}) == (False, None)


@pytest.mark.parametrize("elements", [None, {"element_token": "s1:1", "label": "Pencil"}, []])
def test_unusable_elements_fall_back_to_markdown_rows(elements):
    handler = make_handler()
    cache(handler, "s1", {"pid": 77, "window_id": 31}, elements=elements, tree_markdown='- [1] Button "Pencil"')
    assert handler._element_name({"pid": 77, "window_id": 31, "element_token": "s1:1"}) == "Pencil"


def write_settings(path, settings):
    path.write_text(json.dumps(settings), encoding="utf-8")
    return path


def hook_settings(matcher):
    from windows_computer_use.standalone import hookcheck
    return {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [dict(hookcheck._HOOK)]}]}}


def use_plugin_root(root, monkeypatch, matcher):
    (root / "hooks").mkdir(exist_ok=True)
    write_settings(root / "hooks" / "hooks.json", hook_settings(matcher))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))


def test_hook_matcher_must_cover_every_tool(tmp_path, monkeypatch):
    from windows_computer_use.standalone import hookcheck
    prefix = hookcheck.TOOL_PREFIX
    seven = prefix + "computer_(click|type_text|press_key|hotkey|run_steps|set_value|confirm_risky_step)"
    (tmp_path / "a").mkdir()
    use_plugin_root(tmp_path / "a", monkeypatch, seven)
    assert hookcheck.confirm_hook_installed([]) is False
    (tmp_path / "b").mkdir()
    use_plugin_root(tmp_path / "b", monkeypatch, prefix + ".*")
    assert hookcheck.confirm_hook_installed([]) is True


def test_disable_all_hooks_in_any_file_vetoes_the_guard(tmp_path, monkeypatch):
    from windows_computer_use.standalone import hookcheck
    use_plugin_root(tmp_path, monkeypatch, hookcheck.TOOL_PREFIX + ".*")
    good = write_settings(tmp_path / "settings.json", {})
    off = write_settings(tmp_path / "settings.local.json", {"disableAllHooks": True})
    assert hookcheck.confirm_hook_installed([good]) is True
    assert hookcheck.confirm_hook_installed([good, off]) is False
    assert hookcheck.confirm_hook_installed([off, good]) is False
