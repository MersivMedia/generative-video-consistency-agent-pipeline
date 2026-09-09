---
name: generative-video-consistency
description: Use when building or operating a generative video pipeline that must hold character, wardrobe, location and geometry consistent across many shots. Covers reference locks, numeric QC gates, mode discipline, and the agent-harness split between live rendering and offline improvement.
---

# Generative Video Consistency

Operating manual for the pipeline in this repository. The parent skill
(`branching-ai-film-engine/SKILL.md`) covers the showrunner and branching
architecture; this one covers **keeping the pictures consistent** and **running
the loop inside an agent harness**.

## When to Use

- Generating more than a handful of shots that must share characters or locations
- Character identity, wardrobe or backdrop drifts between clips
- Deciding what an agent should automate and what it must never touch
- Reviewing generated assets without burning budget on vision calls

## 1. Measure everything you can, before you look at anything

Vision review is expensive, slow, non-deterministic, and — in practice —
unreliable enough that five separate review agents exhausted their budgets on a
flaky tool. Arithmetic is free and repeatable. So the order is fixed:

```
generate -> numeric gates (qc.py) -> post-process (normalize.py)
         -> regenerate failures -> ONLY THEN vision review for judgement
```

Vision is reserved for questions arithmetic genuinely cannot answer: "does this
read as grief", "is this the same person", "is the geometry possible".

### The four gates

| Gate | Catches | Tolerance | Real failure seen |
|---|---|---|---|
| `neutrality` | cinematic grade baked into an identity lock | spread ≤ 12 | 30.1 (warm amber) |
| `uniformity` | plates lit at different brightnesses | luma spread ≤ 20 | 129.6 |
| `distinctness` | near-duplicate frames | min delta ≥ 6 | 4.46 (fear≈grief) |
| `costume` | garment colour vs authored canon | hue + **luminance** | white lum 0.93 vs tan 0.53 |

### Write the gate, then ask what it does NOT measure

Every gate here has been wrong at least once, and always in the same way — it
measured something adjacent to the property that mattered.

- `neutrality` measured channel spread **within** each image, never brightness
  **across** the set. Five plates at border lumas `[136,231,224,237,238]` all
  passed individually. `uniformity` exists because of that miss.
- `distinctness` measures pixel difference, not semantics. A profile and a
  head-turned profile differ in pixels while being the same angle class.
- `costume` v1 used hue + saturation and **passed a white coverall**, because
  HLS saturation stays deceptively high near white. Luminance is the honest
  discriminator: a tan garment cannot exceed lum 0.80.

### When two tools disagree, one of them is wrong by construction

`normalize.py` reported PASS 2.0 where `qc.py` reported FAIL 52.4 on the same
image. Cause: one took the **median** of border pixels, the other the **mean**.
A limb intruding into the border moves the mean and not the median. Unify the
primitive; do not average the verdicts.

## 2. Some properties are post-process, not prompt

Pin the backdrop value verbatim in the prompt — and do not expect it to work.
With `#808080, RGB 128 128 128` in every prompt, border-luma spread was still
**64** for batched calls and **129** for per-call generation, because each call
is an independent roll.

Correct it instead:

1. **White balance** — grey-world gains derived from the known-neutral backdrop.
   Needed because strong costume colour words bleed into the backdrop: a
   TAN-KHAKI costume casts the whole frame warm, OLIVE-GREEN casts it teal.
2. **Luma gamma** to a shared target — **luma only**, in YCbCr. Applying gamma
   to R/G/B independently amplifies channel imbalance and *created* a
   neutrality failure (6.2 → 12.6) plus a distinctness failure (2.77) that
   looked exactly like an emotion collapse. It was chroma damage.
3. **Triage** — correct, re-measure, and treat anything still outside tolerance
   as must-regenerate. Gamma cannot rescue a plate shot on a fundamentally
   different backdrop: luma 46 against a set median of 223 tops out at 166.

Expect roughly 1 uncorrectable outlier per 5-plate set.

## 3. Canon must be authored, not inferred

The character schema originally had no `costume` field. The wardrobe spec used
to review the first render was **back-derived from an earlier vision report** and
treated as ground truth. The plates were not drifting from canon — there was no
canon. That is strictly worse than being wrong, because nothing can validate it.

Author it explicitly, inject it **verbatim** into every anchor, sheet and shot
prompt, and gate it numerically:

```json
"costume": "faded TAN-KHAKI canvas coverall, brass buttons, charcoal wool
            undershirt, scuffed DARK BROWN leather work boots"
```

Result: torso `rgb(244,242,234)` (near-white, invented) → `rgb(173,150,114)`
(tan, canon) and verifiable on screen.

Keep the **style bible out of anchor and sheet prompts**. Coloured light baked
into a reference plate propagates through every image-to-image derivative and
corrupts the albedo the video model reads. Location plates are the exception —
they become real first frames and should carry the grade.

## 4. Structural constraints beat prompt phrasing

When a property keeps failing, stop rewriting the prompt and change what the
system can express.

| Problem | Prompt fix | Structural fix that worked |
|---|---|---|
| Identity decay along chained shots | "maintain the same character" | validator forbids chaining; re-anchor every shot |
| Mid-shot location/lighting changes | "single continuous take" | cap duration at 4-6s |
| Doorway with exterior on both sides | describe the interior | required `camera_side` field naming what is BEHIND the subject |
| Dialogue out of sync | mux TTS after render | pass TTS as reference audio *input*, model lip-syncs |
| Backdrop brightness drift | pin the hex value | post-process normalization |

The pattern: make the failure **impossible to express** rather than unlikely to
occur.

### Cite every reference asset in the prompt

Attaching nine reference images without naming them leaves the model to guess
what each is for. Name them positionally:

```
Image 1: body reference for Ilse Wrenn
Image 4: face reference for Ilse Wrenn
Image 5: the location
Audio 1: Wrenn's dialogue for this shot
```

Nine unlabelled images are not nine locks.

## 5. One API call per item. Always.

The single highest-value rule, learned four times in different costumes:

| Batched request | Silent failure |
|---|---|
| "9 images: 5 angles + 4 emotions" | returned **6**, status `succeeded`, no error |
| "5 distinct camera angles" | ladder skewed; both 45° slots wrong |
| "4 distinct emotions" | fear ≈ grief at 4.46 (tol 6.0) |
| any sequential batch | backdrop luma spread 29.4 |

Sequential/batch image modes return **N independent rolls sharing a prompt**, not
a coherent set. Anything requiring *contrast between items* is left to chance.
Iterate in your own code, one prediction per item, and **assert the returned
count** — `max_images` is a ceiling, not a target.

Emotion distinctness went 4.46 → 13.07 on that change alone.

## 6. Prompt craft that survived revision

- **Negate the rendered feature, not its attributes.** "No purple eye rings, use
  grey" produces grey rings. Redirect the mechanism: convey exhaustion through
  slack mouth and heavy eyelids, and state the under-eye skin is "clean and even,
  the same tone as the cheeks".
- **Describe physical muscle action, not emotion labels.** "Neutral guarded"
  rendered placid. Tightened lower lids, clenched jaw, lips pressed flat worked.
- **Anchor age to the reference image, not a number.** "Late 50s" drifted to
  70s; "the same age as the reference" held.
- **Give falsifiable geometry.** "Shoulder line at seventy percent of full
  front-on width, near shoulder larger, head faces the SAME direction as the
  chest; if only one eye is visible it is WRONG" beats "45 degrees".

## 7. Ship what verifies; drop what does not

Three-quarter camera angles failed across three generations and two characters
with escalating prompt specificity, while front / profile / back passed every
time. They were **dropped, not fixed**.

A wrong three-quarter is worse than a missing one, because it teaches the video
model an identity at an angle the character never holds. Retire failing assets to
a quarantine directory so they cannot leak into a reference set.

When vision reports and pixel data disagree, **check the pair matrix before
believing either**. Vision called an angle set a "collapse"; the matrix showed a
*skewed ladder* — one side over-rotated toward profile, the other under-rotated
toward front, making front ≈ three-quarter-right the closest pair at 8.58.
Different diagnosis, different fix.

## 8. Verify edits actually applied

Several string-replace edits **silently did nothing** — the anchor text had
changed, so the replace matched nothing and the file was rewritten unmodified.
A validator rule was "added" three times while appearing zero times in the file,
and only a paid render run proved it was missing.

```python
assert anchor in content, "anchor missing — read the file before editing"
content = content.replace(anchor, new, 1)
assert marker in content, "insert failed"
open(path, "w").write(content)
```

Then prove the behaviour, not the text: feed the known-bad input back through
and confirm it is now rejected.

Same failure class as restoring from an unversioned backup directory: a blanket
restore from `_raw/` reinstated pre-costume plates and resurrected retired
angles. **Version backups by content hash**, and quarantine with `mv` rather
than deleting.

## 9. Where the agent harness belongs

Three tiers, and the boundary is load-bearing:

```
LIVE      deterministic service, NO agent
          state machine + queue + player. One constrained, schema-validated
          LLM call per phase. A vote window cannot absorb agent latency.

BETWEEN   agent harness, scheduled
          QC sweeps, asset regeneration, drift analysis, telemetry review,
          prompt and story-spine proposals. Open-ended work lives here.

GATE      human review before merge
```

Two guardrails:

- **Never optimize for votes.** Tuning toward crowd preference converges on mush
  and rebuilds the infinite-content machine the showrunner exists to prevent.
  Optimize for completion rate and payoff recognition.
- **The agent may never edit its own validator.** Prompts, spines and assets are
  fair game for automated proposals. Schema and validation rules stay
  human-gated, or the system will relax the constraint instead of meeting it.

Quality improves through **better locks, not better prompts**: accumulate
approved frames, fold them into reference sheets, and only then consider a
per-character LoRA. That work is offline either way.

## 10. Cost discipline

- **Dry run first.** `--dry-run` printing payloads and cost caught a 29MB body
  against a ~10MB limit, duplicate reference filenames, and two mutually
  exclusive API fields — all before spending anything.
- **One item before the batch.** Render one scene, review it, then commit to the
  run.
- Text-only story runs cost only LLM calls. Prove the narrative works before any
  pixel is generated.
- Full pre-production for 2 characters and a location: **~$2**, no GPU.
  One 5-shot scene: **~$1.75**.

## Verification checklist

- [ ] Numeric gates pass before any vision review is dispatched
- [ ] Returned asset counts asserted, not assumed
- [ ] One API call per distinct item
- [ ] Costume/appearance authored in the story file and injected verbatim
- [ ] Style bible excluded from anchor and sheet prompts
- [ ] Reference assets cited positionally in the prompt
- [ ] Backdrop normalized in post, not requested in prompt
- [ ] Failing asset classes retired to quarantine, not shipped degraded
- [ ] Every code edit asserted applied, and behaviour re-tested
- [ ] Backups versioned by hash before any restore
- [ ] Dry run reviewed before paid execution
