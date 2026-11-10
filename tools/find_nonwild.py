#!/usr/bin/env python3
"""
Find which of the NUM_POKEMON Pokédex species never appear in any wild encounter
source in Pokémon Yellow:

  - grass/cave and surf tables   data/wild/maps/*.asm
  - Old Rod                      engine/items/item_effects.asm (hardcoded)
  - Good Rod                     data/wild/good_rod.asm
  - Super Rod                    data/wild/super_rod.asm

Version conditionals (IF DEF(_RED) / ELIF / ELSE / ENDC) are tracked so a
species is tagged with the version(s) it appears in. Yellow is single-version,
so in practice every entry is tagged "all", but the parser handles them anyway.

Run from the repo root:  python3 tools/find_nonwild.py
"""

import glob
import os
import re
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def path(*p):
    return os.path.join(ROOT, *p)


def read_lines(rel):
    with open(path(rel), encoding="utf-8") as f:
        return f.read().splitlines()


def strip_comment(line):
    return line.split(";", 1)[0].strip()


# ---------------------------------------------------------------------------
# Species list
# ---------------------------------------------------------------------------

def load_species():
    """Return (species_ids, aliases).

    species_ids: list of constant names in internal-ID order (index 0 = NO_MON),
                 including MissingNo slots.
    aliases:     {ALIAS: SPECIES} for `DEF X EQU Y` lines (e.g. STARTER, RESTLESS_SOUL).
    """
    ids, aliases = [], {}
    for line in read_lines("constants/pokemon_constants.asm"):
        code = strip_comment(line)
        m = re.match(r"const\s+(\w+)$", code)
        if m:
            ids.append(m.group(1))
            continue
        if code == "const_skip":  # MissingNo slot
            ids.append(None)
            continue
        m = re.match(r"DEF\s+(\w+)\s+EQU\s+(\w+)$", code)
        if m:
            aliases[m.group(1)] = m.group(2)
    return ids, aliases


def load_dex():
    """Pokédex-ordered list of species names (DEX_ prefix stripped)."""
    dex = []
    for line in read_lines("constants/pokedex_constants.asm"):
        m = re.match(r"const\s+DEX_(\w+)$", strip_comment(line))
        if m:
            dex.append(m.group(1))
    return dex


def load_pokemon():
    """Return (dex_order_list, species_set, internal_ids, aliases).
    Asserts every DEX_* constant (NUM_POKEMON of them) maps to exactly one internal id."""
    ids, aliases = load_species()
    dex = load_dex()
    dexset = set(dex)
    real = [s for s in ids if s in dexset]
    assert len(real) == len(dex), f"expected {len(dex)} (NUM_POKEMON) real species, got {len(real)}"
    assert set(real) == dexset, f"mismatch: {set(real) ^ dexset}"
    return dex, set(real), ids, aliases


# ---------------------------------------------------------------------------
# Version-conditional tracking
# ---------------------------------------------------------------------------

ALL = "all"


class CondTracker:
    """Track IF/ELIF/ELSE/ENDC nesting; .current() returns a version label."""

    def __init__(self):
        self.stack = []  # list of (label, previous_labels)

    def feed(self, code):
        """Consume a conditional directive. Returns True if line was one."""
        m = re.match(r"IF\s+DEF\((\w+)\)", code)
        if m:
            self.stack.append([m.group(1)])
            return True
        m = re.match(r"ELIF\s+DEF\((\w+)\)", code)
        if m:
            self.stack[-1].append(m.group(1))
            return True
        if code == "ELSE":
            self.stack[-1].append("not(" + "|".join(self.stack[-1]) + ")")
            return True
        if code == "ENDC":
            self.stack.pop()
            return True
        return False

    def current(self):
        if not self.stack:
            return ALL
        return "&".join(frame[-1] for frame in self.stack)


# ---------------------------------------------------------------------------
# Wild sources
# ---------------------------------------------------------------------------

def referenced_wild_labels():
    """Labels pointed to by WildDataPointers (reachable tables)."""
    refs = set()
    for line in read_lines("data/wild/grass_water.asm"):
        m = re.match(r"dw\s+(\w+)", strip_comment(line))
        if m:
            refs.add(m.group(1))
    return refs


def parse_wild_maps(species):
    """Return list of (species, method, location, level, version) and unreferenced labels."""
    out, defined = [], set()
    refs = referenced_wild_labels()
    for fn in sorted(glob.glob(path("data/wild/maps/*.asm"))):
        rel = os.path.relpath(fn, ROOT)
        cond = CondTracker()
        label, method = None, None
        for line in read_lines(rel):
            code = strip_comment(line)
            if not code or cond.feed(code):
                continue
            m = re.match(r"(\w+):", code)
            if m:
                label = m.group(1)
                defined.add(label)
                continue
            if code.startswith("def_grass_wildmons"):
                method = "grass"
                continue
            if code.startswith("def_water_wildmons"):
                method = "surf"
                continue
            if code.startswith("end_"):
                method = None
                continue
            m = re.match(r"db\s+(\d+)\s*,\s*(\w+)$", code)
            if m and method:
                sp = m.group(2)
                assert sp in species, f"{rel}: unknown species {sp}"
                if label in refs:
                    out.append((sp, method, label.removesuffix("WildMons"),
                                int(m.group(1)), cond.current()))
    return out, sorted(defined - refs)


def parse_old_rod(species, aliases):
    """Old Rod species: `lb bc, LEVEL, SPECIES` inside ItemUseOldRod."""
    out = []
    lines = read_lines("engine/items/item_effects.asm")
    cond = CondTracker()
    inside = False
    for line in lines:
        code = strip_comment(line)
        if not code or cond.feed(code):
            continue
        if re.match(r"\w+:", code):
            inside = code.startswith("ItemUseOldRod:")
            continue
        if inside:
            m = re.match(r"lb\s+bc\s*,\s*(\d+)\s*,\s*(\w+)$", code)
            if m:
                sp = aliases.get(m.group(2), m.group(2))
                if sp in species:
                    out.append((sp, "old_rod", "any water", int(m.group(1)), cond.current()))
    assert out, "could not find Old Rod species in ItemUseOldRod"
    return out


def parse_good_rod(species):
    out = []
    cond = CondTracker()
    for line in read_lines("data/wild/good_rod.asm"):
        code = strip_comment(line)
        if not code or cond.feed(code):
            continue
        m = re.match(r"db\s+(\d+)\s*,\s*(\w+)$", code)
        if m and m.group(2) in species:
            out.append((m.group(2), "good_rod", "any water", int(m.group(1)), cond.current()))
    assert out, "no Good Rod species found"
    return out


def parse_super_rod(species):
    """`db MAP, SPECIES, LVL, SPECIES, LVL, ...` rows; terminated by `db -1`."""
    out = []
    cond = CondTracker()
    for line in read_lines("data/wild/super_rod.asm"):
        code = strip_comment(line)
        if not code or cond.feed(code):
            continue
        m = re.match(r"db\s+(.*)$", code)
        if not m:
            continue
        args = [a.strip() for a in m.group(1).split(",")]
        if args == ["-1"]:
            break
        mapname, rest = args[0], args[1:]
        for i in range(0, len(rest), 2):
            # Yellow's format is species, level pairs
            sp, lvl = rest[i], rest[i + 1]
            assert sp in species, f"super_rod.asm: unknown species {sp} on {mapname}"
            out.append((sp, "super_rod", mapname, int(lvl), cond.current()))
    assert out, "no Super Rod species found"
    return out


def all_wild(species, aliases):
    maps, unreferenced = parse_wild_maps(species)
    entries = maps + parse_old_rod(species, aliases) + parse_good_rod(species) + parse_super_rod(species)
    return entries, unreferenced


# ---------------------------------------------------------------------------

def main():
    dex, species, _, aliases = load_pokemon()
    entries, unreferenced = all_wild(species, aliases)

    by_species = {}
    for sp, method, loc, lvl, ver in entries:
        by_species.setdefault(sp, set()).add((method, ver))

    versions = sorted({e[4] for e in entries})
    method_counts = {}
    for sp, methods in by_species.items():
        for m, _ in methods:
            method_counts[m] = method_counts.get(m, 0) + 1

    print(f"Species in Pokédex:            {len(dex)}")
    print(f"Wild encounter rows parsed:    {len(entries)}")
    print(f"Version tags seen:             {', '.join(versions)}")
    if unreferenced:
        print(f"Unreferenced wild tables:      {', '.join(unreferenced)}")
    print("Species per method:            " +
          ", ".join(f"{m}={c}" for m, c in sorted(method_counts.items())))
    print(f"Species found in the wild:     {len(by_species)}")

    nonwild = [s for s in dex if s not in by_species]
    print(f"\nNon-wild species ({len(nonwild)}), Pokédex order:")
    for i, s in enumerate(nonwild):
        print(f"  #{dex.index(s) + 1:03d} {s}")

    # version-exclusive wild species (only meaningful if conditionals exist)
    exclusive = {s: {v for _, v in m} for s, m in by_species.items() if ALL not in {v for _, v in m}}
    if exclusive:
        print("\nVersion-exclusive wild species:")
        for s in dex:
            if s in exclusive:
                print(f"  {s}: {', '.join(sorted(exclusive[s]))}")
    else:
        print("\nNo version-exclusive wild species (no conditionals affect wild data).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
