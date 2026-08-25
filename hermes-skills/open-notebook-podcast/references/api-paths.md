# Open Notebook API Paths (Learned the Hard Way)

## Podcast URLs
- **Generate:** `POST http://127.0.0.1:5055/api/podcasts/generate`
- **Check status:** `GET http://127.0.0.1:5055/api/podcasts/jobs/{job_id}`
- **List profiles:** `GET http://127.0.0.1:5055/api/episode-profiles` (NOT `/api/podcasts/episode-profiles`)

## Source URLs (Critical!)
- **Add source:** `POST http://127.0.0.1:5055/api/sources/json`
  - NOT `POST http://127.0.0.1:5055/api/sources` — this returns 422 on JSON bodies
  - `notebooks` field MUST be a JSON array: `["notebook:xxx"]` not `"notebook:xxx"`

- **Check sources:** `GET http://127.0.0.1:5055/api/sources?notebook_id=notebook:xxx`
  - NOT `GET http://127.0.0.1:5055/api/notebooks/{id}/sources` — this returns 404
  - Use the `source_count` in the notebook response instead

## Notebook Sources
Sources are added via `/api/sources/json` with:
```json
{
  "type": "text",  // or "link" for URLs
  "content": "...",
  "title": "...",
  "notebooks": ["notebook:xxx"],
  "embed": true,
  "async_processing": true
}
```

## Episode Profiles (Verified Aug 2026)
| ID | Name | Voices | Segments |
|---|---|---|---|
| `tech_discussion_vibevoice` | Tech Discussion | VibeVoice | 5 (default) |
| `deep_dive_vibevoice` | Deep Dive | VibeVoice | 10 |
| `tech_discussion` | Tech Discussion | Kokoro | 5 |
| `deep_dive` | Deep Dive | Kokoro | 10 |

**Note:** The `_vibevoice` profiles exist only from 2026-08-17. Earlier versions only had Kokoro-backed profiles.

## Failure Detection
The poller (poll_and_notify.sh) checks:
- `status: "completed"` → success
- Any other status → send error message with log excerpt

If TTS fails, the job will show `status: "failed"` in the final response.