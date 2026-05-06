# claude-code-pet

A Linux desktop pet plugin for [Claude Code](https://claude.com/claude-code), reusing the Codex pet format. The pet sits on top of your desktop, runs while Claude works, jumps when work completes, waves when permission is requested, and can be dragged around.

## Requirements

- Linux (X11 or native Wayland)
- Python 3.9+ available as `python3` (pip / venv module included)
- A working desktop session (`DISPLAY` or `WAYLAND_DISPLAY`)
- A Codex-format pet at `~/.codex/pets/<pet-id>/` (`pet.json` + `spritesheet.webp`)

PySide6 is auto-installed into a private venv at `~/.local/share/claude-code-pet/venv/` on first hook fire (~60 MB download, one-time).

## Install

If you didn't install [my plugin marketplace](https://codeberg.org/xchacha20-poly1305/cc-plugin), install it first.

```shell
claude plugin marketplace add https://codeberg.org/xchacha20-poly1305/cc-plugin.git
```

Then you can install it:

```shell
claude plugin install pet4claude@anrong-plugins
```

## Behavior

| Claude event           | Pet animation                |
| ---------------------- | ---------------------------- |
| `SessionStart`         | wave once → idle             |
| `UserPromptSubmit`     | running (loop)               |
| `PreToolUse`           | running (loop)               |
| `Stop` / `SubagentStop`| jump once → idle             |
| `Notification`         | review once                  |
| `PermissionRequest`    | wave once                    |
| `PostToolUseFailure`   | failed once                  |
| `SessionEnd`           | wave once (daemon stays up)  |
| Drag                   | running-right / running-left / jumping based on motion |

## Slash commands

- `/pet-stop` — quit the pet daemon
- `/pet-set <pet-id>` — switch to a different pet from `~/.codex/pets/`

## Config

`~/.config/claude-code-pet/config.json`:

```json
{
  "pet_id": "claude-muse"
}
```

Override with env var: `CLAUDE_PET_ID=<id>`.

## Files / directories used

- venv: `~/.local/share/claude-code-pet/venv/`
- config: `~/.config/claude-code-pet/config.json`
- window position: `~/.config/claude-code-pet/state.json`
- runtime socket: `${XDG_RUNTIME_DIR:-/tmp}/claude-code-pet.sock`
- log: `~/.local/state/claude-code-pet/event.log`

## Uninstall

```bash
rm -rf ~/.local/share/claude-code-pet ~/.config/claude-code-pet ~/.local/state/claude-code-pet
rm -f "${XDG_RUNTIME_DIR:-/tmp}/claude-code-pet.sock"
```

Then disable the plugin in Claude Code.

# LICENSE

[MIT](./LICENSE)