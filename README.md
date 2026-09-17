# Job Aggregator

A personal job-alert pipeline. It polls public ATS endpoints and a few job-board aggregators on a schedule, filters new postings against your hard requirements, asks an LLM to score how well each survivor matches your written profile, and pushes the keepers to your phone (ntfy) and a Discord channel.

It runs on a machine you own — Docker Compose with SQLite state, no cloud account required.

> **Just want it running?** → **[GETTING_STARTED.md](GETTING_STARTED.md)** walks you through setup end-to-end and how to tailor it to your job preferences.
> **Something broken?** → **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**.
> **What changed?** → **[CHANGELOG.md](CHANGELOG.md)** (release process in [RELEASING.md](RELEASING.md)).

## What it does

- **Polls many sources, often.** Company boards across fifteen ATS families (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, Rippling, Personio, Recruitee, Teamtailor, Oracle Cloud, Eightfold, Phenom, Taleo, and iCIMS / JSON-LD boards) — plus an optional **headless-browser tier** (Avature) for JS-gated sites, HN "Who Is Hiring," Remotive, RemoteOK, and an optional Adzuna API connector for off-ATS inventory — small companies and staffing agencies that don't run a major ATS (free self-service API key). A handful of boards ship in `config.example.yaml`; add companies with `python -m src.settings add-source` or by importing an edited `config.yaml`, or let discovery grow the list to hundreds over time. A Hiring.cafe connector also ships but is disabled: hiring.cafe now disallows the search endpoint it relied on, and the project does not work around that.
- **Hard-filters on your criteria.** Title regex, seniority, location (a config-driven country allowlist — remote postings must be reachable from an allowed country — plus a city allowlist for onsite/hybrid), comp floor, stack-keyword overlap, and a freshness window (`max_age_days`).
- **LLM-scores survivors** against `profile.md`. Postings scoring at or below `relevance.score_low` are suppressed; the rest get notified with the score and a one-line rationale.
- **Flags résumé gaps (opt-in, off by default).** When `gap_analysis.enabled` is set, each notify-worthy posting gets a second LLM pass listing the hard skills the role wants but your résumé doesn't show — surfaced per-posting as a Discord "Stretch areas" field and rolled into a weekly digest. The résumé only annotates; it never feeds the relevance score, so recall is unchanged.
- **Notifies twice per match.** ntfy.sh (push to your phone) + Discord webhook. Quiet hours mute ntfy overnight in your timezone.
- **Dedups across runs.** A state table tracks every job_id ever notified, so you see each posting exactly once.
- **Self-expands, hands-off.** A daily discovery tier validates ATS slugs (yc-oss + a manual list) *and* fingerprints a seed list of enterprise careers pages, adding healthy boards to the active poll set with no code or settings changes. Name-based candidates run a **conversion chain**: up to 3 slug variants (normalized name, website domain label, source slug) probed across all 6 slug ATSs, then a careers-page fingerprint fallback — so a YC company on Workday still converts. Misses record what was tried and retry the full chain after 90 days. Aggregator sightings (Adzuna) double as a discovery source: postings from companies you don't poll directly are classified from their apply URLs and staged as candidates for the same validation pipeline. Configured VC portfolios (a16z today) are re-fetched weekly and staged into the same candidate pipeline. A poll-health circuit breaker suppresses boards that go dead.
- **Explains every rejection.** Every posting the filters drop or the scorer suppresses is written to an audit trail with the reason, browsable at `/audit` — rescue a wrongly-dropped posting or confirm the call. Optional **ops push-alerts** fire when the pipeline stalls, yields nothing for 12h, or the LLM degrades.
- **Self-tunes (opt-in).** `scripts/tune_thresholds.py` mines your `/audit` verdicts into suggested `score_low` / title-regex changes — it never edits config, it recommends.
- **Tracks applications end-to-end.** A kanban board with a daily closed-posting sweep + digest, an optional read-only Gmail sweep that badges rejections/receipts as suggestions, and an apply-kit page (`/kit`) of tap-to-copy form answers.
- **Ships a local web UI.** A triage inbox, an application board, a rejection audit view, a pipeline/ops dashboard, and match analytics (see [Web UI](#web-ui)).

## How it works

```
schedule (ats / slow / headless / discovery / digest)
        │
        ▼
   handler ── tier=ats │ slow │ headless │ discovery │ digest
        │
        ▼
   ┌── Connectors (parallel, per-tier) ───────────────────────────
   │  ats        Greenhouse · Lever · Ashby · Workable · SmartRecruiters
   │             · Workday · Rippling · Oracle Cloud · Eightfold
   │             · Phenom · Taleo · iCIMS/JSON-LD
   │  headless   Avature (Playwright — for JS-gated boards)
   │  slow       HN Who Is Hiring · Remotive · RemoteOK · Adzuna
   │  discovery  new ATS slugs + board fingerprinting + mined candidates
   └──────────────────────────────────────────────────────────────
        │
        ▼ raw postings
   normalize ──► dedup against seen_jobs
        │
        ▼ new postings
   filters (titles, seniority, location, comp, stack, age)
        │
        ▼ keyword-passing postings
   LLM relevance scorer (vs profile.md)
        │
        ▼ score > score_low  (survivors)
   résumé gap analysis (opt-in) ──► "Stretch areas" annotate the posting
        │
        ▼
   fan-out to ntfy + Discord (quiet hours apply to ntfy)
```

A separate tier, `digest`, runs on its own weekly schedule independent of the per-cycle flow: it scans `seen_jobs` for the last 30 days, tallies your most common résumé gaps, and posts a single Discord summary. It does no polling and is skipped unless `gap_analysis.enabled`.

Five tiers run on independent schedules:

| Tier | Default cadence | What it does |
|---|---|---|
| `ats` | every 10 min | Polls the httpx ATS families (Greenhouse … Taleo, iCIMS/JSON-LD). Cheap, low-latency. Fetches run with bounded concurrency (semaphore) so the connection pool can't be exhausted as connectors grow. |
| `slow` | every 15 min | HN Who Is Hiring + aggregators. Rate-friendlier endpoints. |
| `headless` | every ~45 min | JS-gated boards that need a real browser (Avature). Playwright/Chromium; opt-in via the `[headless]` extra. |
| `discovery` | every 24 h | Drains aggregator-sighted (Adzuna) and VC-portfolio candidates first, then runs the conversion chain over yc-oss + `manual_companies` (slug variants → careers-page fingerprint) and fingerprints the enterprise seed list — all within one probe budget that reserves a slice for revalidating known-good boards. Fully-exhausted misses re-probe last, from leftover budget only. Promotes healthy boards into the active poll set. Re-fetches configured VC portfolios weekly (inert by default). No notifications. |
| `digest` | weekly (Mon) | Posts a Discord summary of your most common résumé gaps over the last 30 days. No polling. Skipped unless `gap_analysis.enabled`. |

### State and secrets

SQLite holds everything — including your settings (`settings_versions`, `documents`, `secrets`, `config_generation`) and `seen_jobs` (dedup; also stores each notified posting's relevance score, résumé gaps, and triage/board status + application history), `source_state` (per-connector ETag / cursor), `discovered_slugs` and `discovered_boards` (auto-discovered startup slugs and enterprise boards, with health), `connector_health` (a poll-health circuit breaker that auto-suppresses connectors which 404/410 repeatedly, re-probed daily), `rejected_postings` (the audit trail — what was filtered/suppressed and why), `pipeline_events` (per-cycle telemetry behind `/pipeline`), and `ops_alert_state` (cooldowns for the degraded / zero-yield / pipeline-stopped push alerts).

State lives in a single SQLite file at `./data/job_aggregator.db`.

Secrets (ntfy URL, Discord webhook, your provider's API key) come from `JOB_AGG_*` environment variables (`.env`) or the database (`python -m src.settings set-secret`); a non-empty env var wins.

### Cost

- **Local:** $0 infrastructure (your electricity). LLM cost is $0 if you run Ollama locally, otherwise your provider's rate.

## Web UI

The same local web app (`python -m src.web`, or the `web` service in Docker) serves
several pages over the same state as the pipeline:

> [!WARNING]
> **The web UI has no authentication of any kind, and must never be exposed to the
> internet.** There is no login, no session, and no access control — anyone who can
> reach the port can read everything: your résumé and its tailored variants, every
> job you have applied to and its status, your apply-kit answers (work authorization,
> personal links, EEO responses) and any Gmail-derived data. The Docker `web` service
> binds `0.0.0.0` and publishes `8000:8000`, so on a shared or public network it is
> reachable by anyone who can route to the host. Keep it on a trusted network, put it
> behind a private overlay such as Tailscale, or bind it to loopback by publishing
> `127.0.0.1:8000:8000` in `docker-compose.yml`. Do not port-forward it.


- **Triage inbox** (`/`) — browse, search, and filter notified matches; set a status (New → Interested → Applied → Interviewing, or Dismissed) and click through to apply.
- **Application board** (`/board`) — a kanban view of where each application stands, with a status-history timeline and a staleness badge. Maintains itself with a daily closed-posting sweep + digest, and (opt-in) Gmail-suggested status badges. **Add a job manually** for an opportunity that never came through a connector (a recruiter DM, a referral): it's stored as an ordinary match under the `manual:` source, so it appears on the board, in triage, and in every analytics section — and its description feeds tailoring and gap analysis like any other posting. Hand-added rows carry no relevance score (nothing scored them) and never expire.
- **Rejection audit** (`/audit`) — every posting the pipeline filtered or suppressed, with the gate that dropped it; rescue a wrongly-dropped posting back into the inbox or confirm the rejection (those verdicts feed threshold self-tuning).
- **Pipeline / ops dashboard** (`/pipeline`) — a health strip, connector health, match & score analytics, LLM-failure tally, and cycle stats & fetch failures (rolling 7 days). Each panel is fail-soft.
- **Match analytics** (`/analytics`) — matches over time, where matches come from (by company and ATS), and your most common résumé stretch-skills.
- **Coach** (`/coach`) — on-demand LLM recommendations for improving your application response rate, grounded in your own funnel, audit trail, config, and résumé; keeps a run history.
- **Apply kit** (`/kit`) — a tap-to-copy sheet of your recurring application answers (work authorization, links, EEO), sourced from a gitignored `resume/facts.yaml`.
- **Resume builder** (`/builder`) — manage résumé template packs and rendering settings. Switch between the two built-in designs, upload your own (a Jinja2 HTML file, a zip pack with fonts, or a `.docx` converted once via the LLM and held for your review), and set bullet caps, max pages, and page size/margins. The active template drives every tailored-résumé PDF (the alert deep-links and the CLI); a finished run can be re-rendered in any template instantly, no LLM call. Template contract: [`resume/README.md`](resume/README.md).

See [GETTING_STARTED.md](GETTING_STARTED.md#a4-open-the-web-ui) for how to open it.

## Repo layout

```
src/
  handler.py          pipeline entrypoint + CLI
  scheduler.py        local APScheduler daemon (the Docker poller)
  orchestrator.py     fetch → dedup → filter → score → gaps → notify
  connectors/         one module per source (ATS + aggregators + headless)
  headless.py         Playwright/Chromium factory for the headless tier (Avature)
  fingerprint.py      classify an enterprise careers page → ATS family + identity
  filters.py          title / seniority / location / comp / stack / age rules
  relevance.py        LLM scoring against profile.md
  gaps.py             résumé gap analysis (opt-in; 3 LLM providers, fail-open)
  notify/             ntfy + Discord sinks, message formatting, quiet hours
  discovery.py        slug + candidate validation pipeline (priority-budgeted)
  slugging.py         company name/website → ordered slug variants (conversion chain)
  sightings.py        aggregator sighting capture (Adzuna) → staged discovery candidates
  vc_portfolio.py     VC portfolio drivers (a16z, Sequoia, CSV) — shared by the CLI and weekly auto-discovery
  digest.py           résumé-gap tally + weekly Discord digest
  stores.py           build_stores() — wires the SQLite stores
  settings/           settings service: versions, documents, secrets, import/export CLI
  state.py            shared row types + item shaping for the SQLite stores
  state_sqlite.py     SQLite stores
  web/                local UI (FastAPI + HTMX): triage, board, /audit, /pipeline ops, /analytics, /coach, /kit, /builder
  tailor/             résumé tailoring engine + render/ (template packs → PDF via WeasyPrint) + endpoint/ (deep-link auth, loading page, run orchestration)
scripts/
  capture_fixture.py        saves an ATS response for connector tests
  discover.py               one-shot local discovery run
  discover_enterprise.py    fingerprint a seed CSV of enterprise careers pages (dry-run + --merge)
  tune_thresholds.py        mine /audit verdicts into suggested score_low / title changes
  import_vc_portfolio.py    bulk-import a VC's portfolio into settings
  build_evidence_bank.py    Jira CSV → resume/evidence.json skeleton (for tailoring)
docker-compose.yml    local stack: poller + web + (opt-in) ollama
config.yaml / profile.md   your settings files (gitignored) — load with python -m src.settings import
resume.md.example     template résumé for gap analysis (copy to resume.md — gitignored)
GETTING_STARTED.md    end-to-end setup and tailoring
TROUBLESHOOTING.md    common setup and runtime issues
```

## Configuration

Two files do almost all the customization — you edit them, then import them
into the app database (`python -m src.settings import`); changes apply live:

- **`config.yaml`** — hard filters (titles, seniority, location, comp, stack, freshness), the source lists (companies per ATS + aggregator toggles), LLM provider + score thresholds, quiet hours, schedules, and feature toggles (gap analysis, tailoring, board automation, audit retention, ops alerts, board discovery).
- **`profile.md`** — the prose the LLM grades each surviving posting against (0–10). This is the highest-leverage knob for match quality.
- **`resume.md`** (optional, gitignored) — your markdown résumé, used only by gap analysis.

Seed them once with `cp config.example.yaml config.yaml && cp profile.example.md profile.md`
— both are gitignored, so `git pull` never touches them.

Step-by-step tuning — filters, the relevance profile, providers and calibration, adding companies, gap analysis — is in **[GETTING_STARTED.md §2](GETTING_STARTED.md#2-tailor-it-to-your-job-preferences)**.

The complete flag-by-flag reference (every knob, default, and env secret) is **[docs/CONFIG.md](docs/CONFIG.md)**.

## Extending it

The codebase is ~13,000 lines of Python with strict typing (Pydantic) and a large, small-grained test suite. Recipes are sorted from least to most work.

### Picking your skill tier

| You want to… | Skill needed | Where |
|---|---|---|
| Change titles, stack, comp, locations, companies, quiet hours, LLM provider/thresholds, the LLM judgment criteria, résumé gap analysis | None — edit + import | `config.yaml`, `profile.md`, `resume.md` ([Getting Started §2](GETTING_STARTED.md#2-tailor-it-to-your-job-preferences)) |
| Change polling cadence | None — YAML only | `config.yaml` `schedules:` |
| Add a new notification target (Slack, email, SMS, Telegram) | Beginner Python | `src/notify/` |
| Add a new filter (reject by company, require remote-only) | Beginner Python | `src/filters.py` |
| Tweak the notification message format | Beginner Python | `src/notify/format.py` |
| Adjust how seniority or location is inferred | Intermediate Python | `src/normalize.py` |
| Add a new LLM provider (OpenAI, Mistral, self-hosted) | Intermediate Python | `src/relevance.py` |
| Add a new ATS family (Recruitee, Pinpoint, Teamtailor) | Intermediate Python + reading an HTTP API | `src/connectors/` |
| Add multi-user support or switch to Postgres | Project-sized rewrite | everywhere |

### Prereqs to know before opening the code

- **Async Python.** The whole pipeline is `httpx.AsyncClient` + `asyncio.gather`, with per-run fetch concurrency capped by a semaphore (`run_once(max_concurrency=...)`, default 40). If `async`/`await` are new, read the [Python asyncio tutorial](https://docs.python.org/3/library/asyncio-task.html) first.
- **Pytest with async + mocks.** Tests use `pytest-asyncio` and `unittest.mock.AsyncMock`; `tests/test_relevance.py` shows the pattern.
- **Pydantic v2.** Config and data models are all Pydantic `BaseModel`s — mostly you'll just add fields.

### Recipe: add a new notification sink

Copy `src/notify/discord.py` (the simplest sink) to e.g. `src/notify/slack.py`. The `Sink` protocol is two lines (`name: str`, `async def send(client, payload) -> None`) — see `src/notify/base.py`. Register it in `handler._build_sinks`:

```python
# src/handler.py
def _build_sinks(cfg: AppConfig) -> list[Sink]:
    return [
        NtfySink(...),
        DiscordSink(...),
        SlackSink(webhook_url=cfg.secrets.slack_webhook_url),  # new
    ]
```

Add `slack_webhook_url: str = ""` to `Secrets` in `src/config.py` — the settings service resolves it automatically (`JOB_AGG_SLACK_WEBHOOK_URL`, else `python -m src.settings set-secret slack_webhook_url`); add its row to the secrets table in docs/CONFIG.md. Discord is the cleanest template. Write a test that mocks `httpx.AsyncClient.post`; `tests/notify/test_discord.py` is the template.

### Recipe: add a new filter

`src/filters.py` defines one function per filter, each returning `Verdict.MATCH | REJECT | UNKNOWN`. `evaluate()` runs them in sequence; the first `REJECT` short-circuits.

```python
# src/filters.py — add a function:
def filter_blocked_companies(p: NormalizedPosting, f: FiltersConfig) -> Verdict:
    if p.company.lower() in {c.lower() for c in f.blocked_companies}:
        return Verdict.REJECT
    return Verdict.MATCH

# Call it from evaluate():
def evaluate(p, f):
    ...
    v = filter_blocked_companies(p, f); rec(v, "blocked_companies")
```

Add `blocked_companies: list[str] = Field(default_factory=list)` to `FiltersConfig` in `src/config.py`, set it in `config.yaml` and import, and write a test in `tests/test_filters.py`.

### Recipe: add a new LLM provider

Mirror `GeminiRelevanceScorer` / `OllamaRelevanceScorer` in `src/relevance.py`:

1. **New class.** Copy `GeminiRelevanceScorer` to e.g. `OpenAIRelevanceScorer` — same `__init__`, same `async def score(posting) -> Score`, same fail-open `except Exception`. Swap the SDK call. (For providers without schema enforcement, the shared `_parse_score_json` helper does tolerant JSON parsing — that's how the Ollama scorer works.)
2. **Widen the Literal.** `RelevanceConfig.provider` in `src/config.py` is `Literal["anthropic", "gemini", "ollama"]` — add your value.
3. **Add the secret.** `Secrets.openai_api_key: str = ""`, read from `JOB_AGG_OPENAI_API_KEY`.
4. **Add the SDK to `pyproject.toml`.**
5. **Branch in the handler.** `_build_relevance_scorer` in `src/handler.py` has an `anthropic / gemini / ollama` ladder — add another branch.
6. **Tests.** Copy the Gemini/Ollama tests in `tests/test_relevance.py` (happy path, network error, rate limit, malformed/empty JSON, missing fields, clipping, truncation, timeout).

### Recipe: add a new ATS family

The hardest common extension — you're learning that platform's API quirks (pagination, ETags, rate limits, error shapes), not just writing Python.

1. **Read an existing connector.** `src/connectors/greenhouse.py` is the simplest; `src/connectors/workday.py` is the messiest. Each implements the `Connector` protocol (`name`, `tier`, `async def fetch(client, state) -> FetchResult`).
2. **Write your connector** in `src/connectors/<your_ats>.py`. `FetchResult` returns `RawPosting`s plus the new `ConnectorState` (ETag / cursor caching, optional). See how `greenhouse.py` handles 304 Not Modified.
3. **Capture a fixture.** `scripts/capture_fixture.py <ats> <slug>` saves a JSON file under `tests/fixtures/` to replay without network.
4. **Write tests** mirroring `tests/connectors/test_greenhouse.py` (use `respx` to mock HTTP).
5. **Wire into `build_connectors`** in `src/connectors/base.py` — add it to `ctor_for` and the per-tier section.
6. **Add a config field.** `SourcesConfig.<your_ats>: list[str]` in `src/config.py`; for a slug-list family, also add it to `SLUG_SOURCE_FAMILIES` (so `add-source` accepts it); populate via `config.yaml` + import.

Optionally add the family to the validator in `src/discovery.py` so discovery probes it too, or — for enterprise boards with a machine-classifiable URL shape — to `src/fingerprint.py` so the daily sweep auto-discovers it.

**JS-gated boards** (endpoints that return nothing to a plain HTTP client, like Avature) implement the same `Connector` protocol but declare `tier="headless"` and receive a Playwright `page` instead of an `httpx` client. `src/headless.py` owns the browser lifecycle; `src/connectors/avature.py` is the template. Keep the parser a pure, fixture-tested function — only the fetch driver differs — and gate the browser dependency behind the `[headless]` extra.

### When in doubt

Read the tests. Every behavior is covered by a small test (most 5–15 lines). To learn what a function does, `grep` its name in `tests/` for a worked example.

## Reference

- **Setup and tailoring:** [GETTING_STARTED.md](GETTING_STARTED.md)
- **Common issues:** [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- **Relevance calibration runbook:** [`docs/runbooks/calibrating-relevance-scores.md`](docs/runbooks/calibrating-relevance-scores.md)
- **Upgrading a running local stack:** [`docs/runbooks/deploy-local-stack.md`](docs/runbooks/deploy-local-stack.md)
- **Release process:** [RELEASING.md](RELEASING.md) · changes in [CHANGELOG.md](CHANGELOG.md)
- **Security policy & threat model:** [SECURITY.md](SECURITY.md)

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).

Copyright 2026 Sean Campbell

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied.
