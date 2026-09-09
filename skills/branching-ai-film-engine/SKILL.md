---
name: branching-ai-film-engine
description: "Build live branching AI film engines with audience voting."
version: 1.0.0
author: Nous Research
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [video-generation, interactive-film, replicate, showrunner, character-consistency, speculative-rendering]
    related_skills: [replicate-api-generation, comfyui]
    category: creative
---

# Branching AI Film Engine

Build a live film that generates itself ahead of the viewer: the audience votes at
branch points, futures render in parallel while voting is open, the winner becomes
canon. Reference implementation: `~/thisway/` (THIS WAY).

## When to Use

- Building interactive or branching generative video where viewers choose what happens next
- Audience-voted / live film experiences, "choose your own adventure" video
- Designing a "showrunner" story engine that must hold continuity across generated scenes
- Any pipeline where video generation must stay ahead of playback (speculative rendering)
- Character/scene consistency work for generated video (reference sheets, world plates)

Do NOT use for single one-off video generation — reach for `replicate-api-generation`
or `comfyui` directly instead.

The premise that makes it possible is that generative video is now **faster than
playback**, so the viewer never waits on a model. Every design decision below serves
two goals: **hide latency** and **prevent narrative drift**.

## 1. Verify model limits BEFORE writing any spec

The single most expensive mistake is assuming a clip length. Check the real ceiling
first — it drives shot structure, vote windows, and the entire cost model.

```bash
curl -s "https://api.replicate.com/v1/models/<owner>/<model>" \
  -H "Authorization: Bearer $REPLICATE_API_TOKEN" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(json.dumps(d['latest_version']['openapi_schema']['components']['schemas']['Input']['properties'],indent=2))"
```

Known ceilings (verified, subject to change — re-check):

| Model | Duration | Notes |
|---|---|---|
| MiniMax H3 (fal) | 5–15s | 2K, first-and-last-frame, reference-to-video |
| H3 Max (fal) | 5–15s | 768p, ~3s wall time for a 5s clip |
| bytedance/seedance-2.0 | up to 15s | `image` + `last_frame_image` + up to 9 `reference_images` |

**15 seconds is the ceiling everywhere.** A PRD written around 25-second shots is
wrong by 3x on cost. Ask "what is the max duration" before anything else.

## 2. Showrunner: the model does NOT own canon

A structured state machine mutated by an LLM under constraints. The LLM proposes;
the validator checks; only `commit()` mutates state. Every accepted turn appends to
an immutable canon log.

```
Story     = { chapters[], beats[], characters{}, world_facts[], flags{} }
Character = { name, visual_lock_refs[], voice_id, traits[], memory[], relationships{trust:int} }
Beat      = { id, premise, exit_conditions, branch_axis, head_shots, tail_shots,
              sets_flags[], requires_flags[] }
```

Author the **chapter/beat skeleton ahead of time**; generate only the branches
within a beat. That split is the anti-slop mechanism — the audience *uncovers* a
plot instead of inventing a random one.

Validation rules that earn their keep:

- `duration` integer within the provider's real range, per shot
- exact shot count per beat (`head_shots` / `tail_shots`)
- flags must be **declared** in the story AND **settable in this beat**
- trust deltas: existing characters only, never self-to-self, bounded range
- both choices must differ
- **mode discipline** (see §4) — the strongest single rule

On rejection, feed the validator's own error message back to the model and let it
repair. Never silently accept bad canon.

### Flag gating and payoffs

`requires_flags` entries are ANDed; `a|b` inside one entry is OR. `payoff_contracts`
map a flag to the beat where it must come due.

**Pitfall — single-flag gates delete content.** Two beats gated on the same lone flag
means one early vote silently removes a third of a chapter. Gate on alternatives
(`a|b|c`).

**Pitfall — asymmetric flags make payoffs vote-dependent.** If only the *active* side
of an early branch sets a flag, resistant audiences arm far fewer payoffs than
cooperative ones. Give **both sides** of an early branch their own flag and contract
(`answered_the_hail` / `stayed_silent`). Verified: this lifted the payoff floor from
2 to 3 while the ceiling stayed at 5.

## 3. Head/tail split — speculate less, write better

Divide each scene into a **head** (plays while voting is open) and a **tail**
(consequence shots after the vote). Speculate only heads; generate the winning tail
just-in-time.

```
t=0    play head (2 shots)  ─┬─ speculate branch A head
                              └─ speculate branch B head
t=+12  voting opens
t=+24  voting closes         winner promoted, loser head evicted
t=+24  ── generate WINNER tail just-in-time ──
```

| Strategy | Clips per 4-shot branch point | Waste |
|---|---|---|
| Full speculation | 8 | 50% |
| Head-only | 6 | 33% |

Measured on a 9-beat run: **42 clips vs 52** for flat depth-2, a 19% saving. Real
waste came in at **38%, not 33%**, because 1-shot tails are less favourable than the
2+2 ideal — quote the measured number, not the brochure one.

Two non-obvious wins:

- **Chaining is more reliable.** A speculative tail would chain from a head frame in
  a branch that may never become canon; a just-in-time tail chains from a frame that
  actually exists on the canon path.
- **The tail is written knowing the vote**, so consequences are specific instead of
  hedged across both outcomes. This is a writing-quality gain, not just a cost one.

**Tradeoff:** tails sit on the critical path with no cached alternative behind them.
Mitigate with **long heads (12–15s)** for runway, **short tails (5–8s)** to render
fast, and a cutaway library covering every scene's tail position specifically.

## 4. Mode discipline beats prompt engineering

Validate shot *mode* by position. This makes identity and continuity breaks
structurally impossible rather than merely discouraged:

- `ref2v` (reference-to-video) — **required** for any shot with an identity-locked
  character. Applies the reference sheet.
- `flf` (first-and-last-frame) — mid-scene chaining. Both ends pinned, so the model
  interpolates between fixed points instead of inventing an endpoint.
- `t2v` — legal **only** as the first shot of a head **and** only with no locked
  character on screen.

Every run rejects the same violation: the model always wants to open on a wide
establishing shot with characters in it. The validator catches it pre-spend. Worth
pre-empting in the system prompt to save a repair round-trip.

## 5. Asymmetric lookahead depth

Frames and clips differ in cost by ~2 orders of magnitude, so give them different
depths — but **do not confuse "cheap" with "worth speculating deeply"**:

```
images   depth 2   one level ahead of video, plus a scene of slack
video    depth 1   expensive, head/tail split
```

### Derive the depth, don't pick it

```
depth = ceil(frame_generation_seconds / scene_seconds) + 1
```

At ~10–15s per i2i frame against ~35s scenes that gives **2**. Measured utilization
over simulated 9-beat screenings shows why deeper is pure waste:

| Depth | Frames generated | Frames used | Utilization |
|---|---|---|---|
| 1 | 88 | 5 | 5.7% |
| **2** | **166** | **5** | **3.0%** |
| 3 | 298 | 5 | 1.7% |
| 4 | 538 | 5 | 0.9% |
| 5 | 922 | 5 | 0.5% |

Generation **doubles per level while consumption stays flat**. The reason is
structural: a frame only needs to exist before the *clip consuming it is submitted*,
and clips already lead playback by one scene. Anything beyond that is speculation
about speculation. An early draft of this design specified depth 3–4 on the intuition
that cheap frames justify deep trees; the measurement killed it.

If frame generation ever is slow relative to scene length, the fix is a faster image
model, not a deeper tree.

### What depth 2 still buys

1. No shot is ever unconditioned — a prepared first frame exists whichever way the vote goes
2. `flf` becomes fully specified (both ends are real images)
3. Drift is caught **before** video spend — a bad frame is cheap to re-roll
4. Discarded branches cost almost nothing

### Bias quality, not depth

Uniform depth 2 is correct, but the two branches at that depth need not get equal
treatment. If telemetry shows audiences reliably favour one branch *type*, prepare
the likely branch at full quality and the unlikely one at reduced resolution,
upgrading only if it wins. That is a **quality asymmetry within a fixed depth** —
the right shape for "speculate harder on the likely path".

**Pitfall — prune against the CANON PATH, not the immediate loser.** Pruning only the
losing choice at the current beat leaves stale subtrees from earlier speculation
alive, and live frame count grows without bound (observed 76 → 200 instead of holding
steady). Every branch decision in a job's key must match the canon path or the job is
dead. Correct behaviour at depth 2: live frames hold flat (~35–44) and drain at the end.

## 6. Pre-production via Replicate — no local GPU needed

ComfyUI is excellent but **must not sit in the live render path** (tens of seconds to
minutes per clip; Comfy Cloud caps at 1–5 concurrent jobs, free tier can't call
`/api/prompt`). For asset prep, Replicate's API removes the GPU requirement entirely.

**`bytedance/seedream-4` is the key find:** `sequential_image_generation="auto"` with
`max_images` up to 15 returns a whole **consistent set from one call**, and accepts
1–10 reference images via `image_input`.

```python
# Stage 1: anchor — one canonical full-body image per character
predict("bytedance/seedream-4", {
    "prompt": anchor_prompt,          # full body, head to toe, neutral pose,
                                      # mid-grey seamless backdrop, flat lighting
    "size": "2K", "aspect_ratio": "2:3",
    "sequential_image_generation": "disabled",
})

# Stage 2: sheet — 9 images from that anchor, identity locked
predict("bytedance/seedream-4", {
    "prompt": SHEET_SPEC,             # 5 full-body angles + 4 face/emotion closeups
    "image_input": [data_uri(anchor)],
    "size": "2K", "aspect_ratio": "match_input_image",
    "sequential_image_generation": "auto", "max_images": 9,
})
```

Measured: anchor **~12s**, 5-image body turnaround **~90s**, 4-image face set **~70s**,
roughly **$0.12** per character. Location plates use the same call shape with an
explicit "no people, no figures" clause and 6 angle/range variants.

### ONE CALL PER ITEM. Never batch a "N distinct X" request.

This is the single highest-value rule in this section, and it took three separate
failures to learn because it wears a different mask each time:

| Batched request | Failure |
|---|---|
| "9 images: 5 angles + 4 emotions" | silently returned **6** (5 body, 1 face) |
| "5 distinct camera angles" | angle ladder skewed, both 45s wrong |
| "4 distinct emotions" | fear vs grief converged to **4.46** distinctness (tol 6.0) |
| any sequential batch | backdrop luma spread **29.4** — each image is still an independent roll |

`sequential_image_generation="auto"` does not give you a coherent *set*. It gives you
N independent rolls with a shared prompt, so anything requiring **contrast between
items** — distinct angles, distinct expressions, matched backdrops — is left to
chance.

Fix: iterate in your own code, one prediction per item, each with a focused brief and
`sequential_image_generation="disabled"`. Costs ~$0.02 per image and makes the set
deterministic. Assert `len(urls) == 1` every time.

Reserve batched `auto` for cases where you genuinely want variations on one theme and
do not care how they differ.

**Pitfall — `max_images` is a CEILING, not a target, and under-delivery is SILENT.**
One request for 9 images (5 body + 4 closeups) returned 9; the identical call for a
second character returned **6** — five body shots and a single closeup — with no error
and status `succeeded`. A half-built identity lock silently entering the video stage
is exactly the failure this pipeline cannot absorb.

Two mitigations, use both:

1. **One call per shot type.** Split "5 body angles" and "4 face closeups" into
   separate predictions with separate prompts. Each call then carries a single
   unambiguous brief and a small `max_images`, which the model honours far more
   reliably than a compound 9-image request.
2. **Assert the returned count** before downloading and raise if it is short:

```python
def _expect(urls, want, label):
    if len(urls) != want:
        raise RuntimeError(f"{label}: expected {want}, got {len(urls)}. Re-run; "
                           "do not proceed with an incomplete reference set.")
    return urls
```

Never infer success from `status == "succeeded"` alone on `auto` mode — count the
outputs.

Also available and worth knowing: `google/nano-banana-pro` takes up to **14**
reference images at 1K/2K/4K — good fallback or retouch pass.

Prompt-writing rules that matter for sheets:

- **NEVER put the style bible in the anchor or sheet prompt.** This is the subtlest
  and most damaging mistake. A cinematic grade ("coastal fog, sodium-vapour
  practicals, deep teal shadows, warm amber interiors") bakes coloured light into the
  anchor plate, and because the sheet is generated **i2i from the anchor**, that cast
  propagates into every reference image. Verified failure: an amber gradient with
  teal pooling in the lower-left plus directional shadow, across the whole set. The
  video model then inherits the coloured light instead of the neutral **albedo** of
  face and costume. Reference plates must be neutral; the grade belongs on the *shot*
  prompt, not the identity lock. Use an explicit negative block:

```
"Neutral mid-grey (#808080) seamless studio backdrop, completely flat and even
 white-balanced lighting from the front, no coloured gels, no warm amber cast,
 no teal or cyan cast, no directional shadows, no vignette, no film grain,
 no cinematic colour grading. Clean technical reference plate lighting only."
```

  Location **plates are the exception** — they become real first-frames, so they
  *should* carry the style bible.
- **Enumerate every angle with explicit anti-confusion language.** "front, ¾ left,
  profile, ¾ right, back" silently collapses: an observed run returned two
  near-duplicate profiles and no clean 45° three-quarters. Spell out per image what
  must be visible and what must not — "rotated 45 degrees, HALFWAY between front and
  side, both eyes still visible, this is NOT a profile", and for the back view "NO
  face visible at all, head must NOT be turned over the shoulder". Add "No two images
  may show the same angle."
- Number the images explicitly ("Image 1: FRONT view...")
- Say **"no collage, each image separate"** or you get grid panels inside one image
- Say **"THIS EXACT PERSON from the reference image"** and enumerate what must not
  change: face, hair, build, costume, footwear
- For plates, "no people, no figures" must be stated or figures leak in

See `replicate-api-generation` for API pitfalls (custom User-Agent required from
urllib, `Prefer: wait` 403s above ~1MB bodies, output URLs expire in ~1 hour,
`execute_code` does not inherit env vars so run these from `terminal`).

## 6.1 Asset pipeline order (matters)

```
1. anchor        neutral, NO style bible       -> qc neutrality
2. body angles   ONE CALL PER ANGLE            -> qc distinctness
                 (front / profile / back only; 45s are unreliable)
3. face emotions ONE CALL PER EMOTION          -> qc distinctness
                 (muscle-action briefs, not emotion labels)
4. normalize     luma-only gamma onto target   -> qc uniformity
5. regenerate    uncorrectable outliers only   -> re-run 4
6. vision check  scoped parallel subagents     -> expressions read correctly
```

Steps 4 and 5 loop until `qc.py` passes clean. Only then does the set enter the video
stage. Skipping step 4 ships inconsistent lighting cues into every `ref2v` shot.

## 7. Gate every reference set with objective QC FIRST

**Make numerical everything that can be numerical.** Vision subagents are for
judgements like "does this expression read as grief" — not for things arithmetic
answers exactly. Run `scripts/qc.py` before spending any vision budget:

```bash
python qc.py assets/character/wrenn              # characters: both checks
python qc.py assets/plates/b1_arrival --graded   # plates legitimately keep the grade
```

Two checks, both measured against real observed values:

| Check | What it catches | Good | Failing |
|---|---|---|---|
| `neutrality` | grade baked into an identity lock | spread 0.7–3.0 | **17.9–30.1** |
| `uniformity` | plates from different-brightness studios | luma spread <20 | **88–102** |
| `distinctness` | literal near-duplicate frames | min delta 6.5–30.7 | **< 6** |

`neutrality` averages RGB over the border region only (backdrop, not subject) and
reports max channel spread plus cast direction. It independently confirmed a
grade-leak failure a vision subagent had described by eye — 30.1 warm/amber — and
confirmed the fix at 0.7 on body plates.

**`uniformity` exists because `neutrality` alone gives false confidence.** Neutrality
measures colour cast *within* each image and says nothing about whether plate 1 is a
white studio and plate 4 is mid-grey. A set with border lumas
`[137, 232, 226, 238, 239]` — a **102/255 spread** — passed neutrality cleanly on
every image. Two vision reviews independently flagged it as "mix of white and grey
grounds" and "per-panel backdrop brightness variance", and the numbers were sitting
in the QC output the whole time, unchecked. If you write a metric, ask what it does
*not* measure.

### distinctness does NOT check angle correctness

Critical limitation, learned the hard way: distinctness measures **pixel difference,
not semantics**. A left profile and a head-turned left profile differ substantially
in pixels while being the *same angle class*. Verified false negative — a body set
where two slots were both ~90° profiles instead of the requested 45°
three-quarters **passed at min delta 6.54**. Only vision review can confirm an angle
**ladder**; distinctness catches literal duplicates and nothing more.

### Three-quarter camera angles are NOT reliably promptable — ship what verifies

The most important finding in this section. Verified across **three generations and
two characters**, with escalating prompt specificity — degrees, then shoulder-line
percentage, then explicit both-eyes-visible plus anti-head-turn language, then one
API call per angle:

| Slot | Outcome, every attempt |
|---|---|
| front | **PASS** |
| profile | **PASS** |
| back | **PASS** |
| three-quarter left | over-rotates toward profile |
| three-quarter right | under-rotates toward front |

The failure is a **skewed ladder, not a collapse**. Pixel evidence: `front` vs
`tq_right` was the *closest* pair in the set (8.58) while `tq_left` vs `profile` sat
mid-pack. Both 45 degree slots miss, in opposite directions. Two subagents described
this as "collapsed into duplicate profiles"; the pair-distance matrix showed the real
shape. **When vision reports and pixel data disagree, check the pair matrix before
acting on either.**

Fix: **drop the 45 degree slots.** Ship front + profile + back. Same move as taking
backdrop brightness out of the prompt — stop asking the model for something it does
not reliably do.

The cost is low. `seedance-2.0` accepts up to 9 `reference_images`; 3 solid body
angles + 4 emotion plates = 7 verified locks. And a *wrong* three-quarter plate is
worse than a missing one, because it teaches the video model an identity at an angle
the character never actually holds.

Revisit only with a model offering explicit camera control, or a novel-view-synthesis
step — not with more prompt language. Three attempts is enough evidence.

One call per angle is still correct for the angles you do keep: a batched
"N distinct angles" request collapses, and per-angle calls cost ~$0.02 each.

Also pin the backdrop value verbatim in every call ("exactly 50 percent grey, hex
#808080, RGB 128 128 128 — not white, not light grey"), but **do not expect it to
work** — see below.

### Backdrop brightness is a POST-PROCESS, not a prompt

The single most stubborn failure. Every call picks its own backdrop brightness no
matter how precisely the prompt specifies one. Measured with `#808080, RGB 128 128
128` stated in every prompt:

| Approach | Border-luma spread |
|---|---|
| Single batched call | 64 |
| Per-angle calls (the angle fix) | **129.6** — worse |

Per-angle calls make it *worse*, because each call is an independent roll. Fixing the
angle ladder therefore regresses backdrop uniformity — expect this and plan for it.

Prompting is the wrong tool for a measurable, computable quantity. Use
`scripts/normalize.py`: measure each plate's border **median** luma (median, not
mean — an intruding limb skews a mean), then apply a **gamma** correction onto one
shared target. Gamma not a flat offset, because an additive shift fixes the backdrop
while clipping the subject's highlights.

**Correct LUMA ONLY.** Applying the same gamma LUT to R, G and B independently
amplifies whatever channel imbalance already existed. Verified regression: face
plates passing `neutrality` at 6.2 came back at **12.6 (FAIL)** after an all-channel
correction, and one plate's `distinctness` crashed to 2.77. Brightness and colour
cast are separate properties and must be corrected separately — convert to YCbCr,
curve Y, leave Cb/Cr untouched, recombine. After the luma-only fix the same set
passed everything.

**Then measure again and triage.** Gamma cannot rescue a plate shot on a
fundamentally different backdrop — one plate at luma 46 in a set whose median was 223
only reaches 166, washing out the subject on the way. So: correct, re-measure, and
treat anything still outside tolerance as **must-regenerate**, not correctable.

```
=== body (5 plates)  target luma 223
  spread before: 196.0
  OUTLIERS (regenerate, do not correct): ['body_00.png']
    body_00.png  luma 46.0 -> best 166.0 vs target 223 — uncorrectable
  spread after (corrected plates): 12.0  PASS
  SET NOT PROMOTABLE — regenerate 1 outlier(s) first
```

Typical result: 4 of 5 plates correct cleanly to under 20 spread, 1 needs a re-roll.
Empirical triage ("correct then verify") beats a heuristic threshold — an earlier
ratio-based guess misclassified plates that gamma actually handled fine.

Keep raw originals in a `_raw/` sibling directory. When a correction turns out to be
buggy you need the untouched source to re-run from, and re-generating to recover from
your own post-process bug is pure waste.

**General rule this instance of:** anything you can measure, compute — don't prompt
for it. Prompts are for content; arithmetic is for quantities.

### Scope each metric to the artifact it governs

`uniformity` is a **character-plate** rule, not a universal one. Identity locks must
share one studio so the video model reads albedo rather than lighting. Location
plates are the opposite case — a low-angle interior and a high-angle exterior of the
same place *should* differ in exposure, because they become real first-frames
carrying the grade. Running uniformity on them flagged a legitimate 44-luma spread as
a failure. `--graded` skips both neutrality and uniformity for plates.

### Face-plate failures worth pre-empting

Each of these survived at least one prompt revision, so state them forcefully:

- **downcast eyes** — "eyes look DIRECTLY INTO THE CAMERA LENS, no downcast, no
  averted gaze, in any image". Fixes on the first try.
- **fatigue rendered as injury** — the stubborn one. "Grief and exhaustion" produced
  purple-mauve eye rings and tear streaks reading as black eyes. Negating the *colour*
  ("no purple, use subtle grey instead") is **not enough** — it still renders rings,
  just greyer. Redirect the *mechanism* instead: convey exhaustion through "SLACK
  MOUTH, HEAVY UPPER EYELIDS, loose jaw", and require under-eye skin "CLEAN and EVEN,
  the same tone as the cheeks", with explicit no-rings / no-sunken-sockets / no-tears
  clauses. **Negate the rendered feature, not just its attributes.**
- **wardrobe drift mid-set** — "costume collar and garment IDENTICAL to the reference
  in all four". Fixes reliably.
- **"neutral" collapsing to blank** — asking for "neutral but guarded" yields placid.
  Drop the word *neutral* entirely and describe only the guardedness as physical
  action: "lower eyelids tightened and slightly raised, jaw clenched, lips pressed
  into a flat closed line, chin slightly lifted — sizing up the person in front of her
  and deciding what not to say."
- **age drift upward** — plates read a decade older than the brief, consistently.
  Anchor to the reference, not to a number: "must look the SAME AGE as the reference
  image and no older. Do not add wrinkles, do not deepen lines, do not age the face."

General pattern: **describe physical muscle action, not emotion labels.**

## 8. Then verify with NARROWLY SCOPED vision subagents

You cannot see images. Sets **look** plausible while drifting badly, and QC cannot
judge whether an expression reads as the right emotion.

**Scope the delegation tightly.** One request to verify 10 images across 6 questions
burned 50 API calls, hit the iteration cap, and confirmed only 3 files — because the
child retried a flaky vision tool. Correct shape:

- Build **contact sheets with PIL first** (one row per group), then have the child
  judge the single montage rather than opening ten files
- **One group per subagent** (5 body angles, or 4 face emotions), dispatched in
  parallel — not one child for everything
- Put this in the context verbatim: *"if vision_analyze returns a loader stub, retry
  AT MOST TWICE, then STOP and report 'vision tool unavailable'. An honest partial
  report beats an exhausted one. Do not fabricate observations."*
- Name the **specific known failure mode** under test so the child hunts for it
- Demand unverified files be reported as unverified

The truncated run was still worth it — it caught both the grade leak and the angle
collapse. A partial honest report beats a complete generous one.

## 9. Runtime vs learning loop — where the agent harness belongs

Hard boundary. An agent in the live path makes latency nondeterministic; no agent
between screenings means nothing improves.

```
LIVE      deterministic service, NO agent  — state machine + queue + player
BETWEEN   agent harness on a schedule      — QC, regeneration, proposals
GATE      human review before merge
```

The showrunner is deliberately **not** an agent: one constrained LLM call per phase,
schema-validated, rejected if non-conforming. That is why mode discipline holds — it
is code, not good intentions.

Between screenings is where subagents earn their place: QC sweeps, drift analysis on
canon clips, telemetry-driven spine proposals. Two guardrails:

- **Never optimize for votes.** Tuning toward crowd preference rebuilds the infinite
  content machine the showrunner exists to prevent. Optimize for completion rate and
  payoff recognition.
- **The agent may never edit its own validator.** Prompts, spines and assets are fair
  game for automated proposals; schema and validation rules stay human-gated.

Long-run image quality comes from **better locks, not better prompts**: accumulate
approved frames, fold them into reference sheets, eventually train a per-character
LoRA. That is the point a GPU justifies itself, and it is still an offline job.

## 10. Milestones — do text first

1. **M1 text-only showrunner.** Branch tree, flags, trust, memories as a readable
   script. Zero generation cost. **Go/no-go gate:** if continuity fails in plain text,
   no video pipeline rescues it.
2. **M1.5 asset factory.** Character sheets, world plates, cutaway library. Gates M2.
3. **M2 single-branch render chain.** ref2v/flf shots → ffmpeg mux → continuous film.
4. **M3 speculative queue.** Head speculation, JIT tails, frame tree with live
   prune-and-extend, deadline logic, cutaway fallback.
5. **M4 live layer.** Server-authoritative clock, WebSocket votes, rolling HLS.
6. **M5 public screening.**

Verify provider **concurrency limits** before M3. At ~6s wall time per 10s clip
generation beats playback ~1.7x, but serialized speculation collapses the buffer —
parallel job slots are a procurement requirement, not an optimization.

## Reference implementation layout

```
stories/*.json          authored spine: chapters, beats, characters, flags, contracts
engine/showrunner.py    state machine — owns canon, validates, gates beats
engine/writer.py        two-phase LLM: write_head() / write_tail()
engine/run_story.py     harness: voting, script render, canon log, clip accounting
engine/frame_planner.py image-layer tree: plan / prune-against-canon / extend
engine/preprod.py       Replicate asset factory: anchor / sheet / plates
engine/qc.py            objective QC: neutrality + uniformity + distinctness gates
engine/normalize.py     backdrop luma normalization + outlier triage
```

## Verification checklist

- [ ] Provider max duration confirmed from the live schema, not assumed
- [ ] Text-only run produces a coherent script across ≥3 divergent vote paths
- [ ] Payoff floor measured on the *least* cooperative path, not the best one
- [ ] Zero validator repairs after prompt tuning (repairs are free but slow)
- [ ] Frame planner live count holds steady across a simulated run
- [ ] `qc.py` passes on every reference set BEFORE any vision-subagent spend
- [ ] Angle ladder confirmed by VISION, not by distinctness (distinctness cannot see angles)
- [ ] `normalize.py` run and any uncorrectable outliers REGENERATED before promotion
- [ ] Character sheet verified by narrowly-scoped parallel vision subagents
- [ ] Returned image counts asserted, not assumed (`auto` mode under-delivers silently)
- [ ] Clip count per screening measured, not estimated
