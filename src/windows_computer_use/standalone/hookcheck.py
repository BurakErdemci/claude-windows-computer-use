"""Check whether Claude Code asks the owner before local confirmations."""
import json
import os
from pathlib import Path
import re

from .. import policy

# Claude Code names a plugin's MCP tools mcp__plugin_<plugin>_<server>__<tool>.
TOOL_PREFIX = "mcp__plugin_windows-computer-use_desktop__"
HOOK_MODULE = "windows_computer_use.hook"
# Only the exact hook this plugin ships counts. Anything else (an echo, a wrapper, a hook in
# user settings where ${CLAUDE_PLUGIN_ROOT} is not expanded, an async or zero-timeout hook)
# could make the server accept confirms nobody is asked about.
_HOOK = {"type": "command", "command": "uv",
         "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "--frozen", "--quiet", "python", "-m", HOOK_MODULE]}


def _settings_paths():
    override = os.environ.get("WINDOWS_CU_CLAUDE_SETTINGS")
    if override:
        user = override.split(os.pathsep)
    else:
        config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        user = [config / "settings.json", config / "settings.local.json"]
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()) / ".claude"
    return [*user, project / "settings.json", project / "settings.local.json"]


def _hooks_disabled(paths) -> bool:
    # Claude Code merges the settings files and disableAllHooks is one switch for all of them.
    for path in paths:
        try:
            # utf-8-sig: Windows editors often save settings with a byte-order mark.
            settings = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(settings, dict) and settings.get("disableAllHooks") is True:
            return True
    return False


def confirm_hook_installed(paths=None) -> bool:
    try:
        if _hooks_disabled(_settings_paths() if paths is None else paths):
            return False
        root = os.environ.get("CLAUDE_PLUGIN_ROOT")
        if not root:
            return False
        hooks = json.loads((Path(root) / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        required = [TOOL_PREFIX + tool for tool in policy.TOOLS]
        return any(isinstance(entry, dict) and isinstance(entry.get("matcher"), str)
                   and all(re.fullmatch(entry["matcher"], name) for name in required)
                   and isinstance(entry.get("hooks"), list) and _HOOK in entry["hooks"]
                   for entry in hooks["hooks"]["PreToolUse"])
    except Exception:
        return False
