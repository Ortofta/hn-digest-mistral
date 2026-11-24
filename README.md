# hn-digest-mistral

Mistral Workflow that builds a one-shot Hacker News tech digest:

1. Fetches the Hacker News front page (Algolia HN API).
2. Filters it down to tech-industry news (drops personal blogs, opinion, politics, Ask/Tell HN) and summarizes the top stories with a Mistral model.
3. Writes a 1-2 minute podcast-style script and renders it to MP3 with Voxtral TTS.
4. Emails the digest (markdown body + `hn-tech-digest.mp3` attachment) to an iCloud inbox over SMTP (`smtp.mail.me.com:587`).

The workflow runs as a **one-shot** managed deployment on Mistral Cloud: no schedule is created. Trigger it manually from Mistral Studio (Workflows -> `hn-tech-digest` -> Start Workflow) or via `POST /v1/workflows/hn-tech-digest/execute`.

## Layout

- `src/worker/workflows/hn_digest.py` - workflow + activities
- `src/worker/entrypoints/worker.py` - worker entrypoint (`python -m worker.entrypoints.worker`)
- `tests/` - pytest suite using `mistralai.workflows.testing` (temporal test env, all external effects mocked)
- `Dockerfile` - image built by the managed deployment; must start the worker

## Workflow input (`hn-tech-digest`)

| Field | Default | Description |
| --- | --- | --- |
| `recipient` | `ICLOUD_EMAIL` env | iCloud address that receives the digest. |
| `voice_id` | `gb_jane_neutral` | Voxtral TTS preset voice. |
| `max_stories` | `8` | Max number of stories in the digest (1-15). |
| `chat_model` | `mistral-large-latest` | Model for filtering/summarizing/scripting. |
| `tts_model` | `voxtral-mini-tts-3-0` | TTS model for the audio. |
| `dry_run` | `false` | Build digest + audio but skip sending the email. |
| `monday_lookback_days` | `0` | If > 0, Monday runs (Europe/Stockholm) fetch stories from the last N days (ranked by points) instead of only the current front page, so weekend news is covered. |

## Schedule

Deployment `hn-digest` has one schedule: `45 6 * * 1-5` in `Europe/Stockholm` (Mon-Fri 06:45 Swedish time), input `{"monday_lookback_days": 3}`. Manual Start Workflow runs still work alongside it.

## Ars Technica digest (`ars-tech-digest`)

Second workflow in the same worker: summarizes the latest articles from your **Ars Technica subscriber RSS feed**,
renders a podcast MP3, and emails it to the same iCloud inbox. Same schedule (Mon-Fri 06:45 Europe/Stockholm).

The feed URL contains your subscriber token, so it is **never stored in this repo or in workflow inputs**. The worker
reads it from the `ARS_RSS_URL` environment variable; bind the workspace secret `ars_rss_url` to `ARS_RSS_URL` on the
deployment (Studio -> deployment -> Settings -> Secrets), then restart the deployment.

Input fields: `recipient`, `voice_id`, `max_articles` (default 8), `feed_items` (default 20), `chat_model`,
`tts_model`, `dry_run` — all optional.

## Secrets

The deployment binds two workspace secrets (created in Mistral Studio -> Secrets) as environment variables:

- `icloud_email` -> `ICLOUD_EMAIL` (your @iCloud.com/@me.com/@mac.com address; also the SMTP login and From:)
- `icloud_app_password` -> `ICLOUD_APP_PASSWORD` (an Apple **app-specific password** from appleid.apple.com; iCloud SMTP rejects the main Apple ID password)

## Local development

```bash
uv sync --frozen
uv run pytest
```

## Deployment

Managed deployment built from this repository (see `Dockerfile` at the root, build context `.`):

- deployment name: `hn-digest`
- revision: the pushed branch/commit you want to serve
- secrets: the two bindings above
