"""M3 — speculative A/B render queue with deadline logic.

The problem this solves: a live screening has no pause. The next clip must
exist before the current one ends, and the audience is voting *while* the
system decides what to render. So generation must run ahead of playback, both
possible futures must be in flight before the vote lands, and anything that
misses its deadline must degrade to something watchable rather than stall.

WHAT RUNS WHEN
    A scene splits into a HEAD (plays while voting is open) and a TAIL
    (consequence shots, authored once the winner is known).

        t=0     head starts playing         (already rendered)
                |-- speculate branch A head  \\  both in flight
                |-- speculate branch B head  /  during playback
        t=+10   voting opens
        t=+15   voting closes -> winner known
                loser's head is discarded
                winner's TAIL enters the queue at CRITICAL priority
        t=+25   head ends, winner's head plays, tail must be ready

CONCURRENCY IS THE WHOLE BUDGET
    Measured on fal: 4 concurrent 5s clips complete in 4.6s wall where serial
    would take 16s — a 3.5x speedup, ~4.3x realtime throughput. Serially the
    same work runs at 0.22x realtime and the buffer collapses immediately.
    Parallel job slots are therefore a hard requirement, not an optimization.

PRIORITY, NOT FAIRNESS
    A queue that renders speculative branch work before the clip playing in
    12 seconds is worse than no queue. Jobs carry an explicit priority and a
    deadline; the scheduler always serves the earliest deadline first, and
    abandons speculative work that can no longer be used.
"""
from __future__ import annotations

import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Callable


class Priority(IntEnum):
    """Lower value is served first. Deadline breaks ties within a class."""
    CRITICAL = 0      # on the canon path, playing imminently
    TAIL = 1          # winner's tail — canon, but slightly further out
    SPECULATIVE = 2   # branch heads, 50% will be discarded
    PREFETCH = 3      # frame/image layer, cheap and abandonable


@dataclass(order=True)
class Job:
    sort_key: tuple = field(init=False, repr=False)
    priority: Priority = Priority.SPECULATIVE
    deadline: float = 0.0            # absolute monotonic time
    seq: int = 0
    job_id: str = ""
    branch: str | None = None        # choice id this job belongs to, or None for canon
    beat_id: str = ""
    payload: dict = field(default_factory=dict)
    render: Callable[[], Path] | None = field(default=None, repr=False)

    def __post_init__(self):
        self.sort_key = (int(self.priority), self.deadline, self.seq)


class RenderQueue:
    """Deadline-ordered speculative queue with a fixed pool of render slots.

    Not a general task queue: it knows about branches so it can abandon work
    the audience has already voted away, and it knows about deadlines so it can
    tell the difference between "late" and "useless".
    """

    def __init__(self, slots: int = 4, clock: Callable[[], float] = time.monotonic):
        self.slots = slots
        self.clock = clock
        self._heap: list[Job] = []
        self._seq = itertools.count()
        self._lock = threading.RLock()
        self._running: dict[str, Job] = {}
        self.done: dict[str, Path] = {}
        self.stats = {"submitted": 0, "rendered": 0, "abandoned": 0,
                      "missed": 0, "fallback": 0}

    # -- submission -----------------------------------------------------
    def submit(self, job_id: str, render: Callable[[], Path], *,
               priority: Priority = Priority.SPECULATIVE,
               deadline_in: float = 30.0, branch: str | None = None,
               beat_id: str = "", payload: dict | None = None) -> Job:
        with self._lock:
            job = Job(priority=priority, deadline=self.clock() + deadline_in,
                      seq=next(self._seq), job_id=job_id, branch=branch,
                      beat_id=beat_id, payload=payload or {}, render=render)
            heapq.heappush(self._heap, job)
            self.stats["submitted"] += 1
            return job

    # -- pruning --------------------------------------------------------
    def resolve_vote(self, beat_id: str, winner: str) -> int:
        """Discard queued work for branches the audience did not choose.

        Must prune against the branch id, not merely "the other option": with
        more than two choices, or with stale jobs from an earlier beat still
        queued, cancelling only the immediate loser leaves orphaned subtrees
        alive. The frame planner hit exactly this and grew 76 -> 200 live jobs.
        """
        with self._lock:
            keep, dropped = [], 0
            for j in self._heap:
                if j.beat_id == beat_id and j.branch not in (None, winner):
                    dropped += 1
                else:
                    keep.append(j)
            self._heap = keep
            heapq.heapify(self._heap)
            self.stats["abandoned"] += dropped
            return dropped

    def promote(self, beat_id: str, branch: str,
                priority: Priority = Priority.CRITICAL) -> int:
        """Raise the winner's queued work now that it is on the canon path."""
        with self._lock:
            n = 0
            for j in self._heap:
                if j.beat_id == beat_id and j.branch == branch:
                    j.priority = priority
                    j.__post_init__()
                    n += 1
            heapq.heapify(self._heap)
            return n

    def drop_expired(self) -> int:
        """Abandon speculative work whose deadline has already passed.

        Rendering a clip that can no longer be shown spends a slot the next
        clip needs. Canon work is never dropped — it is late, not useless.
        """
        now = self.clock()
        with self._lock:
            keep, dropped = [], 0
            for j in self._heap:
                if j.priority >= Priority.SPECULATIVE and j.deadline < now:
                    dropped += 1
                else:
                    keep.append(j)
            self._heap = keep
            heapq.heapify(self._heap)
            self.stats["abandoned"] += dropped
            return dropped

    # -- execution ------------------------------------------------------
    def _next(self) -> Job | None:
        with self._lock:
            return heapq.heappop(self._heap) if self._heap else None

    def run(self, until_empty: bool = True, on_result=None) -> dict:
        """Drain the queue with a bounded worker pool.

        Threads are correct here despite the GIL: every job is a network call
        that releases it, and the measured 3.5x speedup on 4 slots confirms the
        provider genuinely parallelises.
        """
        from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

        pending: set = set()
        with ThreadPoolExecutor(max_workers=self.slots) as ex:
            while True:
                self.drop_expired()
                while len(pending) < self.slots:
                    job = self._next()
                    if job is None:
                        break
                    self._running[job.job_id] = job
                    fut = ex.submit(self._execute, job)
                    fut._job = job          # type: ignore[attr-defined]
                    pending.add(fut)
                if not pending:
                    if until_empty:
                        break
                    time.sleep(0.05)
                    continue
                finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in finished:
                    job = fut._job          # type: ignore[attr-defined]
                    self._running.pop(job.job_id, None)
                    try:
                        path = fut.result()
                        self.done[job.job_id] = path
                        self.stats["rendered"] += 1
                        late = self.clock() > job.deadline
                        if late:
                            self.stats["missed"] += 1
                        if on_result:
                            on_result(job, path, late)
                    except Exception as exc:
                        self.stats["fallback"] += 1
                        if on_result:
                            on_result(job, None, True, exc)
        return dict(self.stats)

    def _execute(self, job: Job) -> Path:
        assert job.render is not None
        return job.render()

    # -- introspection --------------------------------------------------
    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._heap)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [{"id": j.job_id, "priority": j.priority.name,
                     "branch": j.branch, "beat": j.beat_id,
                     "due_in": round(j.deadline - self.clock(), 1)}
                    for j in sorted(self._heap)]


def plan_scene(beat_id: str, head_shots: int, tail_shots: int,
               choices: list[str], shot_seconds: float = 5.0) -> dict:
    """Job counts and the deadline each shot must meet.

    Head shots are canon — they play regardless of the vote. Branch heads are
    speculative: one full branch is discarded. Tails are generated only for
    the winner, which is what the head/tail split buys.
    """
    play = head_shots * shot_seconds
    return {
        "canon_head": head_shots,
        "speculative": len(choices) * head_shots,
        "tail_after_vote": tail_shots,
        "vote_closes_at": play * 0.6,
        "tail_deadline": play,
        "discarded_if_two_choices": (len(choices) - 1) * head_shots,
    }
