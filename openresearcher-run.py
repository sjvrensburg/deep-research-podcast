#!/usr/bin/env python3
"""Drive OpenResearcher-30B-A3B through its own agent loop, against local SearXNG.

Extracted from a hardware runbook (github.com/sjvrensburg/halo-prep) where the model
serves off a local llama-server and SearXNG runs alongside it; see that repo's
docs/07-expansion.md §9.2/§9.10 for how those two pieces were stood up. This script
is the missing executor between them and has no other dependency on that box --
point DRP_LLM_URL/DRP_SEARXNG_URL at any OpenAI-compatible chat endpoint and any
SearXNG instance and it works the same way.

    export DRP_LLM_URL=http://127.0.0.1:8085/v1/chat/completions   # your llama-server, vLLM, etc.
    export DRP_SEARXNG_URL=http://127.0.0.1:8888/search            # your SearXNG instance
    python3 openresearcher-run.py "your question"

WHY THIS EXISTS. The model emits `browser.search` / `browser.open` / `browser.find`
correctly through llama.cpp's --jinja (so none of upstream's vLLM/pyserini/Java
harness is needed to DRIVE it) once something on the other end actually answers
those calls. This is that executor, ~150 lines rather than the 8xA100 harness
upstream ships.

It is deliberately NOT a port of deploy_agent.py. That file imports
openai_harmony at module scope, expects vLLM server URLs, and carries
BrowseComp-Plus dataset plumbing. None of that is needed to answer a question.

--max-turns and --json exist for deep-research-podcast.py (same repo), which
needs a much bigger turn budget than a quick interactive answer (this model was
post-trained on 100+ turn trajectories -- 20 is early by its own standards, see
the comment at the bottom of main()) and machine-readable output (answer + the
URLs actually read, not just search hits) to hand off to Open Notebook as a
source. Plain-text mode (no --json) is unchanged for interactive use.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request

LLM = os.environ.get("DRP_LLM_URL", "http://127.0.0.1:8085/v1/chat/completions")
SEARX = os.environ.get("DRP_SEARXNG_URL", "http://127.0.0.1:8888/search")
MAX_TURNS = 20
PAGE_CHARS = 4000          # per browser.open cursor window

# The schema the model was post-trained on. Names and argument shapes matter:
# it emits `browser.search` with a `topn` it was never prompted about, so the
# tool definitions have to match what it expects rather than what we'd design.
TOOLS = [
    {"type": "function", "function": {
        "name": "browser.search",
        "description": "Search the web. Returns a numbered list of results.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"},
            "topn": {"type": "integer", "description": "how many results"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "browser.open",
        "description": "Open a search result by its number, or a URL. Use cursor to page.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"},
            "cursor": {"type": "integer"}},
            "required": ["id"]}}},
    {"type": "function", "function": {
        "name": "browser.find",
        "description": "Find a pattern in the currently open page.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}}, "required": ["pattern"]}}},
]

STATE = {"results": [], "page": "", "url": "", "opened": {}}
# Ordered (insertion order = citation order) map of url -> title, populated by
# tool_open. This, not STATE["results"] (every search hit) or STATE["opened"]
# (keyed by (url, cursor), so one URL appears N times if paged through), is
# what --json reports as "sources": the URLs the model actually read.
SOURCES = {}


def _get(url, timeout=45):
    req = urllib.request.Request(url, headers={
        # SearXNG and most sites reject the default python-urllib agent.
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) halo-prep/1.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _text(raw):
    """HTML -> rough text. Deliberately dependency-free.

    trafilatura/readability would extract better, but each drags a tree into a
    venv this repo would then have to maintain. The model only needs enough
    prose to quote and cite; it is not rendering the page.
    """
    s = raw.decode("utf-8", "replace")
    s = re.sub(r"(?is)<(script|style|nav|footer|header|svg)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"[ \t\r\f\v]+", " ", html.unescape(s)).strip()


def tool_search(query, topn=10):
    url = f"{SEARX}?{urllib.parse.urlencode({'q': query, 'format': 'json'})}"
    data = json.loads(_get(url))
    STATE["results"] = data.get("results", [])[:topn]
    if not STATE["results"]:
        return "No results."
    return "\n".join(
        f"[{i}] {r.get('title','')}\n    {r.get('url','')}\n    {(r.get('content') or '')[:200]}"
        for i, r in enumerate(STATE["results"]))


def tool_open(ident, cursor=0):
    ident = str(ident).strip()
    title = ""
    if ident.isdigit() and int(ident) < len(STATE["results"]):
        r = STATE["results"][int(ident)]
        url, title = r.get("url", ""), r.get("title", "")
    elif ident.startswith("http"):
        url = ident
    else:
        return f"Cannot resolve '{ident}' to a result number or URL."
    try:
        STATE["page"], STATE["url"] = _text(_get(url)), url
    except Exception as e:                      # dead links are normal mid-research
        return f"Failed to open {url}: {type(e).__name__}"
    if url not in SOURCES:
        SOURCES[url] = title
    # Re-opening the same (url, cursor) is the observed failure mode: the model
    # asks for id 0 / cursor 0 repeatedly, gets byte-identical content, and
    # loops until the turn cap. Returning the same text teaches it nothing, so
    # say so explicitly and point at the two ways forward.
    key = (url, cursor)
    STATE["opened"][key] = STATE["opened"].get(key, 0) + 1
    if STATE["opened"][key] > 1:
        end = cursor + PAGE_CHARS
        nxt = (f"Use cursor={end} to read further in this page. "
               if len(STATE["page"]) > end else "This page has no more text. ")
        return (f"You have already read {url} at cursor={cursor}; the content is unchanged. "
                f"{nxt}Or open a different result number, or answer with what you have.")
    chunk = STATE["page"][cursor:cursor + PAGE_CHARS]
    more = (f" [truncated — {len(STATE['page']) - cursor - PAGE_CHARS} chars remain, "
            f"use cursor={cursor + PAGE_CHARS}]"
            if len(STATE["page"]) > cursor + PAGE_CHARS else "")
    return f"{url}\n\n{chunk}{more}"


def tool_find(pattern):
    if not STATE["page"]:
        return "No page open."
    hits = [m.start() for m in re.finditer(re.escape(pattern), STATE["page"], re.I)][:5]
    if not hits:
        return f"'{pattern}' not found in {STATE['url']}"
    return "\n---\n".join(STATE["page"][max(0, h - 200):h + 400] for h in hits)


DISPATCH = {"browser.search": lambda a: tool_search(a.get("query", ""), a.get("topn", 10)),
            "browser.open":   lambda a: tool_open(a.get("id", ""), a.get("cursor", 0)),
            "browser.find":   lambda a: tool_find(a.get("pattern", ""))}


def chat(messages, use_tools=True, stop=None, temperature=0.6):
    body = {"messages": messages, "max_tokens": 1200,
            "temperature": temperature, "top_p": 0.95}
    if use_tools:
        body.update(tools=TOOLS, tool_choice="auto")
    if stop:
        body["stop"] = stop
    req = urllib.request.Request(LLM, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=1800))["choices"][0]


def _is_degenerate(content):
    """Catch a repetition failure mode that `stop=["<tool_call>"]` doesn't:
    prose repetition, not tool-call-syntax repetition.

    Observed 2026-08-18 in a real deep-research-podcast run: force_answer()
    wrote a decent opening paragraph, then degenerated into ~25 near-identical
    sentences -- "Use the arxiv (source 0) for detection." repeated over and
    over -- while trying to plan citations instead of just writing them. That
    passed every existing check: content was non-empty, contained no
    `<tool_call>` text, and answer/JSON output are both plain strings, so
    nothing downstream flagged it. It only didn't reach the actual episode
    because Open Notebook's own transcript model happened to filter it out --
    luck, not this script working as designed.

    Sentence-level, not line-level: this kind of repetition lands inside one
    continuous paragraph, not across separate lines, so splitting on newlines
    (as a naive dedup check might) would miss it entirely.
    """
    sentences = [s.strip().lower() for s in re.split(r"(?<=[.!?])\s+", content) if s.strip()]
    if len(sentences) < 6:
        return False
    counts = {}
    for s in sentences:
        counts[s] = counts.get(s, 0) + 1
    return max(counts.values()) >= 4


def force_answer(messages):
    """Get a final answer out of a model that was post-trained hard enough on
    agentic tool use that plain `tool_choice: none` does not reliably stop it
    from emitting tool-call syntax anyway (observed: raw, unparsed
    `<tool_call>...` XML-ish text landing in `content` even with no `tools`
    in the request). Two things fix this in combination, neither alone:

    1. Prefill the assistant turn with "Final answer, no tool calls:" --
       continuing from an already-started non-tool-call utterance reliably
       steers generation away from the tool-call template, vs. asking fresh.
    2. `stop=["<tool_call>"]` bounds the failure mode observed when the model
       doesn't know the answer (e.g. a locally-specific term outside its
       training data): it can spiral into a repeating loop of `<tool_call>`
       attempts that never resolves. The stop sequence caps that at whatever
       reasoning text came before the first attempt, instead of burning the
       rest of max_tokens on garbage.

    Neither guards against prose-level repetition (a citation-planning
    sentence looping instead of tool-call syntax looping) -- see
    `_is_degenerate()` for that case, added after it reached a real run.
    """
    msgs = messages + [{"role": "assistant", "content": "Final answer, no tool calls:"}]
    content = chat(msgs, use_tools=False, stop=["<tool_call>"],
                   temperature=0.3)["message"].get("content") or ""
    # A clean run leaves the drafted answer after the model's own closing
    # </think> tag (llama.cpp's deepseek reasoning-format marker); a
    # degenerate run never reaches one, so this is a no-op fallback there,
    # not a silent failure -- see the docstring's case 2.
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    content = content.strip()
    if content and not _is_degenerate(content):
        return content
    # Reached with either empty content (observed at very small turn budgets
    # on locally-specific/out-of-training topics: the model never produces
    # usable prose even with the stop sequence) or degenerate content
    # (_is_degenerate() above). Rather than gamble on a third LLM call (which
    # can just as easily degenerate the same way -- tried, see the commit
    # message), fall back to something deterministic and honest: the pipeline
    # downstream (Open Notebook's own outline/transcript LLM) still gets real
    # material to work with, and nothing pretends the synthesis succeeded
    # when it didn't.
    if SOURCES:
        listing = "\n".join(f"- {t or '(untitled)'}: {u}" for u, t in SOURCES.items())
        return ("Automated research did not produce a synthesized answer within "
                "its turn budget. The following sources were found and read during "
                f"research and may still be useful raw material:\n{listing}")
    return "Automated research found no answer and no usable sources within its turn budget."


def enrich_question(question):
    """Rewrite a possibly-sloppy input question into a precise research brief
    before handing it to the agent loop.

    Added 2026-08-18 after a real deep-research-podcast run: an open,
    casually-phrased sub-question ("how does X compare to what other labs
    have done") reliably let the agent take the easy path -- re-describing
    background it had already read (the source document's own citations)
    rather than actually finding and naming new material. That is not
    force_answer()'s job to catch (the resulting prose was fluent and
    non-repetitive, so `_is_degenerate()` correctly left it alone) -- the
    question itself needed to demand more. This is exactly the gap between
    "what a user types into a chat" and "what a research brief should say,"
    and real callers (a Hermes skill decomposing a vague request, a human
    typing a quick question) routinely produce the former. One cheap LLM
    call up front, asking a model to restate the question with an explicit
    bar for specificity, costs a few seconds and changes what "a natural
    conclusion" is allowed to look like for everything downstream.

    Best-effort: a failed enrichment call returns the original question
    unchanged rather than blocking research on an LLM call that isn't the
    point of the run.
    """
    msgs = [
        {"role": "system", "content": (
            "You rewrite research questions for an autonomous web-research agent. "
            "The input question may be short, casual, or underspecified -- typical "
            "of a real user's chat message, not a careful research brief. Rewrite it "
            "into a precise, demanding research brief that:\n"
            "1. Preserves the original question's intent and scope exactly -- do not "
            "broaden, narrow, or change the topic.\n"
            "2. If the question asks for a comparison, trend, or what has changed, "
            "explicitly requires the agent to name specific papers, systems, "
            "organizations, or people, with approximate dates -- general statements "
            "without named specifics are not an acceptable answer.\n"
            "3. Explicitly forbids treating background the question already assumes "
            "is known (e.g. the subject's own well-known baseline comparisons) as if "
            "restating it were a new finding.\n"
            "Return ONLY the rewritten question/brief, 2-4 sentences, nothing else -- "
            "no preamble, no explanation of what you changed."
        )},
        {"role": "user", "content": question},
    ]
    try:
        content = chat(msgs, use_tools=False, temperature=0.3)["message"].get("content") or ""
    except Exception:
        return question
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    content = content.strip()
    return content or question


def _emit(question, answer, turns_used, budget_spent, as_json, researched_as=None):
    sources = [{"url": u, "title": t} for u, t in SOURCES.items()]
    if as_json:
        out = {
            "question": question, "answer": answer, "sources": sources,
            "turns_used": turns_used, "budget_spent": budget_spent,
        }
        # Only present when it differs from `question` -- most callers should
        # never need this, but it's the honest record of what was actually
        # researched when enrich_question() rewrote a vague input.
        if researched_as and researched_as != question:
            out["researched_as"] = researched_as
        print(json.dumps(out))
        return
    if researched_as and researched_as != question:
        print(f"\n[researched as: {researched_as}]", file=sys.stderr)
    if budget_spent:
        print(f"\n[turn budget spent — forcing an answer from {turns_used} turns of research]")
    print("\n=== ANSWER ===\n" + answer)
    if sources:
        print("\n=== SOURCES ===")
        for s in sources:
            print(f"- {s['title'] or '(untitled)'}\n  {s['url']}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("question", nargs="*", help="the research question")
    p.add_argument("--max-turns", type=int, default=MAX_TURNS,
                    help=f"tool-call budget before forcing an answer (default {MAX_TURNS}; "
                         "the deep-research-podcast skill passes something much larger, "
                         "e.g. 80-150, since this model was post-trained on 100+ turn "
                         "trajectories and a small budget cuts it off early)")
    p.add_argument("--json", action="store_true",
                    help="emit one JSON object on stdout instead of human-readable text "
                         "-- {question, answer, sources, turns_used, budget_spent}")
    p.add_argument("--no-enrich", action="store_true",
                    help="skip the question-enrichment pre-step (see enrich_question()) and "
                         "research the question exactly as given. Enrichment is on by default "
                         "because real callers -- a Hermes skill decomposing a vague request, "
                         "a human typing a quick question -- routinely hand this script an "
                         "underspecified question, and that underspecification is what let a "
                         "real comparative sub-question degrade into restating background "
                         "instead of finding new material. Turn it off for a question that's "
                         "already a precise, demanding brief -- the extra LLM call has no "
                         "upside there.")
    args = p.parse_args()
    max_turns = args.max_turns
    question = " ".join(args.question) or "What is AMD Strix Halo and why is it notable?"
    researched_as = question if args.no_enrich else enrich_question(question)

    # Without this the model researches indefinitely -- it has no notion of a
    # turn budget and will keep opening pages until the cap fires with no answer.
    messages = [
        {"role": "system", "content":
            "You are a research assistant with web tools. Search, read the most "
            f"promising sources, then ANSWER. You have at most {max_turns} tool "
            f"calls; aim to answer within {max(8, max_turns // 3)}. Do not re-open "
            "a page you have already read. When you make a comparative, empirical, "
            "or 'this changed' claim, name the specific paper, system, organization, "
            "or person behind it with an approximate date, inline in your answer -- "
            "not only as a trailing URL list. A claim without a named source attached "
            "is not an acceptable answer to a comparative question. Do not restate "
            "background the question already assumes is known as if it were a new "
            "finding."},
        {"role": "user", "content": researched_as},
    ]
    for turn in range(max_turns):
        choice = chat(messages)
        msg = choice["message"]
        calls = msg.get("tool_calls") or []
        # reasoning_content, not content, on tool-calling turns -- llama.cpp's
        # default --reasoning-format deepseek. Dropping it loses the model's plan.
        messages.append({k: v for k, v in msg.items() if k in
                         ("role", "content", "tool_calls", "reasoning_content")})
        if not calls:
            # 2026-08-18: `content` on the turn where the model stops calling
            # tools is NOT a trustworthy answer signal by itself -- two
            # distinct degenerate cases observed back to back while
            # recovering one real run's lost sub-question:
            #   1. content empty, real text landed in reasoning_content
            #      instead (the same deepseek reasoning-format split already
            #      handled for tool-calling turns above).
            #   2. content non-empty but degenerate: the model stopped
            #      calling tools while still mid-plan, e.g. repeating
            #      "Search for 'tail'." for dozens of lines (85-turn run, 10
            #      sources read, never actually answered).
            # Before this fix, case 1 emitted a literal "(empty)" answer and
            # case 2 emitted the rambling planning text -- both reported as a
            # *natural* conclusion (budget_spent=False), which is worse than
            # the forced-budget path: that one already degrades gracefully
            # via force_answer()'s "answer, or else list the sources" logic.
            # Rather than try to heuristically detect "is this text
            # degenerate" (repetition thresholds are fragile and this is a
            # research pipeline, not a text classifier), route every
            # tool-calls-empty turn through force_answer() unconditionally:
            # it always re-asks with the anti-degeneration prefill + stop
            # sequence that already exists for exactly this reason, so
            # natural and forced conclusions now get the same quality floor.
            # A user turn goes first, same as the forced-budget path below --
            # force_answer() appends its own assistant-role prefill, and
            # `messages` here already ends in an assistant turn (the
            # degenerate one), so skipping this would stack two assistant
            # turns back to back with no user turn between them.
            messages.append({"role": "user", "content":
                "That did not produce a usable answer. Stop researching and "
                "answer now, using only what you have already read. Cite the "
                "URLs you used."})
            _emit(question, force_answer(messages), turn, False, args.json, researched_as)
            return
        for c in calls:
            fn = c["function"]["name"]
            try:
                args_ = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args_ = {}
            if not args.json:
                print(f"[turn {turn}] {fn}({json.dumps(args_)[:90]})", file=sys.stderr)
            out = DISPATCH.get(fn, lambda a: f"Unknown tool {fn}")(args_)
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "content": out[:6000]})
    # Do NOT just give up. This model was post-trained on 96K trajectories of
    # 100+ turns, so even a raised turn budget is early by its standards and it
    # will happily keep researching -- observed twice at the old 20-turn
    # default, opening genuinely relevant sources (the ROCm Strix Halo page
    # among them) and never concluding. Withdrawing the tools forces it to
    # answer from what it has gathered, which is the whole point of a bounded
    # run -- deep research still needs to actually finish and hand off to a
    # podcast job, not run until the box is turned off.
    messages.append({"role": "user", "content":
        "Stop researching and answer now, using only what you have already read. "
        "Cite the URLs you used."})
    _emit(question, force_answer(messages), max_turns, True, args.json, researched_as)


main()
