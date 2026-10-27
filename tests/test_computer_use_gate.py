"""Computer-use authorization decisions and the best-effort action journal."""
import json
import os

import pytest

from conftest import NOTEPAD_ID
from windows_computer_use import gate, policy

PARAMS = {"pid": 4242, "bundle_id": NOTEPAD_ID, "app_name": "notepad",
          "risky": False, "risk_reason": "", "summary": "Click OK", "foreground": False}
MSPAINT_ID = r"win:c:\windows\system32\mspaint.exe"
CMD_ID = r"win:c:\windows\system32\cmd.exe"
EXPLORER_ID = r"win:c:\windows\explorer.exe"


@pytest.fixture(autouse=True)
def _platform(fake_win32):
    return fake_win32


def decide(tool="computer_click", *, enabled=True, granted=False, **params):
    return gate.decide(tool, {**PARAMS, **params}, enabled=enabled, granted=granted)


@pytest.mark.parametrize("granted", [False, True])
@pytest.mark.parametrize("tool", ["computer_click", "computer_list_apps", "computer_clipboard_write",
                                  "computer_list_windows", "computer_run_steps"])
def test_disabled_refuses_with_the_switch_reason(tool, granted):
    decision = decide(tool, enabled=False, granted=granted)
    assert decision.kind == "deny"
    assert decision.reason == gate.INACTIVE_REASON


def test_inactive_reason_names_only_the_switch():
    assert "mode" not in gate.INACTIVE_REASON.casefold()
    assert "auto" not in gate.INACTIVE_REASON.casefold()
    assert gate.HALTED_REASON


@pytest.mark.parametrize("granted", [False, True])
def test_gate_decision_depends_only_on_the_grant(granted):
    decision = decide(granted=granted)
    assert decision.kind == ("allow" if granted else "card")
    assert decision.grant is (not granted)
    assert decide(enabled=False, granted=granted).kind == "deny"


@pytest.mark.parametrize("tool", ["computer_run_script", "", "launch", None, 7])
def test_unknown_tool_is_denied(tool):
    decision = decide(tool, granted=True)
    assert decision.kind == "deny"
    assert decision.reason == "Unknown computer-use tool."


@pytest.mark.parametrize("granted", [False, True])
@pytest.mark.parametrize("tool", ["computer_list_apps", "computer_clipboard_write"])
def test_pidless_tools_are_allowed_without_card_or_app_id(tool, granted):
    decision = gate.decide(tool, {}, enabled=True, granted=granted)
    assert decision.kind == "allow"
    assert gate.decide(tool, {}, enabled=False, granted=granted).reason == gate.INACTIVE_REASON


@pytest.mark.parametrize("bundle", [" " + NOTEPAD_ID, NOTEPAD_ID + "\n", "win:c:\\apps\\\x01editor.exe",
                                    7, None, "", NOTEPAD_ID.upper(), r"win:c:\windows\..\evil.exe",
                                    r"win:c:\apps\editor.dll", "a b"])
@pytest.mark.parametrize("granted", [False, True])
def test_malformed_or_unrecognised_ids_deny(bundle, granted):
    decision = decide(bundle_id=bundle, granted=granted)
    assert decision.kind == "deny"
    assert decision.reason == "The target application was not recognised."


@pytest.mark.parametrize("bundle", [CMD_ID, EXPLORER_ID, CMD_ID.replace("cmd", "powershell")])
@pytest.mark.parametrize("granted", [False, True])
@pytest.mark.parametrize("tool", ["computer_click", "computer_list_windows"])
def test_hard_denied_apps_are_refused_even_with_grant(bundle, granted, tool):
    decision = decide(tool, bundle_id=bundle, granted=granted)
    assert decision.kind == "deny"
    assert decision.reason == "This application is closed to computer use."


def test_own_process_is_hard_denied_by_pid():
    decision = decide(pid=os.getpid(), granted=True)
    assert decision.kind == "deny"
    assert decision.reason == "This application is closed to computer use."


@pytest.mark.parametrize("granted", [False, True])
def test_window_listing_checks_the_app_id_but_never_cards(granted):
    assert decide("computer_list_windows", granted=granted).kind == "allow"
    assert decide("computer_list_windows", bundle_id=MSPAINT_ID, granted=granted).kind == "allow"
    assert decide("computer_list_windows", bundle_id=None, granted=granted).kind == "deny"
    assert decide("computer_list_windows", bundle_id=" " + NOTEPAD_ID, granted=granted).kind == "deny"
    assert decide("computer_list_windows", bundle_id=CMD_ID, granted=granted).kind == "deny"


def test_ungranted_app_gets_a_grant_card():
    decision = decide()
    assert decision.kind == "card"
    assert decision.grant is True
    assert decision.reason == "First access to this application needs confirmation."


def test_ungranted_risky_step_gets_a_grant_card_with_the_reason():
    decision = decide(risky=True, risk_reason="Delete files")
    assert decision.kind == "card"
    assert decision.grant is True
    assert "First access" in decision.reason and "Delete files" in decision.reason


def test_granted_risky_step_gets_a_card_without_grant():
    decision = decide(granted=True, risky=True, risk_reason="Delete files")
    assert decision == gate.Decision("card", "Delete files")
    assert decision.grant is False


@pytest.mark.parametrize("granted", [False, True])
def test_risky_step_without_reason_gets_the_default_text(granted):
    decision = decide(granted=granted, risky=True, risk_reason="")
    assert decision.kind == "card"
    assert "This risky step needs confirmation." in decision.reason


@pytest.mark.parametrize("tool", ["computer_launch_app", "computer_click", "computer_list_windows",
                                  "computer_window_state", "computer_confirm_risky_step"])
@pytest.mark.parametrize("bundle", [policy.CLIPBOARD_BUNDLE_ID, policy.CLIPBOARD_BUNDLE_ID.upper()])
def test_clipboard_id_is_reserved_for_the_clipboard_tool(tool, bundle):
    params = {**PARAMS, "bundle_id": bundle, "app_name": "Clipboard"}
    decision = gate.decide(tool, params, enabled=True, granted=True)
    assert decision.kind == "deny"
    read = gate.decide("computer_clipboard_read", params, enabled=True, granted=False)
    assert read.kind == "card" and read.grant is True
    assert gate.decide("computer_clipboard_read", params, enabled=True, granted=True).kind == "allow"
    assert gate.decide("computer_clipboard_read", params, enabled=False, granted=True).kind == "deny"


def test_journal_row_has_only_the_known_fields(tmp_path):
    log = tmp_path / "computer_use_actions.jsonl"
    record = {"conversation_id": 1, "tool": "computer_click", "bundle_id": NOTEPAD_ID, "pid": 4242,
              "decision": "allow", "reason": "", "secret": "must not be written"}
    gate.actions_log_append(log, record)
    gate.actions_log_append(log, {**record, "confirm": "Send the report"})
    first, second = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert set(first) == {"ts", "conversation_id", "tool", "bundle_id", "pid", "decision", "reason"}
    assert first["bundle_id"] == NOTEPAD_ID and first["decision"] == "allow"
    assert second["confirm"] == "Send the report"


def test_journal_failure_is_best_effort(tmp_path, caplog):
    gate.actions_log_append(tmp_path / "missing" / "file", {})
    assert "Could not append" in caplog.text
