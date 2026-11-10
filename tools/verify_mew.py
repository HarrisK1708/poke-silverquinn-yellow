#!/usr/bin/env python3
"""
Headless proof that the step-3 Mew static encounter works in the built
pokeyellow.gbc: talking to the Mew object in Cerulean Cave B1F starts a WILD,
catchable battle against MEW Lv30, it can be caught into the party/Pokédex, and
the one-time EVENT_BEAT_MEW flag suppresses a re-encounter.

Placement (no save state needed; reuses tools/verify_evo.py's harness):
  1. Boot a temp copy of the ROM (repo .ram/.state never touched), play the
     intro to the bedroom (REDS_HOUSE_2F).
  2. Inject a party mon (so a battle has someone to send out) and a MASTER_BALL.
  3. Redirect the bedroom stairs' wWarpEntries row to CERULEAN_CAVE_B1F and walk
     onto the stairs, so the game performs a *real* warp into B1F.
  4. A PyBoy hook on LoadDestinationWarpPosition (right after its CopyData, which
     the opcode check below confirms is `pop af`) rewrites the 4 bytes it just
     copied (wCurrentTileBlockMapViewPointer, wYCoord, wXCoord) with the values
     the game's own `warp_to`/event_displacement macro would produce for the
     tile directly below Mew. The game then derives the block coords and loads
     the map, objects and sprites normally.
  5. Press UP (blocked by the Mew sprite, so the player only turns to face it).

Checks (all must pass; exit non-zero otherwise):
  (1) battle fires         talking to Mew sets wIsInBattle
  (2) MEW Lv30             wEnemyMonSpecies == MEW, wEnemyMonLevel == 30
  (3) wild/catchable       wIsInBattle == 1 (wild, not 2 = trainer), normal
                           wBattleType, "Wild MEW appeared!" on screen, and a
                           Master Ball catch puts MEW Lv30 in the party, sets its
                           Pokédex owned bit, sets EVENT_BEAT_MEW and hides the
                           object (TOGGLE_MEW)
  (4) flag suppresses      negative control: from the same pre-talk snapshot
                           with EVENT_BEAT_MEW pre-set, talking does NOT start a
                           battle

Requires: pip install pyboy pillow
Usage:    python3 tools/verify_mew.py [--rom pokeyellow.gbc] [--screenshot-dir DIR]
"""

import argparse
import io
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_evo as ve  # noqa: E402

ROOT = ve.ROOT
MEW_XY = (20, 1)          # object_event in data/maps/objects/CeruleanCaveB1F.asm (checked below)
STAND_XY = (20, 2)        # tile directly below Mew; player faces UP


def mew_object():
    for c in ve.lines("data/maps/objects/CeruleanCaveB1F.asm"):
        m = re.match(r"object_event\s+(\d+),\s*(\d+),\s*(\w+),.*,\s*TEXT_CERULEANCAVEB1F_MEW,\s*(\w+),\s*(\d+)$", c)
        if m:
            return (int(m.group(1)), int(m.group(2))), m.group(3), m.group(4), int(m.group(5))
    raise ValueError("Mew object_event not found")


def flag(m, base, bit):
    return bool(m[base + bit // 8] & (1 << (bit % 8)))


def set_flag(m, base, bit, on):
    a = base + bit // 8
    m[a] = (m[a] | (1 << (bit % 8))) if on else (m[a] & ~(1 << (bit % 8)) & 0xFF)


def walk_to(g, x, y, limit=40):
    """Step toward (x, y); if a step on one axis is blocked (furniture), use the other axis."""
    s, m = g.s, g.m
    for _ in range(limit):
        cx, cy = m[s["wXCoord"]], m[s["wYCoord"]]
        if (cx, cy) == (x, y):
            return
        horiz = ["right"] if cx < x else ["left"] if cx > x else []
        vert = ["down"] if cy < y else ["up"] if cy > y else []
        for d in horiz + vert:
            g.p.button(d, 8)
            g.tick(24)
            if (m[s["wXCoord"]], m[s["wYCoord"]]) != (cx, cy):
                break
        else:
            raise RuntimeError(f"stuck at ({cx},{cy}) walking to ({x},{y})")
    raise RuntimeError(f"could not walk to ({x},{y}); at ({m[s['wXCoord']]},{m[s['wYCoord']]})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", default=os.path.join(ROOT, "pokeyellow.gbc"))
    ap.add_argument("--sym", default=None, help="defaults to <rom>.sym")
    ap.add_argument("--screenshot-dir", default=os.getcwd())
    args = ap.parse_args()
    try:
        from pyboy import PyBoy
    except ImportError:
        sys.exit("PyBoy not installed: pip install pyboy pillow")

    sym_path = args.sym or os.path.splitext(args.rom)[0] + ".sym"
    sym = ve.load_sym(sym_path)
    need = ["wCurMap", "wYCoord", "wXCoord", "wCurrentTileBlockMapViewPointer", "wNumberOfWarps",
            "wWarpEntries", "wOverworldMap", "LoadDestinationWarpPosition", "CopyData",
            "wIsInBattle", "wBattleType", "wEnemyMonSpecies", "wEnemyMonLevel", "wEventFlags",
            "wPokedexOwned", "wToggleableObjectFlags", "wPartyCount", "wPartySpecies", "wPartyMon2Level"]
    missing = [n for n in need if n not in sym]
    assert not missing, f"symbols missing from {sym_path}: {missing}"

    consts = {"species": ve.load_consts("constants/pokemon_constants.asm"),
              "items": ve.load_consts("constants/item_constants.asm")}
    ve.TYPE_NAMES.update(ve.load_consts("constants/type_constants.asm"))
    ve.MOVES.update(ve.load_consts("constants/move_constants.asm"))
    ve.GROWTH.update({k: v for k, v in ve.load_consts("constants/pokemon_data_constants.asm").items()
                      if k.startswith("GROWTH_")})
    MEW = consts["species"]["MEW"]
    DEX_MEW = ve.load_consts("constants/pokedex_constants.asm")["DEX_MEW"]
    EVENT_BEAT_MEW = ve.load_consts("constants/event_constants.asm")["EVENT_BEAT_MEW"]
    TOGGLE_MEW = ve.load_consts("constants/toggle_constants.asm")["TOGGLE_MEW"]
    b1f_id = ve.load_consts("constants/map_constants.asm")["CERULEAN_CAVE_B1F"]
    b1f_w = ve.map_width("CERULEAN_CAVE_B1F")
    (ox, oy), sprite, obj_species, obj_level = mew_object()
    assert (ox, oy) == MEW_XY and obj_species == "MEW", ((ox, oy), obj_species)
    sx, sy = STAND_XY

    view_ptr = sym["wOverworldMap"] + 7 + b1f_w + (b1f_w + 6) * (sy >> 1) + (sx >> 1)  # event_displacement

    print("RAM addresses (from %s):" % os.path.relpath(sym_path, ROOT))
    for n in ("wCurMap", "wYCoord", "wXCoord", "wCurrentTileBlockMapViewPointer", "wWarpEntries",
              "wIsInBattle", "wBattleType", "wEnemyMonSpecies", "wEnemyMonLevel", "wEventFlags",
              "wPokedexOwned", "wToggleableObjectFlags", "wPartySpecies"):
        print(f"  {n:<32} ${sym[n]:04X}")
    print(f"  LoadDestinationWarpPosition      $00:{sym['LoadDestinationWarpPosition']:04X}")
    print(f"Ids: MEW=${MEW:02X} (dex {DEX_MEW})  EVENT_BEAT_MEW=${EVENT_BEAT_MEW:03X} "
          f"(byte ${sym['wEventFlags'] + EVENT_BEAT_MEW // 8:04X} bit {EVENT_BEAT_MEW % 8})  "
          f"TOGGLE_MEW=${TOGGLE_MEW:02X}  CERULEAN_CAVE_B1F=${b1f_id:02X} (width {b1f_w})")
    print(f"Mew object: {sprite} at {MEW_XY}, {obj_species} lv{obj_level}; player placed at {STAND_XY} facing UP")

    os.makedirs(args.screenshot_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="verify_mew_")
    rom = os.path.join(tmp, "rom.gbc")
    shutil.copy(args.rom, rom)
    rom_bytes = open(rom, "rb").read()
    # hook right after LoadDestinationWarpPosition's CopyData: find `call CopyData` (CD lo hi)
    # inside the routine; the next instruction must be `pop af` (F1)
    entry = sym["LoadDestinationWarpPosition"]
    cd = sym["CopyData"]
    call = bytes([0xCD, cd & 0xFF, cd >> 8])
    at = rom_bytes.find(call, entry, entry + 0x40)
    assert at > 0 and rom_bytes[at + 3] == 0xF1, "could not locate `call CopyData / pop af` in LoadDestinationWarpPosition"
    hook_addr = at + 3

    pyboy = PyBoy(rom, window="null", sound_emulated=False)
    g = ve.Game(pyboy, sym)
    m = g.m
    state = {"armed": False, "fired": 0}

    def on_warp_position(_):
        if state["armed"] and m[sym["wCurMap"]] == b1f_id:
            m[sym["wCurrentTileBlockMapViewPointer"]] = view_ptr & 0xFF
            m[sym["wCurrentTileBlockMapViewPointer"] + 1] = view_ptr >> 8
            m[sym["wYCoord"]] = sy
            m[sym["wXCoord"]] = sx
            state["fired"] += 1

    pyboy.hook_register(0, hook_addr, on_warp_position, None)
    results = {}
    try:
        ve.reach_overworld(g)
        pyboy.button("b", 4)  # close the start menu (no evolution in play here)
        g.tick(60)
        ve.inject(g, consts, "KADABRA", level=50, catch_rate=0, bag_item="MASTER_BALL", exp_mode="at_level")
        n = m[sym["wNumberOfWarps"]]
        assert n >= 1, "bedroom has no warp"
        wy, wx = m[sym["wWarpEntries"]], m[sym["wWarpEntries"] + 1]
        m[sym["wWarpEntries"] + 2] = 0           # destination warp index (B1F has one warp)
        m[sym["wWarpEntries"] + 3] = b1f_id      # destination map
        state["armed"] = True
        walk_to(g, wx, wy + 1)
        pyboy.button("up", 8)                    # step onto the stairs -> real warp
        for _ in range(60):
            g.tick(10)
            if m[sym["wCurMap"]] == b1f_id and state["fired"]:
                break
        g.tick(120)
        assert m[sym["wCurMap"]] == b1f_id, f"warp failed: map ${m[sym['wCurMap']]:02X}"
        assert state["fired"] == 1, f"position hook fired {state['fired']} times"
        assert (m[sym["wXCoord"]], m[sym["wYCoord"]]) == STAND_XY, \
            f"player at ({m[sym['wXCoord']]},{m[sym['wYCoord']]}), expected {STAND_XY}"
        pyboy.button("up", 8)                    # turn to face Mew (sprite blocks the step)
        g.tick(40)
        assert (m[sym["wXCoord"]], m[sym["wYCoord"]]) == STAND_XY, "player moved instead of turning"
        pyboy.screen.image.save(os.path.join(args.screenshot_dir, "verify_mew_0_facing.png"))
        print(f"\nPlaced: map ${m[sym['wCurMap']]:02X} at ({m[sym['wXCoord']]},{m[sym['wYCoord']]}) "
              f"facing UP toward Mew; EVENT_BEAT_MEW set: {flag(m, sym['wEventFlags'], EVENT_BEAT_MEW)}")
        assert not flag(m, sym["wEventFlags"], EVENT_BEAT_MEW), "EVENT_BEAT_MEW already set"

        snap = io.BytesIO()
        pyboy.save_state(snap)

        # ---------------- main case: talk -> battle ----------------
        pyboy.button("a", 8)
        battle = False
        for _ in range(120):
            g.tick(10)
            if m[sym["wIsInBattle"]]:
                battle = True
                break
            if "Mew!" in g.text():
                pyboy.button("a", 8)             # advance the pre-battle "Mew!" text
        appeared = False
        shot_battle = os.path.join(args.screenshot_dir, "verify_mew_1_battle.png")
        for _ in range(120):
            g.tick(10)
            if "Wild MEW" in g.text() or "appeared" in g.text():
                appeared = "Wild MEW" in g.text()
                g.tick(20)
                pyboy.screen.image.save(shot_battle)
                break
        in_battle = m[sym["wIsInBattle"]]
        species, level = m[sym["wEnemyMonSpecies"]], m[sym["wEnemyMonLevel"]]
        btype = m[sym["wBattleType"]]
        results["(1) battle fires"] = (battle, f"wIsInBattle={in_battle}")
        results["(2) species=MEW lv30"] = (species == MEW and level == obj_level == 30,
                                           f"wEnemyMonSpecies=${species:02X} wEnemyMonLevel={level}")

        # catch it with the Master Ball (A only; NO at the nickname prompt)
        for _ in range(60):
            if "FIGHT" in g.text() and "ITEM" in g.text():
                break
            pyboy.button("a", 8)
            g.tick(20)
        g.select("ITEM")
        assert g.wait_for("MASTER BALL"), "battle bag did not show MASTER BALL:\n" + g.text()
        g.select("MASTER BALL")
        caught_text, shot_caught = False, os.path.join(args.screenshot_dir, "verify_mew_2_caught.png")
        for _ in range(200):
            t = g.text()
            if "caught" in t and not caught_text:
                caught_text = True
                g.tick(20)
                pyboy.screen.image.save(shot_caught)
            if "nickname" in t:
                if g.wait_for("YES", 300) and "NO" in g.text():
                    g.select("NO")       # decline the nickname (cursor starts on YES)
                    continue
                g.press("a", 30)        # text still printing; advance it
                continue
            if not m[sym["wIsInBattle"]] and caught_text:
                break
            g.press("a", 30)
        g.tick(120)
        party = [m[sym["wPartySpecies"] + i] for i in range(m[sym["wPartyCount"]])]
        owned = flag(m, sym["wPokedexOwned"], DEX_MEW - 1)
        beat = flag(m, sym["wEventFlags"], EVENT_BEAT_MEW)
        hidden = flag(m, sym["wToggleableObjectFlags"], TOGGLE_MEW)
        caught = (caught_text and MEW in party and m[sym["wPartyMon2Level"]] == 30 and owned)
        results["(3) wild/catchable"] = (
            in_battle == 1 and btype == 0 and appeared and caught and beat and hidden,
            f"wIsInBattle={in_battle} (1=wild) wBattleType={btype} 'Wild MEW appeared'={appeared}; "
            f"caught={caught_text} party={['$%02X' % s for s in party]} lv{m[sym['wPartyMon2Level']]} "
            f"dex-owned={owned} EVENT_BEAT_MEW={beat} object hidden={hidden}")

        # ---------------- negative control: flag pre-set ----------------
        snap.seek(0)
        pyboy.load_state(snap)
        set_flag(m, sym["wEventFlags"], EVENT_BEAT_MEW, True)
        pyboy.button("a", 8)
        battle2 = False
        for _ in range(90):
            g.tick(10)
            if m[sym["wIsInBattle"]]:
                battle2 = True
                break
            if "Mew!" in g.text():
                pyboy.button("a", 8)
        pyboy.screen.image.save(os.path.join(args.screenshot_dir, "verify_mew_3_flag_set.png"))
        results["(4) flag-set suppresses re-encounter"] = (
            not battle2, f"EVENT_BEAT_MEW pre-set -> wIsInBattle={m[sym['wIsInBattle']]}")
    finally:
        pyboy.stop(save=False)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== Summary ===")
    for k, (ok, detail) in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {k:<38} {detail}")
    print(f"  screenshots in {args.screenshot_dir}: verify_mew_0_facing.png, verify_mew_1_battle.png, "
          f"verify_mew_2_caught.png, verify_mew_3_flag_set.png")
    failed = [k for k, (ok, _) in results.items() if not ok]
    expected = 4
    if len(results) < expected:
        failed.append("not all checks ran")
    print(f"\n{len(results) - len(failed)}/{expected} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
