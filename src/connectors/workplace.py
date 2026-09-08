"""Map an ATS 'workplace type' enum to a remote flag.

Several ATSs expose an explicit workplace type (Ashby: ``Remote``/``Hybrid``/
``OnSite``; Lever: ``remote``/``hybrid``/``onsite``) alongside a separate,
less-reliable remote flag. Companies routinely leave that flag set to true on
hybrid/onsite roles (observed: 69/70 of one Ashby board were ``isRemote: true``
+ ``workplaceType: Hybrid``), so when an explicit workplace type is present it
must win — otherwise hybrid/onsite roles get mislabeled remote and sail past the
location filter.
"""

from __future__ import annotations


def remote_from_workplace_type(
    workplace_type: str | None, *, fallback: bool | None = None
) -> bool | None:
    """Return a remote flag for an ATS workplace-type string (case- and
    separator-insensitive). ``Remote`` → True; ``Hybrid``/``OnSite`` → False
    (overriding any separate remote flag). Absent or unrecognized (e.g. Lever's
    ``"unspecified"``) → ``fallback``."""
    if workplace_type:
        wt = workplace_type.strip().lower().replace("-", "").replace(" ", "")
        if wt == "remote":
            return True
        if wt in ("hybrid", "onsite"):
            return False
    return fallback


def annotate_location(location: str | None, workplace_type: str | None) -> str | None:
    """Fold an explicit Hybrid/OnSite workplace type into the location text so the
    normalizer can tag it accurately. A bare city ("San Francisco") otherwise
    defaults to ``onsite_only``, erasing the hybrid distinction. Remote is carried
    by the remote flag, so it isn't annotated here. No-op if the workplace type is
    absent, unrecognized, or already present in the text."""
    if not workplace_type:
        return location
    wt = workplace_type.strip().lower().replace("-", "").replace(" ", "")
    label = {"hybrid": "Hybrid", "onsite": "Onsite"}.get(wt)
    if label is None:
        return location
    if not location:
        return label
    if label.lower() in location.lower():
        return location
    return f"{location} ({label})"
