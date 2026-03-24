#!/usr/bin/env python3
"""
kicad_hb_mover.py  --  KiCad hierarchical block reference backup/restore

Backup and restore component references when moving hierarchical blocks (HB)
between parent sheets in KiCad EEschema.

Workflow:
    1.  python kicad_hb_mover.py --backup  --project ./my.kicad_pro
    2.  Move the HB in KiCad (Ctrl+X in source parent, Ctrl+V in target parent)
    3.  Save and CLOSE KiCad
    4.  python kicad_hb_mover.py --restore --dry-run --project ./my.kicad_pro
    5.  python kicad_hb_mover.py --restore           --project ./my.kicad_pro
    6.  Reopen KiCad and verify

How it works:
    For each sheet symbol found in any parent schematic, a custom property
    "__RefIdByPosition" is written directly into the sheet symbol node:

        (property "__RefIdByPosition" "R5@150.0000,75.0000\\nC12@160.0000,75.0000\\n" ...)

    The property travels automatically with Ctrl+X / Ctrl+V because it lives
    inside the sheet symbol node -- no sidecar JSON, no UUID tracking.

    Restore key: component position (at X Y) inside the child sheet.
    This is stable as long as components are not moved inside the child sheet
    between backup and restore.
"""

import argparse
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

VERBOSE  = False
PROP_NAME  = "__RefIdByPosition"
PROP_SAVED = "__HBSaved"


def dbg(msg):
    if VERBOSE:
        print(f"  [DBG] {msg}")

def info(msg):
    print(f"[INFO] {msg}")

def warn(msg):
    print(f"[WARN] {msg}")

def fatal(msg):
    print(f"[ERR]  {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# S-expression parser
# ---------------------------------------------------------------------------

def _tokenize(text):
    return re.findall(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+', text)


def _parse_node(tokens, i):
    assert tokens[i] == '(', f"Expected '(' at token {i}, got '{tokens[i]}'"
    i += 1
    node = []
    while i < len(tokens):
        t = tokens[i]
        if t == ')':
            return node, i + 1
        elif t == '(':
            child, i = _parse_node(tokens, i)
            node.append(child)
        else:
            node.append(t[1:-1] if (t.startswith('"') and t.endswith('"')) else t)
            i += 1
    raise SyntaxError("Unclosed s-expression -- reached end of file")


def parse_sexp(text):
    tokens = _tokenize(text)
    dbg(f"Tokenized: {len(tokens)} tokens")
    roots = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == '(':
            node, i = _parse_node(tokens, i)
            roots.append(node)
        elif t == ')':
            raise SyntaxError(f"Unexpected ')' at token {i}")
        else:
            i += 1
    return roots


def child_val(node, key):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == key:
            return c[1] if len(c) > 1 else None
    return None


def direct_children(node, key):
    for c in node[1:]:
        if isinstance(c, list) and c and c[0] == key:
            yield c


# ---------------------------------------------------------------------------
# KiCad helpers
# ---------------------------------------------------------------------------

def get_placed_symbols(kicad_sch_node):
    result = []
    for child in kicad_sch_node[1:]:
        if not (isinstance(child, list) and child and child[0] == 'symbol'):
            continue
        if (len(child) > 1
                and isinstance(child[1], list)
                and child[1]
                and child[1][0] == 'lib_id'):
            result.append(child)
    return result


def get_sheet_nodes(kicad_sch_node):
    return [c for c in kicad_sch_node[1:]
            if isinstance(c, list) and c and c[0] == 'sheet']


def get_sheetname_at(sheet_node):
    """Return (x, y) of the Sheetname property (at ...) node, or None."""
    for prop in direct_children(sheet_node, 'property'):
        if (len(prop) > 2
                and isinstance(prop[1], str)
                and prop[1] in ('Sheetname', 'Sheet name')
                and isinstance(prop[2], str)):
            at_node = next(direct_children(prop, 'at'), None)
            if at_node and len(at_node) >= 3:
                try:
                    return (float(at_node[1]), float(at_node[2]))
                except (ValueError, TypeError):
                    pass
    return None


def get_sheetfile(sheet_node):
    for prop in direct_children(sheet_node, 'property'):
        if (len(prop) > 2
                and isinstance(prop[1], str)
                and prop[1] in ('Sheetfile', 'Sheet file')
                and isinstance(prop[2], str)):
            return prop[2]
    return None


def get_symbol_at(sym_node):
    at_node = next(direct_children(sym_node, 'at'), None)
    if at_node is None or len(at_node) < 3:
        return None
    try:
        return (round(float(at_node[1]), 4), round(float(at_node[2]), 4))
    except (ValueError, TypeError):
        return None


def get_symbol_reference(sym_node, sheet_uuid=None):
    """
    Extract the reference for this symbol.

    If sheet_uuid is given: return the reference from the (path "...") entry
    whose path string contains sheet_uuid -- i.e. the reference for THAT
    specific instance. This is required when a child sheet is used multiple
    times (multi-instance) and each instance has a different reference.

    If sheet_uuid is None: return the first valid (non-?) reference found.
    """
    for inst in direct_children(sym_node, 'instances'):
        for proj in direct_children(inst, 'project'):
            for path_node in direct_children(proj, 'path'):
                path_str = path_node[1] if len(path_node) > 1 else ''
                ref      = child_val(path_node, 'reference')
                if not ref or ref.endswith('?'):
                    continue
                if sheet_uuid is None or sheet_uuid in path_str:
                    return ref
    # Fallback to Reference property (single-sheet projects)
    for prop in direct_children(sym_node, 'property'):
        if (len(prop) > 2
                and isinstance(prop[1], str)
                and prop[1] == 'Reference'
                and isinstance(prop[2], str)
                and not prop[2].endswith('?')):
            return prop[2]
    return None


def get_symbol_uuid(sym_node):
    return child_val(sym_node, 'uuid')


# ---------------------------------------------------------------------------
# Property encoding / decoding
# ---------------------------------------------------------------------------

def encode_prop(pos_ref_map):
    """
    Encode {(x_mm, y_mm): ref} as "REF@X,Y\\n...".
    Positions stored in mm with 4 decimal places.
    Example: "R5@150.0000,75.0000\\nC12@160.0000,80.0000\\n"
    """
    lines = []
    for (x, y), ref in sorted(pos_ref_map.items(), key=lambda kv: kv[1]):
        lines.append(f"{ref}@{x:.4f},{y:.4f}")
    return '\\n'.join(lines) + '\\n' if lines else ''


def decode_prop(value):
    """
    Decode __RefIdByPosition string back to {(x_mm, y_mm): ref}.
    Accepts both literal \\n (as stored in file) and real newlines.
    """
    result = {}
    value  = value.replace('\\n', '\n')
    for line in value.splitlines():
        line = line.strip()
        if not line or '@' not in line:
            continue
        ref, coords = line.split('@', 1)
        if ',' not in coords:
            warn(f"  Malformed coords in {PROP_NAME}: '{line}'")
            continue
        try:
            x, y = coords.split(',', 1)
            result[(round(float(x), 4), round(float(y), 4))] = ref.strip()
        except ValueError:
            warn(f"  Unparseable coords in {PROP_NAME}: '{line}'")
    return result




# ---------------------------------------------------------------------------
# Raw-text property injection
# ---------------------------------------------------------------------------

def _find_sheet_span(text, sheet_uuid):
    """Return (sheet_start, sheet_end) for the (sheet ...) node containing uuid."""
    uuid_literal = f'(uuid "{sheet_uuid}")'
    uuid_idx     = text.find(uuid_literal)
    if uuid_idx == -1:
        return None, None
    sheet_start = text.rfind('(sheet', 0, uuid_idx)
    if sheet_start == -1:
        return None, None
    depth = 0
    for i in range(sheet_start, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return sheet_start, i + 1
    return None, None


def _find_prop_span(text, sheet_start, sheet_end, prop_name):
    """Return (prop_start, prop_end) of named property inside sheet span."""
    prop_literal = f'(property "{prop_name}"'
    prop_idx     = text.find(prop_literal, sheet_start, sheet_end)
    if prop_idx == -1:
        return None, None
    depth = 0
    for i in range(prop_idx, sheet_end):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return prop_idx, i + 1
    return None, None


def _build_property_text(prop_name, value, ref_x, ref_y,
                          hidden=False, show_name=False):
    show_name_line = '\t\t\t(show_name yes)\n' if show_name else ''
    hide_line      = '\t\t\t\t(hide yes)\n'   if hidden    else ''
    return (
        f'\t\t(property "{prop_name}" "{value}"\n'
        f'\t\t\t(at {ref_x:.2f} {ref_y:.2f} 0)\n'
        + show_name_line +
        f'\t\t\t(effects\n'
        f'\t\t\t\t(font\n'
        f'\t\t\t\t\t(size 1.27 1.27)\n'
        f'\t\t\t\t)\n'
        + hide_line +
        f'\t\t\t)\n'
        f'\t\t)'
    )


def upsert_sheet_property(text, sheet_uuid, prop_name, prop_value,
                           ref_x, ref_y, sch_name, hidden=False, show_name=False):
    sheet_start, sheet_end = _find_sheet_span(text, sheet_uuid)
    if sheet_start is None:
        warn(f"  [{sch_name}] Cannot find sheet node for UUID {sheet_uuid[:8]}")
        return text

    prop_start, prop_end = _find_prop_span(text, sheet_start, sheet_end, prop_name)
    new_prop = _build_property_text(prop_name, prop_value, ref_x, ref_y, hidden, show_name)

    if prop_start is not None:
        dbg(f"  [{sch_name}] Updating {prop_name} on sheet {sheet_uuid[:8]}")
        return text[:prop_start] + new_prop + text[prop_end:]
    else:
        # Insert before the closing ')' of the sheet node
        sheet_close = sheet_end - 1
        dbg(f"  [{sch_name}] Inserting {prop_name} on sheet {sheet_uuid[:8]}")
        return text[:sheet_close] + '\n' + new_prop + '\n\t' + text[sheet_close:]


def remove_sheet_property(text, sheet_uuid, prop_name, sch_name):
    sheet_start, sheet_end = _find_sheet_span(text, sheet_uuid)
    if sheet_start is None:
        return text
    prop_start, prop_end = _find_prop_span(text, sheet_start, sheet_end, prop_name)
    if prop_start is None:
        return text
    # eat leading whitespace/newline
    ws = prop_start
    while ws > 0 and text[ws - 1] in (' ', '\t'):
        ws -= 1
    if ws > 0 and text[ws - 1] == '\n':
        ws -= 1
    dbg(f"  [{sch_name}] Removing {prop_name} from sheet {sheet_uuid[:8]}")
    return text[:ws] + text[prop_end:]


# ---------------------------------------------------------------------------
# Schematic tree walker
# ---------------------------------------------------------------------------

def collect_sch_files(root_sch):
    visited = set()

    def walk(sch_file):
        sch_file = sch_file.resolve()
        if sch_file in visited:
            dbg(f"Already visited: {sch_file.name} (multi-instance or cycle)")
            return
        visited.add(sch_file)
        if not sch_file.exists():
            warn(f"Referenced sheet not found: {sch_file}")
            return
        try:
            text = sch_file.read_text(encoding='utf-8')
        except Exception as e:
            warn(f"Cannot read {sch_file.name}: {e}")
            return
        try:
            roots = parse_sexp(text)
        except SyntaxError as e:
            warn(f"Parse error in {sch_file.name}: {e}")
            return
        kicad_sch = next(
            (r for r in roots if isinstance(r, list) and r and r[0] == 'kicad_sch'),
            None
        )
        if kicad_sch is None:
            return
        for sheet_node in get_sheet_nodes(kicad_sch):
            fname = get_sheetfile(sheet_node)
            if fname:
                child = sch_file.parent / fname
                dbg(f"  {sch_file.name} -> {fname}")
                walk(child)

    walk(root_sch)
    info(f"Reachable schematic files from root: {len(visited)}")
    return visited


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def do_backup(project_path, clear=False):
    root_sch = project_path.with_suffix('.kicad_sch')
    if not root_sch.exists():
        fatal(f"Root schematic not found: {root_sch}")

    info(f"Root schematic : {root_sch.name}")
    sch_files = sorted(collect_sch_files(root_sch))
    info(f"Scanning {len(sch_files)} reachable schematic file(s)")

    proj_dir       = project_path.parent
    total_sheets   = 0
    total_comps    = 0
    total_files    = 0

    for sch_file in sch_files:
        rel = sch_file.relative_to(proj_dir)
        dbg(f"Scanning {rel}")
        try:
            text = sch_file.read_text(encoding='utf-8')
        except Exception as e:
            warn(f"Cannot read {rel}: {e}")
            continue
        try:
            roots = parse_sexp(text)
        except SyntaxError as e:
            warn(f"Parse error in {rel}: {e}")
            continue

        kicad_sch = next(
            (r for r in roots if isinstance(r, list) and r and r[0] == 'kicad_sch'),
            None
        )
        if kicad_sch is None:
            continue

        sheet_nodes = get_sheet_nodes(kicad_sch)
        if not sheet_nodes:
            continue

        new_text      = text
        file_modified = False

        for sheet_node in sheet_nodes:
            sheet_uuid = child_val(sheet_node, 'uuid')
            if not sheet_uuid:
                warn(f"  [{rel}] Sheet symbol without UUID -- skipping")
                continue

            fname = get_sheetfile(sheet_node)
            if not fname:
                warn(f"  [{rel}] Sheet {sheet_uuid[:8]} has no Sheetfile -- skipping")
                continue

            if clear:
                before   = new_text
                new_text = remove_sheet_property(new_text, sheet_uuid, PROP_NAME,  str(rel))
                new_text = remove_sheet_property(new_text, sheet_uuid, PROP_SAVED, str(rel))
                if new_text != before:
                    file_modified = True
                continue

            child_path = sch_file.parent / fname
            if not child_path.exists():
                warn(f"  [{rel}] Child not found: {fname} -- skipping")
                continue

            try:
                child_text = child_path.read_text(encoding='utf-8')
            except Exception as e:
                warn(f"  [{rel}] Cannot read child {fname}: {e}")
                continue
            try:
                child_roots = parse_sexp(child_text)
            except SyntaxError as e:
                warn(f"  [{rel}] Parse error in {fname}: {e}")
                continue

            child_sch = next(
                (r for r in child_roots
                 if isinstance(r, list) and r and r[0] == 'kicad_sch'),
                None
            )
            if child_sch is None:
                continue

            placed      = get_placed_symbols(child_sch)
            pos_ref_map = {}

            for sym in placed:
                pos = get_symbol_at(sym)
                if pos is None:
                    continue
                ref = get_symbol_reference(sym, sheet_uuid)
                if ref is None:
                    lib_id = child_val(sym, 'lib_id') or '?'
                    dbg(f"  [{rel}/{fname}] {lib_id} at {pos}: no valid ref -- skipping")
                    continue
                if pos in pos_ref_map:
                    warn(f"  [{rel}/{fname}] POSITION COLLISION at {pos}: "
                         f"'{pos_ref_map[pos]}' vs '{ref}' -- keeping first")
                else:
                    pos_ref_map[pos] = ref
                    dbg(f"    {ref} @ {pos[0]:.4f},{pos[1]:.4f}")

            if not pos_ref_map:
                dbg(f"  [{rel}] No valid refs in {fname} -- skipping")
                continue

            prop_value = encode_prop(pos_ref_map)
            dbg(f"  [{rel}] Sheet {sheet_uuid[:8]} ({fname}): "
                f"{len(pos_ref_map)} refs")

            sn_at = get_sheetname_at(sheet_node)
            if sn_at:
                ref_x = sn_at[0] + 10.0
                ref_y = sn_at[1] + 10.0
            else:
                at_node = next(direct_children(sheet_node, 'at'), None)
                try:
                    ref_x = float(at_node[1]) + 10.0 if at_node else 0.0
                    ref_y = float(at_node[2]) + 10.0 if at_node else 0.0
                except (TypeError, ValueError, IndexError):
                    ref_x, ref_y = 0.0, 0.0

            new_text = upsert_sheet_property(
                new_text, sheet_uuid, PROP_NAME, prop_value,
                ref_x, ref_y, str(rel), hidden=True
            )
            new_text = upsert_sheet_property(
                new_text, sheet_uuid, PROP_SAVED, 'YES',
                ref_x, ref_y, str(rel), hidden=False, show_name=True
            )
            file_modified  = True
            total_sheets  += 1
            total_comps   += len(pos_ref_map)

        if file_modified:
            sch_file.write_text(new_text, encoding='utf-8')
            info(f"  [{rel}] Saved")
            total_files += 1

    if not clear:
        info(f"Backup complete: {total_sheets} sheet symbol(s), "
             f"{total_comps} component ref(s), {total_files} file(s) written")
    else:
        info(f"Clear complete: {total_files} file(s) modified")


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def patch_references_by_instance(text, sym, expected_ref, sheet_uuid,
                                   comp_uuid, lib_id, sch_name, dry_run):
    """
    Patch (reference "...") inside the (path "...") entry whose path string
    contains sheet_uuid, for the component identified by comp_uuid.

    Returns (new_text, changed, old_ref).
    """
    uuid_literal = f'(uuid "{comp_uuid}")'
    uuid_idx     = text.find(uuid_literal)
    if uuid_idx == -1:
        warn(f"  [{sch_name}] UUID '{comp_uuid[:8]}' not found in text")
        return text, False, None

    # Find instances block
    inst_search_end = min(uuid_idx + 8000, len(text))
    inst_idx = text.find('(instances', uuid_idx, inst_search_end)
    if inst_idx == -1:
        warn(f"  [{sch_name}] No instances block for {comp_uuid[:8]}")
        return text, False, None

    # Find end of instances block
    depth    = 0
    inst_end = inst_idx
    for i in range(inst_idx, inst_search_end):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                inst_end = i + 1
                break

    instances_text = text[inst_idx:inst_end]

    # Find the (path "...sheet_uuid...") entry
    # The path string contains sheet_uuid somewhere in it
    path_pattern = re.compile(r'\(path\s+"([^"]*)"')
    for pm in path_pattern.finditer(instances_text):
        path_str = pm.group(1)
        if sheet_uuid not in path_str:
            continue

        # Found the right path entry -- find its (reference "...")
        path_start = pm.start()
        # find end of this path node
        depth = 0
        path_end = path_start
        for i in range(path_start, len(instances_text)):
            if instances_text[i] == '(':
                depth += 1
            elif instances_text[i] == ')':
                depth -= 1
                if depth == 0:
                    path_end = i + 1
                    break

        path_block = instances_text[path_start:path_end]
        ref_match  = re.search(r'\(reference\s+"([^"]*)"', path_block)
        if not ref_match:
            warn(f"  [{sch_name}] No (reference) in path block for "
                 f"{comp_uuid[:8]}, path '{path_str[:40]}'")
            return text, False, None

        old_ref = ref_match.group(1)
        if old_ref == expected_ref:
            return text, False, old_ref   # already correct

        if dry_run:
            return text, True, old_ref   # signal change without modifying

        # Compute absolute offsets and patch
        abs_path_start = inst_idx + path_start
        abs_ref_start  = abs_path_start + ref_match.start(1)
        abs_ref_end    = abs_path_start + ref_match.end(1)
        new_text = text[:abs_ref_start] + expected_ref + text[abs_ref_end:]
        dbg(f"    {comp_uuid[:8]}: '{old_ref}' -> '{expected_ref}'")
        return new_text, True, old_ref

    dbg(f"  [{sch_name}] {lib_id} {comp_uuid[:8]}: "
        f"no path containing '{sheet_uuid[:8]}' found in instances block")
    return text, False, None


def do_restore(project_path, dry_run):
    root_sch = project_path.with_suffix('.kicad_sch')
    if not root_sch.exists():
        fatal(f"Root schematic not found: {root_sch}")

    info(f"Root schematic : {root_sch.name}")
    sch_files = sorted(collect_sch_files(root_sch))
    info(f"Processing {len(sch_files)} reachable schematic file(s)")

    proj_dir = project_path.parent

    stats = {
        "patched":       0,
        "already_ok":    0,
        "no_prop":       0,
        "pos_not_found": 0,
        "patch_failed":  0,
    }

    # Phase 1: collect backup maps from all parent sheets
    # child_path -> list of (sheet_uuid, pos_ref_map, parent_rel_str)
    child_instance_map = {}

    for sch_file in sch_files:
        rel = sch_file.relative_to(proj_dir)
        try:
            text  = sch_file.read_text(encoding='utf-8')
            roots = parse_sexp(text)
        except Exception as e:
            warn(f"Cannot read/parse {rel}: {e}")
            continue

        kicad_sch = next(
            (r for r in roots if isinstance(r, list) and r and r[0] == 'kicad_sch'),
            None
        )
        if kicad_sch is None:
            continue

        for sheet_node in get_sheet_nodes(kicad_sch):
            sheet_uuid = child_val(sheet_node, 'uuid')
            fname      = get_sheetfile(sheet_node)
            if not sheet_uuid or not fname:
                continue

            backup_val = None
            for prop in direct_children(sheet_node, 'property'):
                if (len(prop) > 2
                        and isinstance(prop[1], str)
                        and prop[1] == PROP_NAME
                        and isinstance(prop[2], str)):
                    backup_val = prop[2]
                    break

            if backup_val is None:
                dbg(f"  [{rel}] Sheet {sheet_uuid[:8]} ({fname}): no {PROP_NAME}")
                stats["no_prop"] += 1
                continue

            pos_ref_map = decode_prop(backup_val)
            if not pos_ref_map:
                warn(f"  [{rel}] Sheet {sheet_uuid[:8]}: {PROP_NAME} empty/unreadable")
                continue

            child_key = (sch_file.parent / fname).resolve()
            if child_key not in child_instance_map:
                child_instance_map[child_key] = []
            child_instance_map[child_key].append(
                (sheet_uuid, pos_ref_map, str(rel))
            )
            dbg(f"  [{rel}] Sheet {sheet_uuid[:8]} ({fname}): "
                f"{len(pos_ref_map)} backup entries")

    if not child_instance_map:
        warn("No backup properties found. Did you run --backup first?")
        return

    info(f"Found backup data for {len(child_instance_map)} unique child sheet(s)")

    # Phase 2: patch child sheets
    for child_path, instances in child_instance_map.items():
        rel = child_path.relative_to(proj_dir)
        dbg(f"Processing child: {rel} ({len(instances)} instance(s))")

        try:
            text  = child_path.read_text(encoding='utf-8')
            roots = parse_sexp(text)
        except Exception as e:
            warn(f"Cannot read/parse {rel}: {e}")
            continue

        kicad_sch = next(
            (r for r in roots if isinstance(r, list) and r and r[0] == 'kicad_sch'),
            None
        )
        if kicad_sch is None:
            continue

        placed          = get_placed_symbols(kicad_sch)
        new_text        = text
        patched_in_file = 0

        for sym in placed:
            pos       = get_symbol_at(sym)
            lib_id    = child_val(sym, 'lib_id') or '?'
            comp_uuid = get_symbol_uuid(sym)

            if pos is None or comp_uuid is None:
                continue

            for sheet_uuid, pos_ref_map, parent_rel in instances:
                if pos not in pos_ref_map:
                    expected_ref = None
                else:
                    expected_ref = pos_ref_map[pos]
                if expected_ref is None:
                    dbg(f"  [{rel}] {lib_id} at {pos} not in backup "
                        f"for instance {sheet_uuid[:8]}")
                    stats["pos_not_found"] += 1
                    continue

                # Check current ref for this specific instance
                current_ref  = None
                for inst in direct_children(sym, 'instances'):
                    for proj in direct_children(inst, 'project'):
                        for path_node in direct_children(proj, 'path'):
                            path_str = path_node[1] if len(path_node) > 1 else ''
                            if sheet_uuid in path_str:
                                current_ref = child_val(path_node, 'reference')

                if current_ref == expected_ref:
                    dbg(f"  [{rel}] {lib_id} {comp_uuid[:8]}: '{expected_ref}' OK")
                    stats["already_ok"] += 1
                    continue

                if dry_run:
                    info(f"  [DRY-RUN] [{rel}] {lib_id} {comp_uuid[:8]}: "
                         f"'{current_ref}' -> '{expected_ref}'")
                    stats["patched"] += 1
                    continue

                new_text, changed, old_ref = patch_references_by_instance(
                    new_text, sym, expected_ref, sheet_uuid,
                    comp_uuid, lib_id, str(rel), dry_run=False
                )
                if changed:
                    info(f"  [{rel}] {lib_id} {comp_uuid[:8]}: "
                         f"'{old_ref}' -> '{expected_ref}'")
                    stats["patched"]      += 1
                    patched_in_file       += 1
                elif old_ref is not None:
                    stats["already_ok"] += 1
                else:
                    warn(f"  [{rel}] PATCH FAILED: {lib_id} {comp_uuid[:8]} "
                         f"expected '{expected_ref}'")
                    stats["patch_failed"] += 1

        if not dry_run and patched_in_file > 0:
            child_path.write_text(new_text, encoding='utf-8')
            info(f"  [{rel}] Saved -- {patched_in_file} reference(s) patched")

    print()
    info("=== RESTORE SUMMARY ===")
    info(f"  Patched             : {stats['patched']}")
    info(f"  Already correct     : {stats['already_ok']}")
    info(f"  No backup property  : {stats['no_prop']}  (sheet symbols without {PROP_NAME})")
    info(f"  Position not found  : {stats['pos_not_found']}  (components new since backup)")
    if stats["patch_failed"]:
        warn(f"  PATCH FAILURES      : {stats['patch_failed']}  <- review warnings above!")
    if dry_run:
        info("(DRY-RUN -- no files were written)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global VERBOSE

    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--backup',  action='store_true',
                      help='Write __RefIdByPosition into every sheet symbol')
    mode.add_argument('--restore', action='store_true',
                      help='Restore references from __RefIdByPosition properties')
    mode.add_argument('--clear',   action='store_true',
                      help='Remove all __RefIdByPosition properties (cleanup)')

    ap.add_argument('--project', required=True, type=Path,
                    metavar='FILE.kicad_pro',
                    help='Path to the KiCad project file (.kicad_pro)')
    ap.add_argument('--dry-run', action='store_true',
                    help='(restore only) Show changes without writing files')
    ap.add_argument('--verbose', '-v', action='store_true',
                    help='Enable detailed debug output')

    args = ap.parse_args()
    VERBOSE = args.verbose

    project_path = args.project.resolve()
    if not project_path.exists():
        fatal(f"Project file not found: {project_path}")
    if project_path.suffix != '.kicad_pro':
        warn(f"Expected a .kicad_pro file, got: {project_path.name}")

    if args.backup:
        info("=== BACKUP MODE ===")
        info(f"Project : {project_path}")
        do_backup(project_path, clear=False)

    elif args.restore:
        info("=== RESTORE MODE (DRY-RUN) ===" if args.dry_run
             else "=== RESTORE MODE ===")
        info(f"Project : {project_path}")
        do_restore(project_path, args.dry_run)

    elif args.clear:
        info("=== CLEAR MODE ===")
        info(f"Project : {project_path}")
        do_backup(project_path, clear=True)


if __name__ == '__main__':
    main()
