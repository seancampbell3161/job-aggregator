# Troubleshooting

Common problems getting the app running and operating it day-to-day. If you're
setting up for the first time, start with [GETTING_STARTED.md](GETTING_STARTED.md);
for what the app is and how it works, see the [README](README.md).

## Contents

- [No matches / no alerts](#no-matches--no-alerts)
- [Notifications](#notifications) (ntfy / Discord)
- [LLM scoring](#llm-scoring)
- [Local (Docker Compose) issues](#local-docker-compose-issues)
- [Connectors, discovery & the headless tier](#connectors-discovery--the-headless-tier)
- [Using the app](#using-the-app) (web UI, tailoring, apply kit, Gmail)

---

## No matches / no alerts

| Symptom | Likely cause | Fix |
|---|---|---|
| `matched: 0, notified: 0` cycle after cycle | **Usually normal.** A posting must be fresh, pass every filter, and score above `score_low` to notify. Most cycles are genuinely quiet. | Confirm it's working with a dry-run that shows the backlog (next row). |
| Truly zero matches ever, even with many companies | `filters.max_age_days` (if you set it to `2` as recommended) — it's a "new in the last N days" firehose, so a freshly-added company only surfaces its *future* postings, not its backlog. | Temporarily set `max_age_days: null`, restart/redeploy, confirm matches appear, then restore it. |
| Far fewer matches than expected | Filters too tight — `titles` regex misses real titles ("SDE II", "Member of Technical Staff"), `comp_floor_usd` rejects postings that state a low band, or `stack_any_of` has no overlap. | Loosen one filter at a time. Use a local dry-run (below) to see what each posting is rejected on. |
| Want to see what *would* match, without notifying | — | `python -m src.handler --tier ats --dry-run` logs `would_notify` (score + rationale) for each posting; add `--calibrate` for a score histogram. |

---

## Notifications

| Symptom | Likely cause | Fix |
|---|---|---|
| Phone doesn't buzz | (a) topic not added in the ntfy app; (b) quiet hours active (default 23:00–07:00 in your `quiet_hours.timezone`) drop ntfy to silent **by design**. | Add the topic in the app; check the current time vs. `quiet_hours`. Smoke-test: `curl -d test https://ntfy.sh/your-topic`. |
| Discord channel silent (ntfy works) | Webhook URL wrong, or the channel/webhook was deleted. | Re-copy the webhook URL; check logs for `notify_done` / `notify_failed`. Discord fires 24/7 — quiet hours don't apply to it. |
| Everything silent | Pipeline isn't producing matches. | See [No matches](#no-matches--no-alerts) first. |
| Alerts arrive but with no score/rationale | No LLM key set, or the LLM call is failing open. | See [LLM scoring](#llm-scoring). |

---

## LLM scoring

The scorer is **fail-open**: on any error (rate limit, network, malformed/empty
response, timeout) the posting is delivered **unscored** rather than dropped. So
"alerts arrive but unscored" almost always means the LLM call is failing.

| Symptom | Likely cause | Fix |
|---|---|---|
| Postings notify but never have a score | No key for the configured `relevance.provider`, or the key is wrong. | Set the matching key (`python -m src.settings set-secret anthropic_api_key`, or `JOB_AGG_*_API_KEY` in `.env`). Check logs for `score_failed`. |
| Ollama Cloud returns empty content | `gpt-oss:20b` currently returns empty content on Cloud. | Use `gpt-oss:120b` (a reasoning model that works) instead. |
| Local Ollama is slow or OOMs | `gpt-oss:120b` (the model the Ollama recipes use) targets hosted Cloud and is too big for a typical Mac mini. | Pull a model sized to your RAM (`llama3.1:8b`, `qwen2.5:7b`, `gpt-oss:20b`) and set it in `config.yaml`. |
| Scores look wrong after switching provider/model | Different models have different score distributions, so your old `score_low` no longer fits. | Re-run `python -m src.handler --tier ats --calibrate` and reset `score_low`. See [calibrating-relevance-scores.md](docs/runbooks/calibrating-relevance-scores.md). |
| Want to score nothing for now | — | Set `relevance.enabled: false` — postings still flow through filters, just unscored. |

---

## Local (Docker Compose) issues

| Symptom | Likely cause | Fix |
|---|---|---|
| First `docker compose up --build` takes minutes | Expected — the image installs WeasyPrint's native libraries. | Wait it out; subsequent builds are cached. |
| `http://localhost:8000` won't load | The `web` service crashed, or you only started `poller`. | `docker compose ps` then `docker compose logs web`. |
| Can't reach the UI from your phone | You used `localhost` from another device. | Use `http://<box-lan-ip>:8000`. The web service binds `0.0.0.0` inside the container and is published on the host. |
| Web UI shows **Set up job-aggregator**; poller logs `awaiting_setup` | No settings imported yet (fresh install or a new `./data`). | `docker compose run --rm -v "$PWD:/import:ro" web python -m src.settings import /import` from the directory holding your `config.yaml`. |
| Edited `config.yaml` / `profile.md`, nothing changed | The files are only an import format; the app reads its database. | Re-run the import — changes apply live, no restart. |
| Import fails: `invalid settings — nothing was written` | A value failed validation; the dotted path names the key. | Fix that key and import again — a failed import changes nothing. |
| Import fails: `these files would undo settings saved after your last import …` | `add-source`, `restore`, or a `--merge` / `seed_companies.py` script changed settings in the database since your last import, and your files still have the old values. Importing them would silently drop those changes, so import refuses, names each setting, and writes nothing. | Export the settings in effect (`docker compose run --rm web python -m src.settings export /data/export`), bring the listed settings into your files from `./data/export/`, then import again — it succeeds once the files carry them. Import your own files rather than the export: each import is only checked for changes since the last one. To discard those changes on purpose, re-run the import with `--force`. |
| Banner "Settings version N is invalid"; ops alert **settings fallback** (`config_fallback`) | The newest settings version fails validation (for example, written by a newer release you rolled back from); the app runs on the last valid version. | `python -m src.settings status` lists the errors; import corrected settings, or `python -m src.settings restore ID` (IDs from `history`). |
| `gap_analysis` enabled but no stretch areas | No résumé document was imported. | Put `resume.md` next to `config.yaml` and re-import. |
| Local Ollama provider, but scoring never happens | Started the stack without the Ollama profile, or didn't pull the model. | `docker compose --profile ollama up -d --build` then `docker compose exec ollama ollama pull <model>`. |
| Lost data after `docker compose down` | You removed the `./data` volume, or ran `down -v`. | State lives in `./data/job_aggregator.db`; `down` alone preserves it. Back up by copying `./data`. |

---

## Connectors, discovery & the headless tier

| Symptom | Likely cause | Fix |
|---|---|---|
| An **Avature** board never returns jobs | Either the tenant sits behind a WAF that blocks even a real browser, or (venv runs only) Chromium isn't installed. The Docker image already bundles it. | Watch the `headless` cycle logs. A browser error / `0 jobs` on one tenant usually means it's WAF-gated — remove it. Local venv: `pip install -e '.[headless]' && playwright install chromium`. |
| The `headless` tier never runs | No `sources.avature` entries, or the poller image predates the headless tier. | Add a board (Getting Started → Avature), then rebuild so `src/` + the browser are baked in: `docker compose up -d --build`. |
| **Automated board discovery** finds nothing | It fingerprints only a *budget* of seeds per day — matches join `ats` polling over ~8 days, not at once. | Watch the `board_discovery_done` log for `matched > 0`. If `swept` is `0` every cycle, the seed CSV is missing from the image — rebuild with `--build`. |
| A discovered board polls once, then disappears | Poll-health suppressed it (dead slug / false positive) — by design, the same net that governs discovered slugs. | Check `connector_health`; genuine boards are re-probed daily and recover on their own. |
| **Aggregator mining** stages nothing | No aggregator source is on (`sources.adzuna.enabled: false` — the hiring.cafe connector is disabled and blocked upstream), `discovery.hiringcafe_mining_enabled: false`, or everything sighted is already polled/known — the steady state after the first few days. | Watch `hiringcafe_sightings_captured` on slow cycles: a climbing `deduped` count is healthy; `captured_slugs`/`captured_boards` should be nonzero in the first cycles after enabling. Candidates convert on the next daily `discovery` run — watch `discovery_candidates_drained`. |
| Enabled **`vc_firms`** but no new companies appear | The weekly watermark may be fresh (`vc_discovery_done` with `skipped_fresh`), the fetch failed (`vc_fetch_failed` — watermark not advanced, retries next daily run), everything deduped against rows you already have, or candidates are staged but not yet drained — validation spends the normal probe budget over days, ~2 weeks for a big portfolio. | Check `vc_discovery_done` counts (`fetched`/`staged`/`deduped`) on the daily run, then `discovery_candidates_drained` (`name_ok`/`name_board_ok`/`name_no_match`) over the following runs. Staged rows sit under `candidate:{slug}` keys in `discovered_slugs`. |
| A known company never converts via discovery | Its conversion chain (slug variants + careers-page fingerprint) fully missed, and the miss is suppressed for 90 days (`nomatch:{slug}` row). The domain variant and fingerprint fallback need a website — `manual_companies` entries have none, so they get slug probes only. | Check the `nomatch:` row's `methods_tried` in `discovered_slugs` to see what was probed. Fastest fix for a company you care about: add its slug to the right `sources:` family by hand — config always wins. Enterprise conversions show as `board_ok` in `discovery_probe_phase_done`, not `ok`. |

---

## Using the app

| Symptom | Likely cause | Fix |
|---|---|---|
| `/kit` is empty or has no copy buttons | No `resume/facts.yaml` has been imported — only `facts.example.yaml` ships. | `cp resume/facts.example.yaml resume/facts.yaml`, edit it, then re-import — `/kit` shows it on the next refresh. |
| The `/kit` "Apply Autofill" bookmarklet does nothing | It's dragged to the bookmarks bar but clicked on a non-supported form, or your facts changed since the last import. | It autofills Greenhouse / Lever / Ashby forms only. Re-drag it from `/kit` after re-importing your facts. |
| Gmail badges never appear on `/board` | 2-Step Verification off, wrong app password, or `.env` not re-read. | Generate a Gmail **app password** (requires 2FA), then `python -m src.settings set-secret gmail_app_password` (and `gmail_address`) — applies live, no recreate — or set `JOB_AGG_GMAIL_ADDRESS` + `JOB_AGG_GMAIL_APP_PASSWORD` in `.env` and `docker compose up -d --force-recreate`. |
| Triage inbox is empty though alerts fired | The inbox shows only matches notified **after** the triage feature shipped — the pipeline persists display fields at notify time; older rows are id+score+gaps only. | New matches populate it automatically. |
| A triaged match disappeared | "New" and "Dismissed" matches expire after the 60-day TTL. | Move a match to Interested/Applied/Interviewing — that drops the TTL so it persists. |
| `/analytics` stretch-skills panel shows a hint | `gap_analysis` isn't enabled, or no annotated matches yet. | Enable `gap_analysis` (Getting Started §2f); it populates as matches accrue. |
| Tailor deep-link in an alert doesn't work | `tailor_endpoint_url` unset, the URL isn't reachable from your phone, or the résumé artifacts are missing. | Set `tailor_endpoint_url` (`set-secret tailor_endpoint_url`, or `JOB_AGG_TAILOR_ENDPOINT_URL` in `.env`) to a reachable URL (LAN IP or Tailscale/Cloudflare Tunnel) — the signing secret is generated automatically; import `resume/content.json` + `resume/evidence.json` (see `resume/README.md`). |
| Forgot the web UI password, or locked out | There is no email reset — the password lives only in the app database. | `docker compose run --rm -it web python -m src.settings set-password` sets a new one and signs every device out. |
| A phone or laptop that was signed in is lost | Its session stays valid until it goes 30 days unused. | `docker compose run --rm web python -m src.settings sign-out-everywhere` ends every session (the password is unchanged), or change the password at `/account/password`. |
| Sign-in says **Too many attempts. Try again in N s.** | More than 5 wrong passwords in a row, from any device. Each further failure doubles the wait, up to 5 minutes. | Wait it out; browsers that are already signed in keep working. If someone else is guessing, take the port off that network. |
| A form or button fails with **Cross-origin request blocked** | The browser reported the request as coming from another site — usually a reverse proxy that rewrites the `Host` header. `docker compose logs web` shows `cross_origin_blocked` with the `Origin` and `Host` it saw. | Make the proxy pass the original `Host` through (nginx: `proxy_set_header Host $host;`), or serve the UI over HTTPS. |
| Every page says **Cannot read the login database.** | The web service can't read `./data/job_aggregator.db`. | `docker compose logs web`; check the `./data` mount and file permissions. |

---

## Still stuck?

- **Read the tests.** Every behavior is covered by a small test (most 5–15
  lines). `grep` a function name in `tests/` for a worked example.
- **Turn on a dry-run.** `python -m src.handler --tier ats --dry-run` shows
  exactly what the pipeline does without side effects.
- The runbooks in [`docs/runbooks/`](docs/runbooks/) cover relevance
  calibration in more depth.
