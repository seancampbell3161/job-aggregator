# Recon — Greenhouse application form (reference, not run in CI)

Captured 2026-07-03 from a live modern Greenhouse board
(`job-boards.greenhouse.io/anthropic/jobs/…`). Reference for the matcher's
label-association + React-setter assumptions.

## Load-bearing finding: React-controlled, `_valueTracker` present
The email input carries `__reactFiber$…`, `__reactProps$…`, and a
`_valueTracker`. A plain `el.value = …` is reverted by React's tracker on the
next render / at submit → **the native property setter + dispatched
`input`/`change` events is required** (matcher `nativeSet`). Confirmed necessary,
not optional.

## Label association (matcher `labelText` handles all three)
- `<label for="{id}">` → text like `First Name*`, `Email*` (the primary signal).
- `aria-label` present and clean (`First Name`, `Email`, `Phone`).
- Semantic `id`: `first_name`, `last_name`, `email`, `phone`, `country`.
So matching on label-for + aria-label + id/name/placeholder tokens works well on
Greenhouse.

## Fields observed on this posting
`first_name`, `last_name`, `email`, `country` (+ an intl-tel `search` helper
input to skip), `phone` (type=tel), plus a custom text question. This particular
(Fellows) posting had **no** LinkedIn/GitHub/website inputs, **no** `<select>`,
**no** EEO/demographic radios — minimal custom set. The page text did contain
"sponsor"/"authoriz", so eligibility questions appear on fuller postings.

## Standard Greenhouse fields (from general knowledge, to validate at live-verify)
Fuller Greenhouse forms add: `LinkedIn Profile`, `Website` (text inputs, easy
Tier-1), custom eligibility as text or `<select>` (work auth / sponsorship), and
a **Demographic Questions** section rendered as `<select>` dropdowns
(Gender/Race/Veteran/Disability) whose option text often matches the facts.yaml
values verbatim → Tier-2 option matching.

## Lever / Ashby
Not live-reconned this session (avoiding a rabbit hole; the matcher is
ATS-agnostic label-matching). To be validated at the BM3 live-verification gate
on real Lever/Ashby forms — tune `SYN`/threshold there if a field is missed.
