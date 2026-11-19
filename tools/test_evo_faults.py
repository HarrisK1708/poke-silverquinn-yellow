#!/usr/bin/env python3
"""
Fault-injection test for tools/verify_evo.py (emulator; needs PyBoy).

For each of the four former trade evolutions, a temp copy of the repo
(tools/faultlib.py; the real files are never modified) gets that one line in
data/pokemon/evos_moves.asm reverted to the vanilla
`db EVOLVE_TRADE, 1, <EVOLVED>`, is rebuilt, and the copy's verify_evo.py
runs all four cases on that ROM. Expected: exit non-zero, the reverted case
FAILs (no evolution at Lv37), and the other three still PASS -- so the
harness detects exactly the broken evolution and nothing else.

Usage: python3 tools/test_evo_faults.py [--only KADABRA]
Exit status: 0 if every fault is detected as expected, 1 otherwise.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from faultlib import RepoCopy, Results, need_pyboy, tail  # noqa: E402

CASES = [("KADABRA", "ALAKAZAM"), ("MACHOKE", "MACHAMP"), ("GRAVELER", "GOLEM"), ("HAUNTER", "GENGAR")]
EVOS = "data/pokemon/evos_moves.asm"


def summary(stdout):
    """{'KADABRA -> ALAKAZAM': 'PASS'|'FAIL'} from verify_evo's summary table."""
    part = stdout.split("=== Summary ===", 1)
    rows = re.findall(r"^\s+(\w+ -> \w+)\s+.*?\s(PASS|FAIL)\b", part[1], re.M) if len(part) == 2 else []
    return dict(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", metavar="SPECIES", help="revert just this one (pre-evolution name)")
    args = ap.parse_args()
    need_pyboy()
    cases = [c for c in CASES if not args.only or c[0] == args.only.upper()]
    assert cases, f"--only must be one of {[c[0] for c in CASES]}"

    r = Results("verify_evo fault tests")
    for frm, to in cases:
        print(f"{frm} -> {to} reverted to EVOLVE_TRADE", flush=True)
        with RepoCopy(f"evo_{frm.lower()}") as c:
            c.replace(EVOS, f"\tdb EVOLVE_LEVEL, 37, {to}\n", f"\tdb EVOLVE_TRADE, 1, {to}\n")
            c.build()
            p = c.run_tool("verify_evo.py", "--screenshot-dir", c.path("_shots"))
            res = summary(p.stdout)
            want = {f"{a} -> {b}": ("FAIL" if a == frm else "PASS") for a, b in CASES}
            ok = p.returncode != 0 and res == want
            r.check(f"{frm} reverted -> only {frm} -> {to} FAILs", ok,
                    f"exit {p.returncode}; " + ", ".join(f"{k}: {v}" for k, v in res.items())
                    + ("" if ok else "\n" + tail(p)))
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
