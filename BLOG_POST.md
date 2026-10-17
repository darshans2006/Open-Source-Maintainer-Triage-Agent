# Why Vector Search Fails Open-Source Maintainers: Building a Load-Bearing Knowledge Graph with FalkorDB & FastMCP

*Published for Graph Hacks: Context for AI Agents (Track 1: Agents That Act on Connected Data)*  
*Tags: `falkordb`, `ai`, `python`, `opensource`, `fastmcp`, `graphhacks`*

---

## The Open-Source Maintainer Dilemma

If you have ever maintained a popular open-source repository with thousands of stars, you know the daily feeling of dread when opening GitHub notifications:
- **30+ open pull requests**, all competing for review time.
- **50+ open issues**, many describing symptoms of the exact same bug in different words.
- Two contributors submit PRs touching completely different files, both pass CI independently, and once merged into `main`, **production breaks due to an import-coupled breaking change**.

The common industry response right now is to reach for LLMs and vector search: chunk the issues and diffs, generate embeddings, and run cosine similarity.

However, in practice, **vector search fundamentally struggles with codebase triage**.

---

## Why Code Repositories Defy Vector Embeddings

Embeddings capture linguistic similarity in natural language. But a codebase is governed by **graph topology and version control dynamics**:

1. **Import Coupling is Invisible to Text Search**: If PR A alters a function signature in `httpx/_transports/default.py`, and PR B calls that function from `httpx/_client.py`, their diffs share virtually zero textual similarity. A vector database assigns them a low similarity score. Yet in reality, they are **one dependency hop away from breaking main**.
2. **Reviewer Expertise is a Network with Recency Decay**: Recommending the right reviewer isn't a text classification problem. It requires traversing who authored commits touching those exact files over the last 90 days, factoring in line churn, and weighting past review approvals in that specific subsystem.
3. **Auditable Evidence vs. Hallucinated Hunches**: When an agent suggests that PR #3741 blocks PR #3778, maintainers need verifiable proof. A graph traversal provides the exact sequence of edges (`PR1 -> FileA -> IMPORTS -> FileB <- PR2`), whereas an LLM prompt provides only probabilistic text.

To address this, we designed the **Open-Source Maintainer Triage Agent** around **FalkorDB**, treating graph topology as the primary source of truth rather than an afterthought.

---

## The Core Philosophy: Zero Embeddings, 100% Graph Topology

Our agent uses **zero text embeddings**. Instead, it models repository reality as a rich graph inside FalkorDB:

```
(:Developer)-[:AUTHORED]->(:Commit)-[:TOUCHED {changes, days_ago}]->(:File)
(:File)-[:IMPORTS]->(:File)
(:PullRequest)-[:MODIFIES {changes}]->(:File)
(:Developer)-[:REVIEWED]->(:PullRequest)
(:Issue)-[:MENTIONS {source, confidence}]->(:File)
(:Issue)-[:POTENTIALLY_ADDRESSED_BY {confidence, hops}]->(:PullRequest)
(:PullRequest)-[:HAS_TRIAGE_DECISION]->(:TriageDecision)
```

We ingested the real, production codebase of [`encode/httpx`](https://github.com/encode/httpx) into a live FalkorDB instance:
- **60 tracked Python files**
- **122 AST import edges**
- **33 open pull requests**
- **50 open issues**
- **63 unique developers**
- **123 commits**

Every relationship is computed deterministically in under **15 milliseconds**.

---

## Real Discoveries on `encode/httpx`

Here are 4 concrete, real-world examples discovered by our agent on live `encode/httpx` data:

### 1. The Hidden 1-Hop Collision: PR #3741 ↔ PR #3778
A maintainer triaging PR #3741 (`Fix no_proxy to support IPV6 CIDR format` by `@p3ck`) might only look for other PRs editing `httpx/_client.py`.
FalkorDB traversed 1 hop deeper across `[:IMPORTS]` and caught an immediate dependency collision with PR #3778 (`Refactor map_httpcore_exceptions` by `@akx`):

```
PullRequest #3741 (@p3ck) 
  -[:MODIFIES]-> File `httpx/_client.py` 
  -[:IMPORTS]-> File `httpx/_transports/default.py` 
  <-[:MODIFIES]- PullRequest #3778 (@akx)
```
`httpx/_client.py` imports `httpx/_transports/default.py`. If both PRs merge without joint testing, the client runtime breaks.

### 2. Structural Issue Resolver: Issue #3728 → PR #3769
Maintainers often have open issues that contributors independently attempt to solve without cross-referencing issue numbers.
- **Issue #3728** reported: `RuntimeError: Attempted to exit a cancel scope that isn't the current task's current cancel scope`, mentioning `httpx/_client.py`.
- **PR #3769** by `@bysiber` had the title: `Ensure mounted transports are closed even if main transport raises`.
- Through deterministic graph traversal, FalkorDB matched the exact modified module and linked the issue to the PR with **0.95 confidence**, writing `[:POTENTIALLY_ADDRESSED_BY]` directly back into the graph.

### 3. Native GraphBLAS PageRank Architectural Risk
Not all files are created equal. Touching `httpx/__version__.py` is low-risk. Touching `httpx/_models.py` or `httpx/_utils.py` affects the entire ecosystem.
FalkorDB executed GraphBLAS PageRank in memory:
- `httpx/_models.py`: PageRank **0.0773** (In-degree 13)
- `httpx/_types.py`: PageRank **0.0598** (In-degree 12)
- `httpx/_utils.py`: PageRank **0.0383** (In-degree 7)

When PR #3741 modified `httpx/_utils.py`, our agent automatically classified it as **`HIGH_RISK_CORE`**, triggering a policy recommendation: **2 senior maintainer approvals recommended before merging** (with executable `gh` CLI commands generated for reviewer assignments, while hard merge blocking remains governed by GitHub Branch Protection Rules or Repository Rulesets).

### 4. Recency-Decayed Reviewer Scoring
To recommend reviewers for PR #3741, FalkorDB traversed commits touching `httpx/_client.py` and `httpx/_utils.py`, filtering out bots and the PR author, applying continuous time-decay:
$$\text{Score} = \sum \frac{\text{changes}}{1.0 + 0.05 \times \text{days}_{\text{ago}}} + 15.0 \times \text{prior}_{\text{reviews}}$$

Top recommendations:
1. `@lovelydinosaur` (Score: 41.73, with +30.0 peer review bonus)
2. `@karpetrosyan` (Score: 12.37)
3. `@RafaelWO` (Score: 4.76)

---

## Under the Hood: The Cypher Walkthrough

Here is the exact Cypher query used by our `link_issues_to_prs` FastMCP tool:

```cypher
// 1. Purge stale unverified candidate links to closed PRs
MATCH (i:Issue {number: $issue_number})-[rel:POTENTIALLY_ADDRESSED_BY]->(pr:PullRequest)
WHERE coalesce(rel.verified_by_human, false) = false
  AND pr.state <> 'open'
DELETE rel;

// 2. Traverse and link active candidate PRs
MATCH (i:Issue {number: $issue_number})-[m:MENTIONS]->(f1:File)
MATCH (f1)-[imp:IMPORTS*0..1]->(f2:File)<-[mod:MODIFIES]-(pr:PullRequest {state: 'open'})
WHERE NOT f2.path STARTS WITH 'tests/'
  AND (length(imp) = 0 OR NOT f1.path ENDS WITH '__init__.py')

WITH i, pr, f1, f2, m, mod, length(imp) AS hop_distance,
     CASE WHEN length(imp) = 0 THEN m.confidence ELSE m.confidence * 0.8 END AS calc_confidence
ORDER BY calc_confidence DESC, mod.changes DESC

// Write-back candidate relationship into FalkorDB
MERGE (i)-[rel:POTENTIALLY_ADDRESSED_BY]->(pr)
ON CREATE SET rel.confidence = calc_confidence,
              rel.hop_distance = hop_distance,
              rel.shared_file = f1.path,
              rel.verified_by_human = false,
              rel.created_at = timestamp()
ON MATCH SET rel.confidence = CASE WHEN coalesce(rel.verified_by_human, false) THEN 1.0 ELSE calc_confidence END,
             rel.hop_distance = hop_distance,
             rel.shared_file = f1.path,
             rel.updated_at = timestamp()
RETURN pr.number, pr.title, pr.author, calc_confidence, hop_distance, f1.path, f2.path
```

Notice the power of this single Cypher query:
1. It handles **both 0-hop direct file matches and 1-hop import matches**.
2. It excludes ubiquitous bridge nodes (`__init__.py`) to prevent false-positive explosions.
3. It performs a **live graph mutation** (`MERGE (i)-[rel:POTENTIALLY_ADDRESSED_BY]->(pr)`), storing candidate connections for human verification.
4. It executes in **under 12 milliseconds**.

---

## The Human-in-the-Loop Streamlit Dashboard

AI agents shouldn't act as unchecked black boxes. In our Streamlit dashboard ([`app.py`](./app.py)):

1. **Top KPI Bar**: Maintainers see real-time counts of files, imports, PRs, linked issues (78%), and active collisions.
2. **Tab 1 (PR Triage Queue)**: PRs are sorted with `HIGH_RISK_CORE` first. Selecting any PR renders the **Explainable Graph Decision Panel** with reviewer rankings, collision radar, and an "Approve & Record" form.
3. **Tab 2 (50-Issue Linker)**: Ingested issues are resolved to candidate PRs. Clicking "Confirm Link" updates FalkorDB and outputs ready-to-run GitHub CLI (`gh`) commands:
   ```bash
   gh pr edit 3741 --add-reviewer "karpetrosyan"
   gh pr comment 3741 --body "[Maintainer Triage Agent]: Approved risk assessment..."
   ```
4. **Tab 3 (FastMCP Tool Inspector)**: Lets developers interactively execute all 5 tools over FastMCP and inspect live `(:TriageDecision)` write-backs.

---

## Data Validation Results

| Benchmark Metric | Result |
| :--- | :---: |
| **Tracked Python Modules** | 60 files |
| **Internal Import Topology** | 122 edges |
| **Open Pull Requests Ingested** | 33 PRs |
| **Open Issues Processed** | 50 issues |
| **Issues Deterministically Linked** | **39 / 50 (78.0%)** |
| **Active Core PR Collisions** | 7 files |
| **Average Cypher Execution Latency** | **< 15 ms** |
| **Vector Embeddings Used** | **0** |

---

## Key Takeaways for AI Agent Developers

Building agents on connected data taught us three essential lessons:

1. **Structure Trumps Semantics for Code**: Relationships (who imports what, who touched what file) provide 10x higher signal than semantic vector similarity.
2. **Deterministic Graphs Enable Auditable AI**: Every recommendation our agent makes comes with a concrete `graph_path` string that a maintainer can inspect and trust.
3. **FalkorDB is Uniquely Suited for Agent Context**: With in-memory GraphBLAS execution, Cypher expressiveness, and sub-15ms response times, FalkorDB provides the deterministic reasoning backbone modern AI agents desperately need.

Check out the full repository and run it locally in under 3 minutes:  
🔗 [GitHub: Open-Source Maintainer Triage Agent](https://github.com/darshans2006/Open-Source-Maintainer-Triage-Agent)
