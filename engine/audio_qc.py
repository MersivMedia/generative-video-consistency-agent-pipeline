"""Speech detection for model-generated audio — MEASURED AND REJECTED.

KEEP THIS FILE AS A RECORD OF A METRIC THAT DOES NOT WORK.

The video model always returns an audio track and offers no mute parameter, so
when nothing forbids speech it invents its own: muttering, voice-over, crowd
murmur over shots with no dialogue. The obvious defence is to detect speech in
the returned audio and strip it. That defence does not work.

THE HYPOTHESIS
    Speech = strong 300-3400 Hz energy MODULATED at syllable rate (2-8 Hz).
    Wind, rain and sea share the band but are stationary, so their envelope
    should barely move. Modulation index should therefore separate them.

MEASURED ON LABELLED CLIPS — IT DOES NOT SEPARATE
    shot_00 (no dialogue)        0.014
    shot_01 (ElevenLabs speech)  0.155
    shot_02 (ElevenLabs speech)  0.173
    shot_03 (ElevenLabs speech)  0.106
    shot_04 (ElevenLabs speech)  0.135
    lamp    (pure ambience)      0.102
    sea     (pure ambience)      0.157
    rain    (pure ambience)      0.228   <-- HIGHER THAN EVERY SPEECH CLIP

    Rain is amplitude-modulated at almost exactly syllable rate: individual
    drops and gusts fluctuate 2-8 times a second. Any threshold that catches
    speech at 0.106 destroys rain, sea and wind — the exact ambience the model
    is supposed to provide. The bands overlap completely; there is no cut.

WHY IT IS SHIPPED DISABLED
    Same rule as the angle metric in qc.py: a gate that silently mis-scores is
    worse than an acknowledged gap, because the gap gets a human check while
    the bad gate gets believed. `analyse()` remains available for reporting,
    never for gating.

WHAT ACTUALLY DEFENDS AGAINST INVENTED SPEECH
    1. An explicit AUDIO directive in every shot prompt (render_scene.py):
       diegetic background only, no speech / voice-over / narrator / muttering,
       and for dialogue-free shots an extra "nobody talks" clause.
    2. All real dialogue comes from ElevenLabs and is passed to the model as
       reference audio, so the model has a voice track to lip-sync rather than
       a vacuum to fill.
    3. `--lowpass` below: a deterministic 250 Hz low-pass that destroys vocal
       intelligibility while keeping sea, wind and rumble. Costs air and rain
       detail, so it is opt-in per shot rather than automatic.
"""
from __future__ import annotations

import argparse
import math
import struct
import subprocess
import wave
from pathlib import Path

SPEECH_LO, SPEECH_HI = 300.0, 3400.0
SYLLABLE_LO, SYLLABLE_HI = 2.0, 8.0      # Hz — the rate mouths open and close
WINDOW = 0.05                             # 50ms envelope frames
MOD_THRESHOLD = 0.28   # DISABLED — measured non-discriminating, see docstring


def _pcm(path: Path, sr: int = 16000) -> list[float]:
    """Decode any media file to mono float samples via ffmpeg."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr),
         "-f", "wav", "-"], capture_output=True, check=True).stdout
    import io
    with wave.open(io.BytesIO(out)) as w:
        raw = w.readframes(w.getnframes())
    n = len(raw) // 2
    return [v / 32768.0 for v in struct.unpack(f"<{n}h", raw[:n * 2])]


def _bandpass_envelope(x: list[float], sr: int = 16000) -> list[float]:
    """Energy envelope of the speech band, in 50ms frames.

    A one-pole high-pass then low-pass is enough to isolate 300-3400 Hz for a
    detector; this is not a mastering filter and does not need to be steep.
    """
    # high-pass at SPEECH_LO
    a_hp = math.exp(-2 * math.pi * SPEECH_LO / sr)
    hp, prev_x, prev_y = [], 0.0, 0.0
    for s in x:
        y = a_hp * (prev_y + s - prev_x)
        hp.append(y)
        prev_x, prev_y = s, y
    # low-pass at SPEECH_HI
    a_lp = 1.0 - math.exp(-2 * math.pi * SPEECH_HI / sr)
    band, y = [], 0.0
    for s in hp:
        y += a_lp * (s - y)
        band.append(y)

    frame = int(sr * WINDOW)
    env = []
    for i in range(0, len(band) - frame, frame):
        chunk = band[i:i + frame]
        env.append(math.sqrt(sum(v * v for v in chunk) / len(chunk)))
    return env


def modulation_index(env: list[float]) -> float:
    """How much of the envelope's variation sits at syllable rate.

    Computed as the share of envelope energy in the 2-8 Hz band via a direct
    Goertzel-style sum — no numpy dependency, and the frame count is tiny.
    """
    if len(env) < 8:
        return 0.0
    mean = sum(env) / len(env)
    if mean <= 1e-9:
        return 0.0
    dev = [e - mean for e in env]
    fs = 1.0 / WINDOW                      # envelope sample rate, 20 Hz
    total = sum(d * d for d in dev) or 1e-12

    band = 0.0
    k_lo = max(1, int(SYLLABLE_LO / fs * len(dev)))
    k_hi = min(len(dev) // 2, int(SYLLABLE_HI / fs * len(dev)) + 1)
    for k in range(k_lo, max(k_lo + 1, k_hi)):
        re = sum(d * math.cos(2 * math.pi * k * n / len(dev))
                 for n, d in enumerate(dev))
        im = sum(d * math.sin(2 * math.pi * k * n / len(dev))
                 for n, d in enumerate(dev))
        band += (re * re + im * im) / len(dev)
    return min(1.0, band / total)


def analyse(path: Path) -> dict:
    x = _pcm(path)
    env = _bandpass_envelope(x)
    mod = modulation_index(env)
    # NOTE: `speech_likely` is reported for information only. It was measured
    # non-discriminating against labelled clips (rain scores above speech) and
    # must not be used to gate anything.
    return {"file": path.name, "modulation": round(mod, 3),
            "speech_likely_UNRELIABLE": mod > MOD_THRESHOLD,
            "frames": len(env)}


def quiet_windows(path: Path, want: float) -> list[tuple[float, float]]:
    """Find the least speech-like spans, for rebuilding a clean bed."""
    x = _pcm(path)
    env = _bandpass_envelope(x)
    span = max(4, int(1.0 / WINDOW))       # 1s candidate windows
    scored = []
    for i in range(0, max(1, len(env) - span), span // 2):
        chunk = env[i:i + span]
        if len(chunk) < span:
            break
        scored.append((modulation_index(chunk), i * WINDOW, span * WINDOW))
    scored.sort()
    picked, total = [], 0.0
    for _, start, dur in scored:
        picked.append((start, dur))
        total += dur
        if total >= want:
            break
    return picked


def strip_speech(src: Path, dest: Path, duration: float) -> dict:
    """Rebuild a speech-free ambience bed from the clip's own quiet windows."""
    wins = quiet_windows(src, duration)
    if not wins:
        raise RuntimeError(f"{src.name}: no clean audio to rebuild from")
    tmp = dest.parent / f"_{dest.stem}_parts"
    tmp.mkdir(exist_ok=True)
    parts = []
    for i, (start, dur) in enumerate(wins):
        q = tmp / f"p{i:02d}.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.2f}",
                        "-t", f"{dur:.2f}", "-i", str(src), "-ac", "2",
                        "-af", "afade=t=in:d=0.05,afade=t=out:st="
                               f"{max(0, dur - 0.05):.2f}:d=0.05",
                        str(q)], check=True, capture_output=True)
        parts.append(q)
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-af", f"apad=whole_dur={duration:.2f}",
                    "-t", f"{duration:.2f}", str(dest)],
                   check=True, capture_output=True)
    return {"windows": len(wins), "out": str(dest)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--threshold", type=float, default=MOD_THRESHOLD)
    ap.add_argument("--lowpass", type=Path, default=None, metavar="DEST",
                    help="deterministic fallback: low-pass the first input "
                         "below 250 Hz, killing vocal intelligibility while "
                         "keeping sea, wind and rumble")
    a = ap.parse_args()
    if a.lowpass:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(a.paths[0]),
                        "-af", "lowpass=f=250,volume=1.4", "-c:a", "aac",
                        str(a.lowpass)], check=True)
        print(f"low-passed -> {a.lowpass}")
        return
    print(f"{'file':<22}{'modulation':<13}{'verdict'}")
    for p in a.paths:
        r = analyse(p)
        print(f"{r['file']:<22}{r['modulation']:<13}"
              "(informational only — this metric does not discriminate)")


if __name__ == "__main__":
    main()
