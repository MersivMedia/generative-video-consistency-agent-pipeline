"""Adaptive bitrate ladder for the live stream.

A single 2 Mbps rendition is fine on wifi and stalls on a train. HLS solves
this with a master playlist offering several renditions; the player measures its
own throughput and switches between them mid-stream. Without a ladder the only
options on a weak connection are buffering or nothing.

MEASURED ENCODE COST per 5s clip (libx264 veryfast, 1536x672 source):

    rung    width   video    size      rate       encode
    low      640     800k    0.62 MB   1.0 Mbps   1.6s
    mid      960    1400k    1.01 MB   1.6 Mbps   2.1s
    high    1280    2400k    1.70 MB   2.7 Mbps   3.0s
                                        serial total 6.7s

Serial encoding would eat a third of the ~20s buffer per clip. The rungs are
independent, so they run in parallel and the wall cost collapses to roughly the
slowest one (~3s), which the buffer absorbs comfortably.

SEGMENT ALIGNMENT IS MANDATORY
    Every rendition must be cut at the same instants with keyframes at the same
    positions, or a mid-stream switch lands the player between keyframes and
    produces a visible glitch or a stall. That is why each rung is encoded from
    the same source clip with identical `-g`/`-keyint_min` and `sc_threshold=0`
    (scene-cut detection off, which would otherwise place keyframes at
    content-dependent and therefore per-rung different positions).
"""
from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

GOP = 48          # keyframe every 2s at 24fps — same on every rung


@dataclass(frozen=True)
class Rung:
    name: str
    width: int
    v_bitrate: str
    a_bitrate: str

    @property
    def bandwidth(self) -> int:
        """Declared BANDWIDTH for the master playlist, in bits/sec.

        Must be the PEAK rather than the average: players use it to decide
        whether a rung is safe, and understating it causes them to pick a
        rendition they cannot sustain. Video ceiling + audio + ~10% container.
        """
        v = int(self.v_bitrate.rstrip("k")) * 1000
        a = int(self.a_bitrate.rstrip("k")) * 1000
        return int((v + a) * 1.1)


LADDER = (
    Rung("low", 640, "800k", "64k"),
    Rung("mid", 960, "1400k", "80k"),
    Rung("high", 1280, "2400k", "96k"),
)


def encode(src: Path, dest: Path, rung: Rung, offset: float) -> Path:
    """Encode one rung of one segment, timestamped into the screening timeline."""
    peak = int(rung.v_bitrate.rstrip("k"))
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
         "-b:v", rung.v_bitrate, "-maxrate", rung.v_bitrate,
         "-bufsize", f"{peak * 2}k",
         # identical GOP structure across rungs so switches land on keyframes
         "-g", str(GOP), "-keyint_min", str(GOP), "-sc_threshold", "0",
         "-vf", f"scale={rung.width}:-2",
         "-c:a", "aac", "-b:a", rung.a_bitrate, "-ac", "2",
         "-bsf:v", "h264_mp4toannexb",
         "-muxdelay", "0", "-muxpreload", "0",
         "-output_ts_offset", f"{offset:.3f}",
         "-f", "mpegts", str(dest)],
        check=True, capture_output=True)
    return dest


def encode_all(src: Path, root: Path, index: int, offset: float) -> dict[str, Path]:
    """Encode every rung in parallel. Returns rung name -> segment path."""
    out: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=len(LADDER)) as ex:
        futs = {}
        for r in LADDER:
            d = root / r.name
            d.mkdir(parents=True, exist_ok=True)
            dest = d / f"seg_{index:04d}.ts"
            futs[ex.submit(encode, src, dest, r, offset)] = r.name
        for f, name in futs.items():
            out[name] = f.result()
    return out


def write_master(root: Path) -> Path:
    """Master playlist listing every rendition.

    Ordered lowest-first: a player with no throughput history starts on the
    first entry, and starting low then climbing is a faster path to a picture
    than starting high and stalling.
    """
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for r in LADDER:
        lines.append(
            f"#EXT-X-STREAM-INF:BANDWIDTH={r.bandwidth},"
            f'RESOLUTION={r.width}x{int(r.width / 2.2857) // 2 * 2},'
            f'CODECS="avc1.4d401f,mp4a.40.2"')
        # Relative: resolved against the master playlist's own URL, so the app
        # works under any path prefix and behind a CDN without body rewriting.
        lines.append(f"{r.name}.m3u8")
    out = root / "master.m3u8"
    out.write_text("\n".join(lines) + "\n")
    return out


def write_variant(root: Path, rung: Rung, segments: list, finished: bool,
                  target: int = 6) -> Path:
    """Per-rendition media playlist. Segment list is identical across rungs."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:3",
             f"#EXT-X-TARGETDURATION:{target}",
             "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:EVENT"]
    for i, seg in enumerate(segments):
        if getattr(seg, "kind", "shot") == "cutaway":
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{seg.seconds:.3f},")
        lines.append(f"../segments/{rung.name}/seg_{i:04d}.ts")
    if finished:
        lines.append("#EXT-X-ENDLIST")
    out = root / f"{rung.name}.m3u8"
    out.write_text("\n".join(lines) + "\n")
    return out
