# Open Notebook API Endpoints - Discovered 2026-08-16

## Working Endpoints

### Notebooks
- `POST /api/notebooks` - Create notebook, returns `{id: "notebook:xxx", name, description, source_count, note_count}`
- `GET /api/notebooks/{notebook_id}` - Get notebook details

### Sources
- `POST /api/sources/json` - Add source with JSON body (NOT `/api/sources` which is multipart-only)
  - Required fields: `type`, `url`, `title`, `notebooks` (array), `embed`, `async_processing`
  - Returns: `{id: "source:xxx", title, status: "new"|"processing"|"completed", processing_info}`
- Source embedding happens async; check via `/api/sources/{source_id}` if needed

### Podcasts
- `POST /api/podcasts/generate` - Trigger generation
  - Returns: `{job_id: "command:xxx", status: "submitted", episode_name}`
- `GET /api/podcasts/jobs/{job_id}` - Job status
- Generation runs server-side, independent of conversation

### Episode / speaker profiles (corrected 2026-08-17)
- `GET /api/episode-profiles` - list; `GET|PUT /api/episode-profiles/{id}`, `POST` to create.
  **No `podcast`/`podcasts` path segment** — that is why the 2026-08-16 session concluded the
  endpoint didn't exist.
- `PUT` takes the *full* profile body (name, speaker_config, default_briefing, num_segments,
  outline_llm, transcript_llm, language, max_tokens).
- `max_tokens` (nullable) overrides `podcast_creator`'s hardcoded 3000-token outline cap and
  5000-token transcript cap. Null on a fresh install; set to 12000 on this box.
- `GET|PUT /api/speaker-profiles[/{id}]` - same shape for voices.

## Non-Existent Endpoints (return 404)

- `/api/podcasts/episode-profiles`, `/api/podcast/episode-profiles` - wrong prefix, see above
- `/api/notebooks/{notebook_id}/sources` - No list-sources endpoint for notebook
- `GET /api/podcasts/jobs` - No jobs list endpoint; check job by ID only

## Source Processing Timeline

1. Source created: status = "new", embedded = false
2. After ~10-30s: processing completes, embedded = true (if embed: true)
3. Notebook shows updated source_count

Podcast generation waits for sources automatically; no need to poll before triggering.
