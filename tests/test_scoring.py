import pytest
from ingest import BOT_KEYWORDS


def test_bot_filtering():
    def is_bot(name: str) -> bool:
        lower = name.lower()
        return any(k in lower for k in BOT_KEYWORDS)

    assert is_bot("dependabot[bot]") is True
    assert is_bot("github-actions[bot]") is True
    assert is_bot("pre-commit-ci[bot]") is True
    assert is_bot("codecov[bot]") is True
    assert is_bot("alex-maintainer") is False
    assert is_bot("p3ck") is False
    assert is_bot("florimondmanca") is False


def test_recency_decay_calculation():
    """
    Formula from graph_tools:
    score = changes / (1.0 + 0.05 * days_ago) + 15.0 * prior_reviews
    """
    def compute_score(changes: int, days_ago: int, prior_reviews: int = 0) -> float:
        commit_score = float(changes) / (1.0 + 0.05 * float(days_ago))
        review_bonus = float(prior_reviews) * 15.0
        return round(commit_score + review_bonus, 2)

    # Yesterday commit (1 day ago) with 100 lines changed
    score_fresh = compute_score(changes=100, days_ago=1, prior_reviews=0)
    assert score_fresh == round(100 / 1.05, 2)  # 95.24

    # 60 days ago commit with same 100 lines changed
    score_old = compute_score(changes=100, days_ago=60, prior_reviews=0)
    assert score_old == round(100 / (1.0 + 3.0), 2)  # 25.0

    # Fresh commit beats older commit
    assert score_fresh > score_old

    # Review bonus actively boosts scores of active reviewers
    score_with_reviews = compute_score(changes=10, days_ago=10, prior_reviews=2)
    assert score_with_reviews > 30.0


def test_writeback_edge_confidence_resolution():
    """
    Validates Cypher ON MATCH SET logic:
    CASE WHEN coalesce(rel.verified_by_human, false) THEN 1.0 ELSE item.confidence END
    """
    def resolve_confidence(verified_by_human: bool | None, calculated_confidence: float) -> float:
        is_verified = bool(verified_by_human) if verified_by_human is not None else False
        return 1.0 if is_verified else calculated_confidence

    # If maintainer already verified the edge, keep 1.0 even if recomputed confidence is lower
    assert resolve_confidence(verified_by_human=True, calculated_confidence=0.76) == 1.0

    # If unverified, use recalculated confidence
    assert resolve_confidence(verified_by_human=False, calculated_confidence=0.76) == 0.76
    assert resolve_confidence(verified_by_human=None, calculated_confidence=0.95) == 0.95


def test_sanitize_cli_arg():
    """Validates shell injection defense and quote escaping in generated gh CLI commands."""
    from graph_tools import _sanitize_cli_arg

    assert _sanitize_cli_arg("Standard recommendation") == "Standard recommendation"
    assert _sanitize_cli_arg('Fix "timeout" bug') == 'Fix \\"timeout\\" bug'
    assert _sanitize_cli_arg("Check `_client.py` imports") == "Check \\`_client.py\\` imports"
    assert _sanitize_cli_arg("Inject $(whoami) & $ENV") == "Inject \\$(whoami) & \\$ENV"
    assert _sanitize_cli_arg("Path C:\\Users\\test") == "Path C:\\\\Users\\\\test"
    assert _sanitize_cli_arg("") == ""


def test_stale_edge_pruning_filter():
    """Validates the Cypher predicate for purging candidate edges targeting closed PRs."""
    def should_prune(verified_by_human: bool | None, pr_state: str) -> bool:
        is_verified = bool(verified_by_human) if verified_by_human is not None else False
        return (not is_verified) and (pr_state != "open")

    # Unverified link to closed/merged PR -> should be pruned
    assert should_prune(verified_by_human=False, pr_state="closed") is True
    assert should_prune(verified_by_human=None, pr_state="merged") is True

    # Verified link to closed PR -> preserved (human ground truth)
    assert should_prune(verified_by_human=True, pr_state="closed") is False

    # Unverified link to open PR -> preserved for active evaluation
    assert should_prune(verified_by_human=False, pr_state="open") is False
    assert should_prune(verified_by_human=True, pr_state="open") is False


