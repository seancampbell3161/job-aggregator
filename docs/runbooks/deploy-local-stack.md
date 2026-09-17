# Upgrading a running local stack

The supported deployment is **Docker Compose + SQLite on a dedicated always-on
box**. An upgrade = pull code + rebuild the image. This runbook covers the core
upgrade plus turning on the opt-in features afterwards. Run every command **on
the box that runs the stack**, in the repo directory.

## What a rebuild ships — and what it never touches

- `git pull` updates **tracked** files: `src/`, `Dockerfile`, docs.
- `docker compose up -d --build` rebuilds the image, baking in the new `src/`,
  **Chromium** (headless tier), and the **enterprise seed CSV** (board discovery).
- **Local-only, never touched by pull/build:** `data/` (the SQLite DB —
  **settings, documents, stored secrets**, and all job state), `.env`, and
  your gitignored settings files (`config.yaml`, `profile.md`, `resume.md`,
  `resume/*`). Since the settings-database release those files are an import
  format: edit them, then import (see Phase 1b).
- SQLite migrations run **automatically** on container start and are additive
  (new tables/columns).

## Phase 0 — Pre-flight

```bash
cd <repo-dir>
docker compose ps                            # what's currently up
git fetch origin
git status -sb                               # ⚠️ KEY — see below
git log --oneline -1                         # currently deployed commit
cp -r ./data "./data.bak-$(date +%Y%m%d)"    # back up the DB before migrations
```

The critical line is **`git status`**. If it's clean (or only shows the usual
untracked local files), continue.

## Phase 1 — Core deploy

```bash
git pull --ff-only origin main
docker compose up -d --build                 # rebuild image, recreate poller + web
```

The first build is slow (WeasyPrint natives + Playwright/Chromium); later builds
cache. `web` and `poller` are the only services unless you run local Ollama
(`--profile ollama`); with a cloud LLM provider, plain `up -d` is correct.

## Phase 1b — Settings import (first upgrade to the settings database, and after any edit)

```bash
docker compose run --rm -v "$PWD:/import:ro" web python -m src.settings import /import
# a differently named config: add --config /import/config.friend.yaml
docker compose run --rm web python -m src.settings import-env-secrets   # optional: then secrets can leave .env
docker compose run --rm web python -m src.settings status
```

Import validates everything before writing; a failure changes nothing. Changes
apply live — no restart. Import replaces the settings document but only adds
documents: a file missing from the directory leaves that document as it was.

`add-source`, `restore`, and the host-side `--merge` / `seed_companies.py` scripts
change settings in the database only, so your files fall behind. Before the next
edit, export and bring those changes into your files, then import:

```bash
docker compose run --rm web python -m src.settings export /data/export   # lands in ./data/export/
```

Until then an import refuses, listing the settings versions it would replace
(nothing is written); `import --force` overwrites them on purpose.

## Phase 2 — Verify core health

```bash
docker compose ps                            # poller + web both "Up"
docker compose logs --since 5m poller | grep -E "invocation_done|error|Traceback" | tail -20
docker compose run --rm web python -m src.settings history --limit 3   # newest version marked *
```

- Both containers `Up`; the `ats` cycle logs `invocation_done` within ~10 min.
- Open `http://<box>:8000/pipeline` — health strip green, no degraded flag.

> **Expect a transient `ats stalled` flag on `/pipeline` right after the deploy —
> it is not a break.** The scheduler's interval triggers do **not** fire on boot,
> so the first new `ats` cycle runs up to `ats_minutes` (10) later; until then the
> newest recorded cycle is the pre-rebuild one, and the freshness heartbeat flags
> the gap. It clears on its own once the first post-deploy cycle logs
> `invocation_done` with `failed_sources: []`:
>
> ```bash
> docker compose logs --since 20m poller | grep -E "invocation_done|Traceback" | tail -5
> ```

Kick the long-interval tiers so you don't wait 24 h / 45 min to confirm them
(discovery emits no alerts; the headless check writes nothing):

```bash
# board discovery — writes discovered_boards, no notifications (else waits 24h):
docker compose exec poller python -m src.handler --tier discovery 2>&1 | grep board_discovery_done
# expect: board_discovery_done {"swept": N, "matched": M, ...}  with M >= 0
```

If `swept` is `0`, the seed CSV didn't make it into the image — the rebuild
didn't take; re-run `docker compose up -d --build`.

## Phase 3 — Apply kit (`facts.yaml`)

Create `resume/facts.yaml` from the example, then re-import (Phase 1b) —
`/kit` reflects it on the next refresh:

```bash
cp resume/facts.example.yaml resume/facts.yaml
$EDITOR resume/facts.yaml                     # your real answers
```

Verify: open `/kit`, confirm your groups render with copy buttons and
the **"📋 Apply Autofill"** bookmarklet, and drag the bookmarklet to your
bookmarks bar. (`facts.yaml` is gitignored — stays local.)

## Phase 3b — Tailor artifacts (`content.json` / `evidence.json`)

Whenever the résumé content bank changes on your dev machine, copy the
gitignored artifacts over and re-import (like `facts.yaml`, Phase 1b):

```bash
scp resume/content.json resume/evidence.json <stack-box>:~/job-aggregator/resume/
docker compose run --rm -v "$PWD:/import:ro" web python -m src.settings import /import   # applies live; no restart
```

Verify: open a `/tailor` deep link (or run the tailor CLI on the box) and
confirm the tailored PDF reflects the new bullets.

## Phase 4 — Gmail ingestion

> **Prerequisite — Google-hosted mailbox + an app password only.** The connector
> speaks app-password IMAP to `imap.gmail.com` (hard-coded — no OAuth, no other
> providers). Custom-domain / Google Workspace accounts frequently **disable app
> passwords by admin policy**, and personal accounts on Advanced Protection block
> them too. If you can't create an app password, this feature can't authenticate —
> skip it (it's opt-in; nothing depends on it) and leave the two vars empty so the
> hourly job cleanly skips.

1. Google Account → Security → 2-Step Verification → **App passwords** → generate
   one for "Mail".
2. Store the credentials — no-recreate option:
   ```bash
   docker compose run --rm -it web python -m src.settings set-secret gmail_app_password
   docker compose run --rm -it web python -m src.settings set-secret gmail_address
   ```
   or append to `.env` on the box (it's 16 chars — **strip the spaces** Google shows):
   ```
   JOB_AGG_GMAIL_ADDRESS=you@gmail.com
   JOB_AGG_GMAIL_APP_PASSWORD=<16-char app password>
   ```
3. If you used `.env`, recreate so containers snapshot it (Compose reads it
   only at container creation):
   ```bash
   docker compose up -d --force-recreate
   ```
4. **Verify immediately** — run one real sweep now instead of waiting for the
   top-of-hour cron (a bad password *raises* here, rather than failing silently):
   ```bash
   docker compose exec poller python -c "from src.settings import open_service; from src.stores import build_stores; from src.gmail_ingest import run_gmail_check; cfg=open_service().snapshot().cfg; s=build_stores(); print('GMAIL', run_gmail_check(s.seen, s.source_state, address=cfg.secrets.gmail_address, app_password=cfg.secrets.gmail_app_password, first_run_days=cfg.gmail.first_run_days, lookback_max_days=cfg.gmail.lookback_max_days, max_messages=cfg.gmail.max_messages_per_run))"
   ```
   - ✅ `GMAIL {'fetched': N, 'matched': M, 'suggested': K, ...}` → working; badges land on `/board`.
   - ❌ `imaplib.IMAP4.error: [AUTHENTICATIONFAILED]` → bad/blocked credentials.
     `python -m src.settings status` shows whether each Gmail secret comes from
     `env` or `stored`. If you used `.env`, diagnose **without echoing the
     secret**:
     ```bash
     docker compose exec poller python -c "import os; a=os.environ.get('JOB_AGG_GMAIL_ADDRESS',''); p=os.environ.get('JOB_AGG_GMAIL_APP_PASSWORD',''); print('address=', repr(a), '| password_len=', len(p), '| has_space=', ' ' in p)"
     ```
     `password_len= 0` → the `force-recreate` didn't pick up `.env`. `19` / `has_space= True` → strip the spaces. `16` but still failing → the account can't use app passwords (see prerequisite); skip the feature.

The mailbox is read-only; bodies are never stored. Tunables live under `gmail:`
(`check_cron` default hourly, `first_run_days`, `lookback_max_days`).

## Phase 5 — Avature (headless), one tenant at a time

Avature boards are branded and often WAF-gated, so verify each one passes a real
browser **before** committing it. For each candidate (you supply its `SearchJobs`
URL):

1. Add it to `config.yaml` under `sources.avature`, then re-import (Phase 1b):
   ```yaml
   sources:
     avature:
     - {careers_url: "https://careers.jacobs.com/en_US/careers/SearchJobs", company: "Jacobs"}
   ```
2. Confirm the board parsed, then run a no-side-effect headless
   check. **Read `diff_done`, not `would_notify`** — the dry-run's `invocation_done`
   is terse, and `would_notify` only fires if a job *also* passes your filters:
   ```bash
   docker compose run --rm -v "$PWD:/import:ro" web python -m src.settings import /import
   # (a) did the import pick up the board(s)?
   docker compose exec poller python -c "from src.settings import open_service; print([b.company for b in open_service().snapshot().cfg.sources.avature])"
   # (b) does Chromium render it and pull jobs?
   docker compose exec poller python -m src.handler --tier headless --dry-run 2>&1 \
     | grep -iE "invocation_start|diff_done|score_done|Traceback|Error"
   ```
   - `invocation_start ... "sources": ["avature:<co>"]` + `diff_done ... "new": N` with **N > 0** → Chromium rendered the board and pulled N jobs; **keep it**.
   - `scored: 0` / no `would_notify` is **normal** — it just means none of those jobs cleared your title/location/stack filters this pass, *not* that the board failed.
   - A board missing from `sources`, `diff_done new: 0`, or a browser `Traceback`/timeout → that tenant is WAF-gated under automation; **remove it**.
3. Confirmed boards then poll every 45 min on the `headless` tier automatically.

Edit `sources.avature` in `config.yaml` on the box and re-import; nothing is
committed upstream.

## Rollback

```bash
git checkout <previous-commit> && docker compose up -d --build
# only if a migration misbehaved (rare — migrations are additive):
#   docker compose down && cp -r ./data.bak-<date>/. ./data/ && docker compose up -d
```

Older code tolerates newer additive tables/columns, so the DB backup is
belt-and-suspenders rather than usually necessary.

Releases before the settings database ignore the new tables and read
`config.yaml`/`profile.md` from disk again, so rolling back needs no data
changes — restore the pre-upgrade compose bind mounts by checking out the old
commit.
