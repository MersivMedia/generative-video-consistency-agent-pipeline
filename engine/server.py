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

import identity  # noqa: E402
import ladder  # noqa: E402
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

        # Persistence: replay the journal instead of wiping the stream dir. A
        # crash mid-screening previously lost the entire canon log — what the
        # audience chose is the product's memory, so it is append-only and
        # fsynced rather than snapshotted.
        self.journal = identity.Journal(STREAM / "screening.jsonl")
        resumed = identity.restore(self.screening, self.journal, STREAM)
        self.resumed = resumed["segments"] > 0
        if self.resumed:
            print(f"  resumed: {resumed['segments']} segments, "
                  f"{resumed['decisions']} decisions"
                  + (f", {resumed['missing']} segment files missing"
                     if resumed["missing"] else ""))
            self.playlist = Playlist(STREAM)
            self.playlist.segments = list(self.screening.segments)
        else:
            for old in STREAM.glob("*.ts"):
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
        """Encode one clip into every ladder rung and publish it.

        MUST be called via asyncio.to_thread: this runs three ffmpeg encodes
        (~3s wall) and calling it directly from the event loop froze every
        HTTP request and state push for the duration, which showed up as the
        server timing out while a segment was being prepared.

        The clips are progressive MP4 (ftyp + moov + mdat) — each one a
        self-contained movie. hls.js cannot splice those into a continuous
        timeline: it loads the first and then stalls, which shows up as a
        player that never advances. HLS needs either MPEG-TS segments or fMP4
        with a shared init segment.

        TS is chosen because it needs no init segment and no per-segment
        signalling, so a playlist can grow one clip at a time. The remux is
        stream-copy (no re-encode) and costs ~50ms.
        """
        index = len(self.playlist.segments)
        offset = sum(s.seconds for s in self.playlist.segments)

        # Every source clip is its own movie starting at PTS 1.4, so a naive
        # remux makes every segment claim the same timestamp; the player sees
        # time run backwards at each boundary and freezes. Stamp each segment
        # with the playlist's running offset so presentation timestamps
        # increase monotonically across the whole screening.
        #
        # All ladder rungs are encoded in parallel from the same source with an
        # identical GOP, so a mid-stream quality switch lands on a keyframe.
        paths = ladder.encode_all(src, STREAM, index, offset)
        dest = paths["mid"]          # the reference rung for duration probing
        seg = Segment(path=dest, seconds=probe(dest), beat_id=beat, kind=kind)
        self.playlist.append(seg)
        self.screening.segments.append(seg)
        for r in ladder.LADDER:
            ladder.write_variant(STREAM, r, self.screening.segments,
                                 self.playlist.finished)
        ladder.write_master(STREAM)
        self.journal.append("segment", file=f"{index:04d}",
                            seconds=seg.seconds, beat=beat, segment_kind=kind,
                            index=index)
        self.screening.seq += 1
        return seg

    # -- the screening loop ---------------------------------------------
    async def run_story(self) -> None:
        beats = [b for ch in self.story["chapters"] for b in ch["beats"]]
        if self.resumed:
            # Rewind the clock so the playhead lands where it left off rather
            # than jumping to the end of already-published media.
            self.screening.started_at = time.time() - sum(
                s.seconds for s in self.screening.segments)
            decided = {c["beat"] for c in self.screening.canon}
            beats = [b for b in beats if b["id"] not in decided]
            print(f"  resuming with {len(beats)} beats remaining")
        else:
            self.screening.started_at = time.time()
            self.journal.append("start", at=self.screening.started_at,
                                title=self.screening.title)
        pool = list(self._pool)
        idx = 0

        for beat in beats:
            head_n = beat.get("head_shots", 4)
            tail_n = beat.get("tail_shots", 2)

            # --- publish the head -----------------------------------------
            head_start = self.screening.published_seconds
            for _ in range(head_n):
                await asyncio.to_thread(
                    self._publish, pool[idx % len(pool)], beat["id"])
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
            self.journal.append("vote_open", beat=beat["id"],
                                choices=self.screening.choices,
                                window=head_seconds * VOTE_CLOSE_FRAC)
            await self.push()

            await self._sleep_until_vote_close()
            winner = self.screening.close_vote()
            if self.screening.canon:
                self.journal.append("decision", record=self.screening.canon[-1])
            await self.push()

            # --- tail: rendered knowing the winner, on the critical path ---
            # Runway is the remaining 60% of the head as WATCHED, which is the
            # 12s the simulator predicted at head_shots=4.
            for _ in range(tail_n):
                await asyncio.to_thread(
                    self._publish, pool[idx % len(pool)], beat["id"])
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

        @app.api_route("/stream/master.m3u8", methods=["GET", "HEAD"])
        async def master():
            f = STREAM / "master.m3u8"
            if not f.exists():
                return Response(status_code=404)
            return Response(f.read_text(),
                            media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store"})

        @app.api_route("/stream/{name}.m3u8", methods=["GET", "HEAD"])
        async def variant(name: str):
            # index.m3u8 is kept as an alias for the mid rung so existing
            # single-rendition clients keep working.
            f = STREAM / (f"{name}.m3u8" if name != "index" else "mid.m3u8")
            if not f.exists():
                return Response(status_code=404)
            # no-store: the playlist grows as clips land, and a cached copy
            # silently freezes a viewer wherever the stream was when they
            # first connected.
            return Response(f.read_text(),
                            media_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store"})

        @app.api_route("/segments/{rung}/{name}", methods=["GET", "HEAD"])
        async def segment(rung: str, name: str):
            p = STREAM / rung / name
            if not p.exists():
                return Response(status_code=404)
            return FileResponse(p, media_type="video/mp2t")

        @app.get("/token")
        async def token():
            """Mint a signed viewer token.

            NOT login. A screening is anonymous; this only has to count one
            person once. Identity previously came from id(websocket) — a memory
            address, which CPython recycles aggressively (2000 short-lived
            objects yielded 2 distinct ids in measurement). That let a
            reconnecting viewer inherit a departed viewer's ballot, and did not
            survive a refresh.
            """
            return {"token": identity.mint()}

        @app.get("/state")
        async def state():
            return self.screening.state()

        @app.websocket("/live")
        async def live(ws: WebSocket):
            await ws.accept()
            await self.hub.add(ws)
            # The client sends its stored token as a query param. An absent or
            # forged token gets a freshly minted identity rather than a
            # rejection: a screening should never refuse an audience member.
            viewer_id = identity.verify(ws.query_params.get("token"))
            if viewer_id is None:
                viewer_id = identity.verify(identity.mint())
            await ws.send_text(json.dumps(self.screening.state()))
            try:
                while True:
                    raw = await ws.receive_text()
                    msg = json.loads(raw)
                    if msg.get("type") == "vote":
                        choice = msg.get("choice", "")
                        ok = self.screening.cast(viewer_id, choice)
                        await ws.send_text(json.dumps(
                            {"type": "vote_ack", "accepted": ok}))
                        if ok:
                            self.journal.append("vote", viewer=viewer_id,
                                                choice=choice,
                                                beat=self.screening.beat_id)
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
