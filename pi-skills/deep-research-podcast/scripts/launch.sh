#!/usr/bin/env bash
# Launch one deep-research-podcast run, detached, with this box's wiring.
# Part of the pi `deep-research-podcast` skill (halo-prep docs/13 Phase E, 2026-09-24; in this
# repo's pi-skills/ since 2026-10-09).
#
#   launch.sh --episode-name "..." --notebook-name "..." [--briefing-suffix "..."] \
#             [--source-document <marker .md> --source-document-title "..."] \
#             "sub-question 1" "sub-question 2" ...
#
# Every variable below was once missing from a hand-typed launch and cost a run
# (the Hermes skill's history, `git show 5aa8f04:config/hermes/skills/research/
# deep-research-podcast/SKILL.md`). Setting them here makes the correct launch the
# only one the model can type:
#   ENRICH/SYNTH  -> :8088. Left unset they fall back to the research model, which
#                    answers questions it was asked to rewrite and emits <tool_call>
#                    spam instead of prose (2026-08-22): research, then no episode.
#   BACKEND_*     -> llama-research is on-demand; without START the run health-checks
#                    :8085 for 300 s and dies. STOP runs on every exit path.
#   POLLER_CMD    -> the ONLY thing that tells the user. 2026-08-22: a finished
#                    24-minute episode nobody was told about. It is also invoked on
#                    failure (job id "failed"), so a dead run reports itself.
#   DOC_VERIFY    -> the pipeline refuses a --source-document without marker provenance.
# --detach makes the pipeline re-launch itself as a transient systemd unit and return
# in <0.1 s, so pi-signal restarts cannot kill it.
set -euo pipefail

SKILLS="${HOME}/.pi/agent/skills"
# This repo's pipeline. The skill is deployed by copy to ~/.pi/agent/skills/, so the path is
# absolute; DRP_PIPELINE overrides it. (~/Projects/deep-research-podcast until 2026-10-09.)
PIPELINE="${DRP_PIPELINE:-${HOME}/Projects/local-podcast-studio/deep-research-podcast.py}"

# --help must not reach the pipeline with DRP_POLLER_CMD set: its argparse exit is
# reported as a failed run, and the poller would send a false "run failed" Signal.
for a in "$@"; do
    case "$a" in -h|--help) exec python3 "${PIPELINE}" --help ;; esac
done

export DRP_ENRICH_LLM_URL=http://127.0.0.1:8088/v1/chat/completions
export DRP_SYNTH_LLM_URL=http://127.0.0.1:8088/v1/chat/completions
export DRP_BACKEND_START_CMD="systemctl --user start llama-research"
export DRP_BACKEND_STOP_CMD="systemctl --user stop llama-research"
export DRP_POLLER_CMD="${SKILLS}/open-notebook-podcast/scripts/poll_and_notify.sh"
export DRP_DOC_VERIFY_CMD="bash ${SKILLS}/ocr-and-documents/scripts/marker_verify.sh"
export PATH="${HOME}/.local/bin:${PATH}"

[ -x "${DRP_POLLER_CMD}" ] || { echo "launch.sh: poller missing at ${DRP_POLLER_CMD}" >&2; exit 2; }
ls "${HOME}"/models/openresearcher/OpenResearcher-30B-A3B-*.gguf >/dev/null 2>&1 \
    || { echo "launch.sh: OpenResearcher weights missing; use open-notebook-podcast instead" >&2; exit 2; }
curl -sf -m 5 http://127.0.0.1:8888/healthz >/dev/null \
    || curl -sf -m 5 'http://127.0.0.1:8888/search?q=test&format=json' >/dev/null \
    || { echo "launch.sh: SearXNG (:8888) is not answering; the pipeline would abort" >&2; exit 2; }

exec python3 "${PIPELINE}" --detach \
    --episode-profile deep_dive --speaker-profile tech_experts \
    --deliver-target signal "$@"
