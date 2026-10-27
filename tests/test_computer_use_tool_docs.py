"""Published tool guidance and refusals describe the policy's actual format."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from conftest import FakeApprovals
from windows_computer_use import platform, policy, server


@pytest.fixture(autouse=True)
def _platform(fake_win32):
    return fake_win32


def make_server():
    return server.create_server(approvals=FakeApprovals(), runtime=AsyncMock(return_value=("bin", "sock")))


def test_instructions_explain_args_envelope():
    mcp = make_server()
    assert (
        'Every tool takes exactly one parameter named args; put all arguments inside it, '
        'e.g. {"args": {"pid": 123, "window_id": 45}}.'
    ) in mcp.instructions
    assert (
        "To check a file the task created, look only in the folder the task names; "
        "never search the whole user folder."
    ) in mcp.instructions
    assert (
        "Work in the background: hotkeys, typing and element_token clicks reach background apps, "
        "including their Open/Save panels. To open a file: ctrl+o in the app, type the full path "
        "starting with the drive letter (for example C:@Users@...), enter. To save: ctrl+s. ".replace("@", chr(92))
        + "computer_launch_app is not available on Windows. "
        "In an Open or Save panel, set the file name with computer_set_value on the name field's element_token; "
        "typed text often does not reach these panels. In Unity, open a scene by double-clicking it in the "
        "Project window or with File > Open Recent Scene rather than the Open panel. "
        "If a hotkey or click is refused in the background (e.g. Find/Replace ctrl+h, background_unavailable), "
        "send the same call again with delivery_mode foreground and window_id: it brings the window to the front "
        "and leaves it there. Use computer_bring_to_front only if that is refused too."
        " Send multi-step foreground work (for example renaming: return, type the name, return) as ONE "
        "computer_run_steps with delivery_mode foreground."
    ) in mcp.instructions


def test_run_steps_description_has_valid_example_and_policy_names():
    tools = asyncio.run(make_server().list_tools())
    assert {tool.name for tool in tools} == set(policy.TOOLS) - platform.current().unsupported_tools
    for tool in tools:
        spec = policy.TOOLS[tool.name]
        prefix = (
            "Computer-use operation. Allowed args: " + ", ".join(sorted(spec.allowed))
            + ". Required: " + ", ".join(sorted(spec.required)) + "."
        )
        description = tool.description
        assert description.startswith(prefix)
        element_hint = (
            ' element_token is `<snapshot_id>:N` from the latest computer_window_state of that window '
            '(e.g. "s00000003:18"); never a bare number.'
        )
        hints = {
            "computer_list_windows": (
                " Also lists the app's open panels and dialogs (layer above 0, e.g. Unity's New Scene window); "
                "the one with the highest z_index is in front. Read it with computer_window_state by its "
                "window_id: the main window's screenshot does not show it."
            ),
            "computer_bring_to_front": (
                " Brings the app's window to the front and leaves it there. Last resort: macOS may refuse to "
                "activate the app; a refused background hotkey or click works when sent again with delivery_mode "
                "foreground and window_id, so use this only if that is refused too. Pass window_id to verify that window is in front."
            ),
        }
        assert (element_hint in description) == ("element_token" in spec.allowed)
        for name, hint in hints.items():
            assert (hint in description) == (tool.name == name)
        if tool.name != "computer_run_steps":
            expected_hint = element_hint if "element_token" in spec.allowed else hints.get(tool.name, "")
            assert description == prefix + expected_hint
            continue
        assert 'Each step is {"tool": NAME, "args": {...}}' in description
        assert 'shorthand {NAME: {...}}' in description
        assert ", ".join(sorted(policy.STEP_TOOLS)) in description
        check_keys = ", ".join(sorted(policy._CHECK_KEYS - {"pid", "window_id"}))
        assert f"a list of up to 4: {check_keys}" in description
        assert "(no computer_ prefix)" in description
        assert "inherit pid/window_id from the batch" in description
        assert "1-64 steps; delay_ms 0-2000 between steps" in description
        example = json.loads(description.split("Example: ", 1)[1])
        assert example == {"args": {"pid": 123, "window_id": 45, "steps": [
            {"tool": "type_text", "args": {"text": "hello "}},
            {"tool": "press_key", "args": {"key": "return"}},
        ]}}
        assert isinstance(policy.validate_call("computer_run_steps", example["args"]), policy.Validated)


@pytest.mark.parametrize("step,key", [
    ({"action": "type_text"}, "action"),
    ({"type": "type_text"}, "type"),
    ({"tool": "type_text", "args": {"text": "hello"}, "extra": True}, "extra"),
    ({"type_text": {"text": "hello"}, "args": {}}, "args"),
])
def test_forbidden_step_key_explains_format(step, key):
    refusal = policy.validate_call("computer_run_steps", {"pid": 123, "steps": [step]})
    assert isinstance(refusal, policy.Refusal)
    assert refusal.reason == (
        f'Forbidden step key: {key}. A step is {{"tool": "type_text", "args": {{...}}}} '
        'or {"type_text": {...}}; tools: ' + ", ".join(sorted(policy.STEP_TOOLS))
    )


@pytest.mark.parametrize("tool", ["computer_type_text", "unknown", 5, ["x" * 80]])
def test_unsupported_step_tool_explains_names(tool):
    refusal = policy.validate_call("computer_run_steps", {"pid": 123, "steps": [{"tool": tool}]})
    assert isinstance(refusal, policy.Refusal)
    value = tool if isinstance(tool, str) else repr(tool)[:40]
    expected = (
        f"Unsupported step tool: {value}. Use one of: "
        + ", ".join(sorted(policy.STEP_TOOLS)) + " (no computer_ prefix)"
    )
    if tool == "computer_type_text":
        expected += '. Did you mean "type_text"?'
    assert refusal.reason == expected


def test_empty_step_explains_format():
    refusal = policy.validate_call("computer_run_steps", {"pid": 123, "steps": [{}]})
    assert isinstance(refusal, policy.Refusal)
    assert refusal.reason == (
        'Step requires a tool, wait_for or expect. A step is {"tool": "type_text", "args": {...}}'
    )


@pytest.mark.parametrize("args", [
    {"pid": 123, "steps": [{"type_text": {"text": "hello"}}], "extra": True},
    {"pid": 123, "steps": [{"type_text": {"text": "hello", "extra": True}}]},
])
def test_non_step_keys_keep_original_refusal(args):
    refusal = policy.validate_call("computer_run_steps", args)
    assert isinstance(refusal, policy.Refusal)
    assert refusal.reason == "Forbidden argument: extra"
