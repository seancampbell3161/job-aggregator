"""Gmail ingestion: read-only IMAP sweep that matches recent mail to active
board cards and classifies rejections/receipts by phrase rules. Suggest-only:
hits write an email_suggestion badge; the user confirms or dismisses on
/board."""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import re
import ssl
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr

from src.models import ConnectorState

log = logging.getLogger(__name__)

# Case-insensitive phrase rules — data, not code; extending is a one-line edit.
REJECTION_PHRASES: tuple[str, ...] = (
    "unfortunately",
    "we will not be moving forward",
    "not moving forward with your application",
    "decided to pursue other candidates",
    "decided to move forward with other candidates",
    "position has been filled",
    "no longer under consideration",
    "unable to offer you a position",
)
RECEIPT_PHRASES: tuple[str, ...] = (
    "thank you for applying",
    "thanks for applying",
    "application has been received",
    "we received your application",
    "your application was sent",
    "successfully submitted your application",
)
# Sender domains of ATSs we poll — matched by suffix (covers subdomains like
# us.greenhouse-mail.io).
ATS_DOMAINS: tuple[str, ...] = (
    "greenhouse-mail.io",
    "greenhouse.io",
    "lever.co",
    "myworkday.com",
    "myworkdayjobs.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "icims.com",
    "oraclecloud.com",
)
_STOP_TOKENS = frozenset(
    {"inc", "llc", "corp", "co", "the", "company", "group", "holdings", "ltd"}
)


def company_tokens(name: str) -> list[str]:
    """Lowercased word tokens of a company name minus corporate stop-tokens;
    falls back to all tokens when stripping would leave nothing."""
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    kept = [t for t in tokens if t not in _STOP_TOKENS]
    return kept or tokens


def classify(subject: str, body: str) -> str | None:
    """"rejected" / "applied" / None from phrase rules over subject + body.
    Rejection wins when both match (a rejection often quotes the receipt)."""
    text = f"{subject}\n{body}".lower()
    # Normalize whitespace to handle line wrapping in email bodies.
    text = " ".join(text.split())
    if any(p in text for p in REJECTION_PHRASES):
        return "rejected"
    if any(p in text for p in RECEIPT_PHRASES):
        return "applied"
    return None


def _sender_domain(from_header: str) -> str:
    addr = parseaddr(from_header)[1]
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def _all_tokens_in(tokens: list[str], text: str) -> bool:
    low = text.lower()
    return all(re.search(rf"\b{re.escape(t)}\b", low) for t in tokens)


def matches_card(company: str, *, from_header: str, subject: str, body: str) -> bool:
    """An email is a candidate for a card when the company's tokens all appear
    in From+Subject, or the sender is a known ATS domain AND the tokens all
    appear in Subject+body."""
    tokens = company_tokens(company)
    if not tokens:
        return False
    if _all_tokens_in(tokens, f"{from_header}\n{subject}"):
        return True
    domain = _sender_domain(from_header)
    if any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS):
        return _all_tokens_in(tokens, f"{subject}\n{body}")
    return False


_ACTIVE_STATUSES = ("interested", "applied", "interviewing", "offer")
_WATERMARK_KEY = "gmail"
_WATERMARK_OVERLAP = timedelta(minutes=30)


def _default_client_factory():
    return imaplib.IMAP4_SSL("imap.gmail.com", ssl_context=ssl.create_default_context())


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 — a mangled header is not worth a crash
        return value


def _body_text(msg: email.message.Message) -> str:
    """text/plain part preferred; text/html tag-stripped as fallback."""
    plain, html = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — skip undecodable parts
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    return plain or re.sub(r"<[^>]+>", " ", html)


def run_gmail_check(
    store,
    source_state,
    *,
    address: str,
    app_password: str,
    first_run_days: int = 3,
    lookback_max_days: int = 7,
    max_messages: int = 200,
    client_factory=None,
    now: datetime | None = None,
) -> dict:
    """One read-only sweep: match recent inbox mail to active-column cards and
    write suggest-only badges. Returns a tally for the log line. No-ops on
    stores without update_email_suggestion (DynamoDB backend). Raises on
    IMAP/connection failure so the caller's watermark stays put (the
    scheduler job catches and logs)."""
    tally = {"fetched": 0, "matched": 0, "suggested": 0, "skipped": 0}
    update = getattr(store, "update_email_suggestion", None)
    if update is None:
        return tally

    cards = [m for m in store.list_matches() if m.get("status") in _ACTIVE_STATUSES]
    if not cards:
        return tally

    now = now or datetime.now(timezone.utc)
    watermark = source_state.get(_WATERMARK_KEY).last_modified
    if watermark:
        try:
            since = datetime.fromisoformat(watermark)
        except ValueError:
            since = now - timedelta(days=first_run_days)
    else:
        since = now - timedelta(days=first_run_days)
    since = max(since, now - timedelta(days=lookback_max_days))
    # Overlap the previous sweep: mail delivered after sweep N but Date-stamped
    # before N's start (queue delay, clock skew) would otherwise be dropped
    # forever. Re-processing is free — message_id idempotence + the dismissed
    # list dedupe anything the overlap re-fetches.
    since -= _WATERMARK_OVERLAP

    client = (client_factory or _default_client_factory)()
    try:
        client.login(address, app_password)
        client.select("INBOX", readonly=True)  # EXAMINE: mailbox is never modified
        _typ, data = client.search(None, "SINCE", since.strftime("%d-%b-%Y"))
        ids = (data[0] or b"").split()
        if len(ids) > max_messages:
            log.warning(
                "gmail_check_truncated",
                extra={"total": len(ids), "cap": max_messages},
            )
            ids = ids[-max_messages:]  # newest have the highest sequence numbers

        for num in ids:
            _typ, msg_data = client.fetch(num.decode(), "(BODY.PEEK[])")
            try:
                raw = next(p[1] for p in msg_data if isinstance(p, tuple))
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                msg_date = email.utils.parsedate_to_datetime(msg["Date"]) if msg["Date"] else None
                if msg_date and msg_date < since:
                    continue  # SEARCH SINCE is day-granular; refine to the watermark
                tally["fetched"] += 1
                subject = _decode(msg["Subject"])
                from_header = _decode(msg["From"])
                message_id = (msg["Message-ID"] or "").strip()
                body = _body_text(msg)
                verdict = classify(subject, body)
                if verdict is None:
                    tally["skipped"] += 1
                    continue
                matched_any = False
                for card in cards:
                    if not matches_card(
                        card.get("company", ""),
                        from_header=from_header, subject=subject, body=body,
                    ):
                        continue
                    matched_any = True
                    if verdict == "applied" and card.get("status") != "interested":
                        continue  # forward-only receipts
                    if message_id and message_id in (card.get("dismissed_suggestions") or []):
                        continue
                    existing = card.get("email_suggestion") or {}
                    if (
                        existing.get("message_id") == message_id
                        and existing.get("suggested_status") == verdict
                    ):
                        continue  # idempotent re-sweep
                    phrases = REJECTION_PHRASES if verdict == "rejected" else RECEIPT_PHRASES
                    text = f"{subject}\n{body}".lower()
                    matched_phrase = next((p for p in phrases if p in text), "")
                    suggestion = {
                        "suggested_status": verdict,
                        "subject": subject[:200],
                        "from": from_header[:200],
                        "date": (msg_date or now).isoformat(),
                        "matched_phrase": matched_phrase,
                        "message_id": message_id,
                    }
                    if update(card["job_id"], suggestion=suggestion):
                        card["email_suggestion"] = suggestion
                        tally["suggested"] += 1
                if matched_any:
                    tally["matched"] += 1
                else:
                    tally["skipped"] += 1
            except Exception:  # noqa: BLE001 — one weird email never kills a sweep — but a dead connection must
                log.debug("gmail_check_message_skipped", exc_info=True)
                tally["skipped"] += 1
    finally:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass

    source_state.put(_WATERMARK_KEY, ConnectorState(last_modified=now.isoformat()))
    return tally
