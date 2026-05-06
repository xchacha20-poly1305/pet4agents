---
description: Switch the active pet (looks under ~/.codex/pets/<id>/). Usage:/pet-set <pet-id>
argument-hint: <pet-id>
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/pet_event.py set-pet:*)
---

The user wants to switch to pet id: `$1`

Run this command exactly once and report the output:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pet_event.py set-pet "$1"
```

If `$1` is empty, tell the user the usage is `/pet-set <pet-id>` and list available pets with:

```bash
ls -1 ~/.codex/pets/ 2>/dev/null
```
