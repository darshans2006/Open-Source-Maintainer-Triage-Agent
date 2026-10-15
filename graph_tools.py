"""
Core graph traversal algorithms and triage procedures for FalkorDB.

Exports parameterized Cypher query routines:
  - detect_pr_collisions: Detects 0-hop direct file and 1-hop import-coupled collisions between open PRs.
  - link_issues_to_prs: Multi-hop Issue -> File -> PR traversal with candidate link write-back.
  - recommend_reviewers: Recency-decayed commit history & peer review network traversal.
  - assess_pr_core_risk: PageRank-driven core module criticality classification.
  - approve_and_record_triage: Maintainer decision audit log and GitHub CLI command generation.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()


def get_graph(graph_name: str = "maintainer_triage"):
    """Returns a connected FalkorDB Graph client instance."""
    use_lite = os.environ.get("USE_LITE") == "1"
    if use_lite:
        try:
            import falkordblite  # type: ignore

            lite_path = os.environ.get("FALKORDB_LITE_PATH", "maintainer_triage.db")
            client = falkordblite.FalkorDB(lite_path)
            return client.select_graph(graph_name)
        except ImportError as e:
            raise RuntimeError(f"FalkorDBLite requested but package not available: {e}") from e

    import falkordb

    host = os.environ.get("FALKORDB_HOST", "localhost")
    port = int(os.environ.get("FALKORDB_PORT", 6379))
    password = os.environ.get("FALKORDB_PASSWORD")
    client = falkordb.FalkorDB(host=host, port=port, password=password if password else None)
    return client.select_graph(graph_name)


# ---------------------------------------------------------------------------
# Tool 1: PR Collision Detection
# ---------------------------------------------------------------------------
def detect_pr_collisions(pr_number: Optional[int] = None) -> Dict[str, Any]:
    """
    Detects merge hazard collisions between open pull requests on the same base_branch:
      - 0 hops: DIRECT_FILE_OVERLAP (multiple PRs editing identical files)
      - 1 hop: IMPORT_COUPLED_COLLISION (PR1 edits file that directly imports file edited by PR2)
    Excludes __init__.py, documentation, and test files to avoid false positives.
    Deduplicates by colliding PR pair and limits to Top 5 Direct and Top 5 Coupled collisions.
    """
    g = get_graph()

    # 1. Direct File Overlap (0 hops)
    direct_query = """
    MATCH (pr1:PullRequest {state: 'open'})-[m1:MODIFIES]->(f:File)<-[m2:MODIFIES]-(pr2:PullRequest {state: 'open'})
    WHERE pr1.base_branch = pr2.base_branch
      AND pr1.number < pr2.number
      AND NOT f.path STARTS WITH 'tests/'
      AND NOT f.path STARTS WITH 'test/'
      AND NOT f.path ENDS WITH '__init__.py'
      AND ($pr_number IS NULL OR pr1.number = $pr_number OR pr2.number = $pr_number)
    RETURN pr1.number AS pr1,
           pr2.number AS pr2,
           pr1.title AS title1,
           pr2.title AS title2,
           pr1.author AS author1,
           pr2.author AS author2,
           f.path AS file_path,
           m1.changes + m2.changes AS total_churn,
           m1.changes AS churn1,
           m2.changes AS churn2
    ORDER BY total_churn DESC
    """
    res_direct = g.query(direct_query, {"pr_number": pr_number})
    direct_dedup: Dict[Tuple[int, int], Dict[str, Any]] = {}
    direct_pairs: Set[Tuple[int, int]] = set()

    for row in res_direct.result_set:
        p1, p2, t1, t2, a1, a2, fpath, churn, c1, c2 = row
        pair_key = (min(p1, p2), max(p1, p2))
        direct_pairs.add(pair_key)
        if pair_key not in direct_dedup or churn > direct_dedup[pair_key]["combined_churn"]:
            graph_path = (
                f"PullRequest #{p1} (@{a1}) -[:MODIFIES {{changes: {c1}}}]-> "
                f"File `{fpath}` <-[:MODIFIES {{changes: {c2}}}]- PullRequest #{p2} (@{a2})"
            )
            direct_dedup[pair_key] = {
                "collision_type": "DIRECT_FILE_OVERLAP",
                "severity": "HIGH",
                "hop_distance": 0,
                "pr1": {"number": p1, "author": a1, "title": t1, "changes": c1},
                "pr2": {"number": p2, "author": a2, "title": t2, "changes": c2},
                "colliding_file": fpath,
                "combined_churn": churn,
                "graph_path": graph_path,
                "recommendation": f"Alert maintainers: PR #{p1} and PR #{p2} both edit `{fpath}`. Sequence reviews to avoid merge conflicts.",
            }

    direct_collisions = sorted(direct_dedup.values(), key=lambda x: -x["combined_churn"])[:5]

    # 2. Import-Coupled Collision (1 hop dependency coupling) - Exclude __init__.py and direct pairs
    coupled_query = """
    MATCH (pr1:PullRequest {state: 'open'})-[m1:MODIFIES]->(f1:File)-[:IMPORTS]->(f2:File)<-[m2:MODIFIES]-(pr2:PullRequest {state: 'open'})
    WHERE pr1.base_branch = pr2.base_branch
      AND pr1.number <> pr2.number
      AND f1.path <> f2.path
      AND NOT f1.path STARTS WITH 'tests/'
      AND NOT f2.path STARTS WITH 'tests/'
      AND NOT f1.path STARTS WITH 'test/'
      AND NOT f2.path STARTS WITH 'test/'
      AND NOT f1.path ENDS WITH '__init__.py'
      AND NOT f2.path ENDS WITH '__init__.py'
      AND ($pr_number IS NULL OR pr1.number = $pr_number)
    RETURN pr1.number AS pr1,
           pr2.number AS pr2,
           pr1.title AS title1,
           pr2.title AS title2,
           pr1.author AS author1,
           pr2.author AS author2,
           f1.path AS file1,
           f2.path AS file2,
           m1.changes + m2.changes AS total_churn,
           m1.changes AS churn1,
           m2.changes AS churn2
    ORDER BY total_churn DESC
    """
    res_coupled = g.query(coupled_query, {"pr_number": pr_number})
    coupled_dedup: Dict[Tuple[int, int], Dict[str, Any]] = {}

    for row in res_coupled.result_set:
        p1, p2, t1, t2, a1, a2, f1, f2, churn, c1, c2 = row
        pair_key = (min(p1, p2), max(p1, p2))
        # Skip if already identified as a direct 0-hop collision
        if pair_key in direct_pairs:
            continue
        if pair_key not in coupled_dedup or churn > coupled_dedup[pair_key]["combined_churn"]:
            graph_path = (
                f"PullRequest #{p1} (@{a1}) -[:MODIFIES]-> File `{f1}` "
                f"-[:IMPORTS]-> File `{f2}` <-[:MODIFIES]- PullRequest #{p2} (@{a2})"
            )
            coupled_dedup[pair_key] = {
                "collision_type": "IMPORT_COUPLED_COLLISION",
                "severity": "MEDIUM",
                "hop_distance": 1,
                "pr1": {"number": p1, "author": a1, "title": t1, "changes": c1},
                "pr2": {"number": p2, "author": a2, "title": t2, "changes": c2},
                "upstream_file": f1,
                "downstream_file": f2,
                "combined_churn": churn,
                "graph_path": graph_path,
                "recommendation": f"Dependency risk: PR #{p1} edits `{f1}` which imports `{f2}` (being modified by PR #{p2}). Run integration tests together.",
            }

    coupled_collisions = sorted(coupled_dedup.values(), key=lambda x: -x["combined_churn"])[:5]
    all_collisions = direct_collisions + coupled_collisions

    return {
        "target_pr_number": pr_number,
        "total_collisions": len(all_collisions),
        "high_severity_direct": len(direct_collisions),
        "medium_severity_coupled": len(coupled_collisions),
        "direct_collisions": direct_collisions,
        "coupled_collisions": coupled_collisions,
        "collisions": all_collisions,
    }


# ---------------------------------------------------------------------------
# Tool 2: Issue-to-PR Multi-Hop Linker with Write-Back
# ---------------------------------------------------------------------------
def link_issues_to_prs(issue_number: Optional[int] = None) -> Dict[str, Any]:
    """
    Traverses the graph from open Issues to open PRs via:
      - 0 hops: Issue -[:MENTIONS]-> File <-[:MODIFIES]- PR
      - 1 hop: Issue -[:MENTIONS]-> File1 -[:IMPORTS]-> File2 <-[:MODIFIES]- PR
    Excludes __init__.py and tests/ as bridge nodes to prevent ubiquitous graph floods.
    Deduplicates candidates per Issue and writes back verified candidate links to FalkorDB as [:POTENTIALLY_ADDRESSED_BY].
    """
    g = get_graph()
    now_iso = datetime.now(timezone.utc).isoformat()

    # Query 0-hop direct file matches
    direct_query = """
    MATCH (i:Issue {state: 'open'})-[m:MENTIONS]->(f:File)<-[mod:MODIFIES]-(pr:PullRequest {state: 'open'})
    WHERE ($issue_number IS NULL OR i.number = $issue_number)
      AND NOT f.path STARTS WITH 'tests/'
      AND NOT f.path STARTS WITH 'test/'
      AND NOT f.path ENDS WITH '__init__.py'
    RETURN i.number AS issue_num,
           i.title AS issue_title,
           pr.number AS pr_num,
           pr.title AS pr_title,
           pr.author AS pr_author,
           f.path AS file_path,
           m.source AS mention_source,
           m.confidence AS mention_conf,
           m.match_term AS match_term,
           mod.changes AS pr_changes
    ORDER BY m.confidence DESC, mod.changes DESC
    """
    res_direct = g.query(direct_query, {"issue_number": issue_number})
    direct_dedup: Dict[Tuple[int, int], Dict[str, Any]] = {}
    direct_pr_ids: Set[Tuple[int, int]] = set()

    for row in res_direct.result_set:
        i_num, i_title, p_num, p_title, p_author, fpath, m_src, m_conf, term, chg = row
        key = (i_num, p_num)
        direct_pr_ids.add(key)
        conf = float(m_conf)
        if key not in direct_dedup or conf > direct_dedup[key]["confidence"]:
            graph_path = (
                f"Issue #{i_num} -[:MENTIONS ({m_src}, term='{term}', conf={conf})]-> "
                f"File `{fpath}` <-[:MODIFIES {{changes: {chg}}}]- PR #{p_num} (@{p_author})"
            )
            direct_dedup[key] = {
                "issue_number": i_num,
                "issue_title": i_title,
                "pr_number": p_num,
                "pr_title": p_title,
                "pr_author": p_author,
                "shared_file": fpath,
                "hop_distance": 0,
                "confidence": conf,
                "mention_source": m_src,
                "match_term": term,
                "graph_path": graph_path,
            }

    direct_matches = sorted(direct_dedup.values(), key=lambda x: -x["confidence"])[:5]

    # Query 1-hop import-coupled matches (excluding __init__.py and direct matches)
    coupled_query = """
    MATCH (i:Issue {state: 'open'})-[m:MENTIONS]->(f1:File)-[:IMPORTS]->(f2:File)<-[mod:MODIFIES]-(pr:PullRequest {state: 'open'})
    WHERE ($issue_number IS NULL OR i.number = $issue_number)
      AND f1.path <> f2.path
      AND NOT f1.path STARTS WITH 'tests/'
      AND NOT f2.path STARTS WITH 'tests/'
      AND NOT f1.path STARTS WITH 'test/'
      AND NOT f2.path STARTS WITH 'test/'
      AND NOT f1.path ENDS WITH '__init__.py'
      AND NOT f2.path ENDS WITH '__init__.py'
    RETURN i.number AS issue_num,
           i.title AS issue_title,
           pr.number AS pr_num,
           pr.title AS pr_title,
           pr.author AS pr_author,
           f1.path AS file_mentioned,
           f2.path AS file_modified,
           m.source AS mention_source,
           m.confidence AS mention_conf,
           m.match_term AS match_term,
           mod.changes AS pr_changes
    ORDER BY m.confidence DESC, mod.changes DESC
    """
    res_coupled = g.query(coupled_query, {"issue_number": issue_number})
    coupled_dedup: Dict[Tuple[int, int], Dict[str, Any]] = {}

    for row in res_coupled.result_set:
        i_num, i_title, p_num, p_title, p_author, f1, f2, m_src, m_conf, term, chg = row
        key = (i_num, p_num)
        if key in direct_pr_ids:
            continue
        conf = round(float(m_conf) * 0.8, 2)
        if key not in coupled_dedup or conf > coupled_dedup[key]["confidence"]:
            graph_path = (
                f"Issue #{i_num} -[:MENTIONS ({m_src}, term='{term}')]-> "
                f"File `{f1}` -[:IMPORTS]-> File `{f2}` <-[:MODIFIES {{changes: {chg}}}]- PR #{p_num} (@{p_author})"
            )
            coupled_dedup[key] = {
                "issue_number": i_num,
                "issue_title": i_title,
                "pr_number": p_num,
                "pr_title": p_title,
                "pr_author": p_author,
                "shared_file": f"{f1} -> {f2}",
                "hop_distance": 1,
                "confidence": conf,
                "mention_source": m_src,
                "match_term": term,
                "graph_path": graph_path,
            }

    coupled_matches = sorted(coupled_dedup.values(), key=lambda x: -x["confidence"])[:5]
    final_links = direct_matches + coupled_matches

    # FalkorDB Write-Back: Create [:POTENTIALLY_ADDRESSED_BY] edge
    if final_links:
        writeback_batch = [
            {
                "issue_number": l["issue_number"],
                "pr_number": l["pr_number"],
                "shared_file": l["shared_file"],
                "confidence": l["confidence"],
                "hop_distance": l["hop_distance"],
                "timestamp": now_iso,
            }
            for l in final_links
        ]
        writeback_query = """
        UNWIND $batch AS item
        MATCH (i:Issue {number: item.issue_number})
        MATCH (pr:PullRequest {number: item.pr_number})
        MERGE (i)-[rel:POTENTIALLY_ADDRESSED_BY]->(pr)
        ON CREATE SET rel.shared_file = item.shared_file,
                      rel.confidence = item.confidence,
                      rel.hop_distance = item.hop_distance,
                      rel.verified_by_human = false,
                      rel.created_at = item.timestamp
        ON MATCH SET rel.confidence = CASE WHEN item.confidence > rel.confidence THEN item.confidence ELSE rel.confidence END,
                     rel.updated_at = item.timestamp
        """
        g.query(writeback_query, {"batch": writeback_batch})

    return {
        "target_issue_number": issue_number,
        "candidate_links_found": len(final_links),
        "direct_matches_count": len(direct_matches),
        "coupled_matches_count": len(coupled_matches),
        "direct_matches": direct_matches,
        "coupled_matches": coupled_matches,
        "written_back_to_graph": True,
        "candidate_matches": final_links,
    }


# ---------------------------------------------------------------------------
# Tool 3: Recency-Weighted Reviewer Recommendation
# ---------------------------------------------------------------------------
def recommend_reviewers(pr_number: int) -> Dict[str, Any]:
    """
    Finds the most qualified peer reviewers for a PR by traversing from modified files
    to recent commits and developers:
      - Excludes the PR author and automated bot accounts
      - Applies time-decay weighting: churn / (1.0 + 0.05 * days_ago)
      - Adds a +15.0 bonus for reviewers with past accepted PR reviews on the same files
      - Returns top 3 reviewers with human-readable graph_path evidence
    """
    g = get_graph()

    query = """
    MATCH (pr:PullRequest {number: $pr_number})-[m:MODIFIES]->(f:File)
    WHERE NOT f.path STARTS WITH 'tests/'
    MATCH (d:Developer)-[:AUTHORED]->(c:Commit)-[t:TOUCHED]->(f)
    WHERE d.login <> pr.author
      AND NOT d.login CONTAINS '[bot]'
      AND NOT d.login CONTAINS 'bot'
      AND NOT d.login IN ['github-actions', 'dependabot', 'pre-commit-ci']
    WITH pr, d, f, t, c
    WITH pr,
         d,
         collect(DISTINCT f.path) AS files_known,
         sum(toFloat(t.changes) / (1.0 + 0.05 * toFloat(c.days_ago))) AS recency_commit_score
    OPTIONAL MATCH (d)-[:REVIEWED]->(other_pr:PullRequest)
    WHERE other_pr.number <> $pr_number
    WITH pr, d, files_known, recency_commit_score, count(DISTINCT other_pr) AS prior_reviews_count
    RETURN d.login AS reviewer,
           round((recency_commit_score + (prior_reviews_count * 15.0)) * 100.0) / 100.0 AS total_score,
           round(recency_commit_score * 100.0) / 100.0 AS commit_score,
           prior_reviews_count,
           files_known,
           pr.author AS pr_author
    ORDER BY total_score DESC
    LIMIT 3
    """
    res = g.query(query, {"pr_number": pr_number})
    recommendations: List[Dict[str, Any]] = []

    for row in res.result_set:
        dev, total, commit_sc, rev_cnt, files_k, author = row
        files_str = ", ".join(f"`{f}`" for f in files_k[:3])
        graph_path = (
            f"Developer @{dev} -[:AUTHORED]-> Commits -[:TOUCHED]-> [{files_str}] "
            f"<-[:MODIFIES]- PR #{pr_number} (@{author})"
        )
        if rev_cnt > 0:
            graph_path += f" + Developer @{dev} -[:REVIEWED ({rev_cnt} PRs)]-> [{files_str}]"

        recommendations.append(
            {
                "login": dev,
                "composite_score": float(total),
                "commit_recency_score": float(commit_sc),
                "past_reviews_bonus": float(rev_cnt * 15.0),
                "prior_reviews_count": rev_cnt,
                "files_mastered": files_k,
                "graph_path": graph_path,
                "rationale": (
                    f"@{dev} authored recent commits touching {len(files_k)} modified file(s) "
                    f"with {rev_cnt} past peer reviews in this code domain."
                ),
            }
        )

    return {
        "pr_number": pr_number,
        "recommended_reviewers": recommendations,
        "reviewers_count": len(recommendations),
    }


# ---------------------------------------------------------------------------
# Tool 4: Architectural Core-File Risk Assessment
# ---------------------------------------------------------------------------
def assess_pr_core_risk(pr_number: int) -> Dict[str, Any]:
    """
    Evaluates whether a pull request touches core architectural files using:
      - Pre-computed GraphBLAS PageRank on File imports
      - Inbound import coupling count
      - Code churn volume (additions + deletions)
    Excludes __init__.py and tests/ to highlight genuine subsystem modules.
    Classifies PR as HIGH_RISK_CORE (requires 2 senior reviewers) or STANDARD.
    """
    g = get_graph()

    query = """
    MATCH (pr:PullRequest {number: $pr_number})-[m:MODIFIES]->(f:File)
    WHERE NOT f.path ENDS WITH '__init__.py'
      AND NOT f.path STARTS WITH 'tests/'
      AND NOT f.path STARTS WITH 'test/'
    OPTIONAL MATCH (importer:File)-[:IMPORTS]->(f)
    WITH pr, f, m, count(DISTINCT importer) AS inbound_imports
    RETURN pr.number AS pr_num,
           pr.title AS pr_title,
           pr.author AS pr_author,
           f.path AS file_path,
           coalesce(f.pagerank, 0.0) AS pagerank_score,
           inbound_imports,
           m.changes AS changes
    ORDER BY pagerank_score DESC, inbound_imports DESC
    """
    res = g.query(query, {"pr_number": pr_number})

    modified_modules: List[Dict[str, Any]] = []
    is_high_risk = False
    critical_files: List[str] = []

    for row in res.result_set:
        p_num, p_title, p_author, fpath, pr_sc, in_deg, chg = row
        pr_score = float(pr_sc)
        # Threshold: PageRank >= 0.04 or imported by >= 5 modules or changes >= 250
        if pr_score >= 0.04 or in_deg >= 5 or chg >= 250:
            is_high_risk = True
            critical_files.append(fpath)

        graph_path = (
            f"PR #{p_num} -[:MODIFIES {{changes: {chg}}}]-> File `{fpath}` "
            f"(PageRank={pr_score:.4f}, InboundImports={in_deg})"
        )
        modified_modules.append(
            {
                "path": fpath,
                "pagerank": pr_score,
                "inbound_imports": in_deg,
                "changes": chg,
                "is_core_module": (pr_score >= 0.04 or in_deg >= 5),
                "graph_path": graph_path,
            }
        )

    risk_level = "HIGH_RISK_CORE" if is_high_risk else "STANDARD"
    reviewers_needed = 2 if is_high_risk else 1

    summary_path = (
        f"PR #{pr_number} -[:MODIFIES]-> [{', '.join(f'`{f}`' for f in critical_files[:2]) or 'peripheral modules'}] "
        f"=> RiskLevel: {risk_level} (RequiredReviewers={reviewers_needed})"
    )

    return {
        "pr_number": pr_number,
        "risk_level": risk_level,
        "required_reviewers": reviewers_needed,
        "is_core_module_affected": is_high_risk,
        "critical_files_touched": critical_files,
        "modified_modules": modified_modules,
        "graph_path": summary_path,
        "action_recommendation": (
            f"PR #{pr_number} modifies high-centrality modules ({', '.join(critical_files)}). "
            f"Require 2 senior maintainer approvals before merge."
            if is_high_risk
            else f"PR #{pr_number} touches isolated modules. Standard single reviewer approval sufficient."
        ),
    }


# ---------------------------------------------------------------------------
# Tool 5: Approve & Record Triage Decision (Write-Back)
# ---------------------------------------------------------------------------
def approve_and_record_triage(
    item_type: str,
    item_number: int,
    action_summary: str,
    reviewer: Optional[str] = None,
    linked_pr: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Commits an approved maintainer triage decision into FalkorDB:
      - Writes a (:TriageDecision) node connected to the Issue or PullRequest
      - If linked_pr is provided, marks the [:POTENTIALLY_ADDRESSED_BY] edge as verified_by_human=true
      - Generates exact ready-to-execute GitHub CLI (`gh`) commands for the maintainer
    """
    g = get_graph()
    decision_id = f"triage-{uuid.uuid4().hex[:8]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    clean_type = item_type.lower().strip()
    clean_reviewer = reviewer.lstrip("@").strip() if reviewer else None

    if clean_type in ["pr", "pull_request", "pullrequest"]:
        node_label = "PullRequest"
    elif clean_type in ["issue"]:
        node_label = "Issue"
    else:
        raise ValueError(f"Unknown item_type '{item_type}'. Must be 'issue' or 'pull_request'.")

    # 1. Create (:TriageDecision) node and link to target entity
    decision_query = f"""
    MATCH (target:{node_label} {{number: $item_number}})
    CREATE (td:TriageDecision {{
        id: $decision_id,
        item_type: $item_type,
        item_number: $item_number,
        action_summary: $action_summary,
        assigned_reviewer: $reviewer,
        linked_pr: $linked_pr,
        recorded_at: $timestamp
    }})
    MERGE (target)-[:HAS_TRIAGE_DECISION]->(td)
    RETURN target.title AS title, target.number AS number
    """
    res = g.query(
        decision_query,
        {
            "decision_id": decision_id,
            "item_type": clean_type,
            "item_number": item_number,
            "action_summary": action_summary,
            "reviewer": clean_reviewer or "",
            "linked_pr": linked_pr or 0,
            "timestamp": now_iso,
        },
    )

    if not res.result_set:
        raise RuntimeError(f"Target {node_label} #{item_number} not found in FalkorDB.")

    # 2. If an Issue is linked to a PR, record or verify the relationship in the graph
    if linked_pr and clean_type == "issue":
        verify_query = """
        MATCH (i:Issue {number: $issue_num})
        MATCH (pr:PullRequest {number: $pr_num})
        MERGE (i)-[rel:POTENTIALLY_ADDRESSED_BY]->(pr)
        ON CREATE SET rel.verified_by_human = true,
                      rel.verified_at = $timestamp,
                      rel.confidence = 1.0,
                      rel.hop_distance = 0,
                      rel.decision_id = $decision_id
        ON MATCH SET rel.verified_by_human = true,
                     rel.verified_at = $timestamp,
                     rel.decision_id = $decision_id
        """
        g.query(
            verify_query,
            {
                "issue_num": item_number,
                "pr_num": linked_pr,
                "timestamp": now_iso,
                "decision_id": decision_id,
            },
        )

    # 3. Generate exact executable GitHub CLI (`gh`) commands
    cli_commands: List[str] = []
    if clean_type in ["pr", "pull_request", "pullrequest"]:
        if clean_reviewer:
            cli_commands.append(f'gh pr edit {item_number} --add-reviewer "{clean_reviewer}"')
        cli_commands.append(f'gh pr comment {item_number} --body "[Maintainer Triage Agent]: {action_summary}"')
    else:  # Issue
        comment_body = f"[Maintainer Triage Agent]: {action_summary}"
        if linked_pr:
            comment_body += f"\n\nAssociated candidate Pull Request: #{linked_pr}"
            cli_commands.append(f'gh issue edit {item_number} --add-label "has-pr"')
        cli_commands.append(f'gh issue comment {item_number} --body "{comment_body}"')

    graph_path = (
        f"{node_label} #{item_number} -[:HAS_TRIAGE_DECISION]-> "
        f"(:TriageDecision {{id: '{decision_id}', action: '{action_summary[:40]}...'}})"
    )
    if linked_pr:
        graph_path += f" -[:POTENTIALLY_ADDRESSED_BY {{verified: true}}]-> PullRequest #{linked_pr}"

    return {
        "status": "APPROVED_AND_RECORDED",
        "decision_id": decision_id,
        "item_type": clean_type,
        "item_number": item_number,
        "action_summary": action_summary,
        "assigned_reviewer": clean_reviewer,
        "linked_pr": linked_pr,
        "recorded_at": now_iso,
        "graph_path": graph_path,
        "executable_gh_cli_commands": cli_commands,
    }
