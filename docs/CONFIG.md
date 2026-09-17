# Configuration Reference

Every configuration flag in the `AppConfig` tree — `config.yaml` plus the env
secrets — one row per flag. (Runtime-only env vars such as `JOB_AGG_OLLAMA_HOST`
or the web host/port are not config flags and are covered in GETTING_STARTED
where their features are set up.) Configuration comes from two places:

- **`config.yaml`** — everything below except secrets — untracked and personal;
  seed it from `config.example.yaml`. The file is
  bind-mounted into the containers: edit, then
  `docker compose restart poller web` (no `--build` needed).
- **Environment secrets** — the [secrets](#secrets) table at the bottom:
  `JOB_AGG_*` env vars in `.env` (for Docker Compose).

Sections omitted from config.yaml run entirely on the defaults listed here.
Setup narrative lives in [GETTING_STARTED.md](../GETTING_STARTED.md); this
page is the lookup table. The drift-guard test `tests/test_config_docs.py`
fails CI whenever a flag is added to `src/config.py` without a row here (or
a row outlives its flag).

## filters

Hard gates run on every posting before LLM scoring — a posting rejected here
is never scored. Details and tuning advice: GETTING_STARTED §2a.

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `filters.titles` | (required) | Title allowlist. A posting must match at least one entry — each entry matches as an exact phrase, case-insensitive, on word boundaries (no regex syntax; add variants like `full-stack engineer` / `fullstack engineer` explicitly). |
| `filters.seniority_allow` | (required) | Allowed seniority bands inferred from the title: any of `junior`, `mid`, `senior`, `staff`. |
| `filters.location.allowed_countries` | `[US]` | ISO 3166-1 alpha-2 codes (`UK` accepted as `GB`). A remote posting passes when its stated area covers one of these; onsite/hybrid postings gate on `allowed_cities`. Must not be empty. |
| `filters.location.allowed_cities` | `[]` | Lowercase city names that onsite/hybrid postings may be in. |
| `filters.location.remote_policy` | `allowed_countries` | `anywhere` disables the remote geo gate entirely (any remote posting passes); `allowed_countries` applies the coverage rule above. |
| `filters.location.allow_unknown` | `true` | Postings whose location can't be parsed pass through to scoring instead of being rejected. |
| `filters.location.remote_must_be_us` | `null` | **Deprecated** pre-v0.6 key: `true` maps to `remote_policy: allowed_countries`, `false` to `anywhere`. Warns at load; setting both keys is an error. Migrate to `remote_policy`. |
| `filters.comp_floor_usd` | (required) | Reject postings whose advertised **minimum** comp is below this — a $150k–$200k range with a $160k floor is rejected. Raw-number comparison, USD-only (a `€85.000` posting is not converted). Postings advertising no comp — or only a maximum — are never comp-rejected. `0` disables. |
| `filters.stack_any_of` | (required) | A posting must mention at least one listed technology. |
| `filters.max_age_days` | `null` | Reject postings older than N days (`null` = no age gate). `config.example.yaml` leaves it unset; `2` is the recommended value once you are past the first run — postings are alert-worthy only while fresh. |
| `filters.blocked_companies` | `[]` | Companies to hard-reject regardless of which source surfaced them. An entry matches when its words appear **consecutively as whole words** in the posting's company name — so `microsoft` covers `Microsoft Corporation` and the slug-derived `Eightfold:Microsoft`, while `apple` does **not** match `Applebee's`. Case- and punctuation-insensitive; not a substring test. Multi-word entries (`career launch`) match as a phrase. Runs before the role gate, so rejections are audited under the `company` gate. Purely additive to `sources` — a blocked company's board keeps being polled, its postings are just dropped; delete the entry and they return on the next poll. `[]` disables the gate. |
| `filters.blocked_employment_types` | `[contract, temporary, part_time, internship]` | Employment types to hard-reject when a posting's type is **known**. Valid values: `full_time`, `part_time`, `contract`, `contract_to_hire`, `temporary`, `internship`. Only connectors that expose the signal populate it (currently Hiring.cafe's `commitment` and Lever's `categories.commitment`); postings with an **unknown** type are never rejected here (fails open, so recall is unchanged). `contract_to_hire` is treated as distinct from `contract` and is allowed by default. Set to `[]` to disable the gate. |

## quiet_hours

Suppresses **phone pushes** (ntfy) during a nightly window; Discord delivery
is unaffected, so nothing is lost. All three keys required.

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `quiet_hours.timezone` | (required) | IANA zone name, e.g. `America/Los_Angeles`. |
| `quiet_hours.start` | (required) | Window start, `HH:MM` (quote values with a leading zero — YAML). |
| `quiet_hours.end` | (required) | Window end, `HH:MM`. |

## sources

What gets polled. Two kinds of entries: **slug-list families** (one string
per company board, added with `./scripts/add_company.sh <family> <slug>`)
and **structured families** (hand-curated mapping entries — see the example
block below the table). Adding/removing companies: GETTING_STARTED §2d.

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `sources.greenhouse` | `[]` | Slugs from `boards.greenhouse.io/{slug}`. |
| `sources.lever` | `[]` | Slugs from `jobs.lever.co/{slug}`. |
| `sources.ashby` | `[]` | Slugs from `jobs.ashbyhq.com/{slug}`. |
| `sources.workable` | `[]` | Slugs from `apply.workable.com/{slug}/`. |
| `sources.smartrecruiters` | `[]` | Slugs from `jobs.smartrecruiters.com/{slug}`. |
| `sources.rippling` | `[]` | Slugs from `ats.rippling.com/{slug}/jobs`. |
| `sources.personio` | `[]` | Slugs from `{slug}.jobs.personio.de` (EU-primary ATS; default-language XML feed, so German-only tenants surface German text — the LLM gate absorbs it). |
| `sources.recruitee` | `[]` | Slugs from `{slug}.recruitee.com` (EU-primary ATS). |
| `sources.teamtailor` | `[]` | Slugs from `{slug}.teamtailor.com/jobs` (EU-primary ATS). |
| `sources.workday` | `[]` | Structured: `{tenant, region, site}` per board — `microsoft.wd1.myworkdayjobs.com/External` → `tenant: microsoft, region: wd1, site: External`. Two sites on one tenant are distinct boards. |
| `sources.oraclecloud` | `[]` | Structured: `{tenant, region, site, company?}` for Oracle Recruiting Cloud; `site` is the CandidateExperience number, usually `CX_1`. |
| `sources.eightfold` | `[]` | Structured: `{slug, domain, flavor?, company?}`; `domain` is the API's required `domain=` param (not always the corporate TLD), `flavor` is `pcsx` (default) or `apply_v2`. |
| `sources.jsonld_boards` | `[]` | Structured: `{family, slug, base_url, company?}` for SuccessFactors / iCIMS / TalentBrew boards scraped via HTML/RSS + JSON-LD; `base_url` is the branded careers domain. |
| `sources.phenom` | `[]` | Structured: `{careers_url, company?}` for Phenom People sites (branded URLs, hand-curated). |
| `sources.taleo` | `[]` | Structured: `{tenant, section, company?}` — `cinfin.taleo.net/careersection/ex` → `tenant: cinfin, section: ex`. Modern template only. |
| `sources.avature` | `[]` | Structured: `{careers_url, company}`. Avature serves HTTP 202 to non-browsers, so these poll on the headless (Playwright) tier — see GETTING_STARTED "Avature / headless connector". |
| `sources.hn_who_is_hiring.enabled` | `true` | Monthly HN "Who is hiring?" thread (slow tier). |
| `sources.remotive.enabled` | `true` | Remotive aggregator (slow tier). |
| `sources.remoteok.enabled` | `true` | RemoteOK aggregator (slow tier). |
| `sources.hiringcafe.enabled` | `false` | hiring.cafe meta-aggregator (slow tier). **Non-functional; leave it off.** hiring.cafe robots-disallows the search endpoint the connector used and challenges every page it touched, and the project does not work around that (see the status note in `src/hiringcafe.py`). Enabling it only logs a fetch failure every slow cycle. |
| `sources.hiringcafe.max_postings_per_cycle` | `500` | Cap on postings emitted per cycle across all hiring.cafe searches. Inert while the connector is disabled. |
| `sources.hiringcafe.location` | `null` | Geographic scope for the primary search: ISO-2 country code or continent name (`Europe`, `Asia`, `North America`, `South America`, `Africa`, `Oceania`, `Antarctica`). Unset, hiring.cafe geo-defaults results by the server's view of your egress IP — pin it to make results deterministic. Inert while the connector is disabled. |
| `sources.hiringcafe.extra_queries` | `[]` | Extra searches per cycle, deduped by posting id, sharing the cap. Entries are plain strings (keyword search) or `{query, location}` mappings for geo-scoped searches — see [examples/eu-config.md](examples/eu-config.md). Inert while the connector is disabled. |
| `sources.adzuna.enabled` | `false` | Adzuna job-search API aggregator (slow tier): off-ATS discovery — small companies and staffing agencies that don't run a major ATS. Hybrid source: alerts directly AND feeds Sightings into board discovery. Requires both `JOB_AGG_ADZUNA_*` [secrets](#secrets); enabled without them logs an error and skips. |
| `sources.adzuna.countries` | `[us]` | ISO 3166-1 alpha-2 country codes, one country-scoped search endpoint each (`us`, `gb`, `de`, ...). |
| `sources.adzuna.queries` | `[]` | Search keyword queries run per country each cycle (e.g. `staff software engineer`). Required non-empty when enabled. |
| `sources.adzuna.max_days_old` | `2` | Server-side freshness window passed to the API; the central `filters.max_age_days` gate still applies. |
| `sources.adzuna.results_per_page` | `50` | Results per API call (Adzuna max 50). One page per query — depth is a non-goal. |
| `sources.adzuna.daily_call_budget` | `60` | Hard stop on API calls per UTC day (persisted across cycles). Keeps the 2,500/month free-tier cap safe with headroom. |

Structured-family example (shapes for the seven mapping families):

```yaml
sources:
  workday:
  - {tenant: microsoft, region: wd1, site: External}
  oraclecloud:
  - {tenant: egug, region: us2, site: CX_1, company: American Express}
  eightfold:
  - {slug: northrop, domain: ngc.com, company: Northrop Grumman}
  jsonld_boards:
  - {family: icims, slug: steel, base_url: "https://careers-steel.icims.com"}
  phenom:
  - {careers_url: "https://careers.fisglobal.com/us/en/search-results", company: FIS}
  taleo:
  - {tenant: cinfin, section: ex, company: Cincinnati Financial}
  avature:
  - {careers_url: "https://careers.jacobs.com/en_US/careers/SearchJobs", company: Jacobs}
```

## schedules

Poll cadence per tier. Intervals are read by the scheduler daemon.

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `schedules.ats_minutes` | (required) | Fast-tier interval: direct ATS boards (shipped: 10). |
| `schedules.slow_minutes` | (required) | Slow-tier interval: aggregators — HN, Remotive, RemoteOK, Adzuna (shipped: 15). |
| `schedules.discovery_hours` | `24` | Discovery-tier interval (candidate validation sweeps). |
| `schedules.headless_minutes` | `45` | Headless (Playwright/Avature) tier interval. |
| `schedules.digest_cron` | `"0 13 * * 1"` | UTC cron for the weekly digest tier (gap-analysis skills digest — Mondays 13:00 UTC). |

## discovery

Automatic board discovery: finding companies/boards you haven't hand-listed.
Deep dive: GETTING_STARTED "Automated board discovery", "Aggregator
candidate mining", and "VC portfolio auto-discovery".

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `discovery.enabled` | `false` | Master switch for the whole discovery tier. |
| `discovery.max_validations_per_run` | `200` | Cap on candidate probes per discovery run (`config.example.yaml` leaves the default; raise it if you enable VC portfolios or aggregator mining). Each candidate costs up to 9 probe requests (one per supported ATS family). |
| `discovery.revalidate_after_days` | `7` | Healthy discovered slugs are re-checked this often. |
| `discovery.no_match_revalidate_after_days` | `21` | Companies that previously matched no board are retried this often. Pair it with `filters.max_age_days`: a company that adopts an ATS mid-window is invisible until the next probe, and by then its backlog is too old to survive the age gate — so a long cadence costs fresh postings, not just stale ones. |
| `discovery.quarantine_after_failures` | `5` | Consecutive probe failures before a discovered slug is quarantined. |
| `discovery.yc_oss_enabled` | `true` | Mine the yc-oss company feed for candidates. |
| `discovery.yc_oss_min_team_size` | `10` | Skip YC companies smaller than this. |
| `discovery.manual_companies` | `[]` | Company slugs always included in the sweep (e.g. `anthropic`). |
| `discovery.board_discovery_enabled` | `true` | Enterprise-board sweep (Workday & friends) over the seed lists. |
| `discovery.board_max_sweeps_per_run` | `60` | Cap on board-candidate sweeps per run. |
| `discovery.board_revalidate_after_days` | `14` | Healthy discovered boards re-checked this often. |
| `discovery.board_quarantine_after_failures` | `5` | Consecutive failures before a discovered board is quarantined. |
| `discovery.eu_seeds_enabled` | `false` | Opt-in: append `scripts/seeds/eu_companies.csv` (149 curated EU companies) to the board sweep — see [examples/eu-config.md](examples/eu-config.md). |
| `discovery.hiringcafe_mining_enabled` | `true` | Convert aggregator sightings (Adzuna; hiring.cafe if its connector ever works again) of unknown boards into discovery candidates (needs `sources.adzuna.enabled`). |
| `discovery.candidate_capture_cap` | `50` | Max sightings staged per slow cycle. |
| `discovery.revalidate_reserve` | `100` | Slice of `max_validations_per_run` reserved for revalidation, so a busy discovery run can't starve rechecks. Keep `max_validations_per_run` comfortably above it. |
| `discovery.vc_firms` | `[]` | VC portfolio drivers. `a16z` is the only working driver today; `sequoia` still parses but its driver gets 404 since sequoiacap.com dropped its WordPress API, so its weekly fetch only logs `vc_fetch_failed`. Empty = feature inert. Weekly refresh stages portfolio companies as candidates. |
| `discovery.vc_refresh_days` | `7` | Per-firm portfolio re-scrape cadence. |
| `discovery.vc_capture_cap` | `500` | Staged companies per firm per run (staging is HTTP-free). |

## relevance

LLM scoring of postings that survive the hard filters. Provider setup and
threshold calibration: GETTING_STARTED §2c and
[runbooks/calibrating-relevance-scores.md](runbooks/calibrating-relevance-scores.md).

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `relevance.enabled` | `false` | Master switch. Off, every filtered posting alerts (no scoring). |
| `relevance.provider` | `anthropic` | `anthropic`, `gemini`, or `ollama` (Ollama covers both local and hosted cloud — the base URL comes from the runtime env). |
| `relevance.model` | `claude-haiku-4-5` | Model name passed to the provider. |
| `relevance.score_high` | `7` | Scores ≥ this get the instant phone push; below it (but above `score_low`) postings go to Discord/inbox only. |
| `relevance.score_low` | `3` | Scores ≤ this are suppressed (still recorded — visible in `/audit`). Shipped: 4. Re-calibrate after any provider/model change. |
| `relevance.profile_path` | `profile.md` | The prose profile the LLM grades against. Keep it in sync with the hard filters — it independently down-scores what it's told is a dealbreaker. |
| `relevance.timeout_seconds` | `10` | Per-posting scoring timeout (shipped: 20 for a large local model). |

## gap_analysis

Optional résumé-gap flags on matched postings, plus a weekly skills digest
(the `schedules.digest_cron` tier). Fully inert when disabled.

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `gap_analysis.enabled` | `false` | Master switch. |
| `gap_analysis.resume_path` | `resume.md` | Gitignored markdown résumé. |
| `gap_analysis.provider` | `null` | LLM provider; `null` falls back to the `relevance` values. |
| `gap_analysis.model` | `null` | Model; `null` falls back to `relevance.model`. |
| `gap_analysis.timeout_seconds` | `20` | Per-posting analysis timeout. |
| `gap_analysis.max_skills_per_job` | `6` | Cap on gap skills extracted per matched job. |
| `gap_analysis.digest_window_days` | `30` | Window the weekly digest aggregates over. |

## tailoring

The tailored-résumé engine behind `/tailor` deep links and the local CLI.
Setup: GETTING_STARTED "Mobile tap alert → tailored résumé loop".

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `tailoring.enabled` | `false` | Used by the tailor endpoint + CLI; the poller ignores it (builds no engine). |
| `tailoring.content_path` | `resume/content.json` | Gitignored structured résumé bank (bind-mounted locally). |
| `tailoring.evidence_path` | `resume/evidence.json` | Gitignored evidence file backing the grounding guards. |
| `tailoring.provider` | `null` | LLM provider; `null` falls back to the `relevance` values. |
| `tailoring.model` | `null` | Model; `null` falls back to `relevance.model`. |
| `tailoring.timeout_seconds` | `60` | Full-rewrite timeout (shipped: 180 — a ~24-bullet bank on a large local model runs 60-90s). |

## board

The `/board` application kanban and its automation (local scheduler only).
Deep dive: GETTING_STARTED "Board automation".

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `board.stale_after_days` | `10` | Applied/interviewing cards older than this show a staleness badge (shipped: 14). |
| `board.closed_check_cron` | `"30 4 * * *"` | UTC cron: daily sweep marking cards whose posting has closed. |
| `board.digest_cron` | `"0 15 * * *"` | UTC cron: daily stale/closed digest push. |
| `board.web_base_url` | `""` | Absolute base URL (e.g. a Tailscale URL) used as the digest's click-through link to `/board`. Empty = no link. |

## audit

The rejection audit trail feeding the `/audit` UI. Deep dive:
GETTING_STARTED "Rejection audit & ops alerts".

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `audit.enabled` | `true` | Record every rejection (stage + reason) for `/audit` review and threshold tuning. |
| `audit.retention_days` | `90` | Audit rows older than this are pruned. |

## coach

The `/coach` page: on-demand LLM recommendations for improving application
response rates, grounded in the funnel, rejection-audit, config, and résumé
data. One click = one LLM call; runs persist on the local SQLite runtime.

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `coach.enabled` | `true` | Master switch for the /coach page's LLM engine and nav link. |
| `coach.provider` | `null` | LLM provider (`anthropic`/`gemini`/`ollama`); `null` follows `relevance.provider`. |
| `coach.model` | `null` | Model override; `null` follows `relevance.model`. |
| `coach.timeout_seconds` | `120` | LLM call timeout — a full-snapshot analysis is a long generation. |
| `coach.max_jobs` | `100` | Cap on the most-recent pursued jobs included in the snapshot. |
| `coach.window_days` | `90` | Lookback window for the rejection-audit aggregates in the snapshot. |

## ops_notify

Thresholds for operational alerts (pipeline health, not job matches). The
alert **sink URLs live in [secrets](#secrets)** (`JOB_AGG_OPS_*`); with no
ops URL set, these thresholds are inert. Each condition fires once, stays
quiet for `cooldown_hours` while it persists, and sends a one-time recovery
notice when it clears.

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `ops_notify.llm_degraded_cycles` | `2` | Consecutive cycles with LLM scoring failures before alerting. |
| `ops_notify.zero_yield_hours` | `12` | Alert when no postings have been ingested for this many hours. Pipeline-wide: it cannot see one source dying while the rest keep working — that is what the two `source_*` flags below cover. |
| `ops_notify.cooldown_hours` | `6` | Minimum gap between repeats of the same alert while the condition persists. |
| `ops_notify.source_zero_yield_hours` | `24` | Per-connector-family watchdog: alert when a watched family delivers nothing for this long **while other families keep flowing**. Stays silent during a whole-pipeline outage, which `pipeline_stopped` already covers. SQLite runtimes only. |
| `ops_notify.source_min_baseline_rows` | `2000` | How much a family must have delivered over the trailing 14 days to be watched at all. The qualifying bar matters more than the silence window: small bursty families legitimately go quiet for a day. At the default, the watched set is the handful of high-volume families. Raise it to watch fewer. |

## gmail

Read-only Gmail IMAP ingestion producing suggest-only badges on `/board`
(local scheduler only). The feature is **env-gated on the two
`JOB_AGG_GMAIL_*` secrets** — these knobs only tune a configured instance.
Setup: GETTING_STARTED "Gmail ingestion".

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `gmail.check_cron` | `"0 * * * *"` | UTC cron for the hourly sweep. |
| `gmail.first_run_days` | `3` | Lookback window on the very first run. |
| `gmail.lookback_max_days` | `7` | Hard cap on any run's lookback (e.g. after downtime). |
| `gmail.max_messages_per_run` | `200` | Cap on messages examined per sweep. |

## kit

The `/kit` tap-to-copy apply helper.

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `kit.facts_path` | `resume/facts.yaml` | Gitignored label/value facts file rendered with copy buttons. `resume/` is bind-mounted — edit, save, refresh. |

## http

How the poller identifies itself to every site it fetches.

| Flag | Default | What it does / when to touch it |
|---|---|---|
| `http.user_agent` | `job-aggregator/<version> (+<repo url>)` | The `User-Agent` on every outbound request to job boards and ATS endpoints. The default identifies this project honestly and is what you should normally run. Override it only to identify your own deployment differently (for example, with your own contact URL). **If you set it, you are responsible for whether the value you send is consistent with the target site's terms of use.** Blank = the default. |

## secrets

Never in config.yaml. `JOB_AGG_*` env vars (Docker Compose reads
`.env` — note Compose snapshots it at container creation; `--force-recreate`
after edits).

| Flag | Env var | What it does |
|---|---|---|
| `secrets.ntfy_topic_url` | `JOB_AGG_NTFY_TOPIC_URL` | (required) ntfy topic for phone pushes. |
| `secrets.discord_webhook_url` | `JOB_AGG_DISCORD_WEBHOOK_URL` | (required) Discord webhook for the match feed. |
| `secrets.anthropic_api_key` | `JOB_AGG_ANTHROPIC_API_KEY` | For `provider: anthropic`. Empty OK otherwise. |
| `secrets.google_api_key` | `JOB_AGG_GOOGLE_API_KEY` | For `provider: gemini`. Empty OK otherwise. |
| `secrets.ollama_api_key` | `JOB_AGG_OLLAMA_API_KEY` | For hosted Ollama Cloud; leave empty for fully-local Ollama. |
| `secrets.tailor_endpoint_url` | `JOB_AGG_TAILOR_ENDPOINT_URL` | Tailor endpoint base URL for alert deep links. Empty = no deep links. |
| `secrets.tailor_signing_secret` | `JOB_AGG_TAILOR_SIGNING_SECRET` | HMAC secret signing the deep-link tokens. |
| `secrets.ops_ntfy_topic_url` | `JOB_AGG_OPS_NTFY_TOPIC_URL` | Separate ntfy topic for [ops alerts](#ops_notify). Empty (and no ops webhook) = ops alerts off. |
| `secrets.ops_discord_webhook_url` | `JOB_AGG_OPS_DISCORD_WEBHOOK_URL` | Separate Discord webhook for ops alerts. |
| `secrets.heartbeat_url` | `JOB_AGG_HEARTBEAT_URL` | healthchecks.io-style dead-man's-switch ping after each cycle. Empty = no ping. |
| `secrets.gmail_address` | `JOB_AGG_GMAIL_ADDRESS` | Gmail address for [gmail](#gmail) ingestion. Empty = feature off. |
| `secrets.gmail_app_password` | `JOB_AGG_GMAIL_APP_PASSWORD` | Gmail app password (requires 2-Step Verification). |
| `secrets.adzuna_app_id` | `JOB_AGG_ADZUNA_APP_ID` | Adzuna API app_id (free self-service key: developer.adzuna.com). Empty = adzuna connector skipped. |
| `secrets.adzuna_app_key` | `JOB_AGG_ADZUNA_APP_KEY` | Adzuna API app_key. |
