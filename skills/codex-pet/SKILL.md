---
name: codex-pet
description: Control the Pet4Claude desktop pet from Codex. Use when the user asks to stop the pet daemon, switch the active pet, or inspect pet configuration/logs.
---

# Codex Pet

Pet4Claude is installed as a Codex plugin in this repository. Its hooks launch
`scripts/pet_event.py`, which relays events to the long-running PySide6 daemon.

## Stop the Pet

When the user asks to stop or quit the desktop pet, run:

```bash
./scripts/pet_event.py daemon-stop
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

## Debug

The primary log is `~/.local/state/claude-code-pet/event.log`. The private venv
is `~/.local/share/claude-code-pet/venv/`.
