#!/usr/bin/env python3
"""
Fault-injection test for the starter-Pikachu stone block (emulator; needs PyBoy).

A temp copy of the repo (tools/faultlib.py; the real files are never
modified) has the $A3 (LIGHT_BALL_GSC) catch-rate check removed from both
routines in engine/pikachu/pikachu_status.asm -- the `cp LIGHT_BALL_GSC` and
the `jr nz` after it in IsThisMonStarterPikachu and in the follower scan
IsStarterPikachuAliveInOurParty -- i.e. the pre-step-2b predicate. The copy is
rebuilt and verify_pikachu_block's own harness (run_rom / run_case from the
copy) is driven on that ROM:

  A  follower replica (catch rate $A3, own OT)            must still be REFUSED
  B  wild-style Pikachu (base catch rate, own OT)          must now be REFUSED too

This is the source-level form of verify_pikachu_block's Case C (which patches
the same check out of the ROM bytes): it proves the catch-rate byte is the only
thing that lets a wild Pikachu evolve.

Usage: python3 tools/test_pikachu_block_faults.py
Exit status: 0 if both cases behave as expected, 1 otherwise.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from faultlib import RepoCopy, Results, need_pyboy  # noqa: E402

PS = "engine/pikachu/pikachu_status.asm"


def main():
    need_pyboy()
    from pyboy import PyBoy
    r = Results("starter-Pikachu block fault test")
    with RepoCopy("pikachu") as c:
        # delete `cp LIGHT_BALL_GSC` and the `jr nz` that followed it, in both routines
        lines = c.read(PS).split("\n")
        code = [l.split(";", 1)[0].strip() for l in lines]
        drop = []
        for label in ("IsThisPartyMonStarterPikachu::", "IsStarterPikachuAliveInOurParty::"):
            i = code.index("cp LIGHT_BALL_GSC", code.index(label))
            assert code[i + 1].startswith("jr nz,"), f"{PS}:{i + 2}: expected jr nz after cp LIGHT_BALL_GSC"
            drop += [i, i + 1]
        print(f"removed {PS} lines {', '.join(str(i + 1) for i in sorted(drop))}: "
              + "; ".join(code[i] for i in sorted(drop)))
        c.write(PS, "\n".join(l for i, l in enumerate(lines) if i not in drop))
        rom = c.build()

        sys.path.insert(0, c.path("tools"))
        import verify_pikachu_block as vp  # the copy's harness, ROOT = the copy
        ve = vp.ve
        assert ve.ROOT == c.dir
        sym = ve.load_sym(c.path("pokeyellow.sym"))
        consts = {"species": ve.load_consts("constants/pokemon_constants.asm"),
                  "items": ve.load_consts("constants/item_constants.asm")}
        ve.TYPE_NAMES.update(ve.load_consts("constants/type_constants.asm"))
        ve.MOVES.update(ve.load_consts("constants/move_constants.asm"))
        ve.GROWTH.update({k: v for k, v in ve.load_consts("constants/pokemon_data_constants.asm").items()
                          if k.startswith("GROWTH_")})
        lb = ve.def_value("constants/item_constants.asm", "LIGHT_BALL_GSC")
        base = ve.base_catch_rate("PIKACHU")
        shots = c.path("_shots")
        os.makedirs(shots, exist_ok=True)
        with open(rom, "rb") as f:
            rom_bytes = f.read()
        res = vp.run_rom(PyBoy, rom_bytes, sym, consts, [
            ("A: follower ($A3), no $A3 check", lb, "refuse", "A.png"),
            ("B: wild-style (base), no $A3 check", base, "refuse", "B.png"),
        ], shots, "ROM built without the $A3 check")
        for x in res:
            r.check(f"{x['case']} (catch rate ${x['catch_rate']:02X}) is refused", x["ok"],
                    f"outcome {x['outcome']}, species after ${(x['after'] or 0):02X}"
                    + (f", error {x['error']}" if x["error"] else ""))
        sys.path.remove(c.path("tools"))
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
