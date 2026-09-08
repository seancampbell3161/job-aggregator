import re

from src.web.funnel import Rate, build_funnel, pipeline_rates


def m(status, *hist):
    """A minimal match dict: current status + prior history statuses."""
    return {
        "status": status,
        "history": [{"status": s, "at": f"2026-07-0{i + 1}T00:00:00+00:00"} for i, s in enumerate(hist)],
    }


def links_of(funnel):
    return {(l.source, l.target): l.count for l in funnel.links}


def counts_of(funnel):
    return {n.id: n.count for n in funnel.nodes}


def test_empty_matches_yield_empty_funnel():
    f = build_funnel([])
    assert f.nodes == [] and f.links == []


def test_untouched_new_matches_flow_to_still_new():
    f = build_funnel([m("new"), m("new")])
    assert counts_of(f) == {"matches": 2, "still_new": 2}
    assert links_of(f) == {("matches", "still_new"): 2}


def test_full_progression_counts_every_stage():
    f = build_funnel([m("offer", "new", "interested", "applied", "interviewing")])
    c = counts_of(f)
    assert c == {"matches": 1, "interested": 1, "applied": 1, "interviewing": 1, "offer": 1}
    assert links_of(f) == {
        ("matches", "interested"): 1, ("interested", "applied"): 1,
        ("applied", "interviewing"): 1, ("interviewing", "offer"): 1,
    }


def test_skipped_stage_counts_as_passed_through():
    # new -> applied directly: Interested node still counts it (implicit pass-through)
    f = build_funnel([m("applied", "new")])
    c = counts_of(f)
    assert c["interested"] == 1 and c["applied"] == 1 and c["awaiting"] == 1
    assert links_of(f)[("applied", "awaiting")] == 1


def test_dismissed_from_new_goes_to_col1_dismissed():
    f = build_funnel([m("dismissed", "new")])
    assert counts_of(f) == {"matches": 1, "dismissed": 1}
    assert links_of(f) == {("matches", "dismissed"): 1}


def test_dismissed_after_interest_is_withdrawn():
    f = build_funnel([m("dismissed", "new", "interested")])
    c = counts_of(f)
    assert c["interested"] == 1 and c["withdrawn"] == 1 and "dismissed" not in c
    assert links_of(f)[("interested", "withdrawn")] == 1


def test_rejection_attaches_where_it_happened():
    f = build_funnel([
        m("rejected", "new", "applied"),                     # rejected at applied
        m("rejected", "new", "applied", "interviewing"),     # rejected at interviewing
    ])
    l = links_of(f)
    assert l[("applied", "rejected")] == 1
    assert l[("interviewing", "rejected")] == 1
    assert counts_of(f)["rejected"] == 2


def test_backward_move_absorbed_by_max_stage():
    # interviewing -> moved back to applied: still counts as reached interviewing
    f = build_funnel([m("applied", "new", "interested", "applied", "interviewing")])
    c = counts_of(f)
    assert c["interviewing"] == 1
    assert "awaiting" not in c   # not stuck at applied — it progressed beyond


def test_offer_reached_draws_no_exit():
    f = build_funnel([m("rejected", "new", "applied", "interviewing", "offer")])
    c = counts_of(f)
    assert c["offer"] == 1 and "rejected" not in c


def test_ghosted_without_history_attaches_at_matches():
    f = build_funnel([m("ghosted")])
    assert links_of(f) == {("matches", "ghosted"): 1}


def test_reduce_tolerates_malformed_history_rows():
    # Corrupt/hand-edited store rows must degrade, not 500 /analytics:
    # non-dict history entries are ignored; current status still classifies.
    f = build_funnel([
        {"status": "applied", "history": [None, "junk", {"status": "interested", "at": ""}]},
        {"status": "new", "history": "corrupt"},   # a string iterates as chars
    ])
    c = counts_of(f)
    assert c["matches"] == 2 and c["applied"] == 1 and c["awaiting"] == 1 and c["still_new"] == 1


def test_rates_denominators():
    rates = pipeline_rates([
        m("new"), m("dismissed"),
        m("applied", "interested"), m("interviewing", "applied"),
        m("offer", "applied", "interviewing"),
    ])
    by_label = {r.label: r for r in rates}
    assert by_label["Apply rate"].num == 3 and by_label["Apply rate"].den == 5
    assert by_label["Interview rate"].num == 2 and by_label["Interview rate"].den == 3
    assert by_label["Offer rate"].num == 1 and by_label["Offer rate"].den == 3


def test_rate_display_rounding_and_zero_denominator():
    assert Rate("x", "applications", 1, 4).display == "25%"
    assert Rate("x", "applications", 1, 308).display == "0.3%"
    assert Rate("x", "applications", 0, 5).display == "0%"
    empty = Rate("x", "applications", 0, 0)
    assert empty.display == "—" and empty.dash == 0.0
    assert empty.caption == "no applications yet"
    assert Rate("x", "applications", 6, 24).caption == "6 of 24 applications"


def test_rate_dash_is_ring_arc_fraction():
    assert Rate("x", "m", 1, 2).dash == 94.2   # half of 188.5, rounded to .1


from src.web.funnel import build_pipeline, pipeline_funnel, triage_split  # noqa: E402


# The production data shape the panel actually has to render: 1855 matches, 94%
# of them dismissed at triage, and a live funnel three orders of magnitude
# smaller. Every layout problem this panel has had showed up here first.
def _production_matches():
    return (
        [m("dismissed")] * 1751
        + [m("new")] * 34
        + [m("rejected")] * 3
        + [m("ghosted")] * 1
        + [m("dismissed", "interested")] * 3
        + [m("ghosted", "applied")] * 35
        + [m("rejected", "applied")] * 16
        + [m("dismissed", "applied")] * 1
        + [m("applied", "interested")] * 6
        + [m("rejected", "applied", "interviewing")] * 1
        + [m("dismissed", "applied", "interviewing")] * 1
        + [m("interviewing", "applied")] * 3
    )


def test_triage_split_partitions_every_match():
    t = triage_split(build_funnel(_production_matches()))
    assert t.total == 1855
    assert sum(s.count for s in t.segments) == t.total   # the bar accounts for all of it
    assert {s.id: s.count for s in t.segments} == {
        "pipeline": 70, "still_new": 34, "dismissed": 1751}
    assert [s.share for s in t.segments] == ["4%", "2%", "94%"]


def test_triage_split_is_none_without_matches():
    assert triage_split(build_funnel([])) is None


def test_pipeline_funnel_drops_the_triage_hop_and_conserves_flow():
    f = pipeline_funnel(build_funnel(_production_matches()))
    counts, links = counts_of(f), links_of(f)
    assert "dismissed" not in counts and "still_new" not in counts and "matches" not in counts
    # In pipeline == matches minus both triage exits, and that is exactly what
    # the surviving links carry out of it — the split cannot leak a job.
    assert counts["pipeline"] == 70
    assert sum(c for (s, _), c in links.items() if s == "pipeline") == 70
    assert all(s != "matches" for s, _ in links)


def test_pipeline_steps_are_furthest_stage_reached():
    p = build_pipeline(_production_matches())
    assert p.live == 70
    assert [(s.label, s.count) for s in p.steps] == [
        ("In pipeline", 70), ("Interested", 66), ("Applied", 63),
        ("Interviewing", 5), ("Offer", 0)]
    # widths are relative to the first step, conversions to the previous one
    assert [s.width_pct for s in p.steps] == [100.0, 94.29, 90.0, 7.14, 0.0]
    assert [s.conversion for s in p.steps] == [None, "94%", "95%", "8%", "0%"]
    assert p.steps[1].conversion_note == "94% of In pipeline"


def test_pipeline_standing_partitions_the_live_jobs():
    p = build_pipeline(_production_matches())
    # Current status, not furthest reached: 5 jobs got to Interviewing, 3 are
    # still there. Segments must total `live` so the bar is a true 100% stack.
    assert sum(s.count for s in p.standing) == p.live == 70
    assert [(s.label, s.count) for s in p.standing] == [
        ("Ghosted", 36), ("Rejected", 20), ("Awaiting reply", 6),
        ("Withdrawn", 5), ("Interviewing", 3)]
    assert [s.share for s in p.standing] == ["51%", "29%", "9%", "7%", "4%"]


def test_pipeline_standing_ordering_is_deterministic_on_ties():
    # Equal counts fall back to the declared status order, so the bar and its
    # key never reshuffle between requests on the same data.
    p = build_pipeline([m("rejected", "applied"), m("ghosted", "applied"),
                        m("interviewing", "applied"), m("applied", "interested")])
    assert [s.label for s in p.standing] == [
        "Interviewing", "Awaiting reply", "Rejected", "Ghosted"]


def test_pipeline_flows_carry_the_stage_level_exit_detail():
    # The one thing the bars fold away: Withdrawn 5 is 3 from Interested, 1 from
    # Applied, 1 from Interviewing. The table view has to keep that.
    p = build_pipeline(_production_matches())
    withdrawn = {f.source: f.count for f in p.flows if f.target == "Withdrawn"}
    assert withdrawn == {"Interested": 3, "Applied": 1, "Interviewing": 1}
    assert all(f.source != "Matches" for f in p.flows)


def test_pipeline_is_empty_but_safe_without_matches():
    p = build_pipeline([])
    assert p.triage is None and p.live == 0
    assert p.steps == [] and p.standing == [] and p.flows == []


def test_pipeline_with_nothing_past_triage():
    p = build_pipeline([m("dismissed")] * 5 + [m("new")] * 2)
    assert p.live == 0 and p.steps == [] and p.standing == []
    assert p.triage.total == 7                     # triage bar still renders
    assert {s.id for s in p.triage.segments} == {"dismissed", "still_new"}


def test_pipeline_unknown_status_still_counted():
    # A status outside the ladder must not silently vanish from the stack, or
    # the standing bar stops summing to `live`.
    p = build_pipeline([m("on_hold", "applied"), m("rejected", "applied")])
    assert sum(s.count for s in p.standing) == p.live == 2
    assert ("other", "On hold", 1) in [(s.id, s.label, s.count) for s in p.standing]
