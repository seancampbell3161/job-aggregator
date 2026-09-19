# Résumé tailoring artifacts

Personal data — `content.json` and `evidence.json` are **gitignored**. You can
hand-write them from the committed examples / regen script, or, once a résumé
document is uploaded, draft `content.json` from it on the web app's
`/settings/documents/draft` page: it reads your résumé, shows every bullet as
editable text, and saves nothing until you confirm it. To import files from
disk instead, use `python -m src.settings import DIR` (they're read from
`DIR/resume/content.json` and `DIR/resume/evidence.json`); the app uses the
imported copies.

## Enable the feature
Tailoring ships **disabled** (`tailoring.enabled: false`). Once your real
`content.json` exists — hand-written, imported, or drafted — set
`tailoring.enabled: true` in `config.yaml` (or the equivalent settings page)
and it picks up live. `evidence.json` is optional; see below.

## `content.json` — structured résumé
Shape: see `content.example.json` (the authoritative schema is the dataclasses
in `src/tailor/models.py`). The top-level keys are `name`, `contact`, `skills`,
`experiences`, `projects` (note the plural `experiences`). Every item in
`experiences[]` and every bullet needs a **stable `id`** (the tailoring output
references selections by id) — the `/settings/documents/draft` page assigns
these for you. Tag bullets by stack/domain and flag `metric_bearing`.
`evidence_refs` link a bullet to Jira tickets in the evidence bank, if you keep
one.

## `evidence.json` — optional pre-digested Jira evidence bank
This file is **optional**, not just nominally so: with no evidence bank,
tailoring still runs, and the prompt drops its citation requirement for the
rule that it must never introduce a number that isn't already in the bullet
being rewritten. Keep the bank only if you want the model citing specific Jira
tickets for the metrics it claims.

Shape: `{ "projects": [ { "key", "summary", "metrics": [{claim, ticket_refs}],
"achievements": [{text, ticket_refs}] } ] }`. It is the citation layer — when
present, every metric the model claims must cite a `ticket_ref` from here.

Regenerate the skeleton (achievements grouped by epic) from a Jira CSV export:

    python -m scripts.build_evidence_bank path/to/jira-export.csv --out resume/evidence.json

The script mechanically turns tickets into `achievements`; curate `metrics`
(the numbers worth claiming) by hand afterwards.

## CLI
    JOB_AGG_ANTHROPIC_API_KEY=<key> \
        .venv/bin/python -m src.tailor --jd path/to/jd.txt --job-id some-id

Reads settings plus the `resume_content`/evidence documents from the app DB
(hand-written, imported, or drafted at `/settings/documents/draft` — see
above). Writes `tailored/<job-id>/{content.json,cover_letter.md,fit.md}`
(gitignored).

Tailoring runs on whichever provider `tailoring.provider` names — Anthropic,
Gemini, or Ollama — falling back to `relevance.provider`/`relevance.model`
when unset (see `tailoring.*` in [`docs/CONFIG.md`](../docs/CONFIG.md)). Set
the matching key as an env var for the CLI: `JOB_AGG_ANTHROPIC_API_KEY`,
`JOB_AGG_GOOGLE_API_KEY` (Gemini), or `JOB_AGG_OLLAMA_API_KEY` (Ollama).

For Ollama specifically: `relevance.ollama_host` defaults to
`http://ollama:11434`, which only resolves inside Docker Compose, so for
hosted Ollama Cloud either set `relevance.ollama_host: https://ollama.com` in
`config.yaml` and import, or export `JOB_AGG_OLLAMA_HOST=https://ollama.com`
for the command.

## Rendering a PDF
The CLI also writes a single-page `tailored/<job-id>/resume.pdf` that reproduces
the classic template's design (teal section titles, Gelasio serif, one US
Letter page) with the experience bullets tailored to the posting and skills
reordered for it. Requires the render extra:

    pip install -e '.[render]'        # WeasyPrint + Jinja2

WeasyPrint ships a self-contained wheel on macOS arm64; on other platforms you
may need its system libs (`brew install pango`, or the distro equivalent). The
Gelasio font is vendored under `resume/fonts/` (OFL), so renders are
deterministic and need no network. Pass `--no-pdf` to skip the PDF (e.g. when
WeasyPrint isn't installed) — the JSON/Markdown outputs are still written. If the
tailored content overflows one page, trailing project bullets (then experience
bullets) are auto-trimmed to fit, and the trim is reported.

## Résumé templates (/builder)

Templates are **packs**: a directory with `template.html.j2`, optional `fonts/`
(.ttf/.otf referenced as `url('fonts/X.ttf')`), and optional `meta.yaml`
(`name`, `description`). Built-ins (`classic`, `headless`) ship in the image;
your uploads live in the templates directory (`/data/templates` in Docker;
`import` copies packs from `DIR/resume/templates/`). Manage
everything on the **/builder** page: upload (`.html`/`.j2`, `.zip` pack, or
`.docx` — imported via one LLM call and held as *pending* until you accept the
preview), preview, switch the active template, and edit builder settings
(bullet caps, min bullets, max pages, page size/margins — stored in SQLite,
no restart). The /tailor page can re-render any stored run through a different
template without re-calling the LLM; the CLI takes `--template <slug>`.

### Template contract

The template renders with one variable, `doc` (autoescaped, sandboxed):

| Field | Shape |
|---|---|
| `doc.name` | str |
| `doc.contact_items` | list[str], pre-ordered phone/email/website/github |
| `doc.skill_rows` | list of rows: `.label` str, `.tokens` list[str] |
| `doc.experiences` | `.company` `.role` `.dates` strs, `.bullets` list[str] |
| `doc.projects` | `.name` `.subtitle` `.dates` strs, `.bullets` list[str] |
| `doc.education` | `.degree` `.institution` `.dates` strs |
| `doc.volunteer` | `.role` `.org` `.dates` strs |

Include an `@page` rule; the user's page-size/margin settings override it.
Render only the sections you want — anything omitted simply doesn't appear.
