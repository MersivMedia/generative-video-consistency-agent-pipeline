"""Backdrop normalization — fix plate brightness in post, not in the prompt.

Reference plates come from separate API calls, and each call picks its own
backdrop brightness no matter how precisely the prompt pins a value. Observed:
"exactly 50 percent grey, hex #808080, RGB 128 128 128" in every prompt still
produced a border-luma spread of 129.6/255 across one 5-image set. Per-angle
calls made it WORSE than a single batched call, because each call is an
independent roll.

Prompting is the wrong tool. Backdrop level is measurable and correctable, so
compute it: estimate each plate's backdrop, then apply a smooth correction that
lands every plate on one shared target while leaving the subject alone.

Same principle as compositing brand assets in PIL instead of regenerating them —
deterministic beats generative for anything you can calculate.

Usage:
  python normalize.py assets/character/wrenn --target 200
  python normalize.py assets/character/wrenn --dry-run
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("pip install pillow")


def backdrop_luma(im: Image.Image, frac: float = 0.10) -> float:
    """Median luma of the border region — backdrop, not subject.

    Median rather than mean: a subject's hair or a limb intruding into the
    border strip skews a mean badly but barely moves a median.
    """
    g = im.convert("L")
    w, h = g.size
    bw, bh = max(1, int(w * frac)), max(1, int(h * frac))
    vals: list[int] = []
    for box in ((0, 0, w, bh), (0, h - bh, w, h),
                (0, 0, bw, h), (w - bw, 0, w, h)):
        vals.extend(g.crop(box).getdata())
    vals.sort()
    return float(vals[len(vals) // 2])


def normalize(im: Image.Image, target: float, frac: float = 0.10) -> Image.Image:
    """Shift the plate so its backdrop sits at `target`.

    Gamma-style correction, not a flat additive offset: an additive shift fixes
    the backdrop while clipping the subject's highlights, whereas a gamma curve
    compresses rather than clips, so faces and costume keep tonal separation.

    LUMA ONLY. Applying the same gamma LUT to R, G and B independently amplifies
    whatever small channel imbalance already existed — verified regression: face
    plates passing `neutrality` at 6.2 came back at 12.6 (FAIL) after an
    all-channel correction. Brightness and colour cast are separate properties
    and must be corrected separately. So: convert to YCbCr, curve Y, leave the
    chroma planes untouched, recombine.
    """
    src = backdrop_luma(im, frac)
    if src <= 0 or abs(src - target) < 1.0:
        return im

    import math
    s, t = src / 255.0, target / 255.0
    s = min(max(s, 1e-3), 1 - 1e-3)
    t = min(max(t, 1e-3), 1 - 1e-3)
    g = math.log(t) / math.log(s)
    g = min(max(g, 0.25), 4.0)  # sanity clamp

    lut = [min(255, max(0, int(round(255.0 * ((i / 255.0) ** g)))))
           for i in range(256)]

    if im.mode != "RGB":
        im = im.convert("RGB")
    y, cb, cr = im.convert("YCbCr").split()
    y = y.point(lut)
    return Image.merge("YCbCr", (y, cb, cr)).convert("RGB")


def white_balance(im: Image.Image, frac: float = 0.10,
                  tol: float = 6.0) -> Image.Image:
    """Neutralise a colour cast using the backdrop as a known-neutral target.

    Needed because strong colour words in the COSTUME canon bleed into the
    backdrop: a TAN-KHAKI wardrobe produced a warm/amber cast (spread 16-24)
    and an OLIVE-GREEN one a cool/teal cast (spread 20-31), on plates whose
    prompt explicitly demanded neutral #808080. Fixing the wardrobe introduced
    the cast, so it has to be corrected in post like the luma.

    The backdrop SHOULD be neutral grey, so per-channel gains that force
    border R=G=B are a principled white balance, not a guess. Applied to the
    whole frame, which also corrects the cast on the subject.
    """
    r, g, b = _border_median_rgb(im, frac)
    if max(r, g, b) - min(r, g, b) <= tol:
        return im
    target = (r + g + b) / 3.0
    luts = []
    for ch in (r, g, b):
        gain = target / max(ch, 1.0)
        luts.append([min(255, max(0, int(round(i * gain)))) for i in range(256)])
    if im.mode != "RGB":
        im = im.convert("RGB")
    rr, gg, bb = im.split()
    return Image.merge("RGB", (rr.point(luts[0]), gg.point(luts[1]),
                               bb.point(luts[2])))


def _border_median_rgb(im: Image.Image, frac: float = 0.10):
    w, h = im.size
    bw, bh = max(1, int(w * frac)), max(1, int(h * frac))
    px: list[tuple] = []
    for box in ((0, 0, w, bh), (0, h - bh, w, h),
                (0, 0, bw, h), (w - bw, 0, w, h)):
        px.extend(im.crop(box).getdata())
    m = len(px) // 2
    return (float(sorted(q[0] for q in px)[m]),
            float(sorted(q[1] for q in px)[m]),
            float(sorted(q[2] for q in px)[m]))


TOL = 20.0  # matches qc.py uniformity tolerance


def triage(lumas: dict, target: float, tol: float = TOL) -> tuple[list, list]:
    """Correct, then MEASURE. Flag whatever refuses to land.

    An earlier version guessed with a ratio threshold. Empirical is better and
    simpler: apply the correction, re-measure the result, and treat any plate
    that still misses tolerance as a must-regenerate outlier. Gamma correction
    handles moderate drift but cannot rescue a plate shot on a fundamentally
    different backdrop — observed luma 46 in a set whose median was 223, which
    gamma lifts only to 166 while washing out the subject.
    """
    fix, regen = [], []
    for pth, v in lumas.items():
        landed = backdrop_luma(normalize(white_balance(Image.open(pth)), target))
        (fix if abs(landed - target) <= tol else regen).append(pth)
    return fix, regen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--target", type=float, default=None,
                    help="target backdrop luma; default = median across the set")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    a = ap.parse_args()

    d = Path(a.dir)
    groups = {
        # Anchors were originally excluded here because uniformity is
        # meaningless for a single image — but qc still grades their
        # NEUTRALITY, so an uncorrected anchor failed the set while every
        # derived plate passed. Any artifact a gate grades must also be a
        # artifact the fixer processes.
        "anchor": sorted(d.glob("anchor_*.png")),
        "body": sorted(d.glob("body_*.png")),
        "face": sorted(d.glob("face_*.png")),
        "plate": sorted(d.glob("plate_*.png")),
    }
    groups = {k: v for k, v in groups.items() if v}
    if not groups:
        sys.exit(f"no plates found in {d}")

    for name, paths in groups.items():
        lumas = {p: backdrop_luma(Image.open(p)) for p in paths}
        vals = sorted(lumas.values())
        # Median target: no single plate has to move very far, versus anchoring
        # on an arbitrary value that drags the whole set.
        target = a.target if a.target is not None else vals[len(vals) // 2]
        spread_before = max(vals) - min(vals)

        print(f"\n=== {name}  ({len(paths)} plates)  target luma {target:.0f}")
        print(f"  spread before: {spread_before:.1f}")

        fix, regen = triage(lumas, target)
        if regen:
            print(f"  OUTLIERS (regenerate, do not correct): "
                  f"{[p.name for p in regen]}")
            for p in regen:
                landed = backdrop_luma(normalize(white_balance(Image.open(p)), target))
                print(f"    {p.name:16s} luma {lumas[p]:6.1f} -> best {landed:6.1f} "
                      f"vs target {target:.0f} — uncorrectable, regenerate")

        after = []
        for p in fix:
            src = lumas[p]
            # Chroma first, then luma: correcting brightness on a cast
            # image bakes the cast in at the new level.
            out = normalize(white_balance(Image.open(p)), target)
            new_l = backdrop_luma(out)
            after.append(new_l)
            print(f"    {p.name:16s} {src:6.1f} -> {new_l:6.1f}")
            if not a.dry_run:
                if not a.no_backup:
                    # Versioned by content hash of the CURRENT file, so a
                    # blanket "restore from _raw" cannot silently reinstate
                    # plates generated from an older prompt. A flat _raw/ dir
                    # did exactly that: it overwrote freshly generated
                    # costume-canon plates with pre-costume backups, and the
                    # only signal was a QC costume FAIL afterwards.
                    import hashlib
                    h = hashlib.sha1(p.read_bytes()).hexdigest()[:8]
                    bak = p.parent / "_raw"
                    bak.mkdir(exist_ok=True)
                    tgt = bak / f"{p.stem}.{h}{p.suffix}"
                    if not tgt.exists():
                        shutil.copy(p, tgt)
                out.save(p)

        if after:
            sp = max(after) - min(after)
            print(f"  spread after (corrected plates): {sp:.1f}  "
                  f"{'PASS' if sp <= 20 else 'STILL FAILING'}")
        if regen:
            print(f"  SET NOT PROMOTABLE — regenerate {len(regen)} outlier(s) first")

    if a.dry_run:
        print("\n(dry run — nothing written)")


if __name__ == "__main__":
    main()
