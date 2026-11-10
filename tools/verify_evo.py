#!/usr/bin/env python3
"""
Headless proof that the four former trade evolutions, now EVOLVE_LEVEL 37,
work in the built pokeyellow.gbc, using PyBoy:
  Kadabra -> Alakazam, Machoke -> Machamp, Graveler -> Golem, Haunter -> Gengar

A bare run executes all four and prints a PASS/FAIL summary table, exiting
non-zero if any fail. The intro is played once; each case reloads that
overworld snapshot (in memory), so cases are independent.

Approach (no mid-game save state is needed):
  1. Boot a *copy* of the ROM in a temp dir (so PyBoy neither loads nor
     overwrites the repo's pokeyellow.gbc.ram battery save) with window="null".
  2. Drive the intro to the overworld by reading the on-screen text from the
     game's own wTileMap buffer (picking a preset name at both name menus).
  3. Per case: inject party slot 1 = the species at Lv36 with XP one below the
     Lv37 threshold of its own growth rate (wPartyCount, wPartySpecies list +
     $FF terminator, full party_struct, OT name, nickname), and put one
     RARE_CANDY in the bag.
  4. START > ITEM > RARE CANDY > USE > <MON>. Rare Candy runs the game's real
     level-up code and then `callfar TryEvolvingMon` (engine/items/item_effects.asm).
  5. Press only A to advance text. B is never pressed (Evolution_CheckForCancel
     in engine/movie/evolution.asm cancels on B).
  6. Screenshot when "evolved into" is on screen; read species back and assert
     it is the expected evolved form.

Every RAM address comes from pokeyellow.sym and every id/value from the
repo's constants and data files.

Requires: pip install pyboy pillow
Usage:    python3 tools/verify_evo.py [--only SPECIES] [--rom pokeyellow.gbc]
                                      [--screenshot-dir DIR]
"""

import argparse
import io
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# The four former trade evolutions, now EVOLVE_LEVEL 37. These pairs are the
# test's expectations, deliberately not read from evos_moves.asm.
CASES = [("KADABRA", "ALAKAZAM"),
         ("MACHOKE", "MACHAMP"),
         ("GRAVELER", "GOLEM"),
         ("HAUNTER", "GENGAR")]
FROM_LEVEL = 36


# ---------------------------------------------------------------------------
# Repo data
# ---------------------------------------------------------------------------

def lines(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return [l.split(";", 1)[0].strip() for l in f]


def load_sym(path):
    sym = {}
    with open(path, encoding="utf-8") as f:
        for l in f:
            m = re.match(r"([0-9a-fA-F]+):([0-9a-fA-F]+)\s+(\S+)$", l.strip())
            if m:
                sym[m.group(3)] = int(m.group(2), 16)
    return sym


def parse_num(s):
    s = s.strip()
    return int(s[1:], 16) if s.startswith("$") else int(s)


def load_consts(rel):
    """Evaluate const_def / const / const_skip / const_next / map_const / dw_const."""
    out, value, inc = {}, 0, 1
    for c in lines(rel):
        m = re.match(r"const_def(?:\s+(\S+?)(?:\s*,\s*(\S+))?)?$", c)
        if m:
            value = parse_num(m.group(1)) if m.group(1) else 0
            inc = parse_num(m.group(2)) if m.group(2) else 1
            continue
        m = re.match(r"(?:const|const_export|map_const)\s+(\w+)", c)
        if m:
            out[m.group(1)] = value
            value += inc
            continue
        m = re.match(r"const_skip(?:\s+(\S+))?$", c)
        if m:
            value += inc * (parse_num(m.group(1)) if m.group(1) else 1)
            continue
        m = re.match(r"const_next\s+(\S+)$", c)
        if m:
            value = parse_num(m.group(1))
    return out


def load_base_stats(name):
    ls = lines(f"data/pokemon/base_stats/{name.lower()}.asm")
    stats = types = rate = level1 = None
    for c in ls:
        m = re.match(r"db\s+(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)$", c)
        if m and stats is None:
            stats = dict(zip(("hp", "atk", "def", "spd", "spc"), map(int, m.groups())))
        m = re.match(r"db\s+(\w+),\s*(\w+)$", c)
        if m and types is None and m.group(1) in TYPE_NAMES and m.group(2) in TYPE_NAMES:
            types = (m.group(1), m.group(2))
        m = re.match(r"db\s+(GROWTH_\w+)$", c)
        if m:
            rate = m.group(1)
        m = re.match(r"db\s+(\w+),\s*(\w+),\s*(\w+),\s*(\w+)$", c)
        if m and level1 is None and all(x in MOVES for x in m.groups()):
            level1 = [x for x in m.groups() if x != "NO_MOVE"]
    assert stats and types and rate and level1 is not None, (stats, types, rate, level1)
    return stats, types, rate, level1


def load_learnset(name):
    """[(level, MOVE)] from <Name>EvosMoves after the evolution terminator."""
    ls = lines("data/pokemon/evos_moves.asm")
    label = name.capitalize() + "EvosMoves:"
    i = ls.index(label) + 1
    while ls[i] != "db 0":
        i += 1
    out, i = [], i + 1
    while ls[i] != "db 0":
        m = re.match(r"db\s+(\d+),\s*(\w+)$", ls[i])
        if m:
            out.append((int(m.group(1)), m.group(2)))
        i += 1
    return out


def load_move_pp():
    pp = {}
    for c in lines("data/moves/moves.asm"):
        m = re.match(r"move\s+(\w+),.*,\s*(\d+)$", c)
        if m:
            pp[m.group(1)] = int(m.group(2))
    return pp


def exp_for_level(rate_const, n):
    """Mirror of CalcExperience using data/growth_rates.asm."""
    rates = []
    for c in lines("data/growth_rates.asm"):
        m = re.match(r"growth_rate\s+(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+),\s*(-?\d+)$", c)
        if m:
            rates.append(tuple(map(int, m.groups())))
    a, b, c, d, e = rates[GROWTH[rate_const]]
    return (n ** 3 * a) // b + c * n * n + d * n - e


def calc_stat(base, dv, level, is_hp):
    # Gen 1 formula with 0 stat exp: ((base + dv) * 2 + 0) * L / 100 + 5  (HP: + L + 10)
    v = ((base + dv) * 2 * level) // 100
    return v + (level + 10 if is_hp else 5)


CHARMAP = {chr(ord("A") + i): 0x80 + i for i in range(26)}
CHARMAP.update({chr(ord("a") + i): 0xA0 + i for i in range(26)})
CHARMAP.update({str(i): 0xF6 + i for i in range(10)})
CHARMAP.update({" ": 0x7F, "?": 0xE6, "!": 0xE7, "é": 0xBA, "▶": 0xED, ".": 0xE8, "'": 0xE0})
DECODE = {v: k for k, v in CHARMAP.items()}
TERMINATOR = 0x50


def encode(s, length):
    b = [CHARMAP[ch] for ch in s] + [TERMINATOR]
    return (b + [TERMINATOR] * length)[:length]


# populated in main()
TYPE_NAMES, MOVES, GROWTH = {}, {}, {}


# ---------------------------------------------------------------------------
# Emulator driving
# ---------------------------------------------------------------------------

class Game:
    def __init__(self, pyboy, sym):
        self.p, self.s, self.m = pyboy, sym, pyboy.memory
        self.frames = 0

    def tick(self, n=1):
        self.p.tick(n, True)
        self.frames += n

    def press(self, button, wait=20):
        assert button != "b", "never press B: it cancels evolution"
        self.p.button(button, 4)
        self.tick(wait)

    def screen(self):
        """Decode the 20x18 wTileMap buffer into text rows."""
        base = self.s["wTileMap"]
        rows = []
        for y in range(18):
            rows.append("".join(DECODE.get(self.m[base + y * 20 + x], " ") for x in range(20)))
        return rows

    def text(self):
        return "\n".join(self.screen())

    def cursor_row(self):
        for r in self.screen():
            if "▶" in r:
                return r
        return ""

    def select(self, label, limit=12):
        """Move the ▶ cursor down until its row contains `label`, then press A."""
        for _ in range(limit):
            if label in self.cursor_row():
                self.press("a", 40)
                return
            self.press("down", 12)
        raise RuntimeError(f"could not find menu entry {label!r}; screen:\n{self.text()}")

    def select_party(self, name):
        """Party menu entries are two rows (name, then HP bar); the ▶ sits on the
        second row. Press A once the cursor is on `name`'s entry."""
        rows = self.screen()
        name_row = next((k for k, r in enumerate(rows) if name in r), None)
        cur_row = next((k for k, r in enumerate(rows) if "▶" in r), None)
        if name_row is None or cur_row not in (name_row, name_row + 1):
            raise RuntimeError(f"cursor not on {name} (name row {name_row}, cursor row {cur_row}):\n"
                               + self.text())
        self.press("a", 40)

    def wait_for(self, needle, frames=600):
        for _ in range(frames // 10):
            if needle in self.text():
                return True
            self.tick(10)
        return False


def reach_overworld(g, budget=60 * 60 * 3):
    """Title -> NEW GAME -> Oak speech (preset names) -> bedroom with start menu open."""
    g.tick(600)
    i = 0
    while g.frames < budget:
        t = g.text()
        if "ITEM" in t and "SAVE" in t:       # start menu: we're in the overworld
            return
        if "NEW NAME" in t:                    # player / rival name menu -> first preset name
            if "NEW NAME" in g.cursor_row():
                g.press("down", 15)
            assert "NEW NAME" not in g.cursor_row(), "could not move off NEW NAME:\n" + t
            g.press("a", 60)
            continue
        g.press("start" if i % 4 == 3 else "a", 30)
        i += 1
    raise RuntimeError("never reached the overworld start menu")


def inject(g, consts, FROM, level=None, catch_rate=0, bag_item="RARE_CANDY", exp_mode="below_next"):
    """Write party slot 1 = FROM (own OT) and put one bag_item in the bag.
    exp_mode 'below_next': XP one below the next level (Rare Candy cases);
    'at_level': XP exactly at `level` (stone cases)."""
    level = FROM_LEVEL if level is None else level
    s, m = g.s, g.m
    species = consts["species"]
    stats, types, rate, level1 = load_base_stats(FROM)
    learn = load_learnset(FROM)
    pp = load_move_pp()

    # moveset the game would have at FROM_LEVEL: level-1 moves, then learned moves push out the oldest
    moves = list(level1)
    for lvl, mv in learn:
        if lvl <= level and mv not in moves:
            moves = (moves + [mv])[-4:]
    exp37 = exp_for_level(rate, level + 1)
    exp = exp37 - 1 if exp_mode == "below_next" else exp_for_level(rate, level)
    assert exp_for_level(rate, level) <= exp < exp37, "XP outside the level's range"

    dv = 15
    st = {k: calc_stat(v, dv, level, k == "hp") for k, v in stats.items()}
    base = s["wPartyMon1"]
    mon = [0] * (s["wPartyMon2"] - base)

    def put(sym, data):
        off = s[sym] - base
        mon[off:off + len(data)] = data

    put("wPartyMon1Species", [species[FROM]])
    put("wPartyMon1HP", list(st["hp"].to_bytes(2, "big")))
    put("wPartyMon1BoxLevel", [level])
    put("wPartyMon1Status", [0])
    put("wPartyMon1Type1", [TYPE_NAMES[types[0]], TYPE_NAMES[types[1]]])
    put("wPartyMon1CatchRate", [catch_rate])
    put("wPartyMon1Moves", [MOVES[x] for x in moves] + [0] * (4 - len(moves)))
    put("wPartyMon1OTID", [m[s["wPlayerID"]], m[s["wPlayerID"] + 1]])
    put("wPartyMon1Exp", list(exp.to_bytes(3, "big")))
    put("wPartyMon1DVs", [0xFF, 0xFF])
    put("wPartyMon1PP", [pp[x] for x in moves] + [0] * (4 - len(moves)))
    put("wPartyMon1Level", [level])
    put("wPartyMon1MaxHP", list(st["hp"].to_bytes(2, "big")))
    put("wPartyMon1Attack", list(st["atk"].to_bytes(2, "big")))
    put("wPartyMon1Defense", list(st["def"].to_bytes(2, "big")))
    put("wPartyMon1Speed", list(st["spd"].to_bytes(2, "big")))
    put("wPartyMon1Special", list(st["spc"].to_bytes(2, "big")))

    for i, b in enumerate(mon):
        m[base + i] = b
    m[s["wPartyCount"]] = 1
    m[s["wPartySpecies"]] = species[FROM]
    m[s["wPartySpecies"] + 1] = 0xFF
    ot = [m[s["wPlayerName"] + i] for i in range(11)]
    for i, b in enumerate(ot):
        m[s["wPartyMonOT"] + i] = b
    for i, b in enumerate(encode(FROM, 11)):
        m[s["wPartyMonNicks"] + i] = b
    m[s["wNumBagItems"]] = 1
    for i, b in enumerate([consts["items"][bag_item], 1, 0xFF]):
        m[s["wBagItems"] + i] = b
    return {"exp": exp, "exp37": exp37, "rate": rate, "moves": moves, "stats": st}


def run_case(g, consts, FROM, TO, shot_path):
    """Inject FROM at Lv36, use one Rare Candy, press only A, check slot 1 becomes TO."""
    sym, pyboy = g.s, g.p
    from_id, to_id = consts["species"][FROM], consts["species"][TO]
    r = {"from": FROM, "to": TO, "from_id": from_id, "to_id": to_id, "shot": None,
         "lv37": False, "evolving": False, "after": None, "after_list": None, "level": None,
         "ok": False, "error": None}
    print(f"\n--- {FROM} (${from_id:02X}) -> {TO} (${to_id:02X}) ---")

    info = inject(g, consts, FROM)
    g.tick(10)
    r["before"] = g.m[sym["wPartyMon1Species"]]
    print(f"Injected slot 1: species=${r['before']:02X} lv{g.m[sym['wPartyMon1Level']]} "
          f"exp={info['exp']} (Lv37 at {info['exp37']}, {info['rate']}) moves={info['moves']}")

    g.select("ITEM")
    assert g.wait_for("RARE CANDY"), "bag did not show RARE CANDY:\n" + g.text()
    g.select("RARE CANDY")
    assert g.wait_for("USE"), "no USE/TOSS menu:\n" + g.text()
    g.select("USE")
    assert g.wait_for(FROM), "party menu did not show " + FROM + ":\n" + g.text()
    g.select_party(FROM)

    # Advance with A only (level-up text, stats box, evolution text). A is
    # ignored by Evolution_CheckForCancel; only B cancels, and we never press it.
    deadline = g.frames + 60 * 60 * 2
    while g.frames < deadline:
        t = g.text()
        r["lv37"] |= g.m[sym["wPartyMon1Level"]] == FROM_LEVEL + 1
        r["evolving"] |= "is evolving" in t
        if "evolved" in t and "into" in t:
            g.tick(30)  # let "into <NAME>!" finish printing
            if TO not in g.text():
                print(f"evolution text names the wrong species:\n{g.text()}")
            pyboy.screen.image.save(shot_path)
            r["shot"] = shot_path
            break
        g.press("a", 40)

    # After "into X!" the game plays SFX_GET_ITEM_2, WaitForSoundToFinish, waits
    # 40 frames, and only then writes the new species (engine/pokemon/evos_moves.asm).
    # Poll without pressing anything until both species bytes change (10 s cap).
    settle = 0
    for settle in range(0, 600, 10):
        if g.m[sym["wPartyMon1Species"]] == to_id and g.m[sym["wPartySpecies"]] == to_id:
            break
        g.tick(10)
    if g.m[sym["wPartyMon1Species"]] == to_id:
        print(f"species written {settle} frames after the evolution text")
    else:
        print(f"species never changed (polled {settle + 10} frames)")
    for _ in range(4):  # then dismiss any remaining text
        g.press("a", 30)

    r["after"] = g.m[sym["wPartyMon1Species"]]
    r["after_list"] = g.m[sym["wPartySpecies"]]
    r["level"] = g.m[sym["wPartyMon1Level"]]
    r["ok"] = (r["shot"] is not None and r["after"] == to_id and r["after_list"] == to_id
               and r["level"] == FROM_LEVEL + 1)
    print(f"level 37 reached: {r['lv37']}; \"is evolving\" seen: {r['evolving']}; "
          f"screenshot: {r['shot'] or 'NOT TAKEN'}")
    print(f"before: wPartyMon1Species=${r['before']:02X} ({FROM})")
    print(f"after:  wPartyMon1Species=${r['after']:02X}  wPartySpecies[0]=${r['after_list']:02X}  "
          f"level={r['level']}  (expected ${to_id:02X} {TO}, level {FROM_LEVEL + 1})")
    print("PASS" if r["ok"] else "FAIL")
    if not r["ok"]:
        print("final screen:\n" + g.text())
    return r


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", default=os.path.join(ROOT, "pokeyellow.gbc"))
    ap.add_argument("--sym", default=None, help="defaults to <rom>.sym")
    ap.add_argument("--only", metavar="SPECIES",
                    help="run one case, by pre- or post-evolution name (e.g. machoke or MACHAMP)")
    ap.add_argument("--screenshot-dir", default=os.getcwd(),
                    help="where verify_evo_<evolved>.png files go (default: current dir)")
    args = ap.parse_args()

    cases = CASES
    if args.only:
        want = args.only.upper()
        cases = [c for c in CASES if want in c]
        if not cases:
            ap.error(f"--only {args.only}: not one of "
                     + ", ".join(f"{a}/{b}" for a, b in CASES))

    try:
        from pyboy import PyBoy
    except ImportError:
        sys.exit("PyBoy not installed: pip install pyboy pillow")

    sym_path = args.sym or os.path.splitext(args.rom)[0] + ".sym"
    sym = load_sym(sym_path)
    need = ["wTileMap", "wPartyCount", "wPartySpecies", "wPartyMon1", "wPartyMon2",
            "wPartyMon1Species", "wPartyMon1HP", "wPartyMon1BoxLevel", "wPartyMon1Status",
            "wPartyMon1Type1", "wPartyMon1CatchRate", "wPartyMon1Moves", "wPartyMon1OTID",
            "wPartyMon1Exp", "wPartyMon1DVs", "wPartyMon1PP", "wPartyMon1Level",
            "wPartyMon1MaxHP", "wPartyMon1Attack", "wPartyMon1Defense", "wPartyMon1Speed",
            "wPartyMon1Special", "wPartyMonOT", "wPartyMonNicks", "wPlayerName", "wPlayerID",
            "wNumBagItems", "wBagItems"]
    missing = [n for n in need if n not in sym]
    assert not missing, f"symbols missing from {sym_path}: {missing}"

    consts = {"species": load_consts("constants/pokemon_constants.asm"),
              "items": load_consts("constants/item_constants.asm")}
    TYPE_NAMES.update(load_consts("constants/type_constants.asm"))
    MOVES.update(load_consts("constants/move_constants.asm"))
    GROWTH.update({k: v for k, v in load_consts("constants/pokemon_data_constants.asm").items()
                   if k.startswith("GROWTH_")})

    print("RAM addresses (from %s):" % os.path.relpath(sym_path, ROOT))
    for n in ("wPartyCount", "wPartySpecies", "wPartyMon1Species", "wPartyMon1Level",
              "wPartyMon1BoxLevel", "wPartyMon1Exp", "wPartyMonOT", "wPartyMonNicks",
              "wNumBagItems", "wBagItems", "wTileMap"):
        print(f"  {n:<20} ${sym[n]:04X}")
    print("Ids: " + "  ".join(f"{n}=${consts['species'][n]:02X}" for c in cases for n in c)
          + f"  RARE_CANDY=${consts['items']['RARE_CANDY']:02X}")

    os.makedirs(args.screenshot_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="verify_evo_")
    rom = os.path.join(tmp, "rom.gbc")
    shutil.copy(args.rom, rom)  # isolates from <rom>.ram / .state next to the real ROM
    pyboy = PyBoy(rom, window="null", sound_emulated=False)
    g = Game(pyboy, sym)
    results = []
    try:
        reach_overworld(g)
        print(f"Reached overworld start menu after {g.frames} frames")
        # Snapshot the overworld once (in memory) so every case starts identically.
        snapshot = io.BytesIO()
        pyboy.save_state(snapshot)
        for frm, to in cases:
            snapshot.seek(0)
            pyboy.load_state(snapshot)
            shot = os.path.join(args.screenshot_dir, f"verify_evo_{to.lower()}.png")
            try:
                results.append(run_case(g, consts, frm, to, shot))
            except Exception as e:  # a harness failure is a FAIL for that case, keep going
                print(f"FAIL ({type(e).__name__}): {e}")
                results.append({"from": frm, "to": to, "ok": False, "error": f"{type(e).__name__}: {e}",
                                "from_id": consts["species"][frm], "to_id": consts["species"][to],
                                "after": None, "level": None, "shot": None})
    finally:
        pyboy.stop(save=False)
        shutil.rmtree(tmp, ignore_errors=True)

    def hx(v):
        return "--" if v is None else f"${v:02X}"

    print("\n=== Summary ===")
    print(f"  {'case':<22} {'expected':>8} {'got':>5} {'lvl':>4}  result  screenshot")
    for r in results:
        case = f"{r['from']} -> {r['to']}"
        lvl = "--" if r["level"] is None else str(r["level"])
        res = "PASS" if r["ok"] else "FAIL"
        extra = r["shot"] or (r["error"] or "not taken")
        print(f"  {case:<22} {hx(r['to_id']):>8} {hx(r['after']):>5} {lvl:>4}  {res:<6}  {extra}")
    failed = sum(not r["ok"] for r in results)
    print(f"\n{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
