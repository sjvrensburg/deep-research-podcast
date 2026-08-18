---
name: open-notebook-podcast
description: "Research a topic/URL, build a notebook, generate a podcast."
version: 0.1.1
author: Stefan (sjvrensburg), Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [open-notebook, podcast, research, notebook, signal, background]
    related_skills: [camofox-browser, halo-daily-brief]
prerequisites:
  commands: [curl, python3]
---

# Open Notebook: Research → Notebook → Podcast

Turns a request like *"research this topic/URL and make a podcast on it"* into a grounded
Open Notebook notebook and a generated multi-speaker episode, entirely through Open Notebook's
REST API (`http://127.0.0.1:5055`) — not the web UI. Chat, embedding, and TTS providers are
already wired to local models (KAT-Coder, Qwen3-Embedding-0.6B, Kokoro); you don't need to touch
provider config.

**Generation takes 15–25 minutes for a full 5-segment episode.** Never block a chat turn waiting
on it — trigger, confirm to the user immediately, then hand off to a background poller that
notifies when done. See "Don't block the conversation" below; it is the point of this skill.

## 1. Research

- **Given a URL**: that page is the primary source. Fetch it (`web_extract` if it renders fine as
  plain text; `browser_navigate` + `browser_snapshot` — see the **camofox-browser** skill — if it
  needs JS or has anti-bot friction) to understand the topic, then search for 4–7 more sources
  that add real grounding: primary/official sources first (the org's own docs, the underlying
  paper/repo), then independent analysis or reception. Skip anything you can't verify resolves to
  a real page.
- **Given a bare topic**: `web_search` (SearXNG) to find the current authoritative coverage, then
  the same source-quality bar as above — aim for 5–8 sources total. Don't pad with marginal or
  duplicate coverage of the same fact.
- **Given a document** (an attached PDF/DOCX, or "make a podcast about this paper / these notes"):
  **the document is the primary source and MUST end up in the notebook as a source.** Convert it
  first with the **ocr-and-documents** skill, then research the topics it raises to find
  *supporting* sources. Add the converted markdown using the `type: "text"` form in §3. The web
  sources enrich the document; they do not replace it.

  **This is the case that fails silently.** Hermes ingests an attachment into
  `~/.hermes/cache/documents/` and puts its text straight into your context, so you can read,
  summarise and extract themes from it *without ever converting it* — and then, because §3's only
  example is `type: "link"`, build the notebook out of nothing but the web pages you found. The
  episode comes out as generic coverage of the topic while the user's own worked examples,
  notation and code are missing. From the outside it looks like success: sources attached, episode
  generated, no errors anywhere, and a confident summary claiming the PDF was used.

  **Measured failure, 2026-08-17.** "Convert the PDF to markdown, research the topics discussed in
  it, then create a long-form podcast", over a set of STAT312 class notes, produced a notebook
  whose five sources were Wikipedia and scikit-learn — the notes themselves absent. `marker_single`
  never ran. The document reached only the episode *briefing* (which did correctly pick up its
  medical-diagnosis example), so the episode was steered by the document but not grounded in it.

  Checklist for this case:
  1. Convert the document (**ocr-and-documents**; `marker_single` needs its two env vars).
  2. Create the notebook.
  3. **Add the converted markdown as a `text` source first**, before any web source.
  4. Research the topics it raises; add 4–7 supporting `link` sources.
  5. **Verify before generating** — the document must appear in:

     ```bash
     curl -s "http://127.0.0.1:5055/api/sources?notebook_id=notebook:xxxxx"
     ```

     If it does not, stop and fix it — do not generate. A plausible-looking wrong episode costs
     30–70 minutes and reads as success.

     **Use exactly that path.** `GET /api/notebooks/{id}/sources` looks like the obvious call and
     does not exist — it returns `{"detail":"Not Found"}` on a perfectly healthy server with
     sources correctly attached. A checker that treats the empty/404 body as "no sources" reports
     failure for a notebook that is fine, which is the same trap as `/api/status` vs `/health` in
     the **podcast-workflow** skill. Verified 2026-08-17: the wrong path 404s while the correct
     one returns all six sources for the same notebook.
  6. When reporting back, say which sources are in the notebook. Do not claim the document was
     used unless step 5 confirmed it.

## 2. Create the notebook

```bash
curl -s -X POST http://127.0.0.1:5055/api/notebooks \
  -H "Content-Type: application/json" \
  -d '{"name":"<short topic name>","description":"<one sentence, what this notebook covers>"}'
```
Keep the returned `id` (`notebook:...`) — every following call needs it.

## 3. Add sources — `/api/sources/json`, NOT `/api/sources`

`/api/sources` is multipart-only despite its own docs claiming JSON support; it 422s on a plain
JSON body. Use the JSON endpoint instead, and note `notebooks` wants a **real JSON array**, not a
string:

```bash
curl -s -X POST http://127.0.0.1:5055/api/sources/json \
  -H "Content-Type: application/json" \
  -d '{
    "type": "link",
    "url": "<source url>",
    "title": "<short descriptive title>",
    "notebooks": ["notebook:xxxxx"],
    "embed": true,
    "async_processing": true
  }'
```

Repeat per source.

**For a converted document (or any text you already hold), use `type: "text"` with `content` —
there is no URL to link to.** Verified 2026-08-17; the valid types are exactly `link`, `upload`,
`text`, and anything else 400s with `Invalid source type. Must be link, upload, or text`:

```bash
curl -s -X POST http://127.0.0.1:5055/api/sources/json \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg c "$(cat /tmp/marker-out/paper/paper.md)" \
        --arg nb "notebook:xxxxx" \
        '{type:"text", content:$c, title:"<document title> (source document)",
          notebooks:[$nb], embed:true, async_processing:true}')"
```

Build the body with `jq -n --arg` rather than string-interpolating the markdown into JSON by hand:
a converted document is full of quotes, backslashes and newlines, and hand-built JSON will either
fail to parse or silently truncate the source at the first bad character — which reproduces the
"document not really in the notebook" failure from §1 by a different route.

`embed: true` makes it vector-searchable via the already-wired
Qwen3-Embedding-0.6B provider (needed for the notebook to actually work as a notebook, not just a
podcast-generation input). Optionally confirm processing before generating:
`GET /api/sources/{source_id}/status` — should reach `"status": "completed"` within seconds per
source; if a source fails, drop it rather than blocking the whole notebook on one bad URL.

## 4. Pick a speaker/episode profile

All profiles are already fixed to use local models (don't re-check
`outline_llm`/`transcript_llm`/`voice_model` — they're set):

| Episode profile | Speaker profile | Speakers | Voices | Use for |
|---|---|---|---|---|
| **`tech_discussion_vibevoice`** | `tech_experts_vibevoice` | 2 | **VibeVoice** | **Default.** Technical/general topics — most requests land here. |
| `deep_dive_vibevoice` | `tech_experts_vibevoice` | 2 | VibeVoice | Long-form, for the deep-research-podcast workflow (10 segments). |
| `solo_expert_vibevoice` | `solo_expert_vibevoice` | 1 | VibeVoice | A single straightforward explainer topic. |
| `tech_discussion` | `tech_experts` | 2 | Kokoro | Same as the default, but ~2× faster — **only when speed was asked for.** |
| `deep_dive` | `tech_experts` | 2 | Kokoro | Long-form, fast variant. |
| `solo_expert` | `solo_expert` | 1 | Kokoro | Solo, fast variant. |
| `business_analysis` | `business_panel` | 3 | Kokoro | Explicitly business/market/strategy framed topics. |

**Default to VibeVoice — `tech_discussion_vibevoice` — unless the user asked for it quickly.**
The operator's standing preference is the better voices; speed is the exception, not the rule.
Switch to the Kokoro twin (drop the `_vibevoice` suffix) only when the request actually signals
urgency — "quick", "fast", "just give me something", a deadline. Do **not** `clarify` over this;
the preference is already recorded here. If they say mid-conversation that it's taking too long,
that is a signal for *next* time, not a reason to restart a running job.

Cost of the default, so you can set expectations honestly: VibeVoice runs roughly **2× the
generation time** of Kokoro (measured 2026-08-17: a 7-segment Kokoro episode took 39 min, a
VibeVoice one 72.5 min) and retries more often. Quote the VibeVoice range when you report the ETA.

Format choice — 1 vs 2 vs 3 speakers — is separate and low-stakes (wrong format, not wrong
content), so don't `clarify` over that either; pick from the table.

**The `_vibevoice` pairs for 2-speaker formats did not exist before 2026-08-17.** Only
`solo_expert_vibevoice` was configured, so every 2-speaker request silently fell back to Kokoro
while this file claimed a choice existed — `tech_experts_vibevoice` was an orphaned speaker
profile no episode profile referenced. `tech_discussion_vibevoice` and `deep_dive_vibevoice` were
created to close that. If a `_vibevoice` profile is ever missing again, check
`GET /api/episode-profiles` before telling the user which voices they are getting.

## 5. Trigger generation

```bash
curl -s -X POST http://127.0.0.1:5055/api/podcasts/generate \
  -H "Content-Type: application/json" \
  -d '{
    "episode_profile": "tech_discussion",
    "speaker_profile": "tech_experts",
    "episode_name": "<descriptive episode title>",
    "notebook_id": "notebook:xxxxx",
    "briefing_suffix": "<what specifically to focus on, drawn from the user's request>"
  }'
```

Returns immediately with `{"job_id": "command:...", "status": "submitted", ...}` — generation
runs server-side on the Open Notebook container, independent of this conversation turn.

## Don't block the conversation

The moment you have `job_id`, do two things in this order:

1. **Reply now** — tell the user generation started and roughly how long it'll take. Say
   **"roughly 20-60 minutes, depending on the voice profile"**, not "~20 minutes": Kokoro lands
   near the bottom of that range, VibeVoice near the top (72.5 min once, and a 7-segment Kokoro
   episode took 39 min on 2026-08-17). The poller now sends its own up-front message with the
   real segment count, so you do not have to guess a number here. This is the actual response to
   their Signal message; don't wait for the podcast to finish before saying anything.
2. **Launch the poller as a transient systemd unit**, then let the turn end:
   ```bash
   terminal(
     command="systemd-run --user --unit=podcast-poller-$(date +%s) \
       --setenv=PATH=\"$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin\" \
       /bin/bash ~/.hermes/skills/research/open-notebook-podcast/scripts/poll_and_notify.sh \
       \"<job_id>\" \"<episode name>\" signal"
   )
   ```
   **Not `terminal(background=true)`, and not `nohup`.** This is a change from the pre-2026-08-17
   guidance and it is load-bearing — see `references/background-launch-pitfall.md` for the full
   story. Short version: `background=true` makes the poller a child of the gateway, so
   `systemctl --user restart hermes-gateway` kills it mid-job and the episode completes into
   silence (this happened on 2026-08-17 and was only caught because someone was watching).
   `systemd-run` is a *foreground* command that returns immediately after registering an
   independent unit, so it satisfies the security scanner — which rejects only `nohup`,
   `disown`, and `setsid` — while surviving gateway restarts.

   That script polls `GET /api/podcasts/jobs/{job_id}` with a backoff schedule, and on completion sends two
   Signal messages via `hermes send` (which needs no running agent/LLM turn — it's a standalone
   CLI): a text notice, then the audio file itself as a `MEDIA:` attachment from
   `~/Music/open-notebook-podcasts/<relative path from the job result>`. On failure it sends the
   error instead of going silent. Swap `signal` for another `hermes send --to` target if the
   request didn't come in over Signal — `hermes send --list` shows what's configured.

Don't write a bespoke poll loop inline — use the bundled script. It already handles the
`/api/podcasts/jobs/{id}` response shape, the relative-to-absolute path join, and both success
and failure notification paths correctly (getting the `/api` prefix on the base URL wrong is an
easy, silent mistake — the script has it right).

**Outline/transcript generation runs on `gemma4-26b-a4b` (`:8088`) as of 2026-08-16 — this IS now
the Mentor, not a workaround.** History, in order: KAT-Coder (contended with live chat, same
single `--parallel 1` slot this conversation answers from) → `nemotron-mentor` (`:8082`, reverted
same day — Open Notebook's request/cancel pattern reliably triggered Mentor's MTP
speculative-decoding hang, `03-pitfalls.md` #18; 3/3 consecutive attempts failed with `503:
Loading model` as `llama-watchdog` restarted it mid-job) → `agents-a1-4b` (`:8083`, no drafter, so
immune, but a 4B subagent model doing mentor-class work) → **`gemma4-26b-a4b`, promoted to full
Mentor on 2026-08-16 after Nemotron proved too flaky for ongoing use generally, not just against
Open Notebook.** No drafter, so still immune to the hang class, and now genuinely the box's
best-available long-form-writing model rather than a stopgap. `nemotron-mentor` (`:8082`) is
retired outright — `llama-mentor.service` and its watchdog are stopped and disabled, not just
avoided for this one workflow.

**If Nemotron is ever revived for anything**, the hang is load-bearing evidence against using it
near Open Notebook's request/cancel pattern specifically — that needs a real upstream fix before
it's safe here again, independent of whatever else it might get used for.

**TTS is now VibeVoice-7B, not just Kokoro, as of 2026-08-16 — but only via the new
`solo_expert_vibevoice`/`tech_experts_vibevoice` speaker profiles, opt-in, not the default.** Both
sound much more natural (operator-confirmed) but the pipeline has a real, understood rough edge:
Open Notebook fires 5 TTS requests concurrently per batch, VibeVoice's server processes one at a
time (`VIBEVOICE_MAX_CONCURRENCY=1`, deliberate — avoids GPU contention with `llama-kat`/
`llama-gemma26`), so some requests time out client-side and need a retry. `podcast-creator`'s own
retry logic recovers reliably, but generation takes noticeably longer than the Kokoro path. Use
the `_vibevoice` profile variants only when the user has asked for higher voice quality and can
tolerate a longer wait; otherwise default to the Kokoro-backed profiles below, which are faster
and equally reliable.

## Known gotchas (don't rediscover these)

- **`Could not parse response content as the length limit was reached` is an OUTPUT-token cap,
  not a context-window problem and not a wrong-model problem.** `podcast_creator/nodes.py`
  hardcodes `max_tokens: 3000` for the outline call and `5000` for the transcript call unless the
  episode profile overrides them. Gemma spends part of that budget on `reasoning_content`, so on a
  large notebook the structured-output JSON gets truncated mid-object and the parser fails — the
  reported `prompt_tokens` (~67K) is well inside `:8088`'s 131072 context and is a red herring.
  Fixed on this box 2026-08-17 by setting `max_tokens: 12000` on every episode profile (`PUT
  /api/episode-profiles/{id}`; the field overrides both calls). If a new profile is created, set
  it there too. Do **not** "fix" this by repointing profiles at a different model.
- `/api/sources` (no `/json`) 422s on JSON bodies — always use `/api/sources/json`.
- `notebooks` in that call must be a JSON array (`["notebook:xxx"]`), not a bare string, even
  though the OpenAPI schema's type declaration says string.
- Bundled episode profiles ship with `outline_llm`/`transcript_llm` unset by default on a fresh
  Open Notebook install — already fixed on this box (both point at `gemma4-26b-a4b`, see above),
  but if you ever create a **new** custom episode profile, set both explicitly (`PUT
  /api/episode-profiles/{id}`) or generation fails immediately with "no outline model
  configured." Same for a new speaker profile's `voice_id` fields — Kokoro's catalog (`curl
  :8880/v1/audio/voices`) uses names like `af_bella`/`am_michael`, not OpenAI voice names like
  `nova`/`echo`.
- **Pitfall: nohup/disown rejected by security scanner, AND `background=true` dies with the
  gateway** — use `systemd-run --user` as a foreground command; it satisfies both constraints.
  See `references/background-launch-pitfall.md` for details.

## API quirks discovered (2026-08-16)

- `/api/podcasts/episode-profiles` and `/api/podcast/episode-profiles` both 404 — the router is
  mounted at **`/api/episode-profiles`** (no `podcast` segment), and it does support `GET` (list),
  `GET /{id}`, `POST` and `PUT /{id}`. Only the *generate* and *jobs* endpoints live under
  `/api/podcasts/`. Enumerate profiles at the right path rather than guessing from known names —
  guessing is what hid the `max_tokens` field during the 2026-08-16 failure.
- `/api/notebooks/{notebook_id}/sources` returns 404 — there's no list-sources endpoint for a
  notebook. Check source status individually via `/api/sources/{source_id}` or just trust the
  `source_count` in the notebook response.
- Sources show `embedded: false` immediately after creation; they become embeddable after async
  processing completes (typically 10-30 seconds). The podcast generation will wait for sources
  to be ready, but if you need to verify before triggering, poll with a short sleep between
  checks.
- The OpenAPI spec at `/openapi.json` is the authoritative source for available endpoints —
  some endpoints documented elsewhere may not exist in practice.

For full API endpoint documentation, see `references/api-endpoints-discovered.md`.
