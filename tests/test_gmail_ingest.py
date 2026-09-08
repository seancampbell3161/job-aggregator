# tests/test_gmail_ingest.py
"""Gmail ingestion: phrase rules, matching, and the IMAP sweep (fake client)."""
from src.gmail_ingest import classify, company_tokens, matches_card

REJECTION_BODY = """Dear Jane,

Thank you for your interest in the Senior Software Engineer role at Acme.
After careful consideration, we have decided to move forward with other
candidates whose experience more closely matches our needs.

We wish you the best in your search.
Acme Recruiting"""

RECEIPT_BODY = """Hi Jane,

Thank you for applying to Widget Inc! Your application has been received
and our team will review it shortly. No action is needed from you.

Widget Inc Talent Team"""


def test_classify_rejection():
    assert classify("Update on your Acme application", REJECTION_BODY) == "rejected"


def test_classify_receipt():
    assert classify("Application received", RECEIPT_BODY) == "applied"


def test_classify_rejection_wins_over_quoted_receipt():
    both = REJECTION_BODY + "\n> " + RECEIPT_BODY
    assert classify("Re: Application received", both) == "rejected"


def test_classify_neither_returns_none():
    assert classify("Your weekly job digest", "50 new jobs match your alerts!") is None


def test_classify_phrase_in_subject_counts():
    assert classify("Unfortunately, an update on your application", "See subject.") == "rejected"


def test_company_tokens_strip_stopwords_and_punctuation():
    assert company_tokens("The Trade Desk, Inc.") == ["trade", "desk"]
    assert company_tokens("Brown & Brown") == ["brown", "brown"]


def test_matches_card_company_in_from_or_subject():
    assert matches_card(
        "Widget Inc",
        from_header="Widget Careers <no-reply@widget.com>",
        subject="Your application",
        body="",
    )
    assert matches_card(
        "Widget Inc",
        from_header="no-reply@notifications.example.com",
        subject="Widget: interview availability",
        body="",
    )


def test_matches_card_ats_domain_with_company_in_body():
    assert matches_card(
        "Widget Inc",
        from_header="no-reply@us.greenhouse-mail.io",
        subject="Your application update",
        body="Thank you for your interest in Widget.",
    )


def test_matches_card_ats_domain_without_company_is_no_match():
    assert not matches_card(
        "Widget Inc",
        from_header="no-reply@us.greenhouse-mail.io",
        subject="Your application update",
        body="Thank you for your interest in Gadget Corp.",
    )


def test_matches_card_unrelated_sender_is_no_match():
    assert not matches_card(
        "Widget Inc",
        from_header="newsletter@jobsite.com",
        subject="50 new jobs for you",
        body="Widget Inc is hiring!",  # body alone is not enough for non-ATS senders
    )


def test_matches_card_tokens_use_word_boundaries():
    # "Meta" must not match inside "metadata"
    assert not matches_card(
        "Meta",
        from_header="no-reply@us.greenhouse-mail.io",
        subject="About your data",
        body="We updated our metadata policy.",
    )


from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from src.gmail_ingest import run_gmail_check
from src.models import ConnectorState, NormalizedPosting
from src.sqlite_db import connect
from src.state_sqlite import SqliteSeenJobsStore, SqliteSourceStateStore

NOW = datetime(2026, 7, 2, 15, 0, 0, tzinfo=timezone.utc)


def _posting(job_id, company):
    return NormalizedPosting(
        job_id=job_id, title="Software Engineer", company=company,
        location_text="Remote (US)", location_tags=frozenset({"remote", "us"}),
        seniority="mid", stack=frozenset({"python"}), comp_min=None, comp_max=None,
        apply_url=f"https://apply/{job_id}", description="d",
        posted_at=datetime(2026, 6, 20, tzinfo=timezone.utc), source="greenhouse:x",
    )


def _rfc822(from_, subject, body, msg_id, date=None):
    m = EmailMessage()
    m["From"] = from_
    m["Subject"] = subject
    m["Message-ID"] = msg_id
    m["Date"] = (date or NOW - timedelta(hours=2)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    m.set_content(body)
    return m.as_bytes()


class FakeImap:
    """Duck-types the imaplib slice run_gmail_check uses."""
    def __init__(self, messages):
        self.messages = list(messages)  # list of rfc822 bytes
        self.readonly = None
        self.logged_out = False
        self.fetch_commands = []

    def login(self, user, password):
        return ("OK", [b"Logged in"])

    def select(self, mailbox, readonly=False):
        self.readonly = readonly
        return ("OK", [str(len(self.messages)).encode()])

    def search(self, charset, *criteria):
        ids = b" ".join(str(i + 1).encode() for i in range(len(self.messages)))
        return ("OK", [ids])

    def fetch(self, num, parts):
        self.fetch_commands.append(parts)
        raw = self.messages[int(num) - 1]
        return ("OK", [(num.encode() if isinstance(num, str) else num, raw)])

    def logout(self):
        self.logged_out = True
        return ("BYE", [b""])


def _stores_with_cards(cards):
    conn = connect(":memory:")
    store = SqliteSeenJobsStore(conn)
    src_state = SqliteSourceStateStore(conn)
    for job_id, company, status in cards:
        store.claim_for_notify(job_id, score=7, posting=_posting(job_id, company))
        store.set_status(job_id, status)
    return store, src_state


def _run(store, src_state, fake):
    return run_gmail_check(
        store, src_state, address="me@gmail.com", app_password="pw",
        client_factory=lambda: fake, now=NOW,
    )


def test_default_client_factory_verifies_tls(monkeypatch):
    from src.gmail_ingest import _default_client_factory
    captured = {}

    class FakeSSL:
        def __init__(self, host, ssl_context=None):
            captured["host"] = host
            captured["ctx"] = ssl_context

    import imaplib
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeSSL)
    _default_client_factory()
    import ssl
    assert captured["host"] == "imap.gmail.com"
    assert isinstance(captured["ctx"], ssl.SSLContext)
    assert captured["ctx"].verify_mode == ssl.CERT_REQUIRED


def test_rejection_email_writes_suggestion():
    store, src_state = _stores_with_cards([("g:1", "Widget Inc", "applied")])
    fake = FakeImap([_rfc822(
        "Widget Recruiting <no-reply@us.greenhouse-mail.io>",
        "Update on your Widget application",
        "Unfortunately, we have decided to pursue other candidates.",
        "<r1@ats>",
    )])
    tally = _run(store, src_state, fake)
    assert tally == {"fetched": 1, "matched": 1, "suggested": 1, "skipped": 0}
    s = store.get_match("g:1")["email_suggestion"]
    assert s["suggested_status"] == "rejected"
    assert s["message_id"] == "<r1@ats>"
    assert s["matched_phrase"] == "unfortunately"
    assert "body" not in s


def test_receipt_only_suggests_applied_for_interested_cards():
    store, src_state = _stores_with_cards(
        [("g:1", "Widget Inc", "interested"), ("g:2", "Widget Inc", "applied")]
    )
    fake = FakeImap([_rfc822(
        "Widget Inc <careers@widget.com>", "Thank you for applying",
        "Your application has been received.", "<a1@ats>",
    )])
    _run(store, src_state, fake)
    assert store.get_match("g:1")["email_suggestion"]["suggested_status"] == "applied"
    assert store.get_match("g:2")["email_suggestion"] is None  # forward-only no-op


def test_readonly_examine_and_peek_are_used():
    store, src_state = _stores_with_cards([("g:1", "Widget Inc", "applied")])
    fake = FakeImap([_rfc822("x@widget.com", "hi", "nothing relevant", "<n1@x>")])
    _run(store, src_state, fake)
    assert fake.readonly is True
    assert all("PEEK" in c for c in fake.fetch_commands)
    assert fake.logged_out is True


def test_dismissed_message_never_resuggests():
    store, src_state = _stores_with_cards([("g:1", "Widget Inc", "applied")])
    msg = _rfc822(
        "no-reply@us.greenhouse-mail.io", "Widget update",
        "Unfortunately we moved on.", "<r1@ats>",
    )
    _run(store, src_state, FakeImap([msg]))
    store.update_email_suggestion("g:1", suggestion=None, record_dismissed=True)
    src_state.put("gmail", ConnectorState(last_modified=(NOW - timedelta(days=1)).isoformat()))
    tally = _run(store, src_state, FakeImap([msg]))
    assert tally == {"fetched": 1, "matched": 1, "suggested": 0, "skipped": 0}
    assert store.get_match("g:1")["email_suggestion"] is None


def test_rejection_suggests_on_all_matching_cards():
    store, src_state = _stores_with_cards(
        [("g:1", "Widget Inc", "applied"), ("g:2", "Widget Inc", "interviewing")]
    )
    fake = FakeImap([_rfc822(
        "no-reply@us.greenhouse-mail.io", "Widget update",
        "Unfortunately we moved on.", "<r1@ats>",
    )])
    tally = _run(store, src_state, fake)
    assert tally["suggested"] == 2  # the human disambiguates same-company cards


def test_same_suggestion_not_rewritten():
    store, src_state = _stores_with_cards([("g:1", "Widget Inc", "applied")])
    msg = _rfc822(
        "no-reply@us.greenhouse-mail.io", "Widget update",
        "Unfortunately we moved on.", "<r1@ats>",
    )
    _run(store, src_state, FakeImap([msg]))
    src_state.put("gmail", ConnectorState(last_modified=(NOW - timedelta(days=1)).isoformat()))
    tally = _run(store, src_state, FakeImap([msg]))
    assert tally["fetched"] == 1
    assert tally["suggested"] == 0  # idempotent second sweep


def test_watermark_advances_on_success_and_no_cards_skips_imap():
    store, src_state = _stores_with_cards([("g:1", "Widget Inc", "applied")])
    _run(store, src_state, FakeImap([]))
    assert src_state.get("gmail").last_modified == NOW.isoformat()

    empty_store, empty_state = _stores_with_cards([])
    boom = FakeImap([])
    boom.login = lambda *a: (_ for _ in ()).throw(AssertionError("no IMAP without cards"))
    tally = run_gmail_check(
        empty_store, empty_state, address="me@gmail.com", app_password="pw",
        client_factory=lambda: boom, now=NOW,
    )
    assert tally == {"fetched": 0, "matched": 0, "suggested": 0, "skipped": 0}


def test_failure_does_not_advance_watermark():
    store, src_state = _stores_with_cards([("g:1", "Widget Inc", "applied")])

    class BrokenImap(FakeImap):
        def search(self, charset, *criteria):
            raise OSError("connection dropped")

    try:
        run_gmail_check(
            store, src_state, address="me@gmail.com", app_password="pw",
            client_factory=lambda: BrokenImap([]), now=NOW,
        )
    except OSError:
        pass
    assert src_state.get("gmail").last_modified is None


def test_truncation_processes_newest_and_counts_skipped(caplog):
    store, src_state = _stores_with_cards([("g:1", "Widget Inc", "applied")])
    msgs = [
        _rfc822("x@other.com", "old noise 0", "irrelevant", "<n0@x>"),
        _rfc822("no-reply@us.greenhouse-mail.io", "Widget update",
                "Unfortunately we moved on.", "<old-rejection@ats>"),
        _rfc822("x@other.com", "new noise 2", "irrelevant", "<n2@x>"),
        _rfc822("no-reply@us.greenhouse-mail.io", "Widget update",
                "Unfortunately we moved on.", "<new-rejection@ats>"),
    ]
    import logging
    with caplog.at_level(logging.WARNING, logger="src.gmail_ingest"):
        tally = run_gmail_check(
            store, src_state, address="me@gmail.com", app_password="pw",
            max_messages=2, client_factory=lambda: FakeImap(msgs), now=NOW,
        )
    assert tally["fetched"] == 2          # newest two only
    assert tally["suggested"] == 1
    assert tally["skipped"] == 1          # the noise message among the kept two
    s = store.get_match("g:1")["email_suggestion"]
    assert s["message_id"] == "<new-rejection@ats>"  # newest kept, oldest dropped
    assert any("gmail_check_truncated" in r.message for r in caplog.records)


def test_midloop_fetch_failure_propagates_and_holds_watermark():
    store, src_state = _stores_with_cards([("g:1", "Widget Inc", "applied")])

    class DropsMidFetch(FakeImap):
        def fetch(self, num, parts):
            raise OSError("connection dropped mid-fetch")

    import pytest
    with pytest.raises(OSError):
        run_gmail_check(
            store, src_state, address="me@gmail.com", app_password="pw",
            client_factory=lambda: DropsMidFetch(
                [_rfc822("x@y.com", "s", "b", "<m@x>")]
            ),
            now=NOW,
        )
    assert src_state.get("gmail").last_modified is None


def test_late_delivered_mail_within_overlap_is_processed():
    store, src_state = _stores_with_cards([("g:1", "Widget Inc", "applied")])
    from src.models import ConnectorState
    # Watermark = NOW; message Date-stamped 15 min BEFORE the watermark
    src_state.put("gmail", ConnectorState(last_modified=NOW.isoformat()))
    msg = _rfc822(
        "no-reply@us.greenhouse-mail.io", "Widget update",
        "Unfortunately we moved on.", "<late@ats>",
        date=NOW - timedelta(minutes=15),
    )
    tally = run_gmail_check(
        store, src_state, address="me@gmail.com", app_password="pw",
        client_factory=lambda: FakeImap([msg]), now=NOW + timedelta(hours=1),
    )
    assert tally["suggested"] == 1
