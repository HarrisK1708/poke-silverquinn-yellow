#!/usr/bin/env python3
"""
State-machine auto-grind bot for pokeyellow.gbc (PyBoy).

It wanders tall grass to trigger wild encounters, fights every wild battle with
the first move that has PP, switches in the next healthy party member when the
active one faints, uses a healing item from the bag when the active Pokemon's
HP is low, and keeps going until a stop condition is hit. A summary (battles
fought / won, the lead's XP and level-ups, elapsed time) is printed at the end.

How to use it
  1. Make a save state: run the ROM in PyBoy
         python -m pyboy pokeyellow.gbc
     walk into tall grass with a party, and press Z. PyBoy writes the state to
     pokeyellow.gbc.state next to the ROM.
  2. Watch the bot play:
         python tools/autoplay.py --state pokeyellow.gbc.state --window
     or grind headless at maximum speed:
         python tools/autoplay.py --state pokeyellow.gbc.state --max-battles 50

Speed: --speed 0 = unlimited (default), 1 = real time, 2 = double, ...

Window mode (--window): press M to toggle AUTO / MANUAL. In MANUAL the bot
stops sending input (it only keeps counting battles) and you play with PyBoy's
keys: arrows, A = a, B = s, START = Enter, SELECT = Backspace; emulation drops
to real time so it is playable. Press M again to hand back: the bot re-plans
from wherever you left it (walking back to the last grass tile if needed).
The mode is shown in the window title and the log. (P is PyBoy's own pause
key, which is why the toggle is M. PyBoy's Z/X state keys act on the temp
copy, so use --save-state to keep a state.)

Stop conditions: --max-battles N, --max-minutes M (wall clock), Ctrl-C, the
whole party fainting, a failed Pokemon Center trip, or an unrecoverable stall.
The bot never throws balls; it only runs from battles on the way to a
Pokemon Center.

Policies
  * Moves: the first move slot with PP left that is not disabled, preferring
    damaging moves (base power > 0, from data/moves/moves.asm).
  * PP depletion: if the active Pokemon has no damaging move with PP, it is
    switched (PkMn > SWITCH) for a healthy teammate that has one. If nobody
    has damaging PP, any remaining status-move PP is used up so the game
    falls back to STRUGGLE ("has no moves left!"), which is damaging. Only a
    battle that still runs past --battle-limit seconds of game time stops the
    bot with a report.
  * Healing: when the active Pokemon's HP is at or below --heal-below (fraction
    of max HP, default 0.33) and the bag has a healing item (POTION, SUPER
    POTION, HYPER POTION, MAX POTION, FULL RESTORE, FRESH WATER, SODA POP,
    LEMONADE), it is used on that Pokemon from the battle ITEM menu.
  * Faint: answer YES to "Use next POKeMON?" and send out the first party
    member with HP left.
  * "Delete an older move to make room for X?": NO, then "Abandon learning X?":
    YES (the new move is not learned). --learn-moves is not offered on purpose:
    picking a move to forget needs a policy the bot cannot judge.
  * Evolution: allowed (A only, B is never pressed while "is evolving" is on
    screen); --no-evolve cancels it with B.
  * Pokemon Center (on by default, --no-pc-heal to disable): when no healthy
    party member has a damaging move with PP, or every healthy one is at or
    below --heal-below HP with no healing item in the bag, the bot walks to
    the nearest Pokemon Center, talks to the nurse (HEAL), and walks back to
    the grass tile it was grinding on. Paths are a breadth-first search over
    every map's step grid, built from the sources the ROM is assembled from
    (maps/*.blk + gfx/blocksets/*.bst, collision and tile-pair collision
    lists, ledges, door/warp tiles, warp_events, map connections), with the
    live NPC positions from RAM as obstacles. A blocked step is marked and
    re-planned; wild battles on the way to the Pokemon Center are run from.
    No path, more than --trip-replans re-plans, or more than --trip-limit
    seconds of game time on one leg stops the bot with the reason, so it
    never soft-locks. Each phase is logged ("out of PP -> heading to a
    Pokemon Center", "healed", "back to grinding").
  * Wandering off the grass (or being left off it in MANUAL) for a few steps
    makes the bot path back to the last grass tile it stood on.
  * Every menu state has a timeout; if the screen stops changing the bot backs
    out with B (never during an allowed evolution), and gives up with a report
    after --stall-seconds of emulated time without progress.

Everything is resolved from pokeyellow.sym and the repo's constants (nothing is
hard-coded).

By default NOTHING of yours is changed: the ROM is copied to a temp dir, the
--state file is only read, and the emulator is stopped without saving, so the
grinding is thrown away at exit. To keep it, opt in:
  --save-state OUT    write a PyBoy save state at the end (load it with X)
  --in-game-save --ram-out OUT
                      save with the game's own START > SAVE > YES, then copy
                      the temp copy's battery file to OUT
A current battle is finished first. An existing OUT is copied to OUT.bak
before it is overwritten.

--make-test-state OUT builds a reproducible test state the same way
tools/verify_statics.py places the player: boot a temp copy, play the intro,
inject a party (--test-party) and bag (--test-bag), redirect the bedroom
stairs to ROUTE 1 and land in its tall grass. Then run the bot on OUT.

Requires: pip install pyboy pillow
Usage:    python3 tools/autoplay.py --state FILE [--rom pokeyellow.gbc] [--window | --headless]
                  [--speed 0] [--max-battles N] [--max-minutes M] [--no-evolve]
          python3 tools/autoplay.py --make-test-state OUT [--test-party RATTATA:15,PIDGEY:12]
                  [--test-bag POTION:3]
"""

import argparse
import io
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_evo as ve  # noqa: E402
import verify_statics as vs  # noqa: E402

ROOT = ve.ROOT
HEAL_ITEMS = ["FULL_RESTORE", "MAX_POTION", "HYPER_POTION", "SUPER_POTION", "POTION",
              "LEMONADE", "SODA_POP", "FRESH_WATER"]

USAGE_HELP = """\
Quick start:
  1. In PyBoy (python -m pyboy pokeyellow.gbc), stand in tall grass with a party
     and press Z to make a save state (written to pokeyellow.gbc.state).
  2. Watch it:     python tools/autoplay.py --state pokeyellow.gbc.state --window
     Max speed:    python tools/autoplay.py --state pokeyellow.gbc.state --headless --max-battles 50
  In the window, M toggles AUTO / MANUAL. --speed 1 = real time, 2 = double, 0 = unlimited.
  No state yet? Build a test one:
                   python tools/autoplay.py --make-test-state /tmp/grind.state
"""

NEED_SYMS = ["wCurMap", "wLastMap", "wNumSprites", "wSpriteStateData1", "wSpriteStateData2", "wTileMap", "wIsInBattle", "wXCoord", "wYCoord", "wCurrentMenuItem", "wListScrollOffset",
             "wPartyCount", "wPartySpecies", "wPartyMon1", "wPartyMon2", "wPartyMon1HP", "wPartyMon1MaxHP",
             "wPartyMon1Exp", "wPartyMon1Level", "wPlayerMonNumber", "wBattleMonHP", "wBattleMonMaxHP",
             "wBattleMonMoves", "wBattleMonPP", "wPlayerDisabledMove", "wEnemyMonSpecies", "wEnemyMonLevel",
             "wEnemyMonHP", "wNumBagItems", "wBagItems", "wGrassTile", "wBattleResult"]


def move_power():
    """{move id: base power} from data/moves/moves.asm (ids from constants/move_constants.asm)."""
    ids = ve.load_consts("constants/move_constants.asm")
    power = {}
    for c in ve.lines("data/moves/moves.asm"):
        m = re.match(r"move\s+(\w+),\s*\w+,\s*(\d+),", c)
        if m:
            power[ids[m.group(1)]] = int(m.group(2))
    return power


def u16(m, a):
    return m[a] << 8 | m[a + 1]


def u24(m, a):
    return m[a] << 16 | m[a + 1] << 8 | m[a + 2]


# ---------------------------------------------------------------------------
# World map: collision, ledges, warps and connections for pathfinding
# ---------------------------------------------------------------------------

def _hex(v):
    v = v.strip()
    return int(v[1:], 16) if v.startswith("$") else int(v)


DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
CONN_DIR = {"north": "up", "south": "down", "west": "left", "east": "right"}


class World:
    """Every map's step-tile grid, built from the same sources the ROM is assembled
    from (maps/*.blk, gfx/blocksets/*.bst, data/maps/headers + objects,
    data/tilesets/*): collision (passable tiles), tile-pair collisions, ledges,
    door/warp tiles, warp_events and map connections. Coordinates are the game's
    step coordinates (wXCoord/wYCoord); a step square's collision tile is its
    lower-left 8x8 tile, as in the game's collision check."""

    def __init__(self):
        self.map_ids = ve.load_consts("constants/map_constants.asm")
        self.map_names = {v: k for k, v in self.map_ids.items()}
        self.last_map = self.map_ids.get("LAST_MAP", 0xFF)
        self.size = {}
        for c in ve.lines("constants/map_constants.asm"):
            m = re.match(r"map_const\s+(\w+),\s*(\d+),\s*(\d+)", c)
            if m:
                self.size[self.map_ids[m.group(1)]] = (int(m.group(2)), int(m.group(3)))
        tileset_ids = ve.load_consts("constants/tileset_constants.asm")
        self._load_tilesets(tileset_ids)
        blk_file = self._labels_to_file("maps.asm", r"(\w+)_Blocks:", r'INCBIN\s+"([^"]+)"')
        self.maps = {}
        root = os.path.join(ROOT, "data/maps/headers")
        for fn in sorted(os.listdir(root)):
            info = {"conn": {}, "warps": [], "objects": []}
            for c in ve.lines(f"data/maps/headers/{fn}"):
                m = re.match(r"map_header\s+(\w+),\s*(\w+),\s*(\w+)", c)
                if m:
                    info["label"], const, ts = m.groups()
                    info["id"], info["tileset"] = self.map_ids[const], tileset_ids[ts]
                m = re.match(r"connection\s+(\w+),\s*\w+,\s*(\w+),\s*(-?\d+)", c)
                if m:
                    info["conn"][CONN_DIR[m.group(1)]] = (self.map_ids[m.group(2)], int(m.group(3)))
            if "id" not in info or info["label"] not in blk_file:
                continue
            obj = f"data/maps/objects/{info['label']}.asm"
            if os.path.exists(os.path.join(ROOT, obj)):
                for c in ve.lines(obj):
                    m = re.match(r"warp_event\s+(\d+),\s*(\d+),\s*(\w+),\s*(\d+)", c)
                    if m:
                        dest = self.last_map if m.group(3) == "LAST_MAP" else self.map_ids[m.group(3)]
                        info["warps"].append((int(m.group(1)), int(m.group(2)), dest, int(m.group(4)) - 1))
                    m = re.match(r"object_event\s+(\d+),\s*(\d+),\s*(\w+)", c)
                    if m:
                        info["objects"].append((int(m.group(1)), int(m.group(2)), m.group(3)))
            w, h = self.size[info["id"]]
            with open(os.path.join(ROOT, blk_file[info["label"]]), "rb") as f:
                info["blk"] = f.read()
            info["w"], info["h"] = w, h
            info["warp_at"] = {(x, y): i for i, (x, y, _, _) in enumerate(info["warps"])}
            self.maps[info["id"]] = info
        # Pokemon Centers: maps with a nurse; talk to her across the counter from below
        self.pc_targets = {}
        for mid, info in self.maps.items():
            if self.tilesets[info["tileset"]]["name"] not in ("Pokecenter", "Lobby"):
                continue                 # e.g. SILPH CO 9F's nurse is not a Pokemon Center counter
            for x, y, sprite in info["objects"]:
                if sprite == "SPRITE_NURSE":
                    for k in (1, 2, 3):
                        if self.passable(mid, x, y + k):
                            self.pc_targets[mid] = (x, y + k)
                            break

    # ---- source parsing
    def _labels_to_file(self, rel, label_re, file_re):
        """Map every label of a run of labels to the file INCBIN'd after them."""
        out, pending = {}, []
        for c in ve.lines(rel):
            for lab in re.findall(label_re, c):
                pending.append(lab)
            m = re.search(file_re, c)
            if m and pending:
                for lab in pending:
                    out[lab] = m.group(1)
                pending = []
        return out

    def _label_lists(self, rel, label_re, data_re):
        """Map every label of a run of labels to the byte list of the data macro after them."""
        out, pending = {}, []
        for c in ve.lines(rel):
            m = re.match(label_re, c)
            if m:
                pending.append(m.group(1))
                continue
            m = re.match(data_re, c)
            if m and pending:
                vals = [_hex(v) for v in m.group(1).split(",")] if m.group(1).strip() else []
                for lab in pending:
                    out[lab] = set(vals)
                pending = []
        return out

    def _load_tilesets(self, tileset_ids):
        names = [re.match(r"tileset\s+(\w+),", c).group(1) for c in ve.lines("data/tilesets/tileset_headers.asm")
                 if re.match(r"tileset\s+\w+,", c)]
        bst = self._labels_to_file("gfx/tilesets.asm", r"(\w+)_Block::", r'INCBIN\s+"([^"]+\.bst)"')
        coll = self._label_lists("data/tilesets/collision_tile_ids.asm", r"(\w+)_Coll::", r"coll_tiles\s*(.*)")
        warp_lists = self._label_lists("data/tilesets/warp_tile_ids.asm", r"\.(\w+):", r"warp_tiles\s*(.*)")
        warp_order = [m.group(1) for c in ve.lines("data/tilesets/warp_tile_ids.asm")
                      for m in [re.match(r"dw\s+\.(\w+)", c)] if m]
        door_lists = self._label_lists("data/tilesets/door_tile_ids.asm", r"\.(\w+):", r"door_tiles\s*(.*)")
        door_of = {tileset_ids[m.group(1)]: m.group(2) for c in ve.lines("data/tilesets/door_tile_ids.asm")
                   for m in [re.match(r"dbw\s+(\w+),\s*\.(\w+)", c)] if m}
        self.tilesets = {}
        for i, n in enumerate(names):
            with open(os.path.join(ROOT, bst[n]), "rb") as f:
                blocks = f.read()
            self.tilesets[i] = {"name": n, "bst": blocks, "coll": coll[n],
                                "auto_warp": warp_lists.get(warp_order[i], set()) | door_lists.get(door_of.get(i), set())}
        self.ledges = set()
        for c in ve.lines("data/tilesets/ledge_tiles.asm"):
            m = re.match(r"db\s+SPRITE_FACING_(\w+),\s*(\$\w+),\s*(\$\w+),", c)
            if m:
                self.ledges.add((m.group(1).lower(), _hex(m.group(2)), _hex(m.group(3))))
        self.pairs = set()
        land = False
        for c in ve.lines("data/tilesets/pair_collision_tile_ids.asm"):
            if c.startswith("TilePairCollisionsLand"):
                land = True
            elif c.startswith("TilePairCollisionsWater"):
                land = False
            m = re.match(r"db\s+(\w+),\s*(\$\w+),\s*(\$\w+)", c)
            if land and m:
                t = tileset_ids[m.group(1)]
                a, b = _hex(m.group(2)), _hex(m.group(3))
                self.pairs |= {(t, a, b), (t, b, a)}

    # ---- queries
    def tile(self, mid, x, y):
        info = self.maps[mid]
        w = info["w"]
        block = info["blk"][(y // 2) * w + x // 2]
        return self.tilesets[info["tileset"]]["bst"][block * 16 + ((y % 2) * 2 + 1) * 4 + (x % 2) * 2]

    def inside(self, mid, x, y):
        info = self.maps.get(mid)
        return info is not None and 0 <= x < info["w"] * 2 and 0 <= y < info["h"] * 2

    def passable(self, mid, x, y):
        return self.inside(mid, x, y) and self.tile(mid, x, y) in self.tilesets[self.maps[mid]["tileset"]]["coll"]

    def name(self, mid):
        return self.map_names.get(mid, f"${mid:02X}")

    def exit_dir(self, mid, x, y, fallback):
        """Direction to press on a warp tile that does not warp by itself: outwards at a map edge."""
        info = self.maps[mid]
        if y == info["h"] * 2 - 1:
            return "down"
        if y == 0:
            return "up"
        if x == 0:
            return "left"
        if x == info["w"] * 2 - 1:
            return "right"
        return fallback

    def neighbours(self, st, blocked):
        """(step, next state) for each legal move from st = (map, x, y, last map)."""
        mid, x, y, last = st
        info = self.maps[mid]
        ts = info["tileset"]
        here = self.tile(mid, x, y)
        for d, (dx, dy) in DIRS.items():
            nx, ny, nm, kind = x + dx, y + dy, mid, "walk"
            if not self.inside(mid, nx, ny):
                if d not in info["conn"]:
                    continue
                nm, off = info["conn"][d]
                if nm not in self.maps:
                    continue
                w2, h2 = self.maps[nm]["w"], self.maps[nm]["h"]
                if d in ("up", "down"):
                    nx, ny = x - 2 * off, (h2 * 2 - 1 if d == "up" else 0)
                else:
                    nx, ny = (w2 * 2 - 1 if d == "left" else 0), y - 2 * off
                kind = "conn"
                if not self.passable(nm, nx, ny) or (nm, nx, ny) in blocked:
                    continue
            else:
                front = self.tile(mid, nx, ny)
                if (d, here, front) in self.ledges:
                    nx, ny, kind = nx + dx, ny + dy, "ledge"
                    if not self.passable(mid, nx, ny) or (mid, nx, ny) in blocked:
                        continue
                elif (not self.passable(mid, nx, ny) or (ts, here, front) in self.pairs
                      or (mid, nx, ny) in blocked):
                    continue
            target = (nm, nx, ny)
            nlast = last if nm == mid else mid
            w = self.maps[nm]["warp_at"].get((nx, ny))
            if w is not None:                       # stepping onto a warp tile takes the warp
                _, _, dest, idx = self.maps[nm]["warps"][w]
                dest = last if dest == self.last_map else dest
                if dest not in self.maps or idx >= len(self.maps[dest]["warps"]):
                    continue
                dx_, dy_, _, _ = self.maps[dest]["warps"][idx]
                auto = self.tile(nm, nx, ny) in self.tilesets[self.maps[nm]["tileset"]]["auto_warp"]
                yield ({"dir": d, "target": target, "kind": "warp", "auto": auto,
                        "exit": d if auto else self.exit_dir(nm, nx, ny, d)}, (dest, dx_, dy_, nm))
            else:
                yield {"dir": d, "target": target, "kind": kind}, (nm, nx, ny, nlast)

    def path(self, start, goal, blocked=frozenset(), limit=300000):
        """BFS from start (map, x, y, last) to the first state goal(state) accepts.
        Returns [(step, state after step)], [] if already there, or None."""
        from collections import deque
        if goal(start):
            return []
        prev = {start: None}
        q = deque([start])
        while q and len(prev) < limit:
            st = q.popleft()
            for step, nxt in self.neighbours(st, blocked):
                if nxt in prev:
                    continue
                prev[nxt] = (st, step)
                if goal(nxt):
                    out = []
                    while prev[nxt] is not None:
                        p, s_ = prev[nxt]
                        out.append((s_, nxt))
                        nxt = p
                    return out[::-1]
                q.append(nxt)
        return None


class Bot:
    """Per-frame state machine. step() classifies the current screen/RAM into a
    state, performs at most one button action for it and advances a few frames."""

    def __init__(self, pyboy, sym, consts, args, log=print):
        self.p, self.s, self.m = pyboy, sym, pyboy.memory
        self.g = ve.Game(pyboy, sym)
        self.args, self.log = args, log
        self.species_name = {v: k for k, v in consts["species"].items()}
        self.items = consts["items"]
        self.heal_ids = [self.items[n] for n in HEAL_ITEMS if n in self.items]
        self.power = move_power()
        self.stride = sym["wPartyMon2"] - sym["wPartyMon1"]
        # stats
        self.battles = self.wins = self.losses = self.other = 0
        self.lead_start = self.lead()
        self.lead_levelups = 0
        self.heals = self.switches = self.declined_moves = self.evolutions = self.cancelled_evos = 0
        self.battle_log = []
        self.stop_reason = None
        # state machine bookkeeping
        self.in_battle = False
        self.battle_info = None
        self.state = None
        self.state_frames = 0
        self.last_screen = None
        self.static_frames = 0
        self.progress_frame = 0
        self.wander_dir = "up"
        self.wander_from = None
        self.blocked = 0
        self.heal_pending = False
        self.heal_tried_hp = None
        self.switch_target = None
        self.switch_want = None
        self.struggle_noted = False
        self.struggle_seen = False
        self.struggles = 0
        self.evolving_seen = False
        self.grass_seen = False
        self.start_frame = None
        # Pokemon Center trips
        self.world = World() if args.pc_heal else None
        self.trip = None
        self.last_grass = None
        self.offgrass = 0
        self.heal_trips = 0
        self.runs = 0
        # manual override (window mode)
        self.manual = False
        self.hotkey = None
        self.title_mark = -1
        self.on_tick = None

    # ---------------------------------------------------------------- RAM
    def mon(self, i, field):
        return self.s[f"wPartyMon1{field}"] + i * self.stride

    def party(self):
        m = self.m
        n = m[self.s["wPartyCount"]]
        return [{"species": m[self.s["wPartySpecies"] + i], "hp": u16(m, self.mon(i, "HP")),
                 "maxhp": u16(m, self.mon(i, "MaxHP")), "level": m[self.mon(i, "Level")]} for i in range(n)]

    def lead(self):
        return {"level": self.m[self.s["wPartyMon1Level"]], "exp": u24(self.m, self.s["wPartyMon1Exp"]),
                "species": self.m[self.s["wPartySpecies"]]}

    def bag(self):
        m, s = self.m, self.s
        return [(m[s["wBagItems"] + 2 * i], m[s["wBagItems"] + 2 * i + 1]) for i in range(m[s["wNumBagItems"]])]

    def heal_item(self):
        """(bag index, item id) of the first healing item in the bag, or None."""
        bag = self.bag()
        for want in self.heal_ids:
            for idx, (it, q) in enumerate(bag):
                if it == want and q:
                    return idx, it
        return None

    def item_name(self, item_id):
        for k, v in self.items.items():
            if v == item_id:
                return k.replace("_", " ")
        return f"${item_id:02X}"

    def damaging(self, move_id):
        return self.power.get(move_id, 0) > 0

    def mon_has_damaging_pp(self, i):
        """Party slot i has a damaging move with PP left (the active mon's live
        battle PP is used while in battle)."""
        m, s = self.m, self.s
        if self.in_battle and i == m[s["wPlayerMonNumber"]]:
            mv, pp = s["wBattleMonMoves"], s["wBattleMonPP"]
        else:
            mv, pp = self.mon(i, "Moves"), self.mon(i, "PP")
        return any(m[mv + k] and m[pp + k] & 0x3F and self.damaging(m[mv + k]) for k in range(4))

    def switch_candidate(self):
        """A non-fainted, non-active party slot with damaging PP, or None."""
        active = self.m[self.s["wPlayerMonNumber"]]
        for i, p in enumerate(self.party()):
            if i != active and p["hp"] and self.mon_has_damaging_pp(i):
                return i
        return None

    def usable_move(self):
        """First slot with PP that is not disabled, preferring damaging moves
        (a status move like TAIL WHIP alone would never end the battle)."""
        m, s = self.m, self.s
        disabled = m[s["wPlayerDisabledMove"]] >> 4  # high nibble = 1-based slot
        ok = [i for i in range(4) if m[s["wBattleMonMoves"] + i] and m[s["wBattleMonPP"] + i] & 0x3F
              and disabled != i + 1]
        dmg = [i for i in ok if self.power.get(m[s["wBattleMonMoves"] + i], 0) > 0]
        return (dmg or ok or [None])[0]

    # ---------------------------------------------------------------- screen
    def screen(self):
        return self.g.screen()

    def cursor_pos(self, rows):
        for y, r in enumerate(rows):
            x = r.find("▶")
            if x >= 0:
                return x, y
        return None

    def press(self, button, hold=6, after=10):
        self.p.button(button, hold)
        self.tick(hold + after)

    def tick(self, n):
        if self.args.speed or self.manual:
            for _ in range(n):               # PyBoy's frame limiter runs once per tick() call
                self.p.tick(1, self.args.window)
        else:
            self.p.tick(n, self.args.window)
        self.g.frames += n
        if self.hotkey and self.g.frames // 60 != self.title_mark:   # PyBoy rewrites the title every 60 frames
            self.title_mark = self.g.frames // 60
            self.hotkey.set_title(self.title())

    def title(self):
        mode = "MANUAL (M: resume auto)" if self.manual else "AUTO (M: take over)"
        return f"autoplay [{mode}] battles {self.battles} won {self.wins}"

    def toggle_manual(self):
        self.manual = not self.manual
        if self.manual:
            self.p.set_emulation_speed(1)        # playable speed while you have the controls
            self.log(f"[MANUAL] automation paused at frame {self.g.frames}: you have the controls "
                     "(press M to resume)")
        else:
            self.p.set_emulation_speed(self.args.speed)
            self.log(f"[AUTO] automation resumed at frame {self.g.frames}")
            self.wander_from, self.blocked, self.last_screen = None, 0, None
            self.progress_frame = self.g.frames
            if self.trip:
                self.trip.update(plan=None, start=self.g.frames, fails=0)
        if self.hotkey:
            self.hotkey.set_title(self.title())

    def move_cursor_to_label(self, rows, label):
        """Battle-menu style grids: move ▶ to the cell whose text starts with label.
        Returns True once the cursor is on it."""
        cur = self.cursor_pos(rows)
        if not cur:
            return False
        for y, r in enumerate(rows):
            x = r.find(label)
            if x > 0:
                tx, ty = x - 1, y
                if (cur[0], cur[1]) == (tx, ty):
                    return True
                if cur[1] != ty:
                    self.press("down" if ty > cur[1] else "up")
                else:
                    self.press("right" if tx > cur[0] else "left")
                return False
        return False

    def menu_index_to(self, target, index_fn):
        """Vertical lists (party menu, bag, move list): press up/down until
        index_fn() == target. Returns True when there."""
        cur = index_fn()
        if cur == target:
            return True
        self.press("down" if cur < target else "up")
        return False

    # ---------------------------------------------------------------- classify
    def classify(self, rows):
        t = "\n".join(rows)
        cursor = self.cursor_pos(rows) is not None
        if "is evolving" in t:
            return "EVOLVING"
        if "Delete an older" in t and "YES" in t and cursor:
            return "LEARN_PROMPT"
        if "Abandon learning" in t and "YES" in t and cursor:
            return "ABANDON_PROMPT"
        if "Which move should" in t:
            return "FORGET_MENU"
        if "blacked" in t or "out of" in t and "useable" in t:
            return "BLACKED_OUT"
        if "Use next" in t and "YES" in t and cursor:
            return "USE_NEXT"
        if "Bring out which" in t:
            return "PARTY_SWITCH"
        if "Use item on which" in t:
            return "PARTY_ITEM"
        if self.m[self.s["wIsInBattle"]] and "SWITCH" in t and "STATS" in t and cursor:
            return "PARTY_SUBMENU"
        if self.m[self.s["wIsInBattle"]] and "Choose a" in t:
            return "PARTY_CHOOSE"
        if self.m[self.s["wIsInBattle"]]:
            if "TYPE" in t and cursor:       # move list (the "/" tile is not in the decoder)
                return "MOVE_MENU"
            if "FIGHT" in t and "RUN" in t and cursor:
                return "BATTLE_MENU"
            if "CANCEL" in t and cursor:
                return "BAG"
            if "SWITCH" in t and "STATS" in t and cursor:
                return "PARTY_SUBMENU"
            return "BATTLE_TEXT"
        return "OVERWORLD"

    # ---------------------------------------------------------------- battle bookkeeping
    def begin_battle(self):
        m, s = self.m, self.s
        self.battles += 1
        # wEnemyMon* is filled in a few frames after wIsInBattle is set: read it in end_battle
        self.battle_info = {"n": self.battles, "lead": self.lead(), "frame": self.g.frames}
        self.heal_tried_hp = None
        self.struggle_noted = False
        self.log(f"battle #{self.battles}")

    def end_battle(self):
        m, s = self.m, self.s
        b = self.battle_info or {"n": self.battles, "lead": self.lead()}
        b.update(enemy=self.species_name.get(m[s["wEnemyMonSpecies"]], "?"), level=m[s["wEnemyMonLevel"]])
        enemy_hp = u16(m, s["wEnemyMonHP"])
        alive = any(p["hp"] for p in self.party())
        if enemy_hp == 0 and alive:
            outcome = "won"
            self.wins += 1
        elif not alive:
            outcome = "lost"
            self.losses += 1
        else:
            outcome = "ran away / enemy fled"
            self.other += 1
        after = self.lead()
        b.update(outcome=outcome, after=after)
        self.battle_log.append(b)
        self.log(f"  vs wild {b['enemy']} Lv{b['level']}: {outcome}; "
                 f"lead XP {b['lead']['exp']} -> {after['exp']} "
                 f"(+{after['exp'] - b['lead']['exp']}), Lv{b['lead']['level']} -> Lv{after['level']}")
        self.battle_info = None

    def track_battle(self):
        battle_now = bool(self.m[self.s["wIsInBattle"]])
        if battle_now and not self.in_battle:
            self.in_battle = True
            self.begin_battle()
        elif not battle_now and self.in_battle:
            self.in_battle = False
            self.end_battle()
        self.track_lead()

    def track_lead(self):
        lv = self.m[self.s["wPartyMon1Level"]]
        if lv > self.cur_lead_level:
            self.lead_levelups += lv - self.cur_lead_level
            self.log(f"  lead grew to Lv{lv}")
        self.cur_lead_level = lv

    # ---------------------------------------------------------------- one step
    def step(self):
        m, s = self.m, self.s
        rows = self.screen()
        state = self.classify(rows)

        self.track_battle()

        # timeouts: the same state with an unchanged screen for too long
        key = (state, tuple(rows))
        if key == self.last_screen:
            self.static_frames += 1
        else:
            self.last_screen = key
            self.static_frames = 0
            self.progress_frame = self.g.frames
        if state != self.state:
            self.state, self.state_frames = state, 0
        self.state_frames += 1
        if (self.in_battle and self.battle_info and self.args.battle_limit
                and self.g.frames - self.battle_info["frame"] > self.args.battle_limit * 60):
            self.stop_reason = (f"battle #{self.battles} exceeded --battle-limit {self.args.battle_limit}s "
                                f"of game time (no damaging move left and STRUGGLE never ended it)")
            return
        if self.g.frames - self.progress_frame > self.args.stall_seconds * 60:
            self.stop_reason = f"stalled: screen unchanged for {self.args.stall_seconds}s in state {state}"
            self.log("final screen:\n" + "\n".join(rows))
            return
        menu_timeout = state not in ("OVERWORLD", "BATTLE_TEXT", "EVOLVING") and self.static_frames > 40
        if menu_timeout and self.static_frames % 40 == 0:
            self.log(f"  [timeout in {state}: pressing B]")
            self.press("b")
            return

        handler = getattr(self, "on_" + state.lower())
        before = self.g.frames
        handler(rows)
        if self.g.frames == before:   # every step advances the emulator, so timeouts always run
            self.tick(2)

    # ---------------------------------------------------------------- handlers
    def on_overworld(self, rows):
        m, s = self.m, self.s
        if self.trip:
            self.trip_step(rows)
            return
        if self.world and self.needs_heal():
            self.start_trip()
            return
        pos = (m[s["wXCoord"]], m[s["wYCoord"]])
        if self.wander_from is None:
            self.wander_from = pos
        if self.start_frame is None:
            self.start_frame = self.g.frames
        on_grass = m[s["wTileMap"] + 9 * 20 + 8] == m[s["wGrassTile"]]
        self.grass_seen |= on_grass
        if on_grass:
            self.last_grass, self.offgrass = self.pos(), 0
        elif self.last_grass and self.world and self.offgrass >= 3 and self.last_grass[0] in self.world.maps:
            # wandered (or was walked, in MANUAL) off the grass: path back to the last grass tile
            g = self.last_grass
            self.log(f"off the grass -> walking back to {self.world.name(g[0])} ({g[1]},{g[2]})")
            self.trip = {"phase": "to_grind", "grind": g, "plan": None, "blocked": {}, "fails": 0,
                         "replans": 0, "start": self.g.frames, "warp_presses": 0, "talk": None}
            self.offgrass = 0
            return
        if (not self.grass_seen and not self.battles
                and self.g.frames - self.start_frame > self.args.stall_seconds * 60):
            self.stop_reason = (f"never stood on tall grass in {self.args.stall_seconds}s of wandering: "
                                "make the save state while standing in grass")
            return
        self.p.button(self.wander_dir, 8)
        self.tick(24)
        new = (m[s["wXCoord"]], m[s["wYCoord"]])
        if m[s["wIsInBattle"]]:
            return
        if new != pos:
            self.blocked = 0
            self.progress_frame = self.g.frames
            if m[s["wTileMap"] + 9 * 20 + 8] != m[s["wGrassTile"]]:
                self.offgrass += 1
            # turn round if we left the grass (step back) or went far enough from the start
            grass = m[s["wTileMap"] + 9 * 20 + 8] == m[s["wGrassTile"]]
            dist = abs(new[1] - self.wander_from[1]) + abs(new[0] - self.wander_from[0])
            if not grass or dist >= self.args.wander:
                self.reverse()
            return
        self.blocked += 1
        if self.blocked == 2:          # first press may only turn the player; two misses = blocked
            self.reverse()
        elif self.blocked >= 4:        # a text box (sign, NPC) or a wall both ways: dismiss, go sideways
            self.press("b", after=20)
            self.wander_dir = {"up": "left", "down": "right", "left": "up", "right": "down"}[self.wander_dir]
            self.wander_from = new
            self.blocked = 0

    def reverse(self):
        self.wander_dir = {"up": "down", "down": "up", "left": "right", "right": "left"}[self.wander_dir]

    def on_battle_text(self, rows):
        t = "\n".join(rows)
        if "moves left" in t and not self.struggle_seen:
            self.struggle_seen = True
            self.struggles += 1
            self.log("  no PP left at all: the game uses STRUGGLE")
        elif "moves left" not in t:
            self.struggle_seen = False
        # let text and animations play; press A only when the screen has settled
        if self.static_frames >= 3:
            self.press("a")
        else:
            self.tick(8)

    def on_battle_menu(self, rows):
        m, s = self.m, self.s
        if self.trip and self.trip["phase"] == "to_pc":
            if self.move_cursor_to_label(rows, "RUN"):
                if self.trip.get("ran_in") != self.battles:
                    self.trip["ran_in"] = self.battles
                    self.runs += 1
                    self.log("  on the way to the Pokemon Center: running")
                self.press("a", after=20)
            return
        hp, maxhp = u16(m, s["wBattleMonHP"]), u16(m, s["wBattleMonMaxHP"])
        heal = self.heal_item()
        want_heal = (heal is not None and hp and maxhp and hp <= maxhp * self.args.heal_below
                     and self.heal_tried_hp != hp)
        self.heal_pending = want_heal
        active = m[s["wPlayerMonNumber"]]
        if not want_heal and not self.mon_has_damaging_pp(active):
            cand = self.switch_candidate()
            if cand is not None:
                self.switch_want = cand
                if self.move_cursor_to_pkmn(rows):
                    self.log(f"  {self.mon_name(active)} has no damaging PP left: switching to "
                             f"party slot {cand + 1} ({self.mon_name(cand)})")
                    self.press("a", after=20)
                return
            if not self.struggle_noted:
                self.struggle_noted = True
                self.log("  no party member has damaging PP: using up any PP left so the game falls back "
                         "to STRUGGLE")
        self.switch_want = None
        if self.move_cursor_to_label(rows, "ITEM" if want_heal else "FIGHT"):
            if want_heal:
                self.heal_tried_hp = hp  # one attempt per HP value: no loop if the item cannot be used
                self.log(f"  HP {hp}/{maxhp}: using {self.item_name(heal[1])}")
            self.press("a", after=20)

    def move_cursor_to_pkmn(self, rows):
        """The PkMn cell (its label tiles are not in the text decoder): FIGHT's row,
        RUN's column."""
        f = next(((r.find("FIGHT"), y) for y, r in enumerate(rows) if r.find("FIGHT") > 0), None)
        r_ = next(((r.find("RUN"), y) for y, r in enumerate(rows) if r.find("RUN") > 0), None)
        cur = self.cursor_pos(rows)
        if not (f and r_ and cur):
            return False
        tx, ty = r_[0] - 1, f[1]
        if cur == (tx, ty):
            return True
        if cur[1] != ty:
            self.press("down" if ty > cur[1] else "up")
        else:
            self.press("right" if tx > cur[0] else "left")
        return False

    def mon_name(self, i):
        return self.species_name.get(self.m[self.s["wPartySpecies"] + i], "?")

    def on_move_menu(self, rows):
        if self.heal_pending:
            self.press("b")
            return
        target = self.usable_move()
        if target is None:            # no PP anywhere: the game uses Struggle
            self.press("a", after=20)
            return
        if self.menu_index_to(target, lambda: self.move_cursor_slot(rows)):
            self.press("a", after=20)

    def move_cursor_slot(self, rows):
        """Slot (0-3) under ▶ in the move list, read from the screen: the list is the
        block of consecutive rows with ▶/space then a name; its first row is slot 0.
        (wCurrentMenuItem is offset in this menu, so it is not used.)"""
        cur = self.cursor_pos(rows)
        x, y = cur
        def is_item(r):
            return len(r) > x + 1 and r[x] in "▶ " and (r[x + 1].isalpha() or r[x + 1] == "-")
        top = y
        while top > 0 and is_item(rows[top - 1]):
            top -= 1
        return y - top

    def on_bag(self, rows):
        heal = self.heal_item()
        if not self.heal_pending or heal is None:
            self.heal_pending = False
            self.press("b")
            return
        idx = heal[0]
        if self.menu_index_to(idx, lambda: self.m[self.s["wCurrentMenuItem"]] + self.m[self.s["wListScrollOffset"]]):
            self.press("a", after=20)

    def on_party_item(self, rows):
        target = self.m[self.s["wPlayerMonNumber"]]
        if self.menu_index_to(target, lambda: self.m[self.s["wCurrentMenuItem"]]):
            if self.heal_pending:
                self.heals += 1
                self.heal_pending = False
            self.press("a", after=30)

    def on_party_switch(self, rows):
        party = self.party()
        active = self.m[self.s["wPlayerMonNumber"]]
        target = self.switch_want if self.switch_want is not None and party[self.switch_want]["hp"] else None
        if target is None:
            target = next((i for i, p in enumerate(party)
                           if p["hp"] and i != active and self.mon_has_damaging_pp(i)), None)
        if target is None:
            target = next((i for i, p in enumerate(party) if p["hp"] and i != active), None)
        if target is None:
            target = next((i for i, p in enumerate(party) if p["hp"]), None)
        if target is None:
            self.press("b")
            return
        if self.menu_index_to(target, lambda: self.m[self.s["wCurrentMenuItem"]]):
            if self.switch_target != (self.battles, target):
                self.switch_target = (self.battles, target)
                self.switches += 1
                self.log(f"  sending out party slot {target + 1} "
                         f"({self.species_name.get(party[target]['species'], '?')}, HP {party[target]['hp']})")
            self.switch_want = None
            self.press("a", after=30)

    def on_party_choose(self, rows):
        """Voluntary switch from the battle PkMn menu ("Choose a POKeMON.")."""
        if self.switch_want is None:
            self.press("b")
            return
        self.on_party_switch(rows)

    def on_party_submenu(self, rows):
        if self.move_cursor_to_label(rows, "SWITCH"):
            self.press("a", after=30)

    def on_use_next(self, rows):
        if self.move_cursor_to_label(rows, "YES"):
            self.press("a", after=20)

    def on_learn_prompt(self, rows):
        if self.move_cursor_to_label(rows, "NO"):
            self.press("a", after=20)

    def on_abandon_prompt(self, rows):
        if self.move_cursor_to_label(rows, "YES"):
            self.declined_moves += 1
            self.log("  declined to learn a new move")
            self.press("a", after=20)

    def on_forget_menu(self, rows):
        self.press("b")              # back out to "Abandon learning?"

    def on_evolving(self, rows):
        if not self.evolving_seen:
            self.evolving_seen = True
            self.log("  " + " ".join(r.strip() for r in rows[12:] if r.strip()))
        if self.args.no_evolve:
            self.press("b", hold=4, after=4)
        else:
            self.tick(10)          # A is harmless but not needed; B would cancel

    # ---------------------------------------------------------------- Pokemon Center trips
    def pos(self):
        m, s = self.m, self.s
        return m[s["wCurMap"]], m[s["wXCoord"]], m[s["wYCoord"]]

    def needs_heal(self):
        """No healthy party member has a damaging move with PP, or every healthy one is
        at or below --heal-below HP with no healing item left in the bag."""
        party = self.party()
        alive = [i for i, p in enumerate(party) if p["hp"]]
        if not alive:
            return False                       # blacked out: handled elsewhere
        knows_damaging = any(self.damaging(self.m[self.mon(i, "Moves") + k]) for i in alive for k in range(4))
        if knows_damaging and not any(self.mon_has_damaging_pp(i) for i in alive):
            self.trip_reason = "out of PP"         # (a party with no damaging move at all just STRUGGLEs)
            return True
        if self.heal_item() is None and all(party[i]["hp"] <= party[i]["maxhp"] * self.args.heal_below
                                            for i in alive):
            self.trip_reason = "low HP and no healing items"
            return True
        return False

    def start_trip(self):
        cur = self.pos()
        if cur[0] not in self.world.maps:
            self.stop_reason = f"{self.trip_reason}, and map ${cur[0]:02X} is not in the world model"
            return
        self.heal_trips += 1
        self.trip = {"phase": "to_pc", "grind": cur, "plan": None, "blocked": {}, "fails": 0,
                     "replans": 0, "start": self.g.frames, "warp_presses": 0, "talk": None}
        self.log(f"{self.trip_reason} -> heading to a Pokemon Center (trip #{self.heal_trips}, "
                 f"from {self.world.name(cur[0])} ({cur[1]},{cur[2]}))")

    def trip_fail(self, why):
        cur = self.pos()
        self.stop_reason = (f"Pokemon Center trip failed ({self.trip['phase']}): {why} at "
                            f"{self.world.name(cur[0])} ({cur[1]},{cur[2]})")

    def sprite_obstacles(self):
        """Map tiles occupied by the current map's NPC sprites (live RAM positions;
        Yellow's following Pikachu, the last sprite slot, is not an obstacle)."""
        m, s = self.m, self.s
        mid = m[s["wCurMap"]]
        n = m[s["wNumSprites"]]
        out = set()
        for i in range(1, min(n, 14) + 1):
            if m[s["wSpriteStateData1"] + i * 16] == 0:
                continue
            y, x = m[s["wSpriteStateData2"] + i * 16 + 4] - 4, m[s["wSpriteStateData2"] + i * 16 + 5] - 4
            out.add((mid, x, y))
        return out

    def trip_goal(self):
        if self.trip["phase"] == "to_pc":
            targets = self.world.pc_targets
            return lambda st: targets.get(st[0]) == (st[1], st[2])
        g = self.trip["grind"]
        return lambda st: st[:3] == g

    def replan(self):
        t = self.trip
        t["replans"] += 1
        if t["replans"] > self.args.trip_replans:
            self.trip_fail(f"gave up after {self.args.trip_replans} re-plans")
            return None
        m, s = self.m, self.s
        start = self.pos() + (m[s["wLastMap"]],)
        now = self.g.frames
        t["blocked"] = {k: v for k, v in t["blocked"].items() if v > now}
        blocked = set(t["blocked"]) | self.sprite_obstacles()
        plan = self.world.path(start, self.trip_goal(), frozenset(blocked))
        if plan is None and blocked:
            plan = self.world.path(start, self.trip_goal())       # NPCs may move out of the way
        if plan is None:
            self.trip_fail("no walkable path " + ("to any Pokemon Center" if t["phase"] == "to_pc"
                                                  else "back to the grind spot"))
        t["plan"] = plan
        return plan

    def trip_step(self, rows):
        t = self.trip
        m, s = self.m, self.s
        if self.g.frames - t["start"] > self.args.trip_limit * 60:
            self.trip_fail(f"not done after --trip-limit {self.args.trip_limit}s of game time")
            return
        if t["phase"] == "talk":
            self.nurse_step(rows)
            return
        if t["plan"] is None and self.replan() is None:
            return
        plan = t["plan"]
        cur = self.pos()
        if not plan:                                 # arrived
            if t["phase"] == "to_pc":
                self.log(f"  at the nurse in {self.world.name(cur[0])}: talking")
                t.update(phase="talk", talk={"start": self.g.frames, "farewell": False, "turned": False,
                                             "presses": 0})
            else:
                self.log(f"back to grinding at {self.world.name(cur[0])} ({cur[1]},{cur[2]})")
                self.trip = None
                self.wander_from, self.blocked = None, 0
            return
        step, nxt = plan[0]
        if step["kind"] == "warp" and cur == step["target"]:
            # standing on a warp that needs a push towards the map edge
            t["warp_presses"] += 1
            if t["warp_presses"] > 4:
                t["blocked"][step["target"]] = self.g.frames + 600
                t["warp_presses"] = 0
                t["plan"] = None
                return
            self.p.button(step["exit"], 10)
            self.tick(30)
            self.after_move(cur, step, nxt)
            return
        self.p.button(step["dir"], 8)
        self.tick(40 if step["kind"] == "ledge" else 24)
        self.after_move(cur, step, nxt)

    def after_move(self, cur, step, nxt):
        t = self.trip
        if self.m[self.s["wIsInBattle"]]:
            t["plan"] = None                         # re-plan after the battle
            return
        new = self.pos()
        if new[0] != cur[0]:
            self.tick(90)                            # let the warp / map transition settle
            new = self.pos()
        if new == nxt[:3]:
            t["plan"].pop(0)
            t["fails"] = t["warp_presses"] = 0
            self.progress_frame = self.g.frames
            if step["kind"] == "warp":
                self.log(f"  entered {self.world.name(new[0])}")
                t["plan"] = None                     # re-plan inside: positions after a warp can differ
            elif step["kind"] == "conn":
                self.log(f"  crossed into {self.world.name(new[0])}")
            return
        if step["kind"] == "warp" and new == step["target"]:
            self.progress_frame = self.g.frames      # on the warp tile; push through next step
            return
        if new == cur:
            t["fails"] += 1
            if t["fails"] >= 2:                      # first press may only turn the player
                self.press("b", after=20)            # in case a text box is open
                t["blocked"][step["target"]] = self.g.frames + 600
                t["fails"] = 0
                t["plan"] = None
            return
        t["plan"] = None                             # somewhere unexpected (auto-step out of a door, ...)
        self.progress_frame = self.g.frames

    def nurse_step(self, rows):
        tk = self.trip["talk"]
        text = "\n".join(rows)
        if self.g.frames - tk["start"] > 90 * 60:
            self.trip_fail("the nurse dialogue did not finish")
            return
        if not tk["turned"]:
            self.p.button("up", 4)                   # face the nurse across the counter
            self.tick(20)
            tk["turned"] = True
            return
        if "again" in text and "see" in text:
            tk["farewell"] = True
        cursor = self.cursor_pos(rows)
        if cursor and ("HEAL" in text or "YES" in text):
            if self.move_cursor_to_label(rows, "HEAL" if "HEAL" in text else "YES"):
                self.press("a", after=30)
            return
        talking = any(ch.isalpha() for r in rows[12:] for ch in r)
        if tk["farewell"] and not talking:
            party = self.party()
            healed = all(p["hp"] == p["maxhp"] for p in party) and (
                self.trip_reason != "out of PP" or any(self.mon_has_damaging_pp(i) for i in range(len(party))))
            if not healed:
                self.trip_fail("talked to the nurse but the party was not healed")
                return
            self.log("  healed: " + ", ".join(f"{self.mon_name(i)} {p['hp']}/{p['maxhp']} HP"
                                              for i, p in enumerate(party)) + "; PP restored")
            if self.needs_heal():
                self.stop_reason = "healed, but the party still cannot fight (no damaging moves at all?)"
                return
            self.trip.update(phase="to_grind", plan=None, start=self.g.frames, replans=0)
            g = self.trip["grind"]
            self.log(f"  heading back to {self.world.name(g[0])} ({g[1]},{g[2]})")
            return
        if not talking and not tk["farewell"]:
            if tk["presses"] >= 6:
                self.trip_fail("the nurse does not answer (not facing her?)")
                return
            tk["presses"] += 1
            self.press("a", after=40)                # start (or continue) the conversation
            return
        if tk["farewell"]:
            if self.static_frames >= 3:
                self.press("b", after=20)            # close the last box; A would talk to her again
            else:
                self.tick(8)
        elif self.static_frames >= 3:
            self.press("a", after=10)
        else:
            self.tick(8)

    def on_blacked_out(self, rows):
        if self.in_battle:
            self.in_battle = False
            self.end_battle()
        self.stop_reason = "the whole party fainted (blacked out)"

    # ---------------------------------------------------------------- main loop
    def run(self):
        self.cur_lead_level = self.lead_start["level"]
        t0 = time.time()
        f0 = self.g.frames
        try:
            while self.stop_reason is None:
                if self.args.max_battles and self.battles >= self.args.max_battles and not self.in_battle:
                    self.stop_reason = f"reached --max-battles {self.args.max_battles}"
                    break
                if self.args.max_minutes and time.time() - t0 >= self.args.max_minutes * 60:
                    self.stop_reason = f"reached --max-minutes {self.args.max_minutes}"
                    break
                if self.on_tick:
                    self.on_tick(self)
                if self.hotkey and self.hotkey.poll():
                    self.toggle_manual()
                if self.manual:                       # the player is driving: only keep the books
                    self.tick(1)
                    self.track_battle()
                    self.progress_frame = self.g.frames
                    continue
                self.step()
                # evolution bookkeeping from the game's own result text
                if self.evolving_seen:
                    t = self.g.text()
                    if "evolved" in t and "into" in t:
                        self.evolving_seen = False
                        self.evolutions += 1
                        self.log("  " + " ".join(r.strip() for r in self.screen()[12:] if r.strip()))
                    elif "stopped" in t and "evolving" in t:
                        self.evolving_seen = False
                        self.cancelled_evos += 1
                        self.log("  evolution cancelled (--no-evolve)")
                if self.party() and not any(p["hp"] for p in self.party()) and not self.in_battle:
                    self.stop_reason = "no usable Pokemon left"
        except KeyboardInterrupt:
            self.stop_reason = "interrupted (Ctrl-C)"
        if self.in_battle and (self.args.save_state or self.args.in_game_save) and self.stop_reason.startswith(
                ("reached", "interrupted")):
            self.finish_battle()
        if self.in_battle and self.battle_info:
            self.battle_info.update(outcome="unfinished", after=self.lead())
        self.elapsed = time.time() - t0
        self.frames_run = self.g.frames - f0

    def finish_battle(self, limit_seconds=300):
        """Keep fighting until the current battle is over, so a save lands in the overworld."""
        self.log("  finishing the current battle before saving")
        reason, self.stop_reason = self.stop_reason, None
        end = self.g.frames + limit_seconds * 60
        try:
            while self.in_battle and self.stop_reason is None and self.g.frames < end:
                self.step()
        except KeyboardInterrupt:
            pass
        self.stop_reason = self.stop_reason or reason

    def to_overworld(self, tries=20):
        """Dismiss leftover text until the START menu opens; leaves it open. True on success."""
        for _ in range(tries):
            if self.m[self.s["wIsInBattle"]]:
                return False
            self.press("start", after=40)
            if self.g.wait_for("SAVE", 60) and "ITEM" in self.g.text():
                return True
            self.press("b", after=30)
            self.press("b", after=30)
        return False

    def in_game_save(self):
        """START > SAVE > YES (and YES to overwrite an older file); True once the game says it saved."""
        self.log("in-game save: START > SAVE > YES")
        if not self.to_overworld():
            self.log("  could not reach the START menu (still in a battle or a cutscene): not saved")
            return False
        try:
            self.g.select("SAVE")
        except RuntimeError as e:
            self.log(f"  {e}")
            return False
        for _ in range(120):
            t = self.g.text()
            if "saved" in t:
                self.tick(180)
                for _ in range(3):
                    self.press("b", after=30)
                self.log("  game saved")
                return True
            if "YES" in t and self.cursor_pos(self.screen()):
                if self.move_cursor_to_label(self.screen(), "YES"):
                    self.press("a", after=60)
                continue
            self.tick(10)
        self.log("  the game never confirmed the save")
        return False

    def summary(self):
        end = self.lead()
        lines = ["", "=== autoplay summary ===",
                 f"  stop reason        {self.stop_reason}",
                 f"  battles fought     {self.battles}",
                 f"  battles won        {self.wins}",
                 f"  battles lost       {self.losses}",
                 f"  other endings      {self.other}",
                 f"  lead               {self.species_name.get(self.lead_start['species'], '?')} -> "
                 f"{self.species_name.get(end['species'], '?')}",
                 f"  lead level         {self.lead_start['level']} -> {end['level']} "
                 f"({self.lead_levelups} level-up{'s' if self.lead_levelups != 1 else ''})",
                 f"  lead XP            {self.lead_start['exp']} -> {end['exp']} (+{end['exp'] - self.lead_start['exp']})",
                 f"  heals used         {self.heals}",
                 f"  switches           {self.switches}",
                 f"  Pokemon Center     {self.heal_trips} trip(s), ran from {self.runs} battle(s) on the way",
                 f"  STRUGGLE uses      {self.struggles}",
                 f"  moves declined     {self.declined_moves}",
                 f"  evolutions         {self.evolutions} (cancelled {self.cancelled_evos})",
                 f"  party HP           " + ", ".join(f"{self.species_name.get(p['species'], '?')} Lv{p['level']} "
                                                    f"{p['hp']}/{p['maxhp']}" for p in self.party()),
                 f"  elapsed            {self.elapsed:.1f}s wall clock, {self.frames_run} frames "
                 f"({self.frames_run / 60:.0f}s game time, {self.frames_run / 60 / max(self.elapsed, 1e-9):.1f}x)"]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# test state (same placement harness as tools/verify_statics.py)
# ---------------------------------------------------------------------------

class Hotkey:
    """Counts presses of one key in the PyBoy SDL2 window. PyBoy drains the SDL event
    queue itself, so this uses an SDL event watch, which sees every event as it is
    queued (real key presses and SDL_PushEvent alike) without consuming it."""

    def __init__(self, key="m"):
        import sdl2
        self.sdl2, self.presses = sdl2, 0
        self.key = sdl2.SDL_GetKeyFromName(key.encode())

        def watch(_userdata, event):
            e = event.contents
            if e.type == sdl2.SDL_KEYDOWN and e.key.keysym.sym == self.key and not e.key.repeat:
                self.presses += 1
            return 0
        self._watch = sdl2.SDL_EventFilter(watch)       # keep a reference: SDL holds a raw pointer
        sdl2.SDL_AddEventWatch(self._watch, None)

    def poll(self):
        """True if the key was pressed an odd number of times since the last poll."""
        n, self.presses = self.presses, 0
        return n % 2 == 1

    def set_title(self, text):
        win = self.sdl2.SDL_GetWindowFromID(1)
        if win:
            self.sdl2.SDL_SetWindowTitle(win, text.encode())


def load_tables():
    consts = {"species": ve.load_consts("constants/pokemon_constants.asm"),
              "items": ve.load_consts("constants/item_constants.asm")}
    ve.TYPE_NAMES.update(ve.load_consts("constants/type_constants.asm"))
    ve.MOVES.update(ve.load_consts("constants/move_constants.asm"))
    ve.GROWTH.update({k: v for k, v in ve.load_consts("constants/pokemon_data_constants.asm").items()
                      if k.startswith("GROWTH_")})
    return consts


def parse_party(spec):
    """'RATTATA:15,PIDGEY:12:50:0/0/5/0' -> [(species, level, hp percent, pp list or None)]
    PP is one number for every move or four slash-separated values."""
    out = []
    for part in spec.split(","):
        f = part.strip().upper().split(":")
        pp = None
        if len(f) > 3:
            pp = [int(x) for x in f[3].split("/")]
            pp = pp * 4 if len(pp) == 1 else pp
        out.append((f[0], int(f[1]), int(f[2]) if len(f) > 2 else 100, pp))
    return out


def grass_spot(map_const, blk_file, grass_tile, blockset="gfx/blocksets/overworld.bst"):
    """A grass step tile with grass directly above and below (lower-left tile of each 2x2 step)."""
    w = ve.map_width(map_const)
    blk = open(os.path.join(ROOT, blk_file), "rb").read()
    bst = open(os.path.join(ROOT, blockset), "rb").read()
    h = len(blk) // w

    def tile(x, y):
        return bst[blk[(y // 2) * w + x // 2] * 16 + ((y % 2) * 2 + 1) * 4 + (x % 2) * 2]

    for y in range(1, h * 2 - 1):
        for x in range(w * 2):
            if all(tile(x, y + d) == grass_tile for d in (-1, 0, 1)):
                return x, y, w
    raise ValueError(f"no grass on {map_const}")


def make_test_state(args, sym, consts):
    from pyboy import PyBoy
    party = parse_party(args.test_party)
    bag = [(f.split(":")[0].upper(), int(f.split(":")[1])) for f in args.test_bag.split(",") if f]
    maps = ve.load_consts("constants/map_constants.asm")
    map_id = maps["ROUTE_1"]
    grass_tile = int(next(l for l in ve.lines("data/tilesets/tileset_headers.asm")
                          if l.startswith("tileset Overworld")).split(",")[4].strip().lstrip("$"), 16)
    sx, sy, w = grass_spot("ROUTE_1", "maps/Route1.blk", grass_tile)
    if args.test_pos:
        sx, sy = (int(v) for v in args.test_pos.split(","))
    view_ptr = sym["wOverworldMap"] + 7 + w + (w + 6) * (sy >> 1) + (sx >> 1)

    rom_bytes = open(args.rom, "rb").read()
    entry, cd = sym["LoadDestinationWarpPosition"], sym["CopyData"]
    at = rom_bytes.find(bytes([0xCD, cd & 0xFF, cd >> 8]), entry, entry + 0x40)
    assert at > 0 and rom_bytes[at + 3] == 0xF1, "could not locate `call CopyData / pop af`"
    hook_addr = at + 3

    tmp = tempfile.mkdtemp(prefix="autoplay_mkstate_")
    rom = os.path.join(tmp, "rom.gbc")
    shutil.copy(args.rom, rom)
    p = PyBoy(rom, window="null", sound_emulated=False)
    g = ve.Game(p, sym)
    s, m = sym, g.m
    hook = {"armed": False, "fired": 0}

    def on_warp_position(_):
        if hook["armed"] and m[s["wCurMap"]] == map_id:
            m[s["wCurrentTileBlockMapViewPointer"]] = view_ptr & 0xFF
            m[s["wCurrentTileBlockMapViewPointer"] + 1] = view_ptr >> 8
            m[s["wYCoord"]], m[s["wXCoord"]] = sy, sx
            hook["fired"] += 1
    p.hook_register(0, hook_addr, on_warp_position, None)
    try:
        ve.reach_overworld(g)
        p.button("b", 4)
        g.tick(60)
        stride = s["wPartyMon2"] - s["wPartyMon1"]
        structs, nicks = [], []
        for sp, lv, _, _ in party:
            ve.inject(g, consts, sp, level=lv, exp_mode="below_next")   # XP one below the next level
            structs.append([m[s["wPartyMon1"] + i] for i in range(stride)])
            nicks.append([m[s["wPartyMonNicks"] + i] for i in range(11)])
        for k, ((sp, lv, pct, pp), st, nk) in enumerate(zip(party, structs, nicks)):
            for i, b in enumerate(st):
                m[s["wPartyMon1"] + k * stride + i] = b
            hp = max(1, u16(m, s["wPartyMon1MaxHP"] + k * stride) * pct // 100) if pct else 0
            m[s["wPartyMon1HP"] + k * stride], m[s["wPartyMon1HP"] + k * stride + 1] = hp >> 8, hp & 0xFF
            for j, v in enumerate(pp or []):
                if m[s["wPartyMon1Moves"] + k * stride + j]:
                    m[s["wPartyMon1PP"] + k * stride + j] = v
            for i, b in enumerate(nk):
                m[s["wPartyMonNicks"] + k * 11 + i] = b
            for i in range(11):
                m[s["wPartyMonOT"] + k * 11 + i] = m[s["wPlayerName"] + i]
            m[s["wPartySpecies"] + k] = consts["species"][sp]
        m[s["wPartyCount"]] = len(party)
        m[s["wPartySpecies"] + len(party)] = 0xFF
        m[s["wNumBagItems"]] = len(bag)
        for i, (it, q) in enumerate(bag):
            m[s["wBagItems"] + 2 * i], m[s["wBagItems"] + 2 * i + 1] = consts["items"][it], q
        m[s["wBagItems"] + 2 * len(bag)] = 0xFF

        wy, wx = m[s["wWarpEntries"]], m[s["wWarpEntries"] + 1]
        m[s["wWarpEntries"] + 2] = 0
        m[s["wWarpEntries"] + 3] = map_id
        hook["armed"] = True
        vs.walk_to(g, wx, wy + 1)
        p.button("up", 8)
        for _ in range(60):
            g.tick(10)
            if m[s["wCurMap"]] == map_id and hook["fired"]:
                break
        g.tick(120)
        hook["armed"] = False
        assert m[s["wCurMap"]] == map_id and hook["fired"] == 1, "warp to ROUTE_1 failed"
        assert (m[s["wXCoord"]], m[s["wYCoord"]]) == (sx, sy), "player not on the grass tile"
        assert m[s["wTileMap"] + 9 * 20 + 8] == m[s["wGrassTile"]], "tile under the player is not grass"
        with open(args.make_test_state, "wb") as f:
            p.save_state(f)
        shot = os.path.splitext(args.make_test_state)[0] + ".png"
        p.screen.image.save(shot)
        print(f"test state written to {args.make_test_state} (screenshot {shot})")
        print(f"  ROUTE_1 grass at ({sx},{sy}); party: "
              + ", ".join(f"{sp} Lv{lv} {pct}% HP" + (f" PP {'/'.join(map(str, pp))}" if pp else "")
                          for sp, lv, pct, pp in party)
              + "; bag: " + (", ".join(f"{it} x{q}" for it, q in bag) or "empty"))
    finally:
        p.stop(save=False)
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------

def backup(path):
    if os.path.exists(path):
        shutil.copy(path, path + ".bak")
        print(f"(existing {path} copied to {path}.bak)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, epilog=USAGE_HELP,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", default=os.path.join(ROOT, "pokeyellow.gbc"))
    ap.add_argument("--sym", default=None, help="defaults to <rom>.sym")
    ap.add_argument("--state", help="PyBoy save state to start from (party in hand, standing in/near grass)")
    ap.add_argument("--speed", type=int, default=0,
                    help="emulation speed: 0 = unlimited (default), 1 = real time, 2 = double, ...")
    vis = ap.add_mutually_exclusive_group()
    vis.add_argument("--window", action="store_true", help="show an SDL2 window")
    vis.add_argument("--headless", action="store_true", help="no window (default)")
    ap.add_argument("--max-battles", type=int, default=0, help="stop after N battles")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop after M wall-clock minutes")
    ap.add_argument("--no-evolve", action="store_true", help="cancel evolutions with B")
    ap.add_argument("--heal-below", type=float, default=0.33,
                    help="use a healing item when HP <= this fraction of max (default 0.33)")
    ap.add_argument("--wander", type=int, default=2, help="tiles to walk before turning round (default 2)")
    ap.add_argument("--stall-seconds", type=int, default=120,
                    help="give up after this much emulated time without screen progress (default 120)")
    ap.add_argument("--battle-limit", type=int, default=600,
                    help="stop if one battle lasts longer than this many seconds of game time (default 600)")
    ap.add_argument("--no-pc-heal", dest="pc_heal", action="store_false",
                    help="do not walk to a Pokemon Center when out of PP / HP (default: do)")
    ap.add_argument("--trip-limit", type=int, default=900,
                    help="give up a Pokemon Center trip leg after this many seconds of game time (default 900)")
    ap.add_argument("--trip-replans", type=int, default=80,
                    help="give up a trip leg after this many re-plans (blocked steps, battles; default 80)")
    ap.add_argument("--screenshot", help="save the final screen to this PNG")
    ap.add_argument("--save-state", metavar="OUT",
                    help="at the end, write a PyBoy save state to OUT (load it in PyBoy with X to keep the "
                         "levelled team); an existing OUT is first copied to OUT.bak")
    ap.add_argument("--in-game-save", action="store_true",
                    help="before stopping, save with the game's own START > SAVE > YES and write the "
                         "resulting battery file to --ram-out")
    ap.add_argument("--ram-out", metavar="OUT",
                    help="where --in-game-save writes the battery (.ram) file, e.g. pokeyellow.gbc.ram "
                         "to load it with the ROM; an existing OUT is first copied to OUT.bak")
    ap.add_argument("--make-test-state", metavar="OUT", help="build a ROUTE 1 grass test state and exit")
    ap.add_argument("--test-party", default="RATTATA:15,PIDGEY:12",
                    help="SPECIES:LEVEL[:HP%%[:PP]],... for --make-test-state (XP one below the next "
                         "level; PP = one value for every move or four slash-separated, e.g. 0/0/2/0)")
    ap.add_argument("--test-bag", default="POTION:3", help="ITEM:QTY,... for --make-test-state")
    ap.add_argument("--test-pos", metavar="X,Y", help="ROUTE 1 grass tile for --make-test-state "
                    "(default: the first grass tile from the north)")
    args = ap.parse_args()
    if args.in_game_save and not args.ram_out:
        ap.error("--in-game-save needs --ram-out OUT (the battery file to write; nothing is written by default)")
    if args.ram_out and not args.in_game_save:
        ap.error("--ram-out only makes sense with --in-game-save")

    try:
        from pyboy import PyBoy
    except ImportError:
        sys.exit("PyBoy not installed: pip install pyboy pillow")
    sym_path = args.sym or os.path.splitext(args.rom)[0] + ".sym"
    sym = ve.load_sym(sym_path)
    missing = [n for n in NEED_SYMS if n not in sym]
    assert not missing, f"symbols missing from {sym_path}: {missing}"
    consts = load_tables()

    if args.make_test_state:
        make_test_state(args, sym, consts)
        return 0
    if not args.state:
        ap.print_usage()
        print("\nerror: --state is required (or use --make-test-state)\n\n" + USAGE_HELP)
        return 2

    tmp = tempfile.mkdtemp(prefix="autoplay_")
    rom = os.path.join(tmp, "rom.gbc")
    shutil.copy(args.rom, rom)   # the repo's pokeyellow.gbc.ram/.state are never written
    pyboy = PyBoy(rom, window="SDL2" if args.window else "null", sound_emulated=args.window)
    pyboy.set_emulation_speed(args.speed)
    try:
        with open(args.state, "rb") as f:
            pyboy.load_state(io.BytesIO(f.read()))
        pyboy.tick(2, args.window)
        bot = Bot(pyboy, sym, consts, args, log=lambda x: print(x, flush=True))
        if args.window:
            bot.hotkey = Hotkey("m")
            bot.hotkey.set_title(bot.title())
            print("window: press M to toggle AUTO / MANUAL (in MANUAL you play with PyBoy's keys: arrows, "
                  "A = a, B = s, START = Enter; speed drops to real time)", flush=True)
        if not bot.party():
            sys.exit("the state has no party: catch or inject a Pokemon first")
        print(f"autoplay: {'window' if args.window else 'headless'}, speed {args.speed or 'unlimited'}, "
              f"party " + ", ".join(f"{bot.species_name.get(p['species'], '?')} Lv{p['level']}"
                                    for p in bot.party()), flush=True)
        bot.run()
        saved = bot.in_game_save() if args.in_game_save else False
        if args.save_state:
            backup(args.save_state)
            with open(args.save_state, "wb") as f:
                pyboy.save_state(f)
            print(f"save state written to {args.save_state}")
        if args.screenshot:
            pyboy.screen.image.save(args.screenshot)
        print(bot.summary())
    finally:
        pyboy.stop(save=bool(args.in_game_save))   # writes the temp copy's battery file only
        if args.in_game_save:
            ram = rom + ".ram"
            if saved and os.path.exists(ram):
                backup(args.ram_out)
                shutil.copy(ram, args.ram_out)
                print(f"in-game save written to {args.ram_out} (load it next to the ROM as <rom>.ram)")
            else:
                print("in-game save FAILED: nothing written to " + args.ram_out)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if bot.stop_reason and not bot.stop_reason.startswith("stalled") else 1


if __name__ == "__main__":
    sys.exit(main())
