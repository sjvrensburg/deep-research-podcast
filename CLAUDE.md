# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two standalone Python 3 scripts (no packaging, no dependencies beyond the stdlib, no
test suite, no linter config) that turn a topic into a long, cited, multi-speaker
podcast episode. `README.md` is the primary design document and is unusually detailed —
read it before changing behaviour.

- `openresearcher-run.py` — the executor for OpenResearcher-30B-A3B's native
  `browser.search` / `browser.open` / `browser.find` tool loop, against SearXNG, via any
  OpenAI-compatible `/v1/chat/completions` endpoint. Runs one research question.
- `deep-research-podcast.py` — the pipeline: spawns `openresearcher-run.py --json` once
  per sub-question via `subprocess`, then builds an Open Notebook notebook and triggers
  episode generation. Blocks for 30 min – several hours.
- `hermes-skills/` — Hermes Agent skill definitions that wrap the pipeline. Reference
  material for the operational concerns (detached launch, polling, no duplicate jobs),
  not code the scripts import.

## Running

Everything is env-var driven; the defaults assume a local stack.

```bash
export DRP_LLM_URL=http://127.0.0.1:8085/v1/chat/completions
export DRP_SEARXNG_URL=http://127.0.0.1:8888/search
export DRP_OPEN_NOTEBOOK_API=http://127.0.0.1:5055/api
export DRP_EPISODE_PROFILE=deep_dive
export DRP_SPEAKER_PROFILE=tech_experts

# Fastest way to exercise the research half alone:
python3 openresearcher-run.py --max-turns 10 "What is SearXNG used for?"
python3 openresearcher-run.py --json --max-turns 100 "..."     # machine-readable

# Full pipeline (long-running; run detached, e.g. under systemd-run --user):
python3 deep-research-podcast.py --episode-name "..." --notebook-name "..." \
    --max-turns 120 "sub-question 1" "sub-question 2"
```

Other env vars: `DRP_LLM_HEALTH_URL`, `DRP_BACKEND_START_CMD` / `DRP_BACKEND_STOP_CMD`
(bring an on-demand LLM/SearXNG service up and down around the research phase — no-ops
if unset), `DRP_POLLER_CMD` (notification handoff, now fired on failure too),
`DRP_OPENRESEARCHER_PATH`, `DRP_FAST_EPISODE_PROFILE` / `DRP_FAST_SPEAKER_PROFILE`. Guard
rails, all with working defaults: `DRP_KEEP_TOOL_RESULTS`, `DRP_ANSWER_MAX_TOKENS`,
`DRP_RESEARCH_TIMEOUT`, `DRP_POLLER_TIMEOUT`, `DRP_RESULTS_DIR`,
`DRP_ENRICH_LLM_URL`, `DRP_ENRICH_MAX_TOKENS`, `DRP_SYNTH_LLM_URL`,
`DRP_SOURCE_TEXT_PER_PAGE`, `DRP_SOURCE_TEXT_TOTAL`, `DRP_THIN_SECTION_CHARS`,
`DRP_DOC_VERIFY_CMD`
(run as `CMD <path>` against `--source-document`; the run aborts unless it exits 0,
and also if it is missing or hangs -- the gate fails closed on purpose).

Local stack (verified 2026-08-22): SearXNG and Open Notebook run persistently;
`llama-research.service` (OpenResearcher, port 8085, `--ctx-size 131072`) is on-demand
via `systemctl --user start llama-research`; `llama-ornith35` on :8088 is the writer for both
`DRP_ENRICH_LLM_URL` and `DRP_SYNTH_LLM_URL` (it replaced `llama-gemma26` on that port on
2026-08-25 — the port was kept across the swap, so neither env var changed). The box runs several resident llama-servers and is near its memory
ceiling — starting `llama-research` alongside them has been OOM-killed, so free memory
before a long run.

There is a verification harness in the session scratchpad (81 checks over the changed
logic, stubbed I/O, no live services) — recreate it rather than trusting a green
compile. But the last three bugs were all invisible to code reading and to that
harness; they showed up only in a live run's log. Verification is a real run: a 2-sub-question smoke test at reduced
`--max-turns` for the pipeline, or a single `openresearcher-run.py` call for anything in
the research half. Watch stderr — `openresearcher-run.py` logs each turn's tool call
there, and `force_answer()` / `enrich_question()` print why a fallback fired.

## Architecture and invariants

The interface between the two scripts is **one JSON object on the last line of
`openresearcher-run.py --json`'s stdout**: `{question, answer, sources, turns_used,
budget_spent, grounded, synthesized}` (each source carries `used`: whether its text
reached the writer, or was cut by `DRP_SOURCE_TEXT_TOTAL`) (plus `researched_as` when enrichment rewrote the
question). Anything else printed must go to stderr, or `run_research()` breaks.
`grounded` is false when no source was read; `synthesized` is false when `answer` is
`force_answer()`'s canned fallback rather than research prose. The pipeline gates on both
— see the grounding invariant below.

`SOURCES` (module-global, insertion-ordered `url -> title`) is populated only by
`tool_open()` — deliberately not by search hits — so "sources" means pages actually read.
This is the metric the README's Gemma-vs-OpenResearcher comparison turns on. (That comparison
is a 2026-08-22 measurement and stays attributed to Gemma; :8088 has served
Ornith-1.5-35B-A3B since 2026-08-25 and the comparison has not been re-run against it.)

**The single most important property of the whole repo: an ungrounded but fluent answer
must never pass silently as research.** Several guards exist for exactly this, each added
after a real failure, each documented in a long code comment naming the run that produced
it. Do not simplify these away:

- `_strip_reasoning()` handles both reasoning shapes (inline `</think>` in `content`, vs.
  out-of-band `reasoning_content`). Getting this wrong silently discards real answers.
- Every stop — forced budget *and* natural (no tool calls) — routes through
  `force_answer()`, so both get the same anti-degeneration prefill + `stop=["<tool_call>"]`.
- `_is_degenerate()` catches sentence-level prose repetition (`stop` only catches
  tool-call syntax). Sentence-split, not line-split, on purpose.
- When synthesis genuinely fails, the fallback is an honest "no synthesis, here are the
  sources" string — never unvalidated raw model output.
- **Three LLM roles, and only one of them is the research model.** OpenResearcher
  researches; a general instruct model both enriches the question
  (`DRP_ENRICH_LLM_URL`) and writes the final report (`DRP_SYNTH_LLM_URL`). Both
  default to `DRP_LLM_URL` and both are measurably wrong there: it answers questions
  it was asked to rewrite, and emits `<tool_call>` spam instead of prose in every
  context tried, including a clean one with no trajectory. Do not "simplify" these
  back to one endpoint.
- `resolve_answer()` is the answer cascade: grounding gate → the model's own
  concluding turn if `_usable_answer()` accepts it → `synthesize()` on the writer →
  `force_answer()` → honest source listing. `force_answer()`'s prefill lands inside
  the model's `<think>` block, so it usually fails here; it is kept for
  single-endpoint setups.
- `synthesize()` writes from `SOURCE_TEXT` — the page text actually served to the
  researcher. `tool_open()` must retain that text *before* any early return, or a
  page counts as read while contributing nothing to the write-up.
- `enrich_question()` (on by default, `--no-enrich` to skip) turns an underspecified
  question into extra requirements. OpenResearcher cannot do meta-work about a research question:
  measured, it either reasons past the token cap, emits `<tool_call>` spam, or answers
  the question inside the brief — and that last one made the researcher stop at turn 0
  with zero sources. The brief is *attached* to the question as "Research requirements:",
  never substituted for it, and every failure mode falls back to the original question
  with a reason on stderr.
- **Nothing may reach the episode that no source backs.** `force_answer()` refuses to
  return model prose when `SOURCES` is empty; `run_research()` rejects an ungrounded
  result; `build_podcast_content()` excludes sections without a grounded synthesis and
  raises if nothing usable is left. A sourceless section handed to the transcript model
  is a topic with no evidence under a briefing demanding specifics — a fabrication
  generator, not a thin section.
- `verify_source_document()` gates `--source-document` behind `DRP_DOC_VERIFY_CMD`
  when one is set. Three times in two days the converted document at the path
  everything downstream reads was not what it claimed to be -- twice `pdftotext`
  output in the converter's own output path, once an agent's own transcription
  after it wrongly decided the converter had stalled. That last shape defeats every
  content-based check: an LLM transcription has headings, tables and math, and can
  silently alter an equation in a document the episode then narrates as the user's
  paper. Each earlier fix was an instruction in a skill file; each was followed by
  an agent that did not follow it. It is a command rather than a built-in rule
  because this script does not convert documents and must not assume a converter.
- **"Read" and "written from" are different claims, and the episode must make the
  weaker one.** `synthesize()` records which URLs actually reached the writer;
  `build_podcast_content()` lists those as "sources consulted" and the rest under
  "also read, but not part of the write-up". The cap cut 14 of 24 on one real
  question while all 24 were cited.
- **A thin section is marked, never dropped.** Below `DRP_THIN_SECTION_CHARS` the
  section carries a note telling the narrators to cover it briefly and not expand it.
  On a topic months old there may genuinely be little to say, and the writer is told
  to say so rather than fill the gap -- but the global briefing demands "real depth"
  and specifics, which aimed at a two-sentence section is an instruction to invent.
  Dropping such a section instead would hide a real finding and reward padding.
- `tool_open()` banks a URL in `SOURCES` only after a text content-type *and* at least
  `MIN_SOURCE_CHARS` of extracted text. A PDF or cookie wall that fetches fine is not a
  source; counting it produces a citation list for pages nothing read.
- `cursor` on `browser.open` is a **page ordinal** (`_page_offset()`), because that is
  what the model emits. Values ≥ `PAGE_CHARS` are still honoured as byte offsets.

**The conversation must stay inside the context window.** `_prune_history()` runs before
every `chat()` call: system prompt and the last `KEEP_VERBATIM_TOOL_RESULTS` tool results
verbatim, older tool-result *contents* replaced by a stub. Never drop the messages
themselves — an assistant turn with `tool_calls` must be followed by one `tool` message
per call. Unpruned, a 120-turn run needs ~207k tokens against a 131k window and cannot
finish; worse, silent context-shift drops the system prompt and the run goes ungrounded.

**Completed work must survive a later failure.** This class of bug has recurred five
times. Concretely: `chat()` and `api()` both retry transient failures and 5xx/429 and
fail fast on other 4xx; every Open Notebook `POST` is individually guarded, as is
`build_notebook()` as a whole (the episode does not need the notebook); the research loop
appends each result as it completes (never a list comprehension) and skips failed
sub-questions; results are persisted to `RESULTS_DIR` *before* any Open Notebook call,
and that write is itself guarded; a `chat()` that finally fails inside the research loop
ends the loop and answers from what was already read rather than raising (a real run lost
a sub-question this way, and the same failure at turn 90 would have discarded 90 turns);
`chat()` also escalates `max_tokens` when the server rejects the model's own tool call as
truncated, since retrying that verbatim reproduces it; tool dispatch functions return an error *string* as
tool output rather than raising, and the dispatch *call site* is guarded too (the
arguments are model output). SIGTERM/SIGHUP raise `SystemExit` and the top-level
handler catches `BaseException`, so `systemctl --user stop` on a detached run still
releases the backend and notifies. Every exit path notifies — `notify()` is called from the
top-level handler with `job_id=failed`, not only on success — and every exit path
releases the research backend. `release_research_backend()` is idempotent and gated on
`RUN["backend_started"]` (set before `DRP_BACKEND_START_CMD` runs), so a preflight that
starts the model and then fails its own health checks cannot strand a multi-GB
on-demand service: on a memory-tight box that gets OOM-killed and systemd-restarted in a
loop.

**Podcast generation posts `content`, never `notebook_id`.** A real run's notebook of
full-text citation PDFs came to 780,063 tokens against a 131,072-token window. The
notebook still gets every source for browsing; `build_podcast_content()` sends only the
synthesized answers plus a compact title+URL "sources consulted" listing per question.
The episode profile must also set `max_tokens` explicitly (~12000) or long structured
output truncates mid-object.

Other things that are the way they are on purpose: the `TOOLS` schema matches what the
model was post-trained on (including `topn`), not what we'd design; `_text()` is
dependency-free regex HTML-stripping rather than trafilatura; sub-question decomposition
is the caller's job, not the pipeline's; `--max-turns` defaults to 120 and shouldn't drop
below ~80 for comparative/recency questions — trim question *count* for a shorter run.

## Conventions

Stdlib only — do not add a dependency or a `requirements.txt`. Both scripts call `main()`
at module scope (no `if __name__` guard). Non-obvious decisions are recorded as dated
code comments naming the run that motivated them; match that style, and mirror anything
user-facing into the README, which documents each bug as a narrative.
