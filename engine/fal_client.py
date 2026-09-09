"""fal client — H3 Max / H3 video generation for THIS WAY.

Why fal in addition to Replicate:
  * H3 Max is fal-EXCLUSIVE (`minimax/h3-max` 404s on Replicate). Its ~3s wall
    time for a 5s clip is what makes the speculative buffer in PRD 5.2 work.
  * H3's `reference-to-video` and first/last-frame modes map 1:1 onto the
    showrunner's ref2v / flf modes.
  * `enable_safety_checker` is an explicit flag. Replicate's seedance rejected
    our own emotion reference plates with E005 "flagged as sensitive" and gave
    no way to scope it.

ENDPOINT IDS HAVE NO `fal-ai/` PREFIX. The owner is `minimax`, so the correct
id is `minimax/h3-max/image-to-video`. Prefixing it yields
`{"detail":"Path /h3-max/text-to-video not found"}` — a 404 that looks like a
result-URL bug and cost several wasted debugging rounds, including a failed
attempt with fal's own official client (which reproduces the same 404, because
the id was wrong, not the client). Discover real ids from
`https://fal.ai/api/models?keywords=<q>` rather than guessing.

The queue's returned response_url/status_url are truncated to the APP path
(`/minimax/h3-max/requests/{id}`) and that form WORKS — build from the app
segment, not the full endpoint id.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

UA = "ThisWayPipeline/1.0"
_op = urllib.request.build_opener()
_op.addheaders = [("User-Agent", UA)]
urllib.request.install_opener(_op)

QUEUE = "https://queue.fal.run"
REST = "https://rest.alpha.fal.ai"

# Modes the showrunner emits -> fal endpoints.
ENDPOINTS = {
    # H3 Max has no text-to-video of its own; the turbo variant does.
    "t2v": "minimax/h3-max-turbo/text-to-video",
    "i2v": "minimax/h3-max/image-to-video",
    # flf is i2v with end_image_url set — both ends pinned.
    "flf": "minimax/h3-max/image-to-video",
    "ref2v": "minimax/h3-max/reference-to-video",
}

# `prompt_expansion_mode` is REQUIRED and enum is
# 'disabled' | 'fast' | 'balanced' | 'quality'  (NOT 'off').
# Keep it DISABLED: expansion paraphrases the prompt, which would rewrite the
# style bible and the exact character names the reference locks depend on.
EXPANSION = "disabled"


def _key() -> str:
    k = os.environ.get("FAL_KEY")
    if not k:
        raise SystemExit("FAL_KEY not set (expected in hermes-agent/.env)")
    return k


def _req(url: str, data: bytes | None = None, method: str | None = None,
         extra: dict | None = None) -> urllib.request.Request:
    h = {"Authorization": f"Key {_key()}"}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(extra or {})
    return urllib.request.Request(url, data=data, headers=h, method=method)


def upload(path: Path) -> str:
    """Upload a local file to fal storage, return its public URL.

    Content type is derived from the extension. Hardcoding image/png sent an
    mp3 voice track up as a PNG and fal rejected the prediction with
    "Unsupported audio format: .png" — the file was fine, the declared type
    was not.
    """
    p = Path(path)
    ctype = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".aac": "audio/aac", ".ogg": "audio/ogg", ".flac": "audio/flac",
        ".mp4": "video/mp4", ".webm": "video/webm",
    }.get(p.suffix.lower())
    if ctype is None:
        raise ValueError(f"unmapped upload type for {p.name}; add it to the map")

    init = json.dumps({"file_name": p.name, "content_type": ctype}).encode()
    with urllib.request.urlopen(
            _req(f"{REST}/storage/upload/initiate?storage_type=fal-cdn-v3",
                 data=init, method="POST"), timeout=120) as r:
        d = json.loads(r.read())

    put = urllib.request.Request(d["upload_url"], data=p.read_bytes(),
                                 method="PUT",
                                 headers={"Content-Type": ctype})
    with urllib.request.urlopen(put, timeout=300):
        pass
    return d["file_url"]


def run(endpoint: str, payload: dict, timeout: int = 900,
        poll: float = 2.0) -> dict:
    """Submit to the queue and poll to completion. Returns the result dict."""
    body = json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(
                _req(f"{QUEUE}/{endpoint}", data=body, method="POST"),
                timeout=120) as r:
            sub = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"fal submit {endpoint} {e.code}: "
                           f"{e.read().decode()[:600]}") from None

    rid = sub.get("request_id")
    if not rid:
        raise RuntimeError(f"fal submit gave no request_id: {json.dumps(sub)[:400]}")
    # Poll/fetch against the APP path, which is what fal itself returns:
    #   minimax/h3-max/image-to-video  ->  minimax/h3-max/requests/{id}
    app = "/".join(endpoint.split("/")[:2])
    base = f"{QUEUE}/{app}/requests/{rid}"
    status_url, resp_url = f"{base}/status", base

    deadline = time.time() + timeout
    st = {}
    while time.time() < deadline:
        with urllib.request.urlopen(_req(status_url), timeout=60) as r:
            st = json.loads(r.read())
        s = st.get("status")
        if s == "COMPLETED":
            try:
                with urllib.request.urlopen(_req(resp_url), timeout=120) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                for alt in (sub.get("response_url"),):
                    if not alt:
                        continue
                    try:
                        with urllib.request.urlopen(_req(alt), timeout=120) as r:
                            return json.loads(r.read())
                    except urllib.error.HTTPError:
                        pass
                if st.get("response") or st.get("video"):
                    return st
                raise RuntimeError(
                    f"fal result fetch {e.code} at {resp_url}: "
                    f"{e.read().decode()[:400]}\n  status: {json.dumps(st)[:400]}"
                ) from None
        if s in ("FAILED", "ERROR", "CANCELLED"):
            raise RuntimeError(f"fal {endpoint} {s}: {json.dumps(st)[:800]}")
        time.sleep(poll)
    raise RuntimeError(f"fal {endpoint} timed out after {timeout}s "
                       f"(last status {st.get('status')})")


def fetch(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(urllib.request.Request(url), timeout=600) as r:
        dest.write_bytes(r.read())
    return dest


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "smoke":
        t0 = time.time()
        out = run(ENDPOINTS["t2v"], {
            "prompt": "A stone lighthouse on a rocky island at dusk, coastal "
                      "fog drifting past, the lamp turning slowly. Anamorphic "
                      "35mm, deep teal shadows, film grain.",
            "prompt_expansion_mode": EXPANSION,
            "duration": 5,
            "resolution": "768P",
            "aspect_ratio": "21:9",
        })
        print(json.dumps(out, indent=2)[:900])
        print(f"wall time {time.time() - t0:.1f}s")
