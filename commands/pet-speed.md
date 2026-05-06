---
description: Set the pet animation speed multiplier (1.0 = default, 2.0 = twice as fast, 0.5 = half speed). Usage:/pet-speed <multiplier>
argument-hint: <multiplier>
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/pet_event.py set-speed:*)
---

The user wants to set the pet animation speed multiplier to: `$1`

Run this command exactly once and report the output:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pet_event.py set-speed "$1"
```

If `$1` is empty, tell the user the usage is `/pet-speed <multiplier>` (e.g. `/pet-speed 1.5` for 1.5× speed; valid range 0.1–10.0).
