"""Plugin layout, the confirmation hook and its registration check."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from windows_computer_use import hook
from windows_computer_use.standalone import hookcheck

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "mcp__plugin_windows-computer-use_desktop__"


def run_hook(stdin):
    result = subprocess.run([sys.executable, "-m", "windows_computer_use.hook"], input=stdin, text=True,
                            capture_output=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert result.returncode == 0
    return json.loads(result.stdout)["hookSpecificOutput"] if result.stdout.strip() else None


def event(tool, args=None, tool_input=None):
    return json.dumps({"tool_name": tool, "tool_input": tool_input if tool_input is not None else {"args": args or {}}})


def test_confirm_text_becomes_an_ask():
    out = run_hook(event(PREFIX + "computer_click", {"pid": 1, "confirm": "  send the mail  "}))
    assert out["permissionDecision"] == "ask" and "send the mail" in out["permissionDecisionReason"]


@pytest.mark.parametrize("args", [{"pid": 1}, {"pid": 1, "confirm": ""}, {"pid": 1, "confirm": "   "}])
def test_absent_or_empty_confirm_is_allowed(args):
    assert run_hook(event(PREFIX + "computer_click", args))["permissionDecision"] == "allow"


def test_risky_step_always_asks():
    out = run_hook(event(PREFIX + "computer_confirm_risky_step", {"summary": "delete files"}))
    assert out["permissionDecision"] == "ask" and "delete files" in out["permissionDecisionReason"]


@pytest.mark.parametrize("confirm", [5, True, ["x"], {"a": 1}])
def test_non_string_confirm_asks(confirm):
    assert run_hook(event(PREFIX + "computer_click", {"confirm": confirm}))["permissionDecision"] == "ask"


@pytest.mark.parametrize("stdin", ["not json", "", "[]", json.dumps({"tool_input": {}}),
                                   event(PREFIX + "computer_click", tool_input={"args": "text"})])
def test_unreadable_input_asks(stdin):
    assert run_hook(stdin)["permissionDecision"] == "ask"


def test_foreign_tools_get_no_output():
    assert run_hook(event("Bash", tool_input={"command": "ls"})) is None
    assert run_hook(event("mcp__other__computer_click", {"confirm": "x"})) is None


def test_settings_prefix_is_not_handled():
    # The settings-mode tool name is gone: the hook owns only the plugin prefix.
    assert not hasattr(hook, "SETTINGS_PREFIX")
    assert hook.decide(json.loads(event("mcp__windows-computer-use__computer_click", {"confirm": "x"}))) is None
    tool_input = {"args": {"confirm": "x"}, "note": "windows_computer_use.hook"}
    assert hook.decide({"tool_name": "mcp__windows-computer-use__computer_click", "tool_input": tool_input}) is None


def plugin_root(tmp_path, matcher=PREFIX + ".*", module="windows_computer_use.hook", write=True):
    (tmp_path / "hooks").mkdir()
    if write:
        data = {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [
            {"type": "command", "command": "uv", "args": ["run", "--directory", "${CLAUDE_PLUGIN_ROOT}", "--frozen", "--quiet", "python", "-m", module]}]}]}}
        (tmp_path / "hooks" / "hooks.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


@pytest.fixture
def plugin_env():
    assert hookcheck.TOOL_PREFIX == PREFIX


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Settings lookups that never touch the real ~/.claude or the real project directory."""
    clean = tmp_path / "clean-settings"
    clean.mkdir()
    monkeypatch.setenv("WINDOWS_CU_CLAUDE_SETTINGS", str(clean / "settings.json"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(clean / "project"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    return clean


def write_hooks(root, hook_obj, matcher=PREFIX + ".*"):
    (root / "hooks").mkdir(parents=True, exist_ok=True)
    data = {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [hook_obj]}]}}
    (root / "hooks" / "hooks.json").write_text(json.dumps(data), encoding="utf-8")
    return root


def test_plugin_mode_accepts_the_real_hooks_file(monkeypatch, plugin_env):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    assert hookcheck.confirm_hook_installed(paths=[]) is True


def test_plugin_mode_false_without_hooks_file(monkeypatch, plugin_env, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root(tmp_path, write=False)))
    assert hookcheck.confirm_hook_installed(paths=[]) is False


@pytest.mark.parametrize("matcher, module", [(PREFIX + "computer_click", "windows_computer_use.hook"),
                                             (PREFIX + ".*", "something_else")])
def test_plugin_mode_false_for_partial_matcher_or_other_command(monkeypatch, plugin_env, tmp_path, matcher, module):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root(tmp_path, matcher, module)))
    assert hookcheck.confirm_hook_installed(paths=[]) is False


def test_disable_all_hooks_beats_plugin_hooks(monkeypatch, plugin_env, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
    assert hookcheck.confirm_hook_installed(paths=[settings]) is False


def test_settings_mode_no_longer_counts(monkeypatch, clean_env, tmp_path):
    # A perfect hook in a user settings file is not the plugin hook: ${CLAUDE_PLUGIN_ROOT} is not
    # expanded there, so the server must not accept confirms on its strength.
    good = {"hooks": {"PreToolUse": [{"matcher": hookcheck.TOOL_PREFIX + ".*", "hooks": [dict(hookcheck._HOOK)]}]}}
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    assert hookcheck.confirm_hook_installed(paths=[path]) is False
    monkeypatch.setenv("WINDOWS_CU_CLAUDE_SETTINGS", str(path))
    assert hookcheck.confirm_hook_installed() is False
    empty_root = tmp_path / "empty-root"
    empty_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(empty_root))
    assert hookcheck.confirm_hook_installed(paths=[path]) is False
    assert hookcheck.confirm_hook_installed() is False


def load(rel):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_manifests_use_the_agreed_names():
    plugin = load(".claude-plugin/plugin.json")
    assert plugin["name"] == "windows-computer-use" and plugin["version"] == "0.1.0"
    market = load(".claude-plugin/marketplace.json")
    assert market["name"] == "windows-computer-use"
    assert [(p["name"], p["source"]) for p in market["plugins"]] == [("windows-computer-use", "./")]
    server = load(".mcp.json")["mcpServers"]["desktop"]
    assert server["command"] == "uv"
    assert server["args"] == ["run", "--project", "${CLAUDE_PLUGIN_ROOT}", "--frozen", "--quiet", "windows-computer-use"]
    assert "env" not in server
    entry = load("hooks/hooks.json")["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == PREFIX + ".*"
    assert "windows_computer_use.hook" in entry["hooks"][0]["args"]


def test_skill_has_frontmatter():
    text = (ROOT / "skills" / "windows-computer-use" / "SKILL.md").read_text(encoding="utf-8")
    front = re.match(r"---\r?\n(.*?)\r?\n---", text, re.S).group(1)
    assert re.search(r"^name: windows-computer-use$", front, re.M)
    assert re.search(r"^description: .+", front, re.M)


def test_non_ascii_confirm_survives_a_cp1252_console():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env.update(PYTHONPATH=str(ROOT / "src"), PYTHONUTF8="0")
    stdin = event(PREFIX + "computer_click", {"pid": 1, "confirm": "Mesajı gönder ıİ 😀"})
    result = subprocess.run([sys.executable, "-m", "windows_computer_use.hook"], input=stdin.encode("utf-8"),
                            capture_output=True, cwd=ROOT, env=env)
    assert result.returncode == 0
    out = json.loads(result.stdout.decode("ascii"))["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask" and "Mesajı gönder ıİ 😀" in out["permissionDecisionReason"]


@pytest.mark.parametrize("bad", [[], "", 0, False])
def test_falsy_non_dict_args_ask(bad):
    assert run_hook(event(PREFIX + "computer_click", tool_input={"args": bad}))["permissionDecision"] == "ask"
    assert run_hook(json.dumps({"tool_name": PREFIX + "computer_click", "tool_input": bad}))["permissionDecision"] == "ask"


GOOD_ARGS = list(hookcheck._HOOK["args"])


@pytest.mark.parametrize("directory", ["C:/x;true", "C:/x&&true", "C:/x|true", "$(true)", "`true`", '"a"x',
                                       "C:/x'y", "C:/x>y", "C:/x\ny", "\u200b", "  ", " \u200b "])
def test_unsafe_or_blank_directory_is_rejected(monkeypatch, clean_env, tmp_path, directory):
    args = GOOD_ARGS[:2] + [directory] + GOOD_ARGS[3:]
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(write_hooks(tmp_path, {"type": "command", "command": "uv", "args": args})))
    assert hookcheck.confirm_hook_installed() is False


@pytest.mark.parametrize("command", ["uv run --directory C:/x --frozen --quiet python -m windows_computer_use.hook",
                                     "uv run --directory C:/x;true --frozen --quiet python -m windows_computer_use.hook"])
def test_shell_form_is_rejected(monkeypatch, clean_env, tmp_path, command):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(write_hooks(tmp_path, {"type": "command", "command": command})))
    assert hookcheck.confirm_hook_installed() is False


@pytest.mark.parametrize("key, value", [("async", True), ("timeout", 0), ("timeout", 0.001), ("timeout", "30"),
                                        ("timeout", 5), ("timeout", 30), ("if", "x"), ("shell", "bash"),
                                        ("disabled", True)])
def test_any_extra_hook_key_is_rejected(monkeypatch, clean_env, tmp_path, key, value):
    hook_obj = {**hookcheck._HOOK, key: value}
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(write_hooks(tmp_path, hook_obj)))
    assert hookcheck.confirm_hook_installed() is False
    # Control: the same file with the exact hook is accepted.
    write_hooks(tmp_path, dict(hookcheck._HOOK))
    assert hookcheck.confirm_hook_installed() is True


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_project_level_disable_all_hooks_is_honoured(monkeypatch, clean_env, tmp_path, name):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / name).write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "empty"))
    assert hookcheck.confirm_hook_installed() is True
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    assert hookcheck.confirm_hook_installed() is False


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_disable_all_hooks_in_claude_config_dir_is_honoured(monkeypatch, clean_env, tmp_path, name):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    monkeypatch.delenv("WINDOWS_CU_CLAUDE_SETTINGS")
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    assert hookcheck.confirm_hook_installed() is True
    (config / name).write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
    assert hookcheck.confirm_hook_installed() is False


def test_real_hooks_file_is_accepted_with_clean_settings(monkeypatch, clean_env):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    assert hookcheck.confirm_hook_installed() is True


def test_hook_decodes_raw_utf8_stdin_without_utf8_mode(monkeypatch):
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "0"}
    env.pop("PYTHONIOENCODING", None)
    payload = json.dumps({"tool_name": PREFIX + "computer_click",
                          "tool_input": {"args": {"pid": 1, "confirm": "Mesajı gönder"}}}, ensure_ascii=False)
    assert chr(92) + "u" not in payload and "Mesajı gönder" in payload
    result = subprocess.run([sys.executable, "-m", "windows_computer_use.hook"], input=payload.encode("utf-8"),
                            capture_output=True, cwd=ROOT, env=env)
    assert result.returncode == 0
    out = json.loads(result.stdout.decode("ascii"))["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert out["permissionDecisionReason"] == "Critical action confirmation: Mesajı gönder"


def test_mcp_json_does_not_redirect_state():
    assert "WINDOWS_CU_DIR" not in (ROOT / ".mcp.json").read_text(encoding="utf-8")


def test_disable_all_hooks_with_a_byte_order_mark_is_honoured(monkeypatch, clean_env):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(ROOT))
    assert hookcheck.confirm_hook_installed() is True
    (clean_env / "settings.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"disableAllHooks": True}).encode())
    assert hookcheck.confirm_hook_installed() is False
