#!/usr/bin/env python3
"""
In-game look at the species added after #151: open each one's Pokédex entry
(screenshots + decoded text) and record its cry, plus the cry it is derived
from, to WAV.

Flow: boot a temp copy of the ROM (the repo's .ram/.state are never touched),
play the intro to the bedroom, set EVENT_GOT_POKEDEX and mark the new species
and the cry-base species seen + owned (a display test: the catch path itself is
proven by tools/verify_statics.py), then START > POKéDEX:
  * for every new species: select it > DATA; screenshot every text page and
    decode the tile map: number, name, category and HT/WT must match
    data/pokemon/dex_entries.asm, and each page's three lines must equal the
    lines in data/pokemon/dex_text.asm exactly (no clipping, no wrap/spill
    into the rows between lines, nothing in the border column)
  * for every new species and its base: select it > CRY and record the audio
    to <species>_cry.wav. Before each cry the overworld music is stopped
    (music channel ids zeroed, DACs off) so only the cry is captured; each
    recording starts with a short pre-roll that must be silent.

Cry analysis per WAV: the mon_cry parameters from data/pokemon/cries.asm,
duration of the audible part, RMS level and spectral centroid (a rough
brightness measure; these cries are noise-heavy, so judge pitch by ear).

Requires: pip install pyboy pillow numpy
Usage:    python3 tools/verify_new_species.py [--rom pokeyellow.gbc] [--out-dir DIR]
"""

import argparse
import os
import re
import shutil
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_evo as ve  # noqa: E402

ROOT = ve.ROOT
NEW = ["SILVER", "QUINN", "RAYA"]
PREROLL_FRAMES, CRY_FRAMES = 20, 200
NR12, NR22, NR30, NR42 = 0xFF12, 0xFF17, 0xFF1A, 0xFF21


def cry_params():
    """species name (from the comment) -> (base sfx, pitch, length) from data/pokemon/cries.asm"""
    out = {}
    for l in open(os.path.join(ROOT, "data/pokemon/cries.asm")):
        m = re.match(r"\s*mon_cry (SFX_CRY_\w+), \$(\w+), \$(\w+) ; (.+)$", l)
        if m:
            out[m.group(4).strip().upper()] = (m.group(1), int(m.group(2), 16), int(m.group(3), 16))
    return out


TEXT_ROWS, TEXT_COL, TEXT_W = (11, 13, 15), 1, 18
EXTRA = {0xF4: ",", 0xE3: "-", 0x60: "′", 0x61: "″", 0x54: "#", 0xEE: "▼", 0xF2: "."}  # $F2 = <DOT>


def tile_char(t):
    if t in EXTRA:
        return EXTRA[t]
    return ve.DECODE.get(t, "¤")           # an unmapped glyph shows as ¤, never as a blank


def text_box(g):
    """The three dex text lines exactly as on screen (trailing blanks stripped)."""
    base = g.s["wTileMap"]
    return [("".join(tile_char(g.m[base + y * 20 + x]) for x in range(TEXT_COL, TEXT_COL + TEXT_W))).rstrip()
            for y in TEXT_ROWS]


def box_gaps_clear(g):
    """Rows between/below the lines must be blank (only the ▼ prompt at (18,16)): anything
    else means text wrapped or spilled. Col 19 must be the box border, not text."""
    base = g.s["wTileMap"]
    for y in (12, 14, 16):
        for x in range(TEXT_COL, TEXT_COL + TEXT_W):
            t = g.m[base + y * 20 + x]
            if t != 0x7F and not (y == 16 and x == 18 and t == 0xEE):
                return False
    # col 19 is the box border: a letter or punctuation there means a line overflowed
    return not any(tile_char(g.m[base + y * 20 + 19]) in ve.CHARMAP or g.m[base + y * 20 + 19] in EXTRA
                   for y in TEXT_ROWS if g.m[base + y * 20 + 19] != 0x7F)


def screen_rows(g):
    base = g.s["wTileMap"]
    return ["".join(" " if g.m[base + y * 20 + x] == 0x7F else tile_char(g.m[base + y * 20 + x])
                    for x in range(20)) for y in range(18)]


def pad3(lines):
    return (list(lines) + ["", "", ""])[:3]


def expected_entries():
    """category / height / weight from dex_entries.asm and the paged text from dex_text.asm,
    as the game should draw it (the dex terminator appends the final period)."""
    out = {}
    src = open(os.path.join(ROOT, "data/pokemon/dex_entries.asm")).read()
    txt = open(os.path.join(ROOT, "data/pokemon/dex_text.asm")).read()
    for n in NEW:
        cap = n.capitalize()
        m = re.search(rf'^{cap}DexEntry:\n\tdb "([^@]*)@"\n\tdb (\d+),\s*(\d+)\n\tdw (\d+)', src, re.M)
        assert m, f"no {cap}DexEntry"
        ft, inch, wt = int(m.group(2)), int(m.group(3)), int(m.group(4))
        body = re.search(rf"^_{cap}DexEntry::\n(.*?)\tdex\n", txt, re.M | re.S).group(1)
        pages = []
        for kw, s in re.findall(r'^\t(text|next|page) "(.*)"$', body, re.M):
            if kw in ("text", "page"):
                pages.append([])
            pages[-1].append(s)
        pages[-1][-1] += "."
        for p in pages:
            assert len(p) <= 3 and all(len(l) <= TEXT_W for l in p), f"{n}: source line too long / page too tall: {p}"
        out[n] = dict(category=m.group(1), ht=f"{ft}′{inch:02d}″", wt=f"{wt // 10}.{wt % 10}lb", pages=pages)
    return out


# Each new cry next to the vanilla cry it was derived from (same base SFX).
CRY_BASES = {"SILVER": "RHYDON", "QUINN": "VILEPLUME", "RAYA": "DRAGONITE"}


def cry_base_species(params):
    for n, b in CRY_BASES.items():
        assert params[n][0] == params[b][0], f"{n} ({params[n][0]}) and {b} ({params[b][0]}) differ in base SFX"
    return CRY_BASES


def goto_entry(g, name, dexnum):
    """From the Pokédex list, put the cursor on `name` and press A (opens the side menu)."""
    for _ in range(30):                       # back to the top of the list
        g.press("left", 8)
    for _ in range((dexnum - 1) // 7):        # page down
        g.press("right", 8)
    for _ in range(14):                       # the list redraws slowly: wait before re-reading
        g.tick(30)
        if name in g.cursor_row():
            g.press("a", 40)
            return
        g.press("down", 8)
    raise RuntimeError(f"could not put the cursor on {name}:\n" + g.text())


def reopen_dex(g):
    """Back out (B) to the START menu, then open POKéDEX again: every lookup starts from
    the same known state (the list after a DATA/CRY page does not always take input)."""
    for _ in range(12):
        t = g.text()
        if "ITEM" in t and "SAVE" in t and "CONTENTS" not in t:
            break
        if "CONTENTS" not in t and "HT" in t:  # still on an entry page
            g.press("a", 30)
            continue
        g.p.button("b", 4)
        g.tick(60)
    else:
        raise RuntimeError("could not get back to the START menu:\n" + g.text())
    g.select("POKéDEX")
    assert g.wait_for("CONTENTS"), "Pokédex did not reopen:\n" + g.text()
    g.tick(60)


def stop_music(g):
    s, m = g.s, g.m
    for ch in range(4):                       # CHAN1..CHAN4 = music
        m[s["wChannelSoundIDs"] + ch] = 0
    for reg in (NR12, NR22, NR30, NR42):      # DACs off; the cry re-enables what it uses
        m[reg] = 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", default=os.path.join(ROOT, "pokeyellow.gbc"))
    ap.add_argument("--sym", default=None, help="defaults to <rom>.sym")
    ap.add_argument("--out-dir", default=os.getcwd())
    args = ap.parse_args()
    try:
        from pyboy import PyBoy
        import numpy as np
    except ImportError:
        sys.exit("needs: pip install pyboy pillow numpy")

    sym_path = args.sym or os.path.splitext(args.rom)[0] + ".sym"
    sym = ve.load_sym(sym_path)
    dex = ve.load_consts("constants/pokedex_constants.asm")
    events = ve.load_consts("constants/event_constants.asm")
    params = cry_params()
    expected = expected_entries()
    bases = cry_base_species(params)
    os.makedirs(args.out_dir, exist_ok=True)

    tmp = tempfile.mkdtemp(prefix="verify_new_species_")
    rom = os.path.join(tmp, "rom.gbc")
    shutil.copy(args.rom, rom)
    pyboy = PyBoy(rom, window="null", sound_emulated=True)
    g = ve.Game(pyboy, sym)
    m = g.m
    sr = pyboy.sound.sample_rate
    entries, cries, ok = {}, {}, True
    try:
        ve.reach_overworld(g)
        pyboy.button("b", 4)
        g.tick(60)
        e = events["EVENT_GOT_POKEDEX"]
        m[sym["wEventFlags"] + e // 8] |= 1 << (e % 8)
        for n in NEW + list(bases.values()):
            d = dex["DEX_" + n] - 1
            for b in ("wPokedexOwned", "wPokedexSeen"):
                m[sym[b] + d // 8] |= 1 << (d % 8)
        pyboy.button("start", 4)
        g.tick(40)
        g.select("POKéDEX")
        assert g.wait_for("CONTENTS"), "Pokédex did not open:\n" + g.text()

        # ---- entries ----
        for n in NEW:
            num = dex["DEX_" + n]
            exp = expected[n]
            goto_entry(g, n, num)
            g.select("DATA")
            # header first (category, HT/WT), then each A press shows the next text page
            assert g.wait_for(exp["category"], 300), f"{n}: entry header never appeared:\n" + g.text()
            shots, pages_seen, page_ok = [], [], []
            for k, want in enumerate(exp["pages"]):
                for _ in range(40):
                    if text_box(g)[0] == want[0]:
                        break
                    g.tick(15)
                    if k == 0 and not any(text_box(g)):  # page 1 sometimes waits for A
                        g.press("a", 30)
                g.tick(40)
                got = text_box(g)
                pages_seen.append(got)
                page_ok.append(got == pad3(want) and box_gaps_clear(g))
                path = os.path.join(args.out_dir, f"dex_{n.lower()}_{k + 1}.png")
                pyboy.screen.image.save(path)
                shots.append(path)
                if k == 0:
                    first_header = screen_rows(g)
                g.press("a", 30)
            assert g.wait_for("CONTENTS", 300), f"{n}: did not return to the list after the last page:\n" + g.text()
            reopen_dex(g)
            rows = first_header
            checks = {"number": f"{num:03d}" in "".join(rows),
                      "name": n in rows[2],
                      "category": exp["category"] in rows[4],
                      "height": exp["ht"] in rows[6].replace(" ", ""),
                      "weight": exp["wt"] in rows[8].replace(" ", ""),
                      **{f"page {k + 1}": ok_ for k, ok_ in enumerate(page_ok)}}
            entries[n] = (checks, rows, pages_seen, exp["pages"], shots)
            ok &= all(checks.values())

        # ---- cries ----
        for n in NEW + [bases[x] for x in NEW]:
            num = dex["DEX_" + n]
            goto_entry(g, n, num)
            # side menu is open with the cursor on DATA; move to CRY without pressing A yet
            for _ in range(6):
                if "CRY" in g.cursor_row():
                    break
                g.press("down", 12)
            assert "CRY" in g.cursor_row(), "cursor not on CRY:\n" + g.text()
            chunks = []
            for _ in range(PREROLL_FRAMES):
                stop_music(g)
                g.tick(1)
                chunks.append(pyboy.sound.ndarray.copy())
            n_pre = sum(len(c) for c in chunks)
            pyboy.button("a", 2)
            for _ in range(CRY_FRAMES):
                g.tick(1)
                chunks.append(pyboy.sound.ndarray.copy())
            audio = np.concatenate(chunks).astype(np.int16)
            path = os.path.join(args.out_dir, f"{n.lower()}_cry.wav")
            with wave.open(path, "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(sr)
                w.writeframes((audio * 256).astype("<i2").tobytes())
            mono = audio.astype(np.float64).mean(axis=1)
            pre_peak = float(np.abs(mono[:n_pre]).max()) if n_pre else 0.0
            body = mono[n_pre:]
            loud = np.flatnonzero(np.abs(body) > 2)
            if len(loud):
                seg = body[loud[0]:loud[-1] + 1]
                dur = len(seg) / sr
                rms = float(np.sqrt((seg ** 2).mean()))
                spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
                freqs = np.fft.rfftfreq(len(seg), 1 / sr)
                centroid = float((freqs * spec).sum() / spec.sum())
            else:
                dur = rms = centroid = 0.0
            cries[n] = dict(path=path, params=params[n], dur=dur, rms=rms,
                            centroid=centroid, pre_peak=pre_peak)
            ok &= dur > 0.05 and pre_peak <= 2
            reopen_dex(g)
    finally:
        pyboy.stop(save=False)
        shutil.rmtree(tmp, ignore_errors=True)

    print("=== Pokédex entries (decoded from the tile map; text box = rows 11/13/15, cols 1-18) ===")
    for n, (checks, rows, seen, want, shots) in entries.items():
        print(f"\n--- {n} --- " + "  ".join(f"{k}:{'ok' if v else 'MISMATCH'}" for k, v in checks.items()))
        print("header:\n" + "\n".join("  |" + r + "|" for r in rows[:10]))
        for k, (got, w) in enumerate(zip(seen, want)):
            print(f"page {k + 1} ({os.path.basename(shots[k])}):")
            for gl, wl in zip(got, pad3(w)):
                print(f"  |{gl:<18}|" + ("" if gl == wl else f"   expected |{wl}|"))
    print("\n=== Cries (WAV, %d Hz stereo 16-bit; %d-frame silent pre-roll then the cry) ===" % (sr, PREROLL_FRAMES))
    print(f"  {'species':<10} {'mon_cry (base, pitch, length)':<30} {'audible':>8} {'RMS':>6} "
          f"{'centroid Hz':>11}  pre-roll  file")
    for n in NEW:
        for sp in (n, bases[n]):
            c = cries[sp]
            prm = f"{c['params'][0]}, ${c['params'][1]:02X}, ${c['params'][2]:02X}"
            role = "new" if sp == n else f"base of {n}"
            print(f"  {sp:<10} {prm:<30} {c['dur']:>7.2f}s {c['rms']:>6.1f} "
                  f"{c['centroid']:>11.0f}  {'silent' if c['pre_peak'] <= 2 else 'NOISY'}    "
                  f"{os.path.basename(c['path'])}  ({role})")
    print(f"\nfiles in {args.out_dir}")
    print("\nOK" if ok else "\nFAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
