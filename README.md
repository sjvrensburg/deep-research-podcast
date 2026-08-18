# deep-research-podcast

Turn a topic into hours of unattended, multi-turn agentic research, then into a
long, grounded podcast episode — trigger it before you leave, get a finished
episode when you're back.

Two small scripts, no framework:

- **`openresearcher-run.py`** drives [OpenResearcher-30B-A3B](https://github.com/TIGER-AI-Lab/OpenResearcher)
  through its own native `browser.search` / `browser.open` / `browser.find` tool
  loop against [SearXNG](https://github.com/searxng/searxng), via any
  OpenAI-compatible chat endpoint (llama.cpp, vLLM, Ollama, whatever you're
  already running). ~200 lines, no vLLM, no Java/Lucene, no dataset plumbing —
  see "Why not the upstream harness" below.
- **`deep-research-podcast.py`** chains several `openresearcher-run.py` calls
  (one per sub-question) into an [Open Notebook](https://github.com/lfnovo/open-notebook)
  notebook and a generated multi-speaker episode.

Both were extracted from [`halo-prep`](https://github.com/sjvrensburg/halo-prep),
a runbook for a specific local-LLM box, where this pipeline first ran end to end.
That repo has the hardware/model setup this was built against; this repo is the
hardware-independent part.

## Why this exists

A single long research call against one broad question tends to skim
breadth-first and never go deep on any one thread. What actually produces a
long, detailed episode is: decompose the topic into several focused
sub-questions yourself (this is deliberately *not* automated — it needs
context about what the user actually wants covered), research each one to a
real turn budget (80–150 tool calls, not the 15–20 a "quick answer" would use),
and feed each synthesized answer to Open Notebook as its own grounded source.

## Why not the upstream OpenResearcher harness

OpenResearcher ships as a *model* (30B-A3B, MoE, distilled from GPT-OSS-120B
driving native browser tools on 96K trajectories of 100+ turns each — reports
54.8% on BrowseComp-Plus) plus a harness built for an 8×A100 vLLM deployment:
`pyproject.toml` hard-pins `vllm==0.13.0`, wants `pyserini` (Java 21 + Lucene),
`faiss-cpu`, `gpt-oss[all]`; `setup.sh` downloads a multi-GB BrowseComp-Plus
corpus and search index. None of that is needed to answer a question — the
model emits its `browser.*` tool schema correctly through any OpenAI-compatible
`tools`/`tool_choice` request (confirmed against a plain llama.cpp `--jinja`
server), so `openresearcher-run.py` is the missing ~150-line executor instead
of a port of `deploy_agent.py`.

## Setup

1. **An LLM endpoint** serving OpenResearcher-30B-A3B (or another model that
   handles OpenAI-style `tools`/`tool_choice` reasonably) — any
   OpenAI-compatible `/v1/chat/completions` server. Point at it with:
   ```bash
   export DRP_LLM_URL=http://127.0.0.1:8085/v1/chat/completions
   ```
2. **A SearXNG instance** with the JSON output format enabled (`search:
   formats: [html, json]` in `settings.yml`) — no API key needed:
   ```bash
   export DRP_SEARXNG_URL=http://127.0.0.1:8888/search
   ```
3. **An Open Notebook instance** (self-hosted, `docker compose up`) with a chat
   model, an embedding model, and a TTS model already configured as providers,
   plus at least one **episode profile** that:
   - generates from a `content` string, not a `notebook_id` (see "The
     780K-token bug" below for why),
   - has `max_tokens` set high enough for its outline/transcript calls — Open
     Notebook's node code hardcodes 3000/5000-token caps unless the profile
     overrides them, and a long multi-segment episode's structured-output JSON
     will truncate mid-object against that default,
   - is briefed for real length (`num_segments` around 8–10, not the 5-segment
     default), if you want long episodes rather than padded short ones.

   Set `--episode-profile`/`--speaker-profile` (or `$DRP_EPISODE_PROFILE`/
   `$DRP_SPEAKER_PROFILE`) to whatever you name it. Optionally keep a second,
   faster/lower-fidelity twin profile and pass `--fast` (with
   `--fast-episode-profile`/`--fast-speaker-profile`) for runs where speed
   matters more than voice quality.
   ```bash
   export DRP_OPEN_NOTEBOOK_API=http://127.0.0.1:5055/api
   ```

Everything above is environment-variable-driven with the author's original
defaults baked in, so it also runs unmodified if your setup happens to match
`halo-prep`'s (`:8085` LLM, `:8888` SearXNG, `:5055` Open Notebook).

If your LLM or SearXNG are on-demand services rather than always-on, set
`DRP_BACKEND_START_CMD` / `DRP_BACKEND_STOP_CMD` to whatever brings them up and
down (e.g. `systemctl --user start llama-research`) — both are optional and a
no-op if unset.

## Usage

```bash
python3 openresearcher-run.py "What is SearXNG and what is it used for?"
# or, for a script/pipeline: python3 openresearcher-run.py --json --max-turns 100 "..."

python3 deep-research-podcast.py \
    --episode-name "Score-driven volatility models" \
    --notebook-name "GAS models research" \
    --briefing-suffix "Focus on the practitioner tradeoffs, not just theory." \
    --max-turns 100 \
    "What is the history and motivation behind score-driven (GAS) models?" \
    "How do GAS models compare to GARCH and stochastic volatility in practice?" \
    "What are the open controversies or unresolved questions in this area?"
```

`deep-research-podcast.py` blocks for the full pipeline — realistically 30
minutes to a few hours for a multi-question deep dive. Run it under
`systemd-run --user`, `tmux`, or similar; don't expect it to return quickly.
Set `DRP_POLLER_CMD` to a script that takes `job_id episode_name
deliver_target` if you want an automatic notification when the episode is
ready — `hermes-skills/open-notebook-podcast/scripts/poll_and_notify.sh` is a
working example (Signal delivery via [Hermes Agent](https://hermes-agent.nousresearch.com)),
included for reference, not a hard dependency.

## `hermes-skills/`

Two [Hermes Agent](https://hermes-agent.nousresearch.com) skill definitions
that wrap these scripts for an agentic assistant: decomposing a topic into
sub-questions from conversation context, launching the pipeline as a
detached unit so an agent-gateway restart can't kill a multi-hour job, and
polling for completion. Written for a specific box and a specific agent
harness — read them as a worked example of the operational concerns (how do
you background a two-hour job without losing it, how do you notify on
completion, how do you avoid a duplicate job on a retried tool call) rather
than a drop-in for a different setup.

## Lessons from a real production run

The pipeline passed two small smoke tests (2 sub-questions, 5–8 sources) before
a real 5-sub-question run surfaced three bugs that scale alone had been hiding:

**A 60-second, no-retry HTTP timeout killed a completed run.** Research
finished — all 5 sub-questions resolved, 43 sources — but a single
`/sources/json` POST landed while Open Notebook was mid-flight embedding a
backlog of other sources and took longer than 60s. Worse: a raw socket
`TimeoutError` from `urllib` is not an `HTTPError`, so it slipped past the
`except RuntimeError` guard entirely and killed the process, taking an
unsaved synthesized answer with it. Fixed with a 180s-per-attempt, 3-retry
`api()` wrapper that normalizes every failure to `RuntimeError`, *and* by
writing research results to disk (`/tmp/drp-results-<ts>.json`) immediately
after research finishes and before any Open Notebook call — a crash after
that point now loses at most convenience, never research.

**780,063 tokens against a 131,072-token context.** The recovered notebook
still couldn't generate a podcast: 26 sources (4 short synthesized answers,
216–2114 characters each, plus 22 full-text citation PDFs, each hundreds of
embedding chunks) blew straight past the model's context window once
`notebook_id`-driven generation pulled every raw source in. The synthesized
answers are the actual product of running OpenResearcher — a few KB total —
so `deep-research-podcast.py` builds podcast `content` directly from them
instead. The notebook still gets every source for browsing/citation in the
Open Notebook UI; podcast generation just no longer depends on it.

**The natural-stop path trusted raw model output with zero validation.**
Re-running a lost sub-question standalone failed twice before succeeding: the
model would stop calling tools with `content` empty (emitted as a literal
`"(empty)"` "successful" answer) or full of rambling repeated planning text
(`"Search for 'tail'."` repeated for dozens of lines, 85 turns, never actually
answering) — both reported as a clean natural conclusion. The already-existing
forced-budget fallback (`force_answer()` — prefill the assistant turn with
`"Final answer, no tool calls:"` plus `stop=["<tool_call>"]`, because plain
`tool_choice: "none"` does not reliably stop a model this deep into agentic
post-training from emitting tool-call syntax anyway) degrades gracefully, but
it was only reached on a *forced* stop. Fixed by routing every tool-calls-empty
turn — natural or forced — through it unconditionally, so both get the same
quality floor: a real answer, or an honest "no synthesis, here are the
sources" note, never raw unvalidated output.

The general lesson: a background/unattended pipeline's real failure modes are
often the ones only production scale — enough sources, enough sub-questions,
enough stochastic variation across runs — actually reaches. A passing smoke
test is not evidence of readiness for an unattended multi-hour job.

## License

MIT — see `LICENSE`.
