"""A call's window_id must belong to the call's pid, before approval and again before dispatch."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult

from conftest import FakeApprovals
from windows_computer_use import platform, server

REFUSAL = "window 8 does not belong to pid 4242; nothing was sent."


@pytest.fixture
def setup(fake_win32):
    fake_win32.pids.update({7: 4242, 8: 44})
    driver = SimpleNamespace(binary="fake", socket="fake", close=AsyncMock(),
                             call=AsyncMock(return_value=CallToolResult(content=[])))
    approvals = FakeApprovals()
    handler = server.ComputerServer(driver, approvals, runtime=AsyncMock(return_value=("fake", "fake")))
    return handler, driver, approvals, fake_win32


def step(window_id):
    return {"tool": "type_text", "args": {"text": "hi", "window_id": window_id}}


@pytest.mark.parametrize("tool, args", [
    ("computer_click", {"pid": 4242, "window_id": 8, "x": 1, "y": 2}),
    ("computer_window_state", {"pid": 4242, "window_id": 8}),
])
def test_foreign_window_is_refused_before_approval(setup, tool, args):
    handler, driver, approvals, _ = setup
    result = asyncio.run(handler.call(tool, args))
    assert result.is_error and result.content[0].text == REFUSAL
    approvals.approval.assert_not_awaited()
    driver.call.assert_not_awaited()


def test_batch_with_foreign_window_is_refused_before_approval(setup):
    handler, driver, approvals, _ = setup
    result = asyncio.run(handler.call("computer_run_steps", {
        "pid": 4242, "window_id": 8, "steps": [step(8), step(8)], "observe": {"window_id": 8}}))
    assert result.is_error and result.content[0].text == REFUSAL
    approvals.approval.assert_not_awaited()
    driver.call.assert_not_awaited()


@pytest.mark.parametrize("where", ["step", "observe", "expect"])
def test_step_check_or_observe_cannot_name_another_window(setup, where):
    handler, driver, approvals, _ = setup
    args = {"pid": 4242, "window_id": 7, "steps": [step(7)]}
    if where == "step":
        args["steps"].append(step(8))
    elif where == "observe":
        args["observe"] = {"window_id": 8}
    else:
        args["steps"].append({"expect": {"window_id": 8}})
    result = asyncio.run(handler.call("computer_run_steps", args))
    assert result.is_error and "must match batch window_id" in result.content[0].text
    driver.call.assert_not_awaited()


def test_window_changing_owner_after_approval_is_refused_before_dispatch(setup):
    handler, driver, approvals, win32 = setup

    async def approve(tool, params):
        win32.pids[7] = 44
        return {"approved": True}

    approvals.approval = AsyncMock(side_effect=approve)
    result = asyncio.run(handler.call("computer_click", {"pid": 4242, "window_id": 7, "x": 1, "y": 2}))
    assert result.is_error and result.content[0].text == "window 7 does not belong to pid 4242; nothing was sent."
    approvals.approval.assert_awaited_once()
    driver.call.assert_not_awaited()


def test_batch_window_changing_owner_after_approval_is_refused_before_dispatch(setup):
    handler, driver, approvals, win32 = setup

    async def approve(tool, params):
        win32.pids[7] = 44
        return {"approved": True}

    approvals.approval = AsyncMock(side_effect=approve)
    result = asyncio.run(handler.call("computer_run_steps", {
        "pid": 4242, "window_id": 7, "steps": [step(7), step(7)]}))
    assert result.is_error and "window 7 does not belong to pid 4242" in result.content[0].text
    driver.call.assert_not_awaited()


def test_matching_window_is_allowed_and_points_without_window_id_are_refused(setup):
    handler, driver, _, _ = setup
    assert not asyncio.run(handler.call("computer_click", {"pid": 4242, "window_id": 7, "x": 1, "y": 2})).is_error
    refused = asyncio.run(handler.call("computer_click", {"pid": 4242, "x": 1, "y": 2}))
    assert refused.is_error and "need the window_id" in refused.content[0].text
    assert driver.call.await_count == 1


@pytest.mark.parametrize("window_id, pid", [(7, 4242), ("7", 4242), (True, 1), (8, 4242), (99, 4242), (7, "4242")])
def test_window_owned_by(fake_win32, window_id, pid):
    fake_win32.pids.update({7: 4242, 8: 44})
    assert platform.current().window_owned_by(window_id, pid) is (window_id == 7 and pid == 4242)
