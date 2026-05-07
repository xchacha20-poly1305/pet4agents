# claude-code-pet

A Linux desktop pet plugin for [Claude Code](https://claude.com/claude-code) and Codex CLI, reusing the Codex pet format. The pet sits on top of your desktop, runs while the agent works, jumps when work completes, waves when permission is requested, and can be dragged around.

## Requirements

- Linux (X11 or native Wayland)
- Python 3.9+ available as `python3` (pip / venv module included)
- A working desktop session (`DISPLAY` or `WAYLAND_DISPLAY`)
- A Codex-format pet at `~/.codex/pets/<pet-id>/` (`pet.json` + `spritesheet.webp` or `spritesheet.png`)

PySide6 is auto-installed into a private venv at `~/.local/share/claude-code-pet/venv/` on first hook fire (~60 MB download, one-time).

## Install

### Claude Code

If you didn't add [my plugin marketplace](https://codeberg.org/xchacha20-poly1305/cc-plugin), add it first.

```shell
claude plugin marketplace add https://codeberg.org/xchacha20-poly1305/cc-plugin.git
```

Then you can install it:

```shell
claude plugin install pet4claude@anrong-plugins
```

### Codex

Add my plugin marketplace if not have:

```shell
codex plugin marketplace add https://codeberg.org/xchacha20-poly1305/cc-plugin.git
```

Codex has no `plugin install` CLI subcommand — install through the TUI: launch `codex`, run `/plugins`, pick `pet4claude@anrong-plugins`, and confirm.

Enable `plugin_hooks` feature in `~/.codex/config.toml`:

```toml
[features]
plugin_hooks = true
```

Then trust the hooks in TUI: run `/hooks`, enter each hook, then press `t` to trust.

#### Behavior difference

In Codex, some behaviors may different.

- The pet will be delayed to display after sending your first request, not on program starting.

## Behavior

| Agent event            | Pet animation                |
| ---------------------- | ---------------------------- |
| `SessionStart`         | wave once → idle             |
| `UserPromptSubmit`     | running (loop)               |
| `PreToolUse`           | running (loop)               |
| `Stop` / `SubagentStop`| jump once → idle             |
| `Notification`         | review once                  |
| `PermissionRequest`    | wave once                    |
| `PostToolUseFailure`   | failed once                  |
| `SessionEnd`           | wave once → quit if no other sessions remain (override with `stay_even_no_session`) |
| Drag                   | running-right / running-left / jumping based on motion |

## Slash commands

- `/pet-stop` — quit the pet daemon
- `/pet-set <pet-id>` — switch to a different pet from `~/.codex/pets/`

## Config

`~/.config/claude-code-pet/config.json`:

```json
{
  "pet_id": "claude-muse",
  "stay_even_no_session": false,
  "animation_durations": {
    "idle":          [280, 110, 110, 140, 140, 320],
    "running-right": [120, 120, 120, 120, 120, 120, 120, 220],
    "waving":        [140, 140, 140, 280]
  }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `pet_id` | (auto-discover) | Which pet folder under `~/.codex/pets/` (or this plugin's `pets/`) to load. |
| `stay_even_no_session` | `false` | When `false`, the daemon exits shortly after the last agent session ends. Set to `true` to keep the daemon running until you call `/pet-stop` or ask Codex to stop the pet. The "session ends" check covers both the clean `SessionEnd` hook *and* the case where Claude Code / Codex itself crashes / is `SIGKILL`'d / has its terminal closed (the daemon polls the agent PID every 5s as a safety net). |
| `animation_durations` | `{}` | Per-animation per-frame duration overrides in **milliseconds**. Each value is a list, one entry per frame. Length **must** match the default frame count for that animation (the spritesheet row has a fixed number of cells); mismatched, malformed, or unknown entries are silently ignored. Animation names: `idle`, `running-right`, `running-left`, `waving`, `jumping`, `failed`, `waiting`, `running`, `review` — frame counts: see `scripts/config.py:ANIMATIONS`. JSON is the only entry point; there is no slash command for this. |

Override the pet via env var: `CLAUDE_PET_ID=<id>` or `CODEX_PET_ID=<id>`.

## Files / directories used

- venv: `~/.local/share/claude-code-pet/venv/`
- config: `~/.config/claude-code-pet/config.json`
- window position: `~/.config/claude-code-pet/state.json`
- runtime socket: `${XDG_RUNTIME_DIR:-/tmp}/claude-code-pet.sock`
- log: `~/.local/state/claude-code-pet/event.log`

## Uninstall

Remove the daemon's runtime files (venv, config, state, socket):

```bash
rm -rf ~/.local/share/claude-code-pet ~/.config/claude-code-pet ~/.local/state/claude-code-pet
rm -f "${XDG_RUNTIME_DIR:-/tmp}/claude-code-pet.sock"
```

Then remove the plugin from each agent that had it installed.

### Claude Code

```shell
claude plugin uninstall pet4claude@anrong-plugins
```

### Codex CLI

Codex has no `plugin uninstall` CLI subcommand (`codex plugin marketplace remove` only drops a marketplace, not an installed plugin). Uninstall it from the TUI's `/plugins` popup, or do it by hand:

1. Delete the `[plugins."pet4claude@anrong-plugins"]` block from `~/.codex/config.toml`.
2. Drop the cached plugin source:

   ```shell
   rm -rf ~/.codex/plugins/cache/anrong-plugins/pet4claude
   ```

# LICENSE

[MIT](./LICENSE)
