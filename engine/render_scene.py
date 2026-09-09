"""M2 — render one canon scene to video via Replicate seedance-2.0.

Consumes a scene from `runs/<run>/canon.json` and produces a muxed MP4, using
the pre-production identity locks from M1.5 as `reference_images`.

Mode mapping (validated by the showrunner, honoured here):
  ref2v  -> reference_images = character locks + location plate
  flf    -> image (first frame) + last_frame_image, chained from prior shot
  t2v    -> prompt only

The style bible is applied HERE, on the shot prompt — never on the reference
plates, which stay neutral so the model reads albedo not coloured light.

Cost control: --dry-run prints the exact payloads and an estimate without
spending; --shots limits how many clips actually render.

Usage:
  python render_scene.py runs/<run>/canon.json --scene 0 --dry-run
  python render_scene.py runs/<run>/canon.json --scene 0 --shots 1
  python render_scene.py runs/<run>/canon.json --scene 0
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fal_client as fal  # noqa: E402

COST_PER_CLIP = 0.35  # H3 Max ~768P; refine once billing shows real numbers

# H3 Max reference-to-video takes multiple subject/style refs. Budget them
# deliberately: character locks first, since identity is the hard problem.
MAX_REFS = 9
LOCKS_PER_CHARACTER = 3   # front + profile + back
PLATES_PER_SCENE = 2      # wide + medium of the location


def character_refs(assets: Path, keys: list[str]) -> list[Path]:
    """Identity locks, interleaved so no character is starved by the 9-ref cap.

    Basenames collide across characters (every character has body_00.png), so
    always carry resolved paths and dedupe on those, never on name.
    """
    per: list[list[Path]] = []
    for k in keys:
        d = assets / "character" / k
        angles = sorted(d.glob("body_*.png"))[:LOCKS_PER_CHARACTER]
        face = d / "face_00.png"
        per.append(angles + ([face] if face.exists() else []))

    # Round-robin: with 2 characters and a 9-ref budget each gets an equal
    # share instead of the first one consuming everything.
    out: list[Path] = []
    for i in range(max((len(x) for x in per), default=0)):
        for lst in per:
            if i < len(lst):
                out.append(lst[i])
    return out


def plate_refs(assets: Path, beat_id: str) -> list[Path]:
    d = assets / "plates" / beat_id
    return sorted(d.glob("plate_*.png"))[:PLATES_PER_SCENE] if d.is_dir() else []


def build_refs(assets: Path, shot: dict, beat_id: str) -> list[Path]:
    """Character identity outranks location: plates are dropped first."""
    keys = shot.get("characters_on_screen", [])
    chars = character_refs(assets, keys)
    plates = plate_refs(assets, beat_id)

    refs, seen = [], set()
    for p in chars + plates:
        r = p.resolve()
        if r not in seen:
            seen.add(r)
            refs.append(p)
    if len(refs) > MAX_REFS:
        keep_chars = [p for p in refs if "character" in p.parts][:MAX_REFS - 1]
        keep_plates = [p for p in refs if "plates" in p.parts]
        refs = (keep_chars + keep_plates)[:MAX_REFS]
    return refs


def shot_prompt(shot: dict, style: str, chars: dict | None = None,
                shot_chars: list[str] | None = None,
                ref_labels: list[str] | None = None,
                audio_label: str | None = None) -> str:
    """Style bible belongs on the SHOT, never on the reference plates.

    CRITICAL: H3's reference API expects each asset to be CITED IN THE PROMPT as
    "Image 1", "Image 2", "Audio 1" etc. Passing reference_image_urls without
    naming them in the prompt leaves the model to guess what they are for, which
    is how render A ended up with weak identity conditioning despite nine locks
    attached. Name every asset and say what it governs.

    Costume canon is restated as text as well: the plates carry it visually, but
    a written reminder measurably reduces wardrobe invention mid-shot.
    """
    p = shot["video_prompt"]

    if ref_labels:
        p = f"{p}\n\nREFERENCE ASSETS:\n" + "\n".join(ref_labels)

    if chars and shot_chars:
        wardrobe = " ".join(
            f"{chars[k]['name']} wears: {chars[k]['costume']}"
            for k in shot_chars if k in chars and chars[k].get("costume"))
        if wardrobe:
            p = f"{p}\n\nWARDROBE (mandatory, do not change): {wardrobe}"

    lines = shot.get("dialogue", [])
    if lines and audio_label:
        spoken = " ".join(f'{chars[d["character"]]["name"]} says "{d["line"]}"'
                          for d in lines) if chars else ""
        p += (f"\n\nDIALOGUE: {audio_label} contains the spoken dialogue for this "
              f"shot. The speaking character's mouth must be LIP-SYNCED to it. "
              f"{spoken}")
    elif lines and chars:
        spoken = " ".join(f'{chars[d["character"]]["name"]}: "{d["line"]}"'
                          for d in lines)
        p += f"\n\nDIALOGUE (spoken on camera): {spoken}"

    # Interior/exterior incoherence: the model put a character inside a doorway
    # with the exterior landscape behind her. State camera side explicitly.
    if shot.get("camera_side"):
        p += (f"\n\nCAMERA POSITION: {shot['camera_side']}. Everything behind the "
              "subject must belong to that side of the doorway — do not show the "
              "opposite side's environment behind them.")

    if style.split(",")[0].strip().lower() not in p.lower():
        p = f"{p}\n\nSTYLE: {style}"
    return p


def render_shot(shot: dict, beat_id: str, style: str, assets: Path,
                outdir: Path, prev_last: Path | None, idx: int,
                dry: bool, chars: dict | None = None,
                voice_track: Path | None = None) -> tuple[Path | None, dict]:
    """Map one showrunner shot onto the right H3 Max endpoint.

    AUDIO ORDER MATTERS. Dialogue is synthesized FIRST and passed in as
    `reference_audio_urls`, so the model generates the shot lip-synced to the
    real voice track. Rendering first and muxing speech afterwards (render A)
    produced two problems the user caught immediately: the model's own ambience
    bed playing under unrelated speech, and mouths moving out of time with the
    words. Reference audio fixes both — the returned clip already contains the
    dialogue, so nothing is muxed on top.

    Constraint: images + videos + audio must total <= 12 files, and audio may
    never be the only reference input.
    """
    mode = shot["mode"]
    refs = build_refs(assets, shot, beat_id)
    used_refs: list[Path] = []
    ref_labels: list[str] = []
    audio_label = None

    inp: dict = {
        "prompt_expansion_mode": fal.EXPANSION,
        "duration": shot["duration"],
        "resolution": "768P",
        "enable_safety_checker": False,
    }

    if mode == "flf" and prev_last is not None:
        endpoint = fal.ENDPOINTS["flf"]
        inp["image_url"] = "<uploaded>" if dry else fal.upload(prev_last)
        note = f"chained from {prev_last.name}"
        # i2v has no reference_audio_urls, so a chained shot cannot lip-sync.
        # Prefer ref2v for any shot carrying dialogue.
        if shot.get("dialogue"):
            note += " (WARNING: dialogue shot on flf — no lip-sync available)"
    elif refs:
        endpoint = fal.ENDPOINTS["ref2v"]
        # Budget: 12 files total, reserve one slot for the voice track.
        cap = MAX_REFS - (1 if voice_track else 0)
        refs = refs[:cap]
        inp["reference_image_urls"] = (
            ["<uploaded>"] * len(refs) if dry
            else [fal.upload(p) for p in refs])
        inp["aspect_ratio"] = "21:9"
        used_refs = refs
        for i, p in enumerate(refs, 1):
            who = p.parent.name
            kind = ("face" if p.name.startswith("face")
                    else "location plate" if "plate" in p.name else "body")
            ref_labels.append(
                f"Image {i}: {kind} reference for "
                f"{chars[who]['name'] if chars and who in chars else who}"
                if kind != "location plate"
                else f"Image {i}: the location itself")
        if voice_track:
            audio_label = f"Audio 1"
            inp["reference_audio_urls"] = (
                ["<uploaded>"] if dry else [fal.upload(voice_track)])
        note = f"{len(refs)} cited locks" + (" + lip-sync audio" if voice_track else "")
        if mode == "flf":
            note += " (no prior frame; ref2v fallback)"
    else:
        endpoint = fal.ENDPOINTS["t2v"]
        inp["aspect_ratio"] = "21:9"
        note = "text only, no locks"

    inp["prompt"] = shot_prompt(shot, style, chars,
                                shot.get("characters_on_screen"),
                                ref_labels, audio_label)

    meta = {"slug": shot["slug"], "mode": mode, "endpoint": endpoint,
            "duration": shot["duration"],
            "refs": [f"{p.parent.name}/{p.name}" for p in used_refs],
            "lip_sync": bool(voice_track and audio_label),
            "note": note,
            "payload_bytes": len(json.dumps({"input": inp})),
            "ref_disk_mb": round(sum(p.stat().st_size for p in used_refs) / 1e6, 1)}

    if dry:
        return None, meta

    res = fal.run(endpoint, inp, timeout=900)
    url = (res.get("video") or {}).get("url")
    if not url:
        raise RuntimeError(f"no video in result: {json.dumps(res)[:400]}")
    out = outdir / f"shot_{idx:02d}.mp4"
    fal.fetch(url, out)
    meta["file"] = str(out)
    meta["inference"] = (res.get("timings") or {}).get("inference")
    return out, meta


def last_frame(video: Path) -> Path:
    """Extract the final frame so the next `flf` shot can chain from it."""
    out = video.with_name(video.stem + "_last.png")
    subprocess.run(
        ["ffmpeg", "-y", "-sseof", "-0.5", "-i", str(video),
         "-vsync", "0", "-q:v", "2", "-frames:v", "1", str(out)],
        check=True, capture_output=True)
    return out


def concat(clips: list[Path], out: Path) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{c.resolve()}'\n" for c in clips))
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(out)],
                   check=True, capture_output=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("canon")
    ap.add_argument("--story", default="stories/the_signal.json")
    ap.add_argument("--assets", default="assets")
    ap.add_argument("--scene", type=int, default=0)
    ap.add_argument("--shots", type=int, default=None,
                    help="render at most N shots (cost control)")
    ap.add_argument("--no-voice", action="store_true",
                    help="skip TTS/lip-sync reference audio")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="renders")
    a = ap.parse_args()

    story = json.loads(Path(a.story).read_text())
    canon = [e for e in json.loads(Path(a.canon).read_text())
             if e["type"] == "canon"]
    ev = canon[a.scene]
    shots = ev["head_shots"] + ev["tail_shots"]
    if a.shots:
        shots = shots[:a.shots]

    assets = Path(a.assets)
    outdir = Path(a.out) / f"{Path(a.canon).parent.name}_scene{a.scene}"
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"scene {a.scene}: {ev['scene']['slug']}  beat={ev['beat']}")
    print(f"shots {len(shots)}  seconds {sum(s['duration'] for s in shots)}")
    print(f"estimated cost ${COST_PER_CLIP * len(shots):.2f}\n")

    # Voice tracks are built BEFORE rendering so they can be passed as
    # reference audio and the model lip-syncs to them.
    voices: dict[int, Path] = {}
    if not a.no_voice:
        sys.path.insert(0, str(Path(__file__).parent))
        from dialogue import build_track
        vdir = outdir / "_voice"
        vdir.mkdir(exist_ok=True)
        for i, shot in enumerate(shots):
            if shot.get("dialogue"):
                t = build_track(shot["dialogue"], story["characters"],
                                shot["duration"], vdir, i, a.dry_run)
                if t:
                    voices[i] = t
        if voices:
            print(f"voice tracks: {len(voices)} synthesized for lip-sync\n")

    clips, metas, prev_last = [], [], None
    for i, shot in enumerate(shots):
        t0 = time.time()
        video, meta = render_shot(shot, ev["beat"], story["style_bible"],
                                  assets, outdir, prev_last, i, a.dry_run,
                                  story["characters"], voices.get(i))
        meta["seconds"] = round(time.time() - t0, 1)
        metas.append(meta)
        inf = meta.get("inference")
        if meta.get("lip_sync"):
            meta["note"] = meta["note"]
        print(f"[{i}] {meta['slug']:22s} {meta['mode']:6s} "
              f"{meta['duration']:>2}s  {meta['note']}"
              f"  wall={meta['seconds']}s"
              + (f" inference={inf:.1f}s" if inf else ""))
        if video:
            clips.append(video)
            prev_last = last_frame(video)

    (outdir / "shots.json").write_text(json.dumps(metas, indent=2))

    if clips:
        final = concat(clips, outdir / "scene.mp4")
        print(f"\n-> {final}")
    elif a.dry_run:
        print("\n(dry run — no spend)")
        for m in metas:
            print(f"  {m['slug']}: request {m['payload_bytes'] / 1024:.1f}KB "
                  f"(+{m['ref_disk_mb']}MB uploaded once)")
            for r in m["refs"]:
                print(f"      {r}")


if __name__ == "__main__":
    main()
