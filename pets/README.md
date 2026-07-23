# Pets

The desktop pet is loaded from here. Discovery priority:

1. Environment variable `CLAUDE_PET_ID`
2. `pet_id` in `~/.config/pet4agents/config.json` (set via `/pet-set`)
3. `~/.codex/pets/<id>/` (sorted by name, first valid one wins)
4. This directory: `pets/<id>/`

Each pet is a directory with the same layout as Codex:

```
<pet-id>/
├── pet.json           # {id, displayName, description, spritesheetPath, ...}
└── spritesheet.webp   # 1536x1872 RGBA, 8 cols x 9 rows, 192x208 per cell
```

The spritesheet may be `.webp` or `.png`. If `spritesheetPath` is omitted from `pet.json`, the daemon probes `spritesheet.webp` first, then `spritesheet.png`.

Use the `hatch-pet` skill to generate a new pet, or simply reuse an existing Codex one from `~/.codex/pets/`.
