#!/usr/bin/env python3
"""
Compute which of the 151 species are obtainable in Pokémon Yellow on a single
cartridge, and list every species with no obtaining path.

Sources (all parsed from the repo):
  wild        tools/find_nonwild.py (grass, surf, Old/Good/Super Rod)
  static      `ld a, SPECIES` -> `ld [wCurOpponent], a` in scripts/*.asm,
              minus tutorial battles (BATTLE_TYPE_OLD_MAN / BATTLE_TYPE_PIKACHU)
              and species the Poké Ball code refuses to catch (item_effects.asm)
  object      map-object Pokémon: `object_event ..., SPECIES, LEVEL` in data/maps/objects
  gift        `lb bc, SPECIES, LVL` + GivePokemon; Dojo (DisplayPokedex -> wCurPartySpecies);
              fossils (engine/events/cinnabar_lab.asm); starter via AddPartyMon in scripts
  prize       Game Corner prize Pokémon (data/events/prizes.asm)
  trade       in-game NPC trades (data/events/trades.asm) that a script actually uses
  evolution   level / item evolutions from data/pokemon/evos_moves.asm;
              trade evolutions are tracked separately because they need a second cartridge.

Run from the repo root:  python3 tools/dex_completeness.py
"""

import glob
import itertools
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from find_nonwild import (  # noqa: E402
    ROOT, all_wild, load_pokemon, read_lines, strip_comment,
)

STARTER_GIFTS = ("BULBASAUR", "CHARMANDER", "SQUIRTLE")
TUTORIAL_BATTLE_TYPES = ("BATTLE_TYPE_OLD_MAN", "BATTLE_TYPE_PIKACHU")


def rel(p):
    return os.path.relpath(p, ROOT)


def script_files():
    return sorted(glob.glob(os.path.join(ROOT, "scripts", "*.asm")))


def code_lines(relpath):
    return [strip_comment(l) for l in read_lines(relpath)]


def resolve(name, aliases):
    return aliases.get(name, name)


# ---------------------------------------------------------------------------
# Non-evolution sources
# ---------------------------------------------------------------------------

def uncatchable_species(aliases):
    """Species the ball code refuses: `cp SPECIES` followed by the
    'can't be caught' value ($10) being loaded into b."""
    lines = code_lines("engine/items/item_effects.asm")
    out = set()
    for i, c in enumerate(lines):
        m = re.match(r"cp\s+(\w+)$", c)
        if m and i + 1 < len(lines) and re.match(r"ld\s+b\s*,\s*\$10$", lines[i + 1]):
            out.add(resolve(m.group(1), aliases))
    return out


def parse_static(species, aliases, uncatchable):
    """Scripted battles; returns (obtainable, excluded) lists of (sp, where, why)."""
    ok, excluded = [], []
    for fn in script_files():
        lines = code_lines(rel(fn))
        for i, c in enumerate(lines):
            if not re.match(r"ld\s+\[wCurOpponent\]\s*,\s*a$", c):
                continue
            # nearest preceding `ld a, X`
            for j in range(i - 1, max(i - 6, -1), -1):
                m = re.match(r"ld\s+a\s*,\s*(\w+)$", lines[j])
                if m:
                    break
            else:
                continue
            sp = resolve(m.group(1), aliases)
            if sp not in species:
                continue  # trainer class (OPP_*)
            where = f"{rel(fn)}:{i + 1}"
            window = lines[max(0, i - 8): i + 8]
            tutorial = [t for t in TUTORIAL_BATTLE_TYPES
                        if any(re.match(rf"ld\s+a\s*,\s*{t}$", w) for w in window)]
            if tutorial:
                excluded.append((sp, where, f"{tutorial[0]} (not kept)"))
            elif sp in uncatchable:
                excluded.append((sp, where, f"{m.group(1)}: ball refuses capture"))
            else:
                ok.append((sp, where, "scripted battle"))
    return ok, excluded


def parse_map_objects(species):
    out = []
    for fn in sorted(glob.glob(os.path.join(ROOT, "data", "maps", "objects", "*.asm"))):
        for i, c in enumerate(code_lines(rel(fn))):
            m = re.match(r"object_event\s+.*,\s*(\w+)\s*,\s*(\d+)$", c)
            if m and m.group(1) in species:
                out.append((m.group(1), f"{rel(fn)}:{i + 1}", f"overworld lv{m.group(2)}"))
    return out


def parse_gifts(species, aliases):
    out = []
    for fn in script_files():
        lines = code_lines(rel(fn))
        for i, c in enumerate(lines):
            if not re.match(r"(call|jp|predef)\s+GivePokemon$", c):
                continue
            where = f"{rel(fn)}:{i + 1}"
            window = lines[max(0, i - 25): i]
            # direct: lb bc, SPECIES, LEVEL
            direct = [re.match(r"lb\s+bc\s*,\s*(\w+)\s*,\s*(\d+)$", w) for w in window]
            direct = [m for m in direct if m]
            if direct:
                sp = resolve(direct[-1].group(1), aliases)
                if sp in species:
                    tag = "starter gift" if sp in STARTER_GIFTS else "gift"
                    out.append((sp, where, f"{tag} lv{direct[-1].group(2)}"))
                    continue
            # Dojo: ld a, SPECIES / call DisplayPokedex (ShowPokedexData copies
            # wPokedexNum to wCurPartySpecies) ... ld a, [wCurPartySpecies] / ld b, a
            if any("[wCurPartySpecies]" in w for w in window):
                for j in range(len(window) - 1):
                    m = re.match(r"ld\s+a\s*,\s*(\w+)$", window[j])
                    if m and window[j + 1] == "call DisplayPokedex":
                        sp = resolve(m.group(1), aliases)
                        if sp in species:
                            out.append((sp, where, "gift (choose one)"))
                continue
            # Fossils: species chosen in cinnabar_lab.asm and stored in wFossilMon
            if any("[wFossilMon]" in w for w in window):
                for k, l in enumerate(code_lines("engine/events/cinnabar_lab.asm")):
                    m = re.match(r"ld\s+b\s*,\s*(\w+)$", l)
                    if m and resolve(m.group(1), aliases) in species:
                        out.append((resolve(m.group(1), aliases),
                                    f"engine/events/cinnabar_lab.asm:{k + 1}", "fossil revival"))
                continue
            out.append((None, where, "UNRESOLVED GivePokemon"))
    # Starter: ld a, X ... ld [wCurPartySpecies], a / call AddPartyMon in scripts
    for fn in script_files():
        lines = code_lines(rel(fn))
        for i, c in enumerate(lines):
            if c != "call AddPartyMon":
                continue
            for j in range(i - 1, max(i - 6, -1), -1):
                m = re.match(r"ld\s+a\s*,\s*(\w+)$", lines[j])
                if m:
                    sp = resolve(m.group(1), aliases)
                    if sp in species:
                        out.append((sp, f"{rel(fn)}:{i + 1}", f"starter ({m.group(1)})"))
                    break
    return out


def parse_prizes(species):
    out, inside = [], False
    for i, c in enumerate(code_lines("data/events/prizes.asm")):
        if re.match(r"PrizeMenuMon\d+Entries:", c):
            inside = True
            continue
        if re.match(r"\w+:", c):
            inside = False
            continue
        m = re.match(r"db\s+(\w+)$", c)
        if inside and m and m.group(1) in species:
            out.append((m.group(1), f"data/events/prizes.asm:{i + 1}", "Game Corner prize"))
    return out


def parse_trades(species):
    """Returns (used, unused) lists of (received, where, note)."""
    consts = []
    for c in code_lines("constants/script_constants.asm"):
        m = re.match(r"const\s+(TRADE_FOR_\w+)$", c)
        if m:
            consts.append(m.group(1))
    used_consts = {}
    for fn in script_files():
        for i, c in enumerate(code_lines(rel(fn))):
            m = re.match(r"ld\s+a\s*,\s*(TRADE_FOR_\w+)$", c)
            if m:
                used_consts.setdefault(m.group(1), f"{rel(fn)}:{i + 1}")
    used, unused = [], []
    idx = 0
    for c in code_lines("data/events/trades.asm"):
        m = re.match(r"npctrade\s+(\w+)\s*,\s*(\w+)\s*,", c)
        if not m:
            continue
        give, get = m.group(1), m.group(2)
        assert give in species and get in species
        const = consts[idx]
        idx += 1
        if const in used_consts:
            used.append((get, used_consts[const], f"trade {give}->{get}", give))
        else:
            unused.append((get, const, f"unused trade {give}->{get}", give))
    assert idx == len(consts), "trade table / TRADE_FOR_* count mismatch"
    return used, unused


# ---------------------------------------------------------------------------
# Item sources (for item evolutions)
# ---------------------------------------------------------------------------

def load_items():
    items = set()
    for c in code_lines("constants/item_constants.asm"):
        m = re.match(r"const\s+(\w+)$", c)
        if m:
            items.add(m.group(1))
    return items


def text_pointer_targets():
    """{text label or TEXT_ const -> 'scripts/X.asm:N'} from dw_const text pointers."""
    targets = {}
    for fn in script_files():
        for i, c in enumerate(code_lines(rel(fn))):
            m = re.match(r"dw_const\s+(\w+)\s*,\s*(\w+)$", c)
            if m:
                targets.setdefault(m.group(1), []).append((m.group(2), f"{rel(fn)}:{i + 1}"))
    return targets


def parse_marts(items, ptr_targets):
    """Mart inventories (data/items/marts.asm) whose clerk text is wired into a
    map's text pointers. Returns {item: [where]} for buyable items."""
    out, label = {}, None
    for i, c in enumerate(code_lines("data/items/marts.asm")):
        m = re.match(r"(\w+)::?$", c)
        if m:
            label = m.group(1)
            continue
        m = re.match(r"script_mart\s+(.*)$", c)
        if not m:
            continue
        if label not in ptr_targets:
            continue  # mart text not referenced by any map: not reachable
        where = f"{label} ({ptr_targets[label][0][1]})"
        for it in (a.strip() for a in m.group(1).split(",")):
            # TMs/HMs are defined via add_tm/add_hm, not `const`
            assert it in items or re.match(r"[TH]M_", it), f"marts.asm: unknown item {it}"
            out.setdefault(it, []).append(where)
    return out


def parse_item_balls(items, ptr_targets):
    """Overworld item balls: `object_event ..., TEXT_X, ITEM` where TEXT_X is
    mapped to PickUpItemText. Returns list of (item, where)."""
    pickup = {t for t, _ in ptr_targets.get("PickUpItemText", [])}
    out = []
    for fn in sorted(glob.glob(os.path.join(ROOT, "data", "maps", "objects", "*.asm"))):
        for i, c in enumerate(code_lines(rel(fn))):
            m = re.match(r"object_event\s+(.*)$", c)
            if not m:
                continue
            args = [a.strip() for a in m.group(1).split(",")]
            if len(args) == 7 and args[5] in pickup and args[6] in items:
                out.append((args[6], f"item ball @ {rel(fn)}:{i + 1}"))
    return out


def parse_hidden_items(items):
    """Hidden items: `hidden_event x, y, HiddenItems, ITEM` under a
    `hidden_events_for MAP` section that HiddenEventMaps references."""
    lines = code_lines("data/events/hidden_events.asm")
    referenced = {m.group(1) for c in lines
                  for m in [re.match(r"hidden_event_map\s+(\w+)$", c)] if m}
    out, cur = [], None
    for i, c in enumerate(lines):
        m = re.match(r"hidden_events_for\s+(\w+)$", c)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r"hidden_event\s+\d+\s*,\s*\d+\s*,\s*HiddenItems\s*,\s*(\w+)$", c)
        if m and cur in referenced and m.group(1) in items:
            out.append((m.group(1), f"hidden item on {cur} @ data/events/hidden_events.asm:{i + 1}"))
    return out


def parse_item_gifts(items):
    """NPC item gifts: `lb bc, ITEM, N` or `ld b, ITEM` shortly before GiveItem."""
    out = []
    for fn in script_files():
        lines = code_lines(rel(fn))
        for i, c in enumerate(lines):
            if not re.match(r"(call|jp|predef)\s+GiveItem$", c):
                continue
            for j in range(i - 1, max(i - 8, -1), -1):
                m = (re.match(r"lb\s+bc\s*,\s*(\w+)\s*,\s*\d+$", lines[j])
                     or re.match(r"ld\s+b\s*,\s*(\w+)$", lines[j]))
                if m:
                    if m.group(1) in items:
                        out.append((m.group(1), f"NPC gift @ {rel(fn)}:{i + 1}"))
                    break
    return out


def item_sources(needed):
    """For each needed item: {'buy': [...], 'found': [...]}."""
    items = load_items()
    for it in needed:
        assert it in items, f"evolution item {it} not in item_constants.asm"
    ptrs = text_pointer_targets()
    marts = parse_marts(items, ptrs)
    found = parse_item_balls(items, ptrs) + parse_hidden_items(items) + parse_item_gifts(items)
    out = {}
    for it in sorted(needed):
        out[it] = {"buy": marts.get(it, []),
                   "found": [w for i, w in found if i == it]}
    return out


# ---------------------------------------------------------------------------
# Evolutions
# ---------------------------------------------------------------------------

def parse_evos(internal_ids, species):
    """Return list of (from, to, method, detail)."""
    lines = code_lines("data/pokemon/evos_moves.asm")
    # pointer table, in internal-ID order starting at $01
    ptrs, in_table = [], False
    for c in lines:
        if c.startswith("EvosMovesPointerTable:"):
            in_table = True
            continue
        if in_table:
            m = re.match(r"dw\s+(\w+)$", c)
            if m:
                ptrs.append(m.group(1))
            elif re.match(r"\w+:", c) or c.startswith("assert"):
                break
    assert len(ptrs) == len(internal_ids) - 1, (len(ptrs), len(internal_ids))
    label_to_species = {lab: internal_ids[i + 1] for i, lab in enumerate(ptrs)
                        if internal_ids[i + 1] is not None}  # skip MissingNo

    evos, cur, reading = [], None, False
    for c in lines:
        m = re.match(r"(\w+):$", c)
        if m and m.group(1) in label_to_species:
            cur, reading = label_to_species[m.group(1)], True
            continue
        if not reading or not c:
            continue
        if c == "db 0":
            reading = False  # end of evolutions; learnset follows
            continue
        args = [a.strip() for a in re.sub(r"^db\s+", "", c).split(",")]
        method = args[0]
        if method == "EVOLVE_LEVEL":
            evos.append((cur, args[2], "level", f"lv{args[1]}"))
        elif method == "EVOLVE_ITEM":
            evos.append((cur, args[3], "item", args[1]))
        elif method == "EVOLVE_TRADE":
            evos.append((cur, args[2], "trade", "link trade"))
        else:
            raise ValueError(f"unknown evo line for {cur}: {c}")
    for a, b, _, _ in evos:
        assert a in species and b in species, (a, b)
    return evos


# ---------------------------------------------------------------------------
# One-time choices (mutual exclusivity within a single save)
# ---------------------------------------------------------------------------

class VerifyError(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise VerifyError("exclusivity assumption not verified: " + msg)


def enclosing_block(lines, idx):
    """Lines of the top-level label block (label without leading '.') containing idx."""
    start = idx
    while start > 0 and not re.match(r"[A-Za-z_]\w*::?$", lines[start]):
        start -= 1
    end = idx + 1
    while end < len(lines) and not re.match(r"[A-Za-z_]\w*::?$", lines[end]):
        end += 1
    return start, lines[start:end]


def split_where(where):
    f, n = where.rsplit(":", 1)
    return f, int(n) - 1


def fossil_item_to_species(aliases, species):
    """From cinnabar_lab.asm: `cp FOSSIL / jr z, .lab` ... `.lab` / `ld b, SPECIES`."""
    lines = code_lines("engine/events/cinnabar_lab.asm")
    out = {}
    for i in range(len(lines) - 1):
        m1 = re.match(r"cp\s+(\w+)$", lines[i])
        m2 = re.match(r"jr\s+z\s*,\s*(\.\w+)$", lines[i + 1])
        if m1 and m2:
            lab = lines.index(m2.group(1))
            m3 = re.match(r"ld\s+b\s*,\s*(\w+)$", lines[lab + 1])
            if m3 and resolve(m3.group(1), aliases) in species:
                out[m1.group(1)] = resolve(m3.group(1), aliases)
    return out


def verify_dojo(gifts):
    """Both Dojo balls are guarded by CheckEitherEventSet on the same event pair,
    and each sets its own member of that pair after GivePokemon."""
    dojo = [g for g in gifts if g[2].startswith("gift (choose one)")]
    check(len(dojo) >= 2, f"expected >=2 choose-one Dojo gifts, found {len(dojo)}")
    pairs, sets, evidence = set(), {}, []
    for sp, where, _ in dojo:
        f, i = split_where(where)
        _, block = enclosing_block(code_lines(f), i)
        guard = [re.match(r"CheckEitherEventSet\s+(\w+)\s*,\s*(\w+)$", l) for l in block]
        guard = [m for m in guard if m]
        check(guard, f"{where}: no CheckEitherEventSet guard before GivePokemon for {sp}")
        pair = frozenset(guard[0].groups())
        pairs.add(pair)
        setev = [e.strip() for l in block
                 for m in [re.match(r"SetEvents?\s+(.*)$", l)] if m
                 for e in m.group(1).split(",")]
        mine = [e for e in setev if e in pair]
        check(len(mine) == 1, f"{where}: {sp} block doesn't set exactly one guarded event ({setev})")
        sets[sp] = mine[0]
        evidence.append(f"{sp}: guarded by CheckEitherEventSet {', '.join(sorted(pair))}, "
                        f"sets {mine[0]} @ {where}")
    check(len(pairs) == 1, f"Dojo gifts guard on different event pairs: {pairs}")
    check(len(set(sets.values())) == len(sets), "Dojo gifts set the same event")
    options = [(sp, {("gift", sp)}) for sp, _, _ in dojo]
    return "Fighting Dojo gift", options, evidence


def verify_fossils(gifts, aliases, species):
    """Each Mt. Moon fossil is a one-off GiveItem whose block hides its own ball and
    switches to a map script that ends by hiding the *other* fossil's ball."""
    f2s = fossil_item_to_species(aliases, species)
    fossil_items = [it for it in f2s if it.endswith("_FOSSIL")]
    check(len(fossil_items) == 2, f"expected 2 choosable fossils in cinnabar_lab.asm, got {f2s}")
    items = load_items()
    ptrs = text_pointer_targets()
    all_found = (parse_item_balls(items, ptrs) + parse_hidden_items(items) + parse_item_gifts(items))
    marts = parse_marts(items, ptrs)
    evidence, toggles, files = [], {}, set()
    for it in fossil_items:
        srcs = [w for i, w in all_found if i == it]
        check(len(srcs) == 1 and not marts.get(it),
              f"{it} must have exactly one source and not be sold; found {srcs}, marts {marts.get(it)}")
        f, i = split_where(srcs[0].split("@ ", 1)[1])
        files.add(f)
        lines = code_lines(f)
        _, block = enclosing_block(lines, i)
        tog = [m.group(1) for l in block for m in [re.match(r"ld\s+a\s*,\s*(TOGGLE_\w+)$", l)] if m]
        nxt = [m.group(1) for l in block for m in [re.match(r"ld\s+a\s*,\s*(SCRIPT_\w+)$", l)] if m]
        check(len(tog) == 1 and "predef HideObject" in block, f"{it}: block doesn't hide its own ball")
        check(len(nxt) == 1, f"{it}: block doesn't switch map script")
        toggles[it] = (tog[0], nxt[0])
        evidence.append(f"{it} -> {f2s[it]}: GiveItem hides {tog[0]}, then runs {nxt[0]} @ {srcs[0]}")
    check(len(files) == 1, f"fossils given in different files: {files}")
    f = files.pop()
    lines = code_lines(f)
    script_label = {}
    for l in lines:
        m = re.match(r"dw_const\s+(\w+)\s*,\s*(SCRIPT_\w+)$", l)
        if m:
            script_label[m.group(2)] = m.group(1)

    def script_body(const):
        check(const in script_label, f"{const} not in {f} script pointers")
        lab = script_label[const] + ":"
        check(lab in lines, f"label {lab} not found in {f}")
        return enclosing_block(lines, lines.index(lab))[1]

    # follow the script chain (MOVE_SUPER_NERD -> TAKES_OTHER_FOSSIL) until a block hides toggles
    nxt = {s for _, s in toggles.values()}
    check(len(nxt) == 1, f"fossil picks start different scripts: {nxt}")
    const, seen, hider = nxt.pop(), set(), None
    while const and const not in seen:
        seen.add(const)
        body = script_body(const)
        hidden = {m.group(1) for l in body for m in [re.match(r"ld\s+a\s*,\s*(TOGGLE_\w+)$", l)] if m}
        if hidden and "predef HideObject" in body:
            hider = (const, hidden)
            break
        chained = [m.group(1) for l in body for m in [re.match(r"ld\s+a\s*,\s*(SCRIPT_\w+)$", l)] if m]
        const = chained[-1] if chained else None
    own = {t for t, _ in toggles.values()}
    check(hider and own <= hider[1],
          f"no follow-up script hides both fossil balls (chain {sorted(seen)}, found {hider})")
    evidence.append(f"follow-up {hider[0]} hides the other ball ({', '.join(sorted(hider[1]))})")
    options = [(f2s[it], {("gift", f2s[it])}) for it in fossil_items]
    return "Mt. Moon fossil", options, evidence


def find_seq(lines, start, end, patterns):
    """Find each regex in `patterns` in order within lines[start:end] (gaps allowed).
    Returns the matching line indexes, or None if the sequence is not present."""
    out, i = [], start
    for pat in patterns:
        while i < end and not re.match(pat, lines[i]):
            i += 1
        if i >= end:
            return None
        out.append(i)
        i += 1
    return out


def verify_starter_stone_block(aliases, species, trades_used, evos):
    """Yellow refuses stone evolution for the player's own starter Pikachu.

    Verifies, from the code:
      - ItemUseEvoStone calls IsThisPartyMonStarterPikachu after
        CheckEvolveWithUsedStone; on carry it prints RefusingText and jumps to
        .canceledItemUse, and only the not-starter branch reaches the evolution.
      - The predicate returns carry only if species == STARTER AND OT ID ==
        wPlayerID (both bytes) AND OT name == wPlayerName.
    On a single cartridge every obtainable mon of the STARTER species carries the
    player's OT unless an in-game NPC trade hands one out, so if no trade does,
    every item evolution out of STARTER is blocked for a single save.

    Returns ({(from, to): reason}, evidence). Raises VerifyError if the block is gone.
    """
    starter = resolve("STARTER", aliases)
    check(starter in species, f"STARTER alias resolves to {starter!r}, not a species")

    # --- ItemUseEvoStone ---
    ie = code_lines("engine/items/item_effects.asm")
    check("ItemUseEvoStone:" in ie, "ItemUseEvoStone label not found")
    s0, block = enclosing_block(ie, ie.index("ItemUseEvoStone:"))
    s1 = s0 + len(block)
    seq = find_seq(ie, s0, s1, [
        r"call\s+CheckEvolveWithUsedStone$",
        r"callfar\s+IsThisPartyMonStarterPikachu$",
        r"jr\s+nc\s*,\s*(\.\w+)$",
    ])
    check(seq, "ItemUseEvoStone no longer calls IsThisPartyMonStarterPikachu "
               "after CheckEvolveWithUsedStone (starter-Pikachu stone block removed?)")
    skip_label = re.match(r"jr\s+nc\s*,\s*(\.\w+)$", ie[seq[2]]).group(1)
    check(skip_label in ie[seq[2]:s1], f"branch target {skip_label} not in ItemUseEvoStone")
    lab = ie.index(skip_label, seq[2])
    refuse = ie[seq[2] + 1:lab]
    check(any(re.match(r"ld\s+hl\s*,\s*RefusingText$", l) for l in refuse),
          "starter branch of ItemUseEvoStone no longer prints RefusingText")
    check(any(re.match(r"j[rp]\s+\.canceledItemUse$", l) for l in refuse),
          "starter branch of ItemUseEvoStone no longer jumps to .canceledItemUse")
    check(not any("Evol" in l for l in refuse),
          "starter branch of ItemUseEvoStone calls an evolution routine")
    evo_after = [n for n in range(lab, s1) if re.match(r"(callfar|call|predef|farcall)\s+\w*Evol", ie[n])]
    check(evo_after, f"no evolution call after {skip_label}; can't confirm the block is what gates it")
    evidence = [
        f"item_effects.asm:{seq[1] + 1}: ItemUseEvoStone calls IsThisPartyMonStarterPikachu "
        f"after CheckEvolveWithUsedStone (:{seq[0] + 1})",
        f"item_effects.asm:{seq[2] + 2}-{lab}: on carry -> RefusingText, jr .canceledItemUse; "
        f"evolution only via {skip_label} (:{evo_after[0] + 1} {ie[evo_after[0]]})",
    ]

    # --- predicate ---
    ps_path = "engine/pikachu/pikachu_status.asm"
    ps = code_lines(ps_path)
    check("IsThisPartyMonStarterPikachu::" in ps, "IsThisPartyMonStarterPikachu not found")
    p0 = ps.index("IsThisPartyMonStarterPikachu::")
    p1 = p0 + 1
    while p1 < len(ps) and not (re.match(r"[A-Za-z_]\w*::?$", ps[p1])
                                and ps[p1] != "IsThisMonStarterPikachu:"):
        p1 += 1
    fail = r"jr\s+nz\s*,\s*\.notPlayerPikachu$"
    cond_species = find_seq(ps, p0, p1, [r"cp\s+STARTER$", fail])
    cond_id = find_seq(ps, p0, p1, [
        r"ld\s+bc\s*,\s*wPartyMon1OTID\s*-\s*wPartyMon1$",
        r"ld\s+a\s*,\s*\[wPlayerID\]$", r"cp\s+\[hl\]$", fail,
        r"ld\s+a\s*,\s*\[wPlayerID\+1\]$", r"cp\s+\[hl\]$", fail])
    cond_name = find_seq(ps, p0, p1, [
        r"ld\s+de\s*,\s*wPartyMonOT$", r"ld\s+de\s*,\s*wPlayerName$",
        r"cp\s+\[hl\]$", r"jr\s+z\s*,\s*\.loop$"])
    result = find_seq(ps, p0, p1, [r"\.notPlayerPikachu$", r"and\s+a$", r"ret$",
                                   r"\.isPlayerPikachu$", r"scf$", r"ret$"])
    check(cond_species, "predicate no longer requires species == STARTER")
    check(cond_id, "predicate no longer compares both bytes of OT ID against wPlayerID")
    check(cond_name, "predicate no longer compares OT name (wPartyMonOT) against wPlayerName")
    check(result, "predicate no longer returns carry only on .isPlayerPikachu")
    check(cond_species[0] < cond_id[1] < cond_name[1],
          "predicate conditions are not checked in sequence (species, OT ID, OT name)")
    evidence.append(
        f"pikachu_status.asm:{p0 + 1}: carry iff species == STARTER ({starter}) (:{cond_species[0] + 1}) "
        f"AND OT ID == wPlayerID (:{cond_id[1] + 1},{cond_id[4] + 1}) "
        f"AND OT name == wPlayerName (:{cond_name[1] + 1}); any mismatch -> .notPlayerPikachu")

    # --- single-cartridge OT argument ---
    foreign = [(get, where) for get, where, _, _ in trades_used if get == starter]
    blocked = {}
    if foreign:
        evidence.append(f"NOT blocking: in-game trade gives a foreign-OT {starter}: {foreign}")
    else:
        evidence.append(f"no in-game NPC trade gives {starter}, so every single-cart {starter} "
                        f"has the player's OT and fails the stone check")
        for a, b, m, d in evos:
            if a == starter and m == "item":
                blocked[(a, b)] = (f"stone evolution refused for own-OT {starter}; "
                                   f"needs a traded-in {starter} (foreign OT)")
    return blocked, evidence


def verify_eevee(gifts, paths, evos):
    """Exactly one Eevee for the player: one GivePokemon that hides its ball, and
    no other source. Each Eevee evolution consumes it."""
    eg = [g for g in gifts if g[0] == "EEVEE"]
    check(len(eg) == 1, f"expected exactly one player Eevee gift, found {eg}")
    f, i = split_where(eg[0][1])
    lines = code_lines(f)
    _, block = enclosing_block(lines, i)
    check(any(re.match(r"ld\s+a\s*,\s*TOGGLE_\w+$", l) for l in block) and "predef HideObject" in block,
          f"{eg[0][1]}: Eevee gift does not hide its Poké Ball after being taken")
    other = [p for p in paths["EEVEE"] if p[0] != "gift"]
    # Oak's Lab Eevee: referenced for the rival only, never given to the player
    oak = code_lines("scripts/OaksLab.asm")
    oak_eevee = [n for n, l in enumerate(oak) if re.match(r"ld\s+a\s*,\s*EEVEE$", l)]
    check(oak_eevee, "expected an EEVEE reference in OaksLab.asm (rival's Eevee)")
    for n in oak_eevee:
        _, blk = enclosing_block(oak, n)
        check(any("wRivalStarter" in l for l in blk) or not any(
            "GivePokemon" in l or "AddPartyMon" in l for l in blk),
              f"OaksLab.asm:{n + 1}: Eevee reference may give it to the player")
    branches = [(a, b) for a, b, m, _ in evos if a == "EEVEE"]
    check(len(branches) >= 2, f"Eevee has {len(branches)} evolutions")
    evidence = [f"Eevee gift: {eg[0][2]} @ {eg[0][1]} (ball hidden after, one-time)",
                f"OaksLab.asm EEVEE refs at line(s) {', '.join(str(n + 1) for n in oak_eevee)} "
                f"are the rival's (wRivalStarter), not given to player",
                f"evolutions: {', '.join(b for _, b in branches)}"]
    if other:
        # Repeatable Eevee source: as many Eevee as wanted, so the choice doesn't bind.
        # (Its options disable evolution edges, which would be wrong to apply now.)
        evidence.append("NOT BINDING: Eevee is also obtainable from "
                        + "; ".join(f"{s}: {d}" for s, d in other))
        return "Eevee evolution", [], evidence
    for _, b in branches:
        check(not paths[b], f"{b} has a non-evolution source {paths[b]}; exclusivity wouldn't bind")
    options = [(b, {("evo", a, b)}) for a, b in branches]
    return "Eevee evolution", options, evidence


# ---------------------------------------------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Pokédex completeness for pokeyellow")
    ap.add_argument("--explain", action="append", metavar="SPECIES",
                    help="print every obtaining path found for SPECIES (repeatable)")
    args = ap.parse_args()

    dex, species, internal_ids, aliases = load_pokemon()

    paths = {s: [] for s in dex}  # species -> list of (source, detail)

    wild, _ = all_wild(species, aliases)
    for sp, method, loc, lvl, ver in wild:
        paths[sp].append(("wild", f"{method} {loc} lv{lvl}" + ("" if ver == "all" else f" [{ver}]")))

    uncatchable = uncatchable_species(aliases)
    static_ok, static_excluded = parse_static(species, aliases, uncatchable)
    objects = parse_map_objects(species)
    gifts = parse_gifts(species, aliases)
    prizes = parse_prizes(species)
    trades_used, trades_unused = parse_trades(species)
    evos = parse_evos(internal_ids, species)

    unresolved = [g for g in gifts if g[0] is None]
    assert not unresolved, f"unresolved GivePokemon calls: {unresolved}"
    gifted = {g[0] for g in gifts}
    missing_starters = [s for s in STARTER_GIFTS if s not in gifted]
    assert not missing_starters, f"starter gift events not found: {missing_starters}"

    for src, rows in (("static", static_ok), ("static", objects), ("gift", gifts), ("prize", prizes)):
        for sp, where, note in rows:
            paths[sp].append((src, f"{note} @ {where}"))

    # Evolution items: an item evolution only counts if the item is buyable
    # or can be found / is given somewhere.
    evo_items = {d for _, _, m, d in evos if m == "item"}
    isrc = item_sources(evo_items)
    item_ok = {it for it, s in isrc.items() if s["buy"] or s["found"]}

    # Evolutions the game itself refuses on a single cartridge (starter Pikachu + stone).
    blocked_evos, starter_block_evidence = verify_starter_stone_block(aliases, species, trades_used, evos)

    def compute(disabled=frozenset()):
        """Reachability fixpoint. `disabled` holds source keys switched off by a
        one-time choice: ("gift", SPECIES) or ("evo", FROM, TO).
        Trades only count if the species you hand over is itself obtainable."""
        obt = {s for s, p in paths.items()
               if any(not (src == "gift" and ("gift", s) in disabled) for src, _ in p)}
        changed = True
        while changed:
            changed = False
            for get, where, note, give in trades_used:
                if give in obt and get not in obt:
                    obt.add(get)
                    changed = True
            for a, b, method, detail in evos:
                if method == "trade" or ("evo", a, b) in disabled or (a, b) in blocked_evos:
                    continue
                if method == "item" and detail not in item_ok:
                    continue
                if a in obt and b not in obt:
                    obt.add(b)
                    changed = True
        return obt

    # Every source enabled = obtainable across any number of playthroughs.
    obtainable = compute()
    trade_evo_only = set()

    # ---- one-time choices ----
    groups = [verify_eevee(gifts, paths, evos),
              verify_fossils(gifts, aliases, species),
              verify_dojo(gifts)]
    for name, options, evidence in groups:
        for opt, keys in options:
            for k in keys:
                if k[0] == "gift":
                    gp = [p for p in paths[k[1]] if p[0] == "gift"]
                    check(len(gp) == 1, f"{name}: {k[1]} must have exactly one gift source, has {gp}")
                    # Other sources (e.g. wild) are fine: compute() only switches off the
                    # gift, so the species stays obtainable and the choice stops binding for it.
                    alt = [f"{s}: {d}" for s, d in paths[k[1]] if s != "gift"]
                    if alt:
                        evidence.append(f"NOT BINDING for {k[1]}: also from {'; '.join(alt)}")
    all_groups = list(groups)
    groups = [g for g in groups if g[1]]  # groups with no options don't constrain a save
    best, combo_sizes = None, set()
    for combo in itertools.product(*(opts for _, opts, _ in groups)):
        chosen = {opt for opt, _ in combo}
        disabled = frozenset(k for (_, opts, _), pick in zip(groups, combo)
                             for opt, keys in opts if opt != pick[0] for k in keys)
        obt = compute(disabled)
        combo_sizes.add(len(obt))
        if best is None or len(obt) > len(best[0]):
            best = (obt, chosen, disabled)
    single_save, chosen, _ = best
    excluded = {}  # species -> (group, chosen option)
    for name, options, _ in groups:
        pick = next(o for o, _ in options if o in chosen)
        for s in obtainable - single_save:
            # attribute to the group whose option-disable removes it
            for opt, keys in options:
                if opt != pick and s not in compute(frozenset(keys)):
                    excluded.setdefault(s, (name, pick))
    check(set(excluded) == obtainable - single_save,
          f"unattributed exclusions: {(obtainable - single_save) - set(excluded)}")

    for get, where, note, give in trades_used:
        if give in obtainable:
            paths[get].append(("trade", f"{note} @ {where}"))
    blocked_item_evos = []
    for a, b, method, detail in evos:
        if method == "item" and detail not in item_ok:
            blocked_item_evos.append((a, b, detail))
            continue
        if (a, b) in blocked_evos:
            continue  # reported under "needs a link partner", not as an obtaining path
        if a in obtainable and method != "trade":
            paths[b].append(("evolution", f"{a} ({method} {detail})"))

    # With a link partner: trade evolutions, and evolutions the game refuses for
    # own-OT mons (a traded-in, foreign-OT pre-evolution can take them).
    link_reason = {}
    for a, b, method, detail in evos:
        if b in obtainable:
            continue
        if method == "trade" and a in obtainable:
            link_reason.setdefault(b, f"trade evolution of {a}")
        elif (a, b) in blocked_evos and a in obtainable:
            link_reason.setdefault(b, f"{a} + {detail}: {blocked_evos[(a, b)]}")
    trade_evo_only.update(link_reason)

    # ---------------- report ----------------
    print("=== Sources parsed ===")
    print(f"  wild rows:            {len(wild)}")
    print(f"  scripted battles:     {len(static_ok)} kept, {len(static_excluded)} excluded")
    for sp, where, why in static_excluded:
        print(f"      excluded {sp:<10} {where}  ({why})")
    print(f"  overworld Pokémon:    {len(objects)}")
    print(f"  gifts:                {len(gifts)}")
    print(f"  prize Pokémon:        {len(prizes)}")
    print(f"  in-game trades:       {len(trades_used)} used, {len(trades_unused)} unused in table")
    for get, const, note, give in trades_unused:
        print(f"      unused {const:<18} {note}")
    print(f"  evolution edges:      {len(evos)} "
          f"({sum(e[2] == 'trade' for e in evos)} require link trade)")

    print("\n=== Evolution items ===")
    for it, s in isrc.items():
        users = sorted({b for a, b, m, d in evos if m == "item" and d == it})
        status = ("BUYABLE" if s["buy"] else
                  f"FOUND ONLY ({len(s['found'])} in game)" if s["found"] else "UNOBTAINABLE")
        print(f"  {it:<14} {status:<22} -> {', '.join(users)}")
        for w in s["buy"]:
            print(f"      sold at {w}")
        for w in s["found"]:
            print(f"      {w}")
    for a, b, it in blocked_item_evos:
        print(f"  BLOCKED: {a} -> {b} needs {it}, which cannot be obtained")

    # Finite supply: for found-only items, how many evolutions depend solely on them?
    for it, s in isrc.items():
        if s["buy"] or not s["found"]:
            continue
        sole = []
        for b in sorted({b for a, b, m, d in evos if m == "item" and d == it}):
            others = [p for p in paths[b] if not (p[0] == "evolution" and it in p[1])]
            if b in obtainable and not others:
                sole.append(b)
        verdict = "enough" if len(sole) <= len(s["found"]) else "NOT ENOUGH"
        print(f"  {it} supply: {len(s['found'])} available, {len(sole)} species need it as "
              f"their only path ({', '.join(sole)}) -> {verdict}")

    print("\n=== Non-wild species and how they're obtained ===")
    wild_species = {w[0] for w in wild}
    for s in dex:
        if s in wild_species or s not in obtainable:
            continue
        srcs = sorted({(src, det) for src, det in paths[s] if src != "wild"})
        print(f"  #{dex.index(s) + 1:03d} {s:<11} " + "; ".join(f"{src}: {det}" for src, det in srcs))

    print("\n=== One-time choices (verified in scripts) ===")
    for name, options, evidence in all_groups:
        hit = sorted(s for s, (g, _) in excluded.items() if g == name)
        state = (f"binding, excludes {', '.join(hit)}" if hit else "NOT binding on this build")
        print(f"  {name}: one of {', '.join(o for o, _ in options) or '(repeatable source)'} -> {state}")
        for e in evidence:
            print(f"      {e}")

    print("\n=== Starter Pikachu stone block (verified in code) ===")
    for e in starter_block_evidence:
        print(f"      {e}")
    for (a, b), why in blocked_evos.items():
        print(f"  BLOCKED on a single save: {a} -> {b} ({why})")

    def num(s):
        return f"#{dex.index(s) + 1:03d} {s}"

    print("\n=== Summary ===")
    print(f"  Obtainable across playthroughs (no link trades): {len(obtainable)}/151")
    print(f"  SINGLE-SAVE obtainable (no link trades):         {len(single_save)}/151")
    print(f"      choices taken for the count: {', '.join(sorted(chosen))} "
          + ("(every combination gives the same total)" if len(combo_sizes) == 1 else
             f"(best of {len(combo_sizes)} distinct totals: {sorted(combo_sizes)})"))
    if trade_evo_only:
        print(f"  +{len(trade_evo_only)} more with a link partner "
              f"(trade evolutions / foreign-OT stone evolution)")

    unobtainable = [s for s in dex if s not in single_save]
    no_path = [s for s in unobtainable if s not in trade_evo_only and s not in excluded]
    trade_only = [s for s in unobtainable if s in trade_evo_only]
    excl = [s for s in unobtainable if s in excluded]
    assert len(no_path) + len(trade_only) + len(excl) == len(unobtainable) == 151 - len(single_save)

    print(f"\n=== Not obtainable in a single save ({len(unobtainable)}) ===")
    print(f"\n  (a) No path at all ({len(no_path)})")
    for s in no_path:
        un = [f"{const} ({n})" for g, const, n, _ in trades_unused if g == s]
        print(f"      {num(s)}" + (f"   <- only in {', '.join(un)}" if un else ""))
    print(f"\n  (b) Needs a link partner ({len(trade_only)})")
    for s in trade_only:
        print(f"      {num(s)}   <- {link_reason[s]}")
    print(f"\n  (c) Excluded by one-time choice ({len(excl)})")
    for s in excl:
        g, pick = excluded[s]
        print(f"      {num(s)}   <- {g} (counted: {pick})")

    # --explain: every path the script found, following evolutions back to a source
    def explain(s, depth=0, seen=()):
        pad = "    " * (depth + 1)
        if s in seen:
            print(f"{pad}{s}: (cycle)")
            return
        status = ("single-save" if s in single_save else
                  "excluded by choice" if s in excluded else
                  "needs link partner" if s in trade_evo_only else "UNOBTAINABLE")
        print(f"{pad}{s} [{status}]")
        if not paths[s]:
            print(f"{pad}  (no single-save paths found)")
        for (a, b), why in blocked_evos.items():
            if b == s:
                print(f"{pad}  BLOCKED evolution: {a} -> {b}: {why}")
        if s in link_reason:
            print(f"{pad}  with a link partner: {link_reason[s]}")
        for src, det in paths[s]:
            print(f"{pad}  {src}: {det}")
            if src == "evolution":
                pre = det.split(" ", 1)[0]
                m = re.search(r"\(item (\w+)\)", det)
                if m:
                    it = isrc[m.group(1)]
                    where = (["sold at " + w for w in it["buy"]] + it["found"]) or ["NOT OBTAINABLE"]
                    for w in where:
                        print(f"{pad}    {m.group(1)}: {w}")
                explain(pre, depth + 1, seen + (s,))

    for name in args.explain or []:
        s = name.upper()
        if s not in paths:
            print(f"\n--explain {name}: unknown species")
            continue
        print(f"\n=== Obtaining paths for {num(s)} ===")
        explain(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
