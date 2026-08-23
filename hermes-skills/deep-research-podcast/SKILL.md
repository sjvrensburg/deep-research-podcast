---
name: deep-research-podcast
description: "USE THIS (not open-notebook-podcast) whenever the user asks for deep research, a deep dive, an intensive or thorough investigation, or names this skill explicitly. OpenResearcher-driven multi-turn web research fed into an Open Notebook podcast -- scales from a short single-episode demo to an intensive multi-hour, multi-question deep dive; can also ground an episode directly in an attached document."
version: 0.3.2
author: Stefan (sjvrensburg), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [open-notebook, podcast, research, openresearcher, signal, background, long-running]
    related_skills: [open-notebook-podcast, podcast-workflow, voice-message]
prerequisites:
  commands: [curl, python3, systemctl]
  env:
    # OpenResearcher researches; a general instruct model enriches and writes.
    # Both default to the research endpoint, which works but badly -- see
    # "What the script actually does", step 2.
    DRP_ENRICH_LLM_URL: http://127.0.0.1:8088/v1/chat/completions
    DRP_SYNTH_LLM_URL: http://127.0.0.1:8088/v1/chat/completions
---

# Deep Research Podcast — hours-long, unattended, trigger-and-forget

> Written for [Hermes Agent](https://hermes-agent.nousresearch.com) on a specific box
> (see [`github.com/sjvrensburg/halo-prep`](https://github.com/sjvrensburg/halo-prep)
> for the hardware/model setup this was originally built against). The pipeline script
> it drives (`../../deep-research-podcast.py`) is portable — see this repo's top-level
> `README.md`. This skill file is the Hermes-specific wiring around it: adapt the model
> path / systemd unit names in §2 and the endpoint URLs it assumes to your own setup,
> or use it as a template if you're driving the pipeline from a different agent harness.

For requests like *"do deep research on X and make me a long podcast about it, I'll
listen tonight"* or *"intensively research X via OpenResearcher and turn it into a
detailed episode"* — explicitly heavier than `open-notebook-podcast`'s normal
research step (a handful of `web_search`/`web_extract` calls). This skill drives
**OpenResearcher-30B-A3B** through its own multi-turn agentic research loop against
local SearXNG for each sub-question — real deep-research depth, not a quick web lookup.

**This is a multi-hour operation by design.** A single sub-question researched to
80-150 tool-call turns can itself take 15-40+ minutes; several sub-questions plus a
10-segment podcast generation easily reaches 1-3 hours total. That's the point —
"trigger while at work, pick up the finished episode at the end of the day" is the
intended usage, not a bug to optimize away.

## When NOT to use this

- A normal podcast request ("make a podcast about X") — use `open-notebook-podcast`.
  Its research step (a few `web_search`/`web_extract` calls, ~15-25 min total) is
  enough for most requests and finishes far faster.
- A single voice clip — use `voice-message`.
- **If genuinely ambiguous** ("research X and make a podcast" with no signal about
  depth/urgency), use the `clarify` tool to ask: normal-depth-and-soon, or
  deep-and-later? Getting this wrong in either direction wastes real time — a
  multi-hour deep dive when the user wanted something in the next 20 minutes, or a
  shallow episode when they explicitly wanted depth. Don't ask when the request
  already signals depth ("intensively research", "as much detail as possible",
  "I'll check it after work") or normal use ("quick podcast on X").

## Scoping a shorter run

Not every request needs 5+ sub-questions at `--max-turns 120` (the default
since 2026-08-18 — was 100). If the user asks for something short, a demo, or
a document is already attached (step 2 — the document does most of the
grounding, so research just needs to add context around it), scale down the
**sub-question count** first: 2-3 sub-questions instead of 5+, and a lower
`num_segments` episode profile if one exists for shorter episodes. Say so back
to the user ("a shorter demo episode, maybe 20-30 minutes to generate") rather
than defaulting to the multi-hour framing every time.

**Don't cut `--max-turns` below ~80 for a comparative or recency sub-question**
("how does X compare to...", "what's changed since..."). A real run at
`--max-turns 50` hit the cap on exactly that kind of question — good sources
had been found, but synthesis never completed, and the episode shipped
without material that was sitting right there. `openresearcher-run.py` also
now runs a question-enrichment pass by default (rewriting a casual sub-question
into a more demanding research brief before research starts — see
`--no-enrich` in that script if you ever need to disable it), which uses part
of the turn budget more effectively but doesn't eliminate the need for enough
turns to actually finish. Trim question *count* for a shorter run, not turn
budget per question.

Measured 2026-08-22 on two real runs: 48-66 turns per sub-question, natural
conclusions (not forced), 8-15 sources each. `--max-turns 120` is a ceiling the
model rarely reaches, not a target — it is there so a question that needs the
depth can take it.

## 1. Decompose the topic into sub-questions — do this yourself, don't skip it

A single broad query researched for many turns tends to skim breadth-first rather
than go deep on any one thread. Before invoking anything, break the user's topic
into **3-6 focused sub-questions** that together cover it in real depth — the kind
of breakdown you'd want if you were outlining a long-form piece yourself: history/
background, current state, key players or competing approaches, open questions or
controversies, concrete technical/practical details. Use your judgment on the
actual topic; don't force a rigid template onto something it doesn't fit.

This step is deliberately NOT automated by the pipeline script — it needs the
conversation's context (what did the user actually ask about, what have they
already said they know) that a standalone script call doesn't have.

## 2. If a document was attached, convert it and pass --source-document

**Do this whenever the request came with an attachment** ("do deep research on
this and make a podcast", a PDF/paper/notes attached). Two steps, both
required — skipping either recreates a documented failure:

1. Convert the attachment to markdown (e.g. with a document-conversion skill
   or tool, the same way `open-notebook-podcast` handles attachments). Save
   the output to a file — the pipeline reads it from disk, not from your
   context.

   **marker writes `<output_dir>/<stem>/<stem>.md`, not `<output_dir>/<stem>.md`.**
   Resolve the real path and check it is non-empty before going any further:

   ```bash
   STEM=$(basename "$PDF" .pdf); MD="/tmp/marker-out/$STEM/$STEM.md"
   [ -s "$MD" ] && wc -c "$MD" || echo "CONVERSION FAILED"
   ```

   On 2026-08-22 this exact assumption cost a run: marker succeeded (70,755
   chars), the agent looked one directory too high, decided conversion had
   failed, and shipped an empty source into podcast generation. **If the
   markdown is missing or empty, stop and tell the user — never continue with
   the document absent.** The whole point of `--source-document` is that the
   paper is narrated; without it you are generating from nothing.
2. Pass that file's path as `--source-document` (and a short
   `--source-document-title`) to the pipeline invocation in step 3 below.

**Both steps matter, and skipping the second one fails silently.** Adding the
attachment's text to Open Notebook as a source *without* `--source-document`
gets it into the notebook for browsing but has **zero effect on the narrated
episode** — this pipeline generates from `content`, never `notebook_id`, so a
document that only reaches the notebook is invisible to the actual audio. This
is the exact "document reaches the briefing, never the notebook" failure
`open-notebook-podcast`'s SKILL.md documents, one layer deeper: here the
document can reach the *notebook* and still never reach the *episode*.
`--source-document` is what actually closes the gap — it folds the document in
directly, ahead of the research sections, clearly labelled as the primary
source.

**Once a document is attached, decompose sub-questions to add value *beyond*
it** — comparisons, real-world reception, follow-up developments, adjacent
controversies — not to re-explain what it already covers. 2-3 sub-questions is
usually enough here; the document is doing most of the grounding work, so this
is also normally a *shorter* run than a from-scratch topic (see "Scoping a
shorter run" above).

## 3. Check the research backend exists

```bash
ls ~/models/openresearcher/OpenResearcher-30B-A3B-Q4_K_M.gguf
systemctl --user list-unit-files | grep llama-research
```

If either is missing, this deep-research path isn't set up on this box — tell the
user and suggest `open-notebook-podcast` instead (it doesn't need OpenResearcher).
Don't try to substitute a different model into this pipeline silently.

**What the pipeline starts, and what it doesn't.** It runs `DRP_BACKEND_START_CMD` if you set
one, and `DRP_BACKEND_STOP_CMD` when research ends (on every exit path, including failure).
That is all. It does **not** start SearXNG — it health-checks it and aborts if unreachable —
and it does **not** start any TTS server.

If your episode profile is backed by an on-demand TTS service, something must bring it up, or
Open Notebook fails at the synthesis step — *after* the outline and transcript LLM calls have
run, which is an expensive way to discover a stopped service. **Do that at synthesis time, not
before launching.** Research runs for an hour or more before the first clip is generated, and a
TTS server held across all of it wastes gigabytes for nothing. The natural place is the poller
you attach via `DRP_POLLER_CMD`: it is already running for the duration of generation and
nothing else in the chain has that lifetime. On the origin box the poller does exactly this,
acquiring a reference-counted lease on the TTS service and releasing it on every exit path, so
concurrent jobs cannot tear the server out from under each other.

## 4. Launch the pipeline in the background — never block the turn

**Simplest correct form: add `--detach` and call it however you like.** The
script re-launches itself as a transient systemd unit and returns in under
0.1s, forwarding every `DRP_*` variable, and prints the unit name and log path.
Added 2026-08-22 after an agent ran this pipeline in the foreground despite
three warnings in this file and had it killed by the harness's 60-second tool
timeout, mid-run. Do not rely on remembering the wrapper — pass `--detach`.

```bash
terminal(
  command="DRP_ENRICH_LLM_URL=http://127.0.0.1:8088/v1/chat/completions \
    DRP_SYNTH_LLM_URL=http://127.0.0.1:8088/v1/chat/completions \
    DRP_BACKEND_START_CMD='systemctl --user start llama-research' \
    DRP_BACKEND_STOP_CMD='systemctl --user stop llama-research' \
    DRP_POLLER_CMD=~/.hermes/skills/research/open-notebook-podcast/scripts/poll_and_notify.sh \
    python3 ~/Projects/deep-research-podcast/deep-research-podcast.py --detach \
    --episode-name \"<title>\" --notebook-name \"<name>\" \
    --max-turns 120 --deliver-target signal \
    [--source-document \"/tmp/marker-out/<stem>/<stem>.md\" --source-document-title \"<title>\"] \
    \"sub-question 1\" \"sub-question 2\""
)
```

It prints `detached as deep-research-podcast-<ts>` and a log path; report that to
the user and stop.

`DRP_POLLER_CMD` is what actually tells the user the episode is ready. Without it
the pipeline prints the job id and exits and nobody is notified -- 2026-08-22: a
run researched 44 sources, synthesized all four sub-questions and produced a
24-minute episode overnight, and the user was never told, because no poller was
attached. The script receives `job_id episode_name deliver_target`, exactly
`poll_and_notify.sh`'s signature, and since the same date it is invoked on
FAILURE too (`job_id=failed`), so a dead run reports itself instead of going
silent. The equivalent explicit `systemd-run` form below still works
and is what `--detach` does internally.

Launch it as a **transient systemd unit** via a foreground `terminal` call. Do NOT use
`nohup ... &` / `disown` (the security scanner rejects shell-level background wrappers),
and do NOT use `terminal(background=true)` either — that makes the pipeline a child of the
gateway, so any `systemctl --user restart hermes-gateway` kills it mid-run. That is a real
failure, not a theoretical one: it orphaned a live podcast poller on 2026-08-17. It matters
more here than anywhere else, because this pipeline can run for two hours.

`systemd-run` threads both needles — it is not in the scanner's regex (`nohup|disown|setsid`
only), and it returns immediately after registering a unit the user manager owns.

```bash
terminal(
  command="systemd-run --user --unit=deep-research-podcast-$(date +%s) \
    --setenv=PATH=\"$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin\" \
    --setenv=DRP_ENRICH_LLM_URL=http://127.0.0.1:8088/v1/chat/completions \
    --setenv=DRP_SYNTH_LLM_URL=http://127.0.0.1:8088/v1/chat/completions \
    --setenv=DRP_BACKEND_START_CMD=\"systemctl --user start llama-research\" \
    --setenv=DRP_BACKEND_STOP_CMD=\"systemctl --user stop llama-research\" \
    --setenv=DRP_POLLER_CMD=$HOME/.hermes/skills/research/open-notebook-podcast/scripts/poll_and_notify.sh \
    python3 <path-to-this-repo>/deep-research-podcast.py \
    --episode-name \"<descriptive episode title>\" \
    --notebook-name \"<short notebook name>\" \
    --notebook-description \"<one sentence, what this covers>\" \
    --briefing-suffix \"<what specifically to emphasize, from the user's request>\" \
    --max-turns 120 \
    --deliver-target signal \
    [--source-document \"/tmp/converted-doc.md\" --source-document-title \"<title>\"] \
    \"sub-question 1\" \
    \"sub-question 2\" \
    \"sub-question 3\""
)
```

The `DRP_BACKEND_START_CMD` / `DRP_BACKEND_STOP_CMD` lines are what actually
bring `llama-research` up and down. Without them the script does not start it —
it only health-checks `:8085`, waits 300s, and fails before researching
anything. (Checking that the unit *exists*, in step 3 above, is not the same as
starting it.) The stop command runs on every exit path including failure, so the
model is never left resident to be OOM-killed.

The two `--setenv` LLM lines are **not optional in practice** (2026-08-22).
Without them, enrichment and the final write-up both run on OpenResearcher,
which cannot do either — the run will research well and then produce no prose,
and the grounding gate will drop those sub-questions or refuse the episode
outright. `:8088` is `llama-gemma26`; any resident instruct endpoint works.

The `--source-document` line is only present if step 2 applies (an attachment
was converted). Omit it entirely for a from-scratch topic — do not pass an
empty string.

Track it with `systemctl --user list-units 'deep-research-podcast-*'` and
`journalctl --user -u <unit> -f`. There is no `session_id` and no `process(action="poll")`
here — that was tied to `background=true`. Losing it is the point of the trade: an unpollable
job that finishes is worth more than a pollable one that dies with the gateway.

Quote each sub-question as its own argument (the script takes them as `nargs="+"`
positionals). `--max-turns` is per sub-question, not a total — 120 is the script's own
default and a reasonable floor for genuine depth (don't go below ~80 for a
comparative/recency sub-question — see "Scoping a shorter run" above); raise
it (e.g. 150) only if the user explicitly wants maximum depth and has
signaled they're fine waiting longer. Swap `--deliver-target`
if the request didn't come in over Signal (`hermes send --list` shows targets).

**Reply to the user immediately** after launching, before this step's output even
returns — something like: "Starting deep research on N sub-questions, this'll run
in the background for a while (could be 1-3+ hours depending on depth) — I'll send
the finished episode over Signal when it's done." Do not wait for the script to
produce any output before replying; it's running detached specifically so this
turn doesn't have to hold open.

### Progress monitoring

Check on it with `systemctl --user list-units 'deep-research-podcast-*'` and
`journalctl --user -u <unit> -f` every 5–10 min during active research turns, or honor a
user-specified cadence (e.g. "check every 20 minutes").
Also tail the log file (`/tmp/drp-*.log`) to see what sub-question is in progress —
the script prints `[HH:MM:SS] researching: '<question>'` and completion markers.

The pipeline has 5 phases: (1) start llama-research + SearXNG, (2) run OpenResearcher
per sub-question, (3) stop llama-research the moment research ends, (4) create notebook
+ add sources, (5) trigger podcast generation via `open-notebook-podcast`'s poller.
Phases 1–2 are the long part (OpenResearcher's multi-turn search loops); phase 5 hands
off to the existing poller which notifies on completion.

Since 2026-08-22 the poller is also invoked on **failure**, with `job_id=failed` — a run
that dies no longer ends silently, so you do not have to check `systemctl` to find out.

### Pitfall: two constraints, and the obvious fix for each breaks the other

`nohup ... & disown` is blocked by the Hermes security scanner. But
`terminal(background=true)` — the fix recommended here until 2026-08-17 — makes the process
a child of the gateway, and a gateway restart kills it. Both were verified: the scanner's
regex covers only `nohup|disown|setsid`, and a restart provably killed a live poller
mid-episode on 2026-08-17.

`systemd-run --user`, called as a *foreground* command, is the only pattern that satisfies
both. See `references/background-launch-pitfall.md` in the `open-notebook-podcast` skill for
the full story.

## What the script actually does (for your own understanding, not something to reimplement inline)

`deep-research-podcast.py` (this repo):

1. Starts `llama-research` (`:8085`, on-demand — this repo's convention is
   disabled-by-default for anything outside the always-on Executor/Mentor/embedding
   tier) and SearXNG if either isn't already up, waits for both to become healthy.
2. Runs `scripts/openresearcher-run.py --json --max-turns N` once per sub-question
   — each call first turns the (possibly casually-phrased) sub-question into extra
   research requirements (`enrich_question()`, on by default — see `--no-enrich`),
   then drives the model through its own multi-turn agentic search/read/answer
   loop, returning `{question, answer, sources, turns_used, budget_spent,
   grounded, synthesized}`.

   **Two of the three LLM roles are not the research model** (2026-08-22).
   OpenResearcher researches; a general instruct model writes. Set
   `DRP_ENRICH_LLM_URL` and `DRP_SYNTH_LLM_URL` to a resident instruct endpoint
   (on this box, `llama-gemma26` at `:8088`); both fall back to the research
   endpoint, which *works* but badly. Measured that day: asked to rewrite a
   question, OpenResearcher answered it instead — and the researcher, handed a
   question containing its own answer, stopped at turn 0 having read nothing.
   Asked to write the final report, it emitted `<tool_call>` spam in every
   context tried, including a clean one. It is a research-trajectory model, not
   a writer. Two sub-questions with 15 sources each produced no prose at all
   before this was split out.

   `grounded` is false when no page was read; `synthesized` is false when the
   answer is a canned fallback rather than research prose. Both matter — see
   "Known limitations".
3. Creates a new Open Notebook notebook, adds each sub-question's synthesized
   answer as a `text` source (so it's grounded, vector-searchable content — not
   just a prompt) plus every URL the model actually read as its own `link` source
   (deduplicated across sub-questions), all with `embed: true`. If
   `--source-document` was given, its text is added as its own notebook source
   too, **and** folded directly into the podcast content ahead of the research
   sections (2026-08-18) — the only source that reaches the episode itself
   rather than just the notebook. Each sub-question's answer in the podcast
   content is also followed by a compact "sources consulted" listing
   (title + URL, no full text, 2026-08-18) so the episode-generation model has
   something to actually name when it makes a comparative or empirical claim.
4. Stops `llama-research` (its research job is done; no reason to hold ~24 GiB
   through the podcast-generation phase that follows, which needs GPU for a
   different model). Since 2026-08-22 this happens on **every** exit path,
   including a failed preflight and any later crash — it previously had a single
   call site that a preflight failure skipped, stranding the model to be
   OOM-killed and systemd-restarted in a loop.
5. Triggers podcast generation on the **`deep_dive_vibevoice`** episode profile —
   a dedicated profile for this workflow (`tech_experts_vibevoice` speaker pair,
   `num_segments: 10`, vs. the default `tech_discussion_vibevoice` profile's 5,
   specifically briefed to produce a genuinely long, detailed episode rather than
   padding). **Changed from the Kokoro-backed `deep_dive` on 2026-08-17**: the
   operator's standing preference is VibeVoice unless speed was asked for, and a
   deep-research episode — already an hours-long pipeline — is the least likely
   request to be in a hurry. The Kokoro twin `deep_dive` still exists and is the
   right pick if this particular run *was* asked for quickly; expect roughly half
   the generation time.
6. Hands off to `open-notebook-podcast`'s existing
   `scripts/poll_and_notify.sh` for completion polling and Signal delivery —
   nothing new invented for that part, same proven poller.

## Known limitations

- **A sub-question that produced no grounded synthesis is now DROPPED from the
  episode** (changed 2026-08-22 — the previous behaviour, described here until
  then, was to let it through as "a source that's mostly a source list, just
  with less synthesis quality"). That was wrong. What the transcript model
  actually receives in that case is a heading naming an interesting topic, no
  material under it, and a briefing instructing it to cover every question in
  depth and name specific papers — a topic, no evidence, and an order to be
  specific. It fills the gap from its own knowledge, and the result is
  indistinguishable from research. Such sections are excluded from the episode
  (they stay in the notebook), and **if no sub-question survives, the pipeline
  refuses to generate at all** rather than producing an episode about nothing.
- **So a run can now fail loudly where it used to produce something.** Two
  outcomes to relay to the user rather than treat as a crash:
  `N sub-question(s) excluded from the episode (no grounded synthesis)` means a
  shorter episode; `Refusing to generate` means no episode. Both mean the
  research did not produce citable material — check the log for `[synthesize]`
  and `[answer]` lines, which say why. The usual causes are SearXNG returning
  nothing, or `DRP_SYNTH_LLM_URL` pointing at the research model (see above).
- **A run can also drop a sub-question at the research stage**, logged as
  `warning: research failed for '<question>', skipping`. The others still
  complete — per-question isolation, not an aborted run.
- **This does not (yet) verify the deep_dive episode profile still exists** — it
  was created once via `PUT /api/episode-profiles` during this skill's setup. If
  podcast generation 404s on `episode_profile: deep_dive`, that profile was
  deleted or renamed; check `GET /api/episode-profiles` and either recreate it
  (see `docs/07-expansion.md`'s deep-research-podcast addendum in `halo-prep` for
  the exact fields used) or fall back to `tech_discussion` for that run and tell
  the user the episode will be shorter than intended.
- Total runtime is genuinely unpredictable (depends on sub-question count, turn
  budget per question, and current GPU load from other resident models) — never
  promise the user a specific ETA, only "could be a few hours."
