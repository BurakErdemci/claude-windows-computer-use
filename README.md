# windows-computer-use

A Claude Code plugin that lets Claude look at and operate Windows desktop apps (read a window, click, type, press shortcuts) through a local MCP server. It ships a skill that teaches Claude the desktop loop and a hook that makes Claude Code ask you before any critical action.

Windows only.

## Requirements

- Windows 10 or 11
- [uv](https://docs.astral.sh/uv/)
- Claude Code

## Install

In Claude Code:

```
/plugin marketplace add BurakErdemci/claude-windows-computer-use
/plugin install windows-computer-use@windows-computer-use
```

Then download and enable the driver once. The server refuses to start until you do, and its startup message prints this exact command for your install path:

```
uv run --directory "<plugin folder>" windows-computer-use install-driver
```

`install-driver` also turns computer use on. Reconnect the server with `/mcp` afterwards.

## What asks for confirmation

Claude calls these tools with `args.confirm = "<what and why>"` and Claude Code then shows you a permission prompt with that text:

- risky shortcuts;
- targets named send, delete, buy, pay, publish, confirm, submit or share (English and Turkish);
- in mail and chat apps, only sending: Enter or other send chords, and text with a newline.

Everything else (reading windows, clicking, typing in ordinary apps) runs without a prompt. The server accepts `args.confirm` only while the plugin's hook is active, so critical actions are refused if the hook is off.

## Stop key

Ctrl+Alt+Esc halts computer use at once. It resumes only when you reconnect the server with `/mcp`.

## Security model

- A hard-deny list blocks terminals, system settings, password managers and similar apps. Use Claude's own shell tool for those.
- Window ownership: Claude can act only on windows it has listed and read.
- Critical actions are gated by `args.confirm` plus the hook above.
- The server is not started as administrator.
- Text on screen is treated as data, never as instructions.

Limit to know about: the hook runs through `uv`. If `uv` cannot start when a tool is called, the hook gives no answer and Claude Code falls back to its normal permission mode for that call, so a call carrying `args.confirm` may run without your prompt. The server checks the hook's registration only when it starts; it cannot see a later `uv` failure. `disableAllHooks` in your Claude Code settings counts as no hook, and the server then refuses critical actions.

## Third-party driver

The server drives Windows through `cua-driver` 0.35.4 from the [Cua project](https://github.com/trycua/cua). `install-driver` downloads it from PyPI, checks the wheel and executable against pinned SHA-256 hashes, and stores it in `%LOCALAPPDATA%\claude-windows-computer-use`. Its package metadata declares the MIT licence.

## Uninstall

```
/plugin uninstall windows-computer-use@windows-computer-use
```

Uninstalling does not touch the server's state, which lives in `%LOCALAPPDATA%\claude-windows-computer-use` (the downloaded driver, `config.json`, the action journal and log). Delete that folder yourself to remove it.

## Licence

MIT. See [LICENSE](LICENSE).
