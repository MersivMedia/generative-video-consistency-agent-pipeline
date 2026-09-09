"""THIS WAY — showrunner state machine (Milestone 1, text-only).

Canon is owned by this module, not by the model. The LLM proposes; the state
machine validates and commits. Every accepted turn is appended to an
immutable canon log so a run is fully reconstructable.
"""
from __future__ import annotations

import copy
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ----------------------------------------------------------------- schema

SHOT_SCHEMA = {
    "type": "object",
    "required": ["slug", "video_prompt", "duration", "mode"],
    "additionalProperties": False,
    "properties": {
        "slug": {"type": "string", "minLength": 3, "maxLength": 80},
        "video_prompt": {"type": "string", "minLength": 40},
        # Provider allows 5-15s, but MEASURED drift forces a tighter window:
        # at 13-14s the model re-stages and relights inside a single take.
        # Real range comes from the story's shot_budget; schema is permissive.
        "duration": {"type": "integer", "minimum": 4, "maximum": 15},
        "mode": {"type": "string", "enum": ["ref2v", "flf", "t2v"]},
        "narration": {"type": "string"},
        "dialogue": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["character", "line"],
                "additionalProperties": False,
                "properties": {
                    "character": {"type": "string"},
                    "line": {"type": "string", "minLength": 1},
                },
            },
        },
        "characters_on_screen": {"type": "array", "items": {"type": "string"}},
        # Pins which side of a doorway/threshold the camera is on. Without it
        # the model produced a character standing inside a doorway with the
        # exterior landscape behind her.
        "camera_side": {"type": "string"},
    },
}

# Phase 1 of a scene: the shots that play WHILE VOTING IS OPEN, plus the choices.
HEAD_SCHEMA = {
    "type": "object",
    "required": ["scene", "choices"],
    "additionalProperties": False,
    "properties": {
        "scene": {
            "type": "object",
            "required": ["slug", "shots"],
            "additionalProperties": False,
            "properties": {
                "slug": {"type": "string", "minLength": 3, "maxLength": 80},
                "shots": {"type": "array", "minItems": 1, "maxItems": 3,
                          "items": SHOT_SCHEMA},
            },
        },
        "choices": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {
                "type": "object",
                "required": ["id", "label", "kind", "deltas"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "enum": ["A", "B"]},
                    "label": {"type": "string", "minLength": 3, "maxLength": 150},
                    "kind": {"type": "string", "enum": ["action", "dialogue"]},
                    "consequence_hint": {"type": "string"},
                    "deltas": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "set_flags": {
                                "type": "object",
                                "additionalProperties": {"type": "boolean"},
                            },
                            "trust": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["from", "to", "amount"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "from": {"type": "string"},
                                        "to": {"type": "string"},
                                        "amount": {"type": "integer",
                                                   "minimum": -4, "maximum": 4},
                                    },
                                },
                            },
                            "memories": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["character", "memory"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "character": {"type": "string"},
                                        "memory": {"type": "string", "minLength": 5},
                                    },
                                },
                            },
                            "world_facts": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
    },
}


# Phase 2: consequence shots, generated just-in-time once the vote is known.
TAIL_SCHEMA = {
    "type": "object",
    "required": ["shots"],
    "additionalProperties": False,
    "properties": {
        "shots": {"type": "array", "minItems": 1, "maxItems": 3,
                  "items": SHOT_SCHEMA},
    },
}

TURN_SCHEMA = HEAD_SCHEMA  # back-compat alias


# Consecutive image-to-image chained shots allowed before a ref2v re-anchor.
#
# MEASURED AGAINST flf, three independent strikes:
#   identity   6/10 vs 8/10 for every surrounding ref2v shot
#   stability  the ONLY shot that still re-staged mid-take at 5s — location,
#              camera side, lighting and blocking all changed inside one take,
#              while every ref2v shot held steady
#   audio      i2v has no reference_audio_urls, so a chained shot can never
#              lip-sync to the synthesized dialogue
#
# The theoretical benefit was unbroken motion continuity. It does not survive
# contact with the measurements, so flf is OFF by default. Set to 1 only for a
# shot that genuinely must continue a physical action and carries no dialogue.
MAX_FLF_CHAIN = 0


class DeltaRejected(Exception):
    """A proposed turn violated canon rules and must be regenerated."""


# ----------------------------------------------------------------- state

@dataclass
class RunState:
    story_id: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    chapter_idx: int = 0
    beat_idx: int = 0
    flags: dict[str, bool] = field(default_factory=dict)
    characters: dict[str, dict] = field(default_factory=dict)
    world_facts: list[str] = field(default_factory=list)
    canon: list[dict] = field(default_factory=list)
    finished: bool = False

    def trust(self, a: str, b: str) -> int:
        return int(self.characters.get(a, {}).get("relationships", {}).get(b, 0))


class Showrunner:
    """Holds the authored spine, applies validated deltas, gates beats on flags."""

    def __init__(self, story_path: str | Path):
        self.story = json.loads(Path(story_path).read_text())
        self.beats: list[tuple[dict, dict]] = [
            (ch, b) for ch in self.story["chapters"] for b in ch["beats"]
        ]
        self.state = RunState(
            story_id=self.story["id"],
            characters=copy.deepcopy(self.story["characters"]),
            world_facts=list(self.story["world_facts"]),
            flags={k: False for k in self.story["flags_declared"]},
        )

    # -- spine navigation -------------------------------------------------

    def current_beat(self) -> dict | None:
        if self.state.beat_idx >= len(self.beats):
            return None
        return self.beats[self.state.beat_idx][1]

    def current_chapter(self) -> dict | None:
        if self.state.beat_idx >= len(self.beats):
            return None
        return self.beats[self.state.beat_idx][0]

    def gate_ok(self, beat: dict) -> bool:
        """`requires_flags` entries are ANDed; `a|b` inside one entry is OR."""
        for req in beat.get("requires_flags", []):
            if not any(self.state.flags.get(alt.strip(), False)
                       for alt in req.split("|")):
                return False
        return True

    def advance(self) -> dict | None:
        """Move to the next beat whose flag gate is satisfied."""
        self.state.beat_idx += 1
        while self.state.beat_idx < len(self.beats):
            beat = self.beats[self.state.beat_idx][1]
            if self.gate_ok(beat):
                return beat
            self.state.canon.append({
                "type": "beat_skipped",
                "beat": beat["id"],
                "reason": f"gate unmet: {beat.get('requires_flags')}",
                "ts": time.time(),
            })
            self.state.beat_idx += 1
        self.state.finished = True
        return None

    # -- validation -------------------------------------------------------

    # Words that imply an interior/exterior boundary in frame. A shot whose
    # prompt contains one MUST pin camera_side: a generated shot put the camera
    # outside a lighthouse door and then showed open sea through the doorway,
    # so the building had exterior on both sides and no interior at all.
    BOUNDARY_WORDS = ("door", "doorway", "threshold", "window", "hatch",
                      "gate", "entry", "porch", "inside", "interior")

    def _check_shots(self, shots: list, phase: str) -> None:
        """Shared per-shot rules: duration ceiling, identity locks, chaining."""
        known_chars = set(self.state.characters)
        budget = self.story.get("shot_budget", {})
        lo = budget.get("min_seconds", 5)
        hi = budget.get("max_seconds", 15)

        for i, shot in enumerate(shots):
            if not lo <= shot["duration"] <= hi:
                raise DeltaRejected(
                    f"{phase} shot {i} duration {shot['duration']}s outside "
                    f"provider range {lo}-{hi}s")
            on_screen = shot.get("characters_on_screen", [])
            for name in on_screen:
                if name not in known_chars:
                    raise DeltaRejected(f"unknown character on screen: {name!r}")
            for line in shot.get("dialogue", []):
                if line["character"] not in known_chars:
                    raise DeltaRejected(
                        f"dialogue from unknown character: {line['character']!r}")
            named = [n for n in on_screen
                     if self.state.characters[n].get("visual_lock_refs")]
            if named and shot["mode"] == "t2v":
                raise DeltaRejected(
                    f"{phase} shot {i} has locked characters {named} but mode=t2v; "
                    "use ref2v or flf so reference locks apply")
            # Only the very first shot of a HEAD may open cold. Everything
            # mid-scene, and every TAIL shot, must chain or lock.
            if shot["mode"] == "t2v" and not (phase == "head" and i == 0):
                raise DeltaRejected(
                    f"{phase} shot {i} must chain (flf) or lock (ref2v), not t2v")

            # Lip-sync is only available on ref2v (it alone accepts
            # reference_audio_urls), so a dialogue shot on flf would have
            # mouths out of time with the words.
            if shot.get("dialogue") and shot["mode"] == "flf":
                raise DeltaRejected(
                    f"{phase} shot {i} has dialogue but mode=flf; dialogue shots "
                    "must be ref2v so the voice track can drive lip-sync")

            # Boundary shots must declare which side the camera is on AND what
            # lies beyond the opening. A generated shot put the camera outside a
            # lighthouse door and then showed open sea THROUGH the doorway, so
            # the building had exterior on both sides and no interior at all.
            hay = " ".join([
                shot["video_prompt"], shot.get("slug", ""),
                shot.get("narration") or "",
            ]).lower()
            if any(w in hay for w in self.BOUNDARY_WORDS):
                cs = (shot.get("camera_side") or "").lower()
                if not cs:
                    raise DeltaRejected(
                        f"{phase} shot {i} ({shot.get('slug')}) shows an "
                        "interior/exterior boundary but camera_side is empty. "
                        "State where the camera is and what is BEHIND the subject.")
                if not any(w in cs for w in
                           ("outside", "exterior", "inside", "interior")):
                    raise DeltaRejected(
                        f"{phase} shot {i} camera_side must say whether the camera "
                        f"is INSIDE or OUTSIDE; got {cs!r}")
                if "behind" not in cs:
                    raise DeltaRejected(
                        f"{phase} shot {i} camera_side must state what is BEHIND "
                        "the subject, e.g. 'camera OUTSIDE looking in; the warm "
                        "lamplit interior room is behind Wrenn'. A doorway showing "
                        "the same side on both sides is geometrically impossible.")

    def validate_head(self, turn: dict) -> None:
        import jsonschema
        jsonschema.validate(turn, HEAD_SCHEMA)

        beat = self.current_beat()
        declared = set(self.story["flags_declared"])
        allowed_beat_flags = set(beat.get("sets_flags", [])) if beat else set()

        shots = turn["scene"]["shots"]
        want = beat.get("head_shots") if beat else None
        if want and len(shots) != want:
            raise DeltaRejected(
                f"beat {beat['id']} declares head_shots={want} but head has {len(shots)}")
        self._check_shots(shots, "head")

        # Heads play during voting and buy runway for just-in-time tails.
        nominal = self.story.get("shot_budget", {}).get("head_nominal_seconds", 12)
        if sum(s["duration"] for s in shots) < nominal:
            raise DeltaRejected(
                f"head is only {sum(s['duration'] for s in shots)}s; needs >= {nominal}s "
                "of playback to cover the vote window and tail generation")

        known_chars = set(self.state.characters)
        seen_ids = set()
        for ch in turn["choices"]:
            if ch["id"] in seen_ids:
                raise DeltaRejected("duplicate choice id")
            seen_ids.add(ch["id"])
            d = ch["deltas"]
            for flag in d.get("set_flags", {}):
                if flag not in declared:
                    raise DeltaRejected(
                        f"undeclared flag {flag!r}; declared: {sorted(declared)}")
                if allowed_beat_flags and flag not in allowed_beat_flags:
                    raise DeltaRejected(
                        f"flag {flag!r} not settable in beat {beat['id']}; "
                        f"allowed: {sorted(allowed_beat_flags)}")
            for t in d.get("trust", []):
                if t["from"] not in known_chars or t["to"] not in known_chars:
                    raise DeltaRejected(f"trust delta references unknown character: {t}")
                if t["from"] == t["to"]:
                    raise DeltaRejected("trust delta from a character to itself")
            for m in d.get("memories", []):
                if m["character"] not in known_chars:
                    raise DeltaRejected(f"memory for unknown character: {m['character']!r}")

        if turn["choices"][0]["label"].strip().lower() == \
           turn["choices"][1]["label"].strip().lower():
            raise DeltaRejected("both choices are identical")

    def validate_tail(self, tail: dict) -> None:
        import jsonschema
        jsonschema.validate(tail, TAIL_SCHEMA)

        beat = self.current_beat()
        shots = tail["shots"]
        want = beat.get("tail_shots") if beat else None
        if want and len(shots) != want:
            raise DeltaRejected(
                f"beat {beat['id']} declares tail_shots={want} but tail has {len(shots)}")
        self._check_shots(shots, "tail")

        # Tails sit on the critical path with no cached alternate branch behind
        # them, so they must render fast.
        cap = self.story.get("shot_budget", {}).get("tail_nominal_seconds", 8)
        for i, s in enumerate(shots):
            if s["duration"] > cap + 2:
                raise DeltaRejected(
                    f"tail shot {i} is {s['duration']}s; tails are on the critical path "
                    f"and must stay near {cap}s so they render inside the head runway")

    # back-compat
    def validate_turn(self, turn: dict) -> None:
        self.validate_head(turn)

    # -- commit -----------------------------------------------------------

    def commit(self, turn: dict, chosen_id: str, votes: dict | None = None,
               tail: dict | None = None) -> dict:
        """Commit a resolved scene. `tail` is the just-in-time consequence shots,
        generated after the vote and therefore optional at head-resolve time."""
        chosen = next(c for c in turn["choices"] if c["id"] == chosen_id)
        d = chosen["deltas"]
        beat = self.current_beat()

        for flag, val in d.get("set_flags", {}).items():
            self.state.flags[flag] = bool(val)
        for t in d.get("trust", []):
            rel = self.state.characters[t["from"]].setdefault("relationships", {})
            rel[t["to"]] = max(-10, min(10, int(rel.get(t["to"], 0)) + t["amount"]))
        for m in d.get("memories", []):
            self.state.characters[m["character"]].setdefault("memory", []).append(m["memory"])
        for f in d.get("world_facts", []):
            if f not in self.state.world_facts:
                self.state.world_facts.append(f)

        scene = turn["scene"]
        head_shots = scene["shots"]
        tail_shots = (tail or {}).get("shots", [])
        all_shots = head_shots + tail_shots

        event = {
            "type": "canon",
            "beat": beat["id"] if beat else None,
            "chapter": self.current_chapter()["id"] if beat else None,
            "scene": {"slug": scene["slug"], "shots": all_shots},
            "head_shots": head_shots,
            "tail_shots": tail_shots,
            "head_seconds": sum(s["duration"] for s in head_shots),
            "tail_seconds": sum(s["duration"] for s in tail_shots),
            "scene_seconds": sum(s["duration"] for s in all_shots),
            "shot_count": len(all_shots),
            # Generation accounting: both branch heads were rendered
            # speculatively, only the winning tail was.
            "clips_generated": len(head_shots) * 2 + len(tail_shots),
            "clips_wasted": len(head_shots),
            "offered": [{"id": c["id"], "label": c["label"], "kind": c["kind"]}
                        for c in turn["choices"]],
            "chosen": chosen_id,
            "chosen_label": chosen["label"],
            "discarded": [c["id"] for c in turn["choices"] if c["id"] != chosen_id],
            "deltas_applied": d,
            "votes": votes,
            "flags_after": dict(self.state.flags),
            "trust_after": {c: dict(v.get("relationships", {}))
                            for c, v in self.state.characters.items()
                            if v.get("relationships")},
            "ts": time.time(),
        }
        self.state.canon.append(event)
        return event

    # -- prompt context ---------------------------------------------------

    def context_for_llm(self) -> dict:
        beat = self.current_beat()
        chapter = self.current_chapter()
        recent = [e for e in self.state.canon if e["type"] == "canon"][-4:]
        return {
            "story": {
                "title": self.story["title"],
                "logline": self.story["logline"],
                "style_bible": self.story["style_bible"],
            },
            "chapter": {"id": chapter["id"], "title": chapter["title"],
                        "premise": chapter["premise"]} if chapter else None,
            "beat": beat,
            "characters": {
                k: {
                    "name": v["name"], "role": v["role"], "traits": v["traits"],
                    "secret": v.get("secret"),
                    "memory": v.get("memory", [])[-6:],
                    "relationships": v.get("relationships", {}),
                } for k, v in self.state.characters.items()
            },
            "shot_budget": self.story.get("shot_budget", {}),
            "head_shots_required": beat.get("head_shots") if beat else None,
            "tail_shots_required": beat.get("tail_shots") if beat else None,
            "world_facts": self.state.world_facts,
            "flags": self.state.flags,
            "flags_declared": self.story["flags_declared"],
            "settable_flags_this_beat": beat.get("sets_flags", []) if beat else [],
            "payoff_contracts": self.story["payoff_contracts"],
            "recent_canon": [
                {"beat": e["beat"], "slug": e["scene"]["slug"],
                 "chosen": e["chosen_label"]} for e in recent
            ],
        }

    def context_for_tail(self, head: dict, chosen_id: str) -> dict:
        """Context for authoring the consequence shots. The winner is known, so
        the tail can be specific instead of hedging across both outcomes."""
        ctx = self.context_for_llm()
        chosen = next(c for c in head["choices"] if c["id"] == chosen_id)
        last = head["scene"]["shots"][-1]
        ctx["scene_so_far"] = {
            "slug": head["scene"]["slug"],
            "head_shots": [
                {"slug": s["slug"], "duration": s["duration"],
                 "video_prompt": s["video_prompt"],
                 "dialogue": s.get("dialogue", [])}
                for s in head["scene"]["shots"]
            ],
            "chain_from": {
                "slug": last["slug"],
                "video_prompt": last["video_prompt"],
                "note": "The tail's first shot chains from this shot's final frame.",
            },
        }
        ctx["audience_chose"] = {
            "id": chosen_id, "label": chosen["label"], "kind": chosen["kind"],
            "consequence_hint": chosen.get("consequence_hint"),
        }
        ctx["rejected"] = [c["label"] for c in head["choices"] if c["id"] != chosen_id]
        ctx["tail_shots_required"] = (self.current_beat() or {}).get("tail_shots")
        return ctx

    def snapshot(self) -> dict:
        return asdict(self.state)
