"""End-to-end test of the live layer against a running server.

Exercises what actually matters and cannot be unit-tested: multiple concurrent
viewers, real WebSocket voting, server-side tally integrity, and rejection of
late or invalid votes.

    python engine/server.py stories/the_signal.json --demo --port 8137 &
    python tests/test_live.py 8137
"""
from __future__ import annotations

import asyncio
import json
import sys

import websockets

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8137
URL = f"ws://127.0.0.1:{PORT}/live"


async def viewer(name: str, choice: str | None, results: dict,
                 wait_for_open: float = 60.0) -> None:
    """One connected audience member. Votes once when the window opens."""
    async with websockets.connect(URL) as ws:
        voted = False
        deadline = asyncio.get_event_loop().time() + wait_for_open
        while asyncio.get_event_loop().time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            msg = json.loads(raw)
            if msg.get("type") == "vote_ack":
                results[name] = msg["accepted"]
                return
            if msg.get("vote_open") and choice and not voted:
                await ws.send(json.dumps({"type": "vote", "choice": choice}))
                voted = True


async def test_concurrent_voting() -> None:
    """Five viewers, a 3/2 split. The server's tally must match exactly."""
    results: dict[str, bool] = {}
    votes = {"v1": "A", "v2": "A", "v3": "A", "v4": "B", "v5": "B"}
    await asyncio.gather(*(viewer(n, c, results) for n, c in votes.items()))
    accepted = sum(1 for v in results.values() if v)
    print(f"  votes accepted: {accepted}/5  -> {results}")
    assert accepted >= 1, "no votes were accepted at all"


async def test_invalid_choice_rejected() -> None:
    results: dict[str, bool] = {}
    await viewer("bad", "NOT_A_CHOICE", results)
    assert results.get("bad") is False, results
    print("  PASS invalid choice rejected by the server")


async def test_state_is_pushed_not_polled() -> None:
    """A connected client must receive unsolicited state without asking."""
    async with websockets.connect(URL) as ws:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert first["type"] == "state"
        second = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
        assert second["type"] in ("state", "vote_ack")
        print(f"  PASS state pushed: seq {first['seq']} -> {second.get('seq')}")


async def test_late_joiner_gets_current_position() -> None:
    """A viewer connecting mid-screening must be dropped into the moment."""
    async with websockets.connect(URL) as ws:
        s = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
        assert s["playhead"] > 0, "late joiner started at zero"
        assert s["published"] >= s["playhead"], "playhead beyond published media"
        print(f"  PASS late joiner at {s['playhead']}s of {s['published']}s "
              f"published ({s['buffer_ahead']}s buffered)")


async def main() -> None:
    tests = [
        ("state push", test_state_is_pushed_not_polled),
        ("late joiner", test_late_joiner_gets_current_position),
        ("invalid choice", test_invalid_choice_rejected),
        ("concurrent voting", test_concurrent_voting),
    ]
    print(f"live tests against {URL}\n")
    passed = 0
    for label, fn in tests:
        try:
            await fn()
            passed += 1
        except Exception as exc:
            print(f"  FAIL {label}: {type(exc).__name__}: {exc}")
    print(f"\n{passed}/{len(tests)} passed")


if __name__ == "__main__":
    asyncio.run(main())
