"""Batch interruption tests use a local fake driver and mocked heartbeat."""
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from conftest import NOTEPAD_ID, FakeApprovals
from windows_computer_use import driver_client, server
from windows_computer_use.driver_client import DriverClient
from windows_computer_use.server import ComputerServer


@pytest.fixture
def fake(tmp_path, monkeypatch, fake_win32):
    # Each test starts at most one fake driver process and closes it afterward.
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
    runtime = AsyncMock(return_value=(str(executable), socket))
    driver.env["FAKE_CUA_LOG"] = str(log)
    approval = AsyncMock(return_value={"approved": True})
    return driver, log, approval, runtime


def steps(count):
    return [{"type_text": {"text": str(i)}} for i in range(count)]


def stopping(stop):
    # FakeApprovals whose batch_stop is the given mock (one call per stop check).
    approvals = FakeApprovals()
    approvals.batch_stop = stop
    return approvals


def run_batch(fake, args, stop):
    driver, log, approval, runtime = fake
    approvals = stopping(stop)
    approvals.approval = approval

    async def run():
        handler = ComputerServer(driver, approvals, runtime=runtime)
        try:
            return await handler.call("computer_run_steps", {"pid": 4242, **args})
        finally:
            await handler.close()

    result = asyncio.run(run())
    # The driver starts lazily: no log file exists when no step ran.
    rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    calls = [row["args"] for row in rows if row["tool"] == "run_steps"]
    approval.assert_awaited_once()
    return result, json.loads(result.content[0].text), calls


@pytest.mark.parametrize("checks,executed", [([None, None, None, "halted"], 3),
                                            ([None, "halted"], 1), (["halted"], 0)])
def test_halt_stops_before_next_step(fake, checks, executed):
    halted = AsyncMock(side_effect=checks)
    batch = steps(5)
    result, report, calls = run_batch(fake, {"steps": batch}, halted)
    assert [call["steps"] for call in calls] == [[step] for step in batch[:executed]]
    assert result.is_error
    assert report["executed"] == executed and report["total"] == 5
    assert report["halted"] is True and report["ok"] is False
    assert report["stop_reason"] == "halted"
    assert report["failed_step"] is None
    assert [entry["index"] for entry in report["steps"]] == list(range(executed))
    assert report["halt_reason"] == (
        f"The user stopped computer use for this turn; {executed}/5 steps ran, "
        "the rest were not run. Do not retry the computer tools this turn.")
    assert halted.await_count == len(checks)


@pytest.mark.parametrize("explicit_tool", [False, True])
def test_failure_stops_without_further_halt_checks(fake, explicit_tool):
    halted = AsyncMock(return_value=None)
    batch = steps(4)
    batch[1]["type_text"]["text"] = "fail-step"
    if explicit_tool:
        batch = [{"tool": "type_text", "args": step["type_text"]} for step in batch]
    result, report, calls = run_batch(fake, {"steps": batch}, halted)
    assert [call["steps"] for call in calls] == [[step] for step in batch[:2]]
    assert result.is_error and report["ok"] is False
    assert report["executed"] == 2 and report["total"] == 4
    assert report["failed_step"] == 1
    assert report["steps"][1]["message"] == "failed"
    assert all(entry["tool"] == "type_text" for entry in report["steps"])
    assert "halted" not in report
    assert halted.await_count == 2


def test_defaults_delay_and_final_observation(fake, monkeypatch):
    halted = AsyncMock(return_value=None)
    sleep = AsyncMock()
    monkeypatch.setattr(server.asyncio, "sleep", sleep)
    # The fake driver's bring_to_front report is not a Windows verification report.
    monkeypatch.setattr(server, "_front_guard", AsyncMock(return_value=None))
    defaults = {"pid": 4242, "window_id": 7, "delivery_mode": "background",
                "foreground_fallback": True}
    batch = steps(3)
    observe = {"include_screenshot": True}
    result, report, calls = run_batch(fake, {
        **defaults, "steps": batch, "delay_ms": 10, "observe": observe}, halted)
    assert calls == [{**defaults, "steps": [step], **({"observe": observe} if i == 2 else {})}
                     for i, step in enumerate(batch)]
    assert not result.is_error and report["ok"] is True
    assert report["executed"] == report["total"] == 3
    assert report["failed_step"] is None
    assert [entry["index"] for entry in report["steps"]] == [0, 1, 2]
    assert report["observation"] == {"ok": True, "tool": "get_window_state"}
    assert result.content[1] == ImageContent(type="image", data="aGVsbG8=", mime_type="image/png")
    assert halted.await_count == 3
    approval_params = fake[2].await_args.kwargs["params"]
    assert all(call.args == (approval_params,) for call in halted.await_args_list)
    assert approval_params["window_id"] == 7
    assert sleep.await_count == 2
    assert all(call.args == (0.01,) for call in sleep.await_args_list)


def test_single_step_is_forwarded_without_halt_check(fake):
    halted = AsyncMock(return_value="halted")
    args = {"pid": 4242, "steps": steps(1), "foreground_fallback": False,
            "window_id": 7, "delivery_mode": "background", "observe": {}}
    result, report, calls = run_batch(fake, args, halted)
    assert calls == [args]
    assert not result.is_error and report["ok"] is True
    halted.assert_not_awaited()


@pytest.mark.parametrize("reason,prefix,suffix", [
    ("halted", "The user stopped computer use for this turn",
     " Do not retry the computer tools this turn."),
    ("disabled", "Computer use was turned off", ""),
    ("unverified", "The stop state could not be verified", ""),
])
@pytest.mark.parametrize("executed", [0, 2])
def test_stop_reason(fake, reason, prefix, suffix, executed):
    stop = AsyncMock(side_effect=[None] * executed + [reason])
    result, report, calls = run_batch(fake, {"steps": steps(4)}, stop)
    assert len(calls) == executed
    assert result.is_error and report["halted"] is True
    assert report["stop_reason"] == reason and report["executed"] == executed
    assert report["halt_reason"] == (
        f"{prefix}; {executed}/4 steps ran, the rest were not run.{suffix}")


def test_delay_precedes_check_except_for_first_step(monkeypatch, fake_win32):
    events = []

    async def stop(params):
        events.append("check")
        return "disabled" if len(events) == 3 else None

    async def sleep(delay):
        assert delay == 0.01
        events.append("sleep")

    driver = AsyncMock()
    driver.call.return_value = CallToolResult(content=[TextContent(
        type="text", text=json.dumps({"ok": True, "steps": [{"ok": True}]}))])
    monkeypatch.setattr(server.asyncio, "sleep", sleep)
    handler = ComputerServer(approvals=stopping(stop), runtime=AsyncMock())
    asyncio.run(handler._run_steps_split(
        driver, {"pid": 4242, "steps": steps(3), "delay_ms": 10}, {}))
    assert events == ["check", "sleep", "check"]
    driver.call.assert_awaited_once()


@pytest.mark.parametrize("report", [None, [], {"steps": []}, {"steps": [None]},
                                  {"steps": [{}, {}]}])
def test_unreadable_step_result_preserves_original_content_and_stops(report):
    original = [TextContent(type="text", text=json.dumps(report)),
                ImageContent(type="image", data="aGVsbG8=", mime_type="image/png")]
    driver = AsyncMock()
    driver.call.return_value = CallToolResult(content=original)
    halted = AsyncMock(return_value=None)
    handler = ComputerServer(approvals=stopping(halted), runtime=AsyncMock())
    result = asyncio.run(handler._run_steps_split(driver, {"pid": 4242, "steps": steps(3)}, {}))
    assert result.is_error
    assert result.content[0].text == "The result of step 1/3 could not be read; the remaining steps were not run."
    assert result.content[1:] == original
    driver.call.assert_awaited_once()
    halted.assert_awaited_once_with({})


@pytest.mark.parametrize("failure", ["result", "report", "entry", None])
def test_structured_report_preferred_and_all_failure_signals_stop(fake_win32, failure):
    report = {"ok": failure != "report", "steps": [{"ok": failure != "entry", "index": 0}],
              "observation": {"ok": True}}
    image = ImageContent(type="image", data="aGVsbG8=", mime_type="image/png")
    driver = AsyncMock()
    driver.call.return_value = CallToolResult(
        content=[TextContent(type="text", text="not JSON"), image],
        structured_content=report, is_error=failure == "result")
    halted = AsyncMock(return_value=None)
    handler = ComputerServer(approvals=stopping(halted), runtime=AsyncMock())
    result = asyncio.run(handler._run_steps_split(driver, {"pid": 4242, "steps": steps(2)}, {"bundle_id": NOTEPAD_ID}))
    merged = json.loads(result.content[0].text)
    assert result.is_error is (failure is not None)
    assert merged["executed"] == (1 if failure else 2)
    assert merged["failed_step"] == (0 if failure else None)
    assert merged["steps"][-1]["index"] == (0 if failure else 1)
    assert report["steps"][0]["index"] == 0
    assert merged["observation"] == {"ok": True}
    assert result.content[1:] == [image]
    assert halted.await_count == (1 if failure else 2)
