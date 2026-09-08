# Calibrating relevance scores after a model/provider change

`score_low` (in `config.yaml`) is the suppression cutoff: postings scored at or
below it are not notified (they're still recorded — suppression only affects the
alert). Different models produce different score distributions, so re-tune
`score_low` whenever you change `relevance.model` or `relevance.provider`.

This runbook also covers the **first switch to Ollama Cloud**: the code ships
with `provider: anthropic` still active, and you flip to `ollama` here, *after*
calibrating, as a deliberate separate deploy.

## 0. One-time: provision the Ollama API key

1. Create an Ollama Cloud (Pro) API key at `https://ollama.com/settings/keys`.
2. Store it in SSM (the Terraform `aws_ssm_parameter.ollama_api_key` resource
   holds a `REPLACE_ME_AT_DEPLOY` placeholder with `ignore_changes = [value]`,
   so this real value is never clobbered by `terraform apply`):

   ```
   aws ssm put-parameter --name /job-aggregator/ollama_api_key \
     --value "ol-..." --type SecureString --overwrite
   ```

   > **Gotcha:** if you `put-parameter` *before* the Terraform resource exists in
   > state, the next `terraform apply` errors with `ParameterAlreadyExists`.
   > Resolve with `terraform import aws_ssm_parameter.ollama_api_key /job-aggregator/ollama_api_key`.

## 1. Run a calibration pass

`--calibrate` scores a sample of currently-matched postings (the full keyword-
matched set, **bypassing** the new-since-last-seen diff, capped at 200) WITHOUT
suppression and WITHOUT notifying or marking any job seen. It implies
`--dry-run`. (Like `--dry-run`, it may still persist HTTP conditional-GET cache
hints to the `source_state` table — harmless; it never touches `seen_jobs` or
sends alerts.)

Calibration must run against the model you're evaluating, so point the config at
Ollama for the run. Two ways:

**A. Temporarily set the provider in `config.yaml`** (this is also the eventual
production change — see step 3):

```yaml
relevance:
  provider: ollama
  model: gpt-oss:120b
```

**B. Or use a throwaway config** without touching `config.yaml`:

```
cp config.yaml /tmp/calib.yaml   # then edit provider/model in /tmp/calib.yaml
export JOB_AGG_CONFIG_PATH=/tmp/calib.yaml
```

Then run (the ntfy/discord env vars are required by config loading but unused in
dry-run):

```
export JOB_AGG_OLLAMA_API_KEY=ol-...   # same key as SSM /job-aggregator/ollama_api_key
export JOB_AGG_NTFY_TOPIC_URL=x JOB_AGG_DISCORD_WEBHOOK_URL=x

.venv/bin/python -m src.handler --tier ats --calibrate 2>&1 | grep calibration_
```

(Run with `--tier slow` too if you want the slow-tier sources represented.)

## 2. Read the output

- `calibration_score` lines — one per posting: `score`, `rationale`, title,
  company. Eyeball whether the scores match your judgement of each posting.
- `calibration_summary` — the key line:
  - `histogram` — count per score 0–10.
  - `would_suppress_at` — for cutoffs `{3, 4, 5}`, how many postings each would
    suppress (scores `<= cutoff`).
  - `fallbacks` — postings the LLM failed to score (network/parse/timeout). A
    high fallback count means the model output isn't parsing — see Troubleshooting.
- `calibration_sample_capped` — appears only if more than 200 postings matched;
  the sample was truncated (not silent).

## 3. Pick `score_low` and flip to Ollama

Choose the cutoff in `would_suppress_at` that drops the postings you would *not*
pursue without dropping ones you would. Then make the production change in
`config.yaml`:

```yaml
relevance:
  enabled: true
  provider: ollama          # the flip
  model: gpt-oss:120b       # NOTE: gpt-oss:20b is currently broken on Cloud (empty content)
  score_high: 7
  score_low: <your tuned value>
  ...
```

Deploy (the Lambda bundles `config.yaml` at package time — there is no
hot-reload):

```
./scripts/package.sh && cd infra && terraform apply
```

Re-run `--calibrate` against production config after deploy to confirm the
distribution looks as expected.

## 4. Rollback

If `gpt-oss:120b` scores poorly, in increasing order of change:

1. **Try a different model** — set `model:` to another accessible Cloud model
   (one line), repackage + apply. (Avoid `gpt-oss:20b` — it returns empty content
   on Cloud today.)
2. **Revert the provider** — set `provider: anthropic` (or `gemini`), repackage +
   apply. The Anthropic and Gemini scorers remain fully wired; this is a one-line
   change with no code revert needed.

## Troubleshooting: high `fallbacks` count

The Ollama scorer relies on the model emitting a JSON object (Ollama Cloud does
not enforce a schema). If `fallbacks` is high:

- **Use `gpt-oss:120b`, not `gpt-oss:20b`.** On Ollama Cloud (2026-06), `gpt-oss:20b`
  generates tokens but returns **empty `content`** on every call (both the native
  and OpenAI endpoints) — so every score fails open. `gpt-oss:120b` works.
- **Reasoning needs token headroom.** gpt-oss reasons (into a `thinking` channel)
  before the answer; the scorer's `_OLLAMA_NUM_PREDICT` (`src/relevance.py`) is set
  to 1024 to leave room. If you switch to another verbose reasoning model and see
  empty-content fallbacks, raise it.
- Confirm the model tag is valid for your account; check the live list at
  `https://ollama.com/search?c=cloud`. Some models are gated to higher plan tiers
  (a 403 "this model requires a subscription, upgrade for access").
- Confirm `JOB_AGG_OLLAMA_API_KEY` is set and the plan quota isn't exhausted. A
  per-call cold-start can exceed the 10s `timeout_seconds`; bump it (e.g. to 30)
  in the config if early calls time out.
- As a further reliability lever, try adding `format="json"` to the `chat(...)`
  call in `OllamaRelevanceScorer.score` (`src/relevance.py`) and re-calibrate.
  Left off because Cloud's structured-output support was documented as
  self-hosted-only — **A/B it via calibration before trusting it:** if Cloud
  *rejects* `format`, every call fails open and all postings lose suppression.
