"""The board vocabulary: what a configured source is called, and how it reads.

A board has two identities, and conflating them is the mistake this module
exists to prevent. `board_key` is the POLLER's name for it — the same string
build_connectors gives the connector, connector_health suppresses, and
discovered_slugs stores. The row digest in src/settings/rows.py is the UI's
address for it. The key is for joining; the digest is for addressing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from src.config import SLUG_SOURCE_FAMILIES, AppConfig
from src.settings.rows import list_rows

STRUCTURED_FAMILIES: tuple[str, ...] = (
    "workday", "oraclecloud", "eightfold", "jsonld_boards", "phenom", "taleo", "avature",
)
BOARD_FAMILIES: tuple[str, ...] = (*SLUG_SOURCE_FAMILIES, *STRUCTURED_FAMILIES)


def _host_slug(careers_url: str) -> str:
    """PhenomConnector's naming rule (src/connectors/phenom.py::_slug)."""
    host = (urlparse(careers_url).netloc or careers_url).lower()
    for prefix in ("careers.", "jobs.", "www."):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return host.replace(".", "-")


def _tenant_label(careers_url: str) -> str:
    """AvatureConnector's naming rule (src/connectors/avature.py::_tenant_from_url)."""
    host = (urlparse(careers_url).netloc or "").lower()
    labels = [l for l in host.split(".") if l and l not in ("careers", "www", "jobs")]
    return labels[0] if labels else host or "unknown"


def board_key(family: str, entry: Any) -> str:
    """The connector name for one configured source entry.

    A bare string entry is a slug family. Everything else is a mapping in the
    shape its model stores."""
    if isinstance(entry, str):
        return f"{family}:{entry}"
    if family in ("workday", "oraclecloud"):
        return f"{family}:{entry['tenant']}:{entry['site']}"
    if family == "taleo":
        return f"taleo:{entry['tenant']}:{entry['section']}"
    if family == "jsonld_boards":
        return f"{entry['family']}:{entry['slug']}"
    if family == "eightfold":
        return f"eightfold:{entry['slug']}"
    if family == "phenom":
        return f"phenom:{_host_slug(entry['careers_url'])}"
    if family == "avature":
        return f"avature:{_tenant_label(entry['careers_url'])}"
    raise ValueError(f"unknown source family {family!r}")


def board_label(family: str, entry: Any) -> str:
    """What to call this board on screen: its company name when it has one,
    else the most identifying part of its address."""
    if isinstance(entry, str):
        return entry
    company = entry.get("company")
    if company:
        return str(company)
    for key in ("tenant", "slug"):
        if entry.get(key):
            return str(entry[key])
    url = entry.get("careers_url")
    return _host_slug(url) if url else board_key(family, entry)


def board_summary(family: str, entry: Any) -> str:
    """The identity in one short line, for the row's second column."""
    if isinstance(entry, str):
        return entry
    if family in ("workday", "oraclecloud"):
        return f"{entry['tenant']} · {entry['region']} · {entry['site']}"
    if family == "taleo":
        return f"{entry['tenant']} · {entry['section']}"
    if family == "jsonld_boards":
        return f"{entry['family']} · {entry['base_url']}"
    if family == "eightfold":
        return f"{entry['slug']} · {entry['domain']} · {entry.get('flavor', 'pcsx')}"
    return str(entry.get("careers_url", ""))


@dataclass(frozen=True)
class BoardEntry:
    family: str
    key: str            # connector name — joins to the runtime stores
    label: str
    summary: str
    digest: str         # row address — for the edit/remove routes
    values: dict[str, Any]
    path: str           # the dotted settings path holding this family

    @property
    def structured(self) -> bool:
        return self.family in STRUCTURED_FAMILIES


def board_entries(cfg: AppConfig) -> list[BoardEntry]:
    """Every configured board, families in BOARD_FAMILIES order.

    Slug families and structured families both come from rows.list_rows — a
    slug family is a list of one-field rows — so there is one code path here,
    and the Companies page can edit or remove either kind identically."""
    out: list[BoardEntry] = []
    for family in BOARD_FAMILIES:
        path = f"sources.{family}"
        for row in list_rows(cfg, path):
            out.append(BoardEntry(
                family=family,
                key=board_key(family, row.entry),
                label=board_label(family, row.entry),
                summary=board_summary(family, row.entry),
                digest=row.digest,
                values=row.values,
                path=path,
            ))
    return out
