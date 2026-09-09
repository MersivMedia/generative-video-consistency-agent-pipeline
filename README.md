# Generative Video Consistency Agent Pipeline for User Adaptive Showrunner

A production pipeline for **live branching AI film**: an audience votes at decision points, video generates ahead of playback, and a constrained showrunner keeps characters, wardrobe, geography and plot consistent across the whole run.

The hard problem in generative video is not making a clip. It is making the *hundredth* clip still look like the first one. This repo is an attempt to solve that with **measurable gates instead of hopeful prompting** — every consistency property has a numeric check, and the pipeline refuses to promote assets that fail.

Reference story implementation: *The Signal* — 3 chapters, 9 beats, 3 characters, flag-gated payoffs.

---

## Why this exists

Generation is now faster than playback. On fal's MiniMax H3 Max a 5-second 768p clip renders in **~5.7s of inference** — near parity, and faster than realtime for shorter clips. That inverts the interaction loop: instead of asking and waiting, the system speculatively renders both possible futures while the audience watches the current scene and votes.

Two failure modes dominate:

1. **Latency** — a visible spinner destroys the illusion. Solved by speculative head/tail rendering.
2. **Drift** — characters change face, age and clothing; locations mutate; geography becomes impossible. Solved by identity locks, mode discipline, and numeric QC gates.

Everything below is in service of those two.

---

## Architecture

```
stories/*.json          authored spine: chapters, beats, characters, costume canon,
                        flags, payoff contracts, voice ids, shot budget

engine/showrunner.py    state machine. OWNS CANON. Validates every proposed shot
                        against schema + story rules. The LLM proposes; this decides.
engine/writer.py        two-phase LLM author: write_head() / write_tail()
engine/run_story.py     text-only run harness: voting, script render, canon log

engine/preprod.py       asset factory (Replicate): anchor -> body angles -> face
                        emotions -> location plates
engine/qc.py            objective gates: neutrality, uniformity, distinctness, costume
engine/normalize.py     white-balance + luma normalization, outlier triage
engine/frame_planner.py image-layer lookahead tree: plan / prune / extend

engine/fal_client.py    fal queue client (H3 Max endpoints, storage upload)
engine/dialogue.py      ElevenLabs per-character TTS, padded for lip-sync reference
engine/render_scene.py  canon scene -> video via reference-to-video, ffmpeg mux

skills/                 portable agent skills — see "Running it in an agent harness"
docs/AGENT_HARNESS.md   what to automate, what to keep deterministic
```

### The showrunner does not own canon

The LLM **proposes** one shot at a time. `showrunner.py` validates it and rejects anything non-conforming; only `commit()` mutates state. On rejection the validator's own error message is fed back so the model repairs its output. Every accepted turn appends to an immutable canon log.

Rules that earn their keep:

| Rule | Why |
|---|---|
| `duration` 4–6s | At 13–14s the model re-stages blocking, relights and re-ages characters *inside one take* |
| `mode` must be `ref2v` for characters | Re-anchoring to the reference sheet every shot; `flf` chaining decayed identity 8/10 → 5/10 → 3/10 |
| `camera_side` required at boundaries | A generated shot put the camera outside a door and showed open sea *through* the doorway — exterior on both sides, no interior |
| Dialogue shots must be `ref2v` | Only reference-to-video accepts `reference_audio_urls`, which is what drives lip-sync |
| Flags must be declared **and** settable in this beat | Prevents invented state |
| Costume injected verbatim | Without it the model invents clothing per call and nothing is verifiable |

### Head / tail speculative rendering

Each scene splits into a **head** (plays while voting is open) and a **tail** (consequence shots, generated just-in-time once the winner is known).

```
t=0    play head (3 x 5s)  ─┬─ speculate branch A head
                             └─ speculate branch B head
t=+10  voting opens
t=+15  voting closes         winner promoted, loser head discarded
t=+15  generate WINNER tail just-in-time
```

Measured on a 9-beat run: **42 clips vs 52** for flat depth-2 speculation, ~19% saved, with real waste at **38%** (not the theoretical 33% — 1-shot tails are less favourable than the 2+2 ideal).

Writing quality improves too: with a single-phase turn, consequence shots must hedge across both outcomes. Split it and the tail is authored *knowing the vote*, so the consequence is specific.

### Asymmetric lookahead depth

Prepared frames and generated clips differ in cost by ~2 orders of magnitude, so they get different depths — but cheap does **not** mean worth speculating deeply:

| Depth | Frames generated | Frames used | Utilization |
|---|---|---|---|
| 1 | 88 | 5 | 5.7% |
| **2** | **166** | **5** | **3.0%** |
| 3 | 298 | 5 | 1.7% |
| 4 | 538 | 5 | 0.9% |

Generation doubles per level while consumption stays flat. A frame only needs to exist before the clip consuming it is *submitted*, and clips already lead playback by one scene. So:

```
depth = ceil(frame_generation_seconds / scene_seconds) + 1     -> 2
```

---

## Consistency: measurable, not hopeful

The core thesis of this repo. Every property that can be measured is measured, and vision review is reserved for judgements arithmetic cannot make.

### `qc.py` gates

| Gate | Catches | Pass | Fail observed |
|---|---|---|---|
| `neutrality` | cinematic grade baked into an identity lock | spread 0.7–3.0 | 17.9–30.1 |
| `uniformity` | plates from different-brightness studios | luma spread < 20 | 88–129 |
| `distinctness` | literal near-duplicate frames | min delta 6.5–30 | < 6 |
| `costume` | garment hue/luminance vs authored canon | tan hue 33, lum 0.53 | white lum 0.93 |

Run before spending any vision-review budget:

```bash
python engine/qc.py assets/character/wrenn --costume "TAN-KHAKI coverall, brown boots"
python engine/qc.py assets/plates/b1_arrival --graded   # locations keep the grade
```

### `normalize.py` — compute it, don't prompt for it

Backdrop brightness is not promptable. With `#808080, RGB 128 128 128` stated in every prompt, border-luma spread was still **64** for batched calls and **129** for per-angle calls (each call is an independent roll). So it is corrected in post:

1. White balance — grey-world gains from the known-neutral backdrop. Needed because strong costume colour words bleed into the backdrop (tan → amber cast, olive → teal).
2. Luma gamma onto a shared target — **luma only**, in YCbCr. Applying gamma to R/G/B independently amplifies channel imbalance and *created* a neutrality failure (6.2 → 12.6).
3. Triage — correct, re-measure, and treat anything still outside tolerance as **must-regenerate**. Gamma cannot rescue a plate shot on a fundamentally different backdrop (luma 46 in a set whose median is 223 only reaches 166).

Typical result: 4 of 5 plates correct to under 20 spread, 1 needs a re-roll.

---

## Pipeline order (matters)

```
1. anchor        neutral, NO style bible          -> qc neutrality
2. body angles   ONE CALL PER ANGLE               -> qc distinctness
                 front / profile / back only
3. face emotions ONE CALL PER EMOTION             -> qc distinctness
                 muscle-action briefs, not labels
4. normalize     white balance + luma gamma       -> qc uniformity
5. regenerate    uncorrectable outliers only      -> re-run 4
6. vision check  scoped parallel subagents        -> expressions read correctly
7. render        ref2v + cited locks + TTS audio  -> ffmpeg mux
```

### One call per item. Never batch a "N distinct X" request.

The single highest-value lesson here, learned three times in different disguises:

| Batched request | Failure |
|---|---|
| "9 images: 5 angles + 4 emotions" | silently returned **6** |
| "5 distinct camera angles" | angle ladder skewed, both 45° slots wrong |
| "4 distinct emotions" | fear vs grief converged to 4.46 distinctness (tol 6.0) |
| any sequential batch | backdrop luma spread 29.4 |

`sequential_image_generation="auto"` gives N independent rolls sharing a prompt, not a coherent *set*. Anything requiring **contrast between items** is left to chance. Iterate in your own code, one prediction per item, and assert the returned count.

---

## Models

| Role | Model | Notes |
|---|---|---|
| Video (live) | `minimax/h3-max/*` on **fal** | 5–15s, 480P/768P/1080P. H3 Max is fal-exclusive (404s on Replicate) |
| Reference-to-video | `minimax/h3-max/reference-to-video` | subject refs + `reference_audio_urls` for lip-sync |
| First/last frame | `minimax/h3-max/image-to-video` | `image_url` + `end_image_url` |
| Stills | `bytedance/seedream-4` on Replicate | `sequential_image_generation` + up to 10 refs |
| Dialogue | ElevenLabs | per-character `voice_id` in the story file |
| Showrunner | Claude (Anthropic) | strict JSON, schema-validated |

**Endpoint ids have no `fal-ai/` prefix.** The owner is `minimax`. Prefixing yields `{"detail":"Path /h3-max/text-to-video not found"}` — a 404 that looks like a result-URL bug and reproduces under fal's own official client. Discover ids from `https://fal.ai/api/models?keywords=h3`.

**`prompt_expansion_mode` is required**, enum `disabled|fast|balanced|quality`. Keep it `disabled` — expansion paraphrases away the style bible and the exact character names the reference locks depend on.

**Cite every reference asset in the prompt** as "Image 1", "Audio 1". Passing `reference_image_urls` without naming them leaves the model to guess what they are for. Nine unlabelled images are not nine locks.

---

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install anthropic jsonschema pillow

# ffmpeg is required for frame extraction and muxing
```

### API keys — environment only

No key is ever committed. Every client reads from the environment:

```bash
export ANTHROPIC_API_KEY=...      # showrunner
export FAL_KEY=...                # video generation
export REPLICATE_API_TOKEN=...    # reference plates and location plates
export ELEVENLABS_API_KEY=...     # dialogue
```

Copy `.env.example` to `.env`, fill it in, and source it. `.env` is gitignored.

---

## Usage

```bash
# 1. Text-only story run — no generation cost. The go/no-go gate.
python engine/run_story.py stories/the_signal.json --voter seed --seed 42
python engine/run_story.py stories/the_signal.json --voter interactive

# 2. Build identity locks for a character (anchor + 3 angles + 4 emotions)
python engine/preprod.py sheet stories/the_signal.json --character wrenn
python engine/preprod.py plates stories/the_signal.json --beat b1_arrival

# 3. Gate the assets
python engine/normalize.py assets/character/wrenn
python engine/qc.py assets/character/wrenn --costume "TAN-KHAKI coverall, brown boots"

# 4. Render a scene from canon (dry run first — it prints payloads and cost)
python engine/render_scene.py runs/<run>/canon.json --scene 0 --dry-run
python engine/render_scene.py runs/<run>/canon.json --scene 0

# Frame-tree utilization sweep
python engine/frame_planner.py stories/the_signal.json --sweep
```

`--dry-run` is the cheapest bug-finder in the repo. It caught a 29MB payload (against a ~10MB limit), duplicate reference filenames, and mutually-exclusive API fields — all before spending a cent.

---

## Running it in an agent harness

This pipeline was built and is operated from inside an agent harness
([Hermes](https://hermes-agent.nousresearch.com/docs), OpenClaw, Claude Code —
anything with shell access and scheduled tasks). Full guide:
**[docs/AGENT_HARNESS.md](docs/AGENT_HARNESS.md)**.

The important decision is the boundary:

```
LIVE      deterministic service, NO agent
          state machine + queue + player, one schema-validated LLM call per phase
          a 15s vote window cannot absorb agent latency

BETWEEN   agent harness, scheduled
          QC sweeps · regeneration · drift analysis · prompt and spine proposals

GATE      human review before merge
```

### Install the skills

`skills/` contains two portable skills — plain markdown with YAML frontmatter,
loadable by any harness:

| Skill | Covers |
|---|---|
| `branching-ai-film-engine` | showrunner design, head/tail split, lookahead depth, milestones |
| `generative-video-consistency` | reference locks, QC gates, mode discipline, harness boundary |

```bash
cp -r skills/* ~/.hermes/skills/     # Hermes
cp -r skills/* ~/.claude/skills/     # OpenClaw / Claude Code
```

They ship with `qc.py` and `normalize.py` so an agent can gate assets without
this repo checked out.

### What to automate, ranked

| Task | Automate? | Why |
|---|---|---|
| QC sweeps | **yes, fully** | measurement only, no spend, no writes |
| Drift analysis between renders | **yes, fully** | read-only, high signal |
| Normalization | **yes** | deterministic, reversible |
| Asset regeneration | **with approval gate** | costs money |
| Prompt revisions | **propose only** | needs a human read of the output |
| Story-spine edits | **propose only** | authorial judgement |
| Validator / schema changes | **never** | the agent will relax constraints instead of meeting them |
| Anything in the live path | **never** | latency is the product |

Two guardrails that are not negotiable: **never optimize for votes** (it
converges on mush and rebuilds the infinite-content machine the showrunner exists
to prevent — optimize for completion and payoff recognition), and **the agent may
never edit its own validator**.

### The loop that produced this repo

```
run_story.py          text only, no spend — does the narrative work?
preprod.py            one API call per angle, per emotion
normalize.py + qc.py  gate numerically before spending vision or video budget
[agent]               scoped vision review — two questions, pre-built grid
render_scene.py       --dry-run, then for real
[agent]               drift analysis vs the previous render
[human]               approve, then encode the finding in the validator
```

Every rule in `showrunner.py` — the 4-6s cap, mandatory re-anchoring, required
`camera_side` — started as a measurement in that second-to-last step.

---

## Measured results

**Text-only showrunner**, 3 divergent vote paths on a 9-beat spine:
- 9/9 beats played, 0 skipped, 0 validator repairs
- ~180s and ~19 LLM calls per full run
- Payoff floor 3, ceiling 5 (floor is what matters — measure the *least* cooperative audience)
- Trust diverges asymmetrically: one path ended `wrenn→okonjo +3` while `okonjo→wrenn -6`

**Video**, one 5-shot / 25s scene:
- 5 × `ref2v`, 5s each, ~5.6–6.1s inference per clip
- Costume canon verified on screen: tan `rgb(170,139,100)`, olive `rgb(88,87,67)`
- Identity flat at 8/10 across shots (was 8 → 5 → 3 with long shots and chaining)
- No mid-shot location/lighting/blocking changes at 5s
- ~$1.75 per scene at 768P

---

## Roadmap

| Milestone | Status |
|---|---|
| M1 — text-only showrunner | **done** |
| M1.5 — asset factory + QC gates | **done** |
| M2 — single-branch render chain | **done** |
| M3 — speculative A/B queue, deadline logic, cutaway fallback | next |
| M4 — live layer: synced HLS, WebSocket voting, canon log UI | planned |
| M5 — public screening | planned |
| M6 — learning loop (see below) | planned |

### The learning loop (M6)

A hard boundary, because getting it wrong in either direction is costly:

```
LIVE      deterministic service, NO agent  — state machine + queue + player
BETWEEN   agent harness on a schedule      — QC sweeps, regeneration, proposals
GATE      human review before merge
```

The live tier is deliberately not an agent: one constrained LLM call per phase, schema-validated. Between screenings is where open-ended work belongs — drift analysis, asset regeneration, telemetry-driven spine proposals.

Two guardrails:

- **Never optimize for votes.** Tuning plotlines toward crowd preference rebuilds exactly the infinite-content machine the showrunner exists to prevent. Optimize for completion rate and payoff recognition.
- **The agent may never edit its own validator.** Prompts, spines and assets are fair game for automated proposals. Schema and validation rules stay human-gated.

Long-run image quality comes from **better locks, not better prompts**: accumulate approved frames, fold them into reference sheets, eventually train a per-character LoRA.

---

## Known limitations

- **Three-quarter camera angles are not reliably promptable.** Verified across three generations and two characters with escalating specificity: `front`, `profile` and `back` pass every time; 45° three-quarters over-rotate toward profile on one side and under-rotate toward front on the other. They are dropped rather than shipped wrong — a wrong angle teaches the video model an identity the character never holds.
- **`flf` chaining is retired.** Worse than `ref2v` on identity (6/10 vs 8/10), the only mode still re-staging mid-take at 5s, and it cannot lip-sync.
- **`distinctness` cannot judge angle correctness** — it measures pixel difference, not semantics. A profile and a head-turned profile differ in pixels while being the same angle class.
- Dialogue length is not yet reconciled with shot length; `dialogue.py` warns when speech overruns a shot rather than fixing it.
- Cutaway fallback library (for missed generation deadlines) is specified but not built.

---

## License

MIT
