"""Run a full text-only screening and emit a readable script + canon log.

    python engine/run_story.py stories/the_signal.json --voter auto
    python engine/run_story.py stories/the_signal.json --voter interactive
    python engine/run_story.py stories/the_signal.json --voter seed --seed 7
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from showrunner import Showrunner  # noqa: E402
from writer import TurnWriter  # noqa: E402


def pick(turn, mode, rng):
    if mode == "interactive":
        for c in turn["choices"]:
            print(f"   [{c['id']}] {c['label']}  ({c['kind']})")
        while True:
            v = input("   vote A/B > ").strip().upper()
            if v in ("A", "B"):
                return v, None
    a = rng.randint(1, 100)
    votes = {"A": a, "B": 100 - a}
    return ("A" if votes["A"] >= votes["B"] else "B"), votes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("story")
    ap.add_argument("--voter", choices=["auto", "seed", "interactive"], default="auto")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--model", default="claude-sonnet-4-5")
    ap.add_argument("--out", default="runs")
    args = ap.parse_args()

    rng = random.Random(args.seed if args.voter == "seed" else None)
    sr = Showrunner(args.story)
    tw = TurnWriter(model=args.model)

    print(f"\n=== {sr.story['title']} ===\n{sr.story['logline']}\n")
    script: list[str] = [f"# {sr.story['title']}\n\n_{sr.story['logline']}_\n"]
    t0 = time.time()
    last_chapter = None

    while not sr.state.finished and sr.current_beat():
        beat, chap = sr.current_beat(), sr.current_chapter()
        if chap["id"] != last_chapter:
            hdr = f"\n## {chap['title']}\n\n{chap['premise']}\n"
            print(hdr)
            script.append(hdr)
            last_chapter = chap["id"]

        print(f"-- beat {beat['id']} --")
        # --- Phase 1: head plays while voting is open -------------------
        head = tw.write_head(sr)
        scene = head["scene"]
        head_secs = sum(s["duration"] for s in scene["shots"])

        block = [f"\n### {scene['slug']}  `{beat['id']}`\n"]
        block.append(f"\n**HEAD** — {len(scene['shots'])} shots / {head_secs}s "
                     f"_(plays during voting; both branches speculated)_\n")
        for s in scene["shots"]:
            block.append(f"\n**[{s['duration']}s · {s['mode']}]** {s['slug']}\n")
            if s.get("narration"):
                block.append(f"{s['narration']}\n")
            for ln in s.get("dialogue", []):
                who = sr.state.characters[ln["character"]]["name"].upper()
                block.append(f"**{who}**  \n{ln['line']}\n")
            block.append(f"> _prompt:_ {s['video_prompt']}\n")

        # --- vote resolves ---------------------------------------------
        chosen, votes = pick(head, args.voter, rng)

        # --- Phase 2: tail generated just-in-time, winner known ---------
        tail = tw.write_tail(sr, head, chosen)
        tail_secs = sum(s["duration"] for s in tail["shots"])

        ev = sr.commit(head, chosen, votes, tail=tail)
        opts = "  ".join(f"[{c['id']}] {c['label']}" for c in ev["offered"])
        block.append(f"\n**Choice:** {opts}\n\n**Canon:** {chosen} — {ev['chosen_label']}\n")

        block.append(f"\n**TAIL** — {len(tail['shots'])} shots / {tail_secs}s "
                     f"_(just-in-time; only the winner rendered)_\n")
        for s in tail["shots"]:
            block.append(f"\n**[{s['duration']}s · {s['mode']}]** {s['slug']}\n")
            if s.get("narration"):
                block.append(f"{s['narration']}\n")
            for ln in s.get("dialogue", []):
                who = sr.state.characters[ln["character"]]["name"].upper()
                block.append(f"**{who}**  \n{ln['line']}\n")
            block.append(f"> _prompt:_ {s['video_prompt']}\n")

        applied = ev["deltas_applied"]
        if applied.get("set_flags"):
            block.append(f"\n`flags: {applied['set_flags']}`\n")
        if applied.get("trust"):
            block.append("`trust: " + ", ".join(
                f"{t['from']}→{t['to']} {t['amount']:+d}" for t in applied["trust"]) + "`\n")
        block.append(f"`clips: {ev['clips_generated']} generated, "
                     f"{ev['clips_wasted']} discarded`\n")
        script += block
        print(f"   head {head_secs}s + tail {tail_secs}s = {ev['scene_seconds']}s | "
              f"canon {chosen} | clips {ev['clips_generated']} "
              f"(waste {ev['clips_wasted']})")
        sr.advance()

    out = Path(args.out) / f"{sr.state.story_id}_{sr.state.run_id}"
    out.mkdir(parents=True, exist_ok=True)

    canon = [e for e in sr.state.canon if e["type"] == "canon"]
    skipped = [e for e in sr.state.canon if e["type"] == "beat_skipped"]
    true_flags = [k for k, v in sr.state.flags.items() if v]
    payoffs = [p for p in sr.story["payoff_contracts"] if sr.state.flags.get(p["flag"])]

    script.append("\n---\n\n## Canon summary\n")
    total_shots = sum(e["shot_count"] for e in canon)
    total_secs = sum(e["scene_seconds"] for e in canon)
    script.append(f"- Scenes: **{len(canon)}**, shots: **{total_shots}**, "
                  f"runtime: **{total_secs}s ({total_secs / 60:.1f} min)**, "
                  f"beats skipped by flag gate: **{len(skipped)}**\n")
    gen = sum(e["clips_generated"] for e in canon)
    waste = sum(e["clips_wasted"] for e in canon)
    flat = total_shots * 2
    script.append(f"- Clip generations: **{gen}** "
                  f"({waste} discarded = {100 * waste / gen:.0f}% waste)\n")
    script.append(f"- Flat depth-2 speculation would have cost **{flat}** clips "
                  f"— head/tail split saves **{flat - gen}** ({100 * (flat - gen) / flat:.0f}%)\n")
    script.append(f"- Flags true: {', '.join(true_flags) or 'none'}\n")
    script.append(f"- Payoff contracts armed: **{len(payoffs)}**\n")
    for p in payoffs:
        script.append(f"  - `{p['flag']}` → {p['pays_off_in']}: {p['description']}\n")
    script.append("\n### Character state\n")
    for k, c in sr.state.characters.items():
        rel = c.get("relationships") or {}
        script.append(f"\n**{c['name']}** — trust {rel or '—'}\n")
        for m in c.get("memory", []):
            script.append(f"- {m}\n")

    (out / "script.md").write_text("".join(script))
    (out / "canon.json").write_text(json.dumps(sr.state.canon, indent=2))
    (out / "final_state.json").write_text(json.dumps(sr.snapshot(), indent=2))
    if tw.repair_log:
        (out / "repairs.log").write_text("\n".join(tw.repair_log))

    print(f"\n=== done in {time.time() - t0:.0f}s ===")
    gen = sum(e["clips_generated"] for e in canon)
    waste = sum(e["clips_wasted"] for e in canon)
    tshots = sum(e["shot_count"] for e in canon)
    print(f"scenes={len(canon)} shots={tshots} "
          f"runtime={sum(e['scene_seconds'] for e in canon)}s "
          f"clips={gen} waste={waste} ({100 * waste / gen:.0f}%) "
          f"vs_flat_depth2={tshots * 2} "
          f"skipped={len(skipped)} payoffs_armed={len(payoffs)} "
          f"llm_calls={tw.calls} repairs={len(tw.repair_log)}")
    print(f"flags true: {true_flags}")
    print(f"-> {out}/script.md")


if __name__ == "__main__":
    main()
