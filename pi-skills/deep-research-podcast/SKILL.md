---
name: deep-research-podcast
description: Deep, multi-hour research with OpenResearcher-30B fed into a long Open Notebook podcast, delivered on Signal. Use when the user asks for deep research, a deep dive, an intensive or thorough investigation, or says they will listen later; also to ground a long episode in an attached document. For an ordinary "podcast about X" use open-notebook-podcast.
---

# Deep research podcast: trigger and forget

Ported from the Hermes skill on 2026-09-24 (halo-prep `docs/13` Phase E; the original, with
every incident behind these rules, is `git show 5aa8f04:config/hermes/skills/research/deep-research-podcast/SKILL.md`).
The pipeline is `~/Projects/local-podcast-studio/deep-research-podcast.py` (this skill's own
repo; halo-prep's older `scripts/` copy was deleted 2026-10-09). `scripts/launch.sh` here wires it for this
box and detaches it; always launch through it.

**A run takes 1-3+ hours by design**: OpenResearcher researches each sub-question for up to
120 tool-call turns (48-66 was typical, 2026-08-22), then a 10-segment episode is generated.

## When not to use it

- "Make a podcast about X" with no signal about depth: `open-notebook-podcast` (~20-60 min).
- If it is genuinely unclear whether they want deep-and-later or normal-and-soon, ask one
  short question and end the turn. Don't ask when the wording already says ("intensively",
  "as much detail as possible", "I'll check after work", "quick podcast").

## 1. Decompose the topic yourself: 3-6 sub-questions

A single broad question researched for many turns skims. Break the request into focused
sub-questions that together cover it: background, current state, competing approaches or key
players, open controversies, concrete technical detail. Fit them to the topic; no template.
For a short or demo run, use **fewer questions** (2-3), never fewer turns: a comparative
question at `--max-turns 50` found good sources and ran out before synthesising (2026-08-18).

## 2. An attached document: convert, then --source-document

Convert it with the `ocr-and-documents` skill (marker; takes 3-20 min; tell the user first)
and pass the verified markdown path as `--source-document` with a short
`--source-document-title`. This is the only way a document reaches the *episode*: the pipeline
generates from content, not from the notebook, so adding the document to a notebook some other
way has no effect on the audio. The pipeline re-verifies marker provenance and refuses to start
without it. If conversion fails, stop and tell the user; never transcribe it yourself.

With a document, write sub-questions that add to it (reception, comparisons, later
developments) rather than re-explain it; 2-3 is usually enough.

## 3. Launch

```bash
~/.pi/agent/skills/deep-research-podcast/scripts/launch.sh \
  --episode-name "<descriptive title>" --notebook-name "<short name>" \
  --notebook-description "<one sentence>" \
  --briefing-suffix "<what to emphasise, from the request>" \
  [--source-document "/path/<stem>.md" --source-document-title "<title>"] \
  "sub-question 1" "sub-question 2" "sub-question 3"
```

It returns at once and prints the unit name (`deep-research-podcast-<ts>`) and a log path.
Quote each sub-question as its own argument; they are required. `--max-turns` defaults to 120
per question; raise it only if the user explicitly wants maximum depth. The launcher fixes the
profile pair to `deep_dive`/`tech_experts` (Kokoro) and refuses to start if OpenResearcher's
weights or SearXNG are missing.

## 4. Reply and stop

One message: starting deep research on N sub-questions (list them briefly), it runs in the
background for a few hours, and the episode arrives on Signal when done. Never promise an ETA.
The poller also reports a failed run, so no one has to watch it.

## If asked for progress

Pin to this run's unit; never glob `deep-research-podcast-*` (other runs pollute the counts).
The run's output goes to the log file `launch.sh` printed, `/tmp/deep-research-podcast-<ts>.log`,
**not** the journal (which holds only start and stop lines). The poller's lines land there too:

```bash
L=/tmp/deep-research-podcast-<ts>.log
grep -c 'researching:' "$L"               # sub-questions started
grep -c 'skipping this sub-question' "$L"  # dropped for no grounded synthesis
grep -E 'job_id|Refusing|excluded|persisted|\[poll\]' "$L" | tail
```

`ps -eo args | grep -F 'openresearcher-run.py --json'` shows a live research child (`pgrep` by
name misses it: the comm field is truncated to 15 characters).

## Outcomes to relay, not treat as crashes

- `N sub-question(s) excluded from the episode (no grounded synthesis)`: a shorter episode.
  Ungrounded sections are dropped on purpose; a topic heading with no evidence under it gets
  filled from the model's own knowledge and sounds exactly like research.
- `Refusing to generate`: no episode, because nothing citable was found. Usual causes are
  SearXNG returning nothing or the writer URL pointing at the research model (the launcher
  sets it correctly).

## The research finished but the episode failed

The poller's Signal message says `failed to generate`, and the log has a
`research results persisted to /tmp/drp-results-<ts>.json` line. **Do not redo the research,
and never re-trigger by hand** (`POST /api/podcasts/generate`, or `make_podcast.py` on the run's
notebook). Both generate from `notebook_id`, not from the research. When Open Notebook cannot
read a notebook it silently uses the text `Notebook ID: <id>` as the whole content, and the
result is an invented episode that sounds like research (2026-09-26). Resume through the
launcher, with the same episode name, briefing and `--source-document` as the original:

```bash
~/.pi/agent/skills/deep-research-podcast/scripts/launch.sh \
  --episode-name "<same title>" --briefing-suffix "<same briefing>" \
  [--source-document "/path/<stem>.md" --source-document-title "<title>"] \
  --from-results /tmp/drp-results-<ts>.json
```

No sub-questions and no notebook name: both come from the first run. It takes about 40 min
(outline, transcripts, Kokoro audio) and reports through the poller like a full run. The
results file is in `/tmp`, so it does not survive a reboot. A malformed transcript segment is
retried by Open Notebook now, so a second failure means something else is wrong. Report it
and stop rather than resume again.
