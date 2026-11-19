#!/usr/bin/env python3
"""
Caught-flag tests for the static encounters (emulator; needs PyBoy).

Check (5) of tools/verify_statics.py loads the snapshot taken in front of the
static, pre-sets its EVENT_BEAT_* flag and talks to it: no battle may fire.
This test pins that down for all four statics (SILVER, QUINN, RAYA, MEW) and
proves the check can fail:

  clean      unmodified temp copy: verify_statics.py passes every check, and
             check (5) PASSes for each static (flag pre-set -> no battle)
  fault      temp copy (tools/faultlib.py; the real files are never modified)
             whose four trainer headers point at the wrong flag,
             `trainer (EVENT_BEAT_<X> + 8), ...` (same bit, next byte: the
             `trainer` macro asserts the bit, so the copy still assembles;
             parenthesised because the macro pastes \1 into `\1 % 8`),
             rebuilt: pre-setting the species' own EVENT_BEAT_* flag no
             longer stops the battle, so check (5) must FAIL for every static
             and verify_statics.py must exit non-zero

Usage: python3 tools/test_statics_faults.py [--fault-only]
Exit status: 0 if both phases behave as expected, 1 otherwise.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from faultlib import RepoCopy, Results, need_pyboy, tail  # noqa: E402

HEADERS = {"SILVER": "scripts/RockTunnelB1F.asm", "QUINN": "scripts/ViridianForest.asm",
           "RAYA": "scripts/CeruleanCaveB1F.asm", "MEW": "scripts/CeruleanCaveB1F.asm"}
FLAG_CHECK = "(5) flag-set suppresses re-encounter"


def checks(stdout):
    """({(species, check name): 'PASS'|'FAIL'}, {(species, check name): detail})
    from verify_statics' summary table."""
    part = stdout.split("=== Summary ===", 1)
    out, detail = {}, {}
    if len(part) == 2:
        for res, sp, name, det in re.findall(r"^\s+(PASS|FAIL)\s+(\w+)\s+(\(\d\) .+?|error)\s{2,}(.*)$",
                                             part[1], re.M):
            out[(sp, name.strip())] = res
            detail[(sp, name.strip())] = det.strip()
    return out, detail


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fault-only", action="store_true", help="skip the clean phase")
    args = ap.parse_args()
    need_pyboy()
    r = Results("static caught-flag tests")

    if not args.fault_only:
        print("clean copy: flag pre-set -> no battle", flush=True)
        with RepoCopy("statics_clean") as c:
            c.build()
            p = c.run_tool("verify_statics.py", "--screenshot-dir", c.path("_shots"))
            res, det = checks(p.stdout)
            r.check("verify_statics.py passes on the clean copy",
                    p.returncode == 0 and res and all(v == "PASS" for v in res.values()),
                    f"exit {p.returncode}, {sum(v == 'PASS' for v in res.values())}/{len(res)} checks PASS"
                    + ("" if p.returncode == 0 else "\n" + tail(p)))
            for sp in HEADERS:
                r.check(f"{sp}: EVENT_BEAT_{sp} pre-set -> no battle", res.get((sp, FLAG_CHECK)) == "PASS",
                        f"check (5) {res.get((sp, FLAG_CHECK), 'missing')} ({det.get((sp, FLAG_CHECK), '')})")

    print("fault copy: each trainer header points at EVENT_BEAT_<X> + 8", flush=True)
    with RepoCopy("statics_fault") as c:
        for sp, rel in HEADERS.items():
            c.replace(rel, f"\ttrainer EVENT_BEAT_{sp}, 0,", f"\ttrainer (EVENT_BEAT_{sp} + 8), 0,")
        c.build()
        p = c.run_tool("verify_statics.py", "--screenshot-dir", c.path("_shots"))
        res, det = checks(p.stdout)
        r.check("verify_statics.py exits non-zero on the fault copy", p.returncode != 0, f"exit {p.returncode}")
        for sp in HEADERS:
            got = res.get((sp, FLAG_CHECK), "missing")
            r.check(f"{sp}: header checks EVENT_BEAT_{sp} + 8 -> check (5) FAILs (battle fires)",
                    got == "FAIL" and det.get((sp, FLAG_CHECK), "").endswith("wIsInBattle=1"),
                    f"check (5) {got} ({det.get((sp, FLAG_CHECK), '')})"
                    + ("" if got != "missing" else "\n" + tail(p)))
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
