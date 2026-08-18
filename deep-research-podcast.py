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
(3) create an Open Notebook notebook and add each sub-question's answer as a text
source plus every cited URL as a link source -- for browsing/citation in the Open
Notebook UI, not for podcast generation (see (5)), (4) optionally stop the research
backend (DRP_BACKEND_STOP_CMD) if it's a separate on-demand service you don't want
resident through podcast generation, (5) trigger podcast generation on an episode
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
            raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from e
        except (TimeoutError, urllib.error.URLError, ConnectionError) as e:
            last_exc = e
            if attempt < retries - 1:
                log(f"  api {method} {path} attempt {attempt + 1}/{retries} failed "
                    f"({e!r}); retrying")
                time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{method} {path} -> no response after {retries} attempts: {last_exc!r}")


def wait_healthy(url, tries=60, interval=5):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except Exception:
            time.sleep(interval)
    return False


def ensure_research_backend():
    # DRP_BACKEND_START_CMD is your business -- e.g. `systemctl --user start
    # llama-research` if the LLM is an on-demand service, or leave it unset if
    # your LLM/SearXNG are already always-on. Either way, both endpoints get a
    # real health check afterward rather than trusting the start command alone.
    if BACKEND_START_CMD:
        log(f"starting research backend: {BACKEND_START_CMD!r}")
        subprocess.run(shlex.split(BACKEND_START_CMD), check=True)
    if not wait_healthy(LLM_HEALTH_URL):
        raise RuntimeError(f"LLM endpoint ({LLM_HEALTH_URL}) did not become healthy in time")

    searx_check_url = f"{SEARXNG_URL}?q=x&format=json"
    if not wait_healthy(searx_check_url, tries=1):
        raise RuntimeError(
            f"SearXNG ({SEARXNG_URL}) is not reachable -- start it yourself or set "
            "DRP_BACKEND_START_CMD to bring it up alongside the LLM")
    log("research backend ready")


def release_research_backend():
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
    proc = subprocess.run(
        [sys.executable, OPENRESEARCHER, "--json", "--max-turns", str(max_turns), question],
        capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f"openresearcher-run.py failed for {question!r}: {proc.stderr[-2000:]}")
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    log(f"  -> {len(result['sources'])} sources, "
        f"{'forced' if result['budget_spent'] else 'natural'} conclusion "
        f"after {result['turns_used']} turns")
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
    # answers + 22 raw citations) totalled 780,063 tokens against the Mentor's
    # 131,072 window. The synthesized answers themselves are small (measured
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
        srcs = [s for s in r.get("sources", []) if s.get("url")]
        if srcs:
            listing = "\n".join(f"- {s['title'] or s['url']} ({s['url']})" for s in srcs)
            section += f"\n\nSources consulted for this question:\n{listing}"
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


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("questions", nargs="+", help="one or more research sub-questions")
    p.add_argument("--episode-name", required=True)
    p.add_argument("--notebook-name", required=True)
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
    args = p.parse_args()

    document = None
    if args.source_document:
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

    ensure_research_backend()
    results = []
    try:
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
    results_path = f"/tmp/drp-results-{int(time.time())}.json"
    with open(results_path, "w") as f:
        json.dump(results, f, indent=2)
    log(f"research results persisted to {results_path}")

    build_notebook(args.notebook_name,
                   args.notebook_description or f"Deep research: {args.episode_name}",
                   results, document=document)
    content = build_podcast_content(results, document=document)
    if args.fast:
        profile = args.fast_episode_profile or args.episode_profile
        speakers = args.fast_speaker_profile or args.speaker_profile
    else:
        profile, speakers = args.episode_profile, args.speaker_profile
    job_id = trigger_podcast(content, args.episode_name,
                             args.briefing_suffix or
                             "Cover every research question above in real depth. "
                             "When discussing comparisons, trends, or findings, name "
                             "the specific papers, systems, or organizations behind "
                             "them (see each question's \"sources consulted\" list) "
                             "rather than speaking in generalities.",
                             profile, speakers)

    if POLLER_CMD:
        log("handing off for completion notification...")
        subprocess.run(shlex.split(POLLER_CMD) + [job_id, args.episode_name, args.deliver_target])
    else:
        log(f"done. job_id={job_id} -- poll Open Notebook yourself or set DRP_POLLER_CMD "
            "next time to hand off notification automatically.")


main()
