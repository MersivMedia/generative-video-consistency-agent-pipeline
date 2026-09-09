# Running inside an agent harness

This pipeline was built and operated from inside an agent harness
([Hermes](https://hermes-agent.nousresearch.com/docs), OpenClaw, Claude Code, or
any harness with shell access plus scheduled tasks). This document explains
**what to automate, what to keep deterministic, and how to wire it up.**

The single most important decision is the boundary.

---

## The boundary: three tiers

```
┌─ LIVE ─────────────────────────────────────────────────────┐
│  deterministic service, NO agent                            │
│  showrunner state machine + render queue + player           │
│  ONE constrained, schema-validated LLM call per phase       │
│  a 15-second vote window cannot absorb agent latency        │
└─────────────────────────────────────────────────────────────┘
                             │  telemetry, canon logs, rendered assets
                             ▼
┌─ BETWEEN ──────────────────────────────────────────────────┐
│  agent harness, scheduled or on-demand                      │
│  QC sweeps · asset regeneration · drift analysis            │
│  prompt + story-spine proposals · cost review               │
└─────────────────────────────────────────────────────────────┘
                             │  proposed diffs, never direct writes
                             ▼
┌─ GATE ─────────────────────────────────────────────────────┐
│  human review before merge                                  │
└─────────────────────────────────────────────────────────────┘
```

**Why the live tier is not an agent.** During a screening the system has a hard
deadline: the next clip must exist before the current one ends. The showrunner is
already a state machine making one strict-JSON call per phase, validated against
a schema. Introducing open-ended tool use there trades a bounded latency for an
unbounded one, in the one place the product cannot tolerate it.

**Why the offline tier should be an agent.** Everything between screenings is
open-ended: deciding *which* plate failed and *why*, whether a prompt revision
helped, whether drift is worsening. That is judgement work over noisy artifacts —
exactly what an agent with shell access is good at.

### Two guardrails, non-negotiable

1. **Never optimize for votes.** Tuning plotlines toward crowd preference
   converges on mush and rebuilds the infinite-content machine the showrunner
   exists to prevent. Optimize for **completion rate** and **payoff
   recognition**.
2. **The agent may never edit its own validator.** Prompts, story spines and
   assets are fair game for automated proposals. `showrunner.py` schema and
   validation rules stay human-gated — otherwise the system relaxes constraints
   instead of meeting them.

---

## Install the skills

Both skills in `skills/` are portable markdown with YAML frontmatter and work in
any harness that loads skill files.

**Hermes**

```bash
cp -r skills/branching-ai-film-engine   ~/.hermes/skills/
cp -r skills/generative-video-consistency ~/.hermes/skills/
```

Then `skill_view(name='generative-video-consistency')`, or let the harness match
them automatically from their `description` frontmatter.

**OpenClaw / Claude Code**

```bash
cp -r skills/* ~/.claude/skills/          # or .claude/skills/ in-project
```

**Anything else** — the files are plain markdown. Paste `SKILL.md` into the
system prompt or load it as a document. `scripts/qc.py` and
`scripts/normalize.py` are standalone and dependency-light (Pillow only).

---

## Credentials

The agent needs the same four keys the pipeline does. Keep them in a file
**outside the repository** and source it per command:

```bash
set -a && . ~/.secrets/thisway.env && set +a && python engine/qc.py assets/character/wrenn
```

Two practical notes learned the hard way:

- Sandboxed code-execution tools generally **do not inherit** the shell
  environment. Run anything that touches an API from the harness's terminal tool,
  not its Python sandbox.
- If a key is ever pasted into a chat, treat it as logged and rotate it.

---

## Task 1 — QC sweep (safe to automate fully)

Pure measurement, no spending, no writes. The best first automation.

```
Prompt:
  Run the asset QC sweep for the THIS WAY pipeline at /path/to/repo.

  For each character directory under assets/character/:
    python engine/qc.py <dir> --costume "<costume string from stories/*.json>"
  For each location directory under assets/plates/:
    python engine/qc.py <dir> --graded

  Report ONLY: which directories fail, which gate, and the measured value
  against tolerance. Do not regenerate anything. Do not edit any file.
  If everything passes, say so in one line.
```

Suitable for a scheduled job. In Hermes:

```
cronjob(action='create', schedule='0 9 * * *',
        prompt='<the above>', enabled_toolsets=['terminal'])
```

---

## Task 2 — Regenerate failing assets (needs a spend gate)

```
Prompt:
  Assets at <dir> fail QC: <gate> = <value> (tolerance <tol>).

  1. Run engine/normalize.py on the directory and re-run engine/qc.py.
     Normalization fixes backdrop brightness and colour casts without spending.
  2. If it still fails, identify the specific outlier files. Report the list
     and the estimated regeneration cost, then STOP and wait for approval.
  3. Only after approval, regenerate ONE item per API call — never a batch —
     and assert the returned count.
  4. Re-run normalize + qc and report before/after numbers.

  Never delete assets. Quarantine with `mv` to assets/_quarantine/<name>_<date>/.
```

The stop-before-spending step matters. So does never-delete: a blanket restore
from an unversioned backup directory once overwrote a good asset set with stale
plates, and the only signal was a QC failure afterwards.

---

## Task 3 — Vision review (scope it narrowly)

Numeric gates cannot judge whether an expression reads as grief or whether a
doorway is geometrically possible. That needs vision — but naive dispatch is how
budgets die. Five review agents were truncated at their iteration cap by a vision
tool that intermittently returned loader stubs instead of rendered images.

What works:

```
Prompt:
  Answer TWO questions about ONE pre-built comparison grid at <path>.
  The grid is already assembled — do not extract frames.

  1. <specific question>
  2. <specific question>

  If the vision tool returns a stub instead of an image, retry at most FIVE
  times TOTAL across all images, then report "vision tool unavailable" and
  stop. An honest partial answer is worth more than an exhausted budget.
  Never fabricate observations. Report only these two answers.
```

Rules that made the difference:

- **Pre-build the grid yourself** with ffmpeg + PIL. One labelled image beats
  twenty tool calls.
- **Two questions, maximum.** Broad review briefs get truncated.
- **Cap retries explicitly** and give permission to fail.
- **Ask for pixel samples** on anything colour-related — vision reports and
  pixel data disagreed several times, and the pixels were right.

---

## Task 4 — Drift analysis across renders

```
Prompt:
  Compare renders/<A>/ (baseline) and renders/<B>/ (after changes).
  Changes made: <list>.

  Extract first/mid/last frames per shot with ffmpeg. Sample garment pixels
  with PIL and report RGB numbers — do not eyeball colour. Score identity
  0-10 per shot on the same scale as the baseline.

  Report: per-shot scores, whether fidelity holds flat or decays, and whether
  the specific changes are visible in the pixel data. You cannot regenerate
  anything — verify and report only.
```

This is what proved the rules now encoded in the validator: identity decayed
8/10 → 5/10 → 3/10 across chained shots, and held flat at 8/10 once every shot
re-anchored to the reference sheet.

---

## Task 5 — Story-spine proposals (propose only)

```
Prompt:
  Read runs/*/canon.json and the telemetry summary. Identify beats where
  the payoff floor is low or a flag gate is never satisfied.

  Propose edits to stories/*.json as a DIFF for review. Do not apply them.
  Do not modify engine/showrunner.py under any circumstances — validator
  changes are human-gated.
```

A real finding from this task class: two beats gated on the same single flag, so
one early vote silently skipped a third of a chapter. The fix was authoring
(alternative flag gates), not code.

---

## What to automate, ranked

| Task | Automate? | Why |
|---|---|---|
| QC sweeps | **yes, fully** | measurement only, no spend, no writes |
| Drift analysis between renders | **yes, fully** | read-only, high signal |
| Normalization | **yes** | deterministic post-process, reversible |
| Asset regeneration | **with approval gate** | costs money |
| Prompt revisions | **propose only** | needs a human read of the output |
| Story-spine edits | **propose only** | authorial judgement |
| Validator / schema changes | **never** | the agent will relax constraints instead of meeting them |
| Anything in the live path | **never** | latency is the product |

---

## Harness lessons worth carrying over

- **Batch tool calls into one step.** Navigate-wait-extract-act as a single call,
  not four round trips.
- **Long jobs go to the background.** A full pre-production run takes minutes and
  will blow a foreground timeout. Start it detached, poll for completion.
- **Append results to a file, then aggregate in code.** For multi-item sweeps,
  write each result to JSON as you go rather than holding it in context. Count
  and dedupe with Python, not from memory.
- **Assert every file edit landed.** String-replace edits silently no-op when the
  anchor text has changed. Several "applied" fixes were absent from the file, and
  only a paid render proved it.
- **Quarantine, never delete.** `mv` to a dated directory. Generated assets cost
  real money, and destructive commands should require explicit approval.
- **Dry run before paid execution.** `--dry-run` printing the payload caught a
  29MB request against a ~10MB limit before it cost anything.

---

## Minimal operating loop

```
1.  python engine/run_story.py stories/x.json --voter seed --seed 42
    text only, no spend. Does the narrative work?

2.  python engine/preprod.py sheet stories/x.json --character <name>
    one call per angle and per emotion

3.  python engine/normalize.py assets/character/<name>
    python engine/qc.py assets/character/<name> --costume "<canon>"
    gate before spending vision or video budget

4.  [agent] scoped vision review, two questions, pre-built grid

5.  python engine/render_scene.py runs/<run>/canon.json --scene 0 --dry-run
    then without --dry-run

6.  [agent] drift analysis vs the previous render

7.  [human] approve rule changes; encode findings in the validator
```

Step 7 is the loop closing. Every rule in `showrunner.py` — the 4-6s shot cap,
mandatory re-anchoring, required `camera_side` — started as a measurement in
step 6.
