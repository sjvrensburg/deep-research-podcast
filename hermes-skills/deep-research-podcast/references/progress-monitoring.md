<!-- Generalised copy. The reference box's version of this file carries its own unit ids
     and dated session notes; this one is the technique only. Substitute <INVOCATION_ID>
     with the unit id your launch command printed. -->

# deep-research-podcast — progress monitoring (recipe + verified script)

Durable technique for checking on a long-running `deep-research-podcast` job without
blocking. Derived from a real long run where ad-hoc process checks gave
misleading "research finished" reads while a researcher child was visibly alive.

## Why journal + pinned unit, not systemd state or bare live processes

1. **Transient units don't stay active.** `systemd-run --user --unit=deep-research-podcast-<ts>.service` spawns the pipeline as a detached child that outlives the unit's active state. Mid-run the unit reports **inactive** — so `is-active` is useless for phase detection and you cannot reliably track "which sub-question" from it.
2. **Process liveness flickers.** The per-sub-question researcher launches, runs to completion (spawns/dies), so checking live processes alone gives transient false "finished" reads. Journal event lines don't flicker — they accumulate on disk.
3. **Globbing corrupts counts.** Each run has a unique numeric invocation suffix. A `'deep-research-podcast-*.service'` glob mixes in OTHER past episodes that share that unit name, so `researching:` / skipped tallies would include foreign work. **Pin to THIS run's exact unit id.**

## Reading phase from journal markers (stable)

For the current unit (substitute the invocation id your launch printed):

| Marker in journal                          | Meaning                                  |
|--------------------------------------------|------------------------------------------|
| `[..] researching: '...'`                  | An OpenResearcher child is running       |
| `[..] skipping this sub-question ...`      | A question was DROPPED (no grounded synthesis) — **count these** |
| TTS / vibevoice lines                      | Podcast generation underway              |

```bash
journalctl --user -u 'deep-research-podcast-<INVOCATION_ID>.service' --no-pager \
    | grep -c 'researching:'                       # active research children now
journalctl --user -u 'deep-research-podcast-<INVOCATION_ID>.service' --no-pager \
    | grep -c 'skipping this sub-question'          # total questions dropped so far

# latest line only (skips the systemd-run startup banner)
journalctl --user -u 'deep-research-podcast-<INVOCATION_ID>.service' --no-pager \
    | grep -E 'researching:|skipping this sub-question' | tail -n1
```

## Per-turn progress (2026-08-29 and later)

Everything above reads *journal markers*, which change once per sub-question -- so a
healthy sub-question is 15-40 minutes of no new information. That was not a limit of the
technique; it was a limit of the pipeline. Two things changed on 2026-08-29:

- `openresearcher-run.py` now prints its `[turn N] tool(args)` line in `--json` mode as
  well. It used to be guarded by `if not args.json`, and the pipeline always passes
  `--json`, so the line was never emitted in a real run at all.
- `run_research()` writes the researcher's stderr to a FILE instead of capturing it, and
  logs the path:

```
[13:02:11] researching: 'What changed in X since 2025?' (max 120 turns)
[13:02:11]   research log: $DRP_RESULTS_DIR/research-1788024417-179832.log
```

So the live check for "is this sub-question moving" is now a `tail`, not a tally:

```bash
tail -f "$(grep -oP 'research log: \K\S+' /path/to/run.log | tail -1)"
```

The journal-marker recipe below is still correct and is still what a cron watchdog should
use -- it answers "how many questions are done and how many were dropped", which the turn
lines do not. Use both: markers for phase, the research log for liveness.

## Durable cron watchdog (the 30-min cadence)

A separate `--no-agent` hermes cron job runs a monitor script every tick and delivers its
stdout verbatim to Signal. Cheap per tick (no LLM), human-readable, deduplicates by journal state.

Create it once:

```bash
hermes cron create \
  --name 'deep-research-podcast progress checks' \
  --deliver origin \
  --no-agent \
  --schedule '30m' \
  --script drp-progress.sh   # <-- bare filename only; lives in ~/.hermes/scripts/
```

Notes on that command:

- `--no-agent` → the script **is** the job; its stdout is delivered directly (no LLM per tick). Empty stdout would be silent.
- `--deliver origin` → back to this Signal chat (the origin of the launch). Swap for a target if launched elsewhere (`hermes send --list`).
- `--schedule '30m'` → once every 30 minutes. A cron job with no repeat runs indefinitely — it's a watchdog, not a one-shot.
- **`--script` takes a bare filename** (relative to `~/.hermes/scripts/`). An absolute path is rejected by the harness. Use just `drp-progress.sh`.

## The monitor script (`~/.hermes/scripts/drp-progress.sh`)

```bash
#!/usr/bin/env bash
# Progress snapshot for a running deep-research-podcast job. Run by a Hermes cron
# job (--no-agent) that delivers this verbatim every 30 minutes.
set -uo pipefail

now="$(date '+%H:%M')"
unit='deep-research-podcast-<INVOCATION_ID>.service'   # <-- pin to THIS run's unit id
journalctl --user -u "$unit" --no-pager > /tmp/drp-journal.txt 2>/dev/null || true

if [ ! -s /tmp/drp-journal.txt ]; then
  echo "deep-research-podcast: not running, no progress to report."
  exit 0
fi

research_count="$(grep -c 'researching:' /tmp/drp-journal.txt)"
[ "$research_count" != "0" ] && echo "deep-research-podcast [$now] RESEARCH: $research_count sub-question(s) actively being researched."

dropped="$(grep -c 'skipping this sub-question' /tmp/drp-journal.txt)"
[ "$dropped" != "0" ] && echo "deep-research-podcast [$now] NOTE: $dropped sub-question(s) skipped (no grounded synthesis yet)."

tts="$(grep -ciE 'TTS generation|vibevoice' /tmp/drp-journal.txt)"
[ "$tts" != "0" ] && echo "deep-research-podcast [$now] PODCAST: TTS (vibevoice) started for $tts segment(s)."

latest="$(grep -E 'researching:|warning:|skipping this sub-question' /tmp/drp-journal.txt | tail -n1)"
echo "deep-research-podcast latest journal entry:"
echo "$latest"
```

Key correctness points baked into the script:

- **Pin to one unit, don't glob.** A glob pulls in other episodes' logs and inflates counts. The run id is in the `systemd-run --user --unit=deep-research-podcast-<ts>.service` output; capture it on launch and hard-code it (or derive from the newest journal start line).
- **Counts are tallies, not live flags.** The last written "researching:" line lives near the top of a 3000-line tail window; `grep -c` over recent history reflects what has been *attempted*, which is the honest signal ("N of N questions worked" / how many were dropped), not "currently spinning."
- **Avoid piping into exit-code checks.** Piping through `tail` masks the real exit code. Write the journal to a file first (`journalctl ... > file`), then grep that file, and report the script's own exit code directly.

## What this does NOT do (and why you still want the built-in poller)

The built-in `poll_and_notify.sh` already handles **completion** and **failure** notification on its lease, and reports itself with `job_id=failed`. This monitor is for *periodic human-readable* status at a cadence the user asked for; it's not a substitute for the built-in notify-on-finish. Don't add your own completion poller on top of the script that the launch command already attaches (`DRP_POLLER_CMD`).

## Related gotchas (body section "Progress monitoring gotchas")

- `systemd-run --user` detaches a child, so the unit reports inactive mid-run — trust journal/process state, not `is-active`.
- Plain `pgrep 'openresearcher-run.py'` silently matches nothing (comm field truncates at 15 chars). Use `ps -eo args | grep -F 'openresearcher-run.py --json'` for process-based liveness as a *cross-check*, but rely on journal markers for authoritative phase.
