"""LLM turn writer — two-phase scene authoring.

Phase 1 (`write_head`): the shots that play while voting is open, plus the two
choices. Both branches' heads are speculated in production.

Phase 2 (`write_tail`): the consequence shots, authored AFTER the vote resolves.
Only the winning tail is ever generated — that is the 50% -> 33% waste saving,
and it also lets the consequence be specific instead of hedging both ways.

Retries feed the validator's own error message back so the model repairs its
output instead of us silently accepting bad canon.
"""
from __future__ import annotations

import json
import os
import re

from showrunner import DeltaRejected, Showrunner

_SHARED_RULES = """You do not own canon. You propose; a state machine validates and commits. Obey absolutely:

- Output ONE JSON object and nothing else. No markdown fence, no commentary.
- HARD SHOT LIMIT: every shot duration is an integer between shot_budget.min_seconds and
  shot_budget.max_seconds (4-6 seconds; nominal 5). The provider can render up to 15s but
  MEASURED drift forbids it: at 13-14s the model re-stages blocking, relights the set,
  changes location and re-ages characters INSIDE a single take. Write ONE beat of action
  per shot — a single gesture, a single line, a single camera move. If an action needs
  longer than 6 seconds, split it into two shots.
- Shot modes, one per shot:
  - "ref2v"  reference-to-video. THE DEFAULT. Applies the character reference sheet and
             re-anchors identity. Use this for almost every shot with a named character.
  - "flf"    DO NOT USE. Measured worse than ref2v on every axis: identity 6/10 vs
             8/10, the only shot that still re-staged location/lighting/blocking
             mid-take at 5 seconds, and it cannot lip-sync to dialogue. Use "ref2v"
             for every shot with a character in it, including shots that continue an
             action — re-anchoring to the reference sheet beats chaining.
  - "t2v"    text-to-video. Legal ONLY as the very first shot of a scene HEAD, and only
             when no locked character is on screen (empty landscapes, weather, inserts).
- video_prompt: one or two dense cinematic sentences — subject, action, camera, light.
  Always append the style_bible verbatim at the end. Name characters by full name so
  reference locks apply. No dialogue text inside video_prompt.
- characters_on_screen uses the character KEYS from the characters object, not names.
- MODE POLICY: use "ref2v" for EVERY shot containing a character, including shots that
  continue an action. Do NOT use "flf": measured worse than ref2v on identity (6/10 vs
  8/10), it was the only shot that still re-staged location/lighting/blocking mid-take
  at 5 seconds, and it cannot lip-sync dialogue. "t2v" is legal ONLY as a scene's first
  shot with no character on screen.
- camera_side: MANDATORY on any shot mentioning a door, doorway, threshold, window,
  hatch, gate, entry, porch, inside or interior — in the video_prompt, the slug OR the
  narration. It is REJECTED unless it (a) contains the word OUTSIDE/EXTERIOR or
  INSIDE/INTERIOR, and (b) contains the word "behind" naming what lies behind the
  subject. Examples:
    "camera is OUTSIDE in the storm looking IN through the open doorway; the warm
     lamplit interior room is behind Wrenn"
    "camera is INSIDE the hallway looking OUT; the black storm and breaking sea are
     behind Okonjo"
  A generated shot placed the camera outside a lighthouse door and then showed open sea
  THROUGH the doorway, so the building had exterior on both sides and no interior. Also
  describe that interior/exterior content inside video_prompt itself.
- Continuity is the whole product. Never contradict world_facts, flags, or memories."""

HEAD_SYSTEM = f"""You are the SHOWRUNNER of a live branching AI film. You write the HEAD of one scene.

The head is the footage that plays WHILE THE AUDIENCE IS VOTING. It sets up the choice; it
does not resolve it. The consequence shots are written later, once the vote is known.

{_SHARED_RULES}

HEAD-SPECIFIC RULES:
- Emit EXACTLY head_shots_required shots.
- Heads must cover the vote window AND the wall time to generate the tail just-in-time,
  but do it with MORE SHOTS, not longer ones: 3 shots of 5s, never 1 shot of 15s. Total
  head playback must reach shot_budget.head_nominal_seconds or it will be rejected.
- End the head on the brink of the decision. The last shot should leave the choice open,
  not answer it.
- Offer exactly TWO choices, ids "A" and "B", genuinely different in consequence rather
  than rephrasings. Follow the beat's branch_axis.
- Flags: ONLY flags listed in settable_flags_this_beat. Never invent flags. If the beat
  offers a flag per side (one for acting, one for refusing), give each choice its own.
- Trust deltas: existing characters only, never self-to-self, amount -4..4.
- Memories: first person, what that character will REMEMBER. Concrete and quotable —
  these are how payoffs land later.
- Honour payoff_contracts: if a flag is already true and its contract points at this
  beat, pay it off explicitly.

JSON shape:
{{"scene":{{"slug":"","shots":[
   {{"slug":"","video_prompt":"","duration":5,"mode":"ref2v","narration":"",
    "camera_side":"camera is OUTSIDE ... ; ... is behind <name>",
    "dialogue":[{{"character":"key","line":""}}],"characters_on_screen":["key"]}}
 ]}},
 "choices":[{{"id":"A","label":"","kind":"action|dialogue","consequence_hint":"","deltas":{{"set_flags":{{}},"trust":[{{"from":"key","to":"key","amount":0}}],"memories":[{{"character":"key","memory":""}}],"world_facts":[]}}}},
            {{"id":"B","label":"","kind":"action|dialogue","consequence_hint":"","deltas":{{}}}}]}}"""

TAIL_SYSTEM = f"""You are the SHOWRUNNER of a live branching AI film. You write the TAIL of one scene.

The audience has ALREADY VOTED. You know exactly what they chose. Write the consequence
footage that plays immediately after. Because the winner is known, be SPECIFIC — do not
hedge across outcomes, do not restate the choice, show what it cost or bought.

{_SHARED_RULES}

TAIL-SPECIFIC RULES:
- Emit EXACTLY tail_shots_required shots.
- Tails must be SHORT — target tail_nominal_seconds (around 5s per shot). Tails are
  generated just-in-time on the critical path with no cached alternative behind them, so
  long tails risk dead air and will be rejected.
- ANY shot containing dialogue must use "ref2v", never "flf". Only ref2v accepts the
  synthesized voice track as reference audio, which is what makes the mouth lip-sync to
  the words. A dialogue shot on flf will be out of sync.
- The FIRST tail shot may chain from scene_so_far.chain_from via mode "flf" if it continues
  an unbroken motion AND has no dialogue; otherwise use "ref2v" to re-anchor identity. Subsequent tail shots
  must be "ref2v" — never two flf in a row. No tail shot may ever be "t2v".
- Land the beat's exit_conditions. This is the shot that makes the choice feel real.
- Do NOT emit choices, flags, trust or memories — those were committed with the head.
  Output only shots.

JSON shape:
{{"shots":[
   {{"slug":"","video_prompt":"","duration":5,"mode":"ref2v","narration":"",
    "camera_side":"camera is OUTSIDE ... ; ... is behind <name>",
    "dialogue":[{{"character":"key","line":""}}],"characters_on_screen":["key"]}}
 ]}}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, depth = text.find("{"), 0
    if start < 0:
        raise ValueError("no JSON object in model output")
    for i, ch in enumerate(text[start:], start):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in model output")


class TurnWriter:
    def __init__(self, model: str = "claude-sonnet-4-5", max_repairs: int = 3):
        import anthropic
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("ANTHROPIC_API_KEY not set")
        self.client = anthropic.Anthropic(api_key=key)
        self.model = model
        self.max_repairs = max_repairs
        self.repair_log: list[str] = []
        self.calls = 0

    def _generate(self, system: str, ctx: dict, ask: str, validate, phase: str,
                  beat_id: str) -> dict:
        msgs = [{"role": "user",
                 "content": f"STATE:\n{json.dumps(ctx, indent=1)}\n\n{ask}"}]
        for attempt in range(self.max_repairs + 1):
            self.calls += 1
            resp = self.client.messages.create(
                model=self.model, max_tokens=4000, system=system, messages=msgs)
            raw = "".join(b.text for b in resp.content if b.type == "text")
            try:
                out = _extract_json(raw)
                validate(out)
                return out
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self.repair_log.append(
                    f"beat={beat_id} phase={phase} try={attempt} {err[:200]}")
                if attempt == self.max_repairs:
                    raise DeltaRejected(
                        f"{phase} rejected after {attempt + 1} tries: {err}")
                msgs += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content":
                        f"REJECTED by the canon validator:\n{err}\n\n"
                        "Fix exactly that and return the corrected JSON object only."},
                ]
        raise AssertionError("unreachable")

    def write_head(self, sr: Showrunner) -> dict:
        return self._generate(
            HEAD_SYSTEM, sr.context_for_llm(),
            "Write the HEAD of the next scene, plus the two choices.",
            sr.validate_head, "head", sr.current_beat()["id"])

    def write_tail(self, sr: Showrunner, head: dict, chosen_id: str) -> dict:
        return self._generate(
            TAIL_SYSTEM, sr.context_for_tail(head, chosen_id),
            "The audience has voted. Write the TAIL consequence shots.",
            sr.validate_tail, "tail", sr.current_beat()["id"])

    # back-compat single-phase
    def write_turn(self, sr: Showrunner) -> dict:
        return self.write_head(sr)
