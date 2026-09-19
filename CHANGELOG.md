# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.13.0] - 2026-09-19

The guided release. v0.12.0 made every setting editable in a browser; it did not
tell you what to type. A new install now walks you through six skippable steps,
reads your résumé, and drafts the two things nobody can write cold — the profile
the scorer grades every posting against, and the hard filters underneath it.
Résumé tailoring, which has worked for months behind three gates nobody could
open, is reachable too: it runs on whichever LLM you already configured, needs no
Jira export, and builds its structured résumé from the file you uploaded.

### Added

- **Guided first-run setup** (#12). `/setup` leads with "Start guided setup" —
  six skippable steps (connect an LLM, upload your résumé, review the drafted
  profile and filters, choose where to look, choose how to reach you, preview
  real matches), each committing as you go. Restore and import stay as expert
  escapes. Progress is derived from the same readiness checks the Overview page
  reads, so the two can never disagree.
- **A résumé drafts your profile and filters** (#12). Upload a PDF or DOCX, or
  paste the text; answer the six things a résumé cannot say (target titles,
  seniority, IC or management, location and remote policy, employment types,
  comp floor); get a drafted `profile.md` and filter set to edit and approve.
  Nothing is written until you approve it, and drafting failures fall back to
  the ordinary forms rather than a silently empty profile.
- **A preview of what would match** (#12), as the last wizard step: a bounded,
  one-off poll of real postings that delivers nothing, marks nothing as seen,
  and advances no connector cursors. An empty preview is a real answer — your
  filters are too narrow — not an error.
- **Résumé tailoring on any LLM provider** (#13). Tailoring and `.docx`
  template import previously refused every provider but Ollama, silently
  reporting themselves unavailable. Both now run on whatever
  `tailoring.provider` names, falling back to your relevance settings.
- **A drafted `content.json`, with a review gate** (#13). At
  `/settings/documents/draft` — linked from the documents page and the end of
  the wizard — your stored résumé becomes the structured résumé tailoring
  reads. Every bullet is shown as editable text before anything is saved,
  because the tailorer validates rewrites *against* this document and so cannot
  catch anything invented *into* it. Entry and bullet ids are assigned
  server-side; a draft that reads as empty fails loudly instead of looking
  successful.
- **An apply-kit scaffold** (#13). Saving a drafted résumé also seeds the
  `kit_facts` document behind `/kit`, but only when you have none: links from
  your résumé's contact details, location and desired salary from your existing
  filters, the remaining eligibility answers left blank for you, and **every
  EEO field blank by construction** — no code path can populate those from
  model output.
- **A shared LLM seam** (`src/llm/`, #12, extended in #13) replacing five
  hand-rolled provider factories that each re-derived the same provider/model
  fallback, the same provider-to-secret map, and the same "only a local Ollama
  needs no key" rule. Adding a provider is now one place, not six.

### Changed

- **An evidence bank is no longer required** (#13). `evidence.json` stays
  supported and stays the citation layer when present. With none, the tailoring
  prompt drops its per-metric citation requirement in favour of a stricter rule
  — never introduce a number that is not already in the bullet being rewritten.
  Without that swap an empty bank made the old rule unsatisfiable, and a
  compliant model would have stripped every metric from every bullet.
- **A failed tailoring run names its cause** (#13) instead of advising a retry.
  A model whose output-token ceiling rejects the request now says so, rather
  than sending you round a loop that cannot succeed.
- **`--dry-run` polls now leave no trace** (#12). They already skipped the seen
  store, health and suppression writes, but still persisted connector ETag and
  cursor hints — so a dry run could advance them, and the next real cycle would
  fetch nothing and never alert those postings. `--dry-run` and `--calibrate`
  also report `would_notify` in their JSON now.
- **Résumé drafting is deterministic** (#13). The profile drafter shares the LLM
  seam the ported tailoring and import call sites use, so it runs at
  `temperature: 0` with `think=False`.

### Upgrading

**A source install needs new extras; Docker needs a rebuild.** The `web` extra
gained `pypdf` and `segno` (résumé PDF parsing and the notification QR code):

```bash
uv sync --extra web --extra render
docker compose build     # NOT --force-recreate: src/ is baked into the image
```

**Nothing else is required.** The new `wizard_ui` table is created on boot, and
the one new configuration section — `resume_draft` (`provider`, `model`,
`timeout_seconds`) — is entirely optional: provider and model fall back to your
`relevance` values when unset, and the timeout defaults to 60 seconds. Existing
`config.yaml` files need no edits.

**If you script `--dry-run`, read the behaviour change above.** A dry run no
longer advances connector cursors. If you relied on that — deliberately or not —
your first real cycle after upgrading may fetch a larger batch than usual.

**If you set a non-Ollama `tailoring.provider`, tailoring is live now.** It was
silently unavailable before; there is nothing to change, but it will start
making LLM calls it previously skipped. The same applies to `.docx` template
import.

**Tailoring no longer needs `evidence.json`.** If you have one, nothing changes.
If you do not, tailoring now works — and you can produce the `content.json` it
needs from `/settings/documents/draft` instead of writing it by hand.

## [0.12.0] - 2026-09-19

The installable release. Getting from a fresh checkout to useful alerts used to
mean hand-authoring up to seven files before the web UI was even reachable, and
every install built its own 2 GB image. Settings, documents and secrets now live
in the database behind a login and a full settings UI, and the app is published
as an image: a newcomer downloads one compose file, runs `docker compose up -d`,
and configures everything in the browser. Nothing is authored by hand, and
nothing is cloned.

### Added

- **BREAKING:** Settings foundation — settings, documents, and secrets in
  SQLite (#5). YAML becomes import/export only; the app boots with zero files
  present, in an explicit "not set up" state.
- Require a login for the web UI (#7). First visitor sets the admin password;
  secrets are write-only in the UI and a non-empty `JOB_AGG_*` env var still wins.
- Edit every setting, secret and document from the browser (#8), including an
  Advanced section generated from the config model with per-flag help.
- Manage company boards, settings history and backups from the browser (#9) —
  add a board by pasting a careers URL, diff and restore any past version, and
  move an instance with a downloadable ZIP.
- Publish images to GHCR and install without cloning (#10). Multi-arch
  (amd64/arm64) images on every release tag, in two variants: the default slim
  image (~494 MB) and `-headless` (~2.17 GB), which adds Chromium for Avature
  boards. The container now runs unprivileged.
- `GET /healthz`, a liveness endpoint that answers in every setup state.

### Changed

- The app is distributed as an image. `docker-compose.yml` pulls
  `ghcr.io/seancampbell3161/job-aggregator:latest`; a clone additionally gets
  `docker-compose.override.yml`, which Compose auto-loads, so `docker compose
  up -d --build` still builds your working tree.
- **Two apply rules instead of three.** Settings changed in the UI take effect
  on the next cycle with no restart; only a new image needs anything
  (`docker compose pull && docker compose up -d`). `--force-recreate` is no
  longer needed for `.env` edits — Compose hashes an `env_file`'s content into
  the service config.
- A missing browser is now legible: a slim image with an Avature board
  configured logs `headless_unavailable` and skips that tier, instead of
  raising an uncaught `ImportError` every 45 minutes. The settings UI warns
  before you add such a board.
- Cut releases through a PR, not a push to main.

### Fixed

- Refuse an import only when the files would undo newer settings (#6).
- Treat HTTP 400 as blocked, not transient.
- The image no longer copies `resume/`, which had no runtime purpose and could
  bake a developer's own `resume/content.json` and `resume/evidence.json` into
  a locally built image.

### Removed

- **BREAKING:** Remove the AWS deployment path (#4). One runtime, one config
  source, one secrets store.

### Upgrading

**Existing installs must run one import.** Settings, documents and secrets now
live in the app database. From your clone, once:

```bash
docker compose run --rm -v "$PWD:/import:ro" web python -m src.settings import /import
```

Résumé template packs move to `./data/templates` as part of that import.

**Set the admin password before exposing the port.** The web UI now requires a
login, and the first visitor claims it:

```bash
docker compose run --rm -it web python -m src.settings set-password
```

**Delete `JOB_AGG_BACKEND` from `.env`.** The AWS path is gone; the variable is
ignored and logs a warning. **If you were on DynamoDB, run v0.11.0's
`scripts/migrate_dynamo_to_sqlite.py` BEFORE upgrading** — it does not exist in
this release.

**Ollama Cloud users must set `relevance.ollama_host`.** It now drives
tailoring and DOCX import too, and defaults to `http://ollama:11434` (those
paths previously hardcoded `ollama.com`). A stale `JOB_AGG_OLLAMA_HOST` in
`.env` silently overrides the stored setting and is not copied by
`import-env-secrets` — delete it, or keep it deliberately.

**`./data` changes owner on first boot.** The container no longer runs as root:
the entrypoint takes ownership of `./data` as UID 1000 once, then drops
privileges. Nothing to do. On a Linux host whose own user is not UID 1000,
files under `./data` become owned by that UID — readable and backup-able, but
`rm` needs `sudo`, and a host-side `python -m src.web` against the same
database will not have write access.

**A clone with its own `docker-compose.override.yml` will fail to pull.** That
filename is now tracked. If you created one locally, move it aside before
`git pull`.

**Pulling instead of building.** After this upgrade a clone still builds, via
the tracked override. To run the published image instead, use
`docker compose -f docker-compose.yml up -d`. Add `-headless` to the tag if you
use Avature boards — the default image has no browser, and the poller logs
`headless_unavailable` if you have those boards configured without it.

Requires Docker Compose 2.24 or newer.

## [0.11.0] - 2026-08-23

The reliability release. Three incidents in three weeks — a JSON `null` that
killed the ats tier for sixteen hours, an LLM provider that degraded into
64-second timeouts, and a weekend that made two healthy connectors look dead —
and each one exposed something the watchdogs could not see. All three gaps are
closed: staleness is now judged per tier instead of across the pipeline,
source silence is measured in business time rather than wall clock, and a
malformed posting costs one posting instead of the whole cycle. Separately,
the board finally accepts the jobs that never came through a connector, and
the pipeline panel is legible at the shape the data actually has.

### Public release (2026-09-16)

This is the first public release, re-cut as a fresh tree with no history. On
top of the 0.11.0 changes below, it differs from the private 0.11.0 in these
ways:

- **Honest User-Agent.** Every request to a job board or ATS now identifies as
  `job-aggregator/<version> (+<repo url>)` instead of a browser string. The
  new optional `http.user_agent` key overrides it (`docs/CONFIG.md`).
- **Headless tier no longer masks automation.** The `navigator.webdriver`
  override and `--disable-blink-features=AutomationControlled` flag are gone
  from `src/headless.py`; Avature boards return the same jobs without them.
- **Hiring.cafe connector disabled.** hiring.cafe now robots-disallows the
  search endpoint it used and challenges every page it touched. The code
  stays, off by default, and the project does not work around the block.
- **Apache-2.0 licence**, package metadata, `SECURITY.md`, and Docker images
  that carry `LICENSE` and `README.md`.
- **Tests run without WeasyPrint's native libraries** (PDF tests skip instead
  of aborting collection); the hiring.cafe fixture is a 10-record
  parser-coverage sample; tests never open `./data/job_aggregator.db`.
- **AWS path unmaintained.** The Lambda/DynamoDB deployment is kept in the tree
  and CI still validates its Terraform, but the maintainer no longer runs it.

### Upgrading

**Rebuild — a restart is not enough.** The only new config key is the optional
`http.user_agent` (see the public-release notes above); there are no new env
vars or migrations, but the code is baked into the image:

```bash
docker compose up -d --build
```

**The per-tier watchdog is on by default** and needs no configuration — it
reads the cadences already in `schedules` (`ats_minutes`, `slow_minutes`,
`headless_minutes`). Like every ops alert it stays silent until an ops sink
URL is set (`JOB_AGG_OPS_NTFY_TOPIC_URL` or
`JOB_AGG_OPS_DISCORD_WEBHOOK_URL`).

**If you widened `ops_notify.source_zero_yield_hours` to stop weekend false
positives, you can put it back.** The per-source watchdog now spends its
silence budget only on weekdays, so a 24-hour window evaluated on a Sunday
afternoon reaches back to Friday 00:00 instead of expiring inside the weekend.
Midweek behaviour is unchanged.

### Added

- **Per-tier staleness watchdog** — `pipeline_stopped` reads `MAX(ts_ms)`
  across *all* tiers, so any one healthy tier hides a dead one; the ats tier
  has now died behind a green slow tier twice (a corrupt index, then a crash
  that killed the cycle before it could be recorded). Each fixed-interval tier
  now gets its own `tier_stopped:{tier}` condition judged against its own
  cadence, suppressed when every tier is stale — that case is
  `pipeline_stopped`'s job, and firing per-tier for a whole-poller outage is
  just noise. Discovery and digest are excluded on purpose: on daily and
  weekly cadences, "overdue" is a much weaker signal. Replayed against the
  real events as they stood mid-incident, it fires at 945 minutes stale where
  `pipeline_stopped` reports nothing.
- **Add an off-board opportunity by hand** — recruiter DMs, referrals and
  word-of-mouth roles never pass through a connector, so they were invisible
  to the board and absent from every funnel number: the apply and interview
  rates described only the polled half of the search. `/board` grows a
  collapsed add form that writes through the ordinary `claim_for_notify` +
  `set_status` path rather than a new store method, so the row is
  shape-identical to a polled one and triage, the board, the analytics
  sections and the ATS ranking all pick it up with no special-casing. The
  description is stored as a `description_snapshot` like any other posting, so
  tailoring and gap analysis work on it too. Hand-added rows carry no
  relevance score — nothing scored them, so the card shows `-` rather than
  inventing a number — never expire, and stay identifiable by a `MANUAL` tag
  and the reserved `manual:` source family.

### Fixed

- **A JSON `null` title killed the ats tier for sixteen hours.** Oracle Cloud
  began returning an explicit `null` for `Title` on some requisitions, and
  `.get(k, "")` defends only against a *missing* key — a present-but-null
  value still yields `None`, which reached `_seniority()` and raised. Because
  `normalize()` ran in a bare loop, one bad posting aborted the whole cycle:
  72 consecutive ats cycles crashed, one every ten minutes, and since the
  crash landed before `record_cycle()` the outage never reached `/pipeline`.
  Fixed in three layers so neither half can recur alone — twelve connectors
  shared the same latent bug, `normalize()` coerces the title once up front,
  and `run_once()` isolates each posting so a malformed payload costs one
  posting instead of the cycle. Skips land in a new `normalize_failures`
  rather than `fetch_failures`: the fetch itself succeeded, so a bad payload
  no longer flips a healthy connector's poll-health.
- **Transient LLM failures shipped postings unscored.** When Ollama Cloud
  degraded on 2026-08-02/03 it returned capacity 503s only after hanging ~64
  seconds; against the 20-second per-call timeout those surfaced as timeouts,
  and the fail-open path sent a `[?/10]` notification. Unscored notifications
  went from 0% over the preceding fortnight to 75%. Scoring now makes three
  bounded attempts with linear backoff — a healthy call answers in 3–6s, so
  retrying costs far less than the timeout it replaces. Only the final attempt
  falls back, so `llm_failures` still records one entry per genuinely-unscored
  posting and the `llm_degraded` threshold keeps its meaning. A 200 carrying
  unparseable content is *not* retried: that is model behaviour, not a
  transient fault, and retrying would triple the cost for the same answer.
- **The per-source watchdog alerted all weekend on healthy connectors.**
  recruitee and teamtailor fired every ~6h through 2026-08-22/23 while serving
  thousands of successful responses between them, with not a single
  connector-health row to show for it. The condition counts newly-seen
  postings, so what it actually measures is "did this family publish anything
  *new*" — and over a weekend the honest answer for a bursty family is no.
  Both had drifted over the qualifying row bar as discovery kept adding
  boards, into a shape the original backtest never covered. The silence budget
  now walks backwards through business time, spending itself only on weekdays,
  so a weekend stretches the window instead of emptying it. Midweek behaviour
  is untouched, and a family genuinely dead since before the weekend still
  fires — the weekend pauses the clock, it is not an amnesty.
- **The pipeline funnel panel was unreadable at the real data shape.** With
  1,751 of 1,855 matches dismissed at triage, a Sankey's single linear scale
  pinned every live stage to its 4px floor and crushed the whole funnel into a
  45px sliver, putting a line through the Interested, Applied, Interviewing
  and Rejected labels. Not tunable: giving Interviewing (5) even 10px needs a
  scale that makes Dismissed 3,500px tall. And past triage the funnel barely
  narrows — ~90% retention — before splitting into terminal outcomes, so a
  flow diagram was the wrong form for it regardless of layout. The panel is
  now three bands of plain bars: triage at true proportion, how far jobs got
  (with per-stage conversion), and where they stand now. The two lower bands
  disagree on purpose and their headings say why — one counts the furthest
  stage a job ever reached, the other where it sits today. Stage-level detail
  stays in the table view. The entire Sankey layout engine is deleted, and
  `funnel.py` is net shorter than before the work started.

## 0.10.0 - 2026-07-28

The discoverability release. "I keep seeing the same companies" turned out to
have four separate causes: no off-ATS source, a connector that had been dead
for 13 days without saying so, company names derived from the wrong part of
the source string, and a re-probe cadence that made newly-launched boards
invisible for three months. All four are fixed. A per-source watchdog and a
concentration report now make the next regression visible in hours instead of
weeks. Separately, job-description text is no longer trusted as instructions
by either LLM path.

### Upgrading

**Rebuild — a restart is not enough.** This release adds config fields, and a
container running an older image silently ignores them (pydantic drops unknown
keys without erroring):

```bash
docker compose up -d --build
```

**`discovery.no_match_revalidate_after_days` now defaults to 21, not 90.** A
board that returned no matching jobs was re-probed only once a quarter, so a
company that started hiring right after its probe stayed invisible until the
next one. If you set this key explicitly, your value still wins — only the
default moved.

**Existing rows keep their old company labels.** The `normalize` fix applies
at write time, so postings already in the database keep whatever name they
were stored with (`Ashby:Mercor`, `Hiringcafe`). This is cosmetic — matching,
filtering and the denylist all work on newly-written rows — but company counts
spanning the upgrade will double-count a few employers until the old rows age
out.

**Optional — Adzuna** (off-ATS coverage: small companies and staffing agencies
with no major ATS). Free self-service key at developer.adzuna.com, then set
`JOB_AGG_ADZUNA_APP_ID` / `JOB_AGG_ADZUNA_APP_KEY` and enable
`sources.adzuna` with at least one query. Enabled without the secrets, it logs
an error and skips. Alerts and the detail pane carry the required "Jobs by
Adzuna" attribution automatically.

**Optional — `filters.blocked_companies`** for employers whose req fan-out
drowns the feed. Their boards keep being polled; only the postings drop.

**Optional — the per-source watchdog** ships on by default with sane bars
(`ops_notify.source_zero_yield_hours: 24`,
`ops_notify.source_min_baseline_rows: 2000`) but, like every ops alert, stays
silent until an ops sink URL is set (`JOB_AGG_OPS_NTFY_TOPIC_URL` or
`JOB_AGG_OPS_DISCORD_WEBHOOK_URL`). SQLite runtimes only.

### Added

- **Adzuna connector** (slow tier, opt-in) — a hybrid source that both alerts
  directly and feeds Sightings into board discovery, reaching employers that
  run no major ATS. Country-scoped, query-driven, with a persisted
  `daily_call_budget` that keeps the free tier's monthly cap safe.
- **`filters.blocked_companies`** — a hard denylist gated before the role
  filter, matching whole words consecutively (so `microsoft` covers
  `Microsoft Corporation` but `apple` never matches `Applebee's`). Purely
  additive: delete an entry and the postings return on the next poll.
- **Per-source zero-yield watchdog** — alerts when one high-volume connector
  family goes silent while the rest of the pipeline keeps delivering. The
  "while others flow" condition is the whole point: a total blackout stays
  `pipeline_stopped`'s job, so one outage yields one alert, not nine.
  Backtested over 45 days of history: one alert, no false positives.
- **Prompt-injection defenses on untrusted posting text** — descriptions are
  defanged on the way in *and* on read (so rows written before this release
  are covered without a migration), and both LLM paths now fence posting text
  as data. The relevance prompt treats an attempt to manipulate scoring as a
  negative signal rather than something to merely ignore; the tailor policy
  prompt declares its own rules outrank anything inside the fence.
- **Workplace-type filter** in the triage inbox (remote/hybrid/onsite/unknown).
- **`scripts/concentration_report.py`** — measures employer concentration over
  a window against the equal-length window before it, so "am I seeing the same
  companies?" gets a number. Flags windows overlapping a known outage and
  marks denylisted employers still aging out of the window.
- **Provenance plumbing** — `Sighting.origin` on candidate rows, a generic
  `payload` field on `ConnectorState`, and per-source score statistics in the
  tuning script.

### Fixed

- **Company names were derived from the wrong source segment.** `_company()`
  split on the *first* colon, so every three-segment source was mislabeled —
  a hiring.cafe posting for Mercor became `Ashby:Mercor`, and hiring.cafe's
  own `company` field was fetched but never passed through. Every aggregator
  posting therefore looked like a handful of employers.
- **hiring.cafe had been dead for 13 days in silence.** Cloudflare began
  challenging the homepage, where the connector scraped its buildId — but a
  blanket `try/except` returned an empty result set, which the health
  classifier read as a healthy poll. The swallow is gone, and the buildId now
  resolves from `/jobs`, an unchallenged, robots-allowed page. If that page is
  ever challenged too, the connector fails and stays failed by design rather
  than escalating.
- **Connectors that are blocked now look blocked, not broken.** 401/403 is
  recorded as its own outcome on every tier and never auto-suppresses a board
  the way a 404/410 does.
- **The relevance scorer was reading truncated descriptions** — the cap rose
  from 2,000 to 8,000 characters, so requirements sections reach the model
  instead of being cut off mid-posting.

## 0.9.0 - 2026-07-15

The "make it yours" release. `config.yaml` and `profile.md` — the two files
every deployment personalizes — are no longer tracked by git. Fresh clones
seed them from neutral templates, and `git pull` can never again conflict
with (or overwrite) your customizations.

### Upgrading

**⚠️ This pull deletes your `config.yaml` and `profile.md` unless you follow
this recipe.** git either silently removes them (unmodified copies) or
refuses to pull (modified copies). One recipe covers both:

```sh
cp config.yaml /tmp/config.yaml.bak && cp profile.md /tmp/profile.md.bak
git checkout -- config.yaml profile.md   # discard local edits (backed up above)
git pull
cp /tmp/config.yaml.bak config.yaml && cp /tmp/profile.md.bak profile.md
```

After the restore the files are gitignored — pulls never touch them again.
If you *committed* local edits to these files, the pull instead stops with a
delete/modify conflict — back up both files, accept the deletions
(`git rm config.yaml profile.md && git commit`), then restore your backups.

**Rebuild the image** (the Dockerfile no longer bakes the two files in;
the compose bind-mounts are now the only path into the containers):

```bash
docker compose up -d --build
```

**Fresh clones:** seed the personal files before first start —
`cp config.example.yaml config.yaml && cp profile.example.md profile.md`
(GETTING_STARTED §3). Skipping this now fails loudly with copy instructions
instead of half-working.

### Added

- **`config.example.yaml`** — a minimal starter template (generic titles,
  US-remote location gate, a few live public boards, LLM scoring defaults),
  CI-validated: a drift-guard test loads it through the real config models,
  so schema changes can't silently rot the template.
- **`profile.example.md`** — a fictional-persona relevance profile keeping
  the worked-example shape (stack depth, strong/mild/weak fit bands, explicit
  geography rule).
- **Friendly startup errors** — a missing `config.yaml` now raises copy
  instructions instead of a bare traceback; so does the Docker-Compose trap
  where `docker compose up` before seeding creates *directory stubs* at the
  mount sources (with a scoped `rm -r` hint that can't eat a real file).

### Changed

- **BREAKING:** `config.yaml` and `profile.md` are untracked and gitignored;
  the repo ships templates instead of personal values (see Upgrading).
- The Docker image no longer bakes `config.yaml`/`profile.md` in — the
  compose bind-mounts are the only way they enter a container.
- Onboarding (README, GETTING_STARTED §3, docs/CONFIG.md, TROUBLESHOOTING)
  and the local-stack deploy runbook lead with the copy-first step; the
  runbook no longer treats box-local config edits as drift to reconcile.

## 0.8.0 - 2026-07-14

The résumé-builder release. Templates become **packs** you can upload and switch
between — including a one-time LLM conversion of a `.docx` template — and the
renderer grows user-editable **settings** (bullet caps, page limits, page
size/margins) on a new **/builder** page. Plus a new hard filter that rejects
contract/temp postings by default.

### Upgrading

**Rebuild the image — a plain restart is not enough** (new dependencies
`mammoth` + `python-multipart` and the built-in template packs are baked in,
and `docker-compose.yml` gains a writable `resume/templates` mount that needs
the containers recreated):

```bash
git pull
docker compose up -d --build
```

**Behavior change:** `filters.blocked_employment_types` defaults to
`[contract, temporary, part_time, internship]` — postings whose employment type
is *known* to be one of these are now hard-rejected (unknown types still pass).
Set it to `[]` in `config.yaml` to keep the old behavior.

**Behavior change:** an over-long résumé no longer fails the tailor render.
When trimming bottoms out at your min-bullets floor, the PDF is delivered
anyway with a visible fit warning.

No new `config.yaml` keys beyond `blocked_employment_types`; builder settings
live in SQLite and are edited on the page.

### Added

- **Template packs** — a template is a self-contained directory
  (`template.html.j2` + `fonts/` + `meta.yaml`). Two built-ins ship: `classic`
  (the locked teal/Gelasio design) and `headless` (Play sans, education-first,
  converted from the Headless résumé `.docx`). User packs live in
  `resume/templates/` (gitignored, bind-mounted). All templates — built-in and
  uploaded — render in a sandboxed, autoescaped Jinja2 environment.
- **/builder page** — settings form + template gallery: preview any pack
  against your real `content.json`, switch the active template, delete uploads.
- **Template uploads** — single `.html`/`.j2` files, `.zip` packs with fonts,
  or a `.docx` imported via one LLM call (mammoth extraction, embedded-font
  carry-over) and staged as *pending* until you accept the preview. Every
  upload passes an acceptance gate (sandbox parse + real render to PDF);
  traversal-proof, 10 MB body cap, 50 MB unpacked-zip cap, no partial packs.
- **Builder settings** — max bullets per experience/project, min bullets per
  entry (the trim floor), max pages, page size (Letter/A4), margins. Stored in
  SQLite, applied live; page geometry overrides work on any template.
- **Instant re-render** — `/tailor` gains a template picker that re-renders a
  stored run through any pack in ~1s with no LLM call; the CLI gains
  `--template <slug>`. A missing or broken template always falls back to
  `classic` with a visible warning — a tailor deep-link never dies over a
  template problem.
- **Employment-type hard filter** — reject contract/temp/part-time/internship
  postings when the connector exposes the type (Hiring.cafe, Lever); fails
  open on unknown types (`filters.blocked_employment_types`).

### Changed

- CI installs the `render` extra, so the WeasyPrint-backed render pipeline and
  upload gates are now genuinely exercised in CI (previously skipped), and
  guards `uv.lock` against drift (`uv lock --check`).
- Vendored OFL license texts now accompany both built-in packs' fonts
  (Gelasio, Play).

## 0.7.0 - 2026-07-13

The funnel-visibility release — two new ways to *see and act on* how
applications move. A new **Coach** page turns your own funnel, rejection-audit,
config, and résumé data into on-demand LLM recommendations for lifting response
rates. The analytics page gains a **pipeline sankey** that draws every
stage-to-stage flow with response-rate rings. And triage picks up
quality-of-life: a refresh button with a live new-job badge, and filters that
survive a reload.

### Upgrading

**Rebuild the image — a plain restart is not enough** (this release changes
`src/` and templates):

```bash
git pull && docker compose up -d --build
```

- **Coach is on by default, but SQLite-runtime only.** `coach.enabled` defaults
  to `true`, so the `/coach` page and its nav link appear on the local Docker /
  SQLite runtime. On the DynamoDB backend the provider reports *unavailable*
  and the page says so — run history needs SQLite. The `coach_runs` table is
  created automatically on boot; there is no migration to run.
- **Coach reuses your relevance LLM by default.** With `coach.provider` /
  `coach.model` unset, it follows `relevance.provider` / `relevance.model` — so
  it needs that provider's API key (or a local Ollama host), the same one
  scoring already uses. No new required env vars. Tune `coach.timeout_seconds`
  (120), `coach.max_jobs` (100), and `coach.window_days` (90) as needed; all
  six flags are documented in [docs/CONFIG.md](docs/CONFIG.md).
- **The analytics sankey and triage refresh need no migration.** The pipeline
  panel is derived from existing history, and triage filter state lives in the
  browser's localStorage — both ride in on the same rebuild.

### Added

- **Coach — LLM response-rate recommendations** — a new `/coach` page
  assembles a grounded snapshot of your pursued jobs, rejection-audit
  aggregates, filter/relevance config, and résumé/profile text, then asks an
  LLM for concrete, prioritized recommendations to improve application response
  rates. Anthropic / Gemini / Ollama engines with a fail-open contract (a
  failed call is a visible "error" run, never a 500). Every run is persisted
  and **past runs are browsable** from the page, reopening with their original
  recommendation cards. New `coach.*` config block.
- **Analytics pipeline sankey** — the analytics page gains a pipeline
  panel: a pure-Python, inline-SVG sankey of stage-to-stage flow plus
  ring-meter conversion rates, with funnel edge cases pinned
  (dismissed-after-interested → Withdrawn, no exits past Offer).
- **Triage refresh + persistence** — a refresh button on the triage list
  with a polled new-job count badge (fail-soft `/jobs/new-count` fragment),
  filter selections persisted across reloads via localStorage, and an
  always-visible filtered result count in the list header.

### Fixed

- **Coach hardening** — degrade gracefully on an undecodable profile and
  on config-driven snapshot inputs, and guard `AnthropicCoach` against non-dict
  tool input from the model.
- **Analytics funnel robustness** — tolerate malformed history rows in
  the funnel reduction (deriving stage ordinals), and floor sankey node heights
  to account for the ribbon stack, pinning the column map.

## 0.6.0 - 2026-07-11

The EU release. Geography is now **config-driven** end to end: the location
gate takes any country list, three EU-native ATS families join the roster,
and hiring.cafe searches carry **true server-side geo scope** after two
rounds of reverse-engineering (the old keyword query had never filtered
anything). Plus: every one of the app's 107 config flags is now documented
in a reference that CI keeps honest.

### Upgrading

**Rebuild the image — a plain restart is not enough** (this release changes
`src/`):

```bash
git pull && docker compose up -d --build
```

- **`location.remote_must_be_us` is deprecated.** Use
  `remote_policy: allowed_countries` (old `true`) or `remote_policy: anywhere`
  (old `false`). The legacy key still works with a deprecation warning;
  setting both is an error. The shipped config is migrated.
- **Expect a one-time re-alert blip after deploying.** hiring.cafe live ATS
  labels are now normalized (`grnhse`→`greenhouse`, `icims2`→`icims`,
  `taleo_careersection`→`taleo`), which changes those postings' job ids —
  anything alerted from the aliased families ≤2 days before the deploy may
  alert once more, then it self-heals.
- **hiring.cafe results are geo-pinned by config now.** The shipped config
  sets `sources.hiringcafe.location: US`; before, results were silently
  scoped by the box's egress IP (the server geo-defaults). Unset the knob to
  keep the old behavior.
- **EU levers are opt-in and default off**: `discovery.eu_seeds_enabled`
  (curated 149-company seed sweep), geo-scoped `extra_queries` entries
  (`{query: software engineer, location: Europe}`), and the new
  `personio` / `recruitee` / `teamtailor` slug families. US-only deployments
  are unaffected without opting in.
- **`sources.hiringcafe.title_prefilter` was removed** — no code ever read
  it. A leftover key in a custom config is silently ignored; delete at
  leisure.
- **New:** every flag is documented in [docs/CONFIG.md](docs/CONFIG.md);
  a CI drift guard now fails any PR that adds a config flag without a row.

### Added

- **Configurable geography** — the location gate reads
  `location.allowed_countries` (ISO-2 codes) + `remote_policy` instead of
  hardcoded US logic. Remote postings match by *coverage* ("Remote — EMEA"
  passes for `DE`; "Remote - Anywhere in the U.S." counts as US, not
  worldwide). Worked EU setup: `docs/examples/eu-config.md`.
- **EU ATS families** — Personio, Recruitee, and Teamtailor connectors:
  pollable, discoverable (probe + fingerprint + URL parsing), and wired into
  hiring.cafe sighting conversion. Nine slug families, fifteen overall.
- **EU discovery levers** — opt-in `scripts/seeds/eu_companies.csv`
  board sweep and multi-query hiring.cafe mining with cross-query dedup.
- **hiring.cafe geo-scoped queries** — reverse-engineered the
  `searchState` `locations` key (country + continent shapes, live-verified);
  `sources.hiringcafe.location` pins the primary search and `extra_queries`
  entries take a `{query, location}` mapping form.
- **Configuration reference + drift guard** — `docs/CONFIG.md`
  documents all 107 flags with defaults; `tests/test_config_docs.py` fails CI
  in both directions (undocumented flag / stale row); six previously
  invisible flags surfaced as commented defaults in `config.yaml`.

### Fixed

- **hiring.cafe search actually filters now** — the SSR endpoint
  ignores its `q=` parameter, so the miner had always sampled the unfiltered
  firehose; the real parameter is a `searchState` JSON blob. Live ATS labels
  are also normalized so active-set dedup suppresses boards we already poll
  directly.
- Legacy `remote_must_be_us` shim no longer mutates the caller's dict, and an
  explicit country now suppresses a generic "anywhere/worldwide" token in the
  same location string.
- The `filters.comp_floor_usd` doc row described the gate backwards — it
  rejects on the advertised *minimum*, not maximum.

### Removed

- Dead `sources.hiringcafe.title_prefilter` flag — defined and shipped since
  the hiring.cafe connector landed, never read by any code.

## 0.5.0 - 2026-07-10

Discovery diversification lands its final two pieces: name-based candidates now
run a full **conversion chain** (slug variants + a careers-page fingerprint
fallback) instead of a single slug guess, and configured **VC portfolios**
(a16z, Sequoia) are re-fetched weekly into the same candidate pipeline —
shipped inert until you opt in.

### Upgrading

**Rebuild the image — a plain restart is not enough** (this release changes
`src/`):

```bash
git pull && docker compose up -d --build
```

- **The conversion chain is automatic — no knobs to set.** Every name-based
  candidate (yc-oss, `manual_companies`, VC) gets up to 3 slug variants across
  all 6 slug ATSs, then a careers-page fingerprint when a website is known.
  Misses stay suppressed for the existing `no_match_revalidate_after_days`
  window (default 90); fully-exhausted misses re-probe **last**, from leftover
  budget only, so they can't crowd out fresh candidates. Watch
  `discovery_probe_phase_done` (`ok`/`board_ok`/`skipped_exhausted`) and
  `discovery_exhausted_drain_done`.
- **VC portfolio auto-discovery ships inert.** Opt in with
  `discovery.vc_firms: ["a16z", "sequoia"]` (bind-mounted `config.yaml` →
  restart only). Optional knobs: `vc_refresh_days` (default 7),
  `vc_capture_cap` (default 500 per firm per run). Watch `vc_discovery_done`,
  then `discovery_candidates_drained` (`name_ok`/`name_board_ok`/
  `name_no_match`) over the following runs. Absorption pace scales with
  `max_validations_per_run` — the shipped config uses 600; on the low default
  of 200, two large portfolios can monopolize the candidate queue for weeks.
- No migrations, no new required config — all new store fields are additive
  (JSON-blob stores on both backends).

### Added

- **Conversion chain** — new `src/slugging.py` derives up to 3 ordered
  slug variants per company (normalized name with punctuation and trailing
  `Inc/LLC/Ltd/Corp` stripped, the website's domain label, the source's own
  slug); each is probed across all 6 slug ATSs, with a careers-page
  fingerprint fallback for companies with a website — so a YC company on
  Workday still converts to a polled board. `nomatch` rows now learn
  `company_name`/`website`/`methods_tried`, and fully-exhausted misses drain
  in a new lowest-priority phase.
- **VC portfolio auto-discovery** — a weekly, watermark-gated fetch of
  configured VC portfolios stages every in-profile company as a zero-probe
  `candidate` row; the daily discovery run validates them through the
  conversion chain on the normal probe budget. Failed or **empty** fetches
  don't advance the watermark, so they retry the next day; unknown firm names
  in config are logged and skipped.

### Fixed

- `manual_companies` misses are now keyed consistently with the chain, so
  their 90-day suppression actually holds (previously a missing manual entry
  was re-probed — 12 probes — every single run).
- Companies already converted to a structured board (Workday, Oracle Cloud,
  Eightfold, Taleo, iCIMS) no longer re-fingerprint on every crawl — an
  ok/quarantined board for the seed domain short-circuits the chain.
- Rippling slugs are now part of the discovery-tier active set, so configured
  Rippling companies aren't re-probed by discovery.
- Hiring.cafe capture dedups against VC-staged candidate rows, avoiding
  duplicate validation probes when both sources sight the same company.

### Documentation

- README, GETTING_STARTED (§3d conversion chain, new "VC portfolio
  auto-discovery" section), and TROUBLESHOOTING (two new rows) now cover both
  features; `src/vc_portfolio.py`'s stale "never run from the poller" note is
  gone.

## 0.4.0 - 2026-07-10

Discovery becomes self-expanding: the Hiring.cafe firehose now feeds the
discovery pipeline, staging companies you don't yet poll as validated
candidates. Plus deeper résumé tailoring (rewrite-all contract, JD-aware skill
rows), a fresher `/pipeline` dashboard, and two new Avature boards.

### Upgrading

**Rebuild the image — a plain restart is not enough** (this release changes
`src/`):

```bash
git pull && docker compose up -d --build
```

- **Hiring.cafe candidate mining is on by default** — but only does anything if
  the firehose itself is enabled (`sources.hiringcafe.enabled: true`). Sighted
  companies are staged as candidates on `slow` cycles and validated on the
  daily `discovery` run; healthy ones join `ats` polling automatically. Watch
  `hiringcafe_sightings_captured` and `discovery_candidates_drained`. To
  disable just the mining: `discovery.hiringcafe_mining_enabled: false`. Board
  candidates are SQLite-only (inert on DynamoDB); slug candidates work on both
  backends.
- **Discovery now reserves probes for revalidation** (`revalidate_reserve`,
  default 100, carved out of `max_validations_per_run`). This fixes a latent
  starvation bug where a busy run could skip revalidation entirely — but if
  your `max_validations_per_run` is small (≲200), raise it or lower the
  reserve so the crawl keeps headroom. The shipped `config.yaml` uses 600.
- **Tailoring rewrite-all needs a longer LLM timeout** on a grown content
  bank — the shipped `config.yaml` now sets the tailoring timeout to 180s
  (was 60s); carry that over if you maintain your own config.

### Added

- **Hiring.cafe candidate mining** — the slow-tier firehose connector collects
  a `Sighting` per foreign posting; apply URLs are classified at zero HTTP cost
  (the fingerprint URL classifiers) and staged as `candidate` rows in
  `discovered_slugs` / `discovered_boards`; the daily discovery tier drains
  candidates first within a priority-ordered probe budget (claimed-family slug
  probes cost 1 request; enterprise boards get a live verify). New knobs:
  `hiringcafe_mining_enabled`, `candidate_capture_cap`, `revalidate_reserve`.
- **Tailoring depth** — the tailor rewrites *every* content bullet (ranked
  most-JD-relevant first) with per-entry grounding and a completeness
  backstop; skill-category rows reorder by JD relevance with Languages pinned
  first.
- **`/pipeline` freshness** — per-tier last-cycle anchors that survive
  pruning, recent-cycles detail table, failure tallies with last-seen recency
  (old bursts dimmed), 48h-bounded stall detection, and page auto-refresh.
  Fetch failures also roll up by connector family.
- **Triage bulk actions** — set a status on many inbox matches at once, plus
  an adjustable page size.
- Jacobs + CBRE Avature boards (headless tier).
- Local-stack upgrade runbook (`docs/runbooks/deploy-local-stack.md`)
  and a project verify recipe for driving the web app on a scratch DB.

### Changed

- `ats` cadence 15m → 10m; board stale badge 10d → 14d.
- Tailoring LLM timeout 60s → 180s and output cap (`num_predict`)
  8192 → 16384 for rewrite-all on the grown content bank.

### Fixed

- **Promoted boards no longer double-alert**: once a mined board is polled
  directly, its `(family, token)` pair joins the Hiring.cafe active set, so
  the firehose stops re-emitting the same roles.
- Workday apply URLs include the career-site segment, so links land on the
  posting's real careers page.
- Discovery revalidation can no longer be starved by a busy crawl
  (`revalidate_reserve`).

## 0.3.0 - 2026-07-04

Twenty-one merged PRs since `0.2.0`: five new ATS connector families plus a
headless-browser tier, hands-off enterprise board discovery, a rejection audit
trail with ops alerts and threshold self-tuning, and end-to-end application
tracking (board automation, Gmail ingestion, apply kit).

### Upgrading

**Rebuild the image — a plain restart is not enough.** This release changes
`src/` *and* bakes new assets into the image (Playwright/Chromium for the
headless tier, the enterprise seed CSV for board discovery):

```bash
git pull && docker compose up -d --build
```

- **Automated board discovery is on by default.** After the rebuild, the daily
  `discovery` tier begins fingerprinting a ~490-company seed list and auto-adds
  verified enterprise boards to `ats` polling (over ~8 days, 60 per run). Watch
  the `board_discovery_done` log line. It is SQLite-only — inert on the
  DynamoDB / Lambda backend. To disable: set
  `discovery.board_discovery_enabled: false`.
- **New SQLite tables/columns are created automatically** on startup
  (`rejected_postings`, `ops_alert_state`, `pipeline_events`,
  `discovered_boards`, plus new `seen_jobs` columns) — no manual migration. The
  audit, ops-alert, and board-discovery features exist only on the SQLite
  backend.
- **Check `schedules.ats_minutes` in your `config.yaml`.** A testing leftover
  had shipped the `ats` cadence as `1` (polls every board every minute); the
  corrected default is `15`. Raise it if yours still says `1`.
- **New opt-in env vars** (all off when empty; add to `.env`, then
  `docker compose up -d --force-recreate`):
  - Ops push-alerts: `JOB_AGG_OPS_NTFY_TOPIC_URL` and/or
    `JOB_AGG_OPS_DISCORD_WEBHOOK_URL` (optional dead-man's switch
    `JOB_AGG_HEARTBEAT_URL`).
  - Gmail ingestion: `JOB_AGG_GMAIL_ADDRESS` + `JOB_AGG_GMAIL_APP_PASSWORD`
    (a Gmail **app password**; requires 2-Step Verification).
- **Apply kit:** `cp resume/facts.example.yaml resume/facts.yaml` for `/kit` to
  populate.
- **Avature (headless) is opt-in:** add `sources.avature` entries by hand after
  confirming each site passes a real browser (some are WAF-gated). The
  `[headless]` extra and Chromium are already baked into the Docker image.

### Added

- **New ATS connectors:** Oracle Recruiting Cloud; iCIMS / schema.org
  JSON-LD board scraper; Eightfold.ai; Phenom People; modern
  Oracle Taleo.
- **Headless tier:** a Playwright/Chromium `headless` scheduler tier with an
  Avature connector for JS-gated boards, gated behind the `[headless]` extra
 .
- **Automated board discovery:** the daily `discovery` tier fingerprints an
  enterprise seed list and auto-adds verified boards via a new
  `discovered_boards` store — no `config.yaml` writes; poll-health governs them
 . The manual `scripts/discover_enterprise.py` CLI remains for
  review-first onboarding.
- **Rejection audit & ops:** an audit trail of every filtered/suppressed posting
  with a `/audit` rescue-or-confirm UI and a daily retention sweep; ops
  push-alerts for pipeline-stopped / zero-yield / LLM-degraded, with cooldowns
  and recovery notices. LLM-failure tally + degraded heartbeat on
  `/pipeline`. Per-tier freshness heartbeat + hourly SQLite
  integrity/REINDEX guard.
- **Threshold self-tuning:** `scripts/tune_thresholds.py` mines `/audit`
  verdicts into suggested `score_low` / title-regex changes (read-only report)
 .
- **Application tracking:** board automation — daily closed-posting sweep +
  stale/closed digest; Gmail ingestion — hourly read-only IMAP sweep
  badging rejections/receipts as board suggestions; apply kit `/kit`
  tap-to-copy answers from `resume/facts.yaml` with an "Apply Autofill"
  bookmarklet for Greenhouse/Lever/Ashby forms.
- **Discovery breadth:** VC-portfolio bulk-import drivers; 61
  fingerprint-discovered enterprise boards (49 Workday + 12 Oracle Cloud); new
  companies Toyota + Thomson Reuters (Workday) and Nielsen (SmartRecruiters).
- **Web UI / pipeline:** local pipeline cycle telemetry + errors on `/pipeline`
 ; triage pagination + ops freshness. Résumé tailoring shows all
  skills and fills the page; Tailscale walkthrough for the mobile loop.
- `scripts/restore_env_from_ssm.sh` to rebuild `.env` from kept SSM params.

### Changed

- Workable moved to the `slow` tier with generic 429 rate-limit backoff.
- Widened title filters from `/audit` evidence; yc-oss `min_team_size` retuned
  to 5.
- Documentation refreshed for the full v0.3.0 feature set (README,
  GETTING_STARTED, TROUBLESHOOTING).

### Fixed

- Corrected the default `ats` cadence from a 1-minute testing leftover (which
  hammered every board each minute) to 15 minutes.
- Bookmarklet percent-encoded so it survives the browser URL parser; the
  select/radio matcher fills the exact option only.
- Taleo merge quotes numeric `section:` values and rejects non-board sections
 ; the Sequoia/a16z VC drivers fail loud on API blocks instead of
  returning empty.
- Board discovery ships its seed CSV in the image, follows redirects, and never
  quarantines or demotes benign/live boards.
- Hardened the `llm_failures` migration against a concurrent `ALTER` race;
  audit rows exempt verdicted postings from TTL/prune and rescue/confirm
  dual-origin postings across both stores.
- Connector hardening: iCIMS multi-URL detection + SuccessFactors dead-board
  suppression; Eightfold flavor/domain probing; Phenom watermark
  clamp + sitemap fallback; Oracle Cloud display names + list ordering
 ; Avature full-list render wait + context teardown + empty-cycle guard
 .

## 0.2.0 - 2026-06-26

### Upgrading

First tagged release since `0.1.0`. If you run this app, note before redeploying:

- **The app now runs locally on Docker Compose + SQLite** (no AWS required). See
  the README "Run locally" section: `cp .env.example .env`, choose an LLM
  provider, `docker compose up -d --build`. State backend is selected by
  `JOB_AGG_BACKEND` (`sqlite` default, `dynamodb` for the AWS Lambda).
- **AWS Lambda upgraders MUST set `JOB_AGG_BACKEND=dynamodb`** on the function's
  environment. The code default is `sqlite`; without the env var the poller
  tries to open a SQLite file on the read-only Lambda FS and crashes every
  cycle. `infra/lambda.tf` declares it, but a code-only `aws lambda
  update-function-code` deploy will NOT apply it — set it with
  `aws lambda update-function-configuration` (preserving existing vars).
- **New env vars:** `JOB_AGG_OLLAMA_HOST` (Ollama host; defaults to the local
  container in Compose, set to `https://ollama.com` for hosted Ollama Cloud),
  and the `.env` file for Compose secrets. See `.env.example`.
- **Optional one-shot data migration** from DynamoDB to SQLite:
  `python -m scripts.migrate_dynamo_to_sqlite` (run once with AWS read creds).
- **Config/profile changes still require a redeploy** — `config.yaml` and
  `profile.md` are bundled into the Lambda zip at `scripts/package.sh` time.

### Added

- Add design spec for job aggregator
- Add Remotive and Remote OK aggregator connectors
- Add ConnectorState and FetchResult types
- Add Gemini as alternative LLM provider; rewrite README
- Add ollama provider option + ollama_api_key secret
- Add OllamaRelevanceScorer (prompt-engineered JSON, fail-open)
- Add --calibrate mode to score sample + emit score histogram
- Add san francisco to allowed_cities (takes effect on next deploy)
- Add Portland as an allowed hybrid city
- Add resume gap-analysis design spec
- Add implementation plan
- Add GapAnalysisConfig (disabled by default)
- Add Gaps dataclass, prompts, and shared skill parser
- Add AnthropicGapAnalyzer
- Add GeminiGapAnalyzer
- Add OllamaGapAnalyzer
- Add recent_gap_lists scan for the gap digest
- Add tally_gaps + format_gap_digest
- Add _build_gap_analyzer (gated, soft-fail)
- Add digest tier and wire gap analyzer into run_once
- Add --gaps-report CLI command
- Add sources.rippling + raise discovery budget to 600
- Confirm Rippling location labels; docs(readme): add Rippling ATS
- Merge feat/rippling-ats: add Rippling as a 6th ATS family
- Add mark_suppressed conditional write for score-low postings
- Add list_suppressed for the pipeline score histogram
- Add ConnectorHealthStore (sparse poll-health table)
- Add connector_health table + UpdateItem/DeleteItem IAM + env var
- Add offer/rejected/ghosted statuses, persist their rows
- Add board page-title + archive status-tag colors
- Add 25 live-validated enterprise Workday tenants (13 -> 38)

### Changed

- Gitignore .worktrees/
- Scaffold project layout and dependencies
- YAML + SSM-backed config loader with pydantic validation
- Data models and JSON structured logging
- DynamoDB-backed seen_jobs store with batched diff
- Protocol and factory wiring
- Greenhouse connector
- Lever connector
- Ashby connector
- Workable connector
- HN Who is Hiring connector
- Derive seniority, location tags, and stack from raw postings
- Tri-state predicate pipeline (role, seniority, location, stack, comp)
- Payload formatter with humanized age and tag derivation
- Ntfy.sh sink with quiet-hours priority
- Discord webhook sink with embed format
- Parallel sink fan-out with per-sink failure isolation
- Wire fetch/normalize/diff/filter/notify with failure isolation
- Lambda entry point and local CLI with --dry-run
- Capture_fixture.py for refreshing connector test inputs
- Lambda zip packaging and add_company helper
- TF backend bucket + lock table module
- DynamoDB seen_jobs and SSM SecureString shells
- IAM least-privilege role, Lambda, and EventBridge schedules
- CloudWatch alarm + SNS email for Lambda failures
- README, GH Actions for unit + weekly integration tests
- Merge feature/job-aggregator into main
- Seed_companies.py for bulk-adding ~80 curated SWE employers
- Geographic filter — US-only remote + city allowlist
- Max_age_days predicate to drop stale postings
- Tighten max_age_days from 14 to 2 for first-to-apply intent
- Sourcing & freshness design spec
- Implementation plan for freshness foundation
- Migrate to (client, state) -> FetchResult contract
- SourceStateStore for ETag/Last-Modified persistence
- Source_state DDB table + IAM + Lambda env var
- Thread SourceStateStore through fetch cycle
- Honor ETag/Last-Modified for conditional GET
- Honor ETag/Last-Modified for conditional GET
- Honor ETag/Last-Modified for conditional GET
- Honor ETag/Last-Modified for conditional GET
- Merge feat/freshness-foundation: Plan 1 (freshness foundation)
- Drop ATS poll cadence from 2min to 1min
- Implementation plan for coverage expansion (Plan 2)
- SourcesConfig.smartrecruiters list field
- Direct-ATS connector with conditional GET
- Build_connectors dispatches SmartRecruiters slugs
- DiscoveredSlugsStore + 'discovery' tier in Tier type
- Discovered_slugs DDB table + IAM + Lambda env var
- DiscoveryConfig + HiringCafeConfig + discovery_hours schedule
- Build_connectors unions config + discovered_slugs
- Discovery EventBridge rule (24h cadence)
- Discover.py — probe ATS family for a company slug
- HTTP client + schema-flexible response parser
- Routine that validates Hiring.cafe-discovered ATS slugs
- Slow-tier safety-net connector with active-set dedup
- Wire connector into slow tier with active-set dedup
- Route tier=discovery to run_discovery; pass discovered store to build_connectors
- Enable hiringcafe + discovery sections
- Disable hiringcafe + autodiscovery (Cloudflare JS challenge blocks Lambda)
- Static discovery oracle design spec (Plan 3)
- Implementation plan for static discovery oracle (Plan 3)
- JSON client + filter + slug derivation
- DiscoveredSlugsStore — no_match + revalidation methods
- Rewrite — yc-oss + manual list, probe-all-5, revalidation
- DiscoveryConfig — yc_oss_*, manual_companies, no_match_revalidate
- Wire yc-oss fetcher + new DiscoveryConfig fields
- Re-enable discovery with yc-oss + manual_companies seed
- LLM relevance scoring + active-set fix design spec (Plan 4)
- Implementation plan for LLM relevance scoring + active-set fix (Plan 4)
- RelevanceScorer + Score dataclass + profile.md skeleton
- RelevanceConfig + Secrets.anthropic_api_key
- NotificationPayload relevance fields + score-band priority routing
- Score relevance between filter and notify
- ANTHROPIC_API_KEY SSM param + Lambda env + IAM read + package profile.md
- Merge feat/llm-relevance: Plan 4 (LLM relevance scoring + active-set fix)
- Personal relevance profile for LLM scoring
- Expand titles+stack, fix dead ATS slugs, enable LLM scoring with low-score suppression
- Health-check commands for Lambda + EventBridge + connectors
- Coverage Expansion v2 — Lever/SR/Workable seeds, manual_companies AI growth, Workday connector, Wellfound spike, yc-oss retune
- Phase 1 coverage expansion — Lever, SR, manual_companies
- Mark Phase 1 deployed; record fetched_count 1497→2437
- Workday as the 6th ATS family + 14 verified tenants
- Mark Phase 2 deployed; record limit=50 Workday WAF threshold
- Raise score_low 3→4 (notify only on score >= 5)
- Wellfound blocked by DataDome; pivot Phase 3 to VC portfolio importer
- VC portfolio importer — accessibility, yield, architecture
- VC portfolio importer with a16z driver
- Import 148 a16z portfolio companies into sources.{ats}
- Mark Phase 3' deployed; record 53→201 ATS connectors and 36s Lambda runtime
- Refresh stale numbers after Gemini + cadence changes
- Correct AWS + Haiku cost claims against real billing data
- Design Ollama Cloud relevance provider + calibration mode
- Implementation plan for Ollama Cloud relevance provider
- Extract shared _parse_score_json; Gemini uses it
- Update module docstring for third (Ollama) provider
- Wire ollama provider into _build_relevance_scorer
- Provision ollama_api_key SSM param + Lambda env var
- Calibrate score_low + flip to Ollama Cloud
- Clarify calibrate writes source_state cache hints, not seen_jobs
- Merge branch 'feat/ollama-relevance-provider'
- Merge branch 'fix/calibrate-cap-post-filter'
- Merge branch 'fix/ollama-num-predict-reasoning'
- Switch relevance provider to Ollama Cloud gpt-oss:120b
- Merge branch 'config/flip-relevance-to-ollama'
- Make Ollama Cloud the documented default; add --calibrate
- Merge branch 'docs/readme-ollama'
- Merge branch 'fix/connector-workplace-type'
- Fold hybrid/onsite workplaceType into location text
- Accept San Francisco as a hybrid location
- Merge branch 'feat/sf-hybrid-labeling'
- Merge branch 'test/ashby-null-workplacetype'
- Merge branch 'config/add-portland'
- Suppress fully on-site roles, even in allowed cities
- Merge branch 'profile/suppress-onsite'
- Raise comp target/floor 120k -> 140k
- Merge branch 'config/comp-target-140k'
- Lower comp hard floor to 130k (profile target stays 140k+)
- Merge branch 'config/comp-floor-130k'
- Merge branch 'fix/relevance-log-error-type'
- Raise relevance LLM timeout 10s -> 20s
- Merge branch 'config/relevance-timeout-20s'
- Persist relevance score and rationale on notified jobs
- Score remote roles above hybrid
- Persist gaps list on the notified-job row
- Carry resume gaps on NotificationPayload
- Render resume gaps as a Discord Stretch areas field
- Compute resume gaps for survivors, persist + surface
- Package resume.md, gitignore it, add config block + template
- Weekly EventBridge rule for the resume-gap digest
- Merge branch 'feat/resume-gap-analysis'
- Document résumé gap analysis feature
- Triage UI design — local-first match inbox
- Triage UI implementation plan (9 tasks)
- Persist match display fields at notify time
- List_matches + get_match read methods for triage UI
- Set_status with TTL-drop on kept statuses
- Triage repo — view model, filter/sort/search, store adapter
- FastAPI app factory + inbox shell + static assets
- GET /jobs filtered/sorted/searched list partial
- GET /detail match detail partial + expired fallback
- POST /status — set triage status, refresh list
- Python -m src.web entrypoint + README
- Merge feat/triage-ui: local triage UI (sub-project 1 of 3)
- Pipeline ops dashboard design (sub-project 2 of 3)
- Include tier in invocation_done log for ops dashboard
- Cloudwatch log-event aggregation (pure) for ops cycles panel
- FilterLogEvents fetch + load_pipeline_activity (tolerant JSON parse)
- Pure connector_health + score_analytics aggregations
- OpsProvider (fail-soft) wired into create_app
- /pipeline page — nav, status strip, health + analytics panels
- /pipeline/cycles lazy panel + OOB last-cycle chip
- Document the /pipeline ops dashboard
- Commit pipeline ops dashboard implementation plan
- Merge feat/pipeline-ops: pipeline ops dashboard (sub-project 2 of 3)
- Merge fix/ops-health-no-match: keep discovery no_match out of unhealthy table
- Enable Hiring.cafe firehose for broader coverage
- Rippling ATS connector design
- Rippling ATS connector implementation plan (5 tasks)
- Rippling ATS board connector (list-only)
- Wire Rippling into build_connectors (config + discovered + hiringcafe set)
- Probe Rippling as a 6th ATS family
- Correct stale "5 ATS families" references to 6 (Rippling added)
- Persist & surface LLM-suppressed postings
- Implementation plan for suppressed-postings persistence
- Persist suppressed postings so they aren't re-scored each cycle
- Include suppressed scores in the pipeline histogram
- Surface suppressed count in the analytics panel
- Merge branch 'feat/persist-suppressed-postings'
- Prune 7 confirmed-dead ATS slugs
- Connector poll-health circuit breaker
- Connector poll-health circuit breaker implementation plan
- Outcome classification + circuit-breaker update
- Build_connectors skips suppressed connectors
- Feed per-cycle outcomes into the poll-health circuit breaker
- Type-annotate the health param for consistency
- Recover_suppressed re-probes dead connectors on discovery tier
- Recover_suppressed clears orphaned rows for removed connectors
- Wire poll-health gating + discovery-tier recovery
- Surface auto-suppressed connectors in the health panel
- Merge branch 'feat/connector-poll-health'
- Install the [web] extra so tests/web/ can run
- Dark-mode UI theme
- Dark-mode UI theme implementation plan
- Dark theme palette for the UI (CSS variables)
- Declare dark color-scheme so native controls render dark
- Merge branch 'feat/dark-mode-ui'
- Match analytics UI page (sub-project 3)
- Match analytics UI implementation plan (sub-project 3)
- Tally_gaps min_count/top params (UI shows singletons)
- Matches_by_week weekly bucketing for analytics
- Rank_by_company / rank_by_ats for analytics
- Gap_window tally + denominator for analytics
- MatchAnalytics fail-soft provider (single scan)
- /analytics page — matches over time, sources, stretch skills
- Merge branch 'feat/match-analytics-ui'
- Document /analytics page; refresh table/test/LOC counts
- Two-column ops grid so wide screens aren't mostly empty
- Resume tailoring umbrella + sub-project A (engine)
- Resume tailoring engine (sub-project A) implementation plan
- TailoringConfig + config.yaml block
- Content/evidence/TailorResult dataclasses
- Content.json loader with loud validation
- Evidence bank loader
- Committed content example + schema docs + gitignore
- Clarify content.json schema keys in resume README
- No-fabrication policy prompt + serializers
- Result parser with deterministic grounding guards
- OllamaTailorEngine async call, fail-open
- Build_tailor_engine factory (ollama, fail-soft)
- CLI writes tailored/<job-id>/ render input + cover letter + fit
- Thin Jira-CSV -> evidence.json regen script
- Regen script self-creates --out dir; drop unused test param
- Document src/tailor + regen script; refresh test/LOC counts
- Merge branch 'feat/resume-tailoring-engine'
- B-render — full-résumé single-page PDF renderer
- B-render implementation plan (12 TDD tasks)
- Project bullets + education/volunteer in content model
- Loader parses project bullets, education, volunteer
- Content.example.json on the extended schema
- Derive + reorder categorized skill rows
- Assemble_render_doc merges tailored + static content
- Jinja template reproducing baseline.html
- Vendor Gelasio (OFL) + [render] extra
- WeasyPrint render + page_count + RenderError
- Single-page auto-trim loop (projects then experience)
- Render_resume entry point (assemble + fit + pdf)
- CLI emits tailored/<id>/resume.pdf (--no-pdf to skip)
- Document the PDF render path; refresh test/LOC counts
- Merge branch 'feat/resume-render'
- Gitignore resume/baseline.html (PII — dev reference, not needed at runtime)
- B-tailor++ — tailor project bullets + skills
- B-tailor++ implementation plan (6 TDD tasks)
- TailoredProject + projects field + project_bullet_ids
- Prompt rules for tailored projects, pruned skills, one-page budget
- Ground tailored project bullets + drop invented skills
- Assemble filters skills to the engine's pruned selection
- Assemble renders tailored project bullets (fallback to static)
- Render-input carries projects; real-résumé integration test
- Refresh test/LOC counts
- Merge branch 'feat/resume-tailor-plus'
- C-endpoint — hosted tailor endpoint (container Lambda + Function URL)
- C-endpoint implementation plan (8 tasks)
- HMAC signed-token auth (sign/verify)
- Read stored JD (description_snapshot) from seen_jobs
- Self-contained mobile loading + error pages
- Run_tailor core (cache, tailor, render, S3 presign)
- Lambda Function URL handler (page vs run routing)
- Dockerfile + ECR build/push script
- .dockerignore to shrink the build context
- Terraform — ECR, S3, container Lambda, Function URL, IAM, SSM
- Enable tailoring in config + deploy runbook; refresh counts
- Merge branch 'feat/tailor-endpoint'
- C-wiring — connect the detector to the tailor endpoint
- C-wiring implementation plan (8 tasks)
- Store-forward the JD (description_snapshot, 30KB cap) on notified rows
- Build_tailor_url — signed deep-link for alerts
- Tailor_url on NotificationPayload + format_payload
- Tailor endpoint url + signing secret in Secrets/_load_secrets
- Ntfy Tailor-resume action button when tailor_url is set
- Discord Tailor-résumé embed field when tailor_url is set
- Orchestrator builds the signed tailor_url when the endpoint is configured
- Detector env vars for the tailor endpoint; refresh counts
- Merge branch 'feat/tailor-wiring'
- Refresh tailor-endpoint deploy — alarm_email var, secret import, manifest + 403 troubleshooting
- Allow frontend engineer/developer titles
- Application board — pipeline view with stage timeline
- Application board implementation plan (7 TDD tasks)
- Append a {status,at} history entry on every transition
- TriageMatch history + stage/staleness derived properties
- Board.stale_after_days threshold wired into the web app
- BoardProvider buckets pursued apps into columns + archive
- /board kanban page + /board/advance route + nav link
- Inbox offers all 8 statuses; board styling
- Merge branch 'feat/application-board'
- Spec for any-workplace-type locations + city expansion
- Implementation plan for any-workplace-type locations
- Expand allowed_cities with new metros + metro aliases
- Score remote/hybrid/on-site on merit (slight remote tiebreak)
- Merge branch 'feat/any-workplace-locations'
- Spec for running entirely locally (Docker Compose + SQLite)
- Implementation plan for running entirely locally
- SQLite connection factory + schema
- SqliteSeenJobsStore mirroring DynamoDB seen_jobs
- SqliteSourceStateStore
- SqliteDiscoveredSlugsStore
- SqliteConnectorHealthStore
- Build_stores factory + Stores dataclass
- SeenJobsStore.get_jd (parity with SQLite backend)
- Build stores via backend factory
- Build shared stores via backend factory
- APScheduler daemon (tiers + daily SQLite prune)
- PdfStorage protocol; run_tailor storage-agnostic
- Hosted handler builds S3Storage (+ S3Storage unit tests)
- Local tailor routes + /tailored static serving
- Configurable Ollama host; keyless local connection
- Local-mode ops (skip CloudWatch) + backend-aware startup
- Shared app image (poller + web) + ignore hygiene
- Compose stack (poller + web + ollama) + .env.example
- One-shot DynamoDB -> SQLite migration
- Lambda pins dynamodb backend; web host configurable; README
- Merge branch 'feat/local-runtime'
- Full local setup guide with LLM provider choice; ollama opt-in via compose profile
- Merge branch 'docs/local-setup'
- Make JOB_AGG_OLLAMA_HOST .env-driven (enables Ollama Cloud locally)
- Merge branch 'feat/ollama-cloud-local'
- Spec for Workday completeness (pagination + detail-fetch enrich)
- Implementation plan for Workday completeness
- Paginate the jobs endpoint (drop the 20-role cap)
- Enrich() fetches the real JD; Enrichable connector protocol
- Enrich filter-survivors before scoring (Workday real JD)
- Merge branch 'feat/workday-completeness'
- Merge branch 'feat/workday-tenants-batch'
- Merge branch 'fix/workday-max-pages'

### Fixed

- New_count reflects diff size, not matched count
- Read SSM live via data source; make filter_role config-driven
- Expand non_us country list to ~70 countries
- Read posted_at from first_published, fall back to updated_at
- Capture Hiring.cafe SSR pageProps response
- Honor cfg.discovery.enabled gate
- Honor yc_oss_enabled flag + update validation_status docstring
- Require count>0 for ok verdict (was: any 200)
- Skip candidate when ANY ATS already in active_set (was: ALL)
- Soft-fail when profile.md unreadable + timeout test
- ASCII-safe ntfy Title + Discord embed truncation + 400 diagnostics
- Fold offices[] into location field
- Close precision leaks found in 12h alert audit
- Limit=20 (Workday rejects >=50 with HTTP 400)
- Honor Discord 429 retry_after to stop dropping notifications
- Bound fetch concurrency, drop failure tracebacks, slow ATS to 2m
- Raise Lambda memory 512->1024 to fit now-working connectors
- Base filter_done 'rejected' on filtered sample, not diff set
- Fail open on non-string LLM response content
- Apply sample cap AFTER the keyword filter, not before
- Raise Ollama num_predict so reasoning models can answer
- Honor explicit workplaceType in Ashby + Lever
- Lock null workplaceType -> isRemote fallback resolution
- Log exception type on LLM call failure
- Lock send_gap_digest HTTP behavior
- Tolerate blank min_score from the inbox form
- Render fetch failures by connector in the cycles panel
- Exclude no_match from the connector-health unhealthy table
- Cover fail-soft except branch + mixed batch
- Cover suppressed sub-section fail-soft; type-annotate health param
- Make tally_gaps top-cap test deterministic by count
- Cancel sibling-table margin inside .cols; assert s-vol bar renders
- Cover analytics empty-data and no-gaps rendered states
- Ship tailoring disabled in committed config (convention)
- Parser skips non-dict list items / non-dict fit (fail-open)
- Assert full render-input contract (skills_ordered, summary_placeholder)
- Parser never raises on malformed container types; summary always marker
- Construct Project with new shape in loader (keeps suite green)
- Force jinja autoescape; surface auto-trim count in CLI
- Lock cross-section bullet-id grounding (project vs experience)
- Update assemble skill test for the filter behavior (keeps suite green)
- Run_tailor never raises — render/S3 failures become an error dict
- Scope kms:Decrypt to alias/aws/ssm (match iam.tf least-privilege)
- Escape cover-letter + fit on the page (handles '<' in LLM output)
- Pin description_snapshot omitted when no posting / empty JD
- Single-manifest image build + public Function URL permission
- Chmod baked-in résumé data so the Lambda user can read it
- Assert board advance moves card into the target column
- Reject allowed-city matches that also carry a non_us signal
- Avoid executescript implicit-commit; cover :memory: schema + env path
- Set_status treats TTL-expired rows as not-found (matches DynamoDB)
- Backend-parametrized parity tests (anti-drift gate)
- Assert history + match shape in parity suite
- Register prune job with args= like the other jobs
- Read Ollama host at call time (remove reload-based test isolation hazard)
- Exclude entire infra/ from build context (keeps tfstate out)
- Serialize SqliteSeenJobsStore.set_status to avoid nested-transaction race
- Clearer enrich_capped log fields (enrichable + total_matched)
- Decode JSON inside the try so a bad later page returns partial
- Cap pagination at 5 pages (~100 roles) to keep ats cycle under timeout

<!--
Version links. 0.11.0 is this repository's initial public commit; releases
before it were cut in a private predecessor repo whose history is not
published, so compare links across those tags cannot resolve here. Their
entries below are kept for the record. From 0.12.0 on, the usual
compare/vP.R.E..vX.Y.Z links resume (see RELEASING.md).
-->

[0.13.0]: https://github.com/seancampbell3161/job-aggregator/compare/v0.12.0..v0.13.0
[0.12.0]: https://github.com/seancampbell3161/job-aggregator/compare/v0.11.0..v0.12.0
[0.11.0]: https://github.com/seancampbell3161/job-aggregator/releases/tag/v0.11.0

<!-- generated by git-cliff -->
