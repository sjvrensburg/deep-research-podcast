# deep-research-podcast

Point it at a topic. Walk away. Come back to a long, cited, multi-speaker podcast
episode — grounded in real research an agent did on its own, not a model
riffing from memory.

```bash
python3 deep-research-podcast.py \
    --episode-name "Score-driven volatility models" \
    --notebook-name "GAS models research" \
    --max-turns 100 \
    "What is the history and motivation behind score-driven (GAS) models?" \
    "How do GAS models compare to GARCH and stochastic volatility in practice?" \
    "What are the open controversies or unresolved questions in this area?"
```

Each sub-question gets its own 80–150-turn research agent (real web search,
real page reads, real citations — not one shallow pass), and the synthesized
answers become the podcast's source material. On a real 5-sub-question run this
produced 43 cited sources and a 10-segment episode; a smaller 2-question
smoke test finished in 773.7s end to end. Both are documented below, bugs
included — this has run at real scale, not just in a demo.

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

## Hardened by a real production run

Two small smoke tests (2 sub-questions, 5–8 sources) passed cleanly first.
A real 5-sub-question run then surfaced three bugs — the kind that only show
up at genuine scale — and all three are fixed in this codebase now:

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

Full technical detail on each is in the code's own comments (`api()` in
`deep-research-podcast.py`, `force_answer()` in `openresearcher-run.py`).

## License

MIT — see `LICENSE`.
