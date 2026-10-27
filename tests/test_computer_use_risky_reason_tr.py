import pytest

from windows_computer_use.policy import risky_reason


@pytest.fixture(autouse=True)
def _platform(fake_win32):
    return fake_win32


def test_risky_hotkey_reason_text():
    assert risky_reason("computer_hotkey", {"keys": ["shift", "delete"]}) == "Risky keyboard shortcut: delete+shift"


@pytest.mark.parametrize("step", [
    {"hotkey": {"keys": ["shift", "delete"]}},
    {"tool": "hotkey", "args": {"keys": ["shift", "delete"]}},
])
def test_batch_risky_hotkey_reason_text(step):
    assert risky_reason("computer_run_steps", {"steps": [step]}) == "Risky keyboard shortcut: delete+shift"


def test_harmless_hotkey_has_no_risky_reason():
    assert risky_reason("computer_hotkey", {"keys": ["ctrl", "s"]}) == ""
