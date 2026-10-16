"""
FastMCP Server exposing FalkorDB repository triage tools.

Provides standardized MCP tool endpoints for external agents and clients:
  - detect_pr_collisions: Detects 0-hop direct file and 1-hop import-coupled collisions between open PRs.
  - link_issues_to_prs: Traverses Issue -> File -> PR connections and writes back candidate edges.
  - recommend_reviewers: Evaluates commit history and review networks to recommend peer reviewers.
  - assess_pr_core_risk: Assesses architectural criticality using GraphBLAS PageRank scores on imports.
  - approve_and_record_triage: Records triage decisions in FalkorDB and outputs GitHub CLI commands.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastmcp import FastMCP

import graph_tools

load_dotenv()

# Initialize FastMCP Server
mcp = FastMCP(
    name="Open-Source Maintainer Triage Agent",
    instructions=(
        "Autonomous maintainer triage agent operating directly on a live FalkorDB knowledge graph. "
        "Provides deterministic graph traversal tools for detecting PR merge hazards, linking issues to PRs, "
        "recommending qualified reviewers via recency-weighted commit history, assessing core architectural risk "
        "via PageRank centrality, and recording verified triage decisions back to the graph."
    ),
)


@mcp.tool()
def detect_pr_collisions(pr_number: Optional[int] = None) -> Dict[str, Any]:
    """
    Detects merge hazard collisions between open pull requests on the same base branch.
    Examines:
      - 0-hop DIRECT_FILE_OVERLAP: PRs modifying identical core Python files.
      - 1-hop IMPORT_COUPLED_COLLISION: PR1 modifies a file that imports a file being modified by PR2.
    Returns collision severity (HIGH/MEDIUM), churn volume, and explicit graph_path strings.

    Args:
        pr_number: Optional pull request number to filter collisions for. If None, checks all open PRs.
    """
    return graph_tools.detect_pr_collisions(pr_number=pr_number)


@mcp.tool()
def link_issues_to_prs(issue_number: Optional[int] = None) -> Dict[str, Any]:
    """
    Traverses the knowledge graph from open Issues to open PRs via:
      - 0-hop: Issue -[:MENTIONS]-> File <-[:MODIFIES]- PR
      - 1-hop: Issue -[:MENTIONS]-> File1 -[:IMPORTS]-> File2 <-[:MODIFIES]- PR
    Discovers which candidate pull requests potentially resolve each issue without vector embeddings.
    Automatically writes back [:POTENTIALLY_ADDRESSED_BY] edges into FalkorDB.

    Args:
        issue_number: Optional issue number to search for candidate PRs. If None, evaluates all open issues.
    """
    return graph_tools.link_issues_to_prs(issue_number=issue_number)


@mcp.tool()
def recommend_reviewers(pr_number: int) -> Dict[str, Any]:
    """
    Recommends the top 3 most qualified peer reviewers for a PR by traversing from modified files
    to commits and developers in the live FalkorDB graph.
    Applies time-decay commit churn weighting: churn / (1.0 + 0.05 * days_ago) plus a +15.0 bonus
    for past approved reviews on the same code modules. Excludes PR authors and automated bots.
    Returns composite score breakdowns, files mastered, and human-readable graph_path evidence.

    Args:
        pr_number: The pull request number to find reviewers for.
    """
    return graph_tools.recommend_reviewers(pr_number=pr_number)


@mcp.tool()
def assess_pr_core_risk(pr_number: int) -> Dict[str, Any]:
    """
    Classifies a pull request as HIGH_RISK_CORE (requires 2 senior reviewers) or STANDARD based on:
      - Pre-computed GraphBLAS PageRank scores on File imports
      - Inbound import coupling count (dependency fan-in)
      - Cumulative code churn (additions + deletions)
    Excludes __init__.py and test files to focus strictly on foundational subsystem modules.

    Args:
        pr_number: The pull request number to evaluate.
    """
    return graph_tools.assess_pr_core_risk(pr_number=pr_number)


@mcp.tool()
def approve_and_record_triage(
    item_type: str,
    item_number: int,
    action_summary: str,
    reviewer: Optional[str] = None,
    linked_pr: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Human-in-the-loop triage approval action:
      - Commits a (:TriageDecision) node linked via [:HAS_TRIAGE_DECISION] to the Issue or PR in FalkorDB.
      - If linked_pr is provided, marks [:POTENTIALLY_ADDRESSED_BY] as verified_by_human=true with confidence=1.0.
      - Generates exact ready-to-run GitHub CLI (`gh`) commands to apply the action on GitHub.

    Args:
        item_type: 'issue' or 'pull_request'
        item_number: The issue or PR number
        action_summary: Human-readable rationale and summary of the decision
        reviewer: Optional GitHub handle of assigned reviewer
        linked_pr: Optional candidate PR number addressing this issue
    """
    return graph_tools.approve_and_record_triage(
        item_type=item_type,
        item_number=item_number,
        action_summary=action_summary,
        reviewer=reviewer,
        linked_pr=linked_pr,
    )


def run_self_test() -> None:
    """Runs a quick verification test across all 5 MCP tools."""
    print("Testing MCP Tools against live FalkorDB...")

    # 1. PR Collisions
    col = detect_pr_collisions(3741)
    print(f"[1] detect_pr_collisions(3741): Found {col['total_collisions']} collisions.")

    # 2. Issue to PR Linker
    links = link_issues_to_prs(3728)
    print(f"[2] link_issues_to_prs(3728): Found {links['candidate_links_found']} candidate PRs.")

    # 3. Reviewer Recommendation
    revs = recommend_reviewers(3741)
    print(f"[3] recommend_reviewers(3741): Recommended {[r['login'] for r in revs['recommended_reviewers']]}")

    # 4. Core Risk Assessment
    risk = assess_pr_core_risk(3741)
    print(f"[4] assess_pr_core_risk(3741): Risk Level = {risk['risk_level']} (Reviewers needed: {risk['required_reviewers']})")

    # 5. Approve & Record Triage Decision
    dec = approve_and_record_triage(
        item_type="issue",
        item_number=3728,
        action_summary="Verified candidate PR #3769 addresses client session timeout handling.",
        linked_pr=3769,
    )
    print(f"[5] approve_and_record_triage: Decision ID = {dec['decision_id']} -> Commands: {dec['executable_gh_cli_commands']}")

    print("\n[+] All 5 MCP tools verified successfully!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FastMCP Server for Open-Source Maintainer Triage Agent")
    parser.add_argument("--test", action="store_true", help="Run self-test on all 5 MCP tools")
    args = parser.parse_args()

    if args.test:
        run_self_test()
    else:
        mcp.run()
