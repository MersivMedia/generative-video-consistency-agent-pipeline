"""Dialogue mux — ElevenLabs TTS over rendered shots.

H3 Max has NO audio generation parameter. `generate_audio` was a seedance
field, and after porting to fal it sat in the payload being silently ignored:
the resulting tracks were a ~52 BPM percussive ambience bed, not speech.
Spectrogram analysis caught it; the schema confirmed it.

So dialogue is synthesized per character voice and muxed in post, which is
what the PRD specified all along.

Per-character voices come from the story file (`voice_id`), so a character
sounds the same in every shot of every screening.

Usage:
  python dialogue.py renders/<scene_dir> --canon runs/<run>/canon.json --scene 0
  python dialogue.py renders/<scene_dir> --canon ... --scene 0 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request
from pathlib import Path

TTS = "https://api.elevenlabs.io/v1/text-to-speech"
MODEL = "eleven_multilingual_v2"

# fal reference_audio_urls constraints: each clip 2-15s.
MIN_REF_AUDIO = 2.0
MAX_REF_AUDIO = 15.0


def _key() -> str:
    k = os.environ.get("ELEVENLABS_API_KEY")
    if not k:
        raise SystemExit("ELEVENLABS_API_KEY not set")
    return k


def tts(text: str, voice_id: str, out: Path) -> Path:
    body = json.dumps({
        "text": text,
        "model_id": MODEL,
        # Higher stability keeps a character's delivery consistent across
        # shots; a drifting performance is the audio equivalent of face drift.
        "voice_settings": {"stability": 0.55, "similarity_boost": 0.85,
                           "style": 0.25, "use_speaker_boost": True},
    }).encode()
    req = urllib.request.Request(
        f"{TTS}/{voice_id}", data=body,
        headers={"xi-api-key": _key(), "Content-Type": "application/json",
                 "Accept": "audio/mpeg"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        out.write_bytes(r.read())
    return out


def duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of",
                        "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def build_track(lines: list[dict], chars: dict, shot_len: float,
                workdir: Path, idx: int, dry: bool) -> Path | None:
    """Concatenate a shot's dialogue lines into one track, padded to length.

    Lines are laid end to end with a short beat between them. If the speech
    overruns the shot, that is reported rather than silently truncated — it
    means the showrunner wrote more dialogue than 5 seconds can hold, which
    is a story-engine problem, not an audio one.
    """
    if not lines:
        return None
    parts: list[Path] = []
    total = 0.0
    for j, ln in enumerate(lines):
        ch = chars[ln["character"]]
        seg = workdir / f"s{idx:02d}_l{j}.mp3"
        if dry:
            print(f"      [{ch['name']}] ({ch['voice_id']}) {ln['line'][:60]}")
            continue
        tts(ln["line"], ch["voice_id"], seg)
        total += duration(seg)
        parts.append(seg)

    if dry:
        return None

    if total > shot_len + 0.75:
        print(f"      WARNING dialogue {total:.1f}s exceeds shot {shot_len:.1f}s "
              "— shorten the lines or lengthen the shot")

    lst = workdir / f"s{idx:02d}_list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    joined = workdir / f"s{idx:02d}_voice.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i",
                    str(lst), "-c", "copy", str(joined)],
                   check=True, capture_output=True)

    # fal's reference_audio_urls requires >= 2.0s and a single short line can
    # be 1.57s ("Who sent you?"). Pad with trailing silence out to the shot
    # duration: it clears the minimum AND gives the model audio spanning the
    # whole clip, so the mouth is not still talking after the track ends.
    have = duration(joined)
    want = max(MIN_REF_AUDIO, min(shot_len, MAX_REF_AUDIO))
    if have < want:
        padded = workdir / f"s{idx:02d}_voice_pad.mp3"
        subprocess.run([
            "ffmpeg", "-y", "-i", str(joined),
            "-af", f"apad=whole_dur={want:.2f}",
            "-c:a", "libmp3lame", "-q:a", "4", str(padded)],
            check=True, capture_output=True)
        print(f"      padded voice {have:.2f}s -> {duration(padded):.2f}s")
        return padded
    return joined


def mux(video: Path, voice: Path | None, out: Path) -> Path:
    """Replace the model's ambience bed with dialogue over it.

    The generated track is kept at low level as room tone and the dialogue
    sits on top, so the shot does not go dead when a character stops talking.
    """
    if voice is None:
        subprocess.run(["ffmpeg", "-y", "-i", str(video), "-c", "copy",
                        str(out)], check=True, capture_output=True)
        return out
    subprocess.run([
        "ffmpeg", "-y", "-i", str(video), "-i", str(voice),
        "-filter_complex",
        "[0:a]volume=0.25[amb];[1:a]volume=1.0,adelay=250|250[vo];"
        "[amb][vo]amix=inputs=2:duration=first:dropout_transition=0[a]",
        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
        "-shortest", str(out)], check=True, capture_output=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scene_dir")
    ap.add_argument("--canon", required=True)
    ap.add_argument("--scene", type=int, default=0)
    ap.add_argument("--story", default="stories/the_signal.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    story = json.loads(Path(a.story).read_text())
    chars = story["characters"]
    canon = [e for e in json.loads(Path(a.canon).read_text())
             if e["type"] == "canon"]
    ev = canon[a.scene]
    shots = ev["head_shots"] + ev["tail_shots"]

    d = Path(a.scene_dir)
    work = d / "_audio"
    work.mkdir(exist_ok=True)
    outs: list[Path] = []

    for i, shot in enumerate(shots):
        vid = d / f"shot_{i:02d}.mp4"
        if not vid.exists():
            print(f"[{i}] {shot['slug']}: no video, skipped")
            continue
        lines = shot.get("dialogue", [])
        print(f"[{i}] {shot['slug']}  {len(lines)} line(s)")
        voice = build_track(lines, chars, shot["duration"], work, i, a.dry_run)
        if a.dry_run:
            continue
        out = d / f"shot_{i:02d}_dub.mp4"
        outs.append(mux(vid, voice, out))
        print(f"      -> {out.name}")

    if outs:
        lst = d / "dub_list.txt"
        lst.write_text("".join(f"file '{p.resolve()}'\n" for p in outs))
        final = d / "scene_dub.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i",
                        str(lst), "-c", "copy", str(final)],
                       check=True, capture_output=True)
        print(f"\n-> {final}")
    elif a.dry_run:
        print("\n(dry run — no TTS calls made)")


if __name__ == "__main__":
    main()
