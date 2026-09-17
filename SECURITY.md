# Security Policy

## What this project is

A self-hosted, single-user tool. You run it on hardware you control, against
your own accounts, with your own API keys. There is no hosted service, no
multi-tenancy, and no server operated by the maintainer — so the threat model
is "what can a hostile job posting, a hostile template, or someone on your
network do to *your* box," not "what can one user do to another."

## The web UI requires a login

One admin password, hashed with argon2id, protects every page. Whoever opens
the UI first — at `/welcome`, or in advance via
`python -m src.settings set-password` — sets it. A successful sign-in issues a
server-side session in an `HttpOnly`, `SameSite=lax` `jobagg_session` cookie
that stays valid for 30 days of use and slides forward on every request, so it
only expires from inactivity. State-changing requests (`POST`/`PUT`/`PATCH`/
`DELETE`) are separately checked for cross-origin `Origin`/`Host` mismatches,
regardless of session state. `/tailor` and `/tailor/pdf` — the phone-tappable
deep links in an alert — skip the session entirely: they carry their own
HMAC-signed, per-job token instead, since a phone tapping a link from a
notification has no cookie to send.

This is a single admin login, not multi-user access control, and it makes no
claim about the network it runs on — both are deliberate, documented design
decisions, and reports about them will be closed as such:

- **No accounts, roles, or per-user isolation.** One password is the whole
  model; whoever holds it, or a valid session cookie, is the admin. There is
  nothing to isolate one user's data from another's, because there is only
  one user.
- **No TLS.** The Docker `web` service binds `0.0.0.0` and serves plain HTTP
  on `8000`, so the password (at sign-in) and everything the UI shows cross
  the network in cleartext. Deploy it behind a private overlay (Tailscale or
  similar), put an HTTPS reverse proxy in front, or restrict it to loopback by
  publishing `127.0.0.1:8000:8000` in `docker-compose.yml`. Do not
  port-forward it to the internet.

**What an attacker who obtains the password, or a valid session cookie, gets:**
your résumé and every tailored variant, your full application history and
statuses, your apply-kit answers (work authorization, personal links, EEO
responses), any Gmail-derived rejection/receipt data, and your configured job
preferences.

It is also **read-write, not just readable**: they can add and advance board
entries, rescue or confirm audit verdicts, change builder settings, upload,
activate or delete résumé template packs, and change the password or sign
every other device out. `/coach/run` triggers LLM calls, so they can also
spend your API credits.

## In scope

Reports that a specific mechanism fails to do what it claims:

- **Bypassing the login gate.** Every page outside `/static/*`, `/login`,
  `/welcome`, `/logout`, `/tailor`, and `/tailor/pdf` requires a valid session
  (`src/web/auth.py`). Reaching any other page without one is in scope.
- **Session forgery or fixation.** Session tokens are
  `secrets.token_urlsafe(32)`, stored server-side only as their SHA-256 hash
  (`src/auth/service.py`). Forging a valid `jobagg_session` cookie, fixating a
  victim's session, or extending one past its 30-day idle window is in scope.
- **Cross-origin protection bypass.** `POST`/`PUT`/`PATCH`/`DELETE` requests
  are checked against `Sec-Fetch-Site` and `Origin`/`Host`. Getting a
  state-changing request accepted from a different origin is in scope.
- **Prompt injection reaching an LLM.** Job descriptions are attacker-controlled
  and reach a model three ways (relevance scorer, tailor endpoint, `/audit`
  rescue scoring). `src/sanitize.py` defangs known phrasings and
  `wrap_untrusted` fences the text; the design assumes novel payloads get
  through the filter and relies on the fence. A payload that escapes the fence,
  or a path that reaches a model *without* fencing, is in scope. See
  `tests/test_injection_hardening.py`.
- **Path traversal in template upload.** `.docx`/`.zip` template packs are
  attacker-supplied archives; entries are meant to be confined to the pack
  directory. An escape is in scope.
- **Tailor deep-link token forgery.** `src/tailor/endpoint/auth.py` signs
  `job_id|exp` with HMAC-SHA256 and verifies with `compare_digest`. A forgery,
  a replay past `exp`, or a bypass is in scope.
- **Secret disclosure.** API keys, webhook URLs and the signing secret come from
  the environment and must never reach logs, notification payloads, rendered
  pages, or the audit trail.
- **Server-side request forgery** beyond what a configured board URL implies.
- Anything that lets a **remote posting or template achieve code execution**.

## Out of scope

- The absence of built-in TLS, and the single-admin login having no accounts,
  roles, or per-user isolation (above) — both are design choices, not
  vulnerabilities.
- Consequences of exposing the UI or the `/data` directory to an untrusted
  network.
- Rate limits, blocks or bans imposed by a job board you configured. The project
  identifies itself honestly by default (see `http.user_agent` in
  `docs/CONFIG.md`); if you override that, the consequences are yours.
- Vulnerabilities in a third-party ATS or job board.
- Anything requiring an attacker who already has shell access or your `.env`.

## Reporting

Use GitHub's **private vulnerability reporting** on this repository:
**Security → Report a vulnerability**. That keeps the report private until a fix
exists. Please do not open a public issue for something exploitable.

Include what you need to reproduce it: version or commit, config shape (redact
your own data), and the smallest input that triggers it.

This is a personal project maintained by one person in their spare time.
Expect an acknowledgement within about a week, and please don't take silence as
dismissal — ping the thread. There is no bug bounty.

## Supported versions

The latest release only. Fixes go into a new release; there are no backports.

## Handling your own data

`config.yaml`, `profile.md`, `resume.md`, `resume/facts.yaml`,
`resume/content.json`, and everything under `data/` hold personal information
and are gitignored for that reason — `data/job_aggregator.db` now also holds
your imported settings, documents, and any secrets stored with `set-secret`,
so protect it like `.env`. Keep them out of git. If you fork this and commit
your own config, that is your disclosure, not a vulnerability in the project.
