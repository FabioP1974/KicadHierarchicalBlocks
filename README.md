# kicad_hb_mover

A Python script to safely move hierarchical blocks between parent sheets in KiCad EEschema
without losing component reference designators.

---

## Background

KiCad stores component references (R1, C5, U3 ...) as instance paths inside each `.kicad_sch`
file. The path encodes the full hierarchy from the root sheet down to the component:

```
/root-uuid/parent-sheet-uuid/component-uuid
```

When you move a hierarchical block (sheet symbol) from one parent sheet to another using
Ctrl+X / Ctrl+V, KiCad updates the path correctly — but resets all reference designators
for the affected instance to `R?`, `C?`, `U?` and so on. On a large project with hundreds
of components spread across many hierarchical blocks, re-annotating everything by hand is
error-prone and time-consuming.

This problem is particularly difficult when:
- The same child sheet is instantiated multiple times (multi-instance sheets)
- The hierarchy is deeply nested
- Several hierarchical blocks need to be reorganised at once

---

## How it works

Before the move, `--backup` writes a custom property `__RefIdByPosition` directly inside
each sheet symbol node in the parent schematic:

```
(property "__RefIdByPosition" "C12@160.0000,80.0000\nR5@150.0000,75.0000\n"
    (at ...)
    (effects (font (size 1.27 1.27)) (hide yes))
)
(property "__HBSaved" "YES"
    (at ...)
    (show_name yes)
    (effects (font (size 1.27 1.27)))
)
```

Because the property lives **inside the sheet symbol node**, it travels automatically
with Ctrl+X / Ctrl+V — no sidecar files, no UUID tracking.

The restore key is the component position `(at X Y)` inside the child sheet, which is
stable across hierarchy moves as long as components are not repositioned inside the child
sheet between backup and restore.

After the move, `--restore` reads the backup properties and patches the correct
`(reference "...")` entry for each component instance, leaving all other instances
untouched.

A visible marker property `__HBSaved: YES` appears near the sheet symbol on the schematic,
making it easy to see at a glance which blocks have been backed up.

---

## Requirements

- Python 3.7+
- No external dependencies — pure standard library
- KiCad 8 or 9 (both `"Sheet file"` and `"Sheetfile"` property names supported)

---

## kicad_hb_mover — Quick Reference

### Workflow

```
1. python kicad_hb_mover.py --backup   --project MY.kicad_pro
2. KiCad: Ctrl+X sheet symbol -> navigate to target parent -> Ctrl+V -> Save -> Close KiCad
3. python kicad_hb_mover.py --restore  --dry-run --project MY.kicad_pro
4. python kicad_hb_mover.py --restore            --project MY.kicad_pro
5. Reopen KiCad and verify
```

### Commands

| Command | Description |
|---|---|
| `--backup  --project F.kicad_pro` | Write `__RefIdByPosition` (hidden) and `__HBSaved: YES` (visible) into every sheet symbol |
| `--restore --project F.kicad_pro` | Restore references from the backup properties |
| `--restore --dry-run --project F.kicad_pro` | Preview what would be patched, no files written |
| `--clear   --project F.kicad_pro` | Remove all `__RefIdByPosition` and `__HBSaved` properties |
| `-v` / `--verbose` | Add to any command for detailed debug output |

### Notes

- **KiCad must be closed** before running `--restore`
- **Do not annotate** unannotated symbols between `--backup` and `--restore`
- **Do not move components** inside child sheets between `--backup` and `--restore` (position is the restore key)
- Use **Ctrl+X** to move the sheet symbol — the backup properties travel with it automatically

### How it works

For each sheet symbol found in any parent schematic, `--backup` writes:

```
(property "__RefIdByPosition" "C12@160.0000,80.0000\nR5@150.0000,75.0000\n"
    (at ...)
    (effects (font (size 1.27 1.27)) (hide yes))
)
(property "__HBSaved" "YES"
    (at ...)
    (show_name yes)
    (effects (font (size 1.27 1.27)))
)
```

Both properties live **inside the sheet symbol node** — they travel automatically with Ctrl+X.
Restore key: component `(at X Y)` position inside the child sheet.

---

## License

MIT
