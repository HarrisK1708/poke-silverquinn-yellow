#!/usr/bin/env python3
"""
Run the whole verification for this branch from a clean clone, in order:

  build    make (pokeyellow.gbc + debug ROM); must succeed with no warnings
  checks   dex_completeness.py (must report SINGLE-SAVE 154/154)
           verify_evo.py, verify_pikachu_block.py, verify_statics.py,
           verify_new_species.py                                   [emulator]
  faults   test_dex_completeness_faults.py
           test_evo_faults.py, test_pikachu_block_faults.py,
           test_statics_faults.py --fault-only                     [emulator]
           (verify_statics.py above already covers the clean phase)

Emulator steps need PyBoy (pip install pyboy pillow numpy); --no-emulator
runs only the build and the static-analysis steps. Every emulator step boots
a temp copy of the ROM, and every fault test corrupts a temp copy of the
repo, so the repo's sources and pokeyellow.gbc.ram/.state are never touched.
Screenshots and WAVs go to --out (default: a new temp dir).

Usage: python3 tools/verify_all.py [--no-emulator] [--out DIR]
Exit status: 0 if every step passes, 1 otherwise.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time

TOOLS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(TOOLS, ".."))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-emulator", action="store_true", help="skip the PyBoy steps")
    ap.add_argument("--out", default=None, help="screenshot/WAV directory (default: new temp dir)")
    args = ap.parse_args()
    out = args.out or tempfile.mkdtemp(prefix="verify_all_")
    os.makedirs(out, exist_ok=True)
    emu = not args.no_emulator
    if emu:
        try:
            import pyboy  # noqa: F401
        except ImportError:
            sys.exit("needs PyBoy for the emulator steps (pip install pyboy pillow numpy), or pass --no-emulator")

    results = []

    def step(name, cmd, check=None):
        print(f"\n=== {name} ===", flush=True)
        t = time.time()
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        text = p.stdout + p.stderr
        ok = p.returncode == 0 and (check is None or check(text))
        lines = text.rstrip().split("\n")
        print("\n".join(lines[-12:] if ok else lines[-60:]))
        results.append((name, ok, time.time() - t))
        print(f"--> {'PASS' if ok else 'FAIL'} ({time.time() - t:.0f}s)", flush=True)

    def tool(script, *a):
        return [sys.executable, os.path.join(TOOLS, script), *a]

    step("build: make", ["make"], lambda t: not re.search(r"warning", t, re.I))
    step("dex_completeness.py", tool("dex_completeness.py"),
         lambda t: re.search(r"SINGLE-SAVE obtainable \(no link trades\):\s+154/154", t))
    if emu:
        step("verify_evo.py", tool("verify_evo.py", "--screenshot-dir", out))
        step("verify_pikachu_block.py", tool("verify_pikachu_block.py", "--screenshot-dir", out))
        step("verify_statics.py", tool("verify_statics.py", "--screenshot-dir", out))
        step("verify_new_species.py", tool("verify_new_species.py", "--out-dir", out))
    step("test_dex_completeness_faults.py", tool("test_dex_completeness_faults.py"))
    if emu:
        step("test_evo_faults.py", tool("test_evo_faults.py"))
        step("test_pikachu_block_faults.py", tool("test_pikachu_block_faults.py"))
        step("test_statics_faults.py --fault-only", tool("test_statics_faults.py", "--fault-only"))

    print("\n=== verify_all summary ===")
    for name, ok, secs in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<40} {secs:5.0f}s")
    failed = sum(not ok for _, ok, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} steps passed"
          + ("" if args.no_emulator else f"; screenshots/WAVs in {out}"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
