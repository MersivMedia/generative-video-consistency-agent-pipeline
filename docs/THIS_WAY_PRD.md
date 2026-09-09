# THIS WAY — Live Branching AI Film Engine

**Product Requirements Document + Technical Architecture**
Version 0.1 · Status: Draft for build

## 1. Summary

A live, shared-audience film that generates itself ahead of the viewer. The audience votes at branch points; both possible futures render in parallel while voting is open; the winning branch becomes canon and the loser is discarded. A persistent "showrunner" layer holds characters, relationships, clues and consequences together so the result is a coherent story being *uncovered*, not an infinite content feed.

Inspired by Henry Daubrez's THIS WAY demo ([LinkedIn post](https://www.linkedin.com/posts/upskydown_the-first-live-ai-film-engine-with-a-showrunner-ugcPost-7500634679113469952-gMh1/)), which used MiniMax H3 Max via fal.

### The core insight

Generative video is now faster than playback. On fal's H3 Max a 5-second 768p clip renders in roughly 3 seconds of wall time — about 35x the throughput of the official MiniMax H3 endpoint. That inverts the interaction loop: the viewer never waits on a model, because the system speculatively pre-renders the next branches while the audience watches the current scene.

Every architectural decision below serves two goals — **hide latency** and **prevent narrative drift**.

### Hard model constraints (verified)

| | MiniMax H3 (base) | H3 Max (fal post-trained) |
|---|---|---|
| Duration | 5–15s | 5–15s |
| Resolution | up to 2K | 480p / 768p |
| Speed | baseline | ~3s wall time for a 5s clip |
| Modes | t2v, first-and-last-frame, reference-to-video, video edit | t2v, i2v |
| Audio | native stereo | native stereo |

**Maximum clip length is 15 seconds.** This is the single most load-bearing constraint in the design and it drives §4, §5.2 and §8 below. An earlier draft of this document assumed 25-second shots; that is not achievable on any current endpoint.

## 2. Goals and non-goals

### Goals

- Run a 20–30 minute live branching film for a shared audience with **zero visible wait state**.
- Make choices demonstrably matter: an early decision must visibly return later in the story.
- Maintain recognizable characters and a consistent visual grade across the whole run.
- Keep per-screening generation cost inside a known, predictable budget.

### Non-goals (v1)

- Open-ended text prompting by the audience.
- Per-user personal branches (v1 is one shared canonical stream).
- More than two choices per branch point.
- Audience-created characters or worlds.
- Native mobile app.
- Replay/VOD of arbitrary non-canon branch paths.

## 3. Success criteria

| Metric | Target |
|---|---|
| Shot-to-shot gap (dead air) | ≥95% of transitions under 500ms |
| Character visual consistency | Named characters recognizable in ≥80% of their shots (human-rated) |
| Late payoffs per run | ≥3 moments where an earlier choice visibly returns |
| Generation deadline hit rate | ≥90% of speculative jobs ready before vote close |
| Fallback visibility | Cutaway fallbacks used in <10% of shots |
| Cost per screening | Within agreed ceiling (see §8) |

## 4. Experience

1. Audience joins a shared synced stream. Playback clock is server-authoritative — everyone sees the same frame.
2. A **scene** plays — a cluster of 2–4 generated shots, 20–40 seconds total. Branch points are **per scene, not per shot**, because a single 10-second clip does not leave a room enough time to read two options and vote.
3. Two options appear. They may be an action ("go through the door") or a line of dialogue ("tell her the truth").
4. A live tally bar shows the vote in real time. Voting window closes a few seconds before the current shot ends.
5. The winning branch — already rendered — plays immediately. The losing branch is discarded.
6. A persistent "How we got here" canon log lets late joiners and forgetful viewers catch up on the choices made so far.

## 5. Technical architecture

### 5.1 The Showrunner (story engine)

Not a free-form prompt machine. A structured, versioned state document mutated by an LLM under constraints.

```
Story     = { chapters[], beats[], characters{}, world_facts[], flags{} }
Character = { name, visual_lock_refs[], voice_id, traits[], memory[], relationships{ name: trust_int } }
Beat      = { id, premise, exit_conditions, allowed_branches[2], sets_flags[], requires_flags[] }
```

- Chapter and beat skeleton is **authored ahead of time**. Branches within a beat are **generated**. This split is the anti-slop mechanism: the audience uncovers and shapes an existing plot rather than inventing a random one.
- On each turn the showrunner LLM receives current beat + full state and emits: the next shot's prompt, two choice options, any dialogue lines, and a set of **state deltas** (e.g. `trust.marlow -= 2`, `flags.knows_about_the_key = true`).
- Deltas are validated against a schema before commit. Invalid deltas are rejected and regenerated — the state machine, not the model, owns canon.
- `requires_flags` gates future beats, so a choice made four scenes back can re-enter the story much later. This is what produces the late payoffs in §3.
- Every committed turn is appended to an immutable canon log (event-sourced), making the run fully reconstructable and auditable.

### 5.2 Speculative render pipeline

Scenes are the unit of branching. A scene is 2–4 shots of 5–15s each, 20–40s of playback.

Two optimizations define the pipeline, and both exist to move waste off the expensive layer.

### Asymmetric lookahead depth

Prepared **frames** and generated **clips** differ in cost by roughly two orders of magnitude, so they get different lookahead depths:

| Layer | Depth | Leaves at depth | Rationale |
|---|---|---|---|
| Reference frames (i2i) | **2** | 4 | One level ahead of video, plus a scene of slack |
| Video clips | **1** (current branch only) | 2 heads | Expensive. Never speculate further than the open vote |

**Frames only need to lead the video layer by one level.** An earlier draft specified
depth 3–4 on the theory that cheap frames justify deep speculation. Measured over
simulated screenings, that is wrong:

| Depth | Frames generated | Frames used | Utilization | Live peak |
|---|---|---|---|---|
| 1 | 88 | 5 | 5.7% | 22 |
| **2** | **166** | **5** | **3.0%** | **44** |
| 3 | 298 | 5 | 1.7% | 87 |
| 4 | 538 | 5 | 0.9% | 174 |
| 5 | 922 | 5 | 0.5% | 347 |

Generation roughly **doubles per level while consumption stays flat**. The reason is
structural: a frame only has to exist before the *clip that consumes it is
submitted*, and clips are already submitted one scene ahead of playback. Depth beyond
that buys nothing — it is speculation about speculation.

The correct depth is derived, not chosen:

```
depth = ceil(frame_generation_seconds / scene_seconds) + 1
```

At measured i2i speed (~10–15s per frame) against ~35s scenes that yields **2**. Only
if frame generation became very slow relative to scene length (~120s per frame) would
depth 5 be justified — and at that point the fix is a faster image model, not a
deeper tree.

Because the beat skeleton and branch axes are authored in advance, every frame the story could plausibly need is computable ahead of time. Frames are produced by **i2i from the character sheets and world plates** (§5.4), so identity and location locks propagate into the whole prepared tree.

This buys four things beyond cost:

1. **No shot is ever unconditioned.** A prepared first frame exists whichever way the room votes, so `t2v` never has to be used as a fallback.
2. **First-and-last-frame becomes fully specified.** Both ends of every chain exist as real images before the clip is requested, so the model interpolates between fixed points instead of inventing an endpoint. This is the strongest continuity primitive available.
3. **Drift is caught before video spend.** A bad frame is visible and cheap to re-roll; a bad 10-second clip is only visible after you have paid for it.
4. **Discarded branches cost almost nothing** — waste lives on the image layer, not the clip layer.

Live pruning: when a vote resolves, the dead subtree's frames are dropped and the surviving branch is extended one level deeper, holding depth constant as the story advances.

### Head / tail split within a scene

A scene divides into a **head** (the shots that play while voting is open) and a **tail** (the consequence shots that play after the vote resolves). Only heads are speculated; tails are generated just-in-time once the winner is known.

```
t=0      play scene N head (n1,n2)   ─┬─ speculate branch A head (2 shots)
                                       └─ speculate branch B head (2 shots)
t=+12s   voting opens                  (window = head_duration − safety_margin)
t=+24s   voting closes                 winner promoted; loser head evicted
t=+24s   ── generate WINNER tail (2 shots) just-in-time ──
t=+26s   play scene N tail             tails land during head playback of the winner
```

Per 4-shot branch point:

| Strategy | Video generations | Wasted |
|---|---|---|
| Full speculation | 2 × 4 = **8** | 4 (50%) |
| **Head-only speculation** | 2×2 head + 2 canon tail = **6** | 2 (33%) |

25% fewer clips per branch point, scaling with scene length. Two further wins fall out of it:

- **Chaining is more reliable.** A speculative tail would have to chain from a head frame in a branch that may never become canon. A just-in-time tail chains from a frame that actually exists on the canon path.
- **The tail is written knowing the vote.** With a single-phase turn, consequence shots must hedge across both outcomes. Split the turn and the tail is authored with the winner known, so the consequence is specific rather than vague. This is a writing-quality gain, not only a cost one.

### Buffer math — why parallel job slots are a hard requirement

At ~6s of wall time per 10-second clip, generation beats playback by roughly 1.7x. The head/tail split keeps in-flight work bounded: **4 speculative head clips + 2 just-in-time tail clips**, versus 12 clips for flat depth-2 over 3-shot branches.

Tail runway: two head shots give ~20–24s of playback; two tail clips cost ~12s serialized, ~6s parallel. Comfortable, but it is the tightest constraint in the system, so heads should be the **long** shots (12–15s) and tails the **short** ones (5–8s).

The pipeline is still only viable with **genuine concurrent job slots** — enough parallelism to keep aggregate throughput above playback. This is a procurement requirement on the provider account, not an implementation detail to optimize later.

- Priority job queue (Redis / BullMQ). Jobs on the confirmed-canon path preempt speculative lookahead jobs.
- **Lookahead depth 2** is the sweet spot: 2 branches, 4 jobs in flight. Depth 3 means 8 jobs and the waste multiplies faster than the benefit.
- ~50% of speculative generations are discarded by design. That waste *is* the price of zero latency and must be budgeted, not optimized away.
- **Every job has a deadline.** If a generation fails, is refused, or misses the deadline, the player cuts to a pre-rendered **cutaway** (ambient establishing shot, reaction close-up, or insert with voiceover) while the job is retried. The audience never sees a spinner — that is the entire product promise.
- Prompt validator pass before submission: sanitize, check character locks are present, retry once on refusal, then fall back to cutaway.

### 5.3 Character and visual consistency

The hardest problem, and the one the original demo openly admits still drifts.

H3's native modes are stronger consistency primitives than the plain image-to-video an earlier draft assumed, and they are already built into the endpoint:

- **`reference-to-video`** — a character is locked to a full **reference sheet** (8–12 frames across angles, expressions and lighting), not one or two hand-picked stills. Any shot containing a named character goes through this mode, never pure text-to-video.
- **`first-and-last-frame`** — chain shots within a scene by pinning both ends. The last frame of shot N becomes the first frame of shot N+1, so a 30-second scene made of three 10-second clips reads as continuous motion instead of three separate generations.
- **World plates** — each location is rendered once as a consistent set of establishing frames and reused as first-frames, so the lighthouse is demonstrably the same lighthouse in every scene.
- **Style bible** — a fixed lens, grade and lighting clause appended to every prompt, plus a normalizing LUT pass in post so shots from different generations match.
- **Voice locks** — one fixed TTS voice ID per character. Dialogue is synthesized from the showrunner's script and muxed under the video.
- Continuity checker (v1.5): a vision model spot-checks generated shots against reference sets and flags drift for re-generation.

### 5.4 Pre-production asset factory (ComfyUI, offline)

ComfyUI **must not sit in the live render path.** Local Wan/Hunyuan generation is tens of seconds to minutes per clip against H3 Max's ~3s, and Comfy Cloud caps concurrency at 1–5 jobs by tier (the free tier cannot call `/api/prompt` at all). Either route destroys the speculative buffer.

Its real value is offline, and offline is where consistency is actually decided:

- **Character reference sheets** — a per-character LoRA, or IPAdapter + FaceID, generating each character across many angles, expressions and lighting setups under a single identity lock. These become the input to H3's `reference-to-video`.
- **World plates** — every location rendered once, graded to the style bible, reused as first-frames forever.
- **Cutaway library** — the entire fallback set from §5.2, pre-generated and graded ahead of the screening. Zero live latency, and it is the insurance policy against missed deadlines.
- **Drift repair between screenings** — run the continuity checker over a completed run, re-render problem frames in Comfy, fold improved references back into the locks.

Requires a GPU host: Comfy Cloud on a paid tier, or a rented GPU instance. Consistency improves without costing a millisecond of live latency.

### 5.4.1 Frame precompute — the same factory, running live

The asset factory is not only a pre-production step. Because the beat skeleton and branch axes are authored in advance, the frame tree described in §5.2 can be built by the **same i2i workflows**, both ahead of the screening and continuously during it.

```
character sheets ─┐
                  ├─ i2i ─→ prepared first/last frames, depth 3–4
world plates ─────┘              │
                                 ├─ vote resolves → prune dead subtree
                                 └─ extend survivor one level deeper
```

- **Pre-screening:** build the tree down to depth 2 from the opening beat. Zero live cost.
- **During the screening:** every resolved vote frees the dead subtree's frames and triggers extension of the survivor. Image generation runs continuously at low priority, well behind clip jobs in the queue.
- **Priority discipline:** frame jobs must never contend with clip jobs. Separate queue, strictly lower priority, and an abandon-on-deadline policy — a missing prepared frame degrades to i2v from the character sheet, which is the current behaviour anyway.

The frames are what make `first-and-last-frame` mode fully specified, so this pipeline carries most of the continuity burden at a small fraction of the video budget.

### 5.5 Live layer

- Server-authoritative playback clock; clients sync to it. Shared broadcast semantics, not per-user video tags.
- WebSocket vote channel. One vote per session token, rate-limited, deduped. Live tally pushed to all clients.
- Output as **HLS**: each finished shot is transcoded to a segment and appended to a rolling playlist, so late joiners get a real stream rather than a chain of separate clips.

### 5.6 Stack

| Layer | Choice |
|---|---|
| Video generation | fal (MiniMax H3 Max) primary; second provider as failover |
| Showrunner LLM | Claude / GPT-class model with strict JSON schema output |
| Dialogue + audio | ElevenLabs per-character voice IDs; music/ambience bed per chapter |
| Orchestrator | Node/TypeScript or Python FastAPI |
| Queue / state | Redis (jobs, playback clock) + Postgres (canon log, votes, runs) |
| Assembly | ffmpeg workers — mux, LUT, HLS segmenting |
| Client | Next.js + hls.js, WebSocket votes |
| Storage / CDN | S3 or R2 behind Cloudflare |

## 6. Data model (v1)

- `runs` — one screening. Seed, story id, start time, status.
- `canon_events` — append-only. Beat id, shot id, chosen branch, state deltas, timestamp.
- `shots` — prompt, provider job id, mode (`ref2v`/`flf`/`t2v`), status, asset URL, duration (5–15s), scene_id, is_canon, is_fallback.
- `scenes` — ordered shot list, total duration, branch_point_id, `head_shots` / `tail_shots` counts. The unit of branching.
- `frames` — prepared reference frames: branch path key, position (first/last), source refs used, tree depth, pruned_at. The image-layer tree.
- `branch_points` — options offered, vote counts, window open/close, resolution.
- `votes` — session token, branch point id, choice, timestamp.
- `characters` — reference assets, voice id, current trait/memory snapshot.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Visual/character drift | `reference-to-video` against Comfy-built reference sheets + first-and-last-frame chaining + LUT normalization; accept residual drift as a known v1 limitation |
| Generation cost from discarded branches | Cap lookahead at depth 2; branch per scene not per shot; speculate at 480p and re-render canon at 768p; reuse plates and cutaways |
| Missed deadlines / failed generations | Deadline-aware queue + pre-rendered cutaway library + single automatic retry |
| Model refusals or incoherent geography | Prompt validator, sanitized retry, then cutaway |
| Vote brigading | Session tokens, rate limits, per-session single vote, anomaly detection on tally curves |
| Narrative incoherence | Authored beat skeleton, schema-validated state deltas, flag-gated payoffs |
| Tail generation on the critical path | The head/tail split removes the cached alternate branch behind a tail shot, concentrating risk. Keep heads long (12–15s) for runway, tails short (5–8s) to render fast, and require the cutaway library to cover **every scene's tail position specifically**, not generically |
| Frame precompute starving clip jobs | Separate lower-priority queue for i2i; abandon frames on deadline; degrade to i2v from the character sheet rather than delaying a clip |
| Insufficient parallel job slots | Buffer collapses (see §5.2). Verify concurrency limits on the provider account BEFORE M3; load-test at 12 clips in flight |
| Provider outage mid-screening | Secondary video provider configured and warm; degrade to cutaway montage rather than stopping |

## 8. Cost model

Revised against the real 15-second ceiling and the head/tail split. One 25-minute screening:

**Video layer**

- ~150 canonical shots at ~10s each (not 40–60 at 25s — the earlier figure assumed an impossible clip length)
- Head-only speculation at 33% waste ⇒ **~225 clip generations per run**
- Flat depth-2 speculation would have been ~300, so the split saves ~75 clips per screening

**Image layer**

- Depth 2 frame tree, pruned and re-extended at every branch point
- ~35–44 frames live at any moment, ~166 i2i generations across a full run
- At roughly two orders of magnitude below clip cost, this is **a rounding error against the video layer** while carrying most of the consistency burden

**Other:** TTS dialogue (cheap), ffmpeg/CDN (negligible).

Per-run cost is dominated entirely by `clip_generations × per-clip price`. The design deliberately pushes waste onto the image layer, where it is nearly free.

Levers, in order of impact:

1. **Head/tail split** — speculate only the shots that play during voting. 50% waste to 33%, ~75 fewer clips per screening.
2. **Fewer branch points** — branching per scene rather than per shot already cuts speculative waste by ~3x versus branching every shot, because only the scene's shots are duplicated, not every clip.
3. **Longer clips where motion allows** — 15s shots need 40% fewer generations than 10s shots for the same runtime. Apply to heads specifically, which also buys tail runway.
4. **Reuse** — cutaways, establishing shots and world plates are generated once and amortized across every screening.
5. **Asymmetric quality** — speculate at 480p, re-render only the winning canon branch at 768p.
6. **Shorter runtime** — a 15-minute screening costs 40% less than 25 minutes.

## 9. Milestones

**M1 — Showrunner, text only.** State machine + LLM emits a coherent 3-chapter branch tree with characters, flags, trust deltas and at least three flag-gated late payoffs. Fully readable as a script. No video, no cost. *This is the go/no-go gate: if continuity fails in plain text, no video pipeline can rescue it.*

**M1.5 — Asset factory.** ComfyUI (Cloud or rented GPU) builds character reference sheets, world plates and the cutaway library, all graded to the style bible. Also stands up the i2i frame-precompute workflow (§5.4.1) that the depth 3–4 frame tree runs on. Offline, no latency budget, gates M2 because M2 consumes its output.

**M2 — Single-branch render chain.** Text → `reference-to-video` and first-and-last-frame shots → ffmpeg mux → plays as one continuous film. Validates consistency tooling on the happy path and measures real drift.

**M3 — Speculative A/B queue.** Head-only branch speculation, just-in-time tail generation, the depth-2 frame tree with live prune-and-extend, deadline logic, cutaway fallback, hit-rate instrumentation. Verify provider concurrency limits before starting; load-test at 6 clips in flight.

**M4 — Live layer.** Synced HLS playback, WebSocket voting, canon log UI. Ten-person internal test.

**M5 — Public screening.** Hosted run with a live audience, full telemetry, cost reconciliation against §8.

**M6 — Learning loop.** Stand up the between-screenings agent harness (§10): scheduled QC sweeps, drift analysis on canon clips, telemetry-driven spine and prompt proposals, all human-gated. Requires M5 telemetry to exist first — there is nothing to learn from before a real audience has voted.

## 10. Operational architecture and the learning loop

The system splits into three tiers with a hard boundary between them. Getting this
boundary wrong in either direction is costly: an agent in the live path makes latency
nondeterministic, and no agent between screenings means nothing improves.

```
LIVE          deterministic service — NO agent, NO subagents
              showrunner state machine + job queue + player

BETWEEN       agent harness on a schedule
              QC sweeps, asset regeneration, drift analysis, proposals

GATE          human review before anything merges
```

### 10.1 The live tier is not an agent

During a screening there is a ~24 second vote window, an audience watching, and a
requirement that no shot ever shows a spinner. An agent loop is the wrong instrument:
nondeterministic latency, variable token spend, and a failure mode where it does
something creative at the worst possible moment.

The showrunner in §5.1 is deliberately **not** an agent. It makes exactly one
constrained LLM call per phase, validates the result against a schema, and rejects
anything non-conforming. One call, bounded, verifiable. That is why the mode
discipline in §5.3 actually holds — it is enforced by code, not by a model's good
intentions. The live tier stays a boring deterministic service.

### 10.2 The between-screenings tier is an agent harness

This is where open-ended work belongs, and it is the tier that makes the system
improve:

- run objective QC across all assets; regenerate anything that fails
- vision-check new reference sheets in narrowly scoped parallel batches
- sample canon clips, measure drift against reference locks, re-roll bad frames
- read vote and drop-off telemetry, propose spine and prompt changes
- extend the frame tree for the next screening's opening beats

Subagents are the right tool here specifically because verification is
context-expensive and parallelizable, and because a fresh context is more critical
than the context that produced the asset.

### 10.3 Most of the learning signal is arithmetic, not judgement

| Signal | What it tells you |
|---|---|
| Vote distribution per branch | A 95/5 split means one option was never really a choice |
| Drop-off timing | Where the film loses people |
| Deadline miss rate | Whether the buffer math in §5.2 holds in the wild |
| QC neutrality / uniformity / distinctness | Asset regressions, caught automatically |
| Cutaway usage rate | Pipeline health |
| Payoff recognition | Whether consequences actually land |

These are analytics, not LLM work. The agent's job is narrower and more valuable:
turn a measured pattern into a specific proposed change — *"branch b4 split 94/6
across four screenings; option B is dead weight, here is a rewritten branch_axis."*
A model staring at raw logs hoping for insight is waste.

**Objective checks must come first and gate the expensive ones.** Verified in
development: a vision subagent spent ~400 seconds to report a colour cast that a
border-region histogram measured exactly in under a second. Vision review is for
judgements arithmetic cannot make — "does this expression read as grief" — and
should only run on sets that already passed the cheap gates.

### 10.4 Two guardrails

**Do not optimize for votes.** Tuning plotlines toward whatever the crowd picks
rebuilds precisely the infinite-content machine this design exists to avoid. The
showrunner's purpose is to resist drift; a naive engagement-maximizing loop
reintroduces drift with extra steps. Optimize for **completion rate and payoff
recognition**, never for vote enthusiasm.

**The agent may never edit its own validator.** The validator is the safety layer —
it is what makes unrenderable, identity-breaking and continuity-breaking shots
impossible. An agent able to loosen its own constraints will eventually ship a broken
screening. Prompts, spines, assets and reference sheets are fair game for automated
proposals. Schema and validation rules stay human-gated.

### 10.5 Where quality actually improves

For image and video quality, the long-run gain is not better prompts — it is
**better locks**. Accumulate approved frames across screenings, fold the strongest
into the reference sheets, and once there is a corpus of a few hundred approved
frames per character, train a per-character LoRA on it. Reference sheets are a decent
identity lock; a LoRA trained on approved in-world frames is a much stronger one.

This is the point where a GPU finally justifies itself (§5.4 notes pre-production
needs none), and it remains an offline job either way.

### 10.6 Procedural memory

Everything learned during development persists as a **skill**, not as tribal
knowledge: the 15-second ceiling, the measured 38% waste figure, the
prune-against-canon-path bug, silent `max_images` under-delivery, the grade-leak
failure with its exact measured values, the QC thresholds. A fresh session picks all
of it up cold and does not re-make the mistakes. The production learning loop is the
same mechanism with telemetry feeding it instead of a developer noticing things.

## 11. Open questions

- Do losing branches get archived as a "what if" gallery, or hard-deleted? (Cost vs. novelty.)
- Is the vote plurality or does a supermajority unlock a rarer third path?
- Does the showrunner ever *override* the audience for dramatic reasons, and is that disclosed?
- Fixed screening times, or continuous always-on stream with rolling audience?
- Should tail shots ever be speculated for the highest-stakes branch points, buying safety back at the cost of the 33% waste figure?
- **Biased rather than deeper speculation.** Uniform depth 2 is correct, but the two branches at that depth need not get equal treatment. If telemetry (§10.3) shows audiences reliably favour one branch *type* — cooperate over refuse, act over wait — the likely branch could be prepared at full quality and the unlikely one at reduced resolution, re-rendered only if it wins. This is a *quality* asymmetry within a fixed depth, not extra depth, and it is the right shape for the idea that deep speculation on a likely path could pay off.
- How many approved frames per character are needed before a LoRA beats a reference sheet? (§10.5)
- Does the between-screenings agent propose spine changes as PRs for review, or write directly to a staging spine that a human promotes?
- Is per-character LoRA training worth the GPU cost versus simply curating better reference sheets?
