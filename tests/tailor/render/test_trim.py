from src.tailor.render.assemble import RenderDoc, RenderExperience, RenderProject, fit_to_pages


def _doc(exp_bullets, proj_bullets):
    return RenderDoc(
        name="S", contact_items=[], skill_rows=[],
        experiences=[RenderExperience(company="C", role="R", dates="", bullets=list(exp_bullets))],
        projects=[RenderProject(name="P", subtitle="", dates="", bullets=list(proj_bullets))],
        education=[], volunteer=[],
    )


def _total_bullets(doc):
    return sum(len(e.bullets) for e in doc.experiences) + sum(len(p.bullets) for p in doc.projects)


def _pages_if_over(threshold):
    # stub render_fn: 1 page while total bullets <= threshold, else 2
    return lambda d: 1 if _total_bullets(d) <= threshold else 2


def test_fits_within_max_pages_no_trim():
    doc = _doc(["e1", "e2"], ["p1"])
    fitted, trimmed, warning = fit_to_pages(doc, _pages_if_over(5), max_pages=1)
    assert trimmed == [] and warning is None
    assert _total_bullets(fitted) == 3


def test_trims_projects_before_experiences():
    doc = _doc(["e1", "e2"], ["p1", "p2", "p3"])   # 5 total; threshold 3 -> drop 2
    fitted, trimmed, warning = fit_to_pages(doc, _pages_if_over(3), max_pages=1)
    assert fitted.projects[0].bullets == ["p1"]        # dropped p3, p2 (trailing = least JD-relevant)
    assert fitted.experiences[0].bullets == ["e1", "e2"]
    assert len(trimmed) == 2
    assert warning is None


def test_trims_experience_only_after_projects_exhausted():
    doc = _doc(["e1", "e2", "e3"], ["p1"])   # 4 total; threshold 2
    fitted, trimmed, warning = fit_to_pages(doc, _pages_if_over(2), max_pages=1)
    assert fitted.projects[0].bullets == ["p1"]        # project kept at floor (never emptied)
    assert fitted.experiences[0].bullets == ["e1"]     # experience trimmed to floor
    assert len(trimmed) == 2
    assert warning is None


def _doc_with_entry_bullets(exp, proj):
    return _doc(exp, proj)


def test_min_bullets_floor_respected():
    doc = _doc_with_entry_bullets(exp=["e1", "e2", "e3"], proj=["p1", "p2", "p3"])
    calls = {"n": 0}

    def pages(d):
        calls["n"] += 1
        return 2  # never fits

    fitted, trimmed, warning = fit_to_pages(doc, pages, max_pages=1, min_bullets=2)
    assert len(fitted.experiences[0].bullets) == 2
    assert len(fitted.projects[0].bullets) == 2
    assert warning is not None and "1 page" in warning


def _doc_minimal():
    return _doc(["e1"], ["p1"])   # single bullet everywhere — nothing trimmable below floor 1


def test_overflow_returns_warning_instead_of_raising():
    doc = _doc_minimal()
    fitted, trimmed, warning = fit_to_pages(doc, lambda d: 3, max_pages=2)
    assert trimmed == []
    assert "2 page" in warning


def test_fit_to_two_pages_allows_two():
    doc = _doc(["e1", "e2"], ["p1"])
    fitted, trimmed, warning = fit_to_pages(doc, lambda d: 2, max_pages=2)
    assert trimmed == [] and warning is None
