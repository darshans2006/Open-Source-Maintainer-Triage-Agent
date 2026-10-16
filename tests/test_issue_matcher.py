import pytest
from ingest import IssueFileMatcher


def test_match_issue_explicit_path():
    tracked = {"httpx/_client.py", "httpx/_models.py", "httpx/_utils.py"}
    symbols = {"Client": "httpx/_client.py", "Response": "httpx/_models.py"}
    filenames = {"_client.py": "httpx/_client.py", "_models.py": "httpx/_models.py"}

    matcher = IssueFileMatcher(tracked, symbols, filenames)

    issue = {
        "number": 101,
        "title": "Bug in httpx/_client.py connection pool",
        "body": "When making consecutive requests, httpx/_client.py raises an exception.",
    }

    matches = matcher.match_issue(issue)
    assert len(matches) > 0
    top = matches[0]
    assert top.issue_number == 101
    assert top.file_path == "httpx/_client.py"
    assert top.confidence == 0.95
    assert top.source == "explicit_path"


def test_match_issue_stack_trace():
    tracked = {"httpx/_client.py", "httpx/_transports/default.py"}
    symbols = {}
    filenames = {}

    matcher = IssueFileMatcher(tracked, symbols, filenames)

    issue = {
        "number": 102,
        "title": "Unhandled exception on timeout",
        "body": (
            "Traceback (most recent call last):\n"
            '  File "_client.py", line 1024, in send\n'
            "    raise ConnectTimeout(message)\n"
        ),
    }

    matches = matcher.match_issue(issue)
    assert any(m.file_path == "httpx/_client.py" and m.source == "stack_trace" and m.confidence == 0.90 for m in matches)


def test_match_issue_unique_symbol():
    tracked = {"httpx/_client.py", "httpx/_models.py"}
    symbols = {"UniqueCustomClient": "httpx/_client.py"}
    filenames = {"_client.py": "httpx/_client.py"}

    matcher = IssueFileMatcher(tracked, symbols, filenames)

    issue = {
        "number": 103,
        "title": "Problem with UniqueCustomClient configuration",
        "body": "Passing invalid kwargs to UniqueCustomClient causes silent failure.",
    }

    matches = matcher.match_issue(issue)
    assert any(m.file_path == "httpx/_client.py" and m.source == "symbol_or_filename" and m.confidence == 0.70 for m in matches)
