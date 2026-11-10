#!/usr/bin/env python3
"""
Headless proof of the step-2b starter-Pikachu change in the built pokeyellow.gbc:
only the Oak's Lab follower Pikachu (catch-rate byte == LIGHT_BALL_GSC) refuses
a Thunder Stone; any other own-OT Pikachu (base catch rate, like a wild catch)
evolves into Raichu.

Cases (both must pass):
  A  follower replica: PIKACHU, catch rate = LIGHT_BALL_GSC, own OT  -> must REFUSE
  B  wild-style:       PIKACHU, catch rate = base (from base_stats), own OT -> must EVOLVE

Negative control (also checked; it validates the proof rather than the build):
  C  Case B's exact Pikachu on a temp ROM copy where the LIGHT_BALL_GSC
     condition is patched out of both pikachu_status.asm routines (the
     `jr nz` after `cp LIGHT_BALL_GSC` becomes two NOPs, i.e. the pre-2b
     predicate). It must be REFUSED, showing the catch-rate byte is the only
     thing separating A from B.

Harness (shared with tools/verify_evo.py): boot a temp copy of the ROM (so the
repo's .ram/.state are never touched), reach the overworld via the intro, then
per case reload that snapshot, inject party slot 1 (own OT ID/name), put one
THUNDER_STONE in the bag, START > ITEM > THUNDERSTONE > USE > PIKACHU, press only A.

All RAM addresses come from pokeyellow.sym; ids, catch rates and names from the
repo's constants/data.

Requires: pip install pyboy pillow
Usage:    python3 tools/verify_pikachu_block.py [--rom pokeyellow.gbc] [--screenshot-dir DIR]
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
LEVEL = 25  # neither PIKACHU nor RAICHU learns a move at 25 (no learn-move prompt)


def item_name(item_id):
    """Display name from data/items/names.asm (li entries, 1-based by item id)."""
    names = [m.group(1) for l in ve.lines("data/items/names.asm")
             for m in [re.match(r'li\s+"([^"]*)"$', l)] if m]
    return names[item_id - 1]


def base_catch_rate(name):
    with open(os.path.join(ROOT, f"data/pokemon/base_stats/{name.lower()}.asm"), encoding="utf-8") as f:
        for l in f:
            m = re.match(r"\s*db\s+(\d+)\s*;\s*catch rate", l)
            if m:
                return int(m.group(1))
    raise ValueError(f"no catch rate for {name}")


def light_ball_value():
    for c in ve.lines("constants/item_constants.asm"):
        m = re.match(r"DEF\s+LIGHT_BALL_GSC\s+EQU\s+(\S+)$", c)
        if m:
            return ve.parse_num(m.group(1))
    raise ValueError("LIGHT_BALL_GSC not defined")


def rom_offset(sym_entry):
    bank, addr = sym_entry
    return bank * 0x4000 + (addr - 0x4000) if bank else addr


def load_sym_banked(path):
    out = {}
    with open(path, encoding="utf-8") as f:
        for l in f:
            m = re.match(r"([0-9a-fA-F]+):([0-9a-fA-F]+)\s+(\S+)$", l.strip())
            if m:
                out[m.group(3)] = (int(m.group(1), 16), int(m.group(2), 16))
    return out


def patch_out_catch_rate_check(rom_bytes, sym_banked, lb):
    """Return a copy with `cp LIGHT_BALL_GSC / jr nz, x` neutralised (jr nz -> nop nop)
    in IsStarterPikachuAliveInOurParty and IsThisMonStarterPikachu."""
    rom = bytearray(rom_bytes)
    patched = []
    for routine in ("IsStarterPikachuAliveInOurParty", "IsThisMonStarterPikachu"):
        start = rom_offset(sym_banked[routine])
        window = rom[start:start + 0x60]
        hits = [i for i in range(len(window) - 3)
                if window[i] == 0xFE and window[i + 1] == lb and window[i + 2] == 0x20]
        assert len(hits) == 1, f"{routine}: expected one `cp ${lb:02X} / jr nz`, found {len(hits)}"
        at = start + hits[0] + 2
        rom[at:at + 2] = b"\x00\x00"
        patched.append(f"{routine}+${hits[0] + 2:02X}")
    return bytes(rom), patched


def run_case(g, consts, name, catch_rate, expect, shot_path):
    sym = g.s
    pika, rai = consts["species"]["PIKACHU"], consts["species"]["RAICHU"]
    stone = consts["items"]["THUNDER_STONE"]
    stone_name = item_name(stone)
    r = {"case": name, "catch_rate": catch_rate, "expect": expect, "outcome": None,
         "after": None, "shot": None, "ok": False, "error": None}
    print(f"\n--- {name}: PIKACHU lv{LEVEL}, catch rate ${catch_rate:02X}, own OT -> expect {expect} ---")
    ve.inject(g, consts, "PIKACHU", level=LEVEL, catch_rate=catch_rate,
              bag_item="THUNDER_STONE", exp_mode="at_level")
    g.tick(10)
    m = g.m
    print(f"slot 1: species=${m[sym['wPartyMon1Species']]:02X} "
          f"catch rate=${m[sym['wPartyMon1CatchRate']]:02X} "
          f"OT ID={m[sym['wPartyMon1OTID']]:02X}{m[sym['wPartyMon1OTID'] + 1]:02X} "
          f"(player {m[sym['wPlayerID']]:02X}{m[sym['wPlayerID'] + 1]:02X})")

    g.select("ITEM")
    assert g.wait_for(stone_name), f"bag did not show {stone_name}:\n" + g.text()
    g.select(stone_name)
    assert g.wait_for("USE"), "no USE/TOSS menu:\n" + g.text()
    g.select("USE")
    assert g.wait_for("PIKACHU"), "party menu did not show PIKACHU:\n" + g.text()
    g.select_party("PIKACHU")

    # Press only A until either the refusal or the evolution message appears.
    deadline = g.frames + 60 * 60 * 2
    while g.frames < deadline:
        t = g.text()
        if "is refusing" in t:
            r["outcome"] = "refused"
        elif "evolved" in t and "into" in t:
            r["outcome"] = "evolved"
        if r["outcome"]:
            g.tick(30)
            g.p.screen.image.save(shot_path)
            r["shot"] = shot_path
            break
        g.press("a", 40)

    # Let everything finish (evolution writes the species ~200 frames after its
    # text); keep pressing A only, and give a refused mon time to (not) evolve.
    for _ in range(20):
        if m[sym["wPartyMon1Species"]] == rai:
            break
        g.tick(20)
    for _ in range(6):
        g.press("a", 30)
    g.tick(120)

    r["after"] = m[sym["wPartyMon1Species"]]
    after_list = m[sym["wPartySpecies"]]
    if expect == "refuse":
        r["ok"] = r["outcome"] == "refused" and r["after"] == pika and after_list == pika
    else:
        r["ok"] = r["outcome"] == "evolved" and r["after"] == rai and after_list == rai
    print(f"outcome: {r['outcome']}; after: wPartyMon1Species=${r['after']:02X} "
          f"wPartySpecies[0]=${after_list:02X} (PIKACHU=${pika:02X}, RAICHU=${rai:02X}); "
          f"screenshot: {r['shot'] or 'NOT TAKEN'}")
    print("PASS" if r["ok"] else "FAIL")
    if not r["ok"]:
        print("final screen:\n" + g.text())
    return r


def run_rom(PyBoy, rom_bytes, sym, consts, cases, shot_dir, tag):
    tmp = tempfile.mkdtemp(prefix="verify_pikachu_")
    rom = os.path.join(tmp, "rom.gbc")
    with open(rom, "wb") as f:
        f.write(rom_bytes)  # temp copy: never touches the repo's .ram/.state
    pyboy = PyBoy(rom, window="null", sound_emulated=False)
    g = ve.Game(pyboy, sym)
    results = []
    try:
        ve.reach_overworld(g)
        print(f"[{tag}] reached overworld start menu after {g.frames} frames")
        snap = io.BytesIO()
        pyboy.save_state(snap)
        for name, catch_rate, expect, shot in cases:
            snap.seek(0)
            pyboy.load_state(snap)
            try:
                results.append(run_case(g, consts, name, catch_rate, expect,
                                        os.path.join(shot_dir, shot)))
            except Exception as e:
                print(f"FAIL ({type(e).__name__}): {e}")
                results.append({"case": name, "catch_rate": catch_rate, "expect": expect,
                                "outcome": None, "after": None, "shot": None, "ok": False,
                                "error": f"{type(e).__name__}: {e}"})
    finally:
        pyboy.stop(save=False)
        shutil.rmtree(tmp, ignore_errors=True)
    return results


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
    banked = load_sym_banked(sym_path)
    need = ["wPartyMon1CatchRate", "wPartyMon1OTID", "wPlayerID", "wPartyMon1Species",
            "wPartySpecies", "IsStarterPikachuAliveInOurParty", "IsThisMonStarterPikachu"]
    missing = [n for n in need if n not in sym]
    assert not missing, f"symbols missing from {sym_path}: {missing}"

    consts = {"species": ve.load_consts("constants/pokemon_constants.asm"),
              "items": ve.load_consts("constants/item_constants.asm")}
    ve.TYPE_NAMES.update(ve.load_consts("constants/type_constants.asm"))
    ve.MOVES.update(ve.load_consts("constants/move_constants.asm"))
    ve.GROWTH.update({k: v for k, v in ve.load_consts("constants/pokemon_data_constants.asm").items()
                      if k.startswith("GROWTH_")})
    lb, base = light_ball_value(), base_catch_rate("PIKACHU")
    assert lb != base, "LIGHT_BALL_GSC equals PIKACHU's base catch rate; cases would be identical"

    print("RAM addresses (from %s):" % os.path.relpath(sym_path, ROOT))
    for n in ("wPartySpecies", "wPartyMon1Species", "wPartyMon1CatchRate", "wPartyMon1OTID",
              "wPartyMon1Level", "wPartyMonOT", "wPlayerID", "wPlayerName", "wBagItems"):
        print(f"  {n:<20} ${sym[n]:04X}")
    print(f"  MON_CATCH_RATE offset = {sym['wPartyMon1CatchRate'] - sym['wPartyMon1']}")
    print(f"Ids: PIKACHU=${consts['species']['PIKACHU']:02X}  RAICHU=${consts['species']['RAICHU']:02X}  "
          f"THUNDER_STONE=${consts['items']['THUNDER_STONE']:02X}  LIGHT_BALL_GSC=${lb:02X}  "
          f"PIKACHU base catch rate={base} (${base:02X})")

    os.makedirs(args.screenshot_dir, exist_ok=True)
    with open(args.rom, "rb") as f:
        rom_bytes = f.read()

    results = run_rom(PyBoy, rom_bytes, sym, consts, [
        ("A: follower ($A3)", lb, "refuse", "verify_pikachu_A_refuse.png"),
        ("B: wild-style (base)", base, "evolve", "verify_pikachu_B_evolve.png"),
    ], args.screenshot_dir, "built ROM")

    patched, where = patch_out_catch_rate_check(rom_bytes, banked, lb)
    print(f"\nNegative control: temp ROM copy with the LIGHT_BALL_GSC check patched out at {', '.join(where)}")
    results += run_rom(PyBoy, patched, sym, consts, [
        ("C: control, B w/o $A3 check", base, "refuse", "verify_pikachu_C_control.png"),
    ], args.screenshot_dir, "control ROM")

    def hx(v):
        return "--" if v is None else f"${v:02X}"

    print("\n=== Summary ===")
    print(f"  {'case':<30} {'catch':>5} {'expect':>7} {'outcome':>8} {'species':>8}  result")
    for r in results:
        print(f"  {r['case']:<30} {hx(r['catch_rate']):>5} {r['expect']:>7} "
              f"{(r['outcome'] or '--'):>8} {hx(r['after']):>8}  {'PASS' if r['ok'] else 'FAIL'}"
              + (f"  ({r['error']})" if r["error"] else ""))
    for r in results:
        if r["shot"]:
            print(f"  screenshot {r['case'][:1]}: {r['shot']}")
    failed = sum(not r["ok"] for r in results)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
