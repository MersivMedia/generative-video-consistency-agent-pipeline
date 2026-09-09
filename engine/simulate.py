"""M3 screening simulator — does the buffer actually hold?

Answers the question the architecture rests on: with N parallel render slots
and measured provider latency, does the next clip exist before the current one
ends, for a whole screening?

Uses MEASURED numbers, not assumptions:
  * ref2v 5s clip: 5.6-6.1s inference (renders/*/shots.json)
  * 4 concurrent jobs: 4.6s wall vs 16s serial (3.5x speedup)
  * upload of cached reference plates: 0.05s vs 3.3s cold

Run: python engine/simulate.py stories/the_signal.json --slots 4
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from render_queue import Priority, RenderQueue  # noqa: E402

# Measured on fal H3 Max, 5s clips at 768P. Inference is the floor; the rest
# is queue wait, upload and download. Reference upload is now cached, so the
# per-shot cost is inference plus transfer only.
INFERENCE = (5.6, 6.1)
OVERHEAD = (1.5, 3.0)       # submit + poll + fetch, no re-upload
SHOT_SECONDS = 5.0


class SimClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, d): self.t += d


def simulate(beats: list[dict], slots: int, seed: int = 42,
             vote_close_frac: float = 0.40) -> dict:
    """Discrete-event sim of one screening.

    Models the pipeline as it is actually built: head shots are canon and must
    be ready when the previous scene ends; branch heads are speculative and one
    full branch is thrown away; the tail is generated only after the vote, on
    the critical path with no alternative.
    """
    rng = random.Random(seed)
    playhead = 0.0          # when the currently-playing material runs out
    misses, results, waste = [], [], 0
    rendered = 0
    prev_scene_play = 0.0   # 0 => first beat, pre-rendered before curtain up

    for b in beats:
        head_n = b.get("head_shots", 3)
        tail_n = b.get("tail_shots", 2)
        n_choices = 2

        # --- speculative branch heads, rendered during the previous scene ---
        spec_jobs = n_choices * head_n
        # With `slots` in parallel, wall time for k jobs is ceil(k/slots) waves.
        waves = -(-spec_jobs // slots)
        spec_wall = sum(rng.uniform(*INFERENCE) + rng.uniform(*OVERHEAD)
                        for _ in range(waves))
        rendered += spec_jobs
        waste += (n_choices - 1) * head_n

        head_play = head_n * SHOT_SECONDS
        # Branch heads for THIS beat render during the PREVIOUS beat's full
        # scene (head + tail), not during this one. That is the entire point of
        # speculating: the work is done before it is needed. The first beat is
        # the exception — nothing precedes it, so it is pre-rendered offline
        # before the screening starts and is excluded from the deadline count.
        budget = prev_scene_play if prev_scene_play > 0 else float("inf")
        if spec_wall > budget:
            misses.append({"beat": b["id"], "phase": "head",
                           "over_by": round(spec_wall - budget, 1)})

        # --- vote closes partway through the head ---
        playhead += head_play

        # --- tail: critical path, only renderable AFTER the vote ---
        tail_waves = -(-tail_n // slots)
        tail_wall = sum(rng.uniform(*INFERENCE) + rng.uniform(*OVERHEAD)
                        for _ in range(tail_waves))
        rendered += tail_n
        # Vote close fraction is the single most load-bearing parameter in the
        # whole system: it sets the tail's runway. At 60% the runway is 6.0s
        # against a ~7.1-9.1s minimum tail wave — structurally impossible, and
        # NO number of render slots fixes it, because a tail cannot start
        # before the vote it depends on. Measured: 9/9 tails missed at every
        # slot count from 1 to 8.
        tail_budget = head_play * (1.0 - vote_close_frac)
        late = tail_wall - tail_budget
        if late > 0:
            misses.append({"beat": b["id"], "phase": "tail",
                           "over_by": round(late, 1)})
        results.append({"beat": b["id"], "head_wall": round(spec_wall, 1),
                        "head_budget": round(budget, 1),
                        "tail_wall": round(tail_wall, 1),
                        "tail_budget": round(tail_budget, 1)})
        playhead += tail_n * SHOT_SECONDS
        prev_scene_play = head_play + tail_n * SHOT_SECONDS

    return {"slots": slots, "beats": len(beats), "clips": rendered,
            "waste": waste, "runtime_s": round(playhead, 1),
            "misses": misses, "miss_rate": round(len(misses) / (len(beats) * 2), 3),
            "detail": results}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("story", type=Path)
    ap.add_argument("--slots", type=int, default=4)
    ap.add_argument("--sweep", action="store_true",
                    help="try 1..8 slots and report where the buffer holds")
    ap.add_argument("--trials", type=int, default=20)
    ap.add_argument("--vote-close", type=float, default=0.40,
                    help="fraction of the head after which voting closes; "
                         "the tail's runway is what remains")
    a = ap.parse_args()

    story = json.loads(a.story.read_text())
    beats = [b for ch in story["chapters"] for b in ch["beats"]]

    if a.sweep:
        print(f"{'slots':<7}{'miss rate':<12}{'tail misses':<14}{'verdict'}")
        for s in range(1, 9):
            rates, tails = [], []
            for t in range(a.trials):
                r = simulate(beats, s, seed=t, vote_close_frac=a.vote_close)
                rates.append(r["miss_rate"])
                tails.append(sum(1 for m in r["misses"] if m["phase"] == "tail"))
            mr = statistics.mean(rates)
            tm = statistics.mean(tails)
            verdict = ("HOLDS" if mr == 0 else
                       "cutaways cover it" if mr < 0.15 else "BUFFER COLLAPSES")
            print(f"{s:<7}{mr:<12.3f}{tm:<14.1f}{verdict}")
        return

    r = simulate(beats, a.slots, vote_close_frac=a.vote_close)
    print(f"slots={r['slots']}  beats={r['beats']}  clips={r['clips']}  "
          f"waste={r['waste']}  runtime={r['runtime_s']}s")
    print(f"deadline misses: {len(r['misses'])}  "
          f"(rate {r['miss_rate']:.1%} — each is one cutaway)")
    for m in r["misses"]:
        print(f"   {m['beat']:<20} {m['phase']:<6} over by {m['over_by']}s")


if __name__ == "__main__":
    main()
