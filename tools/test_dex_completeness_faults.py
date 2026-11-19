#!/usr/bin/env python3
"""
Fault-injection tests for tools/dex_completeness.py (no emulator, no build).

Each case corrupts a temp copy of the repo (tools/faultlib.py; the real files
are never modified) and runs the copy's dex_completeness.py against it:

  baseline        unmodified copy -> exit 0, SINGLE-SAVE 154/154
  encounters      delete one static encounter's object_event (SILVER, QUINN,
                  RAYA, MEW) -> 153/154 and that species is listed as not
                  obtainable in a single save
  stone block     delete one line of the starter-Pikachu stone block (the call,
                  the refusal branch, each predicate condition, the follower-scan
                  marker check, the OaksLab marker write) -> dex_completeness
                  exits non-zero with a VerifyError instead of reporting a count
  const_next      regression for the parser that once dropped
                  `const_next $F0 - 2` (spaced expression): the real
                  event_constants.asm and a synthetic constants file with
                  spaced / unspaced / parenthesised expressions must evaluate
                  exactly; an unsupported expression must raise, not be skipped

Usage: python3 tools/test_dex_completeness_faults.py
Exit status: 0 if every case behaves as expected, 1 otherwise.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from faultlib import RepoCopy, Results, tail  # noqa: E402

SUMMARY = re.compile(r"SINGLE-SAVE obtainable \(no link trades\):\s+(\d+)/(\d+)")

# species, map objects file (the static's object_event), level
ENCOUNTERS = [("SILVER", "data/maps/objects/RockTunnelB1F.asm", 50),
              ("QUINN", "data/maps/objects/ViridianForest.asm", 50),
              ("RAYA", "data/maps/objects/CeruleanCaveB1F.asm", 50),
              ("MEW", "data/maps/objects/CeruleanCaveB1F.asm", 30)]

IE = "engine/items/item_effects.asm"
PS = "engine/pikachu/pikachu_status.asm"
OAK = "scripts/OaksLab.asm"
# name: (file, anchor line, line to delete) -- first match at/after the anchor
STONE_BLOCK = {
    "ItemUseEvoStone: starter-check call": (IE, "ItemUseEvoStone:", "callfar IsThisPartyMonStarterPikachu"),
    "ItemUseEvoStone: refusal text":       (IE, "ItemUseEvoStone:", "ld hl, RefusingText"),
    "ItemUseEvoStone: cancel jump":        (IE, "ItemUseEvoStone:", "jr .canceledItemUse"),
    "predicate: species == STARTER":       (PS, "IsThisPartyMonStarterPikachu::", "cp STARTER"),
    "predicate: catch rate == $A3":        (PS, "IsThisPartyMonStarterPikachu::", "cp LIGHT_BALL_GSC"),
    "predicate: OT ID low byte":           (PS, "IsThisPartyMonStarterPikachu::", "ld a, [wPlayerID]"),
    "predicate: OT ID high byte":          (PS, "IsThisPartyMonStarterPikachu::", "ld a, [wPlayerID+1]"),
    "predicate: OT name compare":          (PS, "IsThisPartyMonStarterPikachu::", "ld de, wPlayerName"),
    "predicate: carry on match":           (PS, "IsThisPartyMonStarterPikachu::", "scf"),
    "follower scan: catch rate == $A3":    (PS, "IsStarterPikachuAliveInOurParty::", "cp LIGHT_BALL_GSC"),
    "OaksLab: LIGHT_BALL_GSC marker write": (OAK, "OaksLabPlayerReceivedMonText:", "ld a, LIGHT_BALL_GSC"),
}


def run(copy):
    p = copy.run_tool("dex_completeness.py")
    m = SUMMARY.search(p.stdout)
    return p, (int(m.group(1)), int(m.group(2))) if m else None


def not_obtainable(stdout):
    """Species named in the '=== Not obtainable in a single save' section."""
    part = stdout.split("=== Not obtainable", 1)
    return set(re.findall(r"\b([A-Z][A-Z_]+)\b", part[1])) if len(part) == 2 else set()


def main():
    r = Results("dex_completeness fault tests")

    print("baseline (unmodified copy)")
    with RepoCopy("dex_base") as c:
        p, cnt = run(c)
        r.check("baseline 154/154, exit 0", p.returncode == 0 and cnt == (154, 154),
                f"exit {p.returncode}, count {cnt}")

    print("encounters removed")
    for sp, rel, lvl in ENCOUNTERS:
        with RepoCopy(f"dex_{sp.lower()}") as c:
            lines = c.read(rel).split("\n")
            hits = [i for i, l in enumerate(lines)
                    if re.match(rf"\s*object_event\b.*,\s*{sp},\s*{lvl}\s*(;.*)?$", l)]
            assert len(hits) == 1, f"{rel}: expected one {sp} object_event, found {len(hits)}"
            del lines[hits[0]]
            c.write(rel, "\n".join(lines))
            p, cnt = run(c)
            listed = sp in not_obtainable(p.stdout)
            r.check(f"{sp} object_event deleted -> 153/154, {sp} unobtainable",
                    p.returncode == 0 and cnt == (153, 154) and listed,
                    f"{rel}:{hits[0] + 1}; exit {p.returncode}, count {cnt}, {sp} listed={listed}")

    print("starter-Pikachu stone block: one line deleted per case")
    for name, (rel, anchor, line) in STONE_BLOCK.items():
        with RepoCopy("dex_block") as c:
            n = c.delete_line(rel, anchor, line)
            p, cnt = run(c)
            err = (p.stderr.strip().split("\n") or [""])[-1]
            ok = p.returncode != 0 and "VerifyError" in p.stderr and cnt is None
            r.check(f"{name} ({rel}:{n} `{line}`) -> VerifyError", ok,
                    err[:150] if ok else f"exit {p.returncode}, count {cnt}\n{tail(p)}")

    print("const_next parser (shared load_consts in tools/verify_evo.py)")
    import verify_evo as ve
    ev = ve.load_consts("constants/event_constants.asm")
    got = ev.get("EVENT_POKEMON_TOWER_RIVAL_ON_LEFT")
    r.check("event_constants: `const_next $F0 - 2` -> EVENT_POKEMON_TOWER_RIVAL_ON_LEFT = $EE",
            got == 0xEE, f"got {got if got is None else hex(got)}")
    with RepoCopy("dex_consts") as c:
        rel = "constants/_fault_const_next.asm"
        c.write(rel, "\n".join([
            "\tconst_def",
            "\tconst_next $F0 - 2      ; spaced (the dropped form)",
            "\tconst C_SPACED",
            "\tconst_next $F0-2",
            "\tconst C_TIGHT",
            "\tconst_next  ( $10 + 2 ) * 2",
            "\tconst C_PAREN",
            "\tconst_next %1000 + 1",
            "\tconst C_BIN",
            "\tconst_skip 2",
            "\tconst C_SKIP",
            ""]))
        sys.path.insert(0, c.path("tools"))
        sys.modules.pop("verify_evo", None)
        import verify_evo as ve_copy  # the copy's parser, ROOT = the copy
        assert ve_copy.ROOT == c.dir
        vals = ve_copy.load_consts(rel)
        want = {"C_SPACED": 0xEE, "C_TIGHT": 0xEE, "C_PAREN": 0x24, "C_BIN": 9, "C_SKIP": 12}
        r.check("synthetic const_next / const_skip expressions", vals == want, f"got {vals}")
        c.write(rel, "\tconst_def\n\tconst_next SOME_LABEL + 1\n\tconst C_BAD\n")
        try:
            vals = ve_copy.load_consts(rel)
            r.check("unsupported expression raises (not silently dropped)", False, f"got {vals}")
        except ValueError as e:
            r.check("unsupported expression raises (not silently dropped)", True, str(e))
        sys.path.remove(c.path("tools"))
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
