def test_deliberately_failing_check():
    """THROWAWAY — exists only to prove the branch ruleset blocks a merge when a
    required status check fails. This branch is never merged."""
    assert False, "intentional failure: verifying required status checks gate the merge"
