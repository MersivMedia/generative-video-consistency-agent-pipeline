"""Objective QC for reference plates — no vision model needed.

Two checks a vision subagent is bad at but numpy is exact about:

  neutrality  is the backdrop actually neutral grey, or is a cinematic grade
              baked into the identity lock? Measures per-channel means on the
              border region and reports the max channel spread.
  distinctness are the N angle images actually N different angles, or did the
              ladder collapse into near-duplicates? Compares downsampled
              grayscale frames and flags suspiciously similar pairs.

Usage:
  python engine/qc.py assets/character/wrenn
  python engine/qc.py assets/plates/b1_arrival --graded   # plates keep the grade
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("pip install pillow")


def border_rgb(p: Path, frac: float = 0.10) -> tuple[float, float, float]:
    """Mean RGB of the outer frame — backdrop, not subject."""
    im = Image.open(p).convert("RGB")
    w, h = im.size
    bw, bh = max(1, int(w * frac)), max(1, int(h * frac))
    strips = [
        im.crop((0, 0, w, bh)),           # top
        im.crop((0, h - bh, w, h)),       # bottom
        im.crop((0, 0, bw, h)),           # left
        im.crop((w - bw, 0, w, h)),       # right
    ]
    tot, n = [0.0, 0.0, 0.0], 0
    for s in strips:
        px = list(s.getdata())
        n += len(px)
        for r, g, b in px:
            tot[0] += r
            tot[1] += g
            tot[2] += b
    return tuple(v / n for v in tot)


def neutrality(paths: list[Path], tol: float = 12.0) -> dict:
    """A neutral plate has near-identical R, G and B in the border.

    A baked-in grade shows up as channel spread: warm amber lifts R,
    teal/cyan lifts B and G. Tolerance ~12/255 is generous; a graded
    plate typically lands 25-60.
    """
    rows, worst = [], 0.0
    for p in paths:
        r, g, b = border_rgb(p)
        spread = max(r, g, b) - min(r, g, b)
        worst = max(worst, spread)
        cast = ("warm/amber" if r == max(r, g, b) else
                "cool/teal" if b == max(r, g, b) else "green")
        rows.append({"file": p.name, "rgb": (round(r), round(g), round(b)),
                     "spread": round(spread, 1),
                     "cast": cast if spread > tol else "neutral",
                     "pass": spread <= tol})
    return {"rows": rows, "worst_spread": round(worst, 1),
            "pass": worst <= tol, "tolerance": tol}


def uniformity(paths: list[Path], tol: float = 20.0) -> dict:
    """Do all plates in a set share ONE backdrop brightness?

    `neutrality` checks colour cast WITHIN each image; it says nothing about
    whether plate 3 is a white studio and plate 4 is mid-grey. Observed misses:
    border luma [136, 231, 224, 237, 238] -> spread 102/255, passed neutrality
    cleanly. A video model fed that set inherits inconsistent lighting cues.
    """
    lumas = []
    for p in paths:
        r, g, b = border_rgb(p)
        lumas.append((p.name, 0.299 * r + 0.587 * g + 0.114 * b))
    vals = [v for _, v in lumas]
    spread = max(vals) - min(vals)
    return {"rows": [{"file": n, "luma": round(v, 1)} for n, v in lumas],
            "spread": round(spread, 1), "pass": spread <= tol,
            "tolerance": tol,
            "darkest": min(lumas, key=lambda x: x[1])[0],
            "brightest": max(lumas, key=lambda x: x[1])[0]}


def distinctness(paths: list[Path], size: int = 48, tol: float = 6.0) -> dict:
    """Mean absolute pixel difference between downsampled grayscale frames.

    Two genuinely different camera angles of the same subject differ a lot.
    A collapsed ladder (two near-duplicate profiles) shows a small delta.

    LIMITATION — this is NOT an angle-correctness check. It measures pixel
    difference, not semantics. A left profile and a mirrored/head-turned left
    profile differ substantially in pixels while being the SAME angle class.
    Verified false negative: a set where panels 2 and 4 were both ~90 degree
    profiles instead of the requested 45 degree three-quarters passed at
    min delta 6.54. Only a vision subagent can confirm the angle LADDER;
    distinctness only catches literal near-duplicates.
    """
    thumbs = {}
    for p in paths:
        im = Image.open(p).convert("L").resize((size, size), Image.LANCZOS)
        thumbs[p.name] = list(im.convert("L").tobytes())

    names = list(thumbs)
    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = thumbs[names[i]], thumbs[names[j]]
            d = sum(abs(x - y) for x, y in zip(a, b)) / len(a)
            pairs.append({"a": names[i], "b": names[j], "delta": round(d, 2),
                          "suspect": d < tol})
    pairs.sort(key=lambda x: x["delta"])
    return {"closest": pairs[:5], "min_delta": pairs[0]["delta"] if pairs else None,
            "suspects": [p for p in pairs if p["suspect"]],
            "pass": not any(p["suspect"] for p in pairs), "tolerance": tol}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--graded", action="store_true",
                    help="location plates: skip neutrality AND uniformity "
                         "(see note in main)")
    a = ap.parse_args()
    d = Path(a.dir)

    groups = {
        "body": sorted(d.glob("body_*.png")),
        "face": sorted(d.glob("face_*.png")),
        "plate": sorted(d.glob("plate_*.png")),
        "anchor": sorted(d.glob("anchor_*.png")),
    }
    groups = {k: v for k, v in groups.items() if v}
    if not groups:
        sys.exit(f"no reference images found in {d}")

    ok = True
    for name, paths in groups.items():
        print(f"\n=== {name}  ({len(paths)} images)")

        if not a.graded:
            n = neutrality(paths)
            print(f"  neutrality: {'PASS' if n['pass'] else 'FAIL'}  "
                  f"worst channel spread {n['worst_spread']} (tol {n['tolerance']})")
            for r in n["rows"]:
                flag = "  " if r["pass"] else "<-"
                print(f"    {flag} {r['file']:16s} rgb={r['rgb']} "
                      f"spread={r['spread']:5.1f} {r['cast']}")
            ok &= n["pass"]

        # Uniformity is a CHARACTER-PLATE rule, not a universal one. Identity
        # locks must share one studio so the video model reads albedo rather
        # than lighting. Location plates are the opposite case: a low-angle
        # interior and a high-angle exterior of the same place SHOULD differ in
        # exposure, because they become real first-frames carrying the grade.
        # Enforcing uniformity on them flagged a legitimate 44-luma spread as a
        # failure. Scope every metric to the artifact it actually governs.
        if len(paths) > 1 and not a.graded:
            u = uniformity(paths)
            print(f"  uniformity: {'PASS' if u['pass'] else 'FAIL'}  "
                  f"backdrop luma spread {u['spread']} (tol {u['tolerance']})")
            if not u["pass"]:
                print(f"    <- darkest {u['darkest']} vs brightest {u['brightest']}")
                print(f"       {[r['luma'] for r in u['rows']]}")
            ok &= u["pass"]

            s = distinctness(paths)
            print(f"  distinctness: {'PASS' if s['pass'] else 'FAIL'}  "
                  f"min delta {s['min_delta']} (tol {s['tolerance']})"
                  "  [near-duplicates only; NOT angle correctness]")
            for p in s["closest"][:3]:
                flag = "<-" if p["suspect"] else "  "
                print(f"    {flag} {p['a']} vs {p['b']}: {p['delta']}")
            ok &= s["pass"]

    print(f"\n{'ALL CHECKS PASS' if ok else 'CHECKS FAILED — do not promote this set'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
