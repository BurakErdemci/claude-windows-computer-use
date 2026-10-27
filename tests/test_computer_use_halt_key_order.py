"""Keep batch stop markers ahead of step details in serialized reports."""
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from conftest import FakeApprovals
from windows_computer_use import driver_client
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
    return driver, approval, runtime


@pytest.mark.parametrize("reason", ["halted", "disabled", "unverified", None])
def test_batch_report_key_order(fake, reason):
    driver, approval, runtime = fake
    approvals = FakeApprovals(approval)
    approvals.batch_stop = AsyncMock(side_effect=[None, reason])

    async def run():
        handler = ComputerServer(driver, approvals, runtime=runtime)
        try:
            return await handler.call("computer_run_steps", {
                "pid": 4242,
                "steps": [{"type_text": {"text": str(i)}} for i in range(2)],
            })
        finally:
            await handler.close()

    result = asyncio.run(run())
    approval.assert_awaited_once()
    report = json.loads(result.content[0].text)
    existing_keys = ["executed", "total", "failed_step", "ok", "steps"]
    if reason is not None:
        assert list(report)[:3] == ["halted", "stop_reason", "halt_reason"]
        assert list(report)[3:] == existing_keys
        assert report["stop_reason"] == reason
        assert report["executed"] == 1
        assert result.is_error
    else:
        assert list(report)[0] == "executed"
        assert list(report) == existing_keys
        assert report["executed"] == 2
        assert not result.is_error
