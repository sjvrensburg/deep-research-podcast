#!/usr/bin/env bash
# STAGED v2 — replaces ~/.hermes/skills/research/open-notebook-podcast/scripts/poll_and_notify.sh
#
# Poll an Open Notebook podcast generation job to completion, then notify.
#
# Usage: poll_and_notify.sh <job_id> <episode_name> [deliver_target]
#   job_id          e.g. command:dpyskjwt3gnbi0mpvrlw (from POST /api/podcasts/generate)
#   episode_name    human-readable name, used in the notification text
#   deliver_target  hermes send --to target (default: signal)
#
# ── LAUNCH IT LIKE THIS ──────────────────────────────────────────────────
#   systemd-run --user --unit="podcast-poller-$(date +%s)" \
#     --setenv=PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
#     /bin/bash ~/.hermes/skills/research/open-notebook-podcast/scripts/poll_and_notify.sh \
#     "<job_id>" "<episode name>" signal
#
# NOT `nohup ... & disown`. A backgrounded child of the gateway dies with the
# gateway: `systemctl --user restart hermes-gateway` on 2026-08-17 killed a
# live poller mid-job (verified — the scope went with the service), orphaning a
# running episode that would have completed into silence. A transient unit is
# independent of the gateway and gives you `systemctl --user status` for free.
#
# `hermes send` does NOT need a running gateway for Signal/Telegram/Discord/SMS
# (see hermes_cli/send_cmd.py — those are direct-send platforms), so this script
# still delivers across a gateway restart *if* it is alive to do so.
set -uo pipefail

JOB_ID="${1:?usage: poll_and_notify.sh <job_id> <episode_name> [deliver_target]}"
EPISODE_NAME="${2:?usage: poll_and_notify.sh <job_id> <episode_name> [deliver_target]}"
DELIVER_TARGET="${3:-signal}"

API="http://127.0.0.1:5055/api"
PODCASTS_DIR="${HOME}/Music/open-notebook-podcasts"
FAIL_LOG="${HOME}/.hermes/logs/podcast-notify-failures.log"
CONTAINER="${ON_CONTAINER:-open-notebook}"

# Wall-clock budget rather than a poll count, so changing the interval schedule
# below can't silently change how long we wait. 3h soft (warn, keep watching),
# 6h hard (give up). The old 1h ceiling reported a healthy 72.5-min VibeVoice
# run as stuck, so the soft mark warns instead of abandoning.
SOFT_CEILING_SECS="${SOFT_CEILING_SECS:-10800}"
HARD_CEILING_SECS="${HARD_CEILING_SECS:-21600}"
# No new "segment N/M" line for this long => probably wedged. Segments observed
# at ~3-4 min each on this box, so 25 min is ~7x the normal gap.
STALL_SECS="${STALL_SECS:-1500}"
# Set PROGRESS_PINGS=1 to get a Signal message at every segment boundary.
# Off by default: a 7-segment episode would be 7 pings for no decision value.
PROGRESS_PINGS="${PROGRESS_PINGS:-0}"

START_TS="$(date +%s)"

# ── Single poller per job ────────────────────────────────────────────────
# Without this, a retry loop in the agent that fires the launch twice gets you
# two pollers, and the user gets duplicate "ready" messages AND duplicate audio
# attachments. Today's agent looped 15 times on one tool call, so this is not
# hypothetical.
LOCK_FILE="/tmp/hermes-podcast-${JOB_ID//[^a-zA-Z0-9]/_}.lock"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "another poller already owns ${JOB_ID} (lock: ${LOCK_FILE}) — exiting" >&2
    exit 0
fi

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') [poll] $*" >&2; }

# ── Delivery, with the exit code actually checked ────────────────────────
# The v1 script called `hermes send` and ignored its result. send_cmd.py
# documents exit 1 = delivery failed at the platform level, so a failed send
# exited 0 having told nobody — the audio existed and the user never heard.
notify() {
    local msg="$1" attempt rc
    for attempt in 1 2 3; do
        if hermes send --to "${DELIVER_TARGET}" "${msg}"; then
            return 0
        fi
        rc=$?
        log "hermes send failed (attempt ${attempt}/3, rc=${rc})"
        sleep $((attempt * 10))
    done
    # Last resort: leave a breadcrumb so the work is recoverable by hand.
    mkdir -p "$(dirname "${FAIL_LOG}")"
    printf '%s\tjob=%s\ttarget=%s\tmsg=%s\n' \
        "$(date -Is)" "${JOB_ID}" "${DELIVER_TARGET}" "${msg}" >>"${FAIL_LOG}"
    log "UNDELIVERED — recorded in ${FAIL_LOG}"
    return 1
}

# ── Job status, with the HTTP code kept ──────────────────────────────────
# v1 did `.get("status","?")` on whatever came back and treated "?" as running.
# A deleted or mistyped job id returns HTTP 500 {"detail":"Failed to fetch job
# status"} (verified), so "?" meant three hours of polling a job that never
# existed, followed by a misleading "may be stuck". Keep the code, fail fast.
http_code=""
job_status=""
fetch_status() {
    local resp
    resp="$(curl -s -m 10 -w '\n%{http_code}' "${API}/podcasts/jobs/${JOB_ID}" 2>/dev/null)"
    http_code="$(printf '%s' "${resp}" | tail -n1)"
    local body
    body="$(printf '%s' "${resp}" | sed '$d')"
    job_status="$(printf '%s' "${body}" | python3 -c \
        'import json,sys; print(json.load(sys.stdin).get("status","?"))' 2>/dev/null || echo "")"
    printf '%s' "${body}"
}

audio_path_from_job() {
    curl -s -m 10 "${API}/podcasts/jobs/${JOB_ID}" 2>/dev/null | python3 -c \
        'import json,sys; print(json.load(sys.stdin).get("result",{}).get("audio_file_path",""))' 2>/dev/null
}

# ── Progress signal ──────────────────────────────────────────────────────
# The episode object is useless for this: `outline` and `transcript` stay empty
# for the whole run and only populate at completion (verified against a live
# job and two finished ones). The container log is the only real progress
# source — it emits "Generating transcript for segment N/M".
#
# `--since "${START_TS}"`, NOT a fixed window. Fixed 30m/60m windows read log
# lines emitted BEFORE this poller existed, so a fresh poller reports the
# PREVIOUS episode's progress as its own until its job writes a first segment
# line. Observed 2026-08-17: a poller started at 17:23:21 announced
# "segment 5/7" one second later and sent that to Signal, while its job had not
# even finished generating an outline — 5/7 was the position of the job that had
# just been killed. Mid-run readings were correct; only the startup window lied,
# which is the worst case, since that is the reading a user takes as
# confirmation the job started properly.
#
# docker's --since accepts a Unix timestamp, so START_TS (set at line ~50) is a
# drop-in. It also makes the "attributing another job's progress to this one"
# guard below actually hold, rather than relying on jobs never overlapping.
current_segment() {
    command -v docker >/dev/null 2>&1 || return 0
    docker logs --since "${START_TS}" "${CONTAINER}" 2>&1 \
        | grep -oE 'segment [0-9]+/[0-9]+' | tail -1
}

segment_total() {
    command -v docker >/dev/null 2>&1 || return 0
    docker logs --since "${START_TS}" "${CONTAINER}" 2>&1 \
        | grep -oE 'Generated outline with [0-9]+ segments' | tail -1 | grep -oE '[0-9]+'
}

# Transcript segments are only HALF the job. Once the last transcript is written
# the run moves to audio synthesis, which emits "Generated audio clip" per clip
# and no further "segment N/M" lines at all. A stall detector watching only
# segments is therefore guaranteed to cry wolf on every run whose synthesis
# phase exceeds STALL_SECS -- which is every VibeVoice run, since VibeVoice is
# roughly 2x Kokoro and synthesis is the long phase.
#
# Observed 2026-08-17: a healthy VibeVoice run was reported as "hasn't advanced
# past segment 7/7 in 25 min -- it may be wedged" while it was steadily
# producing one audio clip a minute. The episode completed normally 40 minutes
# later. Counting clips as progress is what makes the stall warning mean
# something.
audio_clips_done() {
    command -v docker >/dev/null 2>&1 || return 0
    docker logs --since "${START_TS}" "${CONTAINER}" 2>&1 \
        | grep -c 'Generated audio clip'
}

# Interval schedule: tight early so an immediate failure is caught fast, then
# relaxed. v1's flat 20s meant 45-540 requests for zero added responsiveness.
interval_for() {
    local elapsed="$1"
    if   [ "${elapsed}" -lt 120 ]; then echo 10
    elif [ "${elapsed}" -lt 600 ]; then echo 30
    else echo 60
    fi
}

log "watching ${JOB_ID} (${EPISODE_NAME}) -> ${DELIVER_TARGET}"

consecutive_errors=0
last_segment=""
last_segment_change="${START_TS}"
announced_total=0
warned_stall=0
warned_soft=0

while true; do
    now="$(date +%s)"
    elapsed=$((now - START_TS))

    fetch_status >/dev/null

    case "${http_code}" in
    200)
        consecutive_errors=0
        ;;
    "")
        # curl itself failed — Open Notebook down, or the container restarting.
        consecutive_errors=$((consecutive_errors + 1))
        log "no response from API (${consecutive_errors} in a row)"
        ;;
    404|500|502|503)
        consecutive_errors=$((consecutive_errors + 1))
        log "HTTP ${http_code} from job endpoint (${consecutive_errors} in a row)"
        ;;
    *)
        consecutive_errors=$((consecutive_errors + 1))
        log "unexpected HTTP ${http_code} (${consecutive_errors} in a row)"
        ;;
    esac

    # Five consecutive failures is a couple of minutes early on — long enough to
    # ride out a container restart, short enough that a bogus job id fails in
    # minutes instead of hours.
    if [ "${consecutive_errors}" -ge 5 ]; then
        notify "Podcast \"${EPISODE_NAME}\" — I can't read its job status (HTTP ${http_code:-no response}, 5 tries). The job id may be wrong or Open Notebook may be down. Check: ${API}/podcasts/jobs/${JOB_ID}"
        exit 1
    fi

    case "${job_status}" in
    completed)
        rel_path="$(audio_path_from_job)"
        if [ -z "${rel_path}" ]; then
            notify "Podcast \"${EPISODE_NAME}\" finished generating, but I couldn't find the audio file path in the job result. Check the Open Notebook UI (http://127.0.0.1:8502) or the job directly: ${API}/podcasts/jobs/${JOB_ID}"
            exit 0
        fi
        audio_path="${PODCASTS_DIR}/${rel_path}"
        notify "🎙️ Your podcast \"${EPISODE_NAME}\" is ready."
        if [ -f "${audio_path}" ]; then
            notify "MEDIA:${audio_path}"
        else
            notify "(File should be at ${audio_path} but I can't find it there -- check ${PODCASTS_DIR})"
        fi
        log "done in $((elapsed / 60))m"
        exit 0
        ;;
    failed|error)
        err="$(curl -s -m 10 "${API}/podcasts/jobs/${JOB_ID}" | python3 -c \
            'import json,sys; print(json.load(sys.stdin).get("error_message","unknown error"))' 2>/dev/null || echo "unknown error")"
        notify "Podcast \"${EPISODE_NAME}\" failed to generate: ${err}"
        exit 1
        ;;
    esac

    # ── Progress / stall tracking ────────────────────────────────────────
    # Only trust the container log once the API has actually confirmed this job
    # exists. The log lines carry no job id, so on a bogus job id they describe
    # whatever OTHER episode is generating — a test with a mistyped id happily
    # announced "7 segments" for a job that did not exist. One generation runs
    # at a time in practice, but attributing another job's progress to this one
    # is wrong in exactly the case you most need a truthful message.
    if [ "${http_code}" != "200" ]; then
        sleep "$(interval_for "${elapsed}")"
        continue
    fi

    seg="$(current_segment)"
    if [ -n "${seg}" ] && [ "${seg}" != "${last_segment}" ]; then
        log "progress: ${seg}"
        last_segment="${seg}"
        last_segment_change="${now}"
        warned_stall=0
        [ "${PROGRESS_PINGS}" = "1" ] && notify "\"${EPISODE_NAME}\": ${seg}"
    fi

    # Audio synthesis counts as progress too -- see audio_clips_done() above.
    # Logged, not pushed to Signal: a 7-segment two-speaker episode is dozens of
    # clips, and a notification each would be worse than saying nothing.
    clips="$(audio_clips_done)"
    if [ -n "${clips}" ] && [ "${clips}" -gt "${last_clips:-0}" ] 2>/dev/null; then
        log "progress: ${clips} audio clip(s) synthesised"
        last_clips="${clips}"
        last_segment_change="${now}"
        warned_stall=0
    fi

    # One up-front message with the real shape of the job. The podcast-workflow
    # skill advertises 15-25 min, which is the Kokoro figure; VibeVoice has run
    # 72.5 min here. Telling the user the segment count beats a wrong ETA.
    if [ "${announced_total}" -eq 0 ]; then
        total="$(segment_total)"
        if [ -n "${total}" ]; then
            announced_total=1
            notify "Podcast \"${EPISODE_NAME}\" is generating — ${total} segments. Roughly 3-4 min per segment on this box plus audio synthesis, so expect ~$(( total * 4 ))-$(( total * 9 )) min. I'll message you when it's ready."
        fi
    fi

    if [ $((now - last_segment_change)) -ge "${STALL_SECS}" ] && [ "${warned_stall}" -eq 0 ]; then
        warned_stall=1
        # Name the phase. "hasn't advanced past segment 7/7" is meaningless once
        # transcripts are done -- it reads as wedged when the job has simply
        # moved on to synthesis.
        if [ "${last_clips:-0}" -gt 0 ]; then
            where="audio synthesis (${last_clips} clips done)"
        elif [ -n "${last_segment}" ]; then
            where="transcript ${last_segment}"
        else
            where="outline generation"
        fi
        notify "Podcast \"${EPISODE_NAME}\" hasn't advanced past ${where} in $((STALL_SECS / 60)) min. It may be wedged — still watching. Logs: docker logs --tail 50 ${CONTAINER}"
    fi

    # ── Ceilings ─────────────────────────────────────────────────────────
    if [ "${elapsed}" -ge "${HARD_CEILING_SECS}" ]; then
        notify "Giving up on podcast \"${EPISODE_NAME}\" after $((elapsed / 3600))h. If it does finish, the audio will still land in ${PODCASTS_DIR}. Job: ${API}/podcasts/jobs/${JOB_ID}"
        exit 1
    fi
    if [ "${elapsed}" -ge "${SOFT_CEILING_SECS}" ] && [ "${warned_soft}" -eq 0 ]; then
        warned_soft=1
        notify "Podcast \"${EPISODE_NAME}\" is still generating after $((elapsed / 60)) min — longer than any run observed on this box. Still watching, but something may be stuck. Job: ${API}/podcasts/jobs/${JOB_ID}"
    fi

    sleep "$(interval_for "${elapsed}")"
done
