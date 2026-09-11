"""Viewer identity and screening persistence.

Two constraints that a public screening cannot go without, and one real bug
found while building them.

THE BUG: viewer identity was `id(websocket)` — a memory address. CPython reuses
addresses aggressively; 2000 short-lived objects produced just 2 distinct ids in
a direct measurement. So a reconnecting viewer could inherit a departed
viewer's ballot and silently overwrite it, identity did not survive a page
refresh, and nothing stopped one person opening N tabs to cast N votes. The M4
tests missed it because they held every socket open simultaneously, so no
address was ever recycled.

IDENTITY
    A signed, opaque token minted on first contact and stored in the browser.
    HMAC over the token id with a server-side secret, so a client cannot forge
    or enumerate one. This is deliberately NOT login: a screening is anonymous,
    it just has to be able to count one person once. The token survives refresh
    and reconnection, and the server binds ballots to it rather than to a
    socket.

    Sybil resistance is bounded and honest: clearing storage or opening a
    private window earns a new identity. Defeating a determined ballot stuffer
    needs real accounts, which is a product decision, not a code one. What this
    does fix is the accidental case — refresh, reconnect, second tab — which is
    what actually corrupts a live tally.

PERSISTENCE
    An append-only JSONL journal. Every state transition is one line, fsynced.
    On restart the journal is replayed to rebuild the screening exactly, then
    appended to. Append-only rather than a snapshot because the canon log is
    the product's memory: what the audience chose, when, and by what margin.
    A crash must not be able to rewrite a decision that has already been shown.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Iterator

TOKEN_BYTES = 16
SECRET_FILE = Path(os.environ.get(
    "THISWAY_SECRET", Path.home() / ".cache/thisway/screening_secret"))


def _secret() -> bytes:
    """Load or create the signing secret.

    Kept outside the repo and 0600. If it is lost, existing tokens stop
    validating and every viewer is simply re-minted — which is an acceptable
    failure mode for an anonymous screening, and much better than shipping a
    hardcoded default that would make every deployment forgeable.
    """
    if SECRET_FILE.exists():
        return SECRET_FILE.read_bytes()
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    s = secrets.token_bytes(32)
    SECRET_FILE.write_bytes(s)
    SECRET_FILE.chmod(0o600)
    return s


def mint() -> str:
    """Issue a new signed viewer token: `<id>.<sig>`."""
    vid = secrets.token_urlsafe(TOKEN_BYTES)
    sig = hmac.new(_secret(), vid.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{vid}.{sig}"


def verify(token: str | None) -> str | None:
    """Return the viewer id if the token is authentic, else None."""
    if not token or "." not in token:
        return None
    vid, _, sig = token.rpartition(".")
    want = hmac.new(_secret(), vid.encode(), hashlib.sha256).hexdigest()[:24]
    # compare_digest: a plain == leaks timing information about the signature
    return vid if hmac.compare_digest(sig, want) else None


class TokenLimiter:
    """Per-IP cap on how many identities one source may mint.

    Signed tokens stop the ACCIDENTAL duplicate — refresh, reconnect, second
    tab — which is what actually corrupts a live tally. They do nothing against
    someone who clears storage in a loop, because minting is free.

    This makes it not free. One source address gets a bounded number of
    identities per screening; beyond that it is handed a token it already has.
    Deliberately NOT a ban: a lecture hall or a household behind one NAT is a
    legitimate crowd, so the cap is generous and the failure mode is "you share
    a ballot with the other people on your router", not "you are locked out".

    Defeating this needs many source addresses, which is a different threat
    model and needs real accounts. What it buys is that ballot stuffing costs
    infrastructure rather than a keyboard shortcut.
    """

    def __init__(self, per_ip: int = 8):
        self.per_ip = per_ip
        self._issued: dict[str, list[str]] = {}

    def issue(self, ip: str) -> tuple[str, bool]:
        """Return (token, fresh). `fresh` is False when the cap recycled one."""
        seen = self._issued.setdefault(ip, [])
        if len(seen) >= self.per_ip:
            # Hand back the oldest identity from this source rather than a new
            # one. The client cannot tell, and the tally stops inflating.
            return seen[0], False
        tok = mint()
        seen.append(tok)
        return tok, True

    def stats(self) -> dict:
        return {"sources": len(self._issued),
                "issued": sum(len(v) for v in self._issued.values()),
                "at_cap": sum(1 for v in self._issued.values()
                              if len(v) >= self.per_ip)}


class Journal:
    """Append-only screening log with replay.

    One JSON object per line. Writes are flushed and fsynced, because the point
    of the journal is to survive the process dying unexpectedly — a buffered
    write that never reached disk is indistinguishable from no journal at all.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def append(self, kind: str, **fields: Any) -> dict:
        rec = {"t": round(time.time(), 3), "kind": kind, **fields}
        self._fh.write(json.dumps(rec) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return rec

    def replay(self) -> Iterator[dict]:
        """Yield every complete record.

        A truncated final line is expected after a hard kill mid-write, and is
        skipped rather than treated as corruption — the rest of the journal is
        still authoritative.
        """
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def restore(screening, journal: Journal, stream_dir: Path) -> dict:
    """Rebuild a screening from its journal. Returns a summary of what resumed.

    Segments are re-attached only if the file still exists on disk: a journal
    entry for a clip whose file was cleaned up would give the player a playlist
    it cannot fetch, which is worse than a shorter screening.
    """
    from live import Segment

    counts = {"segments": 0, "decisions": 0, "missing": 0}
    for rec in journal.replay():
        k = rec["kind"]
        if k == "start":
            screening.started_at = rec["at"]
            screening.title = rec.get("title", screening.title)
        elif k == "segment":
            # Segments live per-rung (mid is the reference used for duration).
            # Journal stores the index, not a path, so a ladder change does not
            # invalidate an existing journal.
            p = stream_dir / "mid" / f"seg_{int(rec['index']):04d}.ts"
            if not p.exists():
                counts["missing"] += 1
                continue
            screening.segments.append(Segment(
                path=p, seconds=rec["seconds"], beat_id=rec["beat"],
                kind=rec.get("segment_kind", "shot")))
            counts["segments"] += 1
        elif k == "vote_open":
            screening.beat_id = rec["beat"]
            screening.choices = rec["choices"]
            screening.votes = {}
            screening.vote_opens_at = rec["t"]
            screening.vote_closes_at = rec["t"] + rec["window"]
        elif k == "vote":
            screening.votes[rec["viewer"]] = rec["choice"]
        elif k == "decision":
            screening.canon.append(rec["record"])
            screening.votes = {}
            screening.vote_closes_at = rec["t"]
            counts["decisions"] += 1
    screening.seq += 1
    return counts
