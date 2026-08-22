# deep-research-podcast

Point it at a topic. Walk away. Come back to a long, cited, multi-speaker podcast
episode — grounded in real research an agent did on its own, not a model
riffing from memory.

```bash
python3 deep-research-podcast.py \
    --episode-name "Score-driven volatility models" \
    --notebook-name "GAS models research" \
    --max-turns 120 \
    "What is the history and motivation behind score-driven (GAS) models?" \
    "How do GAS models compare to GARCH and stochastic volatility in practice?" \
    "What are the open controversies or unresolved questions in this area?"
```

Each sub-question gets its own 80–150-turn research agent (real web search,
real page reads, real citations — not one shallow pass), and the synthesized
answers become the podcast's source material. Before research starts, each
sub-question is also rewritten into a more demanding research brief (see
"Grounded, citable content by default" below) — real callers, whether a Hermes
skill decomposing a casual chat message or a human typing a quick question,
routinely hand this pipeline underspecified questions, and that's compensated
for in the script rather than left as an instruction someone might skip.

On a real 5-sub-question run this produced 43 cited sources and a 10-segment
episode; a smaller 2-question smoke test finished in 773.7s end to end. Both
are documented below, bugs included — this has run at real scale, not just in
a demo.

## How it works

Two scripts, no framework:

- **`openresearcher-run.py`** drives [OpenResearcher-30B-A3B](https://github.com/TIGER-AI-Lab/OpenResearcher)
  (a model *purpose-trained* on 100+ turn research trajectories, not a generic
  chat model prompted to behave like one) through its own native
  `browser.search` / `browser.open` / `browser.find` tool loop against
  [SearXNG](https://github.com/searxng/searxng), via any OpenAI-compatible chat
  endpoint — llama.cpp, vLLM, Ollama, whatever you're already running.
- **`deep-research-podcast.py`** runs several of those in sequence — one per
  sub-question, so depth doesn't get traded for breadth — then hands the
  synthesized answers to [Open Notebook](https://github.com/lfnovo/open-notebook)
  as grounded sources and triggers a multi-speaker episode.

Splitting a topic into sub-questions is the one step deliberately left to you
(or your agent's conversation context): a single long research call against
one broad question tends to skim breadth-first and never go deep on any one
thread, and deciding what actually needs covering is a judgment call the
pipeline can't make for you.

## Setup

You need three things running, none of them exotic:

1. **An OpenAI-compatible chat endpoint** serving OpenResearcher-30B-A3B (or
   another model that handles `tools`/`tool_choice` reasonably well).
   ```bash
   export DRP_LLM_URL=http://127.0.0.1:8085/v1/chat/completions
   ```
2. **A SearXNG instance** with JSON output enabled (`search: formats: [html,
   json]` in `settings.yml`) — no API key needed.
   ```bash
   export DRP_SEARXNG_URL=http://127.0.0.1:8888/search
   ```
3. **An Open Notebook instance**, with chat/embedding/TTS providers configured
   and one episode profile set up for this workflow — see "Open Notebook
   profile notes" below for the two settings that actually matter.
   ```bash
   export DRP_OPEN_NOTEBOOK_API=http://127.0.0.1:5055/api
   export DRP_EPISODE_PROFILE=deep_dive        # or whatever you name it
   export DRP_SPEAKER_PROFILE=tech_experts
   ```

Everything is env-var-driven with sane defaults, so if your setup happens to
land on `:8085`/`:8888`/`:5055` you don't need to set anything. If your LLM or
SearXNG are on-demand services rather than always-on, `DRP_BACKEND_START_CMD`
/ `DRP_BACKEND_STOP_CMD` will run whatever brings them up and down around the
research phase — both optional, both a no-op if unset.

### Open Notebook profile notes

Two things bite people the first time, both one-line fixes:

- **The episode profile must generate from a `content` string, not
  `notebook_id`.** A notebook with a handful of citation PDFs attached can
  easily exceed most models' context windows once every source's full text is
  pulled in (see "780,063 tokens" below) — the synthesized research answers
  are the actual point, and they're a few KB. This repo's scripts always pass
  `content` directly; just make sure your episode profile accepts that.
- **Set `max_tokens` on the profile explicitly.** Open Notebook's
  outline/transcript calls default to a 3000/5000-token cap unless the
  profile overrides it, which truncates a long episode's structured output
  mid-object. 12000 works well for a 10-segment episode.

Optionally keep a second, faster/lower-fidelity profile pair and pass `--fast`
(with `--fast-episode-profile`/`--fast-speaker-profile`) for runs where speed
beats voice quality.

## Usage

```bash
# One research question, human-readable output:
python3 openresearcher-run.py "What is SearXNG and what is it used for?"

# Same, machine-readable, for scripting:
python3 openresearcher-run.py --json --max-turns 100 "..."

# The full pipeline -- see the top of this README for the invocation.
```

`deep-research-podcast.py` blocks for the whole pipeline — realistically 30
minutes to a few hours for a multi-question deep dive — so run it under
`systemd-run --user`, `tmux`, `nohup`, or whatever your environment prefers
for a long background job. Set `DRP_POLLER_CMD` to a script that takes
`job_id episode_name deliver_target` if you want a notification when the
episode's ready; `hermes-skills/open-notebook-podcast/scripts/poll_and_notify.sh`
is a working example (Signal delivery), included for reference rather than as
a hard dependency.

### Grounding an episode in a document, not just research

`--source-document <path>` takes already-converted text (a PDF run through
`marker` or similar — this script does no conversion itself) and folds it into
the episode ahead of the research sections, clearly labelled as the primary
source. Unlike a plain notebook source, there is no path through this script
where the document is fetched but not actually narrated — it goes into
`build_podcast_content()` directly, not just into the notebook for browsing.
Use the sub-questions to add real value *beyond* the document — comparisons,
reception, follow-up developments — rather than restating what it already
covers:

```bash
python3 deep-research-podcast.py \
    --episode-name "SynthID-Text: watermarking LLM output at Gemini scale" \
    --notebook-name "SynthID-Text watermarking" \
    --source-document ./synthid-text.md \
    --source-document-title "SynthID-Text (Nature, 2024)" \
    --briefing-suffix "The source document is the paper itself -- use the research questions for context beyond it, not to re-explain what it already covers." \
    --max-turns 40 \
    "How does SynthID-Text's real-world Gemini deployment compare to how other AI labs have approached watermarking or provenance since 2024?" \
    "What has follow-up research found about watermark robustness against paraphrasing and other scrubbing attacks?"
```

## `hermes-skills/`

Two [Hermes Agent](https://hermes-agent.nousresearch.com) skill definitions
that wrap these scripts for an agentic assistant: deciding when a request
warrants this workflow versus a quicker one, decomposing a topic into
sub-questions from conversation context, launching the pipeline as a detached
unit so an agent-gateway restart can't kill a multi-hour job, and polling for
completion. Written for a specific agent harness — read them as a worked
example of the operational concerns (backgrounding a two-hour job without
losing it, notifying on completion, avoiding a duplicate job on a retried
tool call) rather than a drop-in for a different setup.

## Why not the upstream OpenResearcher harness

OpenResearcher ships as a *model* — 30B-A3B MoE, distilled from GPT-OSS-120B
driving native browser tools on 96K trajectories of 100+ turns each, 54.8% on
BrowseComp-Plus — plus a harness built for an 8×A100 vLLM deployment:
`pyproject.toml` hard-pins `vllm==0.13.0`, wants `pyserini` (Java 21 + Lucene),
`faiss-cpu`, `gpt-oss[all]`; `setup.sh` downloads a multi-GB benchmark corpus
and search index. None of that is needed to answer a question — the model
emits its `browser.*` tool schema correctly through any OpenAI-compatible
`tools`/`tool_choice` request (confirmed against a plain llama.cpp `--jinja`
server), so `openresearcher-run.py` is the missing ~150-line executor instead
of a port of `deploy_agent.py`.

## Why not just use a general model you already have running

The obvious objection to the section above: if the harness is only ~150 lines
of ordinary OpenAI `tools`/`tool_choice`, why keep a second 24 GiB model on
call at all? Point `DRP_LLM_URL` at whatever chat model is already resident and
delete `DRP_BACKEND_START_CMD`, the health wait, and the on-demand service with
it. It is a one-line change and it is the right instinct — the pipeline is
deliberately model-agnostic, so it deserved a real test rather than an appeal
to the model card.

Tested 2026-08-22 against **Gemma-4-26B-A4B**, a capable resident 26B-A4B
instruct model, on identical terms: same question, same harness, same SearXNG,
same `--max-turns 10`.

| | Gemma-4-26B-A4B | OpenResearcher-30B-A3B |
|---|---|---|
| `browser.search` calls | 3 | 6 |
| **`browser.open` calls** | **0** | **4** |
| Sources actually read | **0** | 2 |
| Turns used | 3 of 10 | 10 of 10 (budget exhausted) |
| Answer | 2,465 chars, from memory | 447 chars, grounded |

Gemma is not incapable of driving the loop — it emitted well-formed
`browser.search` calls with genuinely good queries, phrase-quoted and
progressively refined from general criticisms toward estimation and convergence
specifics. **It simply never opened anything.** Three searches, a look at the
result titles, and then it concluded it knew enough and wrote from memory.

The traces are the clearest way to see the difference:

```
Gemma:           search, search, search, [stop -> answer from memory]
OpenResearcher:  search, open(0), search, open(0,cursor=1), open(1,cursor=1),
                 search, open(0,cursor=2), search, search, search
```

OpenResearcher *interleaves*, and pages deeper into the same document with
`cursor:1`, `cursor:2`. That is what reading a source looks like. It also emits
`"topn":10` on every search — the argument the `TOOLS` comment in
`openresearcher-run.py` notes it was never prompted about. Gemma never uses it.
That is direct evidence the schema is trained in rather than prompt-followed.

**The dangerous part is that Gemma's answer is the longer and more fluent one.**
It is well-structured, plausible, and completely ungrounded. It would flow into
Open Notebook as a "researched source", become podcast narration, and nothing
downstream would reveal that no page was ever opened. A pipeline whose whole
premise is *research an agent actually did, not a model riffing from memory*
would quietly become the second thing while still looking like the first. An
outright failure would be safer, because it would be visible.

Two honest caveats:

- **10 turns understates OpenResearcher**, badly. Its design point is 80–150
  turns per sub-question (that is what produces the 43-source runs described
  below); at 10 it was still mid-investigation and got force-answered, hence
  the short result. The gap in a real run is wider, not narrower.
- **If you must drop the second model, change the harness, not the model.**
  Requiring N successful `browser.open` calls before `force_answer()` is
  permitted would compel a general model to read sources. That is coercing one
  model into behaviour another does natively, and its early-stopping instinct
  will fight you at every turn — but it is the honest version of the idea, and
  it would at least fail loudly rather than silently.

The conclusion is the boring one: the purpose-trained model earns its keep, and
`llama-research` stays on-demand. The real cost of that choice is not memory —
the service is not resident — it is `DRP_BACKEND_START_CMD` plus a health wait
measured at ~10 s. That is cheap for the property it protects.

## Hardened by a real production run

Two small smoke tests (2 sub-questions, 5–8 sources) passed cleanly first.
A real 5-sub-question run then surfaced three bugs — the kind that only show
up at genuine scale — and a later document-grounded run surfaced a fourth by
actually listening to the finished episode and checking it against the raw
research output. All four are fixed in this codebase now:

- **A 60-second, no-retry HTTP call could kill an otherwise-finished run.**
  All 5 sub-questions had already succeeded when a single `/sources/json`
  POST landed while Open Notebook was busy embedding a backlog of other
  sources and took longer than 60s — and a raw socket `TimeoutError` isn't an
  `HTTPError`, so it slipped past the error handling entirely. Now: a
  180s-per-attempt, 3-retry wrapper around every API call, *and* research
  results are written to disk the moment research finishes, before any Open
  Notebook call — so a crash after that point loses at most convenience,
  never the actual research.
- **A large notebook could blow the model's context window (780,063 tokens
  against a 131,072-token budget).** Pulling in every cited source's full
  text was never the goal — the synthesized answers are. Podcast generation
  now runs off those directly (see "Open Notebook profile notes" above); the
  notebook still keeps every source for browsing/citation.
- **The model could report a broken run as a clean success.** On rare
  natural (non-forced) stops it could return an empty answer or rambling,
  repeated planning text, and the pipeline reported both as if synthesis had
  worked. Every stop — forced or natural — now routes through the same
  answer-validation path: a real answer, or an honest "no synthesis, here are
  the sources" note. Never unvalidated raw output.
- **Prose repetition slipped past the fix above, wearing a disguise it didn't
  anticipate.** A "natural conclusion" (`budget_spent: False`) turned out to
  be a decent opening paragraph followed by ~25 near-identical sentences —
  "Use the arxiv (source 0) for detection." on repeat, while the model tried
  to *plan* citations instead of writing them. `stop=["<tool_call>"]` catches
  repeated tool-call *syntax*; it doesn't catch a repeated *prose sentence*,
  and "is `content` non-empty" was the only other check. It never reached the
  actual episode audio only because Open Notebook's own transcript model
  happened to filter the noise out while writing dialogue from it — luck, not
  this script working as designed. Fixed with a sentence-level dedup check
  (this repetition lands inside one paragraph, not across separate lines, so
  a naive line-based check would miss it): 4+ verbatim sentence repeats now
  routes to the same honest fallback as empty content.

Full technical detail on each is in the code's own comments (`api()` in
`deep-research-podcast.py`, `force_answer()`/`_is_degenerate()` in
`openresearcher-run.py`).

## Grounded, citable content by default

Fixing the four bugs above got a real episode generating reliably end to end.
Listening to that episode and cross-checking its transcript against the raw
research output surfaced a softer problem: a comparative sub-question
("how does this compare to what other labs have done") never actually named a
competing lab, system, or paper — it just re-compared against the same
baselines already inside the source material. Not a bug (the prose was fluent
and correctly attributed to real research), just weak on the thing that
research was supposed to add. Three changes address that directly:

- **`--max-turns` default raised 100 → 120.** The sub-question above ran at
  `--max-turns 50` (deliberately scoped down for a short demo) and hit the cap
  with good sources already found but no synthesis. Trim sub-question *count*
  for a shorter run, not turn budget per question — don't go below ~80 for a
  comparative/recency sub-question specifically.
- **`enrich_question()`, a new pre-research step in `openresearcher-run.py`,
  on by default.** One extra LLM call rewrites the input question — which in
  real usage is often a casual chat message decomposed by an agent skill, not
  a careful research brief — into a more demanding one: name specific
  papers/systems/organizations/dates for comparative or recency questions,
  and explicitly forbid treating already-known background as if restating it
  were a new finding. The main research system prompt itself now also
  requires inline attribution (name the source behind a claim in the sentence
  making it, not only in a trailing URL list). Pass `--no-enrich` if your
  question is already a precise brief — the extra call has no upside there.
  Best-effort: a failed enrichment call falls back to the original question.
- **Podcast content now carries a compact "sources consulted" listing per
  sub-question** (title + URL only, a handful of lines — nowhere near the
  raw-full-text scale that caused the 780,063-token failure above), so the
  episode-generation model has something to actually name.

This doesn't guarantee a future comparative sub-question resolves cleanly —
it changes what "a natural conclusion" and "a good episode" are asked to look
like, not what the model is capable of on a given day.

## A fifth bug, found the same day it was supposed to be tested

The very next production run surfaced this directly: sub-question 1 finished
cleanly (6 sources, natural conclusion, 81 turns — more turns than an earlier
un-enriched run's 49, consistent with `enrich_question()` pushing for more
demanding specifics), but sub-question 2's first `browser.search` call hit an
uncaught SearXNG `HTTPError: 400` and **crashed `openresearcher-run.py`
outright** — `tool_search()` was the one tool-dispatch function that never got
`tool_open()`'s exception handling. Worse, `deep-research-podcast.py` built
every sub-question's result in one bare list comprehension with no
per-question isolation, so that crash also discarded sub-question 1's
already-completed research — nothing had been appended anywhere yet. Same
failure class as the `api()`-retry fix above ("completed work destroyed by a
later failure"), in a spot that fix never covered. And because the process
exited before ever reaching the poller handoff, **no notification — success
or failure — was ever sent**; the only way to know it died was to check
`systemctl` directly.

Fixed: `tool_search()` now catches search failures the same way `tool_open()`
already catches fetch failures, returning `"Search failed: ..."` as tool
output instead of crashing. `deep-research-podcast.py`'s research loop now
appends each sub-question's result as it completes instead of building the
whole list in one comprehension — a later failure costs only that
sub-question, logged and skipped, not the ones that already succeeded. If
every sub-question fails, the pipeline still raises rather than trying to
build an episode from nothing.

## License

MIT — see `LICENSE`.
