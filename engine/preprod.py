"""Pre-production asset factory via Replicate REST API — no local GPU needed.

Three stages, each feeding the next:

  1. anchor      one full-body canonical image per character (text->image)
  2. sheet       multi-angle full body + face/emotion closeups (anchor as reference)
  3. plates      location plates at several angles and ranges

Stage 2 and 3 pass the anchor/plate back in as `image_input`, so identity and
location locks propagate instead of being re-invented per frame.

Models (verified live on Replicate):
  bytedance/seedream-4    sequential_image_generation='auto' + max_images<=15
                          -> a whole consistent SET from one call. 1-10 refs in.
  google/nano-banana-pro  up to 14 reference images, 1K/2K/4K. Fallback / retouch.

Usage:
  python engine/preprod.py anchor  stories/the_signal.json --character wrenn
  python engine/preprod.py sheet   stories/the_signal.json --character wrenn
  python engine/preprod.py plates  stories/the_signal.json --beat b1_arrival
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import time
import urllib.request
from pathlib import Path

UA = "ThisWayPipeline/1.0"
_op = urllib.request.build_opener()
_op.addheaders = [("User-Agent", UA)]
urllib.request.install_opener(_op)

API = "https://api.replicate.com/v1"
SEEDREAM = "bytedance/seedream-4"
NANO = "google/nano-banana-pro"


def _token() -> str:
    t = os.environ.get("REPLICATE_API_TOKEN")
    if not t:
        raise SystemExit("REPLICATE_API_TOKEN not set")
    return t


def data_uri(p: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


_UPLOAD_CACHE: dict[str, str] = {}


def upload_file(p: Path) -> str:
    """Upload once, reuse the URL. Returns a Replicate-hosted URL.

    Base64 data-URIs are fine for one or two images but 9x 2K PNGs inflate a
    request to ~29MB against Replicate's ~10MB limit. The files API keeps the
    prediction payload tiny and makes repeated reference use free, since the
    same locks are passed to every shot in a scene.
    """
    key = str(p.resolve())
    if key in _UPLOAD_CACHE:
        return _UPLOAD_CACHE[key]

    import uuid
    tok = _token()
    boundary = f"----ThisWay{uuid.uuid4().hex}"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="content"; filename="')
    body.extend(p.name.encode())
    body.extend(b'"\r\n')
    body.extend(b"Content-Type: image/png\r\n\r\n")
    body.extend(p.read_bytes())
    body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())

    req = urllib.request.Request(
        f"{API}/files", data=bytes(body),
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        url = json.loads(r.read())["urls"]["get"]
    _UPLOAD_CACHE[key] = url
    return url


def predict(model: str, inp: dict, timeout: int = 600) -> list[str]:
    """Submit and poll. Returns output URLs."""
    tok = _token()
    body = json.dumps({"input": inp}).encode()
    # Prefer:wait 403s on bodies >~1MB, so only use it when the payload is small.
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    if len(body) < 900_000:
        headers["Prefer"] = "wait=60"

    req = urllib.request.Request(f"{API}/models/{model}/predictions",
                                 data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read())

    pid, status, out = d["id"], d.get("status"), d.get("output")
    pd = d
    deadline = time.time() + timeout
    while status not in ("succeeded", "failed", "canceled") and time.time() < deadline:
        time.sleep(3)
        pr = urllib.request.Request(f"{API}/predictions/{pid}",
                                    headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(pr, timeout=30) as r:
            pd = json.loads(r.read())
        status, out = pd.get("status"), pd.get("output")

    if status != "succeeded":
        # Surface everything the API said. A bare "failed:" wastes a whole
        # debugging cycle — `logs` usually contains the real reason.
        raise RuntimeError(
            f"{model} {status}\n"
            f"  error: {pd.get('error')}\n"
            f"  logs: {(pd.get('logs') or '')[-800:]}\n"
            f"  id: {pid}")
    return [out] if isinstance(out, str) else list(out or [])


def download(urls: list[str], outdir: Path, stem: str) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, u in enumerate(urls):
        p = outdir / f"{stem}_{i:02d}.png"
        p.write_bytes(urllib.request.urlopen(u, timeout=120).read())
        paths.append(p)
    return paths


# ------------------------------------------------------------------ prompts

# The exact grey value is repeated verbatim in every call. Separate calls
# otherwise drift between white and mid-grey studios (observed backdrop luma
# spread of 102/255 across one 5-image set), and a video model inherits those
# inconsistent lighting cues.
NEUTRAL = (
    "BACKDROP: one single flat seamless mid-grey backdrop, exactly 50 percent grey, "
    "hex #808080, RGB 128 128 128, filling the entire frame edge to edge behind and "
    "beneath the subject. The backdrop must be EXACTLY this mid-grey — not white, "
    "not light grey, not dark grey. No horizon line, no visible floor-to-wall seam, "
    "no ground shadow beneath the feet.\n"
    "LIGHTING: completely flat, even, white-balanced frontal illumination, identical "
    "brightness across the whole frame. No key light, no directional shadows, no "
    "vignette, no falloff, no coloured gels, no warm amber cast, no teal or cyan "
    "cast, no film grain, no cinematic colour grading. Clean technical reference "
    "plate lighting only, like a passport photo studio."
)

def anchor_prompt(ch: dict) -> str:
    """NOTE: the style bible is deliberately EXCLUDED here.

    Passing a cinematic grade into the anchor bakes coloured light into the
    plate, and because the sheet is generated i2i FROM the anchor, that cast
    propagates into every reference image. Reference plates must be neutral;
    the grade belongs on the shot prompt, not the lock.

    `appearance` and `costume` are AUTHORED CANON from the story file and are
    injected VERBATIM. Without them the model invents clothing per call and
    there is no ground truth to validate a plate against — a whole render
    cycle was spent auditing plates against a costume spec that never existed.
    """
    return (
        f"Full body character reference photograph of {ch['name']}, {ch['role']}.\n"
        f"APPEARANCE (must match exactly): {ch['appearance']}\n"
        f"COSTUME (must match exactly, these colours are mandatory): {ch['costume']}\n"
        "POSE: standing straight, neutral relaxed pose, arms at sides, facing camera "
        "directly, head to toe fully in frame with margin above the head and below the "
        "feet, feet clearly visible. Sharp focus, full costume and footwear clearly "
        "visible, no props other than those named in the costume, no set dressing, "
        "no text.\n"
        f"{NEUTRAL}"
    )


# ---------------------------------------------------------------- angles
#
# THREE-QUARTER VIEWS ARE NOT RELIABLY PROMPTABLE. Verified over three
# generations and two characters, with escalating prompt specificity
# (degrees -> shoulder-line percentage -> explicit both-eyes-visible +
# anti-head-turn language + one API call per angle):
#
#   tq_left   over-rotates toward profile     every attempt
#   tq_right  under-rotates toward front      every attempt
#
# Pixel evidence for the skew rather than a collapse: front vs tq_right is
# the CLOSEST pair in the set (8.58 on wrenn), while tq_left vs profile sits
# mid-pack. The ladder is skewed in opposite directions at both 45deg slots.
#
# front / profile / back PASSED on every character in every generation. So
# ship those and drop the 45s, exactly as backdrop brightness was moved out
# of the prompt and into a post-process: stop asking the model for something
# it does not reliably do.
#
# Cost of dropping them is low. seedance-2.0 accepts up to 9 reference_images;
# 3 solid body angles + 4 emotion plates = 7 verified locks. A wrong 3/4 plate
# is worse than a missing one, because it teaches the video model an identity
# at an angle the character never actually holds.
#
# Revisit only with a model offering explicit camera control (or a 3D/novel-
# view-synthesis step), not with more prompt language.
RELIABLE = {"front", "profile_left", "back"}

ANGLES = [
    ("front", "FRONT VIEW. Body squared directly to camera, chest fully facing the "
              "lens. BOTH shoulders equally visible and equally distant from camera, "
              "shoulder line perfectly horizontal and at its FULL WIDTH across frame. "
              "Full face visible, both eyes, both ears, nose pointing straight at the "
              "lens."),
    ("tq_left", "THREE-QUARTER LEFT VIEW. The body is rotated so the shoulder line is "
                "at roughly SEVENTY PERCENT of its full front-on width — clearly "
                "narrower than a front view but still obviously wide. Both shoulders "
                "remain visible, the near shoulder larger than the far shoulder. Both "
                "eyes visible, both sides of the nose visible, the far cheek partly "
                "visible. The head faces the SAME direction as the chest — do NOT turn "
                "the head back toward the camera. This is HALFWAY between front and "
                "side. It is NOT a profile: if only one eye is visible it is WRONG."),
    ("profile_left", "FULL LEFT PROFILE. Body rotated a full quarter turn, perfectly "
                     "side-on. The shoulder line is at its NARROWEST, near shoulder "
                     "completely hiding the far shoulder. Exactly ONE eye and ONE ear "
                     "visible, nose in clean silhouette pointing at the edge of frame. "
                     "The head faces the same direction as the chest — do NOT turn the "
                     "head back toward the camera."),
    ("tq_right", "THREE-QUARTER RIGHT VIEW — the MIRROR IMAGE of a three-quarter left. "
                 "The body is rotated to the OPPOSITE side, facing the other edge of "
                 "frame. Shoulder line at roughly SEVENTY PERCENT of full front-on "
                 "width, both shoulders visible, near shoulder larger. Both eyes "
                 "visible, far cheek partly visible. The head faces the SAME direction "
                 "as the chest — do NOT turn the head back over the shoulder toward "
                 "camera. This is HALFWAY between front and side, NOT a profile, and "
                 "NOT a look-back-over-the-shoulder pose."),
    ("back", "BACK VIEW. Body rotated a full half turn, facing directly AWAY from "
             "camera. The back of the head and the back of the costume fill the frame, "
             "shoulder line at FULL WIDTH again. NO face visible at all, no eyes, no "
             "nose, no cheek. The head must NOT be turned over the shoulder."),
]



BODY_BASE = (
    "Full body reference plate of THIS EXACT PERSON from the reference image. "
    "Identical face, hair, build, costume and footwear as the reference.\n"
    "{costume_line}"
    "FRAMING: full body, head to toe entirely inside the frame with clear margin "
    "above the head and below the feet, both feet fully visible, neutral relaxed "
    "standing pose, arms hanging at the sides, weight evenly on both feet.\n"
    "Exactly ONE single image. No collage, no grid, no panels, no text, no labels, "
    "no watermarks, no props, no set dressing.\n"
)

FACE_BASE = (
    "Face close-up reference plate of THIS EXACT PERSON from the reference image. "
    "Identical face structure, hair, and costume collar as the reference.\n"
    "{costume_line}"
    "FRAMING: TIGHT HEAD AND SHOULDERS close-up, front on, filling the frame. Not a "
    "full body, not a medium shot. The eyes look DIRECTLY INTO THE CAMERA LENS — no "
    "downcast eyes, no averted gaze, no closed eyes.\n"
    "AGE: the person must look the SAME AGE as the reference image, and no older. Do "
    "not add wrinkles, do not deepen lines, do not age the face. Keep skin texture "
    "identical to the reference.\n"
    "Exactly ONE single image. No collage, no grid, no panels, no text, no labels.\n"
)

# Emotion briefs describe MUSCLE ACTION, not emotion labels. Labels alone yield
# generic or converging expressions; "grief" in particular renders as bruise-like
# eye rings unless the mechanism is redirected away from socket shading.
EMOTIONS = [
    ("guarded",
     "EXPRESSION — GUARDED AND WITHHOLDING. This is NOT blank, relaxed or neutral. "
     "The lower eyelids are tightened and slightly raised so the eyes read narrowed "
     "and appraising. The jaw is clenched. The lips are pressed into a flat closed "
     "line. The chin is very slightly lifted. Reading the person in front of them and "
     "deciding what not to say. Suspicion held behind a still face."),
    ("fear",
     "EXPRESSION — FEAR AND ALARM. The eyes are WIDE OPEN with white sclera visible "
     "above the iris. The inner ends of the eyebrows are pulled UP and together. The "
     "upper eyelids are raised as far as they go. The mouth is OPEN, lips parted, jaw "
     "dropped slightly. The neck tendons are tensed. Startled and afraid."),
    ("grief",
     "EXPRESSION — GRIEF AND EXHAUSTION. Convey this ENTIRELY through a SLACK OPEN "
     "MOUTH, HEAVY DROOPING UPPER EYELIDS at half-mast, a LOOSE UNCLENCHED JAW, and "
     "the inner eyebrows pulled up and together. The face is drained of energy. "
     "CRITICAL: the skin under and around the eyes must be CLEAN, EVEN and the SAME "
     "TONE as the cheeks. NO dark rings around the eyes, NO shadowed or sunken eye "
     "sockets, NO purple, mauve, blue or grey discolouration, NO tears, NO tear "
     "streaks, NO wet or glistening eyes. The face must NOT look bruised, beaten, "
     "injured or ill."),
    ("anger",
     "EXPRESSION — HARD COLD ANGER. The eyebrows are pulled DOWN and together with a "
     "deep vertical crease between them. The eyes are narrowed to a hard direct "
     "stare. The mouth is a TIGHT DOWNTURNED closed line with the lips pressed "
     "together. The nostrils are slightly flared. Cold controlled fury, not shouting, "
     "not hot rage — no open mouth, no bared teeth."),
]


def plate_spec(beat: dict, facts: list[str], style: str) -> str:
    return (
        f"Generate a consistent location reference set of exactly 6 images of ONE SINGLE "
        f"LOCATION. Every image is unmistakably the same place with identical architecture, "
        f"materials, weather and time of day.\n"
        f"Location: {beat['premise']}\n"
        f"World context: {' '.join(facts[:4])}\n"
        "No people, no figures, no characters anywhere in any image. Empty location only.\n"
        "(1) wide establishing shot, full location in frame; "
        "(2) wide shot from the opposite side, 180 degrees around; "
        "(3) medium shot, mid-range, main feature centred; "
        "(4) medium shot from a low angle looking up; "
        "(5) tight detail insert of a key surface or object in the location; "
        "(6) high angle looking down over the location.\n"
        f"Consistent grade and lighting throughout: {style}\n"
        "No text, no labels, no collage — each image separate."
    )


# ------------------------------------------------------------------ stages

def run_anchor(story: dict, key: str, outdir: Path) -> Path:
    ch = story["characters"][key]
    urls = predict(SEEDREAM, {
        "prompt": anchor_prompt(ch),
        "size": "2K", "aspect_ratio": "2:3",
        "sequential_image_generation": "disabled",
    })
    p = download(urls, outdir / "character" / key, "anchor")[0]
    print(f"anchor -> {p}")
    return p


def _expect(urls: list[str], want: int, label: str) -> list[str]:
    """`auto` mode silently under-delivers. Fail loudly instead of shipping a
    half-built identity lock into the video stage."""
    if len(urls) != want:
        raise RuntimeError(
            f"{label}: expected {want} images, provider returned {len(urls)}. "
            "Re-run this stage; do not proceed with an incomplete reference set.")
    return urls


def run_sheet(story: dict, key: str, outdir: Path) -> list[Path]:
    """One call PER ANGLE for the body turnaround, one batched call for faces.

    Per-angle calls cost ~5x a single batched call in requests but only ~$0.02
    each, and they buy determinism: a batched "5 distinct angles" request
    collapsed the ladder in every observed run. Cheap insurance.
    """
    anchor = outdir / "character" / key / "anchor_00.png"
    if not anchor.exists():
        anchor = run_anchor(story, key, outdir)
    ch = story["characters"][key]
    who = f"The person is {ch['name']}, {ch['role']}."
    costume_line = (f"COSTUME (mandatory, unchanged in every image): "
                    f"{ch['costume']}\n")
    ref = [data_uri(anchor)]
    dest = outdir / "character" / key
    paths: list[Path] = []

    angles = [a for a in ANGLES if a[0] in RELIABLE]
    for i, (name, brief) in enumerate(angles):
        urls = predict(SEEDREAM, {
            "prompt": (BODY_BASE.format(costume_line=costume_line) + who +
                       f"\n\nCAMERA ANGLE — {brief}\n\n{NEUTRAL}"),
            "image_input": ref,
            "size": "2K", "aspect_ratio": "2:3",
            "sequential_image_generation": "disabled",
        })
        p = download(_expect(urls, 1, f"{key} {name}"), dest, f"body_{i}")[0]
        p.rename(dest / f"body_{i:02d}.png")
        print(f"  body {i} {name:14s} ok")
    paths += sorted(dest.glob("body_*.png"))
    print(f"  body  -> {len(angles)} verified angles "
          f"(45-degree three-quarters dropped, see ANGLES note)")

    for i, (name, brief) in enumerate(EMOTIONS):
        urls = predict(SEEDREAM, {
            "prompt": (FACE_BASE.format(costume_line=costume_line) + who +
                       f"\n\n{brief}\n\n{NEUTRAL}"),
            "image_input": ref,
            "size": "2K", "aspect_ratio": "1:1",
            "sequential_image_generation": "disabled",
        })
        q = download(_expect(urls, 1, f"{key} {name}"), dest, f"tmpf_{i}")[0]
        q.replace(dest / f"face_{i:02d}.png")
        print(f"  face {i} {name:10s} ok")
    paths += sorted(dest.glob("face_*.png"))

    print(f"sheet -> {len(paths)} images in {dest}")
    return paths


def run_plates(story: dict, beat_id: str, outdir: Path) -> list[Path]:
    beat = next(b for ch in story["chapters"] for b in ch["beats"]
                if b["id"] == beat_id)
    urls = predict(SEEDREAM, {
        "prompt": plate_spec(beat, story["world_facts"], story["style_bible"]),
        "size": "2K", "aspect_ratio": "21:9",
        "sequential_image_generation": "auto", "max_images": 6,
    })
    paths = download(_expect(urls, 6, f"{beat_id} plates"),
                     outdir / "plates" / beat_id, "plate")
    print(f"plates -> {len(paths)} images in {paths[0].parent}")
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["anchor", "sheet", "plates"])
    ap.add_argument("story")
    ap.add_argument("--character")
    ap.add_argument("--beat")
    ap.add_argument("--out", default="assets")
    a = ap.parse_args()

    story = json.loads(Path(a.story).read_text())
    out = Path(a.out)
    t0 = time.time()

    if a.stage == "anchor":
        run_anchor(story, a.character, out)
    elif a.stage == "sheet":
        run_sheet(story, a.character, out)
    else:
        run_plates(story, a.beat, out)
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
