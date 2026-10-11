---
description: Temporarily hide the desktop pet (e.g. for screenshots). Usage:/pet-hide [seconds]
argument-hint: [seconds]
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/pet_event.py hide:*)
---

The user wants to hide the pet for: `$1` seconds (empty means until `/pet-show`).

Run this command exactly once and report the output:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pet_event.py hide "$1"
```
