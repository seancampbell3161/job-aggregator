# Getting Started

This guide takes you from a fresh checkout to receiving job alerts that are
tuned to *your* preferences. For what the app is and how it works internally,
see the [README](README.md). When something breaks, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Everything persists in `./data`; $0 infra; LLM cost depends on provider.

---

## Contents

1. [Things you need](#1-things-you-need) — ntfy, Discord, an LLM key
2. [Tailor it to your job preferences](#2-tailor-it-to-your-job-preferences) — the part that makes it yours (full flag reference: [docs/CONFIG.md](docs/CONFIG.md))
3. [Run it with Docker Compose](#run-it-with-docker-compose)
4. [Operating it](#operating-it)
5. [Optional extras](#optional-extras) — mobile tailored-résumé loop, rejection audit & ops alerts, board automation, headless connector, auto board discovery, aggregator candidate mining, apply kit

---

## 1. Things you need

Notifications and (optional) scoring work the same way no matter what else you
configure, so set these up first.

### An ntfy topic (phone push — required)

1. Pick a **hard-to-guess** topic string — anyone who knows the URL can post to
   it. Example: `https://ntfy.sh/<your-random-topic>`.
2. Install the **ntfy** app (iOS App Store / Google Play) and add that topic.
3. Smoke-test it — your phone should buzz:
   ```bash
   curl -d "test" https://ntfy.sh/<your-random-topic>
   ```

### A Discord webhook (required)

In the Discord channel you want alerts in:
**Edit channel → Integrations → Webhooks → New Webhook → Copy Webhook URL**.
It looks like `https://discord.com/api/webhooks/123/abc...`.

### An LLM API key (optional but recommended)

The LLM scores each posting that clears your hard filters against `profile.md`
(0–10) so only good matches notify. Without a key the pipeline still runs —
everything that passes the filters notifies, **unscored**. Pick one provider
(you set which one in `config.yaml` — see
[§2](#2-tailor-it-to-your-job-preferences)):

| Provider | `relevance.provider` | Get a key | Cost |
|---|---|---|---|
| **Anthropic** (Claude Haiku) | `anthropic` | [console.anthropic.com](https://console.anthropic.com/) | Per-token; a few $/day while clearing a backlog, pennies/day at steady state |
| **Google Gemini** | `gemini` | [aistudio.google.com](https://aistudio.google.com) (no card) | Free tier (~1,500 req/day) |
| **Ollama Cloud** | `ollama` | [ollama.com/settings/keys](https://ollama.com/settings/keys) | Flat monthly subscription (GPU-time, not per token) |
| **Ollama, fully local** | `ollama` | none — runs on your box | $0 (local only) |

---

## 2. Tailor it to your job preferences

This is what turns a generic scraper into *your* job alert. Two files do almost
all the work: **`config.yaml`** (hard filters + sources + thresholds) and
**`profile.md`** (the prose the LLM grades each posting against).

Neither file is tracked by git — they're personal, so `git pull` never
conflicts with your edits. Seed them from the tracked templates first:

```sh
cp config.example.yaml config.yaml
cp profile.example.md profile.md
```

> **Applying changes.** `docker compose restart poller web` — both `config.yaml`
> and `profile.md` are bind-mounted, so edits apply on restart with no rebuild.

Every `config.yaml` flag — including the ones this guide doesn't narrate —
is catalogued with its default in **[docs/CONFIG.md](docs/CONFIG.md)**.

### 2a. Hard filters (`config.yaml` → `filters`)

A posting must pass **every** filter (or be `UNKNOWN` on filters that allow it)
before it ever reaches the LLM scorer.

```yaml
filters:
  titles:            # word-boundary regex alternation; case-insensitive
  - software engineer
  - backend engineer
  - product engineer
  seniority_allow:   # subset of: junior, mid, senior, staff
  - mid
  - senior
  location:
    allowed_countries: [US]       # ISO 3166-1 alpha-2; also accepts UK as alias for GB
    remote_policy: allowed_countries  # remote postings must cover an allowed country; "anywhere" disables
    allowed_cities:               # onsite/hybrid postings in these cities pass
    - dallas
    - seattle
    allow_unknown: true           # postings with no location signal pass (LLM judges)
  comp_floor_usd: 120000          # rejects only if comp is *present and below*
  stack_any_of:                   # at least one must appear; UNKNOWN passes
  - python
  - typescript
  - go
  max_age_days: 2                 # rejects postings older than N days
```

> **The #1 "why do I see zero matches?" cause is `max_age_days`.** It's a
> "new in the last N days" firehose, not a market search — adding a company only
> surfaces its *future* postings. While testing, set `max_age_days: null` to see
> the existing backlog, then put it back.

### 2b. Your relevance profile (`profile.md`)

This is the most important file for match quality. The LLM grades each surviving
posting against it on a 0–10 scale, then compares to two thresholds:

```yaml
relevance:
  enabled: true
  provider: anthropic            # or: ollama, gemini
  model: claude-haiku-4-5        # ollama: gpt-oss:120b · gemini: gemini-2.0-flash
  score_high: 7                  # >= this gets a "strong fit" marker in the notification
  score_low: 4                   # <= this is suppressed entirely (no notification)
  profile_path: profile.md
  timeout_seconds: 10            # per-call timeout; on exceed the posting fails open (unscored)
```

Write `profile.md` as if you're briefing a recruiter. Be explicit about:

- **Strong fit (8–10)** — what makes you say "I'd love to interview here."
- **Mild fit (5–7)** — interesting but not exciting; the LLM should land these mid-scale.
- **Weak fit / not interested (1–3)** — hard dealbreakers (geography, title, comp, industry).

`profile.example.md` — the template you copied — is a worked example; the *shape*
matters more than the exact text. The LLM reliably picks up plainly-stated signals like "IC only,
no management track" or "US-based only, no visa transfer."

Targeting a non-US market? Set `location.allowed_countries` accordingly and
update the geography rule in `profile.md` to match — the LLM profile is not
derived from config and will keep down-scoring what the gate now passes. See
`docs/examples/eu-config.md` for a full worked example.

You can iterate on `profile.md` without deploying — run a
[local dry-run](#local-dry-run-tuning-your-profile-without-deploying) and watch
the `would_notify` log lines (each includes the score and rationale).

### 2c. Pick your LLM provider — and re-calibrate after switching

Switch providers with one `config.yaml` line plus the matching key
([§1](#an-llm-api-key-optional-but-recommended)). All three are fail-open: if the
LLM errors or times out, the posting goes through **unscored** rather than being
dropped.

```yaml
relevance:
  provider: gemini
  model: gemini-2.0-flash
```

**Different models produce different score distributions**, so re-check
`score_low` whenever you change `provider` or `model`. The `--calibrate` flag
(layered on `--dry-run`, so it never notifies or writes) scores a sample with no
suppression and prints a 0–10 histogram plus how many postings each candidate
cutoff would silence:

```bash
python -m src.handler --tier ats --calibrate 2>&1 | grep calibration_summary
```

Pick the `score_low` that drops the misfits without silencing keepers. Full
workflow: [`docs/runbooks/calibrating-relevance-scores.md`](docs/runbooks/calibrating-relevance-scores.md).

> **Local-Ollama note:** `gpt-oss:120b`, the model the Ollama recipes use,
> targets hosted Ollama Cloud and is far too big for a typical Mac mini. For **local** Ollama
> pick a model sized to your RAM — `llama3.1:8b`, `qwen2.5:7b`, or
> `gpt-oss:20b` — and re-calibrate, since local models score differently.

### 2d. Add or remove companies (`config.yaml` → `sources`)

The "slug" is the path component on the company's careers URL:

| ATS | URL shape | slug |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/stripe` | `stripe` |
| Lever | `jobs.lever.co/netflix` | `netflix` |
| Ashby | `jobs.ashbyhq.com/ramp` | `ramp` |
| Workable | `apply.workable.com/some-co/` | `some-co` |
| SmartRecruiters | `jobs.smartrecruiters.com/Visa` | `Visa` |
| Workday | `microsoft.wd1.myworkdayjobs.com/External` | `tenant=microsoft, region=wd1, site=External` |
| Rippling | `ats.rippling.com/acme/jobs/...` | `acme` |
| Personio | `everphone.jobs.personio.de` | `everphone` |
| Recruitee | `sendcloud.recruitee.com` | `sendcloud` |
| Teamtailor | `tibber.teamtailor.com/jobs` | `tibber` |

```bash
./scripts/add_company.sh greenhouse stripe
# Workday tenants are added by hand in config.yaml:
#   workday:
#   - tenant: microsoft
#     region: wd1
#     site: External
```

`scripts/discover_enterprise.py` auto-detects **iCIMS** boards (scraped via
their static in_iframe listings + each job's schema.org JSON-LD) and merges
them into `sources.jsonld_boards` — review the dry-run report, then `--merge`
and `docker compose restart poller`. **SuccessFactors and TalentBrew** boards
use the same connector but live on branded careers domains that aren't
machine-derivable, so add them by hand under `sources.jsonld_boards`:

```yaml
sources:
  jsonld_boards:
  - {family: successfactors, slug: aosmith, base_url: "https://jobs.aosmith.com", company: "A. O. Smith"}
  - {family: talentbrew, slug: chevron, base_url: "https://jobs.chevron.com", company: "Chevron"}
```

`scripts/discover_enterprise.py` also auto-detects **Eightfold.ai** boards
(native JSON API — pcsx/apply_v2 flavors probed automatically, the required
`domain=` param scraped from each careers page). Matched tenants merge into
`sources.eightfold`; review the dry-run report, then `--merge` and
`docker compose restart poller`.

`scripts/discover_enterprise.py` also auto-detects **modern Oracle Taleo**
career sections (the `searchjobs` REST API). It probes and verifies each
`{tenant}.taleo.net/careersection/{section}` — legacy-template and
migrated-off tenants fail the verify and stay `unsupported`, so only working
modern boards merge into `sources.taleo`. Multi-section tenants surface their
primary section; add the rest by hand.

**Phenom People** career sites (e.g. FIS, GE HealthCare, RTX) are polled via
their static sitemap + per-job schema.org JSON-LD. Their branded careers
domains aren't machine-derivable, so add each by hand under `sources.phenom`:

```yaml
sources:
  phenom:
  - {careers_url: "https://careers.fisglobal.com", company: "FIS"}
  - {careers_url: "https://jobs.gehealthcare.com", company: "GE HealthCare"}
```

The connector fetches per-job detail only for jobs modified since the last
cycle (a lastmod watermark), so steady-state polling is cheap; the first
cycle sweeps jobs modified within the last 3 days.

**Oracle Recruiting Cloud** boards (`sources.oraclecloud`) are hand-added as
`{tenant, region, site}` triples (e.g. `{tenant: egug, ...}` for American
Express). **Avature** boards are JS-gated and polled on the headless tier — see
[Avature / headless connector](#avature--headless-connector-optional) below.

Remove a company by deleting its slug. Already-seen rows are harmless and
self-expire. The daily `discovery` tier keeps adding healthy yc-oss slugs on its
own — and, hands-off, fingerprints the enterprise seed list and starts polling
whatever it can classify (see
[Automated board discovery](#automated-board-discovery-optional)). To bulk-add a
VC's portfolio (a16z is wired up):
`uv run python scripts/import_vc_portfolio.py a16z --merge` — or skip the CLI
and let the daily tier handle it: see
[VC portfolio auto-discovery](#vc-portfolio-auto-discovery-optional).

Each name-based candidate (yc-oss or `manual_companies`) runs a **conversion
chain**, not a single guess: up to 3 slug variants — the normalized name
(punctuation and trailing `Inc/LLC/Ltd/Corp` stripped), the website's domain
label (`airbyte.com` → `airbyte`), and the source's own slug — each probed
across all 6 slug-based ATSs; if every variant misses and the company has a
website, its careers page is fingerprinted, so companies on Workday / Oracle
Cloud / Eightfold / Taleo / iCIMS convert into board polling too. Companies
that miss everything are recorded with *what was tried* and retry the full
chain after 90 days — fully-exhausted ones only with budget nothing else
claimed, so the retry treadmill never crowds out fresh candidates. No knobs to
set; it's how the crawl works. Watch `discovery_probe_phase_done` (`ok`,
`board_ok`, `no_match`) and `discovery_exhausted_drain_done` on the daily run.

### 2e. Toggle aggregator sources, quiet hours, cadence

```yaml
sources:
  hn_who_is_hiring: { enabled: true }   # monthly thread; cheap, high-noise
  remotive:         { enabled: true }
  remoteok:         { enabled: true }
  hiringcafe:       { enabled: false }  # non-functional (blocked upstream) — leave off
  adzuna:                               # off-ATS inventory (small cos, staffing)
    enabled: false                      # needs JOB_AGG_ADZUNA_APP_ID/_APP_KEY —
    countries: [us]                     #   free key: developer.adzuna.com
    queries: [staff software engineer]  # required when enabled; flags: docs/CONFIG.md

quiet_hours:                            # affects ntfy only (Discord fires 24/7)
  timezone: America/Los_Angeles
  start: '23:00'
  end: '07:00'

schedules:                              # drives the poller cadence
  ats_minutes: 10
  slow_minutes: 15
  discovery_hours: 24
```

### 2f. Résumé gap flags (optional, off by default)

When `gap_analysis.enabled: true`, every posting that survives filtering *and*
scoring gets a second LLM pass listing the hard skills the role wants but your
résumé doesn't show — surfaced as a Discord "Stretch areas" field and rolled into
a weekly digest. The résumé only annotates; it never affects the relevance score,
so recall is unchanged.

```bash
cp resume.md.example resume.md        # then fill in your real experience
# set gap_analysis.enabled: true in config.yaml
```

`resume.md` is gitignored PII. No new key is needed — it reuses your
`relevance.provider`. (Locally, also bind-mount it: uncomment the
`./resume.md:/app/resume.md:ro` lines in `docker-compose.yml`.)

---

## Run it with Docker Compose

Run the whole pipeline 24/7 on one machine with SQLite state.
Three services: **poller** (scrape → filter → score → notify on the
`schedules.*` cadence, plus a daily prune), **web** (triage inbox, board,
analytics, ops, and `/tailor`), and **ollama** (opt-in local LLM, only started
with `--profile ollama`).

**Prerequisite:** Docker + Docker Compose (Docker Desktop on macOS/Windows).
Everything else — Python, SQLite, WeasyPrint — lives inside the image.

### A1. Configure

```bash
cp .env.example .env
```

Edit `.env` and set at least your notification secrets:

```bash
JOB_AGG_NTFY_TOPIC_URL=https://ntfy.sh/your-topic
JOB_AGG_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Then tailor `config.yaml` + `profile.md` per
[§2](#2-tailor-it-to-your-job-preferences). Both are bind-mounted into the
containers, so edits apply on restart — no rebuild.

### A2. Set your LLM key in `.env`

Match whatever `relevance.provider` you chose in `config.yaml`:

```bash
# Anthropic
JOB_AGG_ANTHROPIC_API_KEY=sk-ant-...
# or Gemini
JOB_AGG_GOOGLE_API_KEY=...
# or Ollama Cloud (hosted — runs big models without local GPU/RAM):
JOB_AGG_OLLAMA_HOST=https://ollama.com
JOB_AGG_OLLAMA_API_KEY=...
# or fully-local Ollama: leave the key empty, keep the default host
JOB_AGG_OLLAMA_HOST=http://ollama:11434
JOB_AGG_OLLAMA_API_KEY=
```

### A3. Start the stack

```bash
docker compose up -d --build                  # Anthropic / Gemini / Ollama Cloud
# or, for fully-local Ollama:
docker compose --profile ollama up -d --build
docker compose exec ollama ollama pull llama3.1:8b   # pull your model once
```

The first build takes a few minutes (it installs WeasyPrint's native libraries).

### A4. Open the web UI

`http://localhost:8000` — or `http://<this-box-lan-ip>:8000` from your phone or
laptop on the same network.

> [!WARNING]
> **The UI has no authentication — never expose it to the internet.** Anyone who
> can reach port 8000 can read your résumé, every application and its status, and
> your apply-kit answers (work authorization, links, EEO). The LAN address above
> is reachable by every device on that network, which is fine at home and not
> fine on shared or public Wi-Fi. To reach it away from home use
> [Tailscale](#reach-it-from-your-phone-anywhere-tailscale) rather than
> port-forwarding. To restrict it to this machine only,
> publish `127.0.0.1:8000:8000` in `docker-compose.yml`.

Everything persists in **`./data`** (the SQLite DB + generated PDFs), which is
git-ignored. Back it up by copying that folder. Jump to
[Operating it](#operating-it) for day-to-day commands.

---

## Operating it

```bash
docker compose logs -f poller          # follow the poller (or: web)
docker compose restart poller web      # apply config.yaml / profile.md edits
docker compose up -d --build           # apply new code (after git pull)
docker compose down                    # stop everything (add --profile ollama
                                       # if you started Ollama). Data survives in ./data
```

> `matched: 0, notified: 0` for many cycles is **normal** — with `max_age_days`
> set, a notification fires only when a posting is fresh, survives filters, and
> scores above `score_low`. Most cycles are quiet.

---

## Optional extras

### Mobile "tap alert → tailored résumé" loop

Set both `JOB_AGG_TAILOR_SIGNING_SECRET` (any long random string) and
`JOB_AGG_TAILOR_ENDPOINT_URL` in `.env`, pointing the URL at this box's web app —
`http://<lan-ip>:8000/tailor` on your LAN, or a Tailscale / Cloudflare-Tunnel URL
to reach it off-network. Alerts then carry a signed deep-link that renders a
tailored résumé PDF on demand. (This requires the résumé tailoring artifacts —
see [`resume/README.md`](resume/README.md).)

The PDF renders through your **active template pack** — manage packs and
rendering settings (bullet caps, max pages, page size/margins) on the web UI's
`/builder` page, including uploading your own designs (HTML/Jinja2, zip pack, or
a one-time LLM-converted `.docx`). The tailor page also lets you re-render a
finished run in a different template without re-running the LLM.

#### Reach it from your phone anywhere (Tailscale)

On your LAN the `<lan-ip>` URL works, but the tap-to-tailor loop is most useful
away from home — and a LAN IP isn't reachable then. [Tailscale](https://tailscale.com)
puts your phone and the box on one private [WireGuard](https://www.wireguard.com)
network so the phone can reach the box from anywhere, with nothing exposed to the
public internet. It's free for personal use and needs no port-forwarding, firewall
rules, or TLS certs (the tunnel is already encrypted, so plain `http` over it is
fine).

1. **Install Tailscale on the box** (the machine running Docker) and **on your
   phone**, and sign both into the **same tailnet** (same login). macOS: the
   Tailscale app. Linux box: `curl -fsSL https://tailscale.com/install.sh | sh`
   then `sudo tailscale up`. Phone: the iOS/Android app.

2. **Get the box's tailnet address.** Run `tailscale ip -4` on the box for its
   `100.x.y.z` address, or enable **MagicDNS** in the
   [admin console](https://login.tailscale.com/admin/dns) and use its stable
   `<hostname>.<tailnet>.ts.net` name (preferred — it survives IP changes).

3. **Point the env var at it** in `.env` (note the `:8000` host port and the
   `/tailor` path), alongside the signing secret:
   ```bash
   JOB_AGG_TAILOR_SIGNING_SECRET=<any long random string>
   JOB_AGG_TAILOR_ENDPOINT_URL=http://<hostname>.<tailnet>.ts.net:8000/tailor
   ```

4. **Recreate the containers** so they pick up the new env — Compose reads
   `.env` only at container *creation*, so a plain `restart` won't see the change:
   ```bash
   docker compose up -d            # recreates containers with the new env
   ```

5. **On the phone, make sure Tailscale is toggled on**, then tap the **"Tailor
   resume"** action on an alert. The same address also serves the whole triage UI
   — `http://<hostname>.<tailnet>.ts.net:8000` works from anywhere your phone has
   signal, replacing the LAN-only access from [§A4](#a4-open-the-web-ui).

> **The box must stay awake and online** for the phone to reach it. On a Mac
> mini, prevent sleep (System Settings → Energy, or `caffeinate`); a server/NAS
> is already always-on. If a tap does nothing, confirm Tailscale shows the box as
> *connected* in the phone app and that `JOB_AGG_TAILOR_ENDPOINT_URL` uses the
> tailnet name — not `localhost` or the LAN IP.

### Local dry-run: tuning your profile without deploying

You can run the whole pipeline against live ATS endpoints without writing state
or notifying — ideal for iterating on `profile.md` and filters:

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,web,render]'
pytest                        # ~40 s; PDF tests skip if WeasyPrint's native libs are missing

JOB_AGG_NTFY_TOPIC_URL=https://ntfy.sh/your-topic \
JOB_AGG_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... \
JOB_AGG_OLLAMA_API_KEY=ol-...  \
python -m src.handler --tier ats --dry-run
```

`--dry-run` skips state writes and notifications, logging `would_notify` (with
score + rationale) for each posting that would have been sent. Set the key that
matches `relevance.provider`, or omit it entirely to skip scoring. Add
`--calibrate` to print the score histogram (see
[§2c](#2c-pick-your-llm-provider--and-re-calibrate-after-switching)).

### Rejection audit & ops alerts (optional)

The local runtime records every filter-gate rejection; browse them at
`/audit` (filter by gate, rescue wrongly-rejected postings into the inbox,
or confirm the rejection — those judgments feed future threshold tuning).
Retention defaults to 90 days (`audit.retention_days` in config.yaml).

Pipeline-health push alerts are off until you configure a separate ops
channel in `.env`:

    JOB_AGG_OPS_NTFY_TOPIC_URL=https://ntfy.sh/your-ops-topic
    # and/or JOB_AGG_OPS_DISCORD_WEBHOOK_URL=...
    # optional dead-man's-switch (fires when the whole box dies):
    # JOB_AGG_HEARTBEAT_URL=https://hc-ping.com/<your-uuid>

Conditions: pipeline stopped (web-container watchdog), zero new postings for
12h, LLM degraded for 2 consecutive cycles. Thresholds live under
`ops_notify:` in config.yaml; each alert has a 6h cooldown and sends a
recovery notice when the condition clears. Remember: after editing `.env`,
`docker compose up -d --force-recreate`; after pulling this code change,
`docker compose up -d --build`.

### Threshold tuning from audit verdicts (optional)

As you mark rejected postings **rescued**/**confirmed** in `/audit`, those
verdicts become labels. `scripts/tune_thresholds.py` mines them into a
read-only report suggesting `relevance.score_low` and `filters.titles`
changes:

```bash
uv run python scripts/tune_thresholds.py            # all-time
uv run python scripts/tune_thresholds.py --since 2026-06-01 --min-verdicts 15
```

Run it on the box whose SQLite DB holds the verdicts. It never edits config —
copy the suggested lines into `config.yaml` and `docker compose restart
poller`. With few verdicts it honestly reports "insufficient data"; it can
only recommend *lowering* `score_low` (verdicts exist only on suppressed
jobs), never raising it.

### Board automation (optional)

The board maintains itself daily: a posting-closed sweep (04:30 UTC) badges
cards whose req has verifiably disappeared (two consecutive daily misses;
soft-404s don't count), and a board digest (15:00 UTC) pushes stale cards
and newly-closed postings to the job-alert channel — only when there's
something to report. Tune the crons under `board:` in config.yaml; set
`board.web_base_url` (e.g. a Tailscale URL) to make the digest tap through
to /board.

### Apply kit (optional)

`/kit` renders your recurring application-form answers (links, work
authorization, EEO responses) with one-tap copy buttons — a cheat sheet to
keep beside any application form.

```bash
cp resume/facts.example.yaml resume/facts.yaml   # gitignored — edit freely
```

`resume/` is bind-mounted, so edits show up on the next refresh — no
restart. Groups and labels are free-form: add any question you find
yourself answering repeatedly. Configurable via `kit.facts_path` in
`config.yaml` (default `resume/facts.yaml`).

### Gmail ingestion (optional)

The board can read your job-search email: an hourly read-only IMAP sweep
matches recent mail to active board cards and flags formulaic rejections
("unfortunately…") and application receipts ("thank you for applying") as
suggest-only badges — you confirm or dismiss from `/board`. The mailbox is
never modified and email bodies are never stored.

Setup (requires 2-Step Verification on the Google account):

1. Google Account → Security → 2-Step Verification → App passwords →
   generate one for "Mail".
2. Add to `.env`:

```bash
JOB_AGG_GMAIL_ADDRESS=you@gmail.com
JOB_AGG_GMAIL_APP_PASSWORD=abcdabcdabcdabcd
```

3. `docker compose up -d --force-recreate` (compose snapshots `.env` at
   container creation).

Both vars empty = feature off. Tunables under `gmail:` in `config.yaml`
(`check_cron`, `first_run_days`, `lookback_max_days`,
`max_messages_per_run`). The first run sweeps the last 3 days, so pending
rejections surface immediately; the daily board digest counts unconfirmed
suggestions.

### Avature / headless connector (optional)

Avature-powered career sites (Jacobs, etc.) serve an empty `HTTP 202` to plain
HTTP clients, so they're polled on a dedicated **headless** tier that drives a
real Chromium via Playwright. It's opt-in because it pulls a browser into the
image (the Docker image already bundles it; the steps below are only for a local
venv run):

```bash
pip install -e '.[headless]'
playwright install chromium
```

Add each board by its `SearchJobs` URL under `sources.avature` — branded domains
aren't machine-discoverable, so these are hand-curated `{careers_url, company}`
entries:

```yaml
sources:
  avature:
  - {careers_url: "https://careers.jacobs.com/en_US/careers/SearchJobs", company: "Jacobs"}
```

**Verify each site passes headless first** — some Avature tenants sit behind a
WAF that blocks even a real browser. `docker compose restart poller`, then watch
the `headless` cycle logs for that company's job count. The tier runs every
`schedules.headless_minutes` (default 45).

### Automated board discovery (optional)

Beyond validating startup slugs, the daily `discovery` tier **fingerprints a
seed list of ~490 enterprise careers pages** (`scripts/seeds/`), and any board it
can classify **and** live-verify (Workday, Oracle Cloud, Eightfold, Taleo,
iCIMS) joins the `ats` poll set automatically — no `config.yaml` write, no review
gate. Dead or false-positive boards get suppressed by poll-health, exactly like
auto-discovered slugs.

It's **on by default**; the budget rotates through the seed list over several
days (60 fingerprints per run). Tune under `discovery:` in `config.yaml`
(defaults shown — you don't need to add these unless you want to change them):

```yaml
discovery:
  board_discovery_enabled: true
  board_max_sweeps_per_run: 60      # fingerprints attempted per daily run
  board_revalidate_after_days: 14   # recheck a matched/dead board this often
  board_quarantine_after_failures: 5
```

Watch the `board_discovery_done` log line for its `{swept, matched, ...}` counts.
The manual `scripts/discover_enterprise.py` CLI still exists for review-first
onboarding; hand-configured entries win over auto-discovered ones.

### Aggregator candidate mining (optional)

> **Hiring.cafe is disabled and stays that way.** hiring.cafe now robots-disallows
> the search endpoint the connector used and challenges every page it touched.
> The connector is kept in the tree, off by default, and the project does not
> work around the block (see the status note in `src/hiringcafe.py`). Mining
> therefore runs on **Adzuna** sightings today.

If an aggregator source is enabled (`sources.adzuna.enabled: true`),
every slow cycle already surfaces postings from companies you *don't* poll
directly — and the pipeline mines them. Each foreign posting's apply URL is
classified at zero HTTP cost (the same URL classifiers the fingerprint sweep
uses) and staged as a **candidate**; the daily `discovery` tier then validates
candidates first, within its normal probe budget — startup slugs get a 1-request
probe of their claimed ATS, enterprise boards (Workday, Oracle Cloud, Eightfold,
Taleo, iCIMS) get a live verify — and healthy ones join `ats` polling
automatically. Once a mined board is polled directly, the aggregator stops
re-emitting its postings, so you never get duplicate alerts for the same role.

It's **on by default** but does nothing unless an aggregator source is enabled.
Tune under `discovery:` (defaults shown):

```yaml
discovery:
  hiringcafe_mining_enabled: true
  candidate_capture_cap: 50   # max new candidates staged per slow cycle
  revalidate_reserve: 100     # probes reserved per discovery run for revalidating known-good slugs
```

`revalidate_reserve` also fixes a long-standing quirk: revalidation of
already-discovered slugs can no longer be starved by a busy discovery run. The
reserve is carved out of `max_validations_per_run`, so keep that comfortably
above the reserve (`config.example.yaml` leaves the 200 default; raise it if you
enable mining). Watch
`hiringcafe_sightings_captured` on slow cycles and
`discovery_candidates_drained` on the daily run.

**Targeting European sources?** One opt-in lever today (default off):
`discovery.eu_seeds_enabled: true` adds a curated EU enterprise seed list to
the board-discovery sweep. (`sources.hiringcafe.extra_queries` and
`sources.hiringcafe.location` still parse but are inert while that connector is
disabled.) It works independently of the location gate — with
`allowed_countries: [US]` it surfaces EU companies' US-remote roles. See
`docs/examples/eu-config.md` for the full EU setup.

### VC portfolio auto-discovery (optional)

The VC portfolio importer also runs hands-off: list firms under
`discovery.vc_firms` and the daily `discovery` tier re-fetches each firm's
portfolio weekly, staging every in-profile company as a **candidate** — zero
probes at fetch time. Candidates are then validated by the conversion chain
on the normal probe budget over the following days (a ~500-company portfolio
absorbs over ~2 weeks instead of blowing one day's budget). Companies you
already poll, already-staged candidates, converted boards, and recent misses
are deduped at capture. The absorption pace scales with
`max_validations_per_run` — on the 200 default that `config.example.yaml`
leaves in place, two large portfolios can monopolize the candidate queue for weeks, so
raise the budget or stagger `vc_firms` if you enable more than one firm.

It ships **inert** (`vc_firms: []`). Enable it per firm:

```yaml
discovery:
  vc_firms: ["a16z"]              # the only working driver today; "sequoia" parses but 404s since their site moved
  vc_refresh_days: 7              # portfolio re-fetch cadence
  vc_capture_cap: 500             # max new candidates staged per firm per run
```

A failed **or empty** fetch is logged (`vc_fetch_failed`) and retried the
next daily run — the `vc:{firm}` watermark only advances on success. Watch
`vc_discovery_done` for `{firm, fetched, staged, deduped}` counts, then
`discovery_candidates_drained` (`name_ok` / `name_board_ok` /
`name_no_match`) over the following runs. The manual CLI
(`scripts/import_vc_portfolio.py`, including its CSV driver) still works for
ad-hoc, review-first imports.

### Apply bookmarklet (optional)

The `/kit` page renders an **"📋 Apply Autofill"** bookmarklet built from your
`resume/facts.yaml`. Drag it to your browser's bookmarks bar once; then, on a
Greenhouse / Lever / Ashby application form, click it to autofill the standard
fields (name, email, links, work authorization) from your facts. It's a static
`javascript:` bookmarklet — no extension and no network calls. Re-drag it after
editing `facts.yaml` to pick up new answers.
