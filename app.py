"""
Streamlit maintainer dashboard for repository triage and graph exploration.

Displays:
  - Repository health KPIs (tracked files, imports, PRs, linked issues, active collisions)
  - Prioritized PR triage queue with explainable graph collision and reviewer recommendations
  - Issue-to-PR structural linking interface with audit trail write-back
  - Interactive FastMCP inspection console and live graph state explorer
"""

import time
import os
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

import graph_tools

load_dotenv()

st.set_page_config(
    page_title="Open-Source Maintainer Triage Agent",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Custom Styling
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    .main-header {
        font-size: 2.1rem;
        font-weight: 700;
        letter-spacing: -0.5px;
        margin-bottom: 0px;
    }
    .sub-header {
        font-size: 1.05rem;
        color: #94a3b8;
        margin-top: 2px;
        margin-bottom: 20px;
    }
    .kpi-card {
        background-color: #1e293b;
        border: 1px solid #334155;
        border-radius: 10px;
        padding: 14px 18px;
        text-align: left;
    }
    .kpi-value {
        font-size: 1.8rem;
        font-weight: 700;
        color: #38bdf8;
    }
    .kpi-label {
        font-size: 0.85rem;
        color: #94a3b8;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .graph-path-box {
        background-color: #0f172a;
        border-left: 4px solid #38bdf8;
        padding: 10px 14px;
        border-radius: 4px;
        font-family: monospace;
        font-size: 0.88rem;
        color: #f1f5f9;
        margin-top: 6px;
        margin-bottom: 8px;
        overflow-x: auto;
    }
    .badge-high {
        background-color: #ef4444;
        color: white;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .badge-standard {
        background-color: #3b82f6;
        color: white;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 0.78rem;
        font-weight: 600;
    }
    .badge-conf {
        background-color: #10b981;
        color: white;
        padding: 2px 7px;
        border-radius: 5px;
        font-size: 0.75rem;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Data Loaders
# ---------------------------------------------------------------------------
@st.cache_data(ttl=15)
def get_kpi_summary():
    """Queries live FalkorDB for top KPI summary metrics."""
    g = graph_tools.get_graph()
    num_files = g.query("MATCH (f:File) RETURN count(f)").result_set[0][0]
    num_imports = g.query("MATCH ()-[r:IMPORTS]->() RETURN count(r)").result_set[0][0]
    num_prs = g.query("MATCH (p:PullRequest) RETURN count(p)").result_set[0][0]
    num_issues = g.query("MATCH (i:Issue) RETURN count(i)").result_set[0][0]
    linked_issues = g.query("MATCH (i:Issue)-[:MENTIONS]->(:File) RETURN count(DISTINCT i)").result_set[0][0]

    # Active file collisions on core .py files
    col_res = g.query(
        """
        MATCH (p1:PullRequest {state: 'open'})-[m1:MODIFIES]->(f:File)<-[m2:MODIFIES]-(p2:PullRequest {state: 'open'})
        WHERE p1.number < p2.number
          AND NOT f.path STARTS WITH 'tests/'
          AND NOT f.path STARTS WITH 'test/'
          AND NOT f.path ENDS WITH '__init__.py'
        RETURN count(DISTINCT f.path)
        """
    )
    collisions_count = col_res.result_set[0][0]

    return {
        "files": num_files,
        "imports": num_imports,
        "prs": num_prs,
        "issues": num_issues,
        "linked_issues": linked_issues,
        "link_pct": round((linked_issues / max(1, num_issues)) * 100, 1),
        "collisions": collisions_count,
    }


@st.cache_data(ttl=15)
def get_all_prs_table():
    """Fetches all open PRs with core risk flags and collision counts."""
    g = graph_tools.get_graph()
    query = """
    MATCH (pr:PullRequest {state: 'open'})
    OPTIONAL MATCH (pr)-[m:MODIFIES]->(f:File)
    WHERE NOT f.path STARTS WITH 'tests/'
      AND NOT f.path ENDS WITH '__init__.py'
    OPTIONAL MATCH (other:PullRequest {state: 'open'})-[m2:MODIFIES]->(f)
    WHERE other.number <> pr.number
    WITH pr,
         count(DISTINCT f) AS core_files_mod,
         max(coalesce(f.pagerank, 0.0)) AS max_pr,
         count(DISTINCT other) AS collisions_count
    RETURN pr.number AS number,
           pr.title AS title,
           pr.author AS author,
           pr.base_branch AS base_branch,
           core_files_mod,
           max_pr,
           collisions_count,
           CASE WHEN max_pr >= 0.04 OR collisions_count > 0 THEN 'HIGH_RISK_CORE' ELSE 'STANDARD' END AS risk_level
    ORDER BY risk_level DESC, collisions_count DESC, max_pr DESC, pr.number DESC
    """
    res = g.query(query)
    rows = []
    for r in res.result_set:
        rows.append(
            {
                "PR #": int(r[0]),
                "Title": str(r[1]),
                "Author": str(r[2]),
                "Base": str(r[3]),
                "Core Files": int(r[4]),
                "Max PageRank": round(float(r[5]), 4),
                "Collisions": int(r[6]),
                "Risk Level": str(r[7]),
            }
        )
    return pd.DataFrame(rows)


@st.cache_data(ttl=15)
def get_all_issues_table():
    """Fetches all 50 open issues sorted by link confidence."""
    g = graph_tools.get_graph()
    query = """
    MATCH (i:Issue)
    OPTIONAL MATCH (i)-[m:MENTIONS]->(f:File)
    RETURN i.number AS number,
           i.title AS title,
           max(coalesce(m.confidence, 0.0)) AS max_conf,
           collect(DISTINCT m.source)[0] AS primary_source,
           count(DISTINCT f) AS linked_files
    ORDER BY max_conf DESC, linked_files DESC, i.number DESC
    """
    res = g.query(query)
    rows = []
    for r in res.result_set:
        conf = float(r[2])
        source = str(r[3]) if r[3] else "None"
        category = "Unlinked"
        if conf >= 0.95:
            category = "0.95 (Explicit Path)"
        elif conf >= 0.90:
            category = "0.90 (Stack Trace)"
        elif conf >= 0.70:
            category = "0.70 (Symbol Match)"

        rows.append(
            {
                "Issue #": int(r[0]),
                "Title": str(r[1]),
                "Confidence": conf,
                "Match Type": category,
                "Linked Files": int(r[4]),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Header & KPI Bar
# ---------------------------------------------------------------------------
st.markdown('<div class="main-header">⚡ Open-Source Maintainer Triage Agent</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">Graph-native pull request and issue triage powered by FalkorDB</div>',
    unsafe_allow_html=True,
)

kpis = get_kpi_summary()
k1, k2, k3, k4, k5 = st.columns(5)
with k1:
    st.markdown(
        f'<div class="kpi-card"><div class="kpi-label">Tracked Python Files</div><div class="kpi-value">{kpis["files"]}</div></div>',
        unsafe_allow_html=True,
    )
with k2:
    st.markdown(
        f'<div class="kpi-card"><div class="kpi-label">Internal Import Edges</div><div class="kpi-value">{kpis["imports"]}</div></div>',
        unsafe_allow_html=True,
    )
with k3:
    st.markdown(
        f'<div class="kpi-card"><div class="kpi-label">Open Pull Requests</div><div class="kpi-value">{kpis["prs"]}</div></div>',
        unsafe_allow_html=True,
    )
with k4:
    st.markdown(
        f'<div class="kpi-card"><div class="kpi-label">Open Issues Linked</div><div class="kpi-value">{kpis["linked_issues"]} / {kpis["issues"]} <span style="font-size:1.1rem;color:#10b981;">({kpis["link_pct"]}%)</span></div></div>',
        unsafe_allow_html=True,
    )
with k5:
    st.markdown(
        f'<div class="kpi-card"><div class="kpi-label">Active PR Collisions</div><div class="kpi-value" style="color:#ef4444;">{kpis["collisions"]} Files</div></div>',
        unsafe_allow_html=True,
    )

st.markdown("<div style='height: 18px;'></div>", unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Navigation Tabs
# ---------------------------------------------------------------------------
tab_prs, tab_issues, tab_inspector = st.tabs(
    [
        "📋 Prioritized PR Triage Queue",
        "🔍 Issue Triage & Structural PR Linker",
        "🛠️ FastMCP Tools & Graph Explorer",
    ]
)

# ---------------------------------------------------------------------------
# TAB 1: Prioritized PR Triage Queue
# ---------------------------------------------------------------------------
with tab_prs:
    st.subheader("Open Pull Requests — Prioritized Maintainer Queue")
    df_prs = get_all_prs_table()

    col_table, col_detail = st.columns([1.1, 1.4])

    with col_table:
        st.markdown("**Select a Pull Request to inspect explainable graph triage recommendations:**")
        pr_numbers = df_prs["PR #"].tolist()
        default_index = pr_numbers.index(3741) if 3741 in pr_numbers else 0
        selected_pr = st.selectbox("Choose PR Number:", pr_numbers, index=default_index)

        st.dataframe(
            df_prs,
            column_config={
                "PR #": st.column_config.NumberColumn(format="%d"),
                "Risk Level": st.column_config.TextColumn(help="HIGH_RISK_CORE requires 2 senior reviewers"),
            },
            hide_index=True,
            use_container_width=True,
            height=460,
        )

    with col_detail:
        if selected_pr:
            st.markdown(f"### 🛡️ Triage Decision Panel: PR #{selected_pr}")

            # Run deterministic graph tools
            col_data = graph_tools.detect_pr_collisions(selected_pr)
            rev_data = graph_tools.recommend_reviewers(selected_pr)
            risk_data = graph_tools.assess_pr_core_risk(selected_pr)

            # Card 1: Core Architectural Risk
            risk_badge = (
                '<span class="badge-high">HIGH_RISK_CORE (2 Approvals Required)</span>'
                if risk_data["risk_level"] == "HIGH_RISK_CORE"
                else '<span class="badge-standard">STANDARD (1 Approval Sufficient)</span>'
            )
            st.markdown(f"**Architectural Subsystem Risk:** {risk_badge}", unsafe_allow_html=True)
            st.markdown(f"*Rationale:* {risk_data['action_recommendation']}")
            st.markdown(f'<div class="graph-path-box">{risk_data["graph_path"]}</div>', unsafe_allow_html=True)

            # Card 2: Collision Radar
            st.markdown("#### 💥 PR Collision Radar")
            if col_data["total_collisions"] > 0:
                st.caption(
                    f"Detected **{col_data['high_severity_direct']} Direct File Overlaps** and **{col_data['medium_severity_coupled']} Import-Coupled Collisions**:"
                )
                for c in col_data["collisions"][:3]:
                    icon = "🚨" if c["severity"] == "HIGH" else "⚠️"
                    st.markdown(f"{icon} **{c['collision_type']}** ({c['severity']} Risk) — Churn: `{c['combined_churn']}`")
                    st.markdown(f'<div class="graph-path-box">{c["graph_path"]}</div>', unsafe_allow_html=True)
            else:
                st.success("✓ No direct or coupled collisions detected on core Python modules.")

            # Card 3: Recommended Reviewers
            st.markdown("#### 👥 Recency-Weighted Reviewer Recommendations")
            if rev_data["recommended_reviewers"]:
                for r in rev_data["recommended_reviewers"]:
                    st.markdown(
                        f"**@{r['login']}** — *Expertise Score:* `{r['composite_score']}` "
                        f"(Commit Churn: `{r['commit_recency_score']}`, Prior Reviews Bonus: `+{r['past_reviews_bonus']}`)"
                    )
                    st.markdown(f'<div class="graph-path-box">{r["graph_path"]}</div>', unsafe_allow_html=True)
            else:
                st.info("No prior non-bot committers found for modified files.")

            # Human-in-the-Loop Action Box
            st.markdown("---")
            st.markdown("#### ✍️ Human-in-the-Loop: Commit Triage Decision")
            with st.form(f"triage_form_{selected_pr}"):
                top_reviewer = rev_data["recommended_reviewers"][0]["login"] if rev_data["recommended_reviewers"] else ""
                assigned_reviewer = st.text_input("Assigned Reviewer Handle:", value=top_reviewer)
                summary_action = st.text_input(
                    "Triage Action Summary:",
                    value=f"Approved risk assessment ({risk_data['risk_level']}). Assign reviewer @{assigned_reviewer} based on graph traversal.",
                )
                submit_button = st.form_submit_button("✅ Approve & Record Triage Decision")

                if submit_button:
                    decision = graph_tools.approve_and_record_triage(
                        item_type="pull_request",
                        item_number=selected_pr,
                        action_summary=summary_action,
                        reviewer=assigned_reviewer,
                    )
                    st.toast(f"Triage Decision recorded in FalkorDB! ID: {decision['decision_id']}", icon="🎉")
                    st.success(f"✓ Decision successfully written to FalkorDB: `(:TriageDecision {{id: '{decision['decision_id']}'}})`")
                    st.markdown("**Ready-to-run GitHub CLI command:**")
                    for cmd in decision["executable_gh_cli_commands"]:
                        st.code(cmd, language="bash")


# ---------------------------------------------------------------------------
# TAB 2: 50-Issue Triage Queue & Structural PR Linker
# ---------------------------------------------------------------------------
with tab_issues:
    st.subheader("50 Ingested Open Issues — Structural PR Resolver")
    df_issues = get_all_issues_table()

    c_issues_tbl, c_issues_res = st.columns([1.1, 1.4])

    with c_issues_tbl:
        st.markdown("**Select an Issue to resolve candidate Pull Requests via deterministic graph traversal:**")
        issue_nums = df_issues["Issue #"].tolist()
        def_issue_idx = issue_nums.index(3728) if 3728 in issue_nums else 0
        selected_issue = st.selectbox("Choose Issue Number:", issue_nums, index=def_issue_idx)

        st.dataframe(
            df_issues,
            column_config={
                "Issue #": st.column_config.NumberColumn(format="%d"),
                "Confidence": st.column_config.ProgressColumn(min_value=0.0, max_value=1.0, format="%.2f"),
            },
            hide_index=True,
            use_container_width=True,
            height=460,
        )

    with c_issues_res:
        if selected_issue:
            st.markdown(f"### 🔗 Structural Resolution: Issue #{selected_issue}")

            # Run link_issues_to_prs with write-back
            links_result = graph_tools.link_issues_to_prs(selected_issue)

            if links_result["candidate_links_found"] > 0:
                st.markdown(
                    f"Discovered **{links_result['candidate_links_found']} candidate PRs** addressing Issue #{selected_issue} "
                    f"via multi-hop File mentions (written to FalkorDB as `[:POTENTIALLY_ADDRESSED_BY]`):"
                )

                for match in links_result["candidate_matches"][:4]:
                    conf_badge = f'<span class="badge-conf">Confidence {match["confidence"]:.2f} ({match["hop_distance"]} hops)</span>'
                    st.markdown(f"**PR #{match['pr_number']}:** {match['pr_title']} — {conf_badge}", unsafe_allow_html=True)
                    st.markdown(f"*Author:* `@{match['pr_author']}` | *Shared Entity:* `{match['shared_file']}`")
                    st.markdown(f'<div class="graph-path-box">{match["graph_path"]}</div>', unsafe_allow_html=True)

                st.markdown("---")
                st.markdown("#### 🎯 Human-in-the-Loop: Approve Issue-to-PR Link")
                with st.form(f"approve_link_form_{selected_issue}"):
                    best_pr = links_result["candidate_matches"][0]["pr_number"]
                    chosen_pr = st.number_input("Candidate PR Number to Link:", value=int(best_pr), step=1)
                    link_note = st.text_input(
                        "Resolution Note:",
                        value=f"Verified that PR #{chosen_pr} addresses Issue #{selected_issue} through modified client modules.",
                    )
                    approve_link = st.form_submit_button("🔗 Confirm & Commit Issue-to-PR Link")

                    if approve_link:
                        dec = graph_tools.approve_and_record_triage(
                            item_type="issue",
                            item_number=selected_issue,
                            action_summary=link_note,
                            linked_pr=int(chosen_pr),
                        )
                        st.toast(f"Link verified & committed in FalkorDB! Decision: {dec['decision_id']}", icon="✅")
                        st.success(f"✓ Updated `[:POTENTIALLY_ADDRESSED_BY {{verified_by_human: true}}]` for Issue #{selected_issue} -> PR #{chosen_pr}")
                        st.markdown("**Executable GitHub CLI command:**")
                        for cmd in dec["executable_gh_cli_commands"]:
                            st.code(cmd, language="bash")
            else:
                st.info(f"No active pull requests modify files mentioned by Issue #{selected_issue}.")


# ---------------------------------------------------------------------------
# TAB 3: FastMCP Tools & Graph Explorer
# ---------------------------------------------------------------------------
with tab_inspector:
    st.subheader("Live MCP Tool Inspector & Graph State Explorer")
    st.caption("Directly test FastMCP triage tools and inspect real-time write-back nodes in FalkorDB.")

    col_tool_test, col_db_state = st.columns([1.1, 1.4])

    with col_tool_test:
        st.markdown("#### 🛠️ Test MCP Tool Calling")
        tool_choice = st.selectbox(
            "Select MCP Tool:",
            [
                "detect_pr_collisions",
                "link_issues_to_prs",
                "recommend_reviewers",
                "assess_pr_core_risk",
                "approve_and_record_triage",
            ],
        )

        input_pr = st.number_input("PR Number:", value=3741, step=1)
        input_issue = st.number_input("Issue Number:", value=3728, step=1)

        if st.button("🚀 Execute Tool via Cypher"):
            start_t = time.perf_counter()
            tool_output = {}

            if tool_choice == "detect_pr_collisions":
                tool_output = graph_tools.detect_pr_collisions(int(input_pr))
            elif tool_choice == "link_issues_to_prs":
                tool_output = graph_tools.link_issues_to_prs(int(input_issue))
            elif tool_choice == "recommend_reviewers":
                tool_output = graph_tools.recommend_reviewers(int(input_pr))
            elif tool_choice == "assess_pr_core_risk":
                tool_output = graph_tools.assess_pr_core_risk(int(input_pr))
            elif tool_choice == "approve_and_record_triage":
                tool_output = graph_tools.approve_and_record_triage(
                    item_type="pull_request",
                    item_number=int(input_pr),
                    action_summary="Live tool inspector triage action",
                )

            duration_ms = round((time.perf_counter() - start_t) * 1000, 2)
            st.success(f"✓ Executed in **{duration_ms} ms** against FalkorDB.")
            st.json(tool_output)

    with col_db_state:
        st.markdown("#### 💾 Live FalkorDB Session Write-Backs")
        g = graph_tools.get_graph()

        st.markdown("**Recent `(:TriageDecision)` Nodes Committed to FalkorDB:**")
        decisions_res = g.query(
            """
            MATCH (td:TriageDecision)
            RETURN td.id AS id,
                   td.item_type AS item_type,
                   td.item_number AS item_num,
                   td.action_summary AS summary,
                   td.assigned_reviewer AS reviewer,
                   td.linked_pr AS linked_pr,
                   td.recorded_at AS timestamp
            ORDER BY td.recorded_at DESC
            LIMIT 10
            """
        )
        if decisions_res.result_set:
            dec_rows = [
                {
                    "Decision ID": str(r[0]),
                    "Type": str(r[1]),
                    "#": int(r[2]),
                    "Action Summary": str(r[3]),
                    "Reviewer": str(r[4]),
                    "Linked PR": int(r[5]) if r[5] else None,
                    "Timestamp": str(r[6]),
                }
                for r in decisions_res.result_set
            ]
            st.dataframe(pd.DataFrame(dec_rows), hide_index=True, use_container_width=True)
        else:
            st.info("No triage decisions recorded in this database session yet.")

        st.markdown("**Active `[:POTENTIALLY_ADDRESSED_BY]` Edges in FalkorDB:**")
        links_res = g.query(
            """
            MATCH (i:Issue)-[r:POTENTIALLY_ADDRESSED_BY]->(pr:PullRequest)
            RETURN i.number AS issue,
                   pr.number AS pr,
                   r.confidence AS confidence,
                   r.hop_distance AS hops,
                   r.shared_file AS shared_file,
                   r.verified_by_human AS verified
            ORDER BY r.verified_by_human DESC, r.confidence DESC
            LIMIT 10
            """
        )
        if links_res.result_set:
            link_rows = [
                {
                    "Issue #": int(r[0]),
                    "PR #": int(r[1]),
                    "Confidence": float(r[2]),
                    "Hops": int(r[3]),
                    "Shared File": str(r[4]),
                    "Verified": bool(r[5]),
                }
                for r in links_res.result_set
            ]
            st.dataframe(pd.DataFrame(link_rows), hide_index=True, use_container_width=True)
        else:
            st.info("No Issue-to-PR links written to the graph yet.")
