# tests/test_tuning.py
from src.tuning import RoleRecommendation, ScoreLowRecommendation, analyze_role_titles, analyze_score_low


def _row(score, verdict):
    return {"score": score, "audit_verdict": verdict}


def _many(rescued, confirmed):
    return [_row(s, "rescued") for s in rescued] + [_row(s, "confirmed_rejected") for s in confirmed]


def test_score_low_clean_separation_suggests_minimal_change():
    # rescued at 4 (should have surfaced), confirmed at 0/1 (rightly hidden),
    # current=4. Thresholds 1,2,3 all split perfectly (0 errors); tie-break
    # picks the one CLOSEST to current (minimal change) → 3.
    rows = _many(rescued=[4, 4, 4, 4, 4, 4], confirmed=[0, 0, 1, 1, 1, 1])
    rec = analyze_score_low(rows, current=4, min_verdicts=10)
    assert rec.confident is True
    assert rec.suggested == 3                # closest-to-current zero-error threshold
    assert rec.sample_size == 12
    assert rec.errors_by_threshold[1] == 0   # also a perfect split
    assert rec.errors_by_threshold[3] == 0
    assert rec.errors_by_threshold[4] == 6   # suppressing <=4 loses all 6 rescued


def test_score_low_overlap_picks_min_error_threshold():
    # one confirmed(3) sits above a rescued(2) — no perfect split. current=4.
    rows = _many(rescued=[2, 4, 4, 4, 4], confirmed=[0, 1, 1, 3, 1])
    rec = analyze_score_low(rows, current=4, min_verdicts=8)
    # errors(t)=|resc<=t| + |conf>t|:
    #  t=1 -> resc<=1:1(the 2? no, 2>1 →0) ... compute: rescued sorted [2,4,4,4,4]
    #  t=1: resc<=1=0, conf>1: confirmed[0,1,1,3,1] >1 = {3} =1 → 1
    #  t=2: resc<=2=1, conf>2=1 → 2 ; t=3: resc<=3=1, conf>3=0 → 1
    # min error 1 at t=1 and t=3; tie → closest to current(4) → t=3
    assert rec.suggested == 3
    assert rec.errors_by_threshold[1] == 1
    assert rec.errors_by_threshold[3] == 1


def test_score_low_insufficient_data_returns_none():
    rec = analyze_score_low(_many(rescued=[4], confirmed=[1]), current=4, min_verdicts=10)
    assert rec.confident is False
    assert rec.suggested is None
    assert "insufficient" in rec.note.lower()
    assert rec.sample_size == 2


def test_score_low_one_class_only_not_confident():
    rec = analyze_score_low(_many(rescued=[4, 4, 4, 4, 4, 4, 4, 4, 4, 4], confirmed=[]),
                            current=4, min_verdicts=5)
    assert rec.confident is False
    assert rec.suggested is None


def test_score_low_skips_rows_without_int_score():
    rows = _many(rescued=[4, 4, 4, 4, 4], confirmed=[1, 1, 1, 1, 1])
    rows.append({"score": None, "audit_verdict": "rescued"})
    rows.append({"audit_verdict": "confirmed_rejected"})  # no score key
    rec = analyze_score_low(rows, current=4, min_verdicts=10)
    assert rec.sample_size == 10  # the two score-less rows excluded


def test_score_low_note_flags_one_directional_when_no_improvement():
    # all confirmed at the top of the suppressed range → lowering can't help;
    # suggested == current, note must warn it can only lower.
    rows = _many(rescued=[0, 0, 0, 0, 0], confirmed=[4, 4, 4, 4, 4])
    rec = analyze_score_low(rows, current=4, min_verdicts=8)
    assert rec.suggested is not None
    assert rec.suggested >= 0
    # whatever it picks, if it's >= current the note explains the one-directional limit
    if rec.suggested >= rec.current:
        assert "lower" in rec.note.lower()


def _rej(title, verdict, gate="role"):
    return {"title": title, "verdict": verdict, "rejected_by": gate}


def test_role_surfaces_rescued_titles_and_suggests_shared_tokens():
    rows = [
        _rej("Software Engineering Manager", "rescued"),
        _rej("Staff Engineering Manager", "rescued"),
        _rej("Data Analyst", "confirmed_rejected"),
        _rej("Recruiter", "confirmed_rejected"),
        _rej("Product Manager", "rescued", gate="seniority"),  # not role → ignored
    ]
    rec = analyze_role_titles(rows, current_titles=["software engineer", "backend engineer"])
    assert set(rec.rescued_titles) == {"Software Engineering Manager", "Staff Engineering Manager"}
    assert rec.confirmed_count == 2
    toks = dict(rec.suggested_tokens)
    # "engineering" and "manager" and "engineering manager" appear in both rescued
    # titles and are NOT covered by current_titles ("engineer" != "engineering")
    assert toks.get("manager") == 2
    assert toks.get("engineering") == 2
    # "software" IS covered by "software engineer" → not suggested
    assert "software" not in toks


def test_role_covered_token_not_suggested():
    rows = [_rej("Senior Software Engineer", "rescued"), _rej("Lead Software Engineer", "rescued")]
    rec = analyze_role_titles(rows, current_titles=["software engineer"])
    toks = dict(rec.suggested_tokens)
    assert "software" not in toks and "engineer" not in toks  # both covered
    # "senior"/"lead" appear once each (<2) → not suggested; "software engineer" covered


def test_role_no_rescued_titles_note():
    rows = [_rej("Data Analyst", "confirmed_rejected")]
    rec = analyze_role_titles(rows, current_titles=["software engineer"])
    assert rec.rescued_titles == []
    assert rec.suggested_tokens == []
    assert "nothing" in rec.note.lower() or "no role" in rec.note.lower()


def test_role_single_rescued_low_confidence_note():
    rows = [_rej("Machine Learning Engineer", "rescued")]
    rec = analyze_role_titles(rows, current_titles=["software engineer"])
    assert rec.rescued_titles == ["Machine Learning Engineer"]
    assert "low confidence" in rec.note.lower() or "speculative" in rec.note.lower()


def _score_row(job_id, score, *, notified=True, company="Acme Robotics",
               title="Staff Software Engineer", first_seen="2026-07-17T12:00:00+00:00"):
    return {"job_id": job_id, "score": score, "notified": notified,
            "company": company, "title": title, "first_seen": first_seen}


def test_source_scores_grouped_by_family():
    from src.tuning import analyze_source_scores

    rows = [
        _score_row("adzuna:1", 4), _score_row("adzuna:2", 6, notified=False),
        _score_row("greenhouse:acme:1", 8), _score_row("greenhouse:acme:2", 6),
        _score_row("greenhouse:beta:3", 7),
    ]
    stats = {s.source_family: s for s in analyze_source_scores(rows)}
    assert set(stats) == {"adzuna", "greenhouse"}
    assert stats["adzuna"].count == 2
    assert stats["adzuna"].median == 5.0
    assert stats["adzuna"].suppressed_rate == 0.5
    assert stats["greenhouse"].count == 3
    assert stats["greenhouse"].median == 7.0
    assert stats["greenhouse"].suppressed_rate == 0.0


def test_source_scores_single_row_family():
    from src.tuning import analyze_source_scores

    stats = analyze_source_scores([_score_row("adzuna:1", 6)])
    assert stats[0].median == 6.0 and stats[0].p25 == 6.0 and stats[0].p75 == 6.0


def test_snippet_pairs_match_company_title_within_window():
    from src.tuning import analyze_snippet_pairs

    rows = [
        _score_row("adzuna:1", 5, first_seen="2026-07-10T12:00:00+00:00"),
        _score_row("greenhouse:acme:9", 7, first_seen="2026-07-12T12:00:00+00:00"),
        # same key but outside the window:
        _score_row("lever:acme:3", 8, first_seen="2026-09-01T12:00:00+00:00"),
        # different title — never pairs:
        _score_row("greenhouse:acme:4", 2, title="Sales Lead",
                   first_seen="2026-07-11T12:00:00+00:00"),
    ]
    report = analyze_snippet_pairs(rows, window_days=14)
    assert len(report.pairs) == 1
    p = report.pairs[0]
    assert p.adzuna_job_id == "adzuna:1" and p.other_job_id == "greenhouse:acme:9"
    assert p.delta == 2                     # full JD scored 2 higher than the snippet
    assert report.mean_delta == 2.0


def test_snippet_pairs_empty_without_adzuna_rows():
    from src.tuning import analyze_snippet_pairs

    report = analyze_snippet_pairs([_score_row("greenhouse:acme:9", 7)])
    assert report.pairs == [] and report.mean_delta is None
    assert "no adzuna" in report.note


def test_snippet_pairs_window_is_symmetric_at_boundary():
    from src.tuning import analyze_snippet_pairs

    # 14 days 1 hour apart — excluded in BOTH orderings.
    a = _score_row("adzuna:1", 5, first_seen="2026-07-01T00:00:00+00:00")
    o = _score_row("greenhouse:acme:9", 7, first_seen="2026-07-15T01:00:00+00:00")
    assert analyze_snippet_pairs([a, o], window_days=14).pairs == []

    a2 = _score_row("adzuna:1", 5, first_seen="2026-07-15T01:00:00+00:00")
    o2 = _score_row("greenhouse:acme:9", 7, first_seen="2026-07-01T00:00:00+00:00")
    assert analyze_snippet_pairs([a2, o2], window_days=14).pairs == []

    # 13 days 23 hours apart — included in BOTH orderings.
    a3 = _score_row("adzuna:1", 5, first_seen="2026-07-01T01:00:00+00:00")
    o3 = _score_row("greenhouse:acme:9", 7, first_seen="2026-07-15T00:00:00+00:00")
    assert len(analyze_snippet_pairs([a3, o3], window_days=14).pairs) == 1

    a4 = _score_row("adzuna:1", 5, first_seen="2026-07-15T00:00:00+00:00")
    o4 = _score_row("greenhouse:acme:9", 7, first_seen="2026-07-01T01:00:00+00:00")
    assert len(analyze_snippet_pairs([a4, o4], window_days=14).pairs) == 1
