# Security Policy

## What this project is

A self-hosted, single-user tool. You run it on hardware you control, against
your own accounts, with your own API keys. There is no hosted service, no
multi-tenancy, and no server operated by the maintainer — so the threat model
is "what can a hostile job posting, a hostile template, or someone on your
network do to *your* box," not "what can one user do to another."

## Not a vulnerability: the web UI has no authentication

This is a deliberate design decision, documented in the README, and reports
about it will be closed as such.

The web UI (`/`, `/board`, `/audit`, `/pipeline`, `/analytics`, `/kit`,
`/tailor`, `/builder`, `/coach`) has **no login, no session, and no access
control**. It is intended
to be reachable only from a network you trust. The Docker `web` service binds
`0.0.0.0` and publishes `8000:8000`, so on a shared or public network it is
reachable by anyone who can route to the host.

Deploy it behind a private overlay (Tailscale or similar), or restrict it to
loopback by publishing `127.0.0.1:8000:8000` in `docker-compose.yml`. Do not
port-forward it to the internet.

**What an attacker gets if you do expose it:** your résumé and every tailored
variant, your full application history and statuses, your apply-kit answers
(work authorization, personal links, EEO responses), any Gmail-derived
rejection/receipt data, and your configured job preferences.

It is also **read-write, not just readable**: fourteen `POST` endpoints are
exposed, so an attacker can add and advance board entries, rescue or confirm
audit verdicts, change builder settings, and upload, activate or delete résumé
template packs. `/coach/run` triggers LLM calls, so they can also spend your
API credits.

## In scope

Reports that a specific mechanism fails to do what it claims:

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

- The missing web-UI authentication (above).
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
`resume/content.json` and everything under `data/` hold personal information and
are gitignored for that reason. Keep them that way. If you fork this and commit
your own config, that is your disclosure, not a vulnerability in the project.
