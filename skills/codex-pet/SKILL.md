---
name: codex-pet
description: Control the pet4agents desktop pet from Codex. Use when the user asks to stop, hide or show the pet, switch the active pet, verify the config, or inspect pet configuration/logs.
---

# Codex Pet

pet4agents is installed as a Codex plugin in this repository. Its hooks launch
`scripts/pet_event.py`, which relays events to the long-running PySide6 daemon.

## Stop the Pet

When the user asks to stop or quit the desktop pet, run:

```bash
./scripts/pet_event.py daemon-stop
```

Report the command output.

## Hide / Show the Pet

When the user asks to hide the pet temporarily (for example to take a
screenshot), run, with an optional number of seconds:

```bash
./scripts/pet_event.py hide [seconds]
```

Without seconds the pet stays hidden until:

```bash
./scripts/pet_event.py show
```

Report the command output.

## Switch Pets

When the user asks to switch to a pet id, run:

```bash
./scripts/pet_event.py set-pet <pet-id>
```

The id is resolved under `~/.codex/pets/<pet-id>/` first, then under this
plugin's `pets/` directory. If the user did not provide an id, list available
pets with:

```bash
ls -1 ~/.codex/pets/ 2>/dev/null
```

## Verify the Config

When the user asks to check or validate the pet configuration, run:

```bash
./scripts/pet_event.py verify
```

It exits 1 and lists the problems when `~/.config/pet4agents/config.json` has
deprecated keys (`claude_pet_id` / `codex_pet_id`), unknown keys, wrongly typed
values, or malformed `process_rules` / `animation_durations` entries. Report
the output and, if there are problems, offer to fix them.

## Debug

The primary log is `~/.local/state/pet4agents/event.log`. The private venv
is `~/.local/share/pet4agents/venv/`.
