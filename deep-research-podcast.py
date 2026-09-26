#!/usr/bin/env python3
"""Research -> Open Notebook -> long podcast, chained and unattended.

Extracted from a hardware runbook (github.com/sjvrensburg/halo-prep) where this
ran against a local llama-server + SearXNG + Open Notebook stack; see that repo's
docs/07-expansion.md §9.13 for the full origin story, including three real bugs
found running it at production scale. Run this in the BACKGROUND -- it blocks for
as long as research plus podcast generation take, which for a multi-question deep
dive is realistically 30 minutes to a few hours, not something to hold a
conversation turn open for.

    python3 deep-research-podcast.py \
        --episode-name "..." --notebook-name "..." \
        "sub-question 1" "sub-question 2" ...

If the research succeeded and the episode did not, regenerate the episode alone
from the results file the run persisted (see stage (2) below), passing the same
--source-document, if any, again:

    python3 deep-research-podcast.py --episode-name "..." \
        --from-results /tmp/drp-results-<ts>.json

If you're driving this from an agent harness that kills backgrounded child
processes on its own restart (the failure mode that motivated this in the first
place -- see README.md), launch it as a detached unit instead of a shell
background job, e.g. on systemd:

    systemd-run --user --unit=deep-research-podcast-$(date +%s) \
        --setenv=PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        python3 deep-research-podcast.py \
        --episode-name "..." --notebook-name "..." \
        "sub-question 1" "sub-question 2" ...

WHY A SEPARATE SCRIPT, NOT JUST A LONGER openresearcher-run.py CALL. Real depth
needs more than one big query -- a single 100-turn run against one broad question
tends to skim breadth-first and never go deep on any one sub-topic (observed
informally; not something this repo measured rigorously). Splitting the topic into
several focused sub-questions, each researched to its own turn budget, then handed
to Open Notebook as separate grounded sources, is what actually produces the kind
of long/detailed episode this workflow exists for. Decomposing the topic into those
sub-questions is deliberately NOT done here -- it's a judgment call about what the
user actually wants covered, and the caller (an agent skill, a human, whatever has
the surrounding context) does it before invoking this script. This script assumes
it has already been handed good sub-questions and just executes the pipeline.

Stages: (1) optionally start your research backend (see DRP_BACKEND_START_CMD
below) and confirm the LLM + SearXNG endpoints are healthy, (2) run
openresearcher-run.py --json against each sub-question with a large turn budget,
persisting the raw results to /tmp/drp-results-<ts>.json as soon as research
finishes (so a later failure never loses synthesized research, only convenience),
(3) optionally stop the research backend (DRP_BACKEND_STOP_CMD) if it's a separate
on-demand service you don't want resident through podcast generation -- this happens
the moment research ends, in a `finally`, and again from the top-level failure handler,
so a run that dies anywhere never strands a multi-GB model, (4) create an Open Notebook
notebook and add each sub-question's answer as a text source plus every cited URL as a
link source -- for browsing/citation in the Open Notebook UI, not for podcast
generation (see (5)), (5) trigger podcast generation on an episode
profile that takes `content` directly, NOT `notebook_id` -- on the origin box, a
real run's 26-source notebook (4 short synthesized answers + 22 full-text citation
PDFs) totalled 780,063 tokens against a 131,072-token context window and failed
outright; the synthesized answers alone are a few KB and are the actual point of
running OpenResearcher, so generating from them directly is smaller AND more
faithful to "the research" than pulling in every raw source, (6) optionally hand
off to a poller script for completion notification (DRP_POLLER_CMD; the
hermes-skills/open-notebook-podcast/scripts/poll_and_notify.sh in this repo is one
example, written for Hermes Agent + Signal).

OPTIONAL: grounding the episode in an existing document, not just research.
--source-document takes a path to already-converted text (e.g. a PDF run
through `marker` or similar -- this script does no document conversion itself).
That text is added to the notebook as its own source (so it's browsable there)
AND, unlike every other notebook source, is folded directly into the podcast
`content` ahead of the research sections, clearly labelled as the primary
source. This is deliberately different from a plain research sub-question:
open-notebook-podcast's own SKILL.md documents the failure mode where an
attached document reaches only the episode briefing and never the notebook,
so the episode ends up as generic web coverage with the user's actual document
absent. Passing --source-document sidesteps that by construction -- there is
no path through this script where the document is fetched but not narrated.
"""
import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OPENRESEARCHER = os.environ.get(
    "DRP_OPENRESEARCHER_PATH", os.path.join(HERE, "openresearcher-run.py"))
LLM_HEALTH_URL = os.environ.get("DRP_LLM_HEALTH_URL", "http://127.0.0.1:8085/health")
SEARXNG_URL = os.environ.get("DRP_SEARXNG_URL", "http://127.0.0.1:8888/search")
# Optional shell commands to bring your research backend up/down around the
# research phase -- e.g. `systemctl --user start llama-research` if it's an
# on-demand service, or leave both unset if your LLM/SearXNG are always-on.
BACKEND_START_CMD = os.environ.get("DRP_BACKEND_START_CMD", "")
BACKEND_STOP_CMD = os.environ.get("DRP_BACKEND_STOP_CMD", "")
# Optional handoff for completion notification -- receives job_id, episode_name,
# deliver_target as $1 $2 $3. Unset by default: the script just prints the job id
# and exits, leaving polling to whatever's calling it.
POLLER_CMD = os.environ.get("DRP_POLLER_CMD", "")
ON_API = os.environ.get("DRP_OPEN_NOTEBOOK_API", "http://127.0.0.1:5055/api")
# Same agent openresearcher-run.py uses -- see wait_healthy().
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) halo-prep/1.0"
# Wall-clock ceiling for ONE sub-question's research subprocess. Was a hardcoded
# 3600 until 2026-08-22, which sat inside the expected runtime of the default
# 120-turn budget (120 turns at 20-30s/turn is 40-60 minutes before enrichment
# and page fetches) -- so the timeout fired on healthy long runs and destroyed
# the sub-question, since the child's answer only existed on its stdout at exit.
RESEARCH_TIMEOUT = int(os.environ.get("DRP_RESEARCH_TIMEOUT", "10800"))
RESULTS_DIR = os.environ.get("DRP_RESULTS_DIR", "/tmp")
# Below this, a section is marked thin in the content handed to the episode
# model, overriding the briefing's "cover every question in real depth". Not a
# gate -- see build_podcast_content(). Yesterday's healthy sections ran
# 2,693-4,542 chars; the one that prompted this was 366.
THIN_SECTION_CHARS = int(os.environ.get("DRP_THIN_SECTION_CHARS", "1200"))
# Optional gate on --source-document: a command that is run as `CMD <path>` and
# must exit 0 for the run to proceed. Unset by default -- this script has no
# opinion about where a document came from and no knowledge of any particular
# converter. Set it to whatever answers "is this really what it claims to be" on
# your box (see README.md's --source-document section) and the answer becomes an
# exit code instead of an instruction someone has to remember. Added 2026-08-23
# after the third document-substitution incident in two days.
DOC_VERIFY_CMD = os.environ.get("DRP_DOC_VERIFY_CMD", "")
# A poller that hangs must not hold the run open indefinitely.
POLLER_TIMEOUT = int(os.environ.get("DRP_POLLER_TIMEOUT", "14400"))
# Populated by main() as soon as arguments are parsed, so the top-level failure
# handler at the bottom of this file can name the episode it is reporting on.
RUN = {"episode_name": "(unknown episode)", "deliver_target": "signal",
       # Backend lifecycle. "started" flips before DRP_BACKEND_START_CMD runs,
       # so every later exit path knows it owes a stop; "released" makes the
       # stop idempotent across the research-loop finally and the top-level
       # failure handler. See release_research_backend().
       "backend_started": False, "backend_released": False}

DEFAULT_BRIEFING_SUFFIX = (
    "Cover every research question above in real depth. When discussing "
    "comparisons, trends, or findings, name the specific papers, systems, or "
    "organizations behind them (see each question's \"sources consulted\" list) "
    "rather than speaking in generalities."
)
# Appended to EVERY briefing, the caller's --briefing-suffix included, rather
# than living inside DEFAULT_BRIEFING_SUFFIX where a caller passing their own
# suffix would silently drop it (2026-08-22) -- and the documented
# --source-document workflow in README.md passes one. Everything upstream of
# here now guarantees the episode model receives only grounded, synthesized
# material; this is the last link, telling it not to supply from its own
# knowledge what the material does not contain.
GROUNDING_CLAUSE = (
    " Everything you say must come from the material above -- it is the output of "
    "actual research, and it is the only thing you know about this topic. Do not "
    "add facts, names, dates, or figures from your own knowledge, and do not fill "
    "a thin section by elaborating beyond what its sources support; if the "
    "material does not cover something, say so or leave it out."
)
# Was 100 until 2026-08-18: a real comparative sub-question hit this exact cap
# (--max-turns 50 in that run) with good sources found but no synthesis --
# raised the default, and openresearcher-run.py's question-enrichment step
# (enrich_question(), on by default) now spends part of that budget pushing
# toward named, dated sources instead of settling for a quick generality.
DEFAULT_MAX_TURNS = 120


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def api(method, path, body=None, timeout=180, retries=3):
    # timeout was 60s (fixed, no retry) until 2026-08-18: a text-source POST hit
    # it and killed a live run whose 5 research sub-questions had all already
    # succeeded, because Open Notebook was mid-flight embedding a backlog of
    # other sources in the same notebook at the time -- confirmed via docker
    # logs showing concurrent embed_source jobs at the exact failure timestamp.
    # 180s gives real headroom under that kind of load without hiding a
    # genuinely dead API. A raw socket TimeoutError from urllib is NOT an
    # HTTPError, so it was never caught by callers' `except RuntimeError` --
    # retrying here and normalizing to RuntimeError on final failure fixes both
    # the timeout being too short and the wrong exception type propagating.
    data = json.dumps(body).encode() if body is not None else None
    last_exc = None
    for attempt in range(retries):
        req = urllib.request.Request(f"{ON_API}{path}", data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            # 2026-08-22: this used to raise on the FIRST HTTPError regardless of
            # status. A 429, or a 502/503 from Open Notebook (or a reverse proxy
            # in front of it) while it is busy embedding a backlog, is the same
            # transient overload condition the 180s timeout retry above exists
            # for -- it just surfaces as a status code instead of a socket
            # timeout. Retry those; keep failing fast on a 4xx, which retrying
            # verbatim cannot fix.
            if e.code < 500 and e.code != 429:
                raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from e
            last_exc = RuntimeError(f"{method} {path} -> {e.code}: {detail[:500]}")
            if attempt < retries - 1:
                log(f"  api {method} {path} attempt {attempt + 1}/{retries} failed "
                    f"({e.code}); retrying")
                time.sleep(5 * (attempt + 1))
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            last_exc = e
            if attempt < retries - 1:
                log(f"  api {method} {path} attempt {attempt + 1}/{retries} failed "
                    f"({e!r}); retrying")
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{method} {path} -> no response after {retries} attempts: {last_exc!r}")


def wait_healthy(url, tries=60, interval=5):
    for attempt in range(tries):
        try:
            # The User-Agent matters (2026-08-22). This used to call urlopen()
            # bare, with python-urllib's default agent -- the exact agent
            # openresearcher-run.py's _get() sets a browser string to work
            # around, with the comment "SearXNG and most sites reject the
            # default python-urllib agent". So the SearXNG preflight below could
            # fail against an instance the research loop would have queried
            # perfectly well, aborting the whole pipeline before it did any
            # work. Same agent here, same result there.
            urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT}), timeout=5)
            return True
        except Exception:
            if attempt < tries - 1:
                time.sleep(interval)
    return False


def preflight_warnings():
    """Say up front when the run is configured to fail late.

    Both of these produce a run that researches perfectly and then has nothing
    to show for it, 30+ minutes later. Cheap to check, expensive to discover.
    """
    synth = os.environ.get("DRP_SYNTH_LLM_URL", "")
    enrich = os.environ.get("DRP_ENRICH_LLM_URL", "")
    if not synth:
        log("WARNING: DRP_SYNTH_LLM_URL is unset, so the final write-up will run on "
            "the research model. Measured 2026-08-22: OpenResearcher cannot write a "
            "report -- it emits tool-call syntax instead of prose in every context "
            "tried -- so sub-questions will very likely end with no synthesis and be "
            "dropped from the episode. Point it at any general instruct endpoint.")
    if not enrich:
        log("WARNING: DRP_ENRICH_LLM_URL is unset; question enrichment will run on the "
            "research model, which tends to answer the question instead of rewriting "
            "it. Pass --no-enrich or set the variable.")
    if not BACKEND_START_CMD:
        log(f"note: DRP_BACKEND_START_CMD is unset -- expecting {LLM_HEALTH_URL} to be "
            "healthy already; if it is an on-demand service this run will wait and "
            "then fail.")


def ensure_research_backend():
    # DRP_BACKEND_START_CMD is your business -- e.g. `systemctl --user start
    # llama-research` if the LLM is an on-demand service, or leave it unset if
    # your LLM/SearXNG are already always-on. Either way, both endpoints get a
    # real health check afterward rather than trusting the start command alone.
    # Set BEFORE the start command, not after (2026-08-22): from here on the
    # backend must be released on every exit path, and a start command that
    # partially succeeded -- or succeeded but whose health check then failed --
    # is exactly the case that used to strand it. See release_research_backend().
    RUN["backend_started"] = True
    if BACKEND_START_CMD:
        log(f"starting research backend: {BACKEND_START_CMD!r}")
        subprocess.run(shlex.split(BACKEND_START_CMD), check=True)
    if not wait_healthy(LLM_HEALTH_URL):
        raise RuntimeError(f"LLM endpoint ({LLM_HEALTH_URL}) did not become healthy in time")

    searx_check_url = f"{SEARXNG_URL}?q=x&format=json"
    # tries=6, not 1 (2026-08-22): if DRP_BACKEND_START_CMD just brought SearXNG
    # up alongside the LLM, a single immediate probe gives it no time to start
    # listening, and the pipeline aborts before researching anything. The LLM
    # gets 60 tries; SearXNG deserves more than zero patience.
    if not wait_healthy(searx_check_url, tries=6):
        raise RuntimeError(
            f"SearXNG ({SEARXNG_URL}) is not reachable -- start it yourself or set "
            "DRP_BACKEND_START_CMD to bring it up alongside the LLM")
    log("research backend ready")


def release_research_backend():
    """Stop the on-demand research backend. Safe to call more than once.

    Idempotent and reachable from every exit path since 2026-08-22. It used to
    be called from exactly one place -- a `finally` around the research loop --
    with ensure_research_backend() sitting OUTSIDE that try. So a preflight that
    started the LLM and then failed its own health checks (SearXNG unreachable
    being the obvious one) left a multi-GB model resident with nothing left
    running to stop it. On a memory-tight box that is not a tidiness problem:
    an abandoned backend gets OOM-killed, systemd restarts it, and it is killed
    again -- observed twice in one day -- while also crowding out whatever the
    machine is actually meant to be doing.

    Only releases if ensure_research_backend() was reached, so an argument or
    document-loading error before that point does not run a stop command
    against a service this process never touched.
    """
    if RUN["backend_released"] or not RUN["backend_started"]:
        return
    RUN["backend_released"] = True
    # Best-effort -- a failure here shouldn't abort a podcast job whose
    # research already succeeded. No-op if DRP_BACKEND_STOP_CMD isn't set.
    if not BACKEND_STOP_CMD:
        return
    try:
        subprocess.run(shlex.split(BACKEND_STOP_CMD), check=True)
        log(f"stopped research backend: {BACKEND_STOP_CMD!r}")
    except Exception as e:
        log(f"warning: could not stop research backend cleanly: {e}")


def run_research(question, max_turns):
    log(f"researching: {question!r} (max {max_turns} turns)")
    # The child's stderr goes to a FILE, not a pipe (2026-08-29). It used to be
    # captured, which meant its per-turn lines -- `[turn 37] browser.open(...)`, the
    # only evidence a sub-question is still moving -- did not exist anywhere until the
    # sub-question ENDED, 15-40 minutes later. A healthy run and a wedged one looked
    # identical for that whole time, in the journal and to anything watching it; the
    # progress-monitoring notes in hermes-skills work around exactly this absence, and
    # a web front end cannot work around it at all. Writing them out as they happen
    # also means the full diagnostics survive the run instead of the filtered handful
    # echoed below. stdout is deliberately untouched: it is the JSON interface between
    # the two scripts, and one stray line on it breaks the pipeline.
    stderr_path = os.path.join(RESULTS_DIR, f"research-{int(time.time())}-{os.getpid()}.log")
    stderr_file = None
    try:
        stderr_file = open(stderr_path, "w")
        log(f"  research log: {stderr_path}")
    except OSError as e:
        # An unwritable results directory must cost the live view, never the research.
        log(f"  warning: could not open {stderr_path} ({e}); capturing the researcher's "
            f"output in memory instead")
    try:
        proc = subprocess.run(
            [sys.executable, OPENRESEARCHER, "--json", "--max-turns", str(max_turns), question],
            stdout=subprocess.PIPE, stderr=(stderr_file or subprocess.PIPE),
            text=True, timeout=RESEARCH_TIMEOUT)
    finally:
        if stderr_file is not None:
            stderr_file.close()
    stderr_text = proc.stderr
    if stderr_text is None:
        try:
            with open(stderr_path, errors="replace") as f:
                stderr_text = f.read()
        except OSError:
            stderr_text = ""
    if proc.returncode != 0:
        raise RuntimeError(f"openresearcher-run.py failed for {question!r}: {stderr_text[-2000:]}")
    out = proc.stdout.strip()
    if not out:
        raise RuntimeError(f"openresearcher-run.py produced no output for {question!r}: "
                           f"{stderr_text[-2000:]}")
    result = json.loads(out.splitlines()[-1])
    log(f"  -> {len(result['sources'])} sources, "
        f"{'forced' if result['budget_spent'] else 'natural'} conclusion "
        f"after {result['turns_used']} turns, "
        f"{'synthesized' if result.get('synthesized', True) else 'NO SYNTHESIS'}")
    # Surface the child's own diagnostics on SUCCESS too (2026-08-22). stderr was
    # only ever echoed when the subprocess failed, so a run that "succeeded" with
    # no synthesized answer said nothing about why -- the [force_answer] /
    # [synthesize] / [enrich] lines explaining it were captured and dropped. That
    # cost a full 35-minute two-question run to rediagnose by hand.
    for line in (stderr_text or "").splitlines():
        if line.startswith(("[force_answer]", "[synthesize]", "[enrich]", "[answer]",
                            "[chat]", "[tool]")):
            log(f"     {line}")
    # THE grounding gate, pipeline side (2026-08-22). `sources` counts pages the
    # model actually opened and read, and until now it was only ever LOGGED --
    # never checked. A sub-question that searched, read nothing, and wrote a
    # fluent answer from the model's own memory passed straight through to the
    # episode, indistinguishable from real research. `grounded` is set by
    # openresearcher-run.py's _emit(); the len() check keeps this working
    # against an older result file replayed by hand.
    if not result.get("grounded", bool(result.get("sources"))):
        raise RuntimeError(
            "no source was read for this sub-question, so its answer would be the "
            "model's own knowledge rather than research -- discarding it")
    return result


def build_notebook(notebook_name, notebook_description, results, document=None):
    log(f"creating notebook {notebook_name!r}")
    nb = api("POST", "/notebooks", {"name": notebook_name, "description": notebook_description})
    nb_id = nb["id"]

    if document is not None:
        try:
            api("POST", "/sources/json", {
                "type": "text", "title": document["title"], "content": document["text"],
                "notebooks": [nb_id], "embed": True,
            })
        except RuntimeError as e:
            # Not fatal -- the document still reaches the episode via
            # build_podcast_content() regardless of whether it also made it
            # into the notebook for browsing. Losing the browsable copy is a
            # real but strictly smaller problem than losing it from the
            # narration, which is the failure this flag exists to prevent.
            log(f"  warning: could not add source document to notebook: {e}")

    seen_urls = set()
    for r in results:
        body = (f"Research question: {r['question']}\n\n{r['answer']}")
        try:
            api("POST", "/sources/json", {
                "type": "text", "title": r["question"][:200], "content": body,
                "notebooks": [nb_id], "embed": True,
            })
        except RuntimeError as e:
            # 2026-08-18: this call used to be unguarded, and a single timeout
            # here (Open Notebook busy embedding a backlog of other sources)
            # killed the whole pipeline after all research had already
            # succeeded -- the answer above is safe (write_results_json() in
            # main() persisted it before this function ever ran), but the
            # notebook itself would be missing this question's synthesized
            # answer. Log and keep going rather than losing every other
            # result too; the citation links below are independent of this
            # text source and are still worth adding.
            log(f"  warning: could not add text source for {r['question'][:60]!r}: {e}")
        for s in r["sources"]:
            if not s["url"] or s["url"] in seen_urls:
                continue
            seen_urls.add(s["url"])
            try:
                api("POST", "/sources/json", {
                    "type": "link", "url": s["url"], "title": s["title"] or s["url"],
                    "notebooks": [nb_id], "embed": True, "async_processing": True,
                })
            except RuntimeError as e:
                # A dead/blocked link mid-research is normal; don't let one
                # bad URL abort notebook construction.
                log(f"  warning: could not add source {s['url']}: {e}")
    log(f"notebook has {len(results)} research-note sources + {len(seen_urls)} link sources")
    return nb_id


def build_podcast_content(results, document=None):
    # Generation is driven by this, NOT notebook_id, since 2026-08-18. The
    # notebook (build_notebook() above) still gets every cited link as a
    # full-text source for browsing/citation in the Open Notebook UI, but
    # those raw sources -- academic PDFs, often hundreds of embedding chunks
    # each -- are what blew a real run's context: 26 sources (4 synthesized
    # answers + 22 raw citations) totalled 780,063 tokens against the :8088
    # writer's 131,072 window. The synthesized answers themselves are small (measured
    # 216-2114 chars each on that same run) -- they ARE the point of running
    # OpenResearcher at all, so generating from them directly is both far
    # smaller and more faithful to "the research", not a lossy workaround.
    #
    # Each answer is followed by a compact "sources consulted" listing
    # (title + URL only, no full text) -- added 2026-08-18. Without it, the
    # episode-generation model only ever saw prose with no named sources
    # attached, so even when openresearcher-run.py's answer named a specific
    # paper, there was nothing here confirming what that paper actually was or
    # linking it to a citable source; a real episode's dialogue ended up
    # re-explaining background it already had rather than naming the sources
    # research had genuinely found. This is a handful of short lines per
    # sub-question -- nowhere near the raw-full-text scale that caused the
    # 780,063-token failure below.
    # A sub-question with no synthesis is EXCLUDED from the episode entirely
    # (2026-08-22), not narrated as an empty section. It used to be included:
    # `## {question}` followed by force_answer()'s canned "did not produce a
    # synthesized answer, here are the URLs" string. Read that from the
    # transcript model's side -- a heading naming an interesting topic, no
    # material under it, and a briefing (see trigger_podcast()'s default
    # briefing_suffix) instructing it to cover every question in real depth and
    # name specific papers and organizations. Handed a topic, no evidence, and
    # an order to be specific, a capable model produces specifics. That is a
    # fabrication generator, and it is the likeliest route by which "podcast
    # backed by the model's own knowledge" happened while every research-side
    # check passed. The notebook still keeps these for inspection.
    usable = [r for r in results
              if r.get("synthesized", True) and r.get("grounded", bool(r.get("sources")))]
    dropped = len(results) - len(usable)
    if dropped:
        log(f"  {dropped} sub-question(s) excluded from the episode (no grounded "
            "synthesis); they remain in the notebook")
    if not usable and document is None:
        raise RuntimeError(
            "No sub-question produced a grounded, synthesized answer, and no "
            "--source-document was given -- there is nothing to narrate that would "
            "not be the episode model's own knowledge. Refusing to generate.")
    results = usable

    parts = []
    if document is not None:
        # Deliberately first and clearly labelled: the document is the
        # primary source the research sub-questions exist to contextualise,
        # not one more source among equals. Ordering it last would bury it
        # under whatever the episode-profile's model reads as the strongest
        # signal for what to open with.
        parts.append(f"# Source document: {document['title']}\n\n{document['text']}")
    for r in results:
        section = f"## {r['question']}\n\n{r['answer']}"
        # A section can be honestly short: on a topic only months old there may
        # be little that any source actually settles, and the writer is
        # instructed to say so rather than fill the gap. One 2026-08-24
        # sub-question synthesized to 366 characters from 24 sources that way.
        # The danger is the global briefing, which tells the episode model to
        # cover every question "in real depth" and name specifics -- aimed at a
        # two-sentence section, that is an instruction to invent the depth.
        # Deliberately NOT a length gate that drops the section: excluding an
        # honest thin answer would hide a real finding and reward padding.
        # Mark it instead, so the briefing's demand is overridden locally.
        if len(r["answer"]) < THIN_SECTION_CHARS:
            section += ("\n\n[Note to the narrators: research found little on this "
                        "question and the summary above is all of it. Cover it briefly "
                        "and move on. Do not expand it, do not add examples, names, "
                        "dates or figures from your own knowledge, and do not treat "
                        "its brevity as a gap to fill -- that the evidence is thin is "
                        "itself the finding, and saying so is better than inventing "
                        "the rest.]")
        srcs = [s for s in r.get("sources", []) if s.get("url")]
        if srcs:
            # Split by whether the source's text actually reached the writer.
            # `used` is absent on results from an older openresearcher-run.py or
            # a replayed results file, and then every source is listed as before
            # rather than silently demoted to "read".
            fmt = lambda s: f"- {s['title'] or s['url']} ({s['url']})"
            if any("used" in s for s in srcs):
                cited = [s for s in srcs if s.get("used")]
                extra = [s for s in srcs if not s.get("used")]
            else:
                cited, extra = srcs, []
            if cited:
                section += ("\n\nSources consulted for this question:\n"
                            + "\n".join(fmt(s) for s in cited))
            if extra:
                # 14 of 24 on one 2026-08-24 question. Listing those under
                # "sources consulted" made the write-up look like it rested on
                # material no sentence of it could have seen.
                section += ("\n\nAlso read, but not part of the write-up above "
                            "(do not attribute claims to these):\n"
                            + "\n".join(fmt(s) for s in extra))
        parts.append(section)
    return "\n\n---\n\n".join(parts)


def trigger_podcast(content, episode_name, briefing_suffix, profile, speakers):
    # Both must be episode/speaker profiles that already exist in your Open
    # Notebook instance and take `content` (not `notebook_id`) as generation
    # input -- see README.md's "Open Notebook setup" section for what these
    # need. The origin box kept two twin profiles, a natural-voice one and a
    # faster one, and switched between them with --fast; that's a convention
    # worth keeping but the names themselves are just examples.
    log(f"triggering podcast generation ({profile} profile, content={len(content)} chars)...")
    resp = api("POST", "/podcasts/generate", {
        "episode_profile": profile,
        "speaker_profile": speakers,
        "episode_name": episode_name,
        "content": content,
        "briefing_suffix": briefing_suffix,
    })
    job_id = resp["job_id"]
    log(f"job_id={job_id}")
    return job_id


def verify_source_document(path):
    """Gate --source-document behind DRP_DOC_VERIFY_CMD, if one is configured.

    Added 2026-08-23, after the third document-substitution incident in two
    days. The pattern is always the same: the converted document at the path
    everything downstream reads is not what it claims to be. Twice it was plain
    `pdftotext` output written into the converter's own output path; the third
    time an agent decided the converter had stalled -- it had not, it finished
    twenty minutes later -- and transcribed the PDF itself. That last one is the
    dangerous shape: an LLM transcription carries real headings, tables and
    math, so no check on the CONTENT can tell it from a real extraction, and it
    can silently alter an equation or a number in a document this pipeline then
    narrates as the user's own paper.

    Every previous fix was an instruction in a skill file saying to check first,
    and each was followed by an agent that did not. So the check moves here,
    where it is an exit code rather than a paragraph.

    Deliberately a COMMAND, not a built-in rule: this script does not convert
    documents, has no knowledge of any particular converter, and must stay
    usable on a box with a different one. It asks a question and honours the
    answer.

    Fails CLOSED, and that is the whole point. An unset variable means no gate
    was asked for and the document passes -- but once one is configured, a
    verifier that is missing, unrunnable, or slow is a verification that did not
    happen, and this refuses on all three. A gate that opens when it breaks is
    not a gate.
    """
    if not DOC_VERIFY_CMD:
        return
    log(f"verifying source document with {DOC_VERIFY_CMD!r}...")
    try:
        proc = subprocess.run(shlex.split(DOC_VERIFY_CMD) + [path],
                              capture_output=True, text=True, timeout=120)
    except Exception as e:
        raise RuntimeError(
            f"--source-document verification could not run ({type(e).__name__}: {e}). "
            f"DRP_DOC_VERIFY_CMD is set, so this run requires a verified document and "
            f"cannot assume one. Fix the verifier or unset the variable deliberately.")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise RuntimeError(
            f"--source-document {path!r} FAILED verification (exit {proc.returncode}). "
            f"Refusing to narrate a document whose provenance could not be confirmed.\n"
            f"{detail}")
    log("  source document verified")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("questions", nargs="*",
                    help="one or more research sub-questions (omit with --from-results)")
    p.add_argument("--episode-name", required=True)
    p.add_argument("--notebook-name", help="required unless --from-results")
    p.add_argument("--notebook-description", default="")
    p.add_argument("--briefing-suffix", default="",
                    help="passed to Open Notebook's podcast briefing -- what to focus on")
    p.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    p.add_argument("--deliver-target", default="signal",
                    help="passed through to DRP_POLLER_CMD as $3, if set")
    p.add_argument("--episode-profile", default=os.environ.get("DRP_EPISODE_PROFILE", "deep_dive"),
                    help="Open Notebook episode profile to generate against "
                         "(default: $DRP_EPISODE_PROFILE or 'deep_dive')")
    p.add_argument("--speaker-profile", default=os.environ.get("DRP_SPEAKER_PROFILE", "tech_experts"),
                    help="Open Notebook speaker profile to pair with --episode-profile "
                         "(default: $DRP_SPEAKER_PROFILE or 'tech_experts')")
    p.add_argument("--fast-episode-profile",
                    default=os.environ.get("DRP_FAST_EPISODE_PROFILE", ""))
    p.add_argument("--fast-speaker-profile",
                    default=os.environ.get("DRP_FAST_SPEAKER_PROFILE", ""))
    p.add_argument("--fast", action="store_true",
                    help="use --fast-episode-profile/--fast-speaker-profile instead of the "
                         "defaults above, if you keep a faster/lower-quality twin profile "
                         "for when a run was explicitly asked for quickly.")
    p.add_argument("--source-document",
                    help="path to already-converted text/markdown (e.g. a PDF run through "
                         "marker) to ground the episode in directly -- see the module "
                         "docstring's OPTIONAL section. This script does not convert "
                         "documents itself.")
    p.add_argument("--source-document-title", default="",
                    help="defaults to the --source-document filename if unset")
    p.add_argument("--from-results", metavar="DRP_RESULTS_JSON",
                    help="skip research and the notebook: regenerate the episode from a "
                         "drp-results-<ts>.json an earlier run persisted. For a run whose "
                         "research succeeded and whose episode failed. Pass the same "
                         "--source-document again if the original run had one; it is "
                         "not in the results file.")
    p.add_argument("--detach", action="store_true",
                    help="re-launch this same invocation as a detached systemd user unit "
                         "and return immediately, printing the unit name and log path. "
                         "Use this from ANY agent harness: the run takes 30 minutes to "
                         "hours, and a foreground call will be killed by the harness's "
                         "own tool timeout (observed 2026-08-22: a Hermes terminal call "
                         "killed a run 60 seconds in). Every DRP_* variable in the "
                         "current environment is forwarded to the unit.")
    args = p.parse_args()
    # --from-results added 2026-09-26. A run's research took 2h20m and
    # succeeded, and then Open Notebook failed the episode on one malformed
    # transcript segment. There was no way to regenerate from the persisted
    # results, so the agent re-triggered generation by hand with notebook_id
    # instead of content. That is the path trigger_podcast() exists to avoid,
    # and Open Notebook, unable to read the notebook, substituted the literal
    # "Notebook ID: <id>" as the whole content: an episode from nothing that
    # would have sounded like research. Resuming belongs here, where the
    # content is built the same way both times.
    if args.from_results:
        if args.questions:
            p.error("--from-results takes no sub-questions: they are in the results file")
    elif not args.questions:
        p.error("give at least one sub-question, or --from-results")
    elif not args.notebook_name:
        p.error("--notebook-name is required unless --from-results")
    if args.detach and not os.environ.get("DRP_DETACHED"):
        # Self-backgrounding, added 2026-08-22. The skill documentation told the
        # calling agent three separate times to wrap this in `systemd-run`; it
        # ran it in the foreground anyway and the harness killed the run after
        # 60 seconds. Relying on an agent to remember the launch idiom is a bet
        # this pipeline keeps losing, so the script now owns it: --detach
        # re-executes this exact invocation as a transient unit and returns.
        unit = f"deep-research-podcast-{int(time.time())}"
        logfile = os.path.join(RESULTS_DIR, f"{unit}.log")
        passthrough = [a for a in sys.argv[1:] if a != "--detach"]
        env = [f"--setenv={k}={v}" for k, v in os.environ.items()
               if k.startswith("DRP_")]
        env += [f"--setenv=DRP_DETACHED=1", f"--setenv=PATH={os.environ.get('PATH','')}"]
        cmd = (["systemd-run", "--user", f"--unit={unit}", "--collect"] + env
               + [f"--property=StandardOutput=append:{logfile}",
                  f"--property=StandardError=append:{logfile}",
                  sys.executable, os.path.abspath(__file__)] + passthrough)
        subprocess.run(cmd, check=True)
        print(f"detached as {unit}")
        print(f"log: {logfile}")
        print(f"follow with: journalctl --user -f -u {unit}  (or: tail -f {logfile})")
        return
    RUN["episode_name"] = args.episode_name
    RUN["deliver_target"] = args.deliver_target

    document = None
    if args.source_document:
        verify_source_document(args.source_document)
        with open(args.source_document, encoding="utf-8") as f:
            doc_text = f.read().strip()
        if not doc_text:
            raise RuntimeError(f"--source-document {args.source_document!r} is empty")
        document = {
            "title": args.source_document_title or os.path.basename(args.source_document),
            "text": doc_text,
        }
        log(f"source document loaded: {document['title']!r} ({len(doc_text)} chars)")
        if len(doc_text) > 60000:
            # Not a hard limit -- your episode profile's model and its
            # max_tokens override decide what actually fits, and that's not
            # something this script can know. This is a sanity check, not
            # enforcement: past this length you're trusting your own context
            # budget, not this script's judgment.
            log("  warning: that's long enough to risk the same context-overflow failure "
                "mode as Bug 2 below, depending on your model's context window -- consider "
                "trimming to the sections you actually want narrated")

    if args.from_results:
        with open(args.from_results, encoding="utf-8") as f:
            results = json.load(f)
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"--from-results {args.from_results!r} holds no results")
        log(f"resuming from {args.from_results} ({len(results)} sub-question results); "
            "research and the notebook are skipped -- the original run's notebook "
            "already exists")
        generate_episode(args, results, document)
        return

    results = []
    # ensure_research_backend() moved INSIDE the try 2026-08-22. It was the one
    # call that could start the backend and then raise with nothing arranged to
    # stop it again -- see release_research_backend().
    try:
        preflight_warnings()
        ensure_research_backend()
        for q in args.questions:
            try:
                results.append(run_research(q, args.max_turns))
            except Exception as e:
                # Was a bare list comprehension until 2026-08-18: a real run's
                # second sub-question crashed openresearcher-run.py entirely
                # (an uncaught SearXNG error inside tool_search(), separately
                # fixed there), and because every question's result lived only
                # in that one comprehension's return value, the crash also
                # discarded the FIRST sub-question's already-completed
                # research -- nothing had been appended anywhere yet. Same
                # class of failure as the api()-retry fix above, just not
                # covered by it: append as each question finishes, so a later
                # failure only costs that one sub-question, not every one
                # that already succeeded.
                log(f"  warning: research failed for {q[:60]!r}, skipping this "
                    f"sub-question and continuing with the rest: {e}")
    finally:
        release_research_backend()

    if not results:
        raise RuntimeError("Every sub-question failed research -- nothing to build a podcast from.")

    # 2026-08-18: research used to live only in this process's memory until it
    # reached Open Notebook. A single timed-out /sources/json call after all 5
    # sub-questions had already succeeded killed the process and took an
    # entire synthesized answer (never printed anywhere, never on disk) with
    # it -- unrecoverable except by re-running that sub-question from
    # scratch. Writing it out here means a crash anywhere after this line
    # loses at most convenience, never research.
    results_path = os.path.join(RESULTS_DIR, f"drp-results-{int(time.time())}.json")
    try:
        with open(results_path, "w") as f:
            json.dump(results, f, indent=2)
        log(f"research results persisted to {results_path}")
    except OSError as e:
        # Guarded 2026-08-22. This is the one line whose entire purpose is "a
        # crash after here loses nothing", and an unguarded failure AT it lost
        # everything -- an unwritable or full /tmp taking down a run whose
        # research had all succeeded. Dump to stdout instead so the answers are
        # at least in the journal, and carry on to the episode.
        log(f"warning: could not persist results to {results_path}: {e}")
        log("research results follow inline as a fallback:")
        log(json.dumps(results))

    try:
        build_notebook(args.notebook_name,
                       args.notebook_description or f"Deep research: {args.episode_name}",
                       results, document=document)
    except Exception as e:
        # Guarded 2026-08-22. build_notebook() protects each /sources/json POST
        # individually but not POST /notebooks, and the call site protected
        # nothing -- so a failure creating the notebook aborted the run before
        # trigger_podcast(), losing the episode. The episode does not need the
        # notebook: build_podcast_content() takes `results` directly, and this
        # file's own docstring calls the notebook "for browsing/citation in the
        # Open Notebook UI, not for podcast generation". Losing the browsable
        # copy is strictly smaller than losing the deliverable.
        log(f"warning: could not build the notebook ({e}) -- continuing to episode "
            "generation, which does not depend on it")
    generate_episode(args, results, document)


def generate_episode(args, results, document):
    """Content -> Open Notebook job -> poller. Shared by a full run and --from-results."""
    content = build_podcast_content(results, document=document)
    if args.fast:
        profile = args.fast_episode_profile or args.episode_profile
        speakers = args.fast_speaker_profile or args.speaker_profile
    else:
        profile, speakers = args.episode_profile, args.speaker_profile
    job_id = trigger_podcast(content, args.episode_name,
                             (args.briefing_suffix or DEFAULT_BRIEFING_SUFFIX)
                             + GROUNDING_CLAUSE,
                             profile, speakers)

    notify(job_id, args.episode_name, args.deliver_target)


def notify(job_id, episode_name, deliver_target, failed=False):
    """Hand off for completion notification -- on failure as well as success.

    POLLER_CMD used to be invoked only after a successful trigger_podcast(),
    and main() was called bare at module scope with no handler, so every
    failure mode ended as a silent dead systemd unit -- the README's fifth bug
    identified exactly this ("no notification -- success or failure -- was ever
    sent; the only way to know it died was to check systemctl") and only the
    crash that prompted it got fixed. A failed run passes job_id="failed", so a
    poller that cannot interpret it still fires and still tells someone.
    """
    if not POLLER_CMD:
        log(f"{'FAILED' if failed else 'done'}. job_id={job_id} -- poll Open Notebook "
            "yourself or set DRP_POLLER_CMD next time to hand off notification "
            "automatically.")
        return
    log("handing off for notification...")
    try:
        subprocess.run(shlex.split(POLLER_CMD) + [job_id, episode_name, deliver_target],
                       timeout=POLLER_TIMEOUT)
    except Exception as e:
        # Never let the notifier be the thing that fails the run: by this point
        # the episode is already generating on Open Notebook's side.
        log(f"warning: notification handoff failed: {e}")


def _terminate(signum, _frame):
    """Turn a signal into an exception so the handler below actually runs.

    Added 2026-08-23. The whole point of release_research_backend() is that a
    run which dies anywhere never strands a multi-GB on-demand model -- but the
    handler below caught `Exception`, and the most likely way one of these runs
    ends early is not an exception at all. Runs are launched detached as
    transient systemd units (--detach), so `systemctl --user stop <unit>` sends
    SIGTERM, which by default terminates the process with nothing unwound: no
    release, no notification. Ctrl-C on a foreground run had the same gap
    (KeyboardInterrupt is not an Exception). Deliberately minimal -- raise and
    get out of the handler; anything more here risks hanging the shutdown it
    exists to make clean.
    """
    raise SystemExit(f"terminated by signal {signum}")


signal.signal(signal.SIGTERM, _terminate)
signal.signal(signal.SIGHUP, _terminate)

try:
    main()
# BaseException, not Exception (2026-08-23): SystemExit from _terminate above,
# and KeyboardInterrupt, must release the backend too. See _terminate().
except BaseException as exc:
    # Top-level handler added 2026-08-22 -- see notify(). Re-raise after
    # notifying so the exit status and traceback still reach the journal.
    log(f"FAILED: {type(exc).__name__}: {exc}")
    # Before anything else: do not leave an on-demand model resident because the
    # run died. Idempotent, so the normal path having already released it is
    # fine, and it covers the failures the research loop's own finally cannot --
    # a preflight that raised, or anything between research and the episode.
    try:
        release_research_backend()
    except Exception as release_exc:
        log(f"warning: could not release the research backend: {release_exc}")
    try:
        notify("failed", RUN["episode_name"], RUN["deliver_target"], failed=True)
    except Exception as notify_exc:
        log(f"warning: could not send failure notification: {notify_exc}")
    raise
