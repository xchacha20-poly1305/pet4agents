---
description: Check the pet4agents config for deprecated, unknown or malformed fields.
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/pet_event.py verify)
---

Run this command exactly once and report the output (a nonzero exit means the config has problems):

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/pet_event.py verify
```
