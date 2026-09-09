"""Cutaway library — the fallback that makes a missed deadline invisible.

A live screening has a hard constraint the rest of the pipeline does not: the
next clip must exist before the current one ends. Generation is fast (~5.7s
inference for a 5s clip) but it is not guaranteed, and a provider hiccup, a
safety rejection or a queue backup will eventually land inside a vote window.

The answer is not a longer buffer — it is having something honest to cut to.

WHAT A CUTAWAY IS
    A short, character-free, location-true shot that can follow ANY shot and
    precede ANY shot in the same location: the lamp mechanism turning, rain on
    a window, the sea against rocks, a radio dial, a corridor. Because no
    character appears, it cannot break identity continuity. Because it is
    generated from the same location plates, it cannot break the world.

WHY CHARACTER-FREE IS THE WHOLE TRICK
    Every drift failure this pipeline has measured was a character failure:
    faces aging, wardrobe changing, blocking re-staging. A cutaway sidesteps
    all of it. It is the one shot type that is safe to reuse.

WHEN IT PLAYS
    The scheduler asks for the next clip. If it is not ready within the
    deadline, play a cutaway instead and keep the real clip generating. The
    audience sees a deliberate edit, not a spinner. Film language absorbs this
    completely — cutting to the lamp while someone decides is normal grammar.

REUSE POLICY
    Cutaways are generated ONCE per location, offline, and reused across
    screenings. Track usage so the same clip does not appear twice in one
    screening; that is the only way a cutaway reads as a fallback rather than
    as a cut.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fal_client as fal  # noqa: E402

# Per-location cutaway subjects. Deliberately concrete and mechanical — a
# cutaway must be unambiguous about WHERE it is, and contain no people.
SUBJECTS = [
    ("lamp", "the rotating lighthouse lamp mechanism seen close, brass and "
             "glass prisms turning slowly, the beam sweeping past the lens"),
    ("sea", "black water breaking against wet rock below, spray caught in the "
            "sweeping beam, no horizon line visible"),
    ("rain", "rain running down a salt-crusted window pane, the light beyond "
             "diffused into soft bloom"),
    ("detail", "a weathered practical detail of the location — worn handrail, "
               "peeling paint, a bolted hatch — held in shallow focus"),
    ("wide", "an empty wide of the location with no person present, fog "
             "drifting through the frame"),
]

# 5s is the provider FLOOR, not a choice: H3 Max rejects duration<5 with
# "Input should be greater than or equal to 5". Also the shortest clip that
# reads as a deliberate cut rather than a glitch.
DURATION = 5
NEG = ("Absolutely NO people, NO figures, NO silhouettes, NO hands, no human "
       "presence of any kind anywhere in frame.")


def cutaway_prompt(location_desc: str, subject: str, style: str) -> str:
    return (f"{subject}. Location: {location_desc}. {style} {NEG}")


def build(story_path: Path, beat_id: str, dry: bool = False) -> list[Path]:
    """Generate the cutaway set for one location.

    One API call per subject — batching a 'N distinct' request returns N
    independent rolls of the same prompt, which is how three earlier asset
    types silently failed.
    """
    story = json.loads(story_path.read_text())
    beats = [b for ch in story["chapters"] for b in ch["beats"]]
    beat = next((b for b in beats if b["id"] == beat_id), None)
    if beat is None:
        raise SystemExit(f"no beat {beat_id!r}; have {[b['id'] for b in beats]}")
    style = story.get("style_bible", "")
    # Beats carry a premise rather than an explicit location field; the premise
    # plus the world facts is what the location plates were generated from, so
    # it is the right grounding text for a cutaway too.
    location = beat.get("location") or beat["premise"]

    out = Path("assets/cutaways") / beat_id
    out.mkdir(parents=True, exist_ok=True)

    # Location plates ground the cutaway in the same world.
    plates = sorted(Path("assets/plates", beat_id).glob("plate_*.png"))[:3]
    if not plates and not dry:
        raise SystemExit(f"no location plates for {beat_id}; run preprod plates first")

    made = []
    for name, subject in SUBJECTS:
        dest = out / f"{name}.mp4"
        prompt = cutaway_prompt(location, subject, style)
        if dry:
            print(f"  [{name}] {DURATION}s  refs={len(plates)}")
            print(f"      {prompt[:150]}...")
            continue
        if dest.exists():
            print(f"  [{name}] exists, skipping")
            made.append(dest)
            continue
        print(f"  [{name}] generating {DURATION}s...")
        # image-to-video from a location plate: no characters means no identity
        # locks are needed, so the cheap turbo endpoint is fine here.
        payload = {
            "prompt": prompt,
            "image_url": fal.upload(plates[0]),
            "duration": DURATION,
            "resolution": "768P",
            "prompt_expansion_mode": "disabled",
        }
        res = fal.run("minimax/h3-max/image-to-video", payload, timeout=900)
        url = (res.get("video") or {}).get("url")
        if not url:
            raise RuntimeError(f"no video in result: {json.dumps(res)[:300]}")
        fal.fetch(url, dest)
        made.append(dest)
    return made


class CutawayLibrary:
    """Runtime picker. Never returns the same clip twice in one screening."""

    def __init__(self, root: Path = Path("assets/cutaways")):
        self.root = Path(root)
        self.used: set[Path] = set()

    def available(self, beat_id: str) -> list[Path]:
        return sorted((self.root / beat_id).glob("*.mp4"))

    def take(self, beat_id: str, rng: random.Random | None = None) -> Path | None:
        """Pick an unused cutaway for this location, else any location.

        Falling back across locations is deliberate: a slightly wrong-place
        cutaway of rain or sea is far less damaging than a stall. Returns None
        when the library is exhausted, which the caller must treat as a real
        error — that is a signal to generate more, not to stall silently.
        """
        rng = rng or random
        pool = [p for p in self.available(beat_id) if p not in self.used]
        if not pool:
            pool = [p for p in self.root.rglob("*.mp4") if p not in self.used]
        if not pool:
            return None
        pick = rng.choice(pool)
        self.used.add(pick)
        return pick

    def reset(self) -> None:
        self.used.clear()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("story", type=Path)
    ap.add_argument("--beat", required=True,
                    help="beat id whose location gets a cutaway set")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    print(f"cutaways for {a.beat}"
          f"{'  [DRY RUN]' if a.dry_run else ''}")
    made = build(a.story, a.beat, a.dry_run)
    if not a.dry_run:
        print(f"-> {len(made)} cutaways in assets/cutaways/{a.beat}/")


if __name__ == "__main__":
    main()
