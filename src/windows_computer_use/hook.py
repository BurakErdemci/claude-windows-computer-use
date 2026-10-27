"""PreToolUse handler that turns args.confirm into a question to the owner (stdlib only).

Claude Code runs this before every tool of the computer-use server. The server
counts args.confirm only while this hook is registered; the hook makes Claude
Code show its permission prompt whenever a call carries one.
"""
import json
import sys

PLUGIN_PREFIX = "mcp__plugin_windows-computer-use_desktop__"


def _decision(kind, reason=""):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": kind,
                                   "permissionDecisionReason": reason}}


def decide(event):
    """Return the hook output for one event, or None for tools this hook does not own."""
    tool = event["tool_name"]
    if not isinstance(tool, str) or not tool.startswith(PLUGIN_PREFIX):
        return None
    tool_input = event.get("tool_input", {})
    if not isinstance(tool_input, dict):
        raise ValueError("tool_input is not an object")
    args = tool_input.get("args", {})
    if not isinstance(args, dict):
        raise ValueError("args is not an object")
    if tool.endswith("__computer_confirm_risky_step"):
        return _decision("ask", "Critical step confirmation: " + str(args.get("summary") or "")[:300])
    confirm = args.get("confirm")
    if isinstance(confirm, str):
        if confirm.strip():
            return _decision("ask", "Critical action confirmation: " + confirm.strip()[:300])
        return _decision("allow")
    if confirm is not None:
        return _decision("ask", "Invalid confirm field; confirmation required.")
    return _decision("allow")


def main():
    # ASCII-only output: a Windows console encoding must never turn an "ask" into a crash that
    # Claude Code reads as "no answer" and lets the call through.
    try:
        # Claude Code sends UTF-8; read bytes so a cp1252 console does not garble the confirm text.
        output = decide(json.loads(sys.stdin.buffer.read().decode("utf-8")))
        if output is not None:
            print(json.dumps(output, ensure_ascii=True))
    except BaseException as exc:
        print(json.dumps(_decision("ask", f"Computer-use confirmation check failed ({type(exc).__name__}); confirmation required."),
                         ensure_ascii=True))


if __name__ == "__main__":
    main()
