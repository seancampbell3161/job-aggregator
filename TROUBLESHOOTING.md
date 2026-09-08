# Troubleshooting

Common problems getting the app running and operating it day-to-day. If you're
setting up for the first time, start with [GETTING_STARTED.md](GETTING_STARTED.md);
for what the app is and how it works, see the [README](README.md).

## Contents

- [No matches / no alerts](#no-matches--no-alerts) (applies to both paths)
- [Notifications](#notifications) (ntfy / Discord)
- [LLM scoring](#llm-scoring)
- [Local (Docker Compose) issues](#local-docker-compose-issues)
- [AWS (Lambda) issues](#aws-lambda-issues)
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
| Postings notify but never have a score | No key for the configured `relevance.provider`, or the key is wrong. | Set the matching key (`JOB_AGG_*_API_KEY` locally, or the SSM param on AWS). Check logs for `score_failed`. |
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
| Edited `config.yaml` / `profile.md`, nothing changed | They're bind-mounted but read at process start. | `docker compose restart poller web` (no rebuild needed). |
| Local Ollama provider, but scoring never happens | Started the stack without the Ollama profile, or didn't pull the model. | `docker compose --profile ollama up -d --build` then `docker compose exec ollama ollama pull <model>`. |
| `gap_analysis` enabled but no stretch areas | `resume.md` isn't mounted into the containers. | Uncomment the `./resume.md:/app/resume.md:ro` lines in `docker-compose.yml` and restart. |
| Lost data after `docker compose down` | You removed the `./data` volume, or ran `down -v`. | State lives in `./data/job_aggregator.db`; `down` alone preserves it. Back up by copying `./data`. |
| Migration from DynamoDB pulled nothing | AWS read creds weren't passed into the container. | Re-run with `-e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_REGION=us-east-1` (see Getting Started §A3). |
| Migration ran (printed row counts) but the UI shows none of it | Run on the host, it inherited `.env`'s absolute `JOB_AGG_SQLITE_PATH=/data/...` and wrote to a host path the app never reads — leaving only fresh-poll rows (no `applied`/statuses) in `./data/job_aggregator.db`. | Re-run on the host with `JOB_AGG_SQLITE_PATH=./data/job_aggregator.db` (or use the `docker compose run` form in §A3, which is correct by default). It's an idempotent upsert, so safe to repeat. |
| Startup fails: `config.yaml not found — copy config.example.yaml…` | Fresh clone; the personal `config.yaml`/`profile.md` were never seeded. | `cp config.example.yaml config.yaml && cp profile.example.md profile.md`, personalize (GETTING_STARTED §3), restart. |
| Startup fails: `config.yaml is a directory, not a file…` | You ran `docker compose up` before seeding the files — compose created directory stubs at the mount sources. | `rm -r config.yaml  # and/or profile.md — only the ones that are directories`, then `cp config.example.yaml config.yaml && cp profile.example.md profile.md`, personalize, restart. |

---

## AWS (Lambda) issues

### Deploy / Terraform

| Symptom | Likely cause | Fix |
|---|---|---|
| `No valid credential sources found` from Terraform | Used `aws login`; cached creds aren't visible to Terraform. | `eval "$(aws configure export-credentials --format env)"` in the same shell. |
| `terraform apply` says "No configuration files" | You're in the repo root, not `infra/`. | `cd infra` (or `cd infra/bootstrap` for the backend step). |
| `ParameterNotFound` during apply | Set the secrets step was skipped — the Lambda reads them via data sources at apply time. | Set the secrets with `--overwrite`, then re-apply. |
| `ParameterAlreadyExists` on the first apply | You set secrets *before* the first apply — they exist in AWS but not in TF state. | `terraform import aws_ssm_parameter.ntfy_topic_url /job-aggregator/ntfy_topic_url` (and the others), then apply. |
| `aws login` session expired mid-deploy | Default session is ~1h. | Re-run `aws login`, then `eval "$(aws configure export-credentials --format env)"`. |
| Alarm email never arrives | SNS subscription not confirmed. | Click the confirmation link AWS emailed to `alarm_email` (check spam). |

### Runtime

| Symptom | Likely cause | Fix |
|---|---|---|
| Every cycle crashes with `Read-only file system: '/var/task/data'` | The Lambda is defaulting to the SQLite backend (read-only filesystem). The env var that selects DynamoDB is missing. | Ensure `JOB_AGG_BACKEND=dynamodb` is on the Lambda. Terraform sets it in `infra/lambda.tf`; just re-apply. **If you deployed code with `aws lambda update-function-code`, note it ships code only — not env vars; set them via `update-function-configuration` or `terraform apply`.** |
| `Runtime.OutOfMemory` | `lambda_memory_mb` too low. A fresh "every board" cycle peaks near 1 GB; **512 MB OOMs**. | Keep `lambda_memory_mb` ≥ 1024 (bump to 1536 if you add many connectors). More memory also buys more CPU → faster cycles. |
| The `ats` cycle nears the 120s timeout | Too many connectors / Workday boards paginating deep. | Workday pagination is capped at 5 pages by design; trim connectors or raise `lambda_timeout_s` / memory. |
| Lambda errors mentioning `JOB_AGG_NTFY_TOPIC_URL` | Secret not set / not picked up. | Set it in SSM with `--overwrite`, then re-apply so the env var refreshes. |
| EventBridge "not firing" right after deploy | The first invoke can take up to the rate interval (2 or 15 min). | Wait, then check the rule's Monitoring tab, or `aws events list-rules`. |
| A connector keeps 404/410-ing | The poll-health circuit breaker auto-suppresses repeatedly-failing connectors and re-probes daily — usually a dead/renamed slug. | Check `connector_health`; fix or remove the slug in `config.yaml`. |
| Mobile tailor endpoint returns 403 | New/restricted AWS accounts block **public** Lambda Function URLs. | Run the tailor loop locally (Docker) instead, or use the [tailor endpoint runbook](docs/runbooks/deploy-tailor-endpoint.md) once your account allows public URLs. |

---

## Connectors, discovery & the headless tier

| Symptom | Likely cause | Fix |
|---|---|---|
| An **Avature** board never returns jobs | Either the tenant sits behind a WAF that blocks even a real browser, or (venv runs only) Chromium isn't installed. The Docker image already bundles it. | Watch the `headless` cycle logs. A browser error / `0 jobs` on one tenant usually means it's WAF-gated — remove it. Local venv: `pip install -e '.[headless]' && playwright install chromium`. |
| The `headless` tier never runs | No `sources.avature` entries, or the poller image predates the headless tier. | Add a board (Getting Started → Avature), then rebuild so `src/` + the browser are baked in: `docker compose up -d --build`. |
| **Automated board discovery** finds nothing | It's SQLite-only (inert on the DynamoDB/Lambda backend), and it fingerprints only a *budget* of seeds per day — matches join `ats` polling over ~8 days, not at once. | On SQLite, watch the `board_discovery_done` log for `matched > 0`. If `swept` is `0` every cycle, the seed CSV is missing from the image — rebuild with `--build`. |
| A discovered board polls once, then disappears | Poll-health suppressed it (dead slug / false positive) — by design, the same net that governs discovered slugs. | Check `connector_health`; genuine boards are re-probed daily and recover on their own. |
| **Aggregator mining** stages nothing | No aggregator source is on (`sources.adzuna.enabled: false` — the hiring.cafe connector is disabled and blocked upstream), `discovery.hiringcafe_mining_enabled: false`, or everything sighted is already polled/known — the steady state after the first few days. | Watch `hiringcafe_sightings_captured` on slow cycles: a climbing `deduped` count is healthy; `captured_slugs`/`captured_boards` should be nonzero in the first cycles after enabling. Candidates convert on the next daily `discovery` run — watch `discovery_candidates_drained`. |
| Enabled **`vc_firms`** but no new companies appear | The weekly watermark may be fresh (`vc_discovery_done` with `skipped_fresh`), the fetch failed (`vc_fetch_failed` — watermark not advanced, retries next daily run), everything deduped against rows you already have, or candidates are staged but not yet drained — validation spends the normal probe budget over days, ~2 weeks for a big portfolio. | Check `vc_discovery_done` counts (`fetched`/`staged`/`deduped`) on the daily run, then `discovery_candidates_drained` (`name_ok`/`name_board_ok`/`name_no_match`) over the following runs. Staged rows sit under `candidate:{slug}` keys in `discovered_slugs`. |
| A known company never converts via discovery | Its conversion chain (slug variants + careers-page fingerprint) fully missed, and the miss is suppressed for 90 days (`nomatch:{slug}` row). The domain variant and fingerprint fallback need a website — `manual_companies` entries have none, so they get slug probes only. | Check the `nomatch:` row's `methods_tried` in `discovered_slugs` to see what was probed. Fastest fix for a company you care about: add its slug to the right `sources:` family by hand — config always wins. Enterprise conversions show as `board_ok` in `discovery_probe_phase_done`, not `ok`. |

---

## Using the app

| Symptom | Likely cause | Fix |
|---|---|---|
| `/kit` is empty or has no copy buttons | `resume/facts.yaml` doesn't exist — only `facts.example.yaml` ships. | `cp resume/facts.example.yaml resume/facts.yaml`, then edit it. `resume/` is bind-mounted, so a page refresh picks it up — no restart. |
| The `/kit` "Apply Autofill" bookmarklet does nothing | It's dragged to the bookmarks bar but clicked on a non-supported form, or `facts.yaml` was edited after you saved it. | It autofills Greenhouse / Lever / Ashby forms only. Re-drag it from `/kit` after editing `facts.yaml`. |
| Gmail badges never appear on `/board` | 2-Step Verification off, wrong app password, or `.env` not re-read. | Generate a Gmail **app password** (requires 2FA), set `JOB_AGG_GMAIL_ADDRESS` + `JOB_AGG_GMAIL_APP_PASSWORD`, then `docker compose up -d --force-recreate`. |
| Triage inbox is empty though alerts fired | The inbox shows only matches notified **after** the triage feature shipped — the pipeline persists display fields at notify time; older rows are id+score+gaps only. | New matches populate it automatically. |
| A triaged match disappeared | "New" and "Dismissed" matches expire after the 60-day TTL. | Move a match to Interested/Applied/Interviewing — that drops the TTL so it persists. |
| `/pipeline` CloudWatch panel is blank (AWS) | It reads logs with your local creds; per-tier stats rely on the `tier` field on the `invocation_done` line. | Ensure local creds have `logs:FilterLogEvents`; only cycles that ran after the relevant deploy show tier stats. Override the group with `JOB_AGG_LOG_GROUP`. |
| `/analytics` stretch-skills panel shows a hint | `gap_analysis` isn't enabled, or no annotated matches yet. | Enable `gap_analysis` (Getting Started §3f); it populates as matches accrue. |
| Tailor deep-link in an alert doesn't work | `JOB_AGG_TAILOR_SIGNING_SECRET` / `JOB_AGG_TAILOR_ENDPOINT_URL` unset, the URL isn't reachable from your phone, or the résumé artifacts are missing. | Set both env vars to a reachable URL (LAN IP or Tailscale/Cloudflare Tunnel); create `resume/content.json` + `resume/evidence.json` (see `resume/README.md`). |

---

## Still stuck?

- **Read the tests.** Every behavior is covered by a small test (most 5–15
  lines). `grep` a function name in `tests/` for a worked example.
- **Turn on a dry-run.** `python -m src.handler --tier ats --dry-run` shows
  exactly what the pipeline does without side effects.
- The runbooks in [`docs/runbooks/`](docs/runbooks/) cover relevance calibration
  and the hosted tailor endpoint in more depth.
