"""The quit shortcut (alt+f4 on Windows) gets its own card text in every input form."""
import pytest

from windows_computer_use.policy import risky_reason


@pytest.fixture(autouse=True)
def _platform(fake_win32):
    return fake_win32


QUIT_TEXT = "Quit the application? Unsaved work may be lost."


@pytest.mark.parametrize("tool,args", [
    ("computer_hotkey", {"keys": ["alt", "f4"]}),
    ("computer_press_key", {"key": "f4", "modifiers": ["alt"]}),
    ("computer_hotkey", {"keys": ["Alt", "F4"]}),
    ("computer_press_key", {"key": "F4", "modifiers": ["Alt"]}),
    ("computer_hotkey", {"keys": ["Option", "F4"]}),
    ("computer_press_key", {"key": "F4", "modifiers": ["Option"]}),
])
def test_altf4_returns_quit_card_text(tool, args):
    assert risky_reason(tool, args) == QUIT_TEXT


@pytest.mark.parametrize("step", [
    {"hotkey": {"keys": ["alt", "f4"]}},
    {"tool": "hotkey", "args": {"keys": ["Alt", "F4"]}},
    {"press_key": {"key": "f4", "modifiers": ["alt"]}},
    {"tool": "press_key", "args": {"key": "F4", "modifiers": ["Alt"]}},
])
def test_batch_altf4_returns_quit_card_text(step):
    steps = [{"hotkey": {"keys": ["ctrl", "s"]}}, step]
    assert risky_reason("computer_run_steps", {"steps": steps}) == QUIT_TEXT


def test_ctrl_shift_delete_keeps_existing_risky_reason():
    keys = ["ctrl", "shift", "delete"]
    expected = "Risky keyboard shortcut: " + "+".join(sorted(keys))
    assert risky_reason("computer_hotkey", {"keys": keys}) == expected


@pytest.mark.parametrize("key", ["w", "s"])
def test_harmless_shortcuts_have_no_risky_reason(key):
    assert risky_reason("computer_hotkey", {"keys": ["ctrl", key]}) == ""
