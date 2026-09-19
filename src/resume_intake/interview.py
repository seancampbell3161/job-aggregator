"""The six things a résumé cannot tell you.

A résumé is a record of what someone did. profile.example.md's shape — Strong
fit / Mild fit / Weak fit, comp target, IC-vs-management, geography — is
entirely about what they want next. Those cannot be inferred from work history
at any quality, so they are asked. Everything a résumé DOES say (stack, years,
domain) is read off the document instead of asked, which is what keeps this
form to one screen.

Every field but one binds to a real dotted config path, so an answer becomes a
filter directly rather than passing through the LLM's judgement."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.config import EMPLOYMENT_TYPES

KIND_CHIPS, KIND_CHOICE, KIND_MULTI, KIND_INT = "chips", "choice", "multi", "int"


@dataclass(frozen=True)
class InterviewField:
    name: str
    label: str
    kind: str
    path: str | None
    choices: tuple[str, ...] = ()
    help: str = ""


INTERVIEW_FIELDS: tuple[InterviewField, ...] = (
    InterviewField(
        "target_titles", "What job titles are you looking for?", KIND_CHIPS,
        "filters.titles",
        help="Plain words, not patterns. Add each variant you'd accept — "
             "nothing matches a title you didn't list.",
    ),
    InterviewField(
        "seniority", "What level?", KIND_MULTI, "filters.seniority_allow",
        choices=("junior", "mid", "senior", "staff"),
    ),
    InterviewField(
        "ic_or_management", "Hands-on or managing?", KIND_CHOICE, None,
        choices=("ic", "management", "either"),
        help="There is no filter for this — it shapes how your profile reads "
             "to the scorer.",
    ),
    InterviewField(
        "countries", "Which countries?", KIND_CHIPS,
        "filters.location.allowed_countries",
        help="Two-letter codes, e.g. US, GB, DE.",
    ),
    InterviewField(
        "cities", "Any specific cities? (optional)", KIND_CHIPS,
        "filters.location.allowed_cities",
    ),
    InterviewField(
        "remote_policy", "Remote roles anywhere, or only in those countries?",
        KIND_CHOICE, "filters.location.remote_policy",
        choices=("allowed_countries", "anywhere"),
    ),
    InterviewField(
        "employment_types", "What kinds of employment would you take?",
        KIND_MULTI, "filters.blocked_employment_types",
        choices=EMPLOYMENT_TYPES,
        help="Anything you don't tick is filtered out.",
    ),
    InterviewField(
        "comp_floor", "Minimum base salary, in USD (optional)", KIND_INT,
        "filters.comp_floor_usd",
    ),
)

_BY_NAME = {f.name: f for f in INTERVIEW_FIELDS}


def decode_answers(form: Mapping[str, Any]) -> dict[str, list[str]]:
    """Raw form -> {field name: [values]}, dropping blanks. Stored verbatim in
    wizard_ui so the review step can re-render the form if drafting fails."""
    out: dict[str, list[str]] = {}
    for field in INTERVIEW_FIELDS:
        raw = form.get(field.name, [])
        values = [raw] if isinstance(raw, str) else list(raw)
        values = [v.strip() for v in values if isinstance(v, str) and v.strip()]
        if values:
            out[field.name] = values
    return out


def answers_to_patch(answers: Mapping[str, list[str]]) -> dict[str, Any]:
    """Answers -> a dotted-path settings patch. Only filter paths, and only
    for answers actually given."""
    patch: dict[str, Any] = {}
    for field in INTERVIEW_FIELDS:
        if field.path is None:
            continue
        values = answers.get(field.name)
        if not values:
            continue
        if field.name == "employment_types":
            # The form asks what you WANT; the config stores what is BLOCKED.
            wanted = {v for v in values if v in EMPLOYMENT_TYPES}
            patch[field.path] = [t for t in EMPLOYMENT_TYPES if t not in wanted]
        elif field.kind == KIND_INT:
            try:
                patch[field.path] = int(values[0])
            except ValueError:
                continue  # a re-rendered form shows the error; never guess
        elif field.kind == KIND_CHOICE:
            patch[field.path] = values[0]
        else:
            patch[field.path] = values
    return patch
