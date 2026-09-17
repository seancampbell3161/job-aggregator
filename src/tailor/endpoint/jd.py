"""The job-description view the tailor flow consumes.

SqliteSeenJobsStore.get_jd builds a PostingJD from the description_snapshot
written at notify time and sanitizes it on read: the snapshot is
attacker-controlled text on its way into the tailor prompt — the
highest-severity LLM surface here, since injected instructions could steer
résumé bullets into claims that go out under the user's name. Reading through
the sanitizer covers rows written before the filter existed."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PostingJD:
    description: str
    title: str
    company: str
