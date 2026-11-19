#!/usr/bin/env python3
"""
Headless proof that every talk-to static Pokémon added on this branch works in
the built pokeyellow.gbc, plus a save round trip per species. Generalises
tools/verify_mew.py (same warp-and-place harness) to a data table.

Statics (from data/maps/objects/*.asm; species/level/tile are read from there):
  SILVER  Rock Tunnel B1F     EVENT_BEAT_SILVER  TOGGLE_SILVER   Lv50
  QUINN   Viridian Forest     EVENT_BEAT_QUINN   TOGGLE_QUINN    Lv50
  RAYA    Cerulean Cave B1F   EVENT_BEAT_RAYA    TOGGLE_RAYA     Lv50
  MEW     Cerulean Cave B1F   EVENT_BEAT_MEW     TOGGLE_MEW      Lv30

Placement (per static, from one shared bedroom snapshot):
  Boot a temp ROM copy (the repo's .ram/.state are never touched), play the
  intro to the bedroom, inject a Lv50 KADABRA plus a known bag (MASTER BALL x1,
  POTION x7, ESCAPE ROPE x3) and money ¥123456. Redirect the bedroom stairs'
  wWarpEntries row to the static's map and walk onto them (a real warp). A hook
  right after LoadDestinationWarpPosition's CopyData rewrites the view pointer
  and coords for the tile directly below the static (the game's own
  event_displacement formula); press UP to face it.

Checks per static (all must pass; exit non-zero otherwise):
  (1) battle fires        talking to it sets wIsInBattle
  (2) species + level     wEnemyMonSpecies/wEnemyMonLevel match the object_event
  (3) wild/catchable      wIsInBattle == 1 (wild), wBattleType 0, "Wild X appeared!",
                          no move used before the ball, and a Master Ball catch puts it in the party at that
                          level, sets its Pokédex owned AND seen bits, sets its
                          EVENT_BEAT_* flag and hides the object (its TOGGLE_*)
  (4) save round trip     START > SAVE > YES, then stop the emulator writing the
                          cartridge RAM, boot a NEW emulator on the same ROM +
                          .ram, CONTINUE, and compare: the whole Main Data block
                          (wMainDataStart..wMainDataEnd: Pokédex flags, bag,
                          money, event flags, toggles, ...), the party block and
                          the current box must be byte-identical except the play
                          clock, wPlayerDirection (CONTINUE always faces DOWN) and
                          wSavedSpriteImageIndex (per-frame OAM scratch inside the
                          saved block); and the named
                          values (dex bits 152-154, money, POTION count, event
                          flags) are reported before/after
  (5) flag suppresses     negative control: from the pre-talk snapshot with the
                          EVENT_BEAT_* flag pre-set, talking does NOT start a battle

Screenshots: verify_static_<species>_battle.png (battle intro) and
_caught.png / _continued.png / _flag_set.png.

Requires: pip install pyboy pillow
Usage:    python3 tools/verify_statics.py [--only SILVER,MEW] [--rom pokeyellow.gbc] [--screenshot-dir DIR]
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
PLAYER_DIR_DOWN = None  # set in main() from constants/sprite_data_constants.asm

# species, map file, map constant, text constant, expected level
STATICS = [
    ("SILVER", "RockTunnelB1F", "ROCK_TUNNEL_B1F", "TEXT_ROCKTUNNELB1F_SILVER", 50),
    ("QUINN", "ViridianForest", "VIRIDIAN_FOREST", "TEXT_VIRIDIANFOREST_QUINN", 50),
    ("RAYA", "CeruleanCaveB1F", "CERULEAN_CAVE_B1F", "TEXT_CERULEANCAVEB1F_RAYA", 50),
    ("MEW", "CeruleanCaveB1F", "CERULEAN_CAVE_B1F", "TEXT_CERULEANCAVEB1F_MEW", 30),
]
BAG = [("MASTER_BALL", 1), ("POTION", 7), ("ESCAPE_ROPE", 3)]
MONEY_BCD = [0x12, 0x34, 0x56]


def static_object(map_file, text_const):
    for c in ve.lines(f"data/maps/objects/{map_file}.asm"):
        m = re.match(rf"object_event\s+(\d+),\s*(\d+),\s*(\w+),.*,\s*{text_const},\s*(\w+),\s*(\d+)$", c)
        if m:
            return (int(m.group(1)), int(m.group(2))), m.group(3), m.group(4), int(m.group(5))
    raise ValueError(f"{text_const} object_event not found in {map_file}")


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


def snapshot_ram(g):
    s, m = g.s, g.m
    blk = lambda a, b: bytes(m[i] for i in range(s[a], s[b]))  # noqa: E731
    return {"main": blk("wMainDataStart", "wMainDataEnd"),
            "party": blk("wPartyDataStart", "wPartyDataEnd"),
            "box": blk("wBoxDataStart", "wBoxDataEnd")}


def named_values(g, ids):
    s, m = g.s, g.m
    v = {}
    for name in ("SILVER", "QUINN", "RAYA"):
        d = ids["dex"][f"DEX_{name}"]
        v[f"dex #{d} {name} owned/seen"] = (int(flag(m, s["wPokedexOwned"], d - 1)),
                                           int(flag(m, s["wPokedexSeen"], d - 1)))
    v["money (BCD)"] = "".join(f"{m[s['wPlayerMoney'] + i]:02X}" for i in range(3))
    bag = {m[s["wBagItems"] + 2 * i]: m[s["wBagItems"] + 2 * i + 1] for i in range(m[s["wNumBagItems"]])}
    v["bag POTION qty"] = bag.get(ids["items"]["POTION"])
    v["bag ESCAPE_ROPE qty"] = bag.get(ids["items"]["ESCAPE_ROPE"])
    for ev in ids["events_to_report"]:
        v[ev] = int(flag(m, s["wEventFlags"], ids["events"][ev]))
    v["wCurMap / X,Y"] = (m[s["wCurMap"]], m[s["wXCoord"]], m[s["wYCoord"]])
    return v


def boot_continue(g):
    """Title -> CONTINUE -> overworld."""
    g.tick(600)
    for i in range(80):
        if "CONTINUE" in g.text():
            break
        g.p.button("start" if i % 2 else "a", 4)
        g.tick(30)
    else:
        raise RuntimeError("no CONTINUE on the main menu:\n" + g.text())
    g.select("CONTINUE")
    assert g.wait_for("TIME", 300), "no save summary box:\n" + g.text()
    for _ in range(20):                       # A until the menu/summary are gone and the map runs
        if "CONTINUE" not in g.text() and "TIME" not in g.text():
            break
        g.press("a", 60)
    else:
        raise RuntimeError("CONTINUE never entered the game:\n" + g.text())
    g.tick(240)
    # prove we are in the overworld: the START menu opens, then close it
    g.p.button("start", 4)
    g.tick(40)
    assert g.wait_for("SAVE", 120) and "ITEM" in g.text(), "not in the overworld after CONTINUE:\n" + g.text()
    g.p.button("b", 4)
    g.tick(60)


def save_game(g):
    g.p.button("start", 4)
    g.tick(40)
    assert g.wait_for("SAVE", 120), "start menu did not open:\n" + g.text()
    g.select("SAVE")
    assert g.wait_for("YES", 300), "no save prompt:\n" + g.text()
    g.select("YES")
    assert g.wait_for("saved", 900), "game never reported saving:\n" + g.text()
    g.tick(240)


def run_static(PyBoy, rom_src, bedroom_state, sym, consts, ids, st, hook_addr, shot_dir):
    species_name, map_file, map_const, text_const, want_level = st
    s = sym
    (ox, oy), sprite, obj_species, obj_level = static_object(map_file, text_const)
    assert obj_species == species_name, (obj_species, species_name)
    SPECIES = consts["species"][species_name]
    DEX = ids["dex"][f"DEX_{species_name}"]
    EVENT = ids["events"][f"EVENT_BEAT_{species_name}"]
    TOGGLE = ids["toggles"][f"TOGGLE_{species_name}"]
    map_id = ids["maps"][map_const]
    w = ve.map_width(map_const)
    sx, sy = ox, oy + 1  # stand directly below, face UP
    view_ptr = s["wOverworldMap"] + 7 + w + (w + 6) * (sy >> 1) + (sx >> 1)
    tag = species_name.lower()
    shot = lambda k: os.path.join(shot_dir, f"verify_static_{tag}_{k}.png")  # noqa: E731
    res = {}
    print(f"\n--- {species_name}: {sprite} at ({ox},{oy}) on {map_const} (${map_id:02X}, width {w}), "
          f"lv{obj_level}; species ${SPECIES:02X} dex #{DEX}; EVENT_BEAT_{species_name}=${EVENT:03X} "
          f"(byte ${s['wEventFlags'] + EVENT // 8:04X} bit {EVENT % 8}); TOGGLE_{species_name}=${TOGGLE:02X}; "
          f"player at ({sx},{sy}) facing UP ---")

    tmp = tempfile.mkdtemp(prefix=f"verify_static_{tag}_")
    rom = os.path.join(tmp, "rom.gbc")
    shutil.copy(rom_src, rom)  # temp copy; its .ram lives in tmp too
    hook_state = {"armed": False, "fired": 0}

    def make(sound=False):
        p = PyBoy(rom, window="null", sound_emulated=sound)
        g = ve.Game(p, sym)

        def on_warp_position(_):
            if hook_state["armed"] and g.m[s["wCurMap"]] == map_id:
                g.m[s["wCurrentTileBlockMapViewPointer"]] = view_ptr & 0xFF
                g.m[s["wCurrentTileBlockMapViewPointer"] + 1] = view_ptr >> 8
                g.m[s["wYCoord"]] = sy
                g.m[s["wXCoord"]] = sx
                hook_state["fired"] += 1
        p.hook_register(0, hook_addr, on_warp_position, None)
        return p, g

    pyboy, g = make()
    m = g.m
    try:
        bedroom_state.seek(0)
        pyboy.load_state(bedroom_state)
        wy, wx = m[s["wWarpEntries"]], m[s["wWarpEntries"] + 1]
        m[s["wWarpEntries"] + 2] = 0       # destination warp index 0 (position is overridden by the hook)
        m[s["wWarpEntries"] + 3] = map_id
        hook_state["armed"] = True
        walk_to(g, wx, wy + 1)
        pyboy.button("up", 8)
        for _ in range(60):
            g.tick(10)
            if m[s["wCurMap"]] == map_id and hook_state["fired"]:
                break
        g.tick(120)
        hook_state["armed"] = False
        assert m[s["wCurMap"]] == map_id, f"warp failed: map ${m[s['wCurMap']]:02X}"
        assert hook_state["fired"] == 1, f"position hook fired {hook_state['fired']} times"
        assert (m[s["wXCoord"]], m[s["wYCoord"]]) == (sx, sy), \
            f"player at ({m[s['wXCoord']]},{m[s['wYCoord']]}), expected ({sx},{sy})"
        pyboy.button("up", 8)
        g.tick(40)
        assert (m[s["wXCoord"]], m[s["wYCoord"]]) == (sx, sy), "player moved instead of turning"
        assert not flag(m, s["wEventFlags"], EVENT), f"EVENT_BEAT_{species_name} already set"
        assert not flag(m, s["wPokedexOwned"], DEX - 1), "already owned before the catch"
        placed = io.BytesIO()
        pyboy.save_state(placed)

        # ---- (1)-(3) talk, battle, catch ----
        cry_text = f"{species_name.capitalize()}!"
        pyboy.button("a", 8)
        battle = False
        for _ in range(120):
            g.tick(10)
            if m[s["wIsInBattle"]]:
                battle = True
                break
            if cry_text in g.text():
                pyboy.button("a", 8)
        appeared = False
        for _ in range(120):
            g.tick(10)
            t = g.text()
            if "appeared" in t:
                appeared = f"Wild {species_name}" in t
                g.tick(20)
                pyboy.screen.image.save(shot("battle"))
                break
        in_battle, btype = m[s["wIsInBattle"]], m[s["wBattleType"]]
        sp, lv = m[s["wEnemyMonSpecies"]], m[s["wEnemyMonLevel"]]
        res["(1) battle fires"] = (battle, f"wIsInBattle={in_battle}")
        res["(2) species + level"] = (sp == SPECIES and lv == obj_level == want_level,
                                      f"wEnemyMonSpecies=${sp:02X} (want ${SPECIES:02X}) "
                                      f"wEnemyMonLevel={lv} (want {want_level})")
        # Reach the battle menu WITHOUT ever choosing FIGHT: the opponent must get no
        # turn before the ball (a Mew that uses Transform is caught as a Ditto).
        # Press A only when the menu has not shown for a while, and back out of the
        # move list (B) if a press lands on FIGHT.
        used, idle = [], 0
        for _ in range(200):
            t = g.text()
            used += [ln.strip() for ln in t.split("\n") if " used " in f" {ln} " and ln.strip() not in used]
            if "TYPE/" in t:                      # move list opened: back out, never pick a move
                pyboy.button("b", 8)
                g.tick(30)
                continue
            if "FIGHT" in t and "ITEM" in t:
                break
            g.tick(10)
            idle += 1
            if idle >= 12:                        # 120 frames without the menu: advance text
                pyboy.button("a", 8)
                idle = 0
        assert not used, f"a move was used before the ball: {used}"
        g.select("ITEM")
        assert g.wait_for("MASTER BALL"), "battle bag did not show MASTER BALL:\n" + g.text()
        g.select("MASTER BALL")
        caught_text = False
        for _ in range(200):
            t = g.text()
            if "caught" in t and not caught_text:
                caught_text = True
                g.tick(20)
                pyboy.screen.image.save(shot("caught"))
            if "nickname" in t:
                if g.wait_for("YES", 300) and "NO" in g.text():
                    g.select("NO")
                    continue
                g.press("a", 30)
                continue
            if not m[s["wIsInBattle"]] and caught_text:
                break
            g.press("a", 30)
        g.tick(120)
        party = [m[s["wPartySpecies"] + i] for i in range(m[s["wPartyCount"]])]
        owned = flag(m, s["wPokedexOwned"], DEX - 1)
        seen = flag(m, s["wPokedexSeen"], DEX - 1)
        beat = flag(m, s["wEventFlags"], EVENT)
        hidden = flag(m, s["wToggleableObjectFlags"], TOGGLE)
        caught = caught_text and SPECIES in party and m[s["wPartyMon2Level"]] == want_level
        res["(3) wild/catchable"] = (
            in_battle == 1 and btype == 0 and appeared and caught and owned and seen and beat and hidden,
            f"wIsInBattle={in_battle} (1=wild) wBattleType={btype} 'Wild {species_name} appeared'={appeared}; "
            f"caught={caught_text} party={['$%02X' % x for x in party]} lv{m[s['wPartyMon2Level']]} "
            f"dex#{DEX} owned={owned} seen={seen} EVENT_BEAT={beat} object hidden={hidden}")

        # ---- (4) save round trip ----
        save_game(g)
        before_ram, before_vals = snapshot_ram(g), named_values(g, ids)
        pyboy.stop(save=True)  # writes tmp/rom.gbc.ram (the temp copy's cartridge RAM)
        pyboy = None
        assert os.path.exists(rom + ".ram"), "emulator wrote no cartridge RAM"
        pyboy, g = make()
        m = g.m
        boot_continue(g)
        pyboy.screen.image.save(shot("continued"))  # overworld after CONTINUE
        after_ram, after_vals = snapshot_ram(g), named_values(g, ids)
        # Allowed differences, nothing else: the play clock keeps running between the
        # save and the snapshot; CONTINUE always faces the player down
        # (engine/menus/main_menu.asm: PLAYER_DIR_DOWN -> wPlayerDirection); and the
        # OAM scratch byte.
        main_start = s["wMainDataStart"]
        clock = set(range(s["wPlayTimeHours"] - main_start, s["wPlayTimeFrames"] + 1 - main_start))
        facing = s["wPlayerDirection"] - main_start
        # wSavedSpriteImageIndex is PrepareOAMData's per-frame scratch byte that happens
        # to sit inside the saved block (engine/gfx/sprite_oam.asm); its value depends
        # only on which frame the snapshot lands on.
        oam_scratch = s["wSavedSpriteImageIndex"] - main_start
        diffs, expected = {}, []
        for blk, a in before_ram.items():
            b = after_ram[blk]
            diffs[blk] = []
            for i in range(len(a)):
                if a[i] == b[i]:
                    continue
                if blk == "main" and (i in clock or i == oam_scratch or (i == facing and b[i] == PLAYER_DIR_DOWN)):
                    expected.append(f"${main_start + i:04X} ${a[i]:02X}->${b[i]:02X}")
                    continue
                diffs[blk].append(f"{'$%04X' % (main_start + i) if blk == 'main' else '+%d' % i} "
                                  f"${a[i]:02X}->${b[i]:02X}")
        diff_desc = "; ".join(f"{k} ({len(before_ram[k])} B): {len(v)} unexpected diff(s)"
                              + (" " + ", ".join(v[:6]) if v else "") for k, v in diffs.items())
        diff_desc += f"; allowed (play clock, facing reset by CONTINUE, OAM scratch): [{', '.join(expected)}]"
        rt_ok = all(not v for v in diffs.values()) and before_vals == after_vals
        res["(4) save round trip"] = (rt_ok, diff_desc)
        res["_values"] = (before_vals, after_vals)

        # ---- (5) negative control ----
        placed.seek(0)
        pyboy.load_state(placed)
        set_flag(m, s["wEventFlags"], EVENT, True)
        pyboy.button("a", 8)
        battle2 = False
        for _ in range(90):
            g.tick(10)
            if m[s["wIsInBattle"]]:
                battle2 = True
                break
            if cry_text in g.text():
                pyboy.button("a", 8)
        pyboy.screen.image.save(shot("flag_set"))
        res["(5) flag-set suppresses re-encounter"] = (
            not battle2, f"EVENT_BEAT_{species_name} pre-set -> wIsInBattle={m[s['wIsInBattle']]}")
    except Exception as e:
        res["error"] = (False, f"{type(e).__name__}: {e}")
        try:
            print("screen at error:\n" + g.text())
        except Exception:
            pass
    finally:
        if pyboy is not None:
            pyboy.stop(save=False)
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", default=os.path.join(ROOT, "pokeyellow.gbc"))
    ap.add_argument("--sym", default=None, help="defaults to <rom>.sym")
    ap.add_argument("--screenshot-dir", default=os.getcwd())
    ap.add_argument("--only", default=None, help="comma-separated species, e.g. SILVER,MEW")
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
            "wPokedexOwned", "wPokedexSeen", "wToggleableObjectFlags", "wPartyCount", "wPartySpecies",
            "wPartyMon2Level", "wMainDataStart", "wMainDataEnd", "wPartyDataStart", "wPartyDataEnd",
            "wBoxDataStart", "wBoxDataEnd", "wPlayerMoney", "wBagItems", "wNumBagItems",
            "wPlayTimeHours", "wPlayTimeFrames", "wPlayerDirection", "wSavedSpriteImageIndex"]
    missing = [n for n in need if n not in sym]
    assert not missing, f"symbols missing from {sym_path}: {missing}"

    consts = {"species": ve.load_consts("constants/pokemon_constants.asm"),
              "items": ve.load_consts("constants/item_constants.asm")}
    ve.TYPE_NAMES.update(ve.load_consts("constants/type_constants.asm"))
    ve.MOVES.update(ve.load_consts("constants/move_constants.asm"))
    ve.GROWTH.update({k: v for k, v in ve.load_consts("constants/pokemon_data_constants.asm").items()
                      if k.startswith("GROWTH_")})
    ids = {"dex": ve.load_consts("constants/pokedex_constants.asm"),
           "events": ve.load_consts("constants/event_constants.asm"),
           "toggles": ve.load_consts("constants/toggle_constants.asm"),
           "maps": ve.load_consts("constants/map_constants.asm"),
           "items": consts["items"],
           "events_to_report": ["EVENT_BEAT_SILVER", "EVENT_BEAT_QUINN", "EVENT_BEAT_RAYA", "EVENT_BEAT_MEW",
                                "EVENT_BEAT_MEWTWO", "EVENT_FOLLOWED_OAK_INTO_LAB"]}
    global PLAYER_DIR_DOWN
    PLAYER_DIR_DOWN = 1 << ve.load_consts("constants/sprite_data_constants.asm")["PLAYER_DIR_BIT_DOWN"]
    statics = STATICS if not args.only else [st for st in STATICS if st[0] in args.only.upper().split(",")]
    assert statics, "no statics selected"

    print("RAM addresses (from %s):" % os.path.relpath(sym_path, ROOT))
    for n in ("wMainDataStart", "wPokedexOwned", "wPokedexSeen", "wNumBagItems", "wBagItems", "wPlayerMoney",
              "wEventFlags", "wToggleableObjectFlags", "wMainDataEnd", "wPartyDataStart", "wBoxDataStart",
              "wCurMap", "wWarpEntries", "wIsInBattle", "wEnemyMonSpecies", "wEnemyMonLevel"):
        print(f"  {n:<26} ${sym[n]:04X}")
    print(f"  wPokedexOwned size         {sym['wPokedexOwnedEnd'] - sym['wPokedexOwned']} bytes "
          f"(NUM_POKEMON = {len([k for k in ids['dex'] if k.startswith('DEX_')])})")

    os.makedirs(args.screenshot_dir, exist_ok=True)
    rom_bytes = open(args.rom, "rb").read()
    entry, cd = sym["LoadDestinationWarpPosition"], sym["CopyData"]
    at = rom_bytes.find(bytes([0xCD, cd & 0xFF, cd >> 8]), entry, entry + 0x40)
    assert at > 0 and rom_bytes[at + 3] == 0xF1, "could not locate `call CopyData / pop af` in LoadDestinationWarpPosition"
    hook_addr = at + 3

    # one shared bedroom snapshot
    tmp = tempfile.mkdtemp(prefix="verify_statics_boot_")
    rom = os.path.join(tmp, "rom.gbc")
    shutil.copy(args.rom, rom)
    pyboy = PyBoy(rom, window="null", sound_emulated=False)
    g = ve.Game(pyboy, sym)
    try:
        ve.reach_overworld(g)
        pyboy.button("b", 4)
        g.tick(60)
        ve.inject(g, consts, "KADABRA", level=50, catch_rate=0, bag_item="MASTER_BALL", exp_mode="at_level")
        m = g.m
        m[sym["wNumBagItems"]] = len(BAG)
        for i, (it, q) in enumerate(BAG):
            m[sym["wBagItems"] + 2 * i] = consts["items"][it]
            m[sym["wBagItems"] + 2 * i + 1] = q
        m[sym["wBagItems"] + 2 * len(BAG)] = 0xFF
        for i, b in enumerate(MONEY_BCD):
            m[sym["wPlayerMoney"] + i] = b
        assert m[sym["wNumberOfWarps"]] >= 1, "bedroom has no warp"
        bedroom = io.BytesIO()
        pyboy.save_state(bedroom)
    finally:
        pyboy.stop(save=False)
        shutil.rmtree(tmp, ignore_errors=True)

    all_res = {}
    for st in statics:
        all_res[st[0]] = run_static(PyBoy, args.rom, bedroom, sym, consts, ids, st, hook_addr, args.screenshot_dir)

    print("\n=== Summary ===")
    failed = 0
    for sp, res in all_res.items():
        for k, (ok, detail) in res.items():
            if k == "_values":
                continue
            failed += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {sp:<7} {k:<38} {detail}")
        n_checks = len([k for k in res if k.startswith("(")])
        if n_checks < 5:
            failed += 1
            print(f"  FAIL  {sp:<7} only {n_checks}/5 checks ran")
    print("\n=== Save round trip: named values before save -> after reboot + CONTINUE ===")
    for sp, res in all_res.items():
        if "_values" not in res:
            continue
        before, after = res["_values"]
        print(f"  [{sp} caught, saved, rebooted]")
        for k in before:
            mark = "same" if before[k] == after[k] else "CHANGED"
            print(f"    {k:<34} {str(before[k]):<16} -> {str(after[k]):<16} {mark}")
    total = 5 * len(all_res)
    print(f"\nscreenshots in {args.screenshot_dir}: verify_static_<species>_{{battle,caught,continued,flag_set}}.png")
    print(f"\n{total - failed}/{total} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
