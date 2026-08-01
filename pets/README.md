# Pets

The desktop pet is loaded from here. Discovery priority:

1. Environment variable `CLAUDE_PET_ID`
2. `pet_id` in `~/.config/pet4agents/config.json` (set via `/pet-set`)
3. `~/.codex/pets/<id>/` (sorted by name, first valid one wins)
4. This directory: `pets/<id>/`

Each pet is a directory with the same layout as Codex:

```
<pet-id>/
├── pet.json           # {id, displayName, description, spritesheetPath, spriteVersionNumber, ...}
└── spritesheet.webp   # RGBA, 8 cols, 192x208 per cell
```

Both pet generations are supported:

| Version | Atlas | `spriteVersionNumber` |
| --- | --- | --- |
| v1 | `1536x1872`, 8 cols x 9 rows | absent or `1` |
| v2 | `1536x2288`, 8 cols x 11 rows | `2` |

v2 keeps the nine v1 animation rows and adds a neutral look cell at row 0 / column 6 plus 16 clockwise look directions filling rows 9-10 (index 0 = up, 4 = right, 8 = down, 12 = left). The daemon uses those to make the pet face the mouse pointer while idle; see `look_at_cursor` in the README.

The spritesheet may be `.webp` or `.png`. If `spritesheetPath` is omitted from `pet.json`, the daemon probes `spritesheet.webp` first, then `spritesheet.png`.

Use the `hatch-pet` skill to generate a new pet, or simply reuse an existing Codex one from `~/.codex/pets/`.
