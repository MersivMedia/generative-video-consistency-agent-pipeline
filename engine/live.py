"""M4 — the live layer: server-authoritative playback, voting, and canon log.

The design constraint that shapes everything here: **the server owns the
playhead**. Every viewer must see the same frame at the same moment, because a
vote only means something if everyone is voting on the same story state. So
clients do not choose what to play or when — they report their position and are
corrected. A late joiner is dropped into the current moment, not the beginning.

Why HLS rather than WebRTC: this is a shared screening, not a conversation. A
few seconds of uniform latency is fine; per-viewer streams are not. HLS also
lets the playlist be appended to as clips finish rendering, which is exactly the
speculative model from M3 — the player is always consuming a playlist the
renderer is still writing.

Three surfaces:
    GET  /                     the viewer (single page, no build step)
    GET  /stream/index.m3u8    the live playlist, appended as clips land
    WS   /live                 state push + vote intake

VOTING INTEGRITY
    Votes are one per connection per beat, tallied server-side, and frozen when
    the window closes. The tally is pushed to everyone so the room can see the
    split, but the winner is computed from the server's own record, never from
    a client-reported total.

WHAT THIS DOES NOT DO
    No auth, no persistence across restarts, no CDN. A screening is a single
    process holding a single story in memory. That is deliberate for M4 — the
    interesting risk is synchronisation and vote handling, not scale.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VOTE_CLOSE_FRAC = 0.40      # matches the simulated, story-canon value


@dataclass
class Segment:
    """One playable clip in the live playlist."""
    path: Path
    seconds: float
    beat_id: str
    kind: str = "shot"       # shot | cutaway
    branch: str | None = None


@dataclass
class Screening:
    """Authoritative state for one screening.

    The playhead is derived from wall-clock time and the segment durations that
    have actually been published, so it cannot drift from what viewers can
    fetch. Nothing here trusts a client.
    """
    title: str = "THIS WAY"
    started_at: float = 0.0
    segments: list[Segment] = field(default_factory=list)
    beat_id: str = ""
    choices: list[dict] = field(default_factory=list)
    votes: dict[str, str] = field(default_factory=dict)      # viewer -> choice id
    vote_opens_at: float | None = None
    vote_closes_at: float | None = None
    canon: list[dict] = field(default_factory=list)
    viewers: int = 0
    seq: int = 0             # bumps on every state change, for client dedupe

    # -- playhead -------------------------------------------------------
    @property
    def published_seconds(self) -> float:
        return sum(s.seconds for s in self.segments)

    def playhead(self, now: float | None = None) -> float:
        if not self.started_at:
            return 0.0
        now = now or time.time()
        return min(now - self.started_at, self.published_seconds)

    def buffer_ahead(self, now: float | None = None) -> float:
        """Seconds of rendered material beyond the playhead.

        This is the number that decides whether the screening is healthy. When
        it approaches zero the scheduler must publish a cutaway rather than let
        the playlist run dry.
        """
        return self.published_seconds - self.playhead(now)

    # -- voting ---------------------------------------------------------
    def open_vote(self, beat_id: str, choices: list[dict], window: float) -> None:
        self.beat_id = beat_id
        self.choices = choices
        self.votes = {}
        self.vote_opens_at = time.time()
        self.vote_closes_at = self.vote_opens_at + window
        self.seq += 1

    def cast(self, viewer: str, choice_id: str) -> bool:
        """One vote per viewer per beat; re-voting replaces, late votes ignored.

        Returning False rather than raising: a vote arriving a few hundred ms
        after close is a normal network event, not an error condition.
        """
        if self.vote_closes_at is None or time.time() > self.vote_closes_at:
            return False
        if choice_id not in {c["id"] for c in self.choices}:
            return False
        self.votes[viewer] = choice_id
        self.seq += 1
        return True

    def tally(self) -> dict[str, int]:
        out = {c["id"]: 0 for c in self.choices}
        for choice in self.votes.values():
            out[choice] = out.get(choice, 0) + 1
        return out

    def close_vote(self) -> str | None:
        """Freeze the window and return the winner.

        Ties break toward the FIRST choice deterministically rather than
        randomly: a screening must be reproducible from its canon log, and a
        coin flip that is not recorded makes the log a lie.
        """
        if not self.choices:
            return None
        counts = self.tally()
        self.vote_closes_at = time.time()
        winner = max(self.choices, key=lambda c: (counts[c["id"]],
                                                  -self.choices.index(c)))["id"]
        self.canon.append({
            "beat": self.beat_id, "winner": winner, "tally": counts,
            "voters": len(self.votes), "at": round(self.playhead(), 1),
        })
        self.seq += 1
        return winner

    # -- wire format ----------------------------------------------------
    def state(self) -> dict[str, Any]:
        now = time.time()
        remaining = None
        if self.vote_closes_at:
            remaining = max(0.0, round(self.vote_closes_at - now, 1))
        return {
            "type": "state",
            "seq": self.seq,
            "title": self.title,
            "playhead": round(self.playhead(now), 2),
            "published": round(self.published_seconds, 2),
            "buffer_ahead": round(self.buffer_ahead(now), 2),
            "beat": self.beat_id,
            "choices": self.choices,
            "tally": self.tally(),
            "vote_remaining": remaining,
            "vote_open": bool(remaining),
            "viewers": self.viewers,
            "canon": self.canon[-6:],
            "segments": len(self.segments),
        }


class Playlist:
    """HLS playlist written incrementally as clips finish rendering.

    EVENT playlists (no ENDLIST until the screening finishes) are what make the
    speculative model work: the player keeps re-fetching and picks up segments
    that did not exist when it started watching.
    """

    def __init__(self, root: Path, target_duration: int = 6):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.target = target_duration
        self.segments: list[Segment] = []
        self.finished = False

    def append(self, seg: Segment) -> None:
        self.segments.append(seg)
        self.write()

    def finish(self) -> None:
        self.finished = True
        self.write()

    def write(self) -> Path:
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 f"#EXT-X-TARGETDURATION:{self.target}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:EVENT"]
        for s in self.segments:
            # A cutaway is a legitimate discontinuity: different source clip,
            # different encoder run. Without this tag players stutter on it.
            if s.kind == "cutaway":
                lines.append("#EXT-X-DISCONTINUITY")
            lines.append(f"#EXTINF:{s.seconds:.3f},")
            lines.append(f"/segments/{s.path.name}")
        if self.finished:
            lines.append("#EXT-X-ENDLIST")
        out = self.root / "index.m3u8"
        out.write_text("\n".join(lines) + "\n")
        return out


class Hub:
    """Fan-out to connected viewers, with dead-socket pruning."""

    def __init__(self):
        self._clients: set = set()
        self._lock = asyncio.Lock()

    async def add(self, ws) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def drop(self, ws) -> None:
        async with self._lock:
            self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)

    async def broadcast(self, msg: dict) -> int:
        """Send to everyone; drop whoever fails.

        A slow or dead client must never block the screening, so failures are
        swallowed per-socket rather than propagated.
        """
        payload = json.dumps(msg)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.drop(ws)
        return len(self._clients)
