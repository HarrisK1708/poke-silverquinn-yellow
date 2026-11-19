"""
Shared helpers for the fault-injection tests (tools/test_*_faults.py).

A fault test never touches the real repo. It copies the working tree to a
temp dir (RepoCopy), corrupts files in that copy, rebuilds the copy if the
checker needs a ROM, and runs the copy's own tools/*.py against it. Each
tool computes ROOT from its own path, so the copied checker reads the copied,
corrupted sources and ROM. When the copy is closed, every repo file the test
edited in the copy is re-hashed in the real repo; a change there is an error.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SKIP = {".git", ".venv", "venv", "__pycache__", ".DS_Store"}
SKIP_SUFFIXES = (".gbc.ram", ".gbc.state", ".sav", ".sgm")


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


def _ignore(_dir, names):
    return [n for n in names if n in SKIP or n.endswith(SKIP_SUFFIXES)]


class RepoCopy:
    """Context manager: a throwaway copy of the working tree.

    copy2 keeps mtimes, so `make` in the copy only rebuilds what a fault touched
    (a few seconds); from a clean clone the first build in a copy is a full build.
    """

    def __init__(self, label):
        self.label = label
        self.dir = None
        self._guard = {}

    def __enter__(self):
        self.dir = os.path.join(tempfile.mkdtemp(prefix=f"fault_{self.label}_"), "repo")
        shutil.copytree(ROOT, self.dir, ignore=_ignore, symlinks=True)
        assert not os.path.realpath(self.dir).startswith(os.path.realpath(ROOT) + os.sep)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(os.path.dirname(self.dir), ignore_errors=True)
        changed = [rel for rel, h in self._guard.items() if _sha(os.path.join(ROOT, rel)) != h]
        assert not changed, f"real repo files changed during a fault test: {changed}"
        return False

    def path(self, rel):
        p = os.path.normpath(os.path.join(self.dir, rel))
        assert p.startswith(self.dir + os.sep), f"{rel} escapes the copy"
        return p

    def _guard_real(self, rel):
        real = os.path.join(ROOT, rel)
        if os.path.exists(real) and rel not in self._guard:
            self._guard[rel] = _sha(real)

    def read(self, rel):
        with open(self.path(rel), encoding="utf-8") as f:
            return f.read()

    def write(self, rel, text):
        self._guard_real(rel)
        with open(self.path(rel), "w", encoding="utf-8") as f:
            f.write(text)

    def replace(self, rel, old, new, count=1):
        """Replace `old` with `new`; `old` must occur exactly `count` times."""
        text = self.read(rel)
        n = text.count(old)
        assert n == count, f"{rel}: expected {count} occurrence(s) of {old!r}, found {n}"
        self.write(rel, text.replace(old, new))

    def delete_line(self, rel, anchor, line):
        """Delete the first line at or after the line `anchor` whose code (comment
        stripped, whitespace trimmed) equals `line`. Returns the 1-based line number."""
        lines = self.read(rel).split("\n")
        code = [l.split(";", 1)[0].strip() for l in lines]
        assert anchor in code, f"{rel}: anchor {anchor!r} not found"
        start = code.index(anchor)
        assert line in code[start:], f"{rel}: {line!r} not found after {anchor!r}"
        i = code.index(line, start)
        del lines[i]
        self.write(rel, "\n".join(lines))
        return i + 1

    def build(self):
        """`make pokeyellow.gbc` in the copy; returns the ROM path. Fails on any warning."""
        p = subprocess.run(["make", "pokeyellow.gbc"], cwd=self.dir, capture_output=True, text=True)
        out = p.stdout + p.stderr
        assert p.returncode == 0, f"build of the corrupted copy failed:\n{out[-3000:]}"
        assert not re.search(r"warning", out, re.I), f"build of the corrupted copy warned:\n{out[-3000:]}"
        return self.path("pokeyellow.gbc")

    def run_tool(self, script, *args, timeout=3600):
        """Run the copy's tools/<script> with this interpreter, cwd = the copy."""
        return subprocess.run([sys.executable, self.path(os.path.join("tools", script)), *args],
                              cwd=self.dir, capture_output=True, text=True, timeout=timeout)


class Results:
    """Collects named checks; report() prints them and returns the exit code."""

    def __init__(self, title):
        self.title = title
        self.rows = []

    def check(self, name, ok, detail=""):
        self.rows.append((name, bool(ok), detail))
        print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + (f": {detail}" if detail else ""), flush=True)
        return ok

    def report(self):
        failed = [r for r in self.rows if not r[1]]
        print(f"\n{self.title}: {len(self.rows) - len(failed)}/{len(self.rows)} passed")
        return 1 if failed or not self.rows else 0


def need_pyboy():
    try:
        import pyboy  # noqa: F401
    except ImportError:
        sys.exit("needs PyBoy: pip install pyboy pillow numpy")


def tail(proc, n=25):
    return "\n".join((proc.stdout + proc.stderr).rstrip().split("\n")[-n:])
