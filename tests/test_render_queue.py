"""Behavioural tests for the M3 render queue.

Deterministic: a fake clock and instant fake renders, so these assert queue
LOGIC rather than provider timing. The provider timing is measured separately
and recorded in the README.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine"))

from render_queue import Priority, RenderQueue, plan_scene  # noqa: E402


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, d): self.t += d


def fake(name: str, delay: float = 0.0):
    def _r():
        if delay:
            time.sleep(delay)
        return Path(f"/tmp/{name}.mp4")
    return _r


def test_priority_order_beats_submission_order():
    """A CRITICAL job submitted last must still run first."""
    q = RenderQueue(slots=1, clock=Clock())
    order = []
    q.submit("spec_a", fake("a"), priority=Priority.SPECULATIVE, deadline_in=30)
    q.submit("spec_b", fake("b"), priority=Priority.SPECULATIVE, deadline_in=30)
    q.submit("canon", fake("c"), priority=Priority.CRITICAL, deadline_in=30)
    q.run(on_result=lambda j, p, late, exc=None: order.append(j.job_id))
    assert order[0] == "canon", order
    print(f"  PASS priority order: {order}")


def test_earliest_deadline_first_within_a_class():
    q = RenderQueue(slots=1, clock=Clock())
    order = []
    q.submit("due_late", fake("l"), priority=Priority.SPECULATIVE, deadline_in=60)
    q.submit("due_soon", fake("s"), priority=Priority.SPECULATIVE, deadline_in=10)
    q.run(on_result=lambda j, p, late, exc=None: order.append(j.job_id))
    assert order == ["due_soon", "due_late"], order
    print(f"  PASS earliest-deadline-first: {order}")


def test_vote_prunes_the_whole_losing_branch():
    """Not just the immediate loser — every queued job on that branch."""
    q = RenderQueue(slots=2, clock=Clock())
    for i in range(3):
        q.submit(f"A{i}", fake(f"A{i}"), branch="A", beat_id="b1")
        q.submit(f"B{i}", fake(f"B{i}"), branch="B", beat_id="b1")
    q.submit("other_beat", fake("x"), branch="A", beat_id="b2")
    assert q.depth == 7
    dropped = q.resolve_vote("b1", winner="A")
    assert dropped == 3, dropped
    ids = {j["id"] for j in q.snapshot()}
    assert not any(i.startswith("B") for i in ids), ids
    assert "other_beat" in ids, "a different beat must not be pruned"
    print(f"  PASS pruned {dropped} losing jobs, kept {sorted(ids)}")


def test_promote_raises_the_winner():
    q = RenderQueue(slots=1, clock=Clock())
    q.submit("tail_A", fake("t"), branch="A", beat_id="b1",
             priority=Priority.SPECULATIVE, deadline_in=30)
    q.submit("filler", fake("f"), priority=Priority.SPECULATIVE, deadline_in=5)
    n = q.promote("b1", "A", Priority.CRITICAL)
    assert n == 1
    order = []
    q.run(on_result=lambda j, p, late, exc=None: order.append(j.job_id))
    assert order[0] == "tail_A", order
    print(f"  PASS promotion overrides earlier deadline: {order}")


def test_expired_speculative_is_abandoned_but_canon_is_not():
    clock = Clock()
    q = RenderQueue(slots=1, clock=clock)
    q.submit("spec", fake("s"), priority=Priority.SPECULATIVE, deadline_in=10)
    q.submit("canon", fake("c"), priority=Priority.CRITICAL, deadline_in=10)
    clock.advance(20)          # both deadlines now in the past
    dropped = q.drop_expired()
    ids = {j["id"] for j in q.snapshot()}
    assert dropped == 1 and ids == {"canon"}, (dropped, ids)
    print("  PASS expired speculative dropped, late canon retained")


def test_failure_is_reported_not_swallowed():
    q = RenderQueue(slots=1, clock=Clock())

    def boom():
        raise RuntimeError("provider 500")

    seen = []
    q.submit("bad", boom, priority=Priority.CRITICAL)
    stats = q.run(on_result=lambda j, p, late, exc=None: seen.append((j.job_id, exc)))
    assert stats["fallback"] == 1 and seen[0][1] is not None
    print(f"  PASS failure surfaced: {type(seen[0][1]).__name__}")


def test_concurrency_actually_overlaps():
    q = RenderQueue(slots=4)
    for i in range(4):
        q.submit(f"j{i}", fake(f"j{i}", delay=0.3), priority=Priority.CRITICAL)
    t0 = time.time()
    stats = q.run()
    wall = time.time() - t0
    assert stats["rendered"] == 4
    assert wall < 0.9, f"expected overlap, took {wall:.2f}s (serial would be 1.2s)"
    print(f"  PASS 4x0.3s jobs in {wall:.2f}s wall (serial=1.2s)")


def test_scene_plan_matches_head_tail_economics():
    p = plan_scene("b1", head_shots=3, tail_shots=2, choices=["A", "B"])
    assert p["speculative"] == 6 and p["discarded_if_two_choices"] == 3
    assert p["tail_deadline"] == 15.0
    print(f"  PASS plan: {p}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    print(f"running {len(tests)} queue tests\n")
    for t in tests:
        t()
    print(f"\nall {len(tests)} passed")
