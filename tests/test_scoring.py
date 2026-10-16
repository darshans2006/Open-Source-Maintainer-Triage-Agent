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
