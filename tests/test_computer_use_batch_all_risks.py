import pytest

from windows_computer_use.policy import risky_reason


@pytest.fixture(autouse=True)
def _platform(fake_win32):
    return fake_win32


QUIT_TEXT = "Quit the application? Unsaved work may be lost."
DELETE_TEXT = "Risky keyboard shortcut: delete+shift"
# One chord per label: "quit" and "delete" are risky, "s" and "w" are harmless.
CHORDS = {"quit": ["alt", "f4"], "delete": ["shift", "delete"], "s": ["ctrl", "s"], "w": ["ctrl", "w"]}


@pytest.mark.parametrize("explicit", [False, True], ids=["shorthand", "tool-args"])
@pytest.mark.parametrize("labels,expected", [
    (["delete", "quit"], DELETE_TEXT + " · " + QUIT_TEXT),
    (["quit", "delete"], QUIT_TEXT + " · " + DELETE_TEXT),
    (["quit", "delete", "quit", "delete"], QUIT_TEXT + " · " + DELETE_TEXT),
    (["quit", "quit"], QUIT_TEXT),
    (["quit"], QUIT_TEXT),
    (["s", "quit"], QUIT_TEXT),
    (["delete"], DELETE_TEXT),
    (["s", "w"], ""),
    ([], ""),
])
def test_batch_names_all_distinct_risks_in_first_seen_order(explicit, labels, expected):
    steps = []
    for label in labels:
        args = {"keys": list(CHORDS[label])}
        steps.append({"tool": "hotkey", "args": args} if explicit else {"hotkey": args})

    assert risky_reason("computer_run_steps", {"steps": steps}) == expected
