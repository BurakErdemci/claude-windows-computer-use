"""Installed-app names come only from the driver's installed-app rows."""
import asyncio
import json
import sys
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, TextContent

from conftest import NOTEPAD_ID
from windows_computer_use.driver_client import DriverClient

PAINT_ID = r"win:c:\windows\system32\mspaint.exe"
OTHER_ID = NOTEPAD_ID


@pytest.fixture
def driver():
    # No subprocesses: every driver invocation is replaced by an async stub.
    driver = DriverClient(sys.executable, "unused-socket")
    driver.call = AsyncMock(return_value=CallToolResult(content=[]))
    return driver


def app_result(data, structured=False):
    return CallToolResult(
        content=[] if structured else [TextContent(type="text", text=json.dumps(data))],
        **({"structured_content": data} if structured else {}))


@pytest.mark.parametrize("structured,wrapped", [(False, False), (False, True), (True, True)])
@pytest.mark.parametrize("bundle", [PAINT_ID, PAINT_ID.upper()])
@pytest.mark.parametrize("running,pid", [(False, 0), (True, 77)])
def test_installed_name_uses_driver_row(driver, structured, wrapped, bundle, running, pid):
    rows = [{"bundle_id": OTHER_ID, "name": "Other"},
            {"bundle_id": PAINT_ID, "name": "  Paint  ", "running": running, "pid": pid}]
    driver.call.return_value = app_result({"apps": rows} if wrapped else rows, structured)
    assert asyncio.run(driver.installed_app_name(bundle)) == "Paint"
    driver.call.assert_awaited_once_with("list_apps", {})


@pytest.mark.parametrize("data", [
    None, 42, "bad", {}, {"apps": None}, {"apps": {}}, [None, 42, "bad"],
    [{"bundle_id": None, "name": "Fake"}],
    [{"bundle_id": OTHER_ID, "name": "Other"}],
    *[[{"bundle_id": PAINT_ID, "name": name}]
      for name in (None, "", "  \t", 42, True, [], {})],
])
def test_unknown_or_malformed_rows_have_no_name(driver, data):
    driver.call.return_value = app_result(data)
    assert asyncio.run(driver.installed_app_name(PAINT_ID)) is None


@pytest.mark.parametrize("failure", [
    CallToolResult(content=[], is_error=True),
    CallToolResult(content=[TextContent(type="text", text="invalid json")]),
])
def test_lookup_failure_has_no_name(driver, failure):
    driver.call.return_value = failure
    assert asyncio.run(driver.installed_app_name(PAINT_ID)) is None
