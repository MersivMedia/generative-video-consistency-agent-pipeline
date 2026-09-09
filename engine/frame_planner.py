"""Frame planner — the cheap image layer that runs ahead of the expensive clip layer.

Prepared frames and generated clips differ in cost by ~2 orders of magnitude, so
they get different lookahead depths:

    images   depth 3-4   cheap, prunable, discardable
    video    depth 1     expensive, head/tail split

Because the beat skeleton and branch axes are authored in advance, every frame the
story could plausibly need is computable before anyone votes. Frames come from i2i
against the character sheets and world plates, so identity and location locks
propagate into the whole prepared tree.

This module plans and prunes that tree. It emits i2i job specs; it does not call a
provider. Wire the specs into ComfyUI (see the `comfyui` skill) at M1.5.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass
class FrameJob:
    """One i2i generation: a prepared first- or last-frame for a possible shot."""
    key: str               # branch path, e.g. "b2_the_hail/A/b3_the_logbook/B"
    beat_id: str
    depth: int             # 0 = current beat, 1 = next, ...
    position: str          # "first" | "last"
    shot_index: int
    phase: str             # "head" | "tail"
    source_refs: list[str]  # character sheets / world plates driving the i2i
    prompt_hint: str
    status: str = "planned"  # planned | rendered | pruned


class FramePlanner:
    # Depth 2 = video layer depth (1) + one scene of slack. Measured: frames
    # generated roughly DOUBLE per extra level (88/166/298/538/922 across a
    # 9-beat run) while frames actually consumed stay flat at 5. Utilization
    # 5.7% -> 0.5%. Extra depth buys nothing because a frame only has to exist
    # before the clip consuming it is SUBMITTED, and clips lead playback by one
    # scene. Use required_depth() if i2i is slow relative to scene length.
    DEFAULT_DEPTH = 2

    def __init__(self, story: dict, depth: int = DEFAULT_DEPTH):
        self.story = story
        self.depth = depth
        self.beats = [b for ch in story["chapters"] for b in ch["beats"]]
        self.by_id = {b["id"]: b for b in self.beats}
        self.order = [b["id"] for b in self.beats]
        self.jobs: dict[str, FrameJob] = {}
        # Canon path as resolved so far, e.g. ["b1_arrival:A", "b2_the_hail:B"].
        # Any planned frame that does not descend from this is dead.
        self.canon_path: list[str] = []

    # -- refs ------------------------------------------------------------

    def _refs_for(self, beat: dict) -> list[str]:
        """Every locked asset a beat could plausibly need.

        The planner is deliberately generous: a spare reference costs an image,
        a missing one costs an unconditioned clip.
        """
        refs: list[str] = []
        for c in self.story["characters"].values():
            refs += c.get("visual_lock_refs", [])
        refs.append(f"plates/{beat['id']}_plate.png")
        return refs

    # -- planning --------------------------------------------------------

    def plan(self, from_beat: str, path_prefix: str = "") -> list[FrameJob]:
        """Build the frame tree `depth` levels forward from `from_beat`.

        Binary branching: at depth d there are 2**d possible paths, so depth 3
        is 8 leaves and depth 4 is 16 — trivial as images, prohibitive as clips.
        """
        start = self.order.index(from_beat)
        planned: list[FrameJob] = []

        def walk(idx: int, depth: int, key: str) -> None:
            if depth > self.depth or idx >= len(self.order):
                return
            beat = self.by_id[self.order[idx]]
            refs = self._refs_for(beat)
            head_n = beat.get("head_shots", 1)
            tail_n = beat.get("tail_shots", 1)

            for phase, n in (("head", head_n), ("tail", tail_n)):
                for i in range(n):
                    # First shot of a chain needs a first-frame; every shot
                    # needs a last-frame so `flf` mode is fully specified.
                    positions = ["first", "last"] if i == 0 else ["last"]
                    for pos in positions:
                        jk = f"{key}|{beat['id']}|{phase}{i}|{pos}"
                        job = FrameJob(
                            key=jk, beat_id=beat["id"], depth=depth,
                            position=pos, shot_index=i, phase=phase,
                            source_refs=refs,
                            prompt_hint=f"{beat['premise'][:110]} "
                                        f"[{phase} shot {i}, {pos} frame]",
                        )
                        self.jobs[jk] = job
                        planned.append(job)

            # Branch: both choice outcomes lead to the next beat, but the
            # prepared frames differ because the fiction differs.
            for choice in ("A", "B"):
                walk(idx + 1, depth + 1, f"{key}/{beat['id']}:{choice}")

        walk(start, 0, path_prefix)
        return planned

    # -- live pruning ----------------------------------------------------

    def resolve(self, beat_id: str, chosen: str) -> dict:
        """A vote landed. Drop everything off the canon path, extend the survivor.

        Pruning must be measured against the CANON PATH, not just the losing
        choice at this beat. Pruning only the immediate loser leaves stale
        subtrees from earlier speculation alive, and the live-frame count grows
        without bound instead of holding steady.
        """
        self.canon_path.append(f"{beat_id}:{chosen}")
        prefix = "/".join(self.canon_path)

        pruned = 0
        for job in self.jobs.values():
            if job.status != "planned":
                continue
            # A job survives only if its key lies on the canon path: every
            # branch decision recorded in its key must match ours.
            decisions = [seg for seg in job.key.split("/") if ":" in seg]
            if any(d not in self.canon_path for d in decisions):
                job.status = "pruned"
                pruned += 1

        idx = self.order.index(beat_id)
        extended: list[FrameJob] = []
        if idx + 1 < len(self.order):
            nxt = self.order[idx + 1]
            before = set(self.jobs)
            self.plan(nxt, path_prefix=prefix)
            extended = [self.jobs[k] for k in self.jobs if k not in before]

        return {"pruned": pruned, "extended": len(extended),
                "live": sum(1 for j in self.jobs.values() if j.status == "planned")}

    def stats(self) -> dict:
        s = {"planned": 0, "pruned": 0, "rendered": 0}
        for j in self.jobs.values():
            s[j.status] = s.get(j.status, 0) + 1
        return s


def sweep(story: dict, max_depth: int = 5, trials: int = 8) -> None:
    """Measure frame utilization vs depth over simulated screenings.

    A frame is only ever USED if it sits on the canon path. At depth d a binary
    tree has 2**d leaves, so utilization halves with every extra level while
    the benefit is capped by WHEN the frame is actually needed.
    """
    import random
    print(f"{'depth':>6} {'generated':>10} {'used':>7} {'utilization':>12} "
          f"{'live_peak':>10}")
    for d in range(1, max_depth + 1):
        gen_t, used_t, peak_t = [], [], []
        for t in range(trials):
            rng = random.Random(t)
            fp = FramePlanner(story, depth=d)
            fp.plan(fp.order[0])
            peak = 0
            for bid in fp.order:
                peak = max(peak, sum(1 for j in fp.jobs.values()
                                     if j.status == "planned"))
                fp.resolve(bid, rng.choice(("A", "B")))
            s = fp.stats()
            gen = sum(s.values())
            # Frames on the canon path are the ones that were actually needed.
            used = sum(1 for j in fp.jobs.values() if j.status == "planned")
            gen_t.append(gen)
            used_t.append(used)
            peak_t.append(peak)
        g = sum(gen_t) / trials
        u = sum(used_t) / trials
        pk = sum(peak_t) / trials
        print(f"{d:>6} {g:>10.0f} {u:>7.0f} {100 * u / g:>11.1f}% {pk:>10.0f}")


def required_depth(frame_gen_seconds: float, scene_seconds: float) -> int:
    """The principled depth, not a guess.

    A frame must exist before the CLIP that consumes it is submitted, and clips
    are submitted one scene ahead of playback. So the frame layer needs to lead
    the video layer by however many scenes frame generation takes, plus one
    scene of slack against a slow or failed i2i job.
    """
    import math
    lead = math.ceil(frame_gen_seconds / max(scene_seconds, 1))
    return max(1, lead) + 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("story")
    ap.add_argument("--depth", type=int, default=FramePlanner.DEFAULT_DEPTH)
    ap.add_argument("--sweep", action="store_true",
                    help="measure frame utilization across depths 1-5")
    ap.add_argument("--simulate", action="store_true",
                    help="walk the whole spine, pruning at each beat")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    story = json.loads(Path(args.story).read_text())
    fp = FramePlanner(story, depth=args.depth)
    first = fp.order[0]
    initial = fp.plan(first)

    print(f"story: {story['title']}")
    print(f"depth: {args.depth}  beats: {len(fp.order)}")
    print(f"initial frame tree: {len(initial)} i2i jobs "
          f"({2 ** args.depth} leaf paths)")

    if args.sweep:
        sweep(story)
        print()
        for fg, sc in ((10, 35), (30, 35), (60, 35), (120, 35)):
            print(f"  i2i {fg:>3}s per frame, {sc}s scenes -> "
                  f"required depth {required_depth(fg, sc)}")
        return

    if args.simulate:
        print("\nsimulating a full screening (prune + extend per vote):")
        for i, bid in enumerate(fp.order):
            r = fp.resolve(bid, "A" if i % 2 == 0 else "B")
            print(f"  {bid:20s} pruned {r['pruned']:4d}  "
                  f"extended {r['extended']:4d}  live {r['live']:5d}")
        s = fp.stats()
        print(f"\ntotals: {s}")
        print(f"image jobs touched across the run: {sum(s.values())}")
        print("Compare: ~225 clip generations. Images are ~2 orders of magnitude "
              "cheaper per unit, so this layer is a rounding error on the budget.")

    if args.out:
        Path(args.out).write_text(json.dumps(
            [asdict(j) for j in fp.jobs.values()], indent=2))
        print(f"-> {args.out}")


if __name__ == "__main__":
    main()
