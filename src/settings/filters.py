"""The hand-edited filter paths: the hard gates every posting passes.

One list, two readers. The Filters settings page lays these out, in this
order; the résumé draft (src/resume_intake/draft.py) may propose values for
exactly these and nothing else, because its review form is that page. Keeping
the list here rather than in the web section registry is what lets the draft
depend on it without depending on the web layer."""
from __future__ import annotations

FILTER_PATHS: tuple[str, ...] = (
    "filters.titles",
    "filters.seniority_allow",
    "filters.stack_any_of",
    "filters.comp_floor_usd",
    "filters.max_age_days",
    "filters.blocked_employment_types",
    "filters.blocked_companies",
    "filters.location.allowed_countries",
    "filters.location.allowed_cities",
    "filters.location.remote_policy",
    "filters.location.allow_unknown",
)
