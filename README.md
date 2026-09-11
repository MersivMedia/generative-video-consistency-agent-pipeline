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
engine/cutaway.py       character-free fallback clips + no-repeat runtime picker
engine/render_queue.py  M3 deadline-ordered speculative queue, branch pruning
engine/simulate.py      screening simulator — does the buffer hold?
engine/live.py          screening state, HLS playlist, vote tally, viewer hub
engine/server.py        FastAPI + WebSocket screening server
engine/viewer.html      the audience page (no build step)
engine/audio_qc.py      speech detector — MEASURED AND REJECTED, kept as record
engine/identity.py      signed viewer tokens + append-only screening journal
engine/ladder.py        adaptive bitrate ladder (low/mid/high), aligned GOPs
docs/DEPLOYMENT.md      TLS, CDN, supervision — what a public screening needs
tests/test_identity.py  8 tests: identity, forgery, replay, ladder alignment
tests/test_render_queue.py  8 behavioural tests (fake clock, deterministic)
tests/test_live.py      end-to-end: concurrent viewers, real votes
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
| Dialogue must fit the shot | Measured 15.7 chars/sec; a 5s shot holds ~69 characters. Rejected at authoring time, not warned about after paying |

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

# Build the cutaway fallback library for a location (once, offline)
python engine/cutaway.py stories/the_signal.json --beat b1_arrival --dry-run
python engine/cutaway.py stories/the_signal.json --beat b1_arrival

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

## M3 — speculative queue and deadline logic

Generation runs ahead of playback, both futures are in flight before the vote
lands, and anything late degrades to a cutaway instead of a stall.

### Concurrency is the entire budget

```
1 x 5s clip serial      22.2s wall     0.22x realtime   buffer collapses
4 x 5s clips parallel    4.6s wall     4.32x realtime   3.5x speedup
```

Measured on fal, not assumed. Parallel render slots are a hard requirement.

Also found and fixed here: reference plates were being **re-uploaded on every
shot** — 16.9MB per shot, which is where the gap between 5.6s of inference and
22.2s of wall time went. Content-hash upload cache: **3.3s cold, 0.047s warm,
71x faster.**

### The tail deadline was structurally impossible

The simulator's first useful output was a failure. At `head_shots=3` with
voting closing at 60% of the head:

```
tail runway   6.0s
tail render   7.1-9.1s   (5.6-6.1 inference + 1.5-3.0 overhead)
result        9/9 tails missed, at EVERY slot count from 1 to 8
```

More slots never helped, because **a tail cannot begin before the vote it
depends on**. Parallelism cannot buy time that does not exist. The fix was
parametric, not architectural:

| head_shots | vote closes | tail runway | misses |
|---|---|---|---|
| 3 | 60% | 6.0s | 9/9 |
| 3 | 40% | 9.0s | 2.1/9 |
| **4** | **40%** | **12.0s** | **0/9** |

`head_shots: 4` and `vote_close_frac: 0.40` are now story-file canon, with the
derivation recorded inline so nobody "optimizes" them back.

### Verified schedule

```
slots  miss rate  verdict
1      0.944      BUFFER COLLAPSES
2      0.500      BUFFER COLLAPSES
3      0.000      HOLDS
4      0.000      HOLDS
```

Three slots is the floor; four is the operating point. 91 clips per screening,
36 speculative and discarded, 275s of film.

### Queue semantics

`RenderQueue` is deadline-ordered with explicit priorities — `CRITICAL`,
`TAIL`, `SPECULATIVE`, `PREFETCH` — and knows about branches so it can abandon
work the audience already voted away. Eight behavioural tests cover priority
inversion, earliest-deadline-first, whole-branch pruning, promotion on vote,
expiry of speculative work while retaining late canon work, error surfacing,
and real thread overlap.

Pruning is by **branch id across the beat**, not "the other option" — the frame
planner previously grew 76 to 200 live jobs by cancelling only the immediate
loser and orphaning its subtree.

```bash
python engine/simulate.py stories/the_signal.json --sweep
python tests/test_render_queue.py
```

---

## M4 — the live layer

```bash
python engine/server.py stories/the_signal.json --demo --port 8137
# open http://127.0.0.1:8137
```

`--demo` replays already-rendered clips instead of calling the video API, so
the whole live layer can be exercised for free. Everything except the render
call is identical between demo and live.

### The server owns the playhead

Every viewer must see the same frame at the same moment, because a vote is
meaningless if the room is looking at different points in the story. Clients
report nothing that affects state — they receive the authoritative position and
correct toward it when they drift more than 2s. A late joiner is dropped into
the current moment, never the beginning.

```
GET  /                    the viewer (single page, no build step)
GET  /stream/index.m3u8   EVENT playlist, appended as clips land
GET  /segments/{name}     the clips themselves
WS   /live                state push + vote intake
```

HLS rather than WebRTC because this is a shared screening, not a conversation:
a few seconds of *uniform* latency is fine, per-viewer streams are not. An
EVENT playlist (no `ENDLIST` until the screening ends) is what makes the
speculative model work — the player keeps re-fetching and picks up segments
that did not exist when it connected.

### Vote integrity

One vote per connection per beat, tallied server-side, frozen at close. The
tally is broadcast so the room can watch the split, but the winner is computed
from the server's own record and never from a client-reported total. Ties break
deterministically toward the first choice, because an unrecorded coin flip
makes the canon log a lie.

Verified against a running server with real WebSocket clients:

```
PASS state pushed: seq 8 -> 8
PASS late joiner at 10.42s of 31.1s published (20.68s buffered)
PASS invalid choice rejected by the server
votes accepted: 5/5

canon log:
  b2_the_hail   winner=A  tally={'A': 3, 'B': 2}  voters=5  at=34.0s
```

The 3/2 split was recorded exactly, and the decision is permanent in the canon
log with its vote count and playhead position.

### Six bugs that only a real device found

Every one of these passed a component check and failed in a browser. The
component checks were not wrong, they were answering a narrower question than
"can someone watch this".

| Symptom | Cause | Fix |
|---|---|---|
| Page never loaded | `ufw` allowed only 80/443/22; port dropped silently | open the port, or proxy through the existing Caddy |
| No playback at all | segments were progressive MP4 (`ftyp+moov+mdat`) — one self-contained movie per file, which hls.js cannot splice | remux to MPEG-TS on publish, stream-copy, ~50ms |
| Played briefly then froze | every segment started at PTS 1.4, so time jumped backwards at each boundary | `-output_ts_offset` with the playlist's running offset |
| Still no video | **my own resync logic**: seek on >2s drift, evaluated twice a second, so the player re-seeked before a frame could render | seek only when playing, drift >8s, max once per 10s |
| Vote needed several taps | `innerHTML` rebuilt the buttons on every state push, destroying the element mid-tap | build once per beat, update in place, `pointerdown` not `click` |
| Slow start, no sound | 9 Mbps segments (5.6MB before frame one); `muted` is mandatory for autoplay | re-encode to ~2 Mbps / 1280 wide + an explicit unmute button |

`ffprobe` said the files were valid video, and they were. Valid video is not a
valid HLS timeline, and a valid timeline is not a working player. The only
check that meant anything was pointing a real HLS client at the real URL:

```bash
ffmpeg -v error -i "http://HOST:PORT/stream/index.m3u8" -t 15 -f null -
```

### Audio: the model invents speech unless forbidden

H3 Max always returns an audio track and exposes no mute parameter, so with
nothing forbidding speech it generates muttering, crowd murmur and voice-over
across shots that have no dialogue at all. The story's `narration` field is
deliberately never sent to the model — it is authorial subtext, not spoken
text — so any voice heard on a silent shot is pure invention.

Every shot prompt now carries an explicit `AUDIO:` directive: diegetic
background only (weather, sea, footsteps, room tone), and no speech,
voice-over, narrator, muttering, whispering, singing or crowd voices.
Dialogue-free shots add "nobody talks, no lips move to form words".

**Division of labour: the model owns background, ElevenLabs owns every spoken
word** — delivered to the model as reference audio so it has a voice to
lip-sync rather than a vacuum to fill.

A speech *detector* was built as a safety net and **failed on labelled data**:

```
shot_01 (real speech)    0.155
shot_02 (real speech)    0.173
rain    (pure ambience)  0.228   <- higher than every speech clip
```

Rain modulates at almost exactly syllable rate (2-8 Hz), so any threshold that
catches speech destroys the weather. Shipped disabled with the numbers in the
docstring, for the same reason as the angle metric: a gate that silently
mis-scores is worse than an acknowledged gap. `--lowpass` at 250 Hz remains as
a deterministic opt-in fallback.

### Identity, persistence, and adaptive bitrate

Three constraints a public screening cannot ship without — and a real bug found
while closing them.

**Viewer identity was a memory address.** `id(websocket)` looked like a
per-connection identifier and is not: CPython recycles addresses aggressively,
and 2000 short-lived objects produced **3 distinct ids** in direct measurement.
So a reconnecting viewer could inherit a departed viewer's ballot, identity did
not survive a refresh, and N tabs meant N votes. The M4 tests missed it because
they held every socket open at once, so no address was ever recycled.

Replaced with signed HMAC tokens minted on first contact and kept in
`localStorage`. Verified over the wire:

```
tab1 (token A)   accepted=True
tab2 (token A)   accepted=True
phone (token B)  accepted=True
tally={'A': 2, 'B': 0}  total ballots=2
=> 3 connections, 2 identities, 2 ballots
```

This is deliberately not login. A screening is anonymous; it only has to count
one person once. Clearing storage earns a new identity — what this fixes is the
accidental case (refresh, reconnect, second tab), which is what actually
corrupts a live tally.

**Persistence is an append-only journal**, one fsynced JSON line per state
transition, replayed on boot. A snapshot would let a crash rewrite a decision
that has already been shown; the canon log is the product's memory. Verified by
killing the process mid-screening:

```
resumed: 20 segments, 3 decisions
resuming with 6 beats remaining
```

Already-decided beats are skipped and the clock is rewound so the playhead
lands where it left off. A journalled segment whose media file is gone is
skipped rather than offered to the player.

**Adaptive bitrate ladder**, measured per 5s clip:

```
rung   width   video    size      rate       encode
low     640     800k    0.62 MB   1.0 Mbps   1.6s
mid     960    1400k    1.01 MB   1.6 Mbps   2.1s
high   1280    2400k    1.70 MB   2.7 Mbps   3.0s
```

Rungs share an identical GOP with scene-cut detection off, so a mid-stream
switch lands on a keyframe instead of glitching. They encode in parallel, so
6.7s of serial work costs ~3s wall. The master playlist lists lowest-first:
starting low and climbing reaches a picture faster than starting high and
stalling.

**And the bug that ladder encoding exposed:** three ffmpeg passes called
directly from the async loop froze *every* HTTP request and state push for ~3s
per clip — the server timed out entirely while preparing a segment. Moved to
`asyncio.to_thread`; responses now land in 3-17ms during encoding. Any
CPU-bound work in an async server is a liveness bug, not a performance one.

### Buffer health in production

Live measurements during a demo screening: playhead 39.5s, published 62.2s,
**22.7s of rendered material ahead of playback**. That is the M3 schedule
holding in a real process rather than a simulator.

Two fixes found by testing rather than reading: `HEAD` on the playlist and
segments returned 405, which makes some players and CDNs abandon the stream
before ever issuing a `GET`; and the playlist must be served `no-store` or a
cached copy silently freezes a viewer at the moment they connected.

---

## Roadmap

| Milestone | Status |
|---|---|
| M1 — text-only showrunner | **done** |
| M1.5 — asset factory + QC gates | **done** |
| M2 — single-branch render chain | **done** |
| M3 — speculative A/B queue, deadline logic, cutaway fallback | **done** |
| M4 — live layer: synced HLS, WebSocket voting, canon log UI | **done** |
| M5 — public screening | next |
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

- **`flf` chaining is retired.** Worse than `ref2v` on identity (6/10 vs 8/10),
  the only mode still re-staging blocking mid-take at 5s, and it cannot
  lip-sync because image-to-video accepts no audio input.
- **Angle correctness is still not measurable.** Two silhouette heuristics were
  built and both failed on labelled plates — see `qc.distinctness`. Front /
  profile / back are generated and verified; 45-degree three-quarters are not
  generated at all.
- **Invented speech is prevented by prompt, not by gate.** The audio directive
  works, but `audio_qc.py` proves the detector that would enforce it does not
  discriminate (rain modulates at syllable rate). A `--lowpass` fallback exists.
- **Sybil resistance is bounded, by design.** One signed token is one ballot
  across refreshes, reconnections and extra tabs, and a per-IP mint cap means
  clearing storage in a loop returns an identity already held rather than a new
  one. Someone with many source addresses can still stuff a ballot; stopping
  that needs real accounts, which is a product decision, not a code one. The
  cap is deliberately generous (8/source) because a lecture hall behind one NAT
  is a legitimate crowd.
- **One process, one screening.** The journal survives restarts and the CDN
  handles fan-out, but there is no horizontal scale: two screenings means two
  processes. No moderation, no auth, no admin surface.
- Cutaway coverage is per-location and must be generated per beat before a
  live screening; the library raises rather than stalling when exhausted.
- Speech-rate budgeting assumes the three configured ElevenLabs voices. A new
  voice needs re-measuring and a new `SPEECH_CHARS_PER_SEC`.

### Fixed since first publication

| Was | Now |
|---|---|
| Dialogue overruns warned about *after* paying for audio and video | Validator rejects at authoring time from a measured 15.7 chars/sec budget |
| Cutaway fallback "specified but not built" | `engine/cutaway.py` + `CutawayLibrary`, wired in, failure path tested |
| `distinctness` silently implied it checked angles | Documents what it cannot see, with both failed metrics recorded |
| Viewer identity was `id(websocket)` — a recycled memory address | Signed HMAC tokens in `localStorage`; one ballot across tabs and refreshes |
| A crash lost the entire screening | Append-only fsynced journal, replayed on restart |
| One 2 Mbps rendition: buffer or nothing on a weak connection | Three-rung ABR ladder with aligned GOPs |
| Ladder encoding blocked the event loop | `asyncio.to_thread`; responsive in 3-17ms during encodes |
| No HTTPS, app port exposed directly | Caddy reverse proxy with WebSocket upgrade — see `docs/DEPLOYMENT.md` |
| Segments uncacheable, so no CDN was possible | Immutable segments (`max-age=31536000`), `no-store` playlists, open CORS |
| Absolute playlist URLs broke prefix mounting | Relative URLs + client paths derived from `location.pathname` |
| Unlimited token minting | Per-IP cap; 12 requests from one source yield 8 identities |
| Segment route took an unvalidated path | Regex + rung allowlist; traversal attempts return 404 |

## License

MIT
