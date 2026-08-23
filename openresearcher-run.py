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
    export DRP_ENRICH_LLM_URL=http://127.0.0.1:8088/v1/chat/completions  # any general instruct
                                                                   # model; see enrich_question()
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
import time
import urllib.error
import urllib.parse
import urllib.request

LLM = os.environ.get("DRP_LLM_URL", "http://127.0.0.1:8085/v1/chat/completions")
SEARX = os.environ.get("DRP_SEARXNG_URL", "http://127.0.0.1:8888/search")
# Endpoint for the question-enrichment pre-step. Defaults to the research model,
# but should point at a GENERAL INSTRUCT MODEL if you have one -- see
# enrich_question() for the measurements. Same OpenAI-compatible shape.
ENRICH_LLM = os.environ.get("DRP_ENRICH_LLM_URL", "") or LLM
# Endpoint that WRITES the final report from the sources the researcher read.
# Defaults to the enrichment model, then to the research model. As with
# enrichment, a general instruct model is strongly preferred -- see synthesize().
SYNTH_LLM = os.environ.get("DRP_SYNTH_LLM_URL", "") or ENRICH_LLM
MAX_TURNS = 20
PAGE_CHARS = 4000          # per browser.open cursor window
TOOL_RESULT_CHARS = 6000   # cap on one tool result kept verbatim in history
# How many of the most recent tool results stay verbatim in the conversation.
# Everything older is replaced by a one-line stub -- see _prune_history().
KEEP_VERBATIM_TOOL_RESULTS = int(os.environ.get("DRP_KEEP_TOOL_RESULTS", "8"))
# Token budget for the model's own output. Tool-calling turns need very little
# (a tool call plus its reasoning); the final synthesis is the entire product of
# the run and was sharing the same 1200 with reasoning until 2026-08-22 -- see
# the comment in chat().
TURN_MAX_TOKENS = 1200
ANSWER_MAX_TOKENS = int(os.environ.get("DRP_ANSWER_MAX_TOKENS", "4000"))
# Enrichment needs its own budget for the same reason the answer does: measured
# 2026-08-22, the rewrite call spent 6024 characters on reasoning and hit the
# 1200-token cap with finish_reason "length" and empty content -- which is
# exactly why enrichment appeared to "produce no usable rewrite" and silently
# fell back on every question.
ENRICH_MAX_TOKENS = int(os.environ.get("DRP_ENRICH_MAX_TOKENS", "4000"))
# A page that fetched fine but yielded less text than this is not a source. See
# tool_open().
MIN_SOURCE_CHARS = 400
# Cap on a single fetched document, before text extraction. _text()'s regex
# passes over a multi-hundred-MB response are slow enough to stall a run.
MAX_FETCH_BYTES = 8 * 1024 * 1024

# The schema the model was post-trained on. Names and argument shapes matter:
# it emits `browser.search` with a `topn` it was never prompted about, so the
# tool definitions have to match what it expects rather than what we'd design.
# Measured 2026-08-22: a general instruct model (Gemma-4-26B-A4B) driving this
# same schema never emits `topn` -- and never calls `browser.open` at all, so
# it answers from memory with zero sources read. See the README section "Why
# not just use a general model you already have running".
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
        "description": "Open a search result by its number, or a URL. cursor is a page "
                       "number within the document: 0 is the first page, 1 the next, and "
                       "so on. Text only -- PDFs and other binaries cannot be read.",
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

STATE = {"results": [], "page": "", "url": "", "opened": {}, "catalog": {}, "next_id": 0,
         # False once force_answer() has fallen back to a canned string instead
         # of real synthesis. Reported as "synthesized" in --json so the
         # pipeline can tell a researched answer from an honest apology --
         # both are plain strings and were indistinguishable until 2026-08-22.
         "synthesized": True}
# Ordered (insertion order = citation order) map of url -> title, populated by
# tool_open. This, not STATE["results"] (every search hit) or STATE["opened"]
# (keyed by (url, cursor), so one URL appears N times if paged through), is
# what --json reports as "sources": the URLs the model actually read.
SOURCES = {}
# url -> the text actually served to the model from that page, capped. Retained
# so the report can be written from what was READ rather than from the agentic
# trajectory -- see synthesize(). Kept separate from SOURCES so the "what did we
# actually read" accounting stays a plain url->title map.
SOURCE_TEXT = {}
SOURCE_TEXT_PER_PAGE = int(os.environ.get("DRP_SOURCE_TEXT_PER_PAGE", "6000"))
SOURCE_TEXT_TOTAL = int(os.environ.get("DRP_SOURCE_TEXT_TOTAL", "60000"))


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) halo-prep/1.0"


def _get(url, timeout=45, max_bytes=MAX_FETCH_BYTES):
    req = urllib.request.Request(url, headers={
        # SearXNG and most sites reject the default python-urllib agent.
        "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # Read a bounded amount, not the whole response: a research search can
        # surface a multi-hundred-MB dataset dump or video file, and _text()'s
        # regex passes over that are slow enough to look like a hang (the
        # 3600s subprocess timeout in deep-research-podcast.py would then eat
        # the whole sub-question). One byte over the cap is enough to know it
        # was truncated.
        body = r.read(max_bytes + 1)
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return body[:max_bytes], ctype


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
    try:
        data = json.loads(_get(url)[0])
    except Exception as e:
        # Was unguarded until 2026-08-18: a real run's second sub-question hit
        # a SearXNG 400 (query too long or otherwise malformed -- unconfirmed
        # which) on this exact call, uncaught, and crashed the whole process.
        # Because the caller (deep-research-podcast.py) built results in one
        # list comprehension with no per-question isolation, that crash also
        # discarded a FIRST sub-question's already-completed research -- a
        # real instance of exactly the "completed work destroyed by a later
        # failure" problem the 2026-08-18 api()-retry fix was supposed to
        # rule out, just in a spot that fix never covered. tool_open() already
        # treats a failed fetch as normal mid-research noise, not a crash;
        # tool_search() needs the same treatment, not more of it downstream.
        return f"Search failed: {type(e).__name__}: {e}. Try a different or shorter query."
    STATE["results"] = data.get("results", [])[:topn]
    if not STATE["results"]:
        return "No results."
    # Result numbering is GLOBAL and monotonic across searches, not 0..n-1 per
    # search (changed 2026-08-22). STATE["results"] used to be overwritten by
    # every search while browser.open resolved ids against whichever list was
    # current, so the model interleaving search and open -- which is exactly
    # what this model does, see the trace in README.md -- could ask for the
    # "[3]" it read two searches ago and silently be handed a different
    # document, then cite that one. Numbers the model has already seen now keep
    # pointing at what it saw (STATE["catalog"]), and it copies ids straight out
    # of the listing either way, so nothing about its trained behaviour changes.
    lines = []
    for r in STATE["results"]:
        i = STATE["next_id"]
        STATE["next_id"] += 1
        STATE["catalog"][i] = (r.get("url", ""), r.get("title", ""))
        lines.append(f"[{i}] {r.get('title','')}\n    {r.get('url','')}\n"
                     f"    {(r.get('content') or '')[:200]}")
    return "\n".join(lines)


def _page_offset(cursor):
    """Interpret `cursor` as the model means it, not as this file used to.

    The model pages with SMALL ORDINALS -- README.md's own captured trace is
    `open(0,cursor=1)`, `open(1,cursor=1)`, `open(0,cursor=2)`. Until 2026-08-22
    this file sliced `page[cursor:cursor+PAGE_CHARS]`, so `cursor=1` returned the
    same opening 4000 characters shifted by ONE CHARACTER. Paging deeper into a
    document -- the single behaviour that most distinguishes this model from a
    general one skimming search-result titles -- was a no-op, and a model that
    believes it has read a source to the end and has actually re-read its first
    paragraph three times will fall back on what it already knows.

    Both conventions are accepted, because the model's own instinct (ordinals)
    and this harness's previous messaging ("use cursor=4000", which the model
    does sometimes follow) disagree: below PAGE_CHARS is a page index, at or
    above it a character offset. Small values are genuinely ambiguous -- cursor=1
    could mean "page 1" or "byte 1" -- and they resolve to the ordinal reading,
    deliberately: "byte 1" is never a useful thing to ask for, and re-serving the
    same opening paragraph is the failure this function exists to end.
    """
    return cursor * PAGE_CHARS if cursor < PAGE_CHARS else cursor


def _next_cursor(cursor):
    """The cursor value that advances one page, in the SAME convention `cursor`
    used.

    2026-08-23: both "read further" hints were hard-coded to the ordinal form
    (`use cursor={cursor + 1}`) even when the model had addressed the page by
    byte offset. A model that sent `cursor=4000` was told to send `cursor=4001`,
    which _page_offset() reads as byte 4001 -- the same 4000 characters shifted
    by one, i.e. exactly the no-op paging that _page_offset() exists to end,
    reintroduced through the hint text. Advance in whichever unit was asked for.
    """
    return cursor + 1 if cursor < PAGE_CHARS else cursor + PAGE_CHARS


def tool_open(ident, cursor=0):
    ident = str(ident).strip()
    title = ""
    if ident.isdigit() and int(ident) in STATE["catalog"]:
        url, title = STATE["catalog"][int(ident)]
    elif ident.startswith("http"):
        url = ident
    else:
        return (f"Cannot resolve '{ident}' to a result number or URL. Search first, "
                "then open a number from the results, or pass a full http(s) URL.")
    try:
        raw, ctype = _get(url)
    except Exception as e:                      # dead links are normal mid-research
        return f"Failed to open {url}: {type(e).__name__}"

    # A fetch that returns 200 is not a source. Until 2026-08-22 it was treated
    # as one: any successful GET landed the URL in SOURCES and therefore in the
    # episode's "sources consulted" list. A PDF (most of the academic material
    # this pipeline chases) got decoded as UTF-8 with errors="replace" and
    # regex-stripped into mojibake; a Cloudflare interstitial or cookie wall
    # extracted to a line of boilerplate. Either way the model learned nothing,
    # answered from memory, and the citation list still looked grounded --
    # exactly the failure this whole pipeline exists to prevent. Refuse both,
    # and say why, so the model opens something else rather than giving up.
    if ctype and not (ctype.startswith("text/") or ctype in
                      ("application/xhtml+xml", "application/xml", "application/json")):
        return (f"Cannot read {url}: content type {ctype!r} is not text (this reader "
                "extracts HTML/plain text only, it does not parse PDFs or binaries). "
                "Look for an HTML version -- e.g. an abstract or landing page -- or "
                "open a different result.")
    page = _text(raw)
    if len(page) < MIN_SOURCE_CHARS:
        return (f"Opened {url} but extracted only {len(page)} characters of text -- "
                "too little to be a usable source (likely a redirect, paywall, cookie "
                "wall, or bot check). Open a different result.")

    STATE["page"], STATE["url"] = page, url
    if url not in SOURCES:
        SOURCES[url] = title
    # Retain the page the moment it is validated, NOT only on the path that
    # serves a chunk (2026-08-22). A page could otherwise be banked in SOURCES
    # -- counted as read, listed in the episode's "sources consulted" -- while
    # contributing nothing to SOURCE_TEXT, so synthesize() never saw it: an open
    # whose cursor lands past the end of a short page returns early, and so does
    # a re-open. Measured on a real run: 8 sources read, only 3 reached the
    # writer. The fetch already succeeded and the text is in hand here; keeping
    # it costs nothing and closes the gap between "read" and "written from".
    if url not in SOURCE_TEXT:
        SOURCE_TEXT[url] = page[:SOURCE_TEXT_PER_PAGE]
    offset = _page_offset(cursor)
    # Re-opening the same (url, cursor) is the observed failure mode: the model
    # asks for id 0 / cursor 0 repeatedly, gets byte-identical content, and
    # loops until the turn cap. Returning the same text teaches it nothing, so
    # say so explicitly and point at the two ways forward.
    key = (url, offset)
    STATE["opened"][key] = STATE["opened"].get(key, 0) + 1
    if STATE["opened"][key] > 1:
        nxt = (f"Use cursor={_next_cursor(cursor)} to read further in this page. "
               if len(page) > offset + PAGE_CHARS else "This page has no more text. ")
        return (f"You have already read {url} at cursor={cursor}; the content is unchanged. "
                f"{nxt}Or open a different result number, or answer with what you have.")
    if offset >= len(page):
        return (f"{url}\n\n[cursor={cursor} is past the end of this page "
                f"({len(page)} characters total). Nothing further to read here.]")
    chunk = page[offset:offset + PAGE_CHARS]
    # Retain what the model was actually shown, so synthesize() can write the
    # report from the sources rather than from the research trajectory.
    kept = SOURCE_TEXT.get(url, "")
    if len(kept) < SOURCE_TEXT_PER_PAGE:
        SOURCE_TEXT[url] = (kept + ("\n" if kept else "") + chunk)[:SOURCE_TEXT_PER_PAGE]
    more = (f" [truncated — {len(page) - offset - PAGE_CHARS} chars remain, "
            f"use cursor={_next_cursor(cursor)}]"
            if len(page) > offset + PAGE_CHARS else "")
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


def _prune_history(messages):
    """Keep the conversation inside the server's context window.

    Nothing pruned this until 2026-08-22, and the arithmetic was never going to
    work: every tool result is kept verbatim at up to TOOL_RESULT_CHARS, plus the
    assistant's reasoning for each turn, so

         40 turns ~=  69,000 tokens
         80 turns ~= 138,000 tokens   <-- past a 131,072-token window
        120 turns ~= 207,000 tokens

    against a DEFAULT_MAX_TURNS of 120. A run physically could not reach its own
    turn budget. What happened instead, somewhere around turn 60-80, was one of
    two things, and both are consistent with the symptoms this repo has been
    chasing: the server rejected the over-long request (an HTTPError that
    crashed the process and took the whole sub-question with it -- see the retry
    loop in chat()), or, with context shifting enabled, it silently dropped the
    HEAD of the conversation. That head is the system prompt demanding named,
    inline attribution, followed by the earliest sources read. A model that
    loses both mid-run finishes by writing from memory, fluently, with a
    citation list assembled from URLs it can no longer see the contents of.

    So: keep the system prompt and the recent working set verbatim, and replace
    the CONTENT of older tool results with a stub. The messages themselves stay
    -- an assistant turn with tool_calls must still be followed by a tool
    message per call, or the next request is malformed -- and nothing citable is
    lost, because SOURCES already holds every URL actually read.
    """
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_idx) <= KEEP_VERBATIM_TOOL_RESULTS:
        return messages
    # KEEP == 0 means "keep nothing verbatim", and it used to mean the opposite:
    # `tool_idx[:-0]` is `tool_idx[:0]` is empty, so the knob's most aggressive
    # value elided NOTHING -- and, since 2026-08-23, also retained every
    # assistant reasoning block, i.e. exactly the unpruned behaviour this
    # function exists to replace. Spelled out rather than relying on slice
    # arithmetic that reads correct and is not (2026-08-23).
    if KEEP_VERBATIM_TOOL_RESULTS <= 0:
        stale, keep_from = set(tool_idx), len(messages)
    else:
        stale = set(tool_idx[:-KEEP_VERBATIM_TOOL_RESULTS])
        keep_from = tool_idx[-KEEP_VERBATIM_TOOL_RESULTS]
    # Tool results were the only thing bounded until 2026-08-23, and they are not
    # the only thing that grows. Every assistant turn is appended WITH its
    # reasoning_content (the main loop keeps it deliberately -- it carries the
    # model's plan), and at TURN_MAX_TOKENS per turn a 120-turn run accumulates
    # ~144k tokens of reasoning alone: past the 131k window before the system
    # prompt and the eight verbatim tool results are counted. The invariant this
    # function promises -- the request never grows past what the server will
    # accept -- did not hold at the default --max-turns. Older reasoning is the
    # cheapest thing to drop: it is the model's scratch work about tool calls it
    # has already made, whose results are themselves already stubbed out here.
    # The recent working set keeps its reasoning verbatim, same boundary.
    out = []
    for i, m in enumerate(messages):
        if i in stale and not m.get("_elided"):
            first = (m.get("content") or "").splitlines()[:1]
            out.append({**m, "content":
                        f"[earlier tool result elided to stay within the context "
                        f"window: {first[0][:120] if first else '(empty)'}]",
                        "_elided": True})
        elif (i < keep_from and m.get("role") == "assistant"
              and m.get("reasoning_content")):
            out.append({k: v for k, v in m.items() if k != "reasoning_content"})
        else:
            out.append(m)
    return out


def chat(messages, use_tools=True, stop=None, temperature=0.6,
         max_tokens=TURN_MAX_TOKENS, retries=3, endpoint=None):
    # `_elided` is bookkeeping for _prune_history(), not part of the API schema.
    body = {"messages": [{k: v for k, v in m.items() if k != "_elided"} for m in messages],
            "max_tokens": max_tokens, "temperature": temperature, "top_p": 0.95}
    if use_tools:
        body.update(tools=TOOLS, tool_choice="auto")
    if stop:
        body["stop"] = stop
    data = json.dumps(body).encode()
    endpoint = endpoint or LLM
    # Unguarded until 2026-08-22, and the only fatal network call in the repo:
    # every other one (_get, tool_search, deep-research-podcast.py's api())
    # already degrades. A single blip here -- a 503 while an on-demand llama
    # service reloads, a dropped socket, a 400 from an over-long request --
    # propagated straight out of main() and destroyed the entire sub-question at
    # whatever turn it landed on, 90 turns of real research included.
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(endpoint, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req, timeout=1800))["choices"][0]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last = RuntimeError(f"LLM returned {e.code}: {detail}")
            # 4xx is a bad request -- usually the context window, which retrying
            # verbatim cannot fix. 5xx and 429 are worth another attempt.
            if e.code < 500 and e.code != 429:
                raise last from e
        except Exception as e:
            last = e
        if attempt < retries - 1:
            print(f"[chat] attempt {attempt + 1}/{retries} failed ({last!r}); retrying",
                  file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"LLM call failed after {retries} attempts: {last!r}")


def _strip_reasoning(message):
    """Pull the actual answer out of a chat message, whichever way the server
    chose to report the model's reasoning.

    Two shapes are in play, and the naive `rsplit("</think>", 1)[1]` this
    replaced was only correct for one of them:

    1. Reasoning INLINE in `content`, terminated by `</think>`, answer after it
       (llama.cpp's deepseek reasoning-format marker). Tail-after-tag is right.
    2. Reasoning OUT-OF-BAND in `reasoning_content`, answer in `content` -- but
       `content` still carries a trailing `</think>` from the template. Here
       tail-after-tag is the EMPTY STRING, and it silently threw the answer away.

    Shape 2 is what the :8085 research model actually emits, and it broke both
    callers for as long as they existed (found 2026-08-20): enrich_question()
    fell through `return content or question` and researched the un-enriched
    question every time -- so the whole enrichment feature was a no-op, never
    once observed working -- and force_answer() saw empty content and returned
    its honest "did not produce a synthesized answer" fallback even when the
    model HAD written a good answer over 86 turns and 17 sources.

    Rule: if `reasoning_content` is populated, the reasoning is already
    out-of-band, so `content` is the answer and stray tags are just noise to
    strip. Otherwise the reasoning is inline and the answer is the tail after
    the last `</think>` -- an empty tail there is a genuine no-answer (the model
    spent the whole completion planning), not a parsing artifact, and callers
    should still treat it as failure.
    """
    content = message.get("content") or ""
    if (message.get("reasoning_content") or "").strip():
        # Out-of-band reasoning: drop a leading <think> block if one leaked in
        # anyway, then any stray tag, and keep everything else.
        content = re.sub(r"^\s*<think>.*?</think>", "", content, flags=re.S)
        return content.replace("<think>", "").replace("</think>", "").strip()
    if "</think>" in content:
        return content.rsplit("</think>", 1)[1].strip()
    return content.strip()


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
    if len(sentences) < 4:
        # Was `< 6` until 2026-08-22, which let the smallest version of exactly
        # this failure through: five sentences, all of them the same sentence,
        # scored as fine because there were not six of them.
        return False
    counts = {}
    for s in sentences:
        counts[s] = counts.get(s, 0) + 1
    return max(counts.values()) >= 4


def _usable_answer(content):
    """Is this text a real answer, or one of the four ways this model fails?

    Empty, repetitive, and raw tool-call syntax are all things that reached a
    real episode at some point. `<function=` catches the same failure in the
    other shape llama.cpp emits it (`<function=browser.open>...`), observed
    2026-08-22 when the request omits `tools` entirely.
    """
    if not content:
        return False
    if "<tool_call>" in content or "<function=" in content:
        return False
    return not _is_degenerate(content)


def synthesize(question):
    """Write the final report on a WRITER model, from the pages actually read.

    Added 2026-08-22, after the first clean end-to-end run produced two
    sub-questions with 15 sources each, natural conclusions at 66 and 49 turns
    -- and no synthesized answer for either. The research went well; the writing
    never happened.

    The cause is not a prompt. OpenResearcher-30B-A3B does not write final
    reports, in any context this repo could construct. Measured that day, all
    with `tools` omitted from the request:

      - prefill "Final answer, no tool calls:" -> the chat template opens the
        assistant turn inside a reasoning block, so the prefill became the first
        tokens of the model's THINKING. It thought, closed </think>, and emitted
        a tool call, which stop=["<tool_call>"] then truncated to nothing. The
        central mechanism of force_answer() was inverted by the template.
      - no prefill / no stop / blunt "do not emit any tool call" -> emitted
        `<tool_call><function=browser.search>...` as plain text anyway.
      - a CLEAN context (no trajectory, three source excerpts, "write prose,
        not a list of links") -> 29,084 characters of `<tool_call>` repeated.
      - Gemma-4-26B-A4B, that same clean prompt -> a correct grounded paragraph
        naming the system, its authors, its venue and its URL.

    So the researcher researches and a writer writes, which is the same division
    of labour enrich_question() arrived at from the opposite direction. This is
    NOT the model answering from memory: the only material in the prompt is text
    this process fetched and the agent read, and the writer is told so. That is
    strictly more grounded than the trajectory-based synthesis it replaces,
    which could and did wander.

    Returns "" if it cannot produce something usable, so callers fall through.
    """
    if not SOURCE_TEXT:
        return ""
    parts, total = [], 0
    for i, (url, text) in enumerate(SOURCE_TEXT.items(), 1):
        title = SOURCES.get(url) or "(untitled)"
        block = f"SOURCE {i} -- {title} ({url})\n{text}"
        if total + len(block) > SOURCE_TEXT_TOTAL:
            # Say so rather than silently writing from a subset: a report built
            # from 9 of 20 sources should not look identical to one built from
            # all of them. Raise DRP_SOURCE_TEXT_TOTAL if your writer's context
            # allows it.
            print(f"[synthesize] source text capped at {SOURCE_TEXT_TOTAL} chars -- "
                  f"{len(SOURCE_TEXT) - len(parts)} of {len(SOURCE_TEXT)} sources "
                  "excluded from the write-up", file=sys.stderr)
            break
        parts.append(block)
        total += len(block)
    msgs = [
        {"role": "system", "content": (
            "You are a research writer. Using ONLY the source excerpts provided, write a "
            "detailed, well-structured report answering the user's question. Name each "
            "specific paper, system, organization or person, with its date, and give the "
            "URL, inline in the sentence making the claim. Use no knowledge beyond the "
            "excerpts -- they are the entire result of an automated research pass and the "
            "only thing you know about this topic. If the excerpts do not settle "
            "something, say so rather than filling the gap. Do not call tools. Write "
            "prose, not a list of links."
        )},
        {"role": "user", "content":
            f"Question: {question}\n\nSource excerpts:\n\n" + "\n\n".join(parts)},
    ]
    try:
        choice = chat(msgs, use_tools=False, temperature=0.3,
                      max_tokens=ANSWER_MAX_TOKENS, endpoint=SYNTH_LLM)
        content = _strip_reasoning(choice["message"])
    except Exception as e:
        print(f"[synthesize] call failed ({e})", file=sys.stderr)
        return ""
    if choice.get("finish_reason") == "length" and content:
        content += ("\n\n[Note: this synthesis was cut off by the answer token limit "
                    "and may be incomplete.]")
    if not _usable_answer(content):
        print(f"[synthesize] unusable output ({len(content)} chars)", file=sys.stderr)
        return ""
    print(f"[synthesize] wrote {len(content)} chars from {len(parts)} sources "
          f"via {SYNTH_LLM}", file=sys.stderr)
    return content


def resolve_answer(messages, question, natural=None):
    """Produce the final answer, cheapest usable route first.

    Order matters. The natural answer is free and is the model's own conclusion;
    it used to be discarded unconditionally (a 2026-08-18 change made for good
    reasons, before _usable_answer() existed to judge it). synthesize() is the
    reliable route but costs a call on another model. force_answer() is kept for
    the single-endpoint case, where it is the only thing left to try.
    """
    # THE grounding gate (2026-08-22), before any of it. SOURCES is populated
    # only by a browser.open that actually returned readable text, so an empty
    # SOURCES means no page was read and any prose produced from here on is
    # recalled, not researched. Every other check in this file asks whether the
    # answer LOOKS usable; nothing asked whether it could possibly be grounded,
    # and a zero-source run reported `budget_spent: False` -- a natural
    # conclusion, the most successful-looking outcome this script can emit. That
    # is the measured Gemma-4-26B-A4B behaviour in README.md ("search, search,
    # search, stop -> answer from memory"), and downstream it becomes podcast
    # narration indistinguishable from real research. Refuse to launder it: say
    # plainly that nothing was read, and let the pipeline drop the sub-question.
    # Lives here, not in force_answer(): this is the one place every answer
    # route passes through.
    if not SOURCES:
        STATE["synthesized"] = False
        print("[answer] refusing: no source was successfully read, so any answer would "
              "come from the model's own knowledge, not research", file=sys.stderr)
        return ("Automated research read no sources for this question, so no grounded "
                "answer could be produced. Nothing here is research output.")
    if _usable_answer(natural):
        print(f"[answer] using the model's own concluding answer ({len(natural)} chars)",
              file=sys.stderr)
        return natural
    if SYNTH_LLM != LLM:
        written = synthesize(question)
        if written:
            return written
    # Guarded like synthesize() above, and for the same reason: everything below
    # this point is the "the run still produced sources, say so honestly" path,
    # and letting an LLM failure here propagate throws that away along with the
    # whole sub-question. chat() re-raises on any 4xx -- including the 400 an
    # over-long context returns, and this call asks for ANSWER_MAX_TOKENS on top
    # of a window that a 120-turn run has already filled -- and raises
    # RuntimeError after three attempts on 5xx. Fourth recurrence of the
    # "completed work must survive a later failure" class; see CLAUDE.md.
    try:
        forced = force_answer(messages)
    except Exception as exc:
        print(f"[answer] force_answer failed ({exc}); falling back", file=sys.stderr)
        forced = ""
    if _usable_answer(forced):
        return forced
    if SYNTH_LLM == LLM:
        # Single-endpoint setups only get here after force_answer() failed; try
        # a clean-context write on the same model rather than giving up. It is
        # the weakest option, hence last, but it costs one call.
        written = synthesize(question)
        if written:
            return written
    STATE["synthesized"] = False
    print("[answer] no usable synthesis; falling back to the source listing",
          file=sys.stderr)
    listing = "\n".join(f"- {t or '(untitled)'}: {u}" for u, t in SOURCES.items())
    return ("Automated research did not produce a synthesized answer within "
            "its turn budget. The following sources were found and read during "
            f"research and may still be useful raw material:\n{listing}")


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

    And none of the three guards -- empty, repetitive, tool-call syntax -- has
    anything to say about the failure that matters most here: a fluent,
    well-structured, entirely ungrounded answer written from the model's own
    memory. This function does NOT check that; its only caller, resolve_answer(),
    gates on it before calling. Anything else that calls this must gate too --
    the guard used to be described here, which made it look like this function
    owned it (comment corrected 2026-08-23).
    """
    msgs = messages + [{"role": "assistant", "content": "Final answer, no tool calls:"}]
    # ANSWER_MAX_TOKENS, not the tool-turn budget. Until 2026-08-22 this call
    # shared the 1200-token cap sized for "emit one tool call", and on a
    # reasoning model several hundred of those go to reasoning_content before a
    # word of the answer appears. The synthesized answer is the entire product
    # of an 80-turn run and it was being truncated to fit a budget set for
    # something else -- consistent with the 216-2114 characters per answer
    # measured in README.md.
    choice = chat(msgs, use_tools=False, stop=["<tool_call>"], temperature=0.3,
                  max_tokens=ANSWER_MAX_TOKENS)
    content = _strip_reasoning(choice["message"])
    if choice.get("finish_reason") == "length" and content:
        # Never checked until 2026-08-22: an answer cut off mid-sentence was
        # accepted as complete and shipped to the episode. Flag it in-band
        # rather than discarding real synthesis over it.
        #
        # `and content` added 2026-08-23: without it, the documented empty-content
        # failure (a reasoning model spends the whole cap inside reasoning_content
        # and returns finish_reason="length" with no answer text at all) made the
        # NOTE ITSELF the answer -- non-empty, no tool-call syntax, one sentence,
        # so _usable_answer() accepted it and the run reported grounded and
        # synthesized. The episode then narrated a sub-question whose entire body
        # was "[Note: this synthesis was cut off...]". An empty answer must fall
        # through to the "say which failure fired" path below, exactly as it does
        # when finish_reason is anything else. synthesize() has always guarded
        # this the same way.
        print("[force_answer] answer hit the token limit and may be truncated",
              file=sys.stderr)
        content = (content + "\n\n[Note: this synthesis was cut off by the answer token "
                   "limit and may be incomplete.]").strip()
    if _usable_answer(content):
        return content
    # Say WHICH failure fired. Both paths produced the same silent fallback
    # until 2026-08-20, and telling them apart from the outside was impossible
    # -- an empty `content` caused by the `</think>` parsing bug looked
    # identical to a model that genuinely had nothing to say, which is exactly
    # why that bug survived two sessions of debugging the wrong layer.
    why = ("empty content (model produced no answer text)" if not content
           else "tool-call syntax instead of prose" if ("<tool_call>" in content
                                                        or "<function=" in content)
           else "degenerate content (repetition detected)")
    print(f"[force_answer] no usable answer: {why}", file=sys.stderr)
    # Returning "" rather than a canned string: resolve_answer() owns the
    # decision about what to do next, and on this model there is a better next
    # step than giving up -- see synthesize().
    return ""


def enrich_question(question):
    """Turn a possibly-sloppy input question into extra research requirements.

    Added 2026-08-18 after a real deep-research-podcast run: an open,
    casually-phrased sub-question ("how does X compare to what other labs
    have done") reliably let the agent take the easy path -- re-describing
    background it had already read rather than actually finding and naming new
    material. The question itself needed to demand more. Real callers (a Hermes
    skill decomposing a vague request, a human typing a quick question) produce
    the casual form routinely.

    USE A GENERAL INSTRUCT MODEL FOR THIS (DRP_ENRICH_LLM_URL). It defaults to
    the research model because that is the one endpoint this script is
    guaranteed to have, but the research model is the wrong tool for the job and
    the measurements are lopsided. OpenResearcher-30B-A3B is post-trained hard
    enough on agentic research that it cannot reliably do meta-work about a
    research question -- the same trait force_answer() exists to fight. Measured
    2026-08-22 on identical prompts:

      - long, careful prompt -> 6024 characters of reasoning, hit the token cap,
        empty content. That is why enrichment "produced no usable rewrite".
      - short prompt -> degenerated into `<tool_call><tool_call>...` spam,
        trying to research the question instead of scoping it.
      - the one time it did return prose, it ANSWERED the question inside the
        brief ("...a free, open-source, decentralized metasearch engine written
        in Python that aggregates results...") -- and the research model, handed
        a user turn that already contained the answer, stopped at turn 0 with
        zero sources.
      - Gemma-4-26B-A4B on the same prompt: 13 seconds, clean, finish_reason
        "stop", no leaked facts.

    The neat part is that this is the exact inverse of the comparison in
    README.md: the general model that will not do research is good at writing
    the brief, and the researcher that will not write briefs is good at
    research. Point this at whatever instruct model you already have resident.

    Best-effort throughout: anything that goes wrong returns the original
    question rather than blocking research on a call that is not the point of
    the run.
    """
    msgs = [
        {"role": "system", "content": (
            "Turn the user's question into a short research brief for a "
            "web-research agent. State only what must be found out and the "
            "standard the answer must meet: trace claims to named papers, "
            "systems, organizations or people with dates; do not restate assumed "
            "background as a finding. Never state a fact about the subject "
            "yourself -- the agent must find it, and asserting it here tells the "
            "agent the work is already done. Output 2-3 imperative sentences, "
            "nothing else."
        )},
        {"role": "user", "content": question},
    ]
    try:
        # stop=["<tool_call>"] for the same reason force_answer() uses it: if
        # DRP_ENRICH_LLM_URL was left pointing at the research model, this is
        # what bounds the tool-call spam described above.
        choice = chat(msgs, use_tools=False, temperature=0.3,
                      max_tokens=ENRICH_MAX_TOKENS, stop=["<tool_call>"],
                      endpoint=ENRICH_LLM)
        content = _strip_reasoning(choice["message"])
    except Exception as e:
        print(f"[enrich] call failed ({e}); researching the question as given",
              file=sys.stderr)
        return question

    why = None
    if choice.get("finish_reason") == "length":
        # A truncated requirement list is worse than none: it can end mid-clause
        # and read as a constraint the agent must satisfy.
        why = "rewrite hit the token limit"
    elif not content:
        why = "no usable rewrite"
    elif "<tool_call>" in content or "</think>" in content:
        why = "rewrite contained tool-call/reasoning syntax"
    elif _is_degenerate(content):
        why = "rewrite was repetitive"
    elif len(content) > 8 * len(question) + 800:
        # A brief that dwarfs the question is not a brief; on this model that
        # shape was an essay that answered the question.
        why = f"rewrite implausibly long ({len(content)} chars)"
    if why:
        # Silent until 2026-08-20: enrichment failing open is by design, but
        # failing open INVISIBLY meant a no-op enrichment was indistinguishable
        # from a question that simply needed no rewriting. It was the former
        # every single time, for the whole life of the feature.
        print(f"[enrich] {why}; researching the question as given", file=sys.stderr)
        return question
    return content


def _emit(question, answer, turns_used, budget_spent, as_json, researched_as=None):
    sources = [{"url": u, "title": t} for u, t in SOURCES.items()]
    if as_json:
        out = {
            "question": question, "answer": answer, "sources": sources,
            "turns_used": turns_used, "budget_spent": budget_spent,
            # Explicit rather than left for the caller to infer from
            # len(sources) (2026-08-22). deep-research-podcast.py drops
            # ungrounded results instead of narrating them, and that decision
            # should read off one unambiguous field, not a convention.
            "grounded": bool(sources),
            # False when `answer` is force_answer()'s canned fallback rather
            # than research prose -- see STATE["synthesized"].
            "synthesized": STATE["synthesized"],
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
    # The original question always leads, with enrichment ATTACHED as
    # requirements rather than REPLACING it (2026-08-22). Enrichment used to
    # substitute its rewrite for the question outright, and the first live run
    # after the guards above went in showed why that is unsafe: asked to rewrite
    # "What is SearXNG and what is it used for?", the rewriter returned a brief
    # that answered it -- "...a free, open-source, decentralized metasearch
    # engine written in Python that aggregates results from multiple search
    # engines..." -- and the research model, handed a user turn that already
    # contained the answer, correctly concluded there was nothing to look up and
    # stopped at turn 0 with zero sources. Enrichment is on by default, so every
    # sub-question was exposed to that. The prompt now forbids answer content
    # (see enrich_question()), and this keeps the question itself in front of the
    # model even when the rewriter leaks something anyway.
    requirements = None if args.no_enrich else enrich_question(question)
    if requirements and requirements != question:
        researched_as = f"{question}\n\nResearch requirements:\n{requirements}"
    else:
        researched_as = question

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
            "finding. Search results alone are not research: you must actually OPEN "
            "and read sources before answering, and page further into a promising "
            "document with cursor=1, cursor=2 rather than stopping at its first "
            "screenful. An answer written from what you already know, without "
            "opening anything, will be discarded."},
        {"role": "user", "content": researched_as},
    ]
    for turn in range(max_turns):
        # Prune before every call, not once at the end: the point is that the
        # request never grows past what the server will accept. See
        # _prune_history() for why an unpruned 120-turn run could not finish.
        messages = _prune_history(messages)
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
            # The model's own concluding turn is now EXAMINED rather than
            # discarded (2026-08-22). Routing every tool-calls-empty turn
            # straight to force_answer() was right in 2026-08-18, when the only
            # alternative was trusting `content` blindly -- but it also threw
            # away good answers, and on this model force_answer() then fails
            # (see synthesize()), so a run that had genuinely concluded emitted
            # a source listing instead of the conclusion it had just written.
            # _usable_answer() is the judgement that was missing.
            natural = _strip_reasoning(msg)
            messages.append({"role": "user", "content":
                "That did not produce a usable answer. Stop researching and "
                "answer now, using only what you have already read. Cite the "
                "URLs you used."})
            _emit(question, resolve_answer(messages, question, natural),
                  turn, False, args.json, researched_as)
            return
        for c in calls:
            fn = c["function"]["name"]
            try:
                args_ = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args_ = {}
            if not args.json:
                print(f"[turn {turn}] {fn}({json.dumps(args_)[:90]})", file=sys.stderr)
            # Guarded 2026-08-23. Tool dispatch returns an error STRING as tool
            # output rather than raising -- the invariant every dispatch function
            # already honours internally (tool_search's "Search failed: ...").
            # The call itself did not: this script has no top-level handler, so
            # a TypeError here exits non-zero and the pipeline's run_research()
            # logs and skips the sub-question, throwing away every turn of
            # research that had already succeeded. And the arguments are model
            # output, not ours: a JSON string `"1"` for cursor -- a shape models
            # emit despite the schema -- raises in _page_offset()'s comparison,
            # and a string topn raises on the results slice. Telling the model
            # what it got wrong lets it retry; crashing costs the whole run.
            try:
                out = DISPATCH.get(fn, lambda a: f"Unknown tool {fn}")(args_)
            except Exception as exc:
                out = (f"{fn} failed: {type(exc).__name__}: {exc}. Check the "
                       f"argument types (cursor and topn must be integers, not "
                       f"strings) and try again.")
                # "[tool]" prefix, not "[turn N]": the pipeline echoes child
                # stderr only for a known set of diagnostic prefixes, so a
                # "[turn N]" line here would be captured and dropped -- the
                # exact silent-diagnostics failure that cost a 35-minute run.
                print(f"[tool] turn {turn}: {fn} raised: {exc}", file=sys.stderr)
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""),
                             "content": out[:TOOL_RESULT_CHARS]})
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
    _emit(question, resolve_answer(messages, question), max_turns, True, args.json,
          researched_as)


main()
