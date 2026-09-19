# Getting Started

This guide takes you from nothing installed to receiving job alerts that are
tuned to *your* preferences — no clone required. For what the app is and how
it works internally, see the [README](README.md). When something breaks, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Everything persists in `./data`; $0 infra; LLM cost depends on provider.

---

## Contents

1. [Start it](#1-start-it) — one file, one command, no clone
2. [Configure it](#2-configure-it) — filters, your profile, LLM provider, companies, notifications (full flag reference: [docs/CONFIG.md](docs/CONFIG.md))
3. [Run it with Docker Compose](#run-it-with-docker-compose) — upgrading, pinning, the headless image, local Ollama, running from a clone
4. [Operating it](#operating-it)
5. [Optional extras](#optional-extras) — mobile tailored-résumé loop, rejection audit & ops alerts, board automation, headless connector, auto board discovery, aggregator candidate mining, apply kit

---

## 1. Start it

You need Docker with Compose **v2.24 or newer** (`docker compose version`) —
Docker Desktop on macOS/Windows, or Docker Engine + the compose plugin on
Linux. Nothing else: no clone, no Python, no files to write. Everything else
— Python, SQLite, WeasyPrint — lives inside the image.

```bash
mkdir job-aggregator && cd job-aggregator
curl -O https://raw.githubusercontent.com/seancampbell3161/job-aggregator/main/docker-compose.yml
docker compose up -d
```

Open <http://localhost:8000>. The first visitor sets the admin password, so do
this now rather than later — on a shared network, whoever gets there first
claims it. To set it before the port is even reachable:

```bash
docker compose run --rm -it web python -m src.settings set-password
```

> [!WARNING]
> **Keep it off the public internet.** The UI is served over plain HTTP, so
> the password and everything it shows — your résumé, your applications, your
> apply-kit answers — cross the network unencrypted. That's fine on your home
> network, not fine on shared or public Wi-Fi. To reach it away from home use
> [Tailscale](#reach-it-from-your-phone-anywhere-tailscale) (already
> encrypted) rather than port-forwarding. To restrict it to this machine
> only, publish `127.0.0.1:8000:8000` in `docker-compose.yml` instead of
> `8000:8000`. Behind an HTTPS reverse proxy, set `FORWARDED_ALLOW_IPS` (see
> `.env.example`).

Each browser stays signed in for 30 days of use; sign in again from a new
device or after that.

Everything persists in **`./data`**, next to the compose file — settings,
secrets, jobs, generated résumés, template packs — created on first boot.
Back it up by copying that folder, or from **Settings → Backup** once you're
in ([§2](#2-configure-it)).

---

## 2. Configure it

After the password, **/setup** offers three ways in: **Start from defaults**
— a blank slate; **Settings → Overview** then lists what's still missing
before alerts can arrive — restoring a backup `.zip` from an existing
instance, or importing a `config.yaml` (plus `profile.md`/`resume.md`) the
same way a clone would; the page itself shows the exact command for each.

From there, everything is in the web UI under **Settings** — filters, your
relevance profile, LLM provider and key, companies, notification sinks,
schedules. There is no `config.yaml` to author before you start; settings
live in the app database and are versioned, so **Settings → History** shows
every change and restores any of them. `config.yaml` import still works, for
bulk edits or scripting — the full import/export/versioning mechanics, and
every flag's default, are in [docs/CONFIG.md](docs/CONFIG.md). The rest of
this section is the narrative version: what each setting means and why.

Worth doing before the first full cycle:

- **Settings → Filters** (2a, below) — titles, seniority, locations,
  employment types.
- **Settings → Profile** (2b) — the single biggest lever on match quality.
- **Settings → Notifications** — an ntfy topic and/or a Discord webhook.
  Without a sink the app still scores and stores matches; it just cannot tell
  you.
  - **ntfy**: pick a **hard-to-guess** topic string (anyone who knows it can
    post to it), e.g. `https://ntfy.sh/<your-random-topic>`, and install the
    ntfy app (iOS/Android) with that topic added.
  - **Discord**: in the channel you want alerts in, **Edit Channel →
    Integrations → Webhooks → New Webhook → Copy Webhook URL**.

Optional but recommended: an LLM key (2c, below) scores each posting (0–10)
so only good matches notify. Without one the pipeline still runs —
everything that passes your filters notifies **unscored**.

| Provider | Get a key | Cost |
|---|---|---|
| **Anthropic** (Claude Haiku) | [console.anthropic.com](https://console.anthropic.com/) | Per-token; a few $/day while clearing a backlog, pennies/day at steady state |
| **Google Gemini** | [aistudio.google.com](https://aistudio.google.com) (no card) | Free tier (~1,500 req/day) |
| **Ollama Cloud** | [ollama.com/settings/keys](https://ollama.com/settings/keys) | Flat monthly subscription (GPU-time, not per token) |
| **Ollama, fully local** | none — runs on your box | $0 (needs `--profile ollama`, [§3](#run-it-with-docker-compose)) |

Companies are 2d, résumé gap analysis is 2f — both below.

Restoring from an existing install instead? **Settings → Backup** takes the
ZIP that **Settings → Backup** on the old instance produced.

### 2a. Hard filters (Settings → Filters)

A posting must pass **every** filter (or be `UNKNOWN` on filters that allow it)
before it ever reaches the LLM scorer. Set these in **Settings → Filters**; a
`config.yaml` with the same shape imports too, for bulk edits:

```yaml
filters:
  titles:            # exact phrase, case-insensitive, on word boundaries
  - software engineer  # (no regex syntax)
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
> surfaces its *future* postings. While testing, clear it (`null`) in
> **Settings → Filters** to see the existing backlog, then put it back.

### 2b. Your relevance profile (Settings → Profile)

This is the single biggest lever on match quality. The LLM grades each
surviving posting against it on a 0–10 scale, then compares to two
thresholds:

```yaml
relevance:
  enabled: true
  provider: anthropic            # or: ollama, gemini
  model: claude-haiku-4-5        # ollama: gpt-oss:120b · gemini: gemini-2.0-flash
  score_high: 7                  # >= this gets a "strong fit" marker in the notification
  score_low: 4                   # <= this is suppressed entirely (no notification)
  timeout_seconds: 10            # per-call timeout; on exceed the posting fails open (unscored)
```

Write it as if you're briefing a recruiter. Be explicit about:

- **Strong fit (8–10)** — what makes you say "I'd love to interview here."
- **Mild fit (5–7)** — interesting but not exciting; the LLM should land these mid-scale.
- **Weak fit / not interested (1–3)** — hard dealbreakers (geography, title, comp, industry).

`profile.example.md` in the repo is a worked example you can paste into
**Settings → Profile** as a starting point; the *shape* matters more than the
exact text. The LLM reliably picks up plainly-stated signals like "IC only,
no management track" or "US-based only, no visa transfer."

Targeting a non-US market? Set `location.allowed_countries` in **Settings →
Filters** accordingly and update the geography rule in **Settings → Profile**
to match — the LLM profile isn't derived from your filters and will keep
down-scoring what the gate now passes. See `docs/examples/eu-config.md` for a
full worked example.

You can iterate on your profile without deploying — run a
[local dry-run](#local-dry-run-tuning-your-profile-without-deploying) and watch
the `would_notify` log lines (each includes the score and rationale).

### 2c. Pick your LLM provider — and re-calibrate after switching

Switch providers in **Settings → LLM** (or with one `config.yaml` line,
imported) plus a matching key — see [§2](#2-configure-it) above for where to
get one. All three are fail-open: if the LLM errors or times out, the posting
goes through **unscored** rather than being dropped.

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

### 2d. Add or remove companies (Settings → Companies)

The sixteen company-board families (Greenhouse, Lever, Workday, Oracle Cloud,
and the rest) are managed at **Settings → Companies** — paste a company's
careers URL (or a bare domain) and it fingerprints the ATS, shows what it
found, and adds it on confirmation. The `add-source` CLI and a `config.yaml`
import still work too, for scripting or bulk edits; the "slug" they need is
the path component on the company's careers URL:

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
docker compose run --rm web python -m src.settings add-source greenhouse stripe
# Structured boards (Workday, Oracle Cloud, …) are easier pasted into Settings
# → Companies, or go in a config.yaml you import:
#   sources:
#     workday:
#     - tenant: microsoft
#       region: wd1
#       site: External
```

`add-source`, discovery, and the scripts below all save straight to the
database. If you also keep a `config.yaml` for bulk edits, see
[docs/CONFIG.md](docs/CONFIG.md) for how imports and database-only changes
stay in sync — an import from a stale file refuses rather than dropping
changes it doesn't know about, naming each one; `import --force` overwrites
them on purpose.

`scripts/discover_enterprise.py` auto-detects **iCIMS** boards (scraped via
their static in_iframe listings + each job's schema.org JSON-LD) and merges
them into `sources.jsonld_boards` — review the dry-run report, then `--merge`
— matched boards are saved into your settings and apply live.

The discovery scripts and `scripts/seed_companies.py` run on the host —
`uv run python scripts/…` — against `./data/job_aggregator.db`, the same
database the containers use. SQLite's file locks don't reach across Docker
Desktop's bind mount between the host and the containers (verified on macOS),
so a host-side write while the poller or web container writes can corrupt the
database. Stop the containers around every `--merge` or `seed_companies.py`
run:

```bash
docker compose stop poller web
uv run python scripts/discover_enterprise.py --merge
docker compose start poller web
```

**SuccessFactors and TalentBrew** boards use the same connector but live on
branded careers domains that aren't machine-derivable. Pasting the URL into
**Settings → Companies** identifies them; if it can't save the entry
directly, finish it under `sources.jsonld_boards` via **Settings →
Advanced** or a `config.yaml` import:

```yaml
sources:
  jsonld_boards:
  - {family: successfactors, slug: aosmith, base_url: "https://jobs.aosmith.com", company: "A. O. Smith"}
  - {family: talentbrew, slug: chevron, base_url: "https://jobs.chevron.com", company: "Chevron"}
```

`scripts/discover_enterprise.py` also auto-detects **Eightfold.ai** boards
(native JSON API — pcsx/apply_v2 flavors probed automatically, the required
`domain=` param scraped from each careers page). Matched tenants merge into
`sources.eightfold`; review the dry-run report, then `--merge` — or paste a
board's URL directly into **Settings → Companies**.

`scripts/discover_enterprise.py` also auto-detects **modern Oracle Taleo**
career sections (the `searchjobs` REST API). It probes and verifies each
`{tenant}.taleo.net/careersection/{section}` — legacy-template and
migrated-off tenants fail the verify and stay `unsupported`, so only working
modern boards merge into `sources.taleo`. Multi-section tenants surface their
primary section; add the rest the same way.

**Phenom People** career sites (e.g. FIS, GE HealthCare, RTX) are polled via
their static sitemap + per-job schema.org JSON-LD. Their branded careers
domains aren't machine-derivable, so add each in **Settings → Companies** by
pasting the careers URL, or under `sources.phenom`:

```yaml
sources:
  phenom:
  - {careers_url: "https://careers.fisglobal.com", company: "FIS"}
  - {careers_url: "https://jobs.gehealthcare.com", company: "GE HealthCare"}
```

The connector fetches per-job detail only for jobs modified since the last
cycle (a lastmod watermark), so steady-state polling is cheap; the first
cycle sweeps jobs modified within the last 3 days.

**Oracle Recruiting Cloud** boards (`sources.oraclecloud`) take a `{tenant,
region, site}` triple (e.g. `{tenant: egug, ...}` for American Express).
**Avature** boards are JS-gated and polled on the headless tier — see
[Avature / headless connector](#avature--headless-connector-optional) below.

Remove a company from **Settings → Companies**, or by deleting its slug from
`config.yaml`. Already-seen rows are harmless and self-expire. The daily `discovery` tier keeps adding healthy yc-oss slugs on its
own — and, hands-off, fingerprints the enterprise seed list and starts polling
whatever it can classify (see
[Automated board discovery](#automated-board-discovery-optional)). To bulk-add a
VC's portfolio (a16z is wired up):
`uv run python scripts/import_vc_portfolio.py a16z --merge` (with the containers
stopped, as above) — or skip the CLI and let the daily tier handle it: see
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

Aggregator toggles (`hn_who_is_hiring`, `remotive`, `remoteok`, `adzuna`, …)
live under **Settings → Advanced**; quiet hours are under **Settings →
Notifications**, cadence under **Settings → Schedules**. All three also
accept a `config.yaml` import — see [docs/CONFIG.md](docs/CONFIG.md) for the
full shape.

### 2f. Résumé gap flags (optional, off by default)

When `gap_analysis.enabled` is on, every posting that survives filtering *and*
scoring gets a second LLM pass listing the hard skills the role wants but your
résumé doesn't show — surfaced as a Discord "Stretch areas" field and rolled into
a weekly digest. The résumé only annotates; it never affects the relevance score,
so recall is unchanged.

Paste your résumé into **Settings → Documents**, then flip
`gap_analysis.enabled` on in **Settings → Advanced** (or set it in a
`config.yaml` you import — the same directory's `resume.md` is picked up
too):

```bash
cp resume.md.example resume.md        # then fill in your real experience
```

`resume.md` is gitignored PII. No new key is needed — it reuses your
`relevance.provider`.

---

## Run it with Docker Compose

The whole pipeline runs 24/7 on one machine with SQLite state, as three
services: **poller** (scrape → filter → score → notify on the `schedules.*`
cadence, plus a daily prune), **web** (triage inbox, board, analytics, ops,
and `/tailor`), and **ollama** (opt-in local LLM, only started with
`--profile ollama`). [§1](#1-start-it) already got you running; this section
covers upgrading, pinning, the headless image, and running from a clone.

**Upgrading** to a new image:

```bash
docker compose pull && docker compose up -d
```

**Pinning a version.** The downloaded `docker-compose.yml` tracks `:latest`.
To stay on a known-good release, edit the `image:` line under both `poller`
and `web`:

```
image: ghcr.io/seancampbell3161/job-aggregator:0.12.0
```

**The headless image.** Only **Avature** boards (`sources.avature`) need a
real browser — they serve an empty response to plain HTTP clients. Phenom
boards look similar but are fetched over plain HTTP and never need this. If
you add an Avature board, switch both services to the `-headless` tag
(about 1.7 GB larger):

```
image: ghcr.io/seancampbell3161/job-aggregator:latest-headless
```

(`:0.12.0-headless` / `:0.12-headless` pin it, matching the slim tags above.)

**Fully-local Ollama** — no cloud key, no per-token cost: start the bundled
service and pull a model once, then pick it in **Settings → LLM**:

```bash
docker compose --profile ollama up -d
docker compose exec ollama ollama pull llama3.1:8b
```

`gpt-oss:120b`, the model the hosted-Ollama-Cloud recipes use, is far too big
for a typical Mac mini — pick something sized to your RAM instead, like
`llama3.1:8b` or `qwen2.5:7b`, and re-check `score_low` once you switch (see
`docs/runbooks/calibrating-relevance-scores.md`).

**Working on the code instead of just running it?** Clone the repository —
it carries a tracked `docker-compose.override.yml` that Compose loads
automatically and that builds your working tree instead of pulling the
published image, so the command is the same one either way:

```bash
git clone https://github.com/seancampbell3161/job-aggregator.git
cd job-aggregator
docker compose up -d --build
```

After a `git pull`, `docker compose up -d --build` again picks up the
change. Need the headless tier locally? Uncomment `target: headless` under
both `build:` blocks in `docker-compose.override.yml` and rebuild.

---

## Operating it

> **When changes take effect.** Settings changed in the UI apply on the next
> cycle — no restart. Only a new image needs anything:
> `docker compose pull && docker compose up -d` (`--build` instead, after a
> `git pull`, in a clone).

```bash
docker compose logs -f poller          # follow the poller (or: web)
docker compose run --rm -v "$PWD:/import:ro" web python -m src.settings import /import   # bulk-edit via config.yaml (live)
docker compose run --rm web python -m src.settings status       # setup state + secret origins
docker compose run --rm -it web python -m src.settings set-password   # new web UI password; signs every device out
docker compose run --rm web python -m src.settings sign-out-everywhere # lost a device: end every session
docker compose pull && docker compose up -d   # new image (clone: --build, after git pull)
docker compose down                    # stop everything (add --profile ollama
                                       # if you started Ollama). Data survives in ./data
```

> `matched: 0, notified: 0` for many cycles is **normal** — with `max_age_days`
> set, a notification fires only when a posting is fresh, survives filters, and
> scores above `score_low`. Most cycles are quiet.

### Backing up

**`/settings/backup`** downloads everything — settings, profile, résumé
documents, and any template packs you've uploaded — as one `.zip`. It never
includes secrets (API keys, webhook URLs, the tailoring signing secret); after
restoring onto a new box, set those again by hand or with `import-env-secrets`.
The same page, and **`/setup`** on a fresh install with no settings yet, accept
that file back to restore.

Made a change you want to undo? **`/settings/history`** lists every saved
settings version with a diff against what's currently live, and restores any
of them — no file needed.

---

## Optional extras

### Mobile "tap alert → tailored résumé" loop

Set `tailor_endpoint_url` (`set-secret tailor_endpoint_url`, or
`JOB_AGG_TAILOR_ENDPOINT_URL` in `.env`), pointing the URL at this box's web app —
`http://<lan-ip>:8000/tailor` on your LAN, or a Tailscale / Cloudflare-Tunnel URL
to reach it off-network. The signing secret is generated automatically on first
boot. Alerts then carry a signed deep-link that renders a tailored résumé PDF on
demand. (This requires the résumé tailoring artifacts — see
[`resume/README.md`](resume/README.md).)

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

3. **Point the endpoint URL at it** (note the `:8000` host port and the
   `/tailor` path). No-recreate route — applies live:
   ```bash
   docker compose run --rm -it web python -m src.settings set-secret tailor_endpoint_url
   # http://<hostname>.<tailnet>.ts.net:8000/tailor
   ```
   or set `JOB_AGG_TAILOR_ENDPOINT_URL` in `.env` (step 4 applies to this route
   only). The signing secret is generated automatically on first boot — nothing
   to set.

4. **If you used `.env`, recreate the containers** so they pick up the new env —
   Compose reads `.env` only at container *creation*, so a plain `restart` won't
   see the change:
   ```bash
   docker compose up -d            # recreates containers with the new env
   ```

5. **On the phone, make sure Tailscale is toggled on**, then tap the **"Tailor
   resume"** action on an alert. The same address also serves the whole triage UI
   — `http://<hostname>.<tailnet>.ts.net:8000` works from anywhere your phone has
   signal, replacing the LAN-only access from [§1](#1-start-it).
   The tailor link and its PDF download need no sign-in — the signed link is
   the key. Every other page asks you to sign in once on each device.

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

python -m src.settings import .              # loads config.yaml + profile.md into ./data/job_aggregator.db
JOB_AGG_OLLAMA_API_KEY=ol-... JOB_AGG_OLLAMA_HOST=https://ollama.com python -m src.handler --tier ats --dry-run
```

`relevance.ollama_host` defaults to `http://ollama:11434`, which only resolves
inside Docker Compose. For hosted Ollama Cloud, set
`relevance.ollama_host: https://ollama.com` in `config.yaml` and import, or export
`JOB_AGG_OLLAMA_HOST=https://ollama.com` for the command (as above).

No ntfy/Discord secrets are needed for a dry-run. `--dry-run` skips state writes
and notifications, logging `would_notify` (with score + rationale) for each
posting that would have been sent. Set the key that matches
`relevance.provider`, or omit it entirely to skip scoring. Add `--calibrate` to
print the score histogram (see
[§2c](#2c-pick-your-llm-provider--and-re-calibrate-after-switching)).

### Rejection audit & ops alerts (optional)

The local runtime records every filter-gate rejection; browse them at
`/audit` (filter by gate, rescue wrongly-rejected postings into the inbox,
or confirm the rejection — those judgments feed future threshold tuning).
Retention defaults to 90 days (`audit.retention_days`).

Pipeline-health push alerts are off until you configure a separate ops
channel — `set-secret ops_ntfy_topic_url` / `set-secret ops_discord_webhook_url`
(applies live), or in `.env`:

    JOB_AGG_OPS_NTFY_TOPIC_URL=https://ntfy.sh/your-ops-topic
    # and/or JOB_AGG_OPS_DISCORD_WEBHOOK_URL=...
    # optional dead-man's-switch (fires when the whole box dies):
    # JOB_AGG_HEARTBEAT_URL=https://hc-ping.com/<your-uuid>

Conditions: pipeline stopped (web-container watchdog), zero new postings for
12h, LLM degraded for 2 consecutive cycles, and settings fallback (the newest
settings version is invalid). Thresholds live under `ops_notify:`; each alert
has a 6h cooldown and sends a recovery notice when the condition clears.
Settings changes apply live; anything else — a `.env` edit or a new image —
needs `docker compose up -d` (`--build` after pulling this code change, in a
clone).

### Threshold tuning from audit verdicts (optional)

As you mark rejected postings **rescued**/**confirmed** in `/audit`, those
verdicts become labels. `scripts/tune_thresholds.py` mines them into a
read-only report suggesting `relevance.score_low` and `filters.titles`
changes:

```bash
uv run python scripts/tune_thresholds.py            # all-time
uv run python scripts/tune_thresholds.py --since 2026-06-01 --min-verdicts 15
```

Run it on the box whose SQLite DB holds the verdicts. It never edits settings
— copy the suggested values into **Settings → Filters** / **Settings → LLM**
directly, or `export` to a directory, edit `config.yaml`, and re-import
(applies live either way). With few verdicts it honestly reports
"insufficient data"; it can only recommend *lowering* `score_low` (verdicts
exist only on suppressed jobs), never raising it.

### Board automation (optional)

The board maintains itself daily: a posting-closed sweep (04:30 UTC) badges
cards whose req has verifiably disappeared (two consecutive daily misses;
soft-404s don't count), and a board digest (15:00 UTC) pushes stale cards
and newly-closed postings to the job-alert channel — only when there's
something to report. Tune the crons under `board:`; set
`board.web_base_url` (e.g. a Tailscale URL) to make the digest tap through
to /board.

### Apply kit (optional)

`/kit` renders your recurring application-form answers (links, work
authorization, EEO responses) with one-tap copy buttons — a cheat sheet to
keep beside any application form.

`resume/facts.example.yaml` ships as a template — copy it to
`resume/facts.yaml`, fill it in, and re-import — `/kit` shows it on the next
refresh. Groups and labels are free-form: add any question you find yourself
answering repeatedly.

### Gmail ingestion (optional)

The board can read your job-search email: an hourly read-only IMAP sweep
matches recent mail to active board cards and flags formulaic rejections
("unfortunately…") and application receipts ("thank you for applying") as
suggest-only badges — you confirm or dismiss from `/board`. The mailbox is
never modified and email bodies are never stored.

Setup (requires 2-Step Verification on the Google account):

1. Google Account → Security → 2-Step Verification → App passwords →
   generate one for "Mail".
2. Store the credentials — `set-secret` (applies live, no recreate):

```bash
docker compose run --rm -it web python -m src.settings set-secret gmail_address
docker compose run --rm -it web python -m src.settings set-secret gmail_app_password
```

   or add to `.env`:

```bash
JOB_AGG_GMAIL_ADDRESS=you@gmail.com
JOB_AGG_GMAIL_APP_PASSWORD=abcdabcdabcdabcd
```

3. If you used `.env`: `docker compose up -d` (Compose re-reads `.env` at
   container recreation).

Both empty = feature off. Tunables under `gmail:` (`check_cron`,
`first_run_days`, `lookback_max_days`, `max_messages_per_run`). The first run
sweeps the last 3 days, so pending rejections surface immediately; the daily
board digest counts unconfirmed suggestions.

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

Add each board in **Settings → Companies** by pasting its `SearchJobs` URL —
branded domains aren't machine-discoverable, so this is always a hand-curated
`{careers_url, company}` entry, never an auto-detected slug. A `config.yaml`
with the same shape imports too, for bulk edits:

```yaml
sources:
  avature:
  - {careers_url: "https://careers.jacobs.com/en_US/careers/SearchJobs", company: "Jacobs"}
```

**Verify each site passes headless first** — some Avature tenants sit behind a
WAF that blocks even a real browser. After adding it, watch the `headless`
cycle logs for that company's job count. The tier runs every
`schedules.headless_minutes` (default 45).

### Automated board discovery (optional)

Beyond validating startup slugs, the daily `discovery` tier **fingerprints a
seed list of ~490 enterprise careers pages** (`scripts/seeds/`), and any board it
can classify **and** live-verify (Workday, Oracle Cloud, Eightfold, Taleo,
iCIMS) joins the `ats` poll set automatically — no settings change, no review
gate. Dead or false-positive boards get suppressed by poll-health, exactly like
auto-discovered slugs.

It's **on by default**; the budget rotates through the seed list over several
days (60 fingerprints per run). Tune under `discovery:`
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
re-importing your facts to pick up new answers.
