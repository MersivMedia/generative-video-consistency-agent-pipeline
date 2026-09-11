"""M4 — screening server. Serves the stream, runs the vote, drives the queue.

    python engine/server.py stories/the_signal.json --demo

The demo flag replays already-rendered clips instead of calling the video API,
so the live layer can be exercised end to end for free. Everything except the
render call is identical between demo and live.

SHAPE
    A single asyncio process holds one Screening. A background task advances
    the story: publish head segments, open voting, close voting, publish the
    tail, repeat. Viewers connect over WebSocket, receive state pushes, and
    send votes. The playlist grows underneath the player.

WHY THE SERVER OWNS THE PLAYHEAD
    Everyone must see the same frame at the same time or the vote is
    meaningless. Clients report nothing that affects state; they receive
    position and correct themselves against it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, Response  # noqa: E402
import uvicorn  # noqa: E402

from live import Hub, Playlist, Screening, Segment, VOTE_CLOSE_FRAC  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STREAM = ROOT / "renders" / "_live"
VIEWER = Path(__file__).parent / "viewer.html"


def probe(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration", "-of", "default=nw=1:nk=1", str(p)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 5.0


class Server:
    def __init__(self, story: Path, demo: bool = False, vote_window: float = 8.0):
        self.story = json.loads(story.read_text())
        self.demo = demo
        self.vote_window = vote_window
        self.screening = Screening(title=self.story.get("title", "THIS WAY"))
        self.hub = Hub()
        STREAM.mkdir(parents=True, exist_ok=True)
        for old in STREAM.glob("*"):
            old.unlink()
        self.playlist = Playlist(STREAM)
        self.app = self._build()
        self._pool = self._demo_clips() if demo else []

    # -- demo source ----------------------------------------------------
    def _demo_clips(self) -> list[Path]:
        """Reuse already-paid-for renders so the live layer costs nothing."""
        clips = sorted((ROOT / "renders" / "m2_v4_scene0").glob("shot_*.mp4"))
        cutaways = sorted((ROOT / "assets" / "cutaways").rglob("*.mp4"))
        if not clips:
            raise SystemExit("no rendered clips found; run render_scene.py first")
        return clips + cutaways

    def _publish(self, src: Path, beat: str, kind: str = "shot") -> Segment:
        """Remux a clip into an MPEG-TS segment and append it to the playlist.

        The clips are progressive MP4 (ftyp + moov + mdat) — each one a
        self-contained movie. hls.js cannot splice those into a continuous
        timeline: it loads the first and then stalls, which shows up as a
        player that never advances. HLS needs either MPEG-TS segments or fMP4
        with a shared init segment.

        TS is chosen because it needs no init segment and no per-segment
        signalling, so a playlist can grow one clip at a time. The remux is
        stream-copy (no re-encode) and costs ~50ms.
        """
        dest = STREAM / f"seg_{len(self.playlist.segments):04d}.ts"

        # Every source clip is its own movie starting at PTS 1.4, so a naive
        # remux produces segments that ALL start at the same timestamp. The
        # player sees time jump backwards at each boundary and stalls after the
        # first segment — the observed "plays briefly, then freezes".
        #
        # Fix: stamp each segment with the running offset of the playlist, so
        # presentation timestamps increase monotonically across the whole
        # screening, exactly as they would from a single continuous encode.
        offset = sum(s.seconds for s in self.playlist.segments)

        # The source clips are ~9 Mbps at 1536x672 — a 5.6MB download before
        # the first frame appears, which is why startup dragged on mobile.
        # Re-encode to ~2 Mbps at 1280 wide: 5.6MB -> 1.24MB, a 4.5x cut, at
        # 2.7s of CPU per clip. That fits comfortably inside the ~20s buffer
        # the scheduler maintains, so it costs latency we already have.
        #
        # -g 48 forces a keyframe every 2s so the player can start decoding
        # partway into a segment instead of waiting for the next one.
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src),
             "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
             "-b:v", "1800k", "-maxrate", "2000k", "-bufsize", "3000k",
             "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
             "-vf", "scale=1280:-2",
             "-c:a", "aac", "-b:a", "96k", "-ac", "2",
             "-bsf:v", "h264_mp4toannexb",
             "-muxdelay", "0", "-muxpreload", "0",
             "-output_ts_offset", f"{offset:.3f}",
             "-f", "mpegts", str(dest)],
            check=True, capture_output=True)
        seg = Segment(path=dest, seconds=probe(dest), beat_id=beat, kind=kind)
        self.playlist.append(seg)
        self.screening.segments.append(seg)
        self.screening.seq += 1
        return seg

    # -- the screening loop ---------------------------------------------
    async def run_story(self) -> None:
        beats = [b for ch in self.story["chapters"] for b in ch["beats"]]
        self.screening.started_at = time.time()
        pool = list(self._pool)
        idx = 0

        for beat in beats:
            head_n = beat.get("head_shots", 4)
            tail_n = beat.get("tail_shots", 2)

            # --- publish the head -----------------------------------------
            head_start = self.screening.published_seconds
            for _ in range(head_n):
                self._publish(pool[idx % len(pool)], beat["id"])
                idx += 1
            head_seconds = self.screening.published_seconds - head_start
            await self.push()

            # --- wait until the audience is actually WATCHING this head ----
            # The render clock and the playhead are different clocks, and they
            # differ by the whole buffer (20-26s). Opening the vote at publish
            # time asked the room to decide a scene they had not seen yet, and
            # closed it before that scene reached the screen. Every vote landed
            # outside the window. The vote must live on VIEWER time, which is
            # also what the M3 simulation assumed.
            await self._sleep_until_playhead(head_start)

            self.screening.open_vote(beat["id"], [
                {"id": "A", "label": beat.get("branch_axis", "hold")},
                {"id": "B", "label": "the other way"},
            ], window=head_seconds * VOTE_CLOSE_FRAC)
            await self.push()

            await self._sleep_until_vote_close()
            winner = self.screening.close_vote()
            await self.push()

            # --- tail: rendered knowing the winner, on the critical path ---
            # Runway is the remaining 60% of the head as WATCHED, which is the
            # 12s the simulator predicted at head_shots=4.
            for _ in range(tail_n):
                self._publish(pool[idx % len(pool)], beat["id"])
                idx += 1
            await self.push()

            # Let playback catch up to what has been published, so the sim and
            # the live loop agree about buffer state.
            await self._sleep_until_buffer(6.0)

        self.playlist.finish()
        await self.push()

    async def _sleep_until_vote_close(self) -> None:
        while True:
            left = (self.screening.vote_closes_at or 0) - time.time()
            if left <= 0:
                return
            await asyncio.sleep(min(0.5, left))
            await self.push()

    async def _sleep_until_playhead(self, position: float) -> None:
        """Block until the audience's playhead reaches `position` seconds."""
        while self.screening.playhead() < position:
            await asyncio.sleep(0.25)
            await self.push()

    async def _sleep_until_buffer(self, target: float) -> None:
        """Wait until the buffer drains to `target` seconds ahead."""
        while self.screening.buffer_ahead() > target:
            await asyncio.sleep(0.5)
            await self.push()

    async def push(self) -> None:
        self.screening.viewers = self.hub.count
        await self.hub.broadcast(self.screening.state())

    # -- HTTP / WS ------------------------------------------------------
    def _build(self) -> FastAPI:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def lifespan(_app):
            task = asyncio.create_task(self.run_story())
            yield
            task.cancel()

        app = FastAPI(title="THIS WAY — live", lifespan=lifespan)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            return VIEWER.read_text()

        @app.api_route("/stream/index.m3u8", methods=["GET", "HEAD"])
        async def playlist():
            body = (STREAM / "index.m3u8").read_text()
            # no-store: the playlist changes as clips land, and a cached copy
            # silently freezes a viewer at whatever the stream looked like when
            # they first connected.
            return Response(body, media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store"})

        @app.api_route("/segments/{name}", methods=["GET", "HEAD"])
        async def segment(name: str):
            p = STREAM / name
            if not p.exists():
                return Response(status_code=404)
            return FileResponse(p, media_type="video/mp2t")

        @app.get("/state")
        async def state():
            return self.screening.state()

        @app.websocket("/live")
        async def live(ws: WebSocket):
            await ws.accept()
            await self.hub.add(ws)
            viewer_id = f"v{id(ws)}"
            await ws.send_text(json.dumps(self.screening.state()))
            try:
                while True:
                    raw = await ws.receive_text()
                    msg = json.loads(raw)
                    if msg.get("type") == "vote":
                        ok = self.screening.cast(viewer_id, msg.get("choice", ""))
                        await ws.send_text(json.dumps(
                            {"type": "vote_ack", "accepted": ok}))
                        if ok:
                            await self.push()
            except WebSocketDisconnect:
                pass
            except Exception:
                pass
            finally:
                await self.hub.drop(ws)

        return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("story", type=Path)
    ap.add_argument("--demo", action="store_true",
                    help="replay existing renders instead of calling the API")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()

    srv = Server(a.story, demo=a.demo)
    print(f"screening '{srv.screening.title}' on http://{a.host}:{a.port}"
          f"{'  [DEMO — no API spend]' if a.demo else ''}")
    uvicorn.run(srv.app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
