"""Shared fixtures: a fake Windows OS layer and fake approvals for server-level tests."""
import os
from unittest.mock import AsyncMock

import pytest

from windows_computer_use import platform
from windows_computer_use.platform.windows import WindowsPlatform

NOTEPAD = r"C:\Windows\System32\notepad.exe"
NOTEPAD_ID = "win:" + NOTEPAD.casefold()


class FakeWin32:
    """Process and window facts for a few fake applications; no desktop access."""

    def __init__(self):
        self.paths = {
            4242: NOTEPAD,
            77: r"C:\Apps\Front.exe",
            44: r"C:\Windows\System32\cmd.exe",
            55: r"C:\Windows\System32\mspaint.exe",
            66: r"C:\Program Files\Opera\opera.exe",
        }
        self.levels = {os.getpid(): 0x2000}
        self.parents = {}
        self.elevated = False
        self.foreground = 0
        self.windows = []
        self.pids = {7: 4242, 9: 4242}
        self.children = {}
        self.info = {}

    def set_dpi_awareness(self):
        pass

    def image_path(self, pid):
        return self.paths.get(pid)

    def integrity(self, pid):
        return self.levels.get(pid, 0x2000 if pid in self.paths else None)

    def self_elevated(self):
        return self.elevated

    def parent_pids(self):
        return self.parents

    def foreground_window(self):
        return self.foreground

    def window_pid(self, hwnd):
        return self.pids.get(hwnd)

    def top_level_windows(self):
        return self.windows

    def window_info(self, hwnd):
        return self.info.get(hwnd)

    def child_windows(self, hwnd):
        return self.children.get(hwnd, [])


class FakeApprovals:
    """Approvals double: `approval` is an AsyncMock called as approval(tool, params=params)."""

    def __init__(self, approval=None, stop=None):
        self.approval = approval if approval is not None else AsyncMock(return_value={"approved": True})
        self.stop = stop

    def preflight(self):
        return None

    async def request(self, tool, params):
        return await self.approval(tool, params=params)

    async def batch_stop(self, params):
        return self.stop


@pytest.fixture
def fake_win32(monkeypatch):
    """Install a WindowsPlatform over FakeWin32 as the current platform."""
    win32 = FakeWin32()
    monkeypatch.setitem(platform._platforms, "win32", WindowsPlatform(win32=win32))
    return win32
