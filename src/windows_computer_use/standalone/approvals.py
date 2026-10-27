"""Serialized confirmation checks, halt handling and action journal."""
import asyncio

from .. import gate
from . import config, hookcheck


class LocalApprovals:
    accepts_confirm = True

    def __init__(self, startup_refusal: str | None, hotkey, journal=config.journal_path, confirm_available=None):
        self.startup_refusal = startup_refusal
        self.hotkey = hotkey
        self.journal = journal
        # A legacy startup boolean cannot establish that the hook is still installed.
        # A callable, read on every confirm; a stored boolean would outlive the owner's settings.
        if confirm_available is not None and not callable(confirm_available):
            raise TypeError("confirm_available must be a callable")
        self.confirm_available = confirm_available or hookcheck.confirm_hook_installed
        self._lock = asyncio.Lock()

    def _outcome(self, tool, params, decision, reason=""):
        record = {
            "conversation_id": None, "tool": tool,
            "bundle_id": params.get("bundle_id"), "pid": params.get("pid"),
            "decision": decision, "reason": reason,
        }
        if decision == "confirmed":
            record["confirm"] = params.get("confirm_text")
        gate.actions_log_append(self.journal(), record)
        approved = decision in {"allow", "confirmed"}
        return {"approved": True} if approved else {"approved": False, "error": reason}

    def preflight(self):
        if self.startup_refusal:
            self._outcome("preflight", {}, "deny", self.startup_refusal)
            return self.startup_refusal
        return None

    async def request(self, tool, params):
        async with self._lock:
            refusal = await self.dispatch_refusal(params)
            if self.hotkey.halted_at is not None:
                return self._outcome(tool, params, "deny", refusal)
            if self.startup_refusal:
                return self._outcome(tool, params, "deny", self.startup_refusal)
            if refusal is not None:
                return self._outcome(tool, params, "deny", refusal)
            decision = gate.decide(tool, params, enabled=config.load()["enabled"] is True, granted=True)
            if decision.kind == "deny":
                return self._outcome(tool, params, "deny", decision.reason)
            critical = params.get("critical_reason") or (decision.reason if decision.kind == "card" else "")
            confirm_available = self.confirm_available() is True
            confirmed = params.get("confirmed") is True and confirm_available
            if tool == "computer_confirm_risky_step":
                if not confirm_available:
                    return self._outcome(tool, params, "deny",
                                         "The confirmation hook is not installed: critical actions cannot be performed in this session.")
                return self._outcome(tool, params, "confirmed", critical)
            if critical and not confirmed:
                if not confirm_available:
                    return self._outcome(tool, params, "deny",
                                         "The confirmation hook is not installed: critical actions cannot be performed in this session.")
                return self._outcome(tool, params, "deny", f"{critical} - this is a critical action: repeat the same call "
                                     "with args.confirm = '<what will be done>'; the user will be asked.")
            return self._outcome(tool, params, "confirmed" if critical else "allow", critical)

    async def dispatch_refusal(self, params) -> str | None:
        if self.hotkey.halted_at is not None:
            return (gate.HALTED_REASON
                    + " To continue computer use, restart the server (Claude Code: /mcp).")
        if not self.hotkey.registered:
            return "The stop hotkey (Ctrl+Alt+Esc) is not working; computer use was stopped."
        return None

    async def batch_stop(self, params):
        if self.hotkey.halted_at is not None or not self.hotkey.registered:
            return "halted"
        if config.load()["enabled"] is not True:
            return "disabled"
        return None
