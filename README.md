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
1b. **A general instruct model** for the two jobs the research model cannot do:
   rewriting the question into a brief, and writing the final report. Any
   OpenAI-compatible endpoint; it can be a model you already have resident.
   ```bash
   export DRP_ENRICH_LLM_URL=http://127.0.0.1:8088/v1/chat/completions
   export DRP_SYNTH_LLM_URL=http://127.0.0.1:8088/v1/chat/completions
   ```
   Both default to `DRP_LLM_URL`. That *runs*, but OpenResearcher answers the
   question instead of rewriting it, and emits `<tool_call>` spam instead of a
   report — so the pipeline researches well and then produces nothing, and the
   grounding gate refuses the episode. See "The seventh and eighth bugs" below
   for the measurements. If you only set one thing beyond the three below, set
   these.

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

### Tuning knobs you should not normally need

All optional, all with working defaults — listed because each one exists to
prevent a specific failure documented at the bottom of this README:
`DRP_KEEP_TOOL_RESULTS` (8) how many recent tool results stay verbatim in the
research conversation, `DRP_ANSWER_MAX_TOKENS` (4000) the budget for the
synthesized answer itself, `DRP_RESEARCH_TIMEOUT` (10800s) the ceiling on one
sub-question's research subprocess, `DRP_POLLER_TIMEOUT` (14400s),
`DRP_ENRICH_MAX_TOKENS` (4000), and `DRP_RESULTS_DIR` (`/tmp`) where raw
research JSON is persisted before any Open Notebook call.

`DRP_ENRICH_LLM_URL` and `DRP_SYNTH_LLM_URL` are the two worth setting
deliberately — see step 1b above. `DRP_SOURCE_TEXT_PER_PAGE` (6000) and
`DRP_SOURCE_TEXT_TOTAL` (60000) bound how much of each read page is kept for the
write-up; raise the total if your writer's context allows and you routinely read
more than ten sources per sub-question (the run log says when it truncates).

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

A `--briefing-suffix` you pass replaces the default coverage instruction but
never the anti-fabrication clause — that one is appended to every briefing, so
a custom suffix cannot accidentally drop it.

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

Tested 2026-08-22 against **Gemma-4-26B-A4B**, at the time the resident 26B-A4B
instruct model on `:8088`, on identical terms: same question, same harness, same
SearXNG, same `--max-turns 10`.

Everything below is that 2026-08-22 measurement and is left attributed to Gemma.
**`:8088` has served Ornith-1.5-35B-A3B since 2026-08-25** and this comparison has
**not** been re-run against it — so treat the Gemma column as evidence about
general instruct models driving a research loop, which is the point it was making,
and not as a live description of what is on that port today.

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
  it would at least fail loudly rather than silently. Since 2026-08-22 the
  first half of that is implemented: not N opens, but *at least one* — a run
  that read nothing now refuses to answer instead of quietly returning a
  memory-based one (see "A code review, and the two failure modes it found").
  Point `DRP_LLM_URL` at a general model and you will get that loud failure
  rather than the silent one described above; that is an improvement, not a
  substitute for the purpose-trained model.

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

## A code review, and the two failure modes it found

Two symptoms kept recurring after the fixes above: runs that never finished,
and episodes whose content was plausible but not actually traceable to
anything the researcher read. A full read of both scripts on 2026-08-22 found
that each symptom had one dominant cause and a tail of smaller ones. All are
fixed on this branch.

### Why runs did not finish

**The conversation could not fit in the context window, and nothing pruned
it.** Every tool result was kept verbatim at up to 6000 characters, plus each
turn's reasoning, and nothing was ever elided. The arithmetic never worked:

| turns | approximate context |
|---|---|
| 40 | 69,000 tokens |
| 80 | **138,000 tokens** |
| 120 | 207,000 tokens |

against a `DEFAULT_MAX_TURNS` of 120 and a 131,072-token window. A run
physically could not reach its own turn budget. Somewhere around turn 60–80 the
server either rejected the over-long request — an uncaught `HTTPError` that
killed the process and the whole sub-question with it — or, with context
shifting on, silently dropped the *head* of the conversation: the system prompt
demanding named inline attribution, followed by the earliest sources read. That
second outcome is also a direct cause of the grounding problem below, which is
why the two symptoms kept appearing together. `_prune_history()` now keeps the
system prompt and the most recent `DRP_KEEP_TOOL_RESULTS` (default 8) tool
results verbatim and replaces older ones with a one-line stub naming the URL.
The messages themselves stay — an assistant turn with `tool_calls` must still
be followed by one `tool` message per call — and nothing citable is lost,
because `SOURCES` already holds every URL read. Measured: a 200-turn run now
settles at ~53,000 tokens instead of growing without bound.

**`chat()` was the only fatal network call in the repo.** Every other one
(`_get`, `tool_search`, `api()`) already degraded; this one, the most
frequently called, had no handler at all, so one 503 during a service reload
destroyed 90 turns of research. It now retries transient failures and 5xx/429,
fails fast on other 4xx, and surfaces the server's actual message.

**The per-sub-question subprocess timeout sat inside the expected runtime.**
3600s, against a 120-turn budget that realistically takes 40–60 minutes before
enrichment and page fetches — so it fired on healthy long runs and destroyed
the sub-question, since the child's answer only existed on its stdout at exit.
Now `DRP_RESEARCH_TIMEOUT`, default 10800s.

Smaller ones, same class:

- **A notebook failure killed the episode it wasn't needed for.**
  `build_notebook()` was called unguarded, and `POST /notebooks` inside it was
  the one API call without its own guard — so a notebook problem aborted the
  run before `trigger_podcast()`, even though `build_podcast_content()` takes
  the results directly. Now warned and skipped.
- **There was still no failure notification.** `DRP_POLLER_CMD` fired only
  after a successful trigger, and `main()` was called bare at module scope, so
  every failure ended as a silent dead unit — the exact gap the fifth bug
  identified and only half-closed. A top-level handler now notifies with
  `job_id=failed` before re-raising.
- **The SearXNG health check used the User-Agent SearXNG rejects.**
  `wait_healthy()` called `urlopen` bare, with the default python-urllib agent
  that `_get()` sets a browser string specifically to work around — so the
  preflight could fail against an instance the research loop would have queried
  fine, aborting before any work. Same agent both places now, and SearXNG gets
  6 tries instead of 1 (it was given no time to start after
  `DRP_BACKEND_START_CMD`).
- **`api()` retried timeouts but not 5xx**, though a 502/503 from a busy Open
  Notebook is the same transient overload the timeout retry was added for.
- **The results-persistence write was itself unguarded** — the one line whose
  purpose is "a crash after here loses nothing".

### Why episodes drifted back to the model's own knowledge

**Nothing, anywhere, required that a single source had been read.** `SOURCES`
was correctly populated only by a successful `browser.open`, and `--json`
reported it — but it was never *checked*, only logged. A run that searched,
opened nothing, and wrote a fluent answer from memory reported
`budget_spent: False`: a natural conclusion, the most successful-looking
outcome the script can emit. That is precisely the measured Gemma behaviour in
the section above, and nothing downstream could tell it from real research.
`force_answer()` now refuses to return model prose when no source was read, and
`--json` carries an explicit `grounded` flag that `run_research()` gates on.

**Paging into a document was a no-op.** The model pages with small ordinals —
this README's own captured trace is `open(0,cursor=1)`, `open(0,cursor=2)` —
but `tool_open()` sliced `page[cursor:cursor+4000]`, so `cursor=1` returned the
same opening 4000 characters shifted by *one character*. The single behaviour
that most distinguishes this model from a general one skimming result titles
was silently doing nothing, and a model that believes it has read a source to
the end and has actually re-read its first paragraph three times falls back on
what it already knows. `cursor` is now a page ordinal (values ≥ `PAGE_CHARS`
are still honoured as byte offsets, since the old tool output taught the model
that convention too), and the tool description says which it is.

**A failed sub-question still became an episode section.** `## {question}`
followed by the canned "did not produce a synthesized answer" string, handed to
a transcript model under a briefing instructing it to cover every question in
real depth and name specific papers and organizations. A topic, no evidence,
and an order to be specific is a fabrication generator. Sections without a
grounded synthesis are now excluded from the episode entirely (they stay in the
notebook), and if nothing usable remains the pipeline refuses to generate
rather than producing an episode about nothing.

**The synthesized answer — the whole product of an 80-turn run — was capped at
1200 tokens shared with reasoning**, the budget sized for "emit one tool call",
and `finish_reason` was never inspected, so an answer cut off mid-sentence was
accepted as complete. Consistent with the 216–2114 characters per answer
measured above. The answer call now gets `DRP_ANSWER_MAX_TOKENS` (default 4000)
and flags truncation in-band rather than discarding real synthesis.

**Any HTTP 200 counted as a source read.** No content-type check, no minimum
length. An arXiv PDF — most of what this pipeline chases — was decoded as UTF-8
with `errors="replace"` and regex-stripped into mojibake; a Cloudflare
interstitial extracted to a line of boilerplate. Either way the model learned
nothing, answered from memory, and the URL still appeared in the episode's
"sources consulted" list. Grounding theatre: the citation list looks real while
nothing was read. Non-text content types and extractions under 400 characters
are now refused, with a message telling the model to open something else.

### A sixth bug, found by the first live run after the fixes

The grounding gate fired on its very first real invocation — and it was right
to. `openresearcher-run.py` returned `grounded: false` at **turn 0** on
*"What is SearXNG and what is it used for?"*, having called no tool at all.
The cause was `enrich_question()`, on by default since 2026-08-18: asked to
rewrite the question into a demanding brief, it **answered it instead** —

> "...a free, open-source, decentralized metasearch engine written in Python
> that aggregates results from multiple search engines into a single
> interface..."

The research model, handed a user turn that already contained the answer,
correctly concluded there was nothing to look up and stopped without opening a
page. Every sub-question was exposed to this. Note the sequence: enrichment was
a silent no-op for its entire life until the `</think>` parsing fix on
2026-08-20 (documented above) — so the first time it actually *worked* was the
first time it could do harm, and what it did was manufacture exactly the
symptom this whole pipeline exists to prevent.

Prompt tuning alone could not fix it, and the reason is interesting. Measured
2026-08-22 against OpenResearcher-30B-A3B on identical inputs:

| enrichment prompt | result |
|---|---|
| long and careful | 6024 chars of reasoning, hit the token cap, **empty content** |
| short and direct | degenerated into `<tool_call><tool_call>...` spam |
| the one that returned prose | **answered the question** inside the brief |

The model is post-trained hard enough on agentic research that it cannot do
meta-work *about* a research question — the same trait `force_answer()` already
exists to fight. Handed the same short prompt, **Gemma-4-26B-A4B returned a
clean brief in 13 seconds**, `finish_reason: stop`, no leaked facts.

That is the exact inverse of this README's earlier comparison, and the two
findings together are the useful result: *the general model that will not do
research is good at writing the brief, and the researcher that will not write
briefs is good at research.* Enrichment now runs against `DRP_ENRICH_LLM_URL`
(defaulting to `DRP_LLM_URL`, but point it at whatever instruct model you
already have resident), the empty/truncated/tool-call-spam/implausibly-long
cases all fall back to the original question and say so on stderr, and the
brief is **attached to** the question as "Research requirements:" rather than
replacing it — so a rewriter that leaks something anyway cannot hide the
question from the agent.

The truncation cause also turned out to be a second instance of the
`max_tokens` bug fixed above: `enrich_question()` was the other call sharing the
1200-token tool-turn budget with its own reasoning. It now has
`DRP_ENRICH_MAX_TOKENS` (4000) and treats `finish_reason: length` as a failed
enrichment.

Two smaller ones: search result ids were per-search and `STATE["results"]` was
overwritten by each search, so an interleaving model asking for the "[3]" it
saw two searches ago was silently handed a different document and cited that —
numbering is now global and monotonic. And `_is_degenerate()` ignored anything
under six sentences, so five identical sentences scored as fine.

## The seventh and eighth bugs: research worked, writing never happened

With everything above fixed, the first clean end-to-end run went like this:

```
-> 15 sources, natural conclusion after 66 turns
-> 15 sources, natural conclusion after 49 turns
notebook has 2 research-note sources + 30 link sources
2 sub-question(s) excluded from the episode (no grounded synthesis)
FAILED: No sub-question produced a grounded, synthesized answer ... Refusing to generate.
```

Thirty sources read across two sub-questions, both concluding naturally rather
than being force-answered — the best research this pipeline has done — and not
one word of prose. Both answers were the canned "here are the URLs" fallback, so
the grounding gate refused the episode. Correctly: under the old code this would
have shipped two sections of bare URL lists to a transcript model briefed to
name specific papers in depth, and it would have produced a confident episode
with essentially no research in it.

**`force_answer()`'s central mechanism had been inverted by the chat template.**
The prefill `"Final answer, no tool calls:"` lands *inside* the model's `<think>`
block, because the template opens the assistant turn there. So the phrase became
the first tokens of the model's reasoning rather than of its answer. It duly
thought, closed `</think>`, and emitted a tool call — which `stop=["<tool_call>"]`
truncated to nothing. Empty content, every time. The docstring's careful
explanation of why the prefill works had been describing something that wasn't
happening.

No prompt fixes this. Measured 2026-08-22 against OpenResearcher-30B-A3B, all
with `tools` omitted from the request:

| attempt | result |
|---|---|
| prefill + `stop`, as shipped | empty — prefill swallowed by `<think>` |
| no prefill, no stop | `<tool_call><function=browser.search>` as plain text |
| blunt "do NOT emit any tool call" | same |
| **clean context**, 3 source excerpts, "write prose, not a list of links" | **29,084 characters of `<tool_call>` repeated** |
| **Gemma-4-26B-A4B**, that same clean prompt | a correct grounded paragraph: system, authors, venue, URL |

It is a research-*trajectory* model. It does not write reports, in any context
this repo could construct. That also explains the "216–2114 chars each"
measured further up: those were the vestiges of a synthesis step that never
really worked.

So the researcher researches and a writer writes — the same division of labour
`enrich_question()` arrived at from the opposite direction, now confirmed from
both ends. `synthesize()` writes the report on `DRP_SYNTH_LLM_URL` from
`SOURCE_TEXT`: the page text actually served to the researcher, retained per
source. **This is more grounded than what it replaces, not less** — the writer's
prompt contains only text this process fetched and the agent read, it is told
that is all it knows, and it never sees the question without the sources.

`resolve_answer()` is the cascade: grounding gate → the model's own concluding
answer if usable → the writer → `force_answer()` → the honest listing. That
second step matters on its own. The main loop had been *discarding* the model's
final turn and jumping straight to `force_answer()` — correct in 2026-08-18 when
there was no way to judge that text, but combined with a `force_answer()` that
cannot succeed here, it turned genuine conclusions into source listings.

**The eighth bug, found in the next run's log.** Synthesis worked — and reported
`wrote 3407 chars from 3 sources` for a sub-question that had read **eight**. A
page that fetched and validated fine was banked in `SOURCES` — counted as read,
listed in the episode's "sources consulted" — while contributing nothing to
`SOURCE_TEXT`, because retention only happened on the code path that serves a
chunk. An open whose cursor lands past the end of a short page returns early,
and so does a re-open. Same class of overstated grounding as the PDF-mojibake
case: a citation list saying eight while the prose was written from three. The
page is now retained the moment it validates, before any early return.

**Why both of these took a live run to find:** `run_research()` captured the
child's stderr and dropped it unless the subprocess exited non-zero. Every
`[force_answer]` and `[synthesize]` line explaining what had happened was
discarded on exactly the runs that "succeeded" without synthesizing anything.
Those diagnostics now surface in the pipeline log, which is how the eighth bug
was spotted in seconds rather than after a 35-minute re-run.

## Four more, found by code review on 2026-08-23

None of these had fired in a run yet; all four are the same shapes as the bugs
above, caught before they cost an episode.

**A truncated answer that was truncated to nothing became the answer.** When a
reasoning model spends its whole `max_tokens` inside `reasoning_content`, the
response comes back `finish_reason: "length"` with *empty* content — a
documented, measured failure mode of this stack. `force_answer()` appended its
"[Note: this synthesis was cut off...]" flag unconditionally, so the note became
the entire answer: non-empty, no tool-call syntax, one sentence, therefore
accepted by `_usable_answer()` and reported as `grounded: true, synthesized:
true`. The episode would have narrated a sub-question whose complete body was a
truncation notice. The note is now only appended to real text; an empty answer
falls through to the "say which failure fired" path, exactly as `synthesize()`
has always handled it.

**An LLM failure in `force_answer()` destroyed the sub-question it was meant to
rescue.** `resolve_answer()` guarded `synthesize()` but called `force_answer()`
bare, and everything below that call is the honest "research read these sources,
here they are" fallback. `chat()` re-raises on any 4xx — including the 400 an
over-long context returns, and this call asks for `DRP_ANSWER_MAX_TOKENS` on top
of a window a 120-turn run has already filled — so the most likely failure at
the end of the longest runs threw away the whole sub-question instead of
degrading. Fourth recurrence of "completed work must survive a later failure".

**The paging hint told the model to re-read the same paragraph.** `_page_offset()`
accepts both cursor conventions (small ordinals, or byte offsets at or above
`PAGE_CHARS`) because the model emits both. Both "read further" hints were
hard-coded to the ordinal form: a model that had sent `cursor=4000` was told to
send `cursor=4001`, which reads back as byte 4001 — the same 4000 characters
shifted by one. That is precisely the no-op paging `_page_offset()` was written
to end, reintroduced through the hint text. `_next_cursor()` now advances in
whichever unit was asked for.

**Pruning bounded tool results, and tool results were not the only thing
growing.** Every assistant turn is appended with its `reasoning_content` —
deliberately, it carries the model's plan — and nothing ever elided it. At 1200
tokens per turn a 120-turn run accumulates ~144,000 tokens of reasoning alone,
past the 131,072-token window before the system prompt and the eight verbatim
tool results are counted, so the context-window fix above did not actually hold
at the default `--max-turns`. `_prune_history()` now drops `reasoning_content`
from assistant turns older than the recent working set: scratch work about tool
calls whose results are themselves already stubbed out. A 40-turn synthetic
history shrinks by more than half with the message sequence intact.

Five smaller ones from the same review:

- **Tool dispatch could raise, and this script has no top-level handler.** Every
  dispatch function degrades internally (`tool_search` returns `"Search
  failed: ..."` as tool output), but the call itself was unguarded — and its
  arguments are model output. A JSON string `"1"` for `cursor`, a shape models
  emit despite the schema, raises a `TypeError` in `_page_offset()`'s
  comparison; a string `topn` raises on the results slice. Either exits
  non-zero, and the pipeline's `run_research()` logs and skips the
  sub-question — every turn already completed, discarded over one malformed
  argument. The call site now returns the error as tool output, naming the
  argument types, so the model can retry.
- **`DRP_KEEP_TOOL_RESULTS=0` turned pruning off rather than all the way up.**
  `tool_idx[:-0]` is `tool_idx[:0]` is empty, so the knob's most aggressive
  value elided nothing and (after the reasoning fix above) retained every
  reasoning block: the exact unpruned behaviour, from the setting that looks
  like the strictest one. Measured across the knob now: 0 → 10/10 tool results
  stubbed and no reasoning kept; 8 (default) → 2/10 stubbed, 7 reasoning blocks
  kept; system prompt and message sequence intact at every value.
- **Signals bypassed the backend release.** The top-level handler caught
  `Exception`, and the likeliest early end for one of these runs is not an
  exception: runs are launched as transient systemd units, so `systemctl --user
  stop <unit>` sends SIGTERM, which by default terminates with nothing unwound —
  no `release_research_backend()`, no failure notification, and a multi-GB model
  left resident to be OOM-killed and restarted in a loop. SIGTERM and SIGHUP now
  raise `SystemExit`, and the handler catches `BaseException`, so Ctrl-C is
  covered too. Verified live: SIGTERM exits 1 and SIGINT exits 130, both running
  the stop command and the notifier.
- **The grounding-gate comment described a guard its function did not have.**
  The long `# THE grounding gate` block sat in `force_answer()`, whose docstring
  said the ungrounded case "is checked first, before the call is even made" —
  true only because its one caller gates first. The block now lives on the
  actual check in `resolve_answer()`, the one place every answer route passes
  through.
- **The Hermes skill's monitoring instructions pointed at a file nothing
  writes.** It said to tail `/tmp/drp-*.log`; `--detach` writes
  `$DRP_RESULTS_DIR/deep-research-podcast-<ts>.log`. An agent following the
  skill saw no progress at all. Its phase list also still said phase 1 starts
  SearXNG, contradicting the correction a few lines above it.

## Verifying what `--source-document` actually is

`DRP_DOC_VERIFY_CMD`, added 2026-08-23. If set, it is run as `CMD <path>` against
`--source-document` before the file is read, and the run aborts unless it exits 0.

The failure it exists for happened three times in two days, and never inside this
repo: the converted document sitting at the path everything downstream reads was
not what it claimed to be. Twice it was plain `pdftotext` output written into the
converter's own output directory. The third time an agent decided the converter had
stalled — it had not; it finished twenty minutes later — and transcribed the PDF
itself, then said so.

That third shape is the one worth building against. A transcription carries real
headings, tables and display math, so no check on the *content* can distinguish it
from a genuine extraction, and unlike shredded layout it can quietly change an
equation or a number in a document this pipeline goes on to narrate as the user's
own paper. The answer is provenance rather than shape — on the origin box, a
converter wrapper writes a checksummed sidecar and the verifier checks it — but
that belongs to whatever converts documents, not here. This script only asks the
question and honours the answer.

It fails **closed**: once configured, a verifier that is missing, unrunnable or
slow aborts the run exactly as a failed check does. A verification that did not
happen is not a pass, and a gate that opens when it breaks is not a gate. Unset
(the default) means no gate was asked for and nothing changes.

Every earlier attempt at this was a paragraph in a skill file telling an agent to
check first, and each was followed by an agent that did not.

## When the server rejects the model's own tool call

2026-08-24, a real run. Three identical 500s, five seconds apart, and the
sub-question died with them:

```
Failed to parse tool call arguments as JSON: [json.exception.parse_error.101]
parse error at line 1, column 1098: syntax error while parsing object -
unexpected end of input; expected '}'
```

That is llama.cpp refusing what the model had just emitted. `TURN_MAX_TOKENS` is
1200, sized for "emit one tool call", and a model that writes a long enough query
blob runs out of budget *inside the braces* — the JSON stops mid-object and the
server will not accept it.

Two things were wrong, and the second is the expensive one.

**`chat()`'s retry loop is built for transient failures.** A 503 during a reload
or a dropped socket is worth another attempt with the same request. This is not:
the failure is a property of the request, so the second and third attempts
reproduced it exactly. `chat()` now recognises this specific rejection and
doubles `max_tokens` for the retry, capped at 8000, for that call only —
`TURN_MAX_TOKENS` stays where it is, because it is also the per-turn reasoning
budget that 120-turn runs are sized against.

**A dead call took the whole sub-question with it.** The research loop called
`chat()` bare, so an exhausted retry propagated out of `main()`, exited non-zero,
and the pipeline discarded the sub-question — every source, every turn. It cost
one at turn 4; at turn 90 it would have cost ninety turns and everything they
found. But by then nothing is missing: `SOURCES`, `SOURCE_TEXT` and the
trajectory are all in hand. The loop now stops researching and answers from what
it has, exactly as a spent turn budget does. If nothing had been read yet, the
grounding gate refuses and the pipeline drops the sub-question — the same outcome
as before, minus the traceback.

Fifth recurrence of "completed work must survive a later failure".

## Citing what the write-up actually rests on

`synthesize()` writes from `SOURCE_TEXT` up to `DRP_SOURCE_TEXT_TOTAL` (60,000
chars by default) and stops there. On a productive question that cap bites hard:
a 2026-08-24 run read 24 sources for one sub-question and only 10 reached the
writer. All 24 were then listed under the episode's "sources consulted".

Fourteen of those citations were for pages no sentence of the write-up could have
seen. The researcher did read them — the claim was not false — but it was the
stronger claim than the material supported, in a repo whose whole argument is that
"sources" means pages actually read and written from. So each source now carries
`used`, and the episode lists the two groups separately: "sources consulted for
this question", and "also read, but not part of the write-up above (do not
attribute claims to these)".

## A section can be honestly short

The same run produced a 366-character section from 24 sources, next to a
4,542-character one from 13. Nothing was broken: the question was the historical
lineage of a field only months old, and the writer is instructed to say when the
excerpts do not settle something rather than fill the gap. It declined to pad, and
that was correct.

The problem is what the briefing then tells the episode model — cover every
question "in real depth", name specific papers and organizations. Aimed at two
sentences, that is an instruction to invent the depth.

A minimum-length gate was the obvious fix and the wrong one: it would drop exactly
the honest-thin case and reward a writer that padded. Instead, a section below
`DRP_THIN_SECTION_CHARS` (1,200) carries a note into the content telling the
narrators to cover it briefly, not to expand it, and that thin evidence is itself
the finding. The default briefing's demand is overridden where it does not apply,
and nowhere else.

## Verified end to end

Second run, after all of the above, same two sub-questions:

```
-> 8 sources, natural conclusion after 48 turns, synthesized
   [synthesize] wrote 3407 chars from 3 sources
-> 15 sources, natural conclusion after 59 turns, synthesized
   [synthesize] wrote 4474 chars from 14 sources
triggering podcast generation (deep_dive profile, content=10579 chars)
job_id=command:qngc4zen765kmp4reqez
```

Result: a 24-minute, 10-segment episode. Checked along the whole chain —

| stage | check | result |
|---|---|---|
| research → synthesis | every URL cited in the prose actually opened and read | 9/9 |
| synthesis → episode | every named system, author and figure present in the research | all |
| episode | anything narrated that is not in the research | none found |

Segment titles: *The 'No Free Lunch' Problem*, *Advanced Attacks: B⁴ and the
RLCracker Threat*, *The Defensive Frontline: SEEK and SimKey*, *Future-Proofing
with Dual-Embedding Watermarking*. Named papers with authors and dates, specific
figures (a 98.5% watermark-removal rate against GPT-4o's 6.75%), every one
traceable to a page the researcher opened.

Two honest notes on that run. Its first sub-question is thin — 2 of 8 sources
cited, one non-DeepMind lab named, and it spends much of its length re-describing
SynthID, which is the "restating background" failure enrichment exists to
prevent; it ran on pre-fix code where five of its eight sources never reached the
writer. And the writer can still attach wrong metadata to a real source: one
paper is labelled 2026 while its arXiv ID indicates September 2025. "No
fabricated citations" is not the same as "every stated fact is correct."

## License

MIT — see `LICENSE`.
