#!/usr/bin/env python3
"""
Extract glyph bitmaps from 4x6.bdf and 5x8.bdf on the CIRCUITPY drive and
emit JavaScript font data ready to paste into simulator.html.

Usage (with device mounted):
    python3 extract_fonts.py /Volumes/CIRCUITPY
    python3 extract_fonts.py /Volumes/CIRCUITPY --render   # also print ASCII previews
"""

import argparse
import sys
from pathlib import Path


def parse_bdf(path):
    """Parse a BDF file and return {codepoint: [col_bitmasks...]} in 5-col format."""
    glyphs = {}
    in_char = False
    in_bitmap = False
    codepoint = None
    bbw = bbh = bbx = bby = 0
    raw_rows = []

    with open(path, "r") as f:
        for line in f:
            line = line.rstrip()
            if line.startswith("ENCODING "):
                codepoint = int(line.split()[1])
            elif line.startswith("BBX "):
                parts = line.split()
                bbw, bbh, bbx, bby = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
            elif line == "BITMAP":
                in_bitmap = True
                raw_rows = []
            elif line == "ENDCHAR":
                if codepoint is not None and codepoint >= 32:
                    glyphs[codepoint] = _to_col_bitmasks(raw_rows, bbw, bbh)
                in_bitmap = False
                codepoint = None
            elif in_bitmap:
                raw_rows.append(int(line, 16))

    return glyphs


def _to_col_bitmasks(rows, bbw, bbh):
    """Convert row-major hex rows (left-padded in BDF) to 5 column bitmasks
    (bottom-to-top bit order, matching the simulator's FONT_DATA format).
    Extra columns beyond 5 are dropped; missing columns are 0."""
    # BDF row bytes are left-aligned in ceil(bbw/8) bytes, MSB first
    col_masks = [0] * 5
    for row_idx, row_val in enumerate(rows):
        # Shift row value so bit 7 of the MSB byte = leftmost pixel
        shift = (((bbw + 7) // 8) * 8) - 1
        for col in range(min(bbw, 5)):
            bit = (row_val >> (shift - col)) & 1
            if bit:
                col_masks[col] |= (1 << row_idx)   # bit 0 = top row
    return col_masks


def render_glyph(col_masks, bbh):
    """Return a list of strings showing the glyph as ASCII art."""
    lines = []
    for row in range(bbh):
        s = ""
        for col in range(5):
            s += "█" if (col_masks[col] >> row) & 1 else "·"
        lines.append(s)
    return lines


def emit_js(glyphs, var_name, bbh, render=False):
    """Print a JavaScript const block for the given glyph dict."""
    chars = sorted(glyphs.keys())
    print(f"// {var_name}: {len(chars)} glyphs, cell height {bbh}px")
    print(f"const {var_name} = {{")
    for cp in chars:
        mask = glyphs[cp]
        hex_vals = ",".join(f"0x{v:02X}" for v in mask)
        char_repr = repr(chr(cp)) if 32 <= cp < 127 else f"U+{cp:04X}"
        comment = f"  // {char_repr}"
        if render:
            preview = " ".join("".join(
                "1" if (mask[c] >> r) & 1 else "0"
                for c in range(5)
            ) for r in range(bbh))
            comment += f"  [{preview}]"
        print(f"  {cp}:[{hex_vals}],{comment}")
    print("};")
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("circuitpy", nargs="?", default="/Volumes/CIRCUITPY",
                        help="Path to the CIRCUITPY drive (default: /Volumes/CIRCUITPY)")
    parser.add_argument("--render", action="store_true",
                        help="Print ASCII art previews of every glyph")
    parser.add_argument("--preview", metavar="CHARS", default="0123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz°/>",
                        help="Characters to preview when --render is set")
    args = parser.parse_args()

    root = Path(args.circuitpy)
    if not root.exists():
        sys.exit(f"CIRCUITPY drive not found at {root} — is the device mounted?")

    fonts = {
        "4x6.bdf": ("FONT_SMALL_DATA", 6),
        "5x8.bdf": ("FONT_MID_DATA",   8),
    }

    for filename, (var_name, expected_h) in fonts.items():
        path = root / filename
        if not path.exists():
            print(f"// WARNING: {filename} not found on device — skipping", flush=True)
            continue

        glyphs = parse_bdf(path)
        print(f"// Extracted from {path}", flush=True)

        if args.render:
            print(f"\n=== {filename} glyph previews ===")
            for ch in args.preview:
                cp = ord(ch)
                if cp in glyphs:
                    lines = render_glyph(glyphs[cp], expected_h)
                    print(f"  {repr(ch)}:")
                    for line in lines:
                        print(f"    {line}")
            print()

        emit_js(glyphs, var_name, expected_h, render=False)

    print("// Paste FONT_SMALL_DATA and FONT_MID_DATA into simulator.html,")
    print("// then update drawTextSmall() and drawTextMid() to use them")
    print("// instead of the shared FONT_DATA fallback.")


if __name__ == "__main__":
    main()
