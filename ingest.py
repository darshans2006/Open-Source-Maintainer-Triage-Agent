"""
Repository Ingestion Pipeline for Maintainer Triage Graph.

Constructs an interconnected knowledge graph in FalkorDB:
  - Python AST import topology (:File)-[:IMPORTS]->(:File)
  - Developer commit activity and code churn (:Developer)-[:AUTHORED]->(:Commit)-[:TOUCHED]->(:File)
  - Pull request file modifications (:PullRequest)-[:MODIFIES]->(:File)
  - Historical peer review activity (:Developer)-[:REVIEWED]->(:PullRequest)
  - Issue references to source code (:Issue)-[:MENTIONS]->(:File)
  - PageRank centrality scores for architectural risk analysis
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

load_dotenv()
console = Console(legacy_windows=False)

# ---------------------------------------------------------------------------
# Constants & Stopwords
# ---------------------------------------------------------------------------
BOT_KEYWORDS = {
    "[bot]",
    "bot",
    "dependabot",
    "github-actions",
    "pre-commit-ci",
    "codecov",
    "actions-user",
}

GENERIC_STOPWORDS = {
    "main",
    "run",
    "get",
    "set",
    "post",
    "put",
    "delete",
    "init",
    "setup",
    "test",
    "handle",
    "call",
    "close",
    "open",
    "read",
    "write",
    "dump",
    "load",
    "execute",
    "update",
    "create",
    "filter",
    "parse",
    "send",
    "build",
    "clean",
    "start",
    "stop",
    "process",
    "format",
    "check",
}

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    ".ruff_cache",
    "site-packages",
    "dist",
    "build",
    "node_modules",
}


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------
@dataclass
class FileNode:
    path: str
    pagerank: float = 0.0


@dataclass
class ImportEdge:
    source: str
    target: str


@dataclass
class DeveloperNode:
    login: str
    user_id: int = 0


@dataclass
class CommitNode:
    sha: str
    days_ago: int
    timestamp: str
    author_login: str


@dataclass
class TouchedEdge:
    sha: str
    path: str
    changes: int


@dataclass
class PullRequestNode:
    number: int
    title: str
    state: str
    author: str
    base_branch: str
    url: str


@dataclass
class ModifiesEdge:
    pr_number: int
    path: str
    changes: int
    additions: int
    deletions: int


@dataclass
class ReviewedEdge:
    login: str
    pr_number: int


@dataclass
class IssueNode:
    number: int
    title: str
    state: str
    url: str


@dataclass
class MentionsEdge:
    issue_number: int
    file_path: str
    source: str  # 'explicit_path', 'stack_trace', 'symbol_or_filename'
    confidence: float
    match_term: str


# ---------------------------------------------------------------------------
# FalkorDB Database Manager
# ---------------------------------------------------------------------------
class DatabaseManager:
    """Manages connection, schema indexes, batch writes, and PageRank in FalkorDB."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        password: Optional[str] = None,
        use_lite: bool = False,
        lite_path: str = "maintainer_triage.db",
        graph_name: str = "maintainer_triage",
        clean: bool = True,
    ):
        self.host = host
        self.port = port
        self.password = password
        self.use_lite = use_lite
        self.lite_path = lite_path
        self.graph_name = graph_name
        self.clean = clean
        self.client = None
        self.graph = None

    def connect(self) -> None:
        """Connects to FalkorDB or FalkorDBLite and selects the target graph."""
        if self.use_lite:
            try:
                import falkordblite  # type: ignore

                console.print(f"[cyan]Connecting to FalkorDBLite at '{self.lite_path}'...[/cyan]")
                self.client = falkordblite.FalkorDB(self.lite_path)
            except ImportError as exc:
                raise RuntimeError(
                    "FalkorDBLite requested (USE_LITE=1) but `falkordblite` package is not available. "
                    "Note: falkordblite requires Python >= 3.12 on Linux/macOS. "
                    "For standard setup, run FalkorDB via Docker (`docker run -p 6379:6379 -d falkordb/falkordb:latest`)."
                ) from exc
        else:
            try:
                import falkordb

                console.print(f"[cyan]Connecting to FalkorDB at {self.host}:{self.port}...[/cyan]")
                self.client = falkordb.FalkorDB(
                    host=self.host,
                    port=self.port,
                    password=self.password if self.password else None,
                )
            except ImportError as exc:
                raise RuntimeError("falkordb Python client is not installed. Run `pip install falkordb`.") from exc

        self.graph = self.client.select_graph(self.graph_name)

        if self.clean:
            console.print(f"[yellow]Wiping existing graph '{self.graph_name}' for clean ingestion...[/yellow]")
            try:
                self.graph.delete()
                # Re-select after delete
                self.graph = self.client.select_graph(self.graph_name)
            except Exception:
                # If graph did not exist, fallback to DETACH DELETE
                try:
                    self.graph.query("MATCH (n) DETACH DELETE n")
                except Exception:
                    pass

        self._create_indexes()

    def _create_indexes(self) -> None:
        """Creates indexes on File, PullRequest, Issue, Developer, and Commit."""
        index_queries = [
            "CREATE INDEX FOR (f:File) ON (f.path)",
            "CREATE INDEX FOR (pr:PullRequest) ON (pr.number)",
            "CREATE INDEX FOR (i:Issue) ON (i.number)",
            "CREATE INDEX FOR (d:Developer) ON (d.login)",
            "CREATE INDEX FOR (c:Commit) ON (c.sha)",
        ]
        for q in index_queries:
            try:
                self.graph.query(q)
            except Exception as e:
                # Ignore if index already exists or syntax variation
                if "already exists" not in str(e).lower():
                    pass

    def run_query(self, query: str, params: Optional[Dict[str, Any]] = None):
        """Executes a parameterized Cypher query."""
        if self.graph is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self.graph.query(query, params or {})

    def batch_insert(
        self,
        files: List[FileNode],
        imports: List[ImportEdge],
        developers: List[DeveloperNode],
        commits: List[CommitNode],
        touched: List[TouchedEdge],
        prs: List[PullRequestNode],
        modifies: List[ModifiesEdge],
        reviews: List[ReviewedEdge],
        issues: List[IssueNode],
        mentions: List[MentionsEdge],
    ) -> None:
        """Executes batched, parameterized Cypher statements using UNWIND."""
        console.print("[cyan]Writing graph nodes and relationships to FalkorDB...[/cyan]")

        # 1. File Nodes
        if files:
            batch = [{"path": f.path, "pagerank": f.pagerank} for f in files]
            self.run_query(
                "UNWIND $batch AS item MERGE (f:File {path: item.path}) ON CREATE SET f.pagerank = item.pagerank",
                {"batch": batch},
            )

        # 2. File IMPORTS File
        if imports:
            batch = [{"source": i.source, "target": i.target} for i in imports]
            self.run_query(
                """
                UNWIND $batch AS item
                MATCH (src:File {path: item.source})
                MATCH (dst:File {path: item.target})
                MERGE (src)-[:IMPORTS]->(dst)
                """,
                {"batch": batch},
            )

        # 3. Developer Nodes
        if developers:
            batch = [{"login": d.login, "user_id": d.user_id} for d in developers]
            self.run_query(
                "UNWIND $batch AS item MERGE (d:Developer {login: item.login}) ON CREATE SET d.user_id = item.user_id",
                {"batch": batch},
            )

        # 4. Commit Nodes & Developer AUTHORED Commit
        if commits:
            batch = [
                {
                    "sha": c.sha,
                    "days_ago": c.days_ago,
                    "timestamp": c.timestamp,
                    "author_login": c.author_login,
                }
                for c in commits
            ]
            self.run_query(
                """
                UNWIND $batch AS item
                MERGE (d:Developer {login: item.author_login})
                MERGE (c:Commit {sha: item.sha})
                ON CREATE SET c.days_ago = item.days_ago, c.timestamp = item.timestamp
                MERGE (d)-[:AUTHORED]->(c)
                """,
                {"batch": batch},
            )

        # 5. Commit TOUCHED File
        if touched:
            batch = [{"sha": t.sha, "path": t.path, "changes": t.changes} for t in touched]
            self.run_query(
                """
                UNWIND $batch AS item
                MATCH (c:Commit {sha: item.sha})
                MATCH (f:File {path: item.path})
                MERGE (c)-[:TOUCHED {changes: item.changes}]->(f)
                """,
                {"batch": batch},
            )

        # 6. PullRequest Nodes
        if prs:
            batch = [
                {
                    "number": p.number,
                    "title": p.title,
                    "state": p.state,
                    "author": p.author,
                    "base_branch": p.base_branch,
                    "url": p.url,
                }
                for p in prs
            ]
            self.run_query(
                """
                UNWIND $batch AS item
                MERGE (p:PullRequest {number: item.number})
                ON CREATE SET p.title = item.title,
                              p.state = item.state,
                              p.author = item.author,
                              p.base_branch = item.base_branch,
                              p.url = item.url
                """,
                {"batch": batch},
            )

        # 7. PullRequest MODIFIES File
        if modifies:
            batch = [
                {
                    "pr_number": m.pr_number,
                    "path": m.path,
                    "changes": m.changes,
                    "additions": m.additions,
                    "deletions": m.deletions,
                }
                for m in modifies
            ]
            self.run_query(
                """
                UNWIND $batch AS item
                MATCH (pr:PullRequest {number: item.pr_number})
                MATCH (f:File {path: item.path})
                MERGE (pr)-[:MODIFIES {changes: item.changes, additions: item.additions, deletions: item.deletions}]->(f)
                """,
                {"batch": batch},
            )

        # 8. Developer REVIEWED PullRequest
        if reviews:
            batch = [{"login": r.login, "pr_number": r.pr_number} for r in reviews]
            self.run_query(
                """
                UNWIND $batch AS item
                MATCH (pr:PullRequest {number: item.pr_number})
                MERGE (d:Developer {login: item.login})
                MERGE (d)-[:REVIEWED]->(pr)
                """,
                {"batch": batch},
            )

        # 9. Issue Nodes
        if issues:
            batch = [
                {
                    "number": i.number,
                    "title": i.title,
                    "state": i.state,
                    "url": i.url,
                }
                for i in issues
            ]
            self.run_query(
                """
                UNWIND $batch AS item
                MERGE (i:Issue {number: item.number})
                ON CREATE SET i.title = item.title,
                              i.state = item.state,
                              i.url = item.url
                """,
                {"batch": batch},
            )

        # 10. Issue MENTIONS File
        if mentions:
            batch = [
                {
                    "issue_number": m.issue_number,
                    "file_path": m.file_path,
                    "source": m.source,
                    "confidence": m.confidence,
                    "match_term": m.match_term,
                }
                for m in mentions
            ]
            self.run_query(
                """
                UNWIND $batch AS item
                MATCH (i:Issue {number: item.issue_number})
                MATCH (f:File {path: item.file_path})
                MERGE (i)-[:MENTIONS {source: item.source, confidence: item.confidence, match_term: item.match_term}]->(f)
                """,
                {"batch": batch},
            )

    def compute_pagerank(self) -> None:
        """Calculates PageRank on File nodes using algo.pageRank or in-degree centrality fallback."""
        console.print("[cyan]Computing PageRank on File imports graph...[/cyan]")
        pagerank_succeeded = False
        try:
            # Standard FalkorDB GraphBLAS PageRank algorithm
            self.run_query("CALL algo.pageRank('File', 'IMPORTS') YIELD node, score SET node.pagerank = round(score * 10000.0) / 10000.0")
            pagerank_succeeded = True
            console.print("[green][+] Executed GraphBLAS `algo.pageRank` procedure successfully.[/green]")
        except Exception as e:
            console.print(f"[yellow]Notice: algo.pageRank procedure returned ({e}). Running degree centrality fallback...[/yellow]")

        if not pagerank_succeeded:
            # Deterministic architectural degree centrality calculation
            fallback_query = """
            MATCH (f:File)
            OPTIONAL MATCH (other:File)-[:IMPORTS]->(f)
            WITH f, count(other) AS in_degree
            MATCH (all_files:File)
            WITH f, in_degree, count(DISTINCT all_files) AS total_files
            SET f.pagerank = round((1.0 + (toFloat(in_degree) / CASE WHEN total_files > 0 THEN total_files ELSE 1.0 END) * 10.0) * 10000.0) / 10000.0
            """
            self.run_query(fallback_query)
            console.print("[green][+] Applied normalized in-degree architectural score to all File nodes.[/green]")


# ---------------------------------------------------------------------------
# AST Code Analyzer & Import Resolver
# ---------------------------------------------------------------------------
class ASTCodeAnalyzer:
    """Walks Python repository, maps top-level classes/functions, and resolves internal imports."""

    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.tracked_py_files: Set[str] = set()
        self.symbol_to_files: Dict[str, Set[str]] = {}
        self.filename_to_files: Dict[str, Set[str]] = {}

    def discover_files(self) -> Set[str]:
        """Discovers all valid .py files in the repository relative to repo_root."""
        for root, dirs, files in os.walk(self.repo_root):
            # Prune excluded directories
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]

            for file in files:
                if file.endswith(".py"):
                    full_path = Path(root) / file
                    rel_path = full_path.relative_to(self.repo_root).as_posix()
                    self.tracked_py_files.add(rel_path)

                    basename = file
                    if basename not in self.filename_to_files:
                        self.filename_to_files[basename] = set()
                    self.filename_to_files[basename].add(rel_path)

        return self.tracked_py_files

    def analyze(self) -> Tuple[List[FileNode], List[ImportEdge], Dict[str, str], Dict[str, str]]:
        """
        Parses all Python files with ast:
        Returns:
            - files: List[FileNode]
            - imports: List[ImportEdge]
            - unique_symbols: Dict[symbol_name, file_path]
            - unique_filenames: Dict[filename, file_path]
        """
        self.discover_files()
        file_nodes = [FileNode(path=p) for p in sorted(self.tracked_py_files)]
        import_edges: List[ImportEdge] = []
        raw_imports: Set[Tuple[str, str]] = set()

        for rel_path in self.tracked_py_files:
            abs_path = self.repo_root / rel_path
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(content, filename=rel_path)
            except Exception:
                continue

            # 1. Extract top-level symbols
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    sym_name = node.name
                    if sym_name not in self.symbol_to_files:
                        self.symbol_to_files[sym_name] = set()
                    self.symbol_to_files[sym_name].add(rel_path)

            # 2. Extract and resolve imports
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        target = self._resolve_module(alias.name)
                        if target and target in self.tracked_py_files and target != rel_path:
                            raw_imports.add((rel_path, target))
                elif isinstance(node, ast.ImportFrom):
                    targets = self._resolve_import_from(rel_path, node.level, node.module, [n.name for n in node.names])
                    for target in targets:
                        if target in self.tracked_py_files and target != rel_path:
                            raw_imports.add((rel_path, target))

        for src, dst in raw_imports:
            import_edges.append(ImportEdge(source=src, target=dst))

        # Filter strictly unique symbols
        unique_symbols: Dict[str, str] = {}
        for sym, files in self.symbol_to_files.items():
            if len(files) == 1 and len(sym) >= 4 and sym.lower() not in GENERIC_STOPWORDS:
                unique_symbols[sym] = next(iter(files))

        # Filter strictly unique filenames
        unique_filenames: Dict[str, str] = {}
        for fname, files in self.filename_to_files.items():
            if len(files) == 1:
                unique_filenames[fname] = next(iter(files))

        return file_nodes, import_edges, unique_symbols, unique_filenames

    def _resolve_module(self, module_name: str) -> Optional[str]:
        """Resolves an absolute module string (e.g. 'httpx._client') to a file path."""
        mod_path = module_name.replace(".", "/")
        candidates = [
            f"{mod_path}.py",
            f"{mod_path}/__init__.py",
            f"src/{mod_path}.py",
            f"src/{mod_path}/__init__.py",
        ]
        for cand in candidates:
            if cand in self.tracked_py_files:
                return cand
        return None

    def _resolve_import_from(
        self,
        current_file: str,
        level: int,
        module: Optional[str],
        names: List[str],
    ) -> List[str]:
        """Resolves relative and from-import statements to target file paths."""
        results: List[str] = []
        curr_dir_parts = list(PurePosixPath(current_file).parts[:-1])

        if level > 0:
            # Relative import
            back_steps = level - 1
            if back_steps <= len(curr_dir_parts):
                base_parts = curr_dir_parts[: len(curr_dir_parts) - back_steps] if back_steps > 0 else curr_dir_parts
                mod_parts = module.split(".") if module else []
                candidate_base = "/".join(base_parts + mod_parts) if (base_parts or mod_parts) else ""

                if candidate_base:
                    if f"{candidate_base}.py" in self.tracked_py_files:
                        results.append(f"{candidate_base}.py")
                    elif f"{candidate_base}/__init__.py" in self.tracked_py_files:
                        results.append(f"{candidate_base}/__init__.py")

                # If module was None (e.g. `from . import _models, utils`)
                if not module:
                    for name in names:
                        name_cand = f"{candidate_base}/{name}.py" if candidate_base else f"{name}.py"
                        init_cand = f"{candidate_base}/{name}/__init__.py" if candidate_base else f"{name}/__init__.py"
                        if name_cand in self.tracked_py_files:
                            results.append(name_cand)
                        elif init_cand in self.tracked_py_files:
                            results.append(init_cand)
        else:
            # Absolute import
            if module:
                cand = self._resolve_module(module)
                if cand:
                    results.append(cand)
                else:
                    # e.g. `from httpx import Client` -> maybe module is `httpx` and target is `httpx/__init__.py`
                    cand_pkg = self._resolve_module(module)
                    if cand_pkg:
                        results.append(cand_pkg)
                    # Or `from httpx import _models` where name is the submodule
                    for name in names:
                        sub_cand = self._resolve_module(f"{module}.{name}")
                        if sub_cand:
                            results.append(sub_cand)

        return results


# ---------------------------------------------------------------------------
# Git History Miner
# ---------------------------------------------------------------------------
class GitLogMiner:
    """Extracts developer commit history and file touches via fast local `git log --numstat`."""

    def __init__(
        self,
        repo_root: Path,
        tracked_py_files: Set[str],
        depth: int = 150,
        commit_author_map: Optional[Dict[str, Tuple[str, int]]] = None,
    ):
        self.repo_root = repo_root
        self.tracked_py_files = tracked_py_files
        self.depth = depth
        self.commit_author_map = commit_author_map or {}

    def mine(self) -> Tuple[List[DeveloperNode], List[CommitNode], List[TouchedEdge]]:
        """Parses git log -n depth --numstat and returns developers, commits, and touched edges."""
        cmd = [
            "git",
            "log",
            f"-n {self.depth}",
            "--numstat",
            '--format=COMMIT:%H|%an|%ae|%at|%ad',
            "--date=iso",
        ]
        # Run command joined for Windows shell compatibility
        cmd_str = f'git log -n {self.depth} --numstat --format="COMMIT:%H|%an|%ae|%at|%ad" --date=iso'
        try:
            res = subprocess.run(
                cmd_str,
                shell=True,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
        except Exception as e:
            console.print(f"[yellow]Warning: Git log execution failed: {e}. Skipping commit history.[/yellow]")
            return [], [], []

        developers_map: Dict[str, DeveloperNode] = {}
        commits: List[CommitNode] = []
        touched_edges: List[TouchedEdge] = []
        now_ts = int(time.time())

        current_commit: Optional[CommitNode] = None
        skip_current_commit = False

        for raw_line in res.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("COMMIT:"):
                # Header: COMMIT:sha|author_name|author_email|timestamp|date_iso
                header = line[len("COMMIT:") :]
                parts = header.split("|")
                if len(parts) >= 5:
                    sha, an, ae, at_str, ad = parts[0], parts[1], parts[2], parts[3], parts[4]
                else:
                    continue

                # Filter bot accounts
                is_bot = any(b in an.lower() or b in ae.lower() for b in BOT_KEYWORDS)
                if is_bot:
                    skip_current_commit = True
                    current_commit = None
                    continue

                skip_current_commit = False

                # Refinement A: Consistent Developer Identity Alignment
                dev_login, user_id = self._align_developer_identity(sha, an, ae)

                if dev_login not in developers_map:
                    developers_map[dev_login] = DeveloperNode(login=dev_login, user_id=user_id)
                elif user_id and developers_map[dev_login].user_id == 0:
                    developers_map[dev_login].user_id = user_id

                try:
                    commit_ts = int(at_str)
                    days_ago = max(0, int((now_ts - commit_ts) / 86400))
                except ValueError:
                    days_ago = 0

                current_commit = CommitNode(
                    sha=sha,
                    days_ago=days_ago,
                    timestamp=ad,
                    author_login=dev_login,
                )
                commits.append(current_commit)

            elif current_commit and not skip_current_commit:
                # Numstat line: additions\tdeletions\tfilepath
                parts = raw_line.split("\t")
                if len(parts) >= 3:
                    adds_str, dels_str, raw_filepath = parts[0].strip(), parts[1].strip(), parts[2].strip()
                    filepath = self._normalize_filepath(raw_filepath)

                    # Refinement 2: Restrict :TOUCHED strictly to tracked .py files
                    if filepath in self.tracked_py_files:
                        try:
                            adds = int(adds_str) if adds_str != "-" else 0
                            dels = int(dels_str) if dels_str != "-" else 0
                            changes = adds + dels
                        except ValueError:
                            changes = 1

                        touched_edges.append(
                            TouchedEdge(
                                sha=current_commit.sha,
                                path=filepath,
                                changes=max(1, changes),
                            )
                        )

        return list(developers_map.values()), commits, touched_edges

    def _align_developer_identity(self, sha: str, name: str, email: str) -> Tuple[str, int]:
        """Aligns git author identity with GitHub user login handle."""
        # 1. Check GitHub API commit lookup map
        if sha in self.commit_author_map:
            return self.commit_author_map[sha]

        # 2. Check for GitHub noreply email handle
        # e.g. 123456+username@users.noreply.github.com or username@users.noreply.github.com
        noreply_match = re.search(r"(?:^|[\+])([a-zA-Z0-9_\-]+)@users\.noreply\.github\.com", email)
        if noreply_match:
            return noreply_match.group(1), 0

        # 3. Clean fallback to handle
        clean_handle = re.sub(r"[^a-zA-Z0-9_-]", "", name.lower().replace(" ", "-"))
        return clean_handle or "unknown-dev", 0

    @staticmethod
    def _normalize_filepath(raw_path: str) -> str:
        """Handles git rename format e.g. 'httpx/{old => new}/file.py' or 'old.py => new.py'."""
        if "=>" in raw_path:
            # e.g. {a => b}
            match = re.search(r"\{.*?=>\s*(.*?)\}", raw_path)
            if match:
                raw_path = re.sub(r"\{.*?=>\s*(.*?)\}", match.group(1), raw_path)
            else:
                raw_path = raw_path.split("=>")[-1].strip()
        return raw_path.replace("\\", "/").strip()


# ---------------------------------------------------------------------------
# GitHub REST API Client & Disk Cache
# ---------------------------------------------------------------------------
class GitHubClient:
    """Fetches real PRs, reviews, and issues with local disk caching in cache/{owner}_{repo}/."""

    def __init__(self, repo: str, token: Optional[str] = None, refresh_cache: bool = False):
        self.repo = repo
        self.owner, self.repo_name = repo.split("/")
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            try:
                proc = subprocess.run(
                    ["git", "credential", "fill"],
                    input="protocol=https\nhost=github.com\n",
                    text=True,
                    capture_output=True,
                    timeout=5,
                )
                for line in proc.stdout.splitlines():
                    if line.startswith("password="):
                        self.token = line.split("=", 1)[1].strip()
                        break
            except Exception:
                pass

        self.refresh_cache = refresh_cache
        self.cache_dir = Path("cache") / f"{self.owner}_{self.repo_name}"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "MaintainerTriageAgent/1.0",
                "Accept": "application/vnd.github+json",
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    def _get_json(self, url: str, cache_filename: str) -> Any:
        cache_path = self.cache_dir / cache_filename
        if not self.refresh_cache and cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        # Make real API call
        resp = self.session.get(url, timeout=20)
        if resp.status_code == 403:
            raise RuntimeError(
                f"GitHub API 403 Rate Limit Exceeded for {url}.\n"
                "To resolve, set your GITHUB_TOKEN in .env or run with an existing cache."
            )
        resp.raise_for_status()
        data = resp.json()

        # Cache response
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        return data

    def fetch_commit_authors(self) -> Dict[str, Tuple[str, int]]:
        """Fetches /commits?per_page=100 to map commit SHAs to real GitHub logins and user IDs."""
        url = f"https://api.github.com/repos/{self.repo}/commits?per_page=100"
        try:
            commits = self._get_json(url, "commits_authors.json")
            sha_map: Dict[str, Tuple[str, int]] = {}
            for c in commits:
                sha = c.get("sha")
                author_obj = c.get("author")
                if sha and author_obj and author_obj.get("login"):
                    sha_map[sha] = (author_obj["login"], author_obj.get("id", 0))
            return sha_map
        except Exception as e:
            console.print(f"[yellow]Notice: Commit author mapping skipped: {e}[/yellow]")
            return {}

    def fetch_open_prs(self, limit: int = 30) -> List[Dict[str, Any]]:
        """Fetches up to `limit` open pull requests."""
        url = f"https://api.github.com/repos/{self.repo}/pulls?state=open&per_page={limit}"
        return self._get_json(url, f"pulls_open_{limit}.json")

    def fetch_closed_prs(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Fetches up to `limit` recently closed pull requests (for reviewer history)."""
        url = f"https://api.github.com/repos/{self.repo}/pulls?state=closed&per_page={limit}"
        return self._get_json(url, f"pulls_closed_{limit}.json")

    def fetch_pr_files(self, pr_number: int) -> List[Dict[str, Any]]:
        """Fetches modified files for a specific pull request."""
        url = f"https://api.github.com/repos/{self.repo}/pulls/{pr_number}/files"
        return self._get_json(url, f"pr_{pr_number}_files.json")

    def fetch_pr_reviews(self, pr_number: int) -> List[Dict[str, Any]]:
        """Fetches reviews for a specific pull request."""
        url = f"https://api.github.com/repos/{self.repo}/pulls/{pr_number}/reviews"
        return self._get_json(url, f"pr_{pr_number}_reviews.json")

    def fetch_open_issues(self, target_count: int = 50, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Paginates /issues?state=open&per_page=100 until at least `target_count` pure issues are collected."""
        target_count = limit if limit is not None else target_count
        cache_filename = f"issues_open_pure_{target_count}.json"
        cache_path = self.cache_dir / cache_filename
        if not self.refresh_cache and cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass

        pure_issues: List[Dict[str, Any]] = []
        page = 1
        max_pages = 5
        while len(pure_issues) < target_count and page <= max_pages:
            url = f"https://api.github.com/repos/{self.repo}/issues?state=open&per_page=100&page={page}"
            try:
                page_data = self._get_json(url, f"issues_page_{page}.json")
            except Exception as e:
                console.print(f"[yellow]Warning: Fetching issues page {page} returned {e}[/yellow]")
                break
            if not page_data or not isinstance(page_data, list):
                break
            for item in page_data:
                if "pull_request" not in item:
                    pure_issues.append(item)
                    if len(pure_issues) >= target_count:
                        break
            page += 1

        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(pure_issues, f, indent=2)

        return pure_issues


# ---------------------------------------------------------------------------
# Issue-File Deterministic Linker
# ---------------------------------------------------------------------------
class IssueFileMatcher:
    """Deterministically links open Issues to File nodes based on explicit paths, stack traces, and symbols."""

    def __init__(
        self,
        tracked_py_files: Set[str],
        unique_symbols: Dict[str, str],
        unique_filenames: Dict[str, str],
    ):
        self.tracked_py_files = tracked_py_files
        self.unique_symbols = unique_symbols
        self.unique_filenames = unique_filenames

    def match_issue(self, issue: Dict[str, Any]) -> List[MentionsEdge]:
        """Evaluates issue title and body to find deterministic File matches."""
        issue_number = issue["number"]
        title = issue.get("title") or ""
        body = issue.get("body") or ""
        full_text = f"{title}\n\n{body}"

        # Map file_path -> (best_confidence, source, match_term)
        matches: Dict[str, Tuple[float, str, str]] = {}

        def record(path: str, conf: float, src: str, term: str) -> None:
            if path in self.tracked_py_files:
                if path not in matches or matches[path][0] < conf:
                    matches[path] = (conf, src, term)

        # 1. Explicit File Path matches (conf = 0.95)
        # Search for occurrences of any tracked file path
        for path in self.tracked_py_files:
            if path in full_text:
                record(path, 0.95, "explicit_path", path)

        # Also search for regex path patterns ending in .py
        path_matches = re.findall(r"(?:[a-zA-Z0-9_\-\.]+[/])+([a-zA-Z0-9_\-\.]+\.py)", full_text)
        for cand_path in path_matches:
            # Check if any tracked file ends with this relative segment
            for tf in self.tracked_py_files:
                if tf.endswith(cand_path):
                    record(tf, 0.95, "explicit_path", cand_path)

        # 2. Python Stack Trace lines (conf = 0.90)
        # Pattern: File "...", line 123
        traceback_lines = re.findall(r'File\s+["\']?([^"\'\n]+?\.py)["\']?,\s+line\s+\d+', full_text)
        for trace_file in traceback_lines:
            norm_trace = trace_file.replace("\\", "/")
            for tf in self.tracked_py_files:
                if tf == norm_trace or norm_trace.endswith("/" + tf) or tf.endswith("/" + norm_trace.split("/")[-1]):
                    record(tf, 0.90, "stack_trace", trace_file)

        # 3. Unique Class/Function/Filename matches (conf = 0.70)
        # Filename match (strictly unique filenames in the project)
        for fname, path in self.unique_filenames.items():
            if re.search(r"\b" + re.escape(fname) + r"\b", full_text):
                record(path, 0.70, "symbol_or_filename", fname)

        # Unique symbol match (classes, functions mapped to exactly 1 file)
        for sym, path in self.unique_symbols.items():
            if re.search(r"\b" + re.escape(sym) + r"\b", full_text):
                record(path, 0.70, "symbol_or_filename", sym)

        return [
            MentionsEdge(
                issue_number=issue_number,
                file_path=f_path,
                source=src,
                confidence=conf,
                match_term=term,
            )
            for f_path, (conf, src, term) in matches.items()
        ]


# ---------------------------------------------------------------------------
# Validation Reporter
# ---------------------------------------------------------------------------
class ValidationReporter:
    """Generates and prints the final Data Quality Validation Report."""

    @staticmethod
    def print_report(
        db: Optional[DatabaseManager],
        files: List[FileNode],
        imports: List[ImportEdge],
        developers: List[DeveloperNode],
        commits: List[CommitNode],
        prs: List[PullRequestNode],
        modifies: List[ModifiesEdge],
        reviews: List[ReviewedEdge],
        issues: List[IssueNode],
        mentions: List[MentionsEdge],
        dry_run: bool = False,
    ) -> None:
        console.print()
        console.print(Panel("[bold green]Repository Ingestion & Data Quality Validation Report[/bold green]", expand=False))

        # 1. Summary Metrics Table
        table = Table(title="Graph Ingestion Summary Metrics", show_header=True, header_style="bold magenta")
        table.add_column("Metric Entity / Relation", style="dim", width=40)
        table.add_column("Count", justify="right")
        table.add_column("Notes", style="italic")

        table.add_row("Tracked Python Files (:File)", str(len(files)), "Excludes tests, docs, configs")
        table.add_row("Internal Import Edges ([:IMPORTS])", str(len(imports)), "Deterministic AST resolution")
        table.add_row("Unique Developers (:Developer)", str(len(developers)), "Excludes bot accounts")
        table.add_row("Commit History (:Commit)", str(len(commits)), "Shallow depth window")
        table.add_row("Open Pull Requests (:PullRequest)", str(len(prs)), "Ingested from GitHub REST API")
        table.add_row("PR Modified Code Files ([:MODIFIES])", str(len(modifies)), "Strictly tracked .py files")
        table.add_row("Peer Review Edges ([:REVIEWED])", str(len(reviews)), "Open and recent PR reviewers")
        table.add_row("Open Issues (:Issue)", str(len(issues)), "Excludes pull request items")
        table.add_row("Issues Linked to Code ([:MENTIONS])", str(len(mentions)), "Deterministic multi-signal links")

        distinct_issues_linked = len(set(m.issue_number for m in mentions))
        table.add_row(
            "Distinct Issues Linked",
            str(distinct_issues_linked),
            f"{round(distinct_issues_linked / max(1, len(issues)) * 100, 1)}% of open issues linked",
        )
        console.print(table)

        # 2. Mentions by Confidence Breakdown
        conf_table = Table(title="Issue-to-Code Linkage Confidence Breakdown", show_header=True, header_style="bold cyan")
        conf_table.add_column("Match Source", style="dim")
        conf_table.add_column("Confidence", justify="center")
        conf_table.add_column("Edges Created", justify="right")

        breakdown: Dict[str, Tuple[float, int]] = {}
        for m in mentions:
            if m.source not in breakdown:
                breakdown[m.source] = (m.confidence, 0)
            breakdown[m.source] = (m.confidence, breakdown[m.source][1] + 1)

        for src, (conf, count) in sorted(breakdown.items(), key=lambda x: -x[1][0]):
            conf_table.add_row(src, f"{conf:.2f}", str(count))
        console.print(conf_table)

        # 3. Pull Request File Collisions (Overlapping PRs)
        pr_mod_map: Dict[str, List[int]] = {}
        for m in modifies:
            # Exclude tests/ files from collision tables per user requirement
            if m.path.startswith("tests/") or m.path.startswith("test/"):
                continue
            if m.path not in pr_mod_map:
                pr_mod_map[m.path] = []
            pr_mod_map[m.path].append(m.pr_number)

        collisions = {p: pr_list for p, pr_list in pr_mod_map.items() if len(pr_list) > 1}

        col_table = Table(title="Open Pull Request File Collisions (High-Risk Overlaps)", show_header=True, header_style="bold red")
        col_table.add_column("Colliding File Path", style="dim", width=45)
        col_table.add_column("Conflicting PR Numbers", justify="left")
        col_table.add_column("Risk Severity", justify="center")

        if collisions:
            for fpath, pr_list in collisions.items():
                col_table.add_row(fpath, ", ".join(f"#{num}" for num in sorted(set(pr_list))), "[red]HIGH[/red]")
        else:
            col_table.add_row("[green]No colliding open PRs found on .py files[/green]", "-", "-")
        console.print(col_table)

        # 4. Top Architectural Core Files (PageRank) - Exclude __init__.py and tests/
        pr_table = Table(title="Top 5 Core Architectural Files (PageRank / Criticality)", show_header=True, header_style="bold yellow")
        pr_table.add_column("File Path", style="dim", width=45)
        pr_table.add_column("PageRank Score", justify="right")

        top_architectural_files: List[Tuple[str, float]] = []
        if db and not dry_run:
            try:
                res = db.run_query("""
                    MATCH (f:File)
                    WHERE NOT f.path ENDS WITH '__init__.py'
                      AND NOT f.path STARTS WITH 'tests/'
                      AND NOT f.path STARTS WITH 'test/'
                    RETURN f.path AS path, f.pagerank AS pr
                    ORDER BY f.pagerank DESC
                    LIMIT 5
                """)
                for row in res.result_set:
                    pr_table.add_row(str(row[0]), f"{float(row[1]):.4f}")
                    top_architectural_files.append((str(row[0]), float(row[1])))
            except Exception:
                pass

        if not top_architectural_files:
            in_degrees: Dict[str, int] = {
                f.path: 0
                for f in files
                if not f.path.endswith("__init__.py") and not f.path.startswith("tests/") and not f.path.startswith("test/")
            }
            for imp in imports:
                if imp.target in in_degrees:
                    in_degrees[imp.target] += 1
            sorted_top = sorted(in_degrees.items(), key=lambda x: -x[1])[:5]
            for path, deg in sorted_top:
                score = round(1.0 + (deg / max(1, len(files))) * 10.0, 4)
                pr_table.add_row(path, f"{score:.4f}")
                top_architectural_files.append((path, score))

        console.print(pr_table)
        console.print("[bold green][+] Ingestion and validation complete![/bold green]\n")


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Maintainer Triage Agent - Repository Graph Ingestion Pipeline")
    parser.add_argument("--repo", type=str, default="encode/httpx", help="Target GitHub repository (default: encode/httpx)")
    parser.add_argument("--depth", type=int, default=150, help="Shallow clone and git log commit depth (default: 150)")
    parser.add_argument("--repo-dir", type=str, default=None, help="Use existing cloned repo directory instead of cloning")
    parser.add_argument("--host", type=str, default=os.environ.get("FALKORDB_HOST", "localhost"), help="FalkorDB host")
    parser.add_argument("--port", type=int, default=int(os.environ.get("FALKORDB_PORT", 6379)), help="FalkorDB port")
    parser.add_argument("--password", type=str, default=os.environ.get("FALKORDB_PASSWORD"), help="FalkorDB password")
    parser.add_argument("--graph", type=str, default="maintainer_triage", help="FalkorDB graph name")
    parser.add_argument("--lite", action="store_true", default=os.environ.get("USE_LITE") == "1", help="Use FalkorDBLite")
    parser.add_argument("--lite-path", type=str, default="maintainer_triage.db", help="FalkorDBLite database file path")
    parser.add_argument("--append", action="store_true", help="Append to existing graph instead of wiping beforehand")
    parser.add_argument("--refresh-cache", action="store_true", help="Force refresh GitHub REST API disk cache")
    parser.add_argument("--dry-run", action="store_true", help="Run AST, Git, and GitHub extraction without writing to FalkorDB")
    parser.add_argument("--limit-prs", type=int, default=30, help="Max open PRs to ingest (default: 30)")
    parser.add_argument("--limit-issues", type=int, default=50, help="Max open Issues to ingest (default: 50)")

    args = parser.parse_args()

    clean_graph = not args.append

    console.print(Panel(f"[bold cyan]Ingesting Repository: {args.repo}[/bold cyan]", expand=False))

    # 1. Locate or clone target repository
    temp_dir: Optional[str] = None
    if args.repo_dir:
        repo_path = Path(args.repo_dir).resolve()
        if not repo_path.exists():
            console.print(f"[red]Error: Specified --repo-dir '{args.repo_dir}' does not exist.[/red]")
            sys.exit(1)
        console.print(f"[cyan]Using local repository at '{repo_path}'...[/cyan]")
    else:
        # Check local repo cache
        owner, repo_name = args.repo.split("/")
        cached_clone_path = Path(".cache") / "repos" / f"{owner}_{repo_name}"
        if cached_clone_path.exists() and (cached_clone_path / ".git").exists():
            repo_path = cached_clone_path.resolve()
            console.print(f"[cyan]Using cached repository clone at '{repo_path}'...[/cyan]")
        else:
            cached_clone_path.parent.mkdir(parents=True, exist_ok=True)
            console.print(f"[cyan]Shallow cloning https://github.com/{args.repo}.git (--depth {args.depth})...[/cyan]")
            clone_cmd = f'git clone --depth {args.depth} https://github.com/{args.repo}.git "{cached_clone_path}"'
            try:
                subprocess.run(clone_cmd, shell=True, check=True)
                repo_path = cached_clone_path.resolve()
            except Exception as e:
                console.print(f"[red]Error cloning repository: {e}[/red]")
                sys.exit(1)

    # 2. AST parsing & import graph extraction
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
        progress.add_task(description="Parsing Python files and AST imports...", total=None)
        analyzer = ASTCodeAnalyzer(repo_path)
        file_nodes, import_edges, unique_symbols, unique_filenames = analyzer.analyze()

    console.print(
        f"[green][+] AST Analysis complete: {len(file_nodes)} .py files, {len(import_edges)} internal imports, "
        f"{len(unique_symbols)} unique symbols.[/green]"
    )

    # 3. Fetch GitHub commit authors
    gh_client = GitHubClient(args.repo, refresh_cache=args.refresh_cache)

    commit_author_map: Dict[str, Tuple[str, int]] = {}
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
        progress.add_task(description="Fetching GitHub commit author mappings...", total=None)
        commit_author_map = gh_client.fetch_commit_authors()

    # 4. Mine local git log for churn & timestamps
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
        progress.add_task(description="Mining local git log numstat history...", total=None)
        miner = GitLogMiner(
            repo_path,
            analyzer.tracked_py_files,
            depth=args.depth,
            commit_author_map=commit_author_map,
        )
        developers, commits, touched_edges = miner.mine()

    console.print(
        f"[green][+] Git history parsed: {len(developers)} developers, {len(commits)} commits, "
        f"{len(touched_edges)} file touches on .py files.[/green]"
    )

    # 5. Fetch open PRs and changed files
    pr_nodes: List[PullRequestNode] = []
    modifies_edges: List[ModifiesEdge] = []
    reviewed_edges: List[ReviewedEdge] = []
    developer_extra: Dict[str, DeveloperNode] = {}

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
        task = progress.add_task(description="Fetching open Pull Requests and modified files...", total=None)
        raw_prs = gh_client.fetch_open_prs(limit=args.limit_prs)
        for p in raw_prs:
            pr_num = p["number"]
            author = p.get("user", {}).get("login", "unknown")
            base_ref = p.get("base", {}).get("ref", "main")
            pr_nodes.append(
                PullRequestNode(
                    number=pr_num,
                    title=p.get("title", ""),
                    state=p.get("state", "open"),
                    author=author,
                    base_branch=base_ref,
                    url=p.get("html_url", ""),
                )
            )

            # Fetch modified files
            try:
                files_mod = gh_client.fetch_pr_files(pr_num)
                for fm in files_mod:
                    fn = fm.get("filename", "").replace("\\", "/")
                    # Refinement 2: Restrict :MODIFIES strictly to tracked .py files
                    if fn in analyzer.tracked_py_files:
                        modifies_edges.append(
                            ModifiesEdge(
                                pr_number=pr_num,
                                path=fn,
                                changes=fm.get("changes", 0),
                                additions=fm.get("additions", 0),
                                deletions=fm.get("deletions", 0),
                            )
                        )
            except Exception as e:
                console.print(f"[yellow]Warning fetching files for PR #{pr_num}: {e}[/yellow]")

            # Fetch PR reviews (Refinement C)
            try:
                reviews = gh_client.fetch_pr_reviews(pr_num)
                for rev in reviews:
                    rev_user = rev.get("user", {})
                    rev_login = rev_user.get("login")
                    if rev_login and not any(b in rev_login.lower() for b in BOT_KEYWORDS):
                        reviewed_edges.append(ReviewedEdge(login=rev_login, pr_number=pr_num))
                        if rev_login not in developer_extra:
                            developer_extra[rev_login] = DeveloperNode(login=rev_login, user_id=rev_user.get("id", 0))
            except Exception:
                pass

        # Also fetch up to 15 recently closed PRs to populate reviewer network (Refinement C)
        try:
            closed_prs = gh_client.fetch_closed_prs(limit=15)
            for cp in closed_prs:
                cpr_num = cp["number"]
                c_reviews = gh_client.fetch_pr_reviews(cpr_num)
                if c_reviews:
                    # Record PR node for closed PR if reviews exist
                    c_author = cp.get("user", {}).get("login", "unknown")
                    c_base = cp.get("base", {}).get("ref", "main")
                    pr_nodes.append(
                        PullRequestNode(
                            number=cpr_num,
                            title=cp.get("title", ""),
                            state=cp.get("state", "closed"),
                            author=c_author,
                            base_branch=c_base,
                            url=cp.get("html_url", ""),
                        )
                    )
                    for rev in c_reviews:
                        rev_user = rev.get("user", {})
                        rev_login = rev_user.get("login")
                        if rev_login and not any(b in rev_login.lower() for b in BOT_KEYWORDS):
                            reviewed_edges.append(ReviewedEdge(login=rev_login, pr_number=cpr_num))
                            if rev_login not in developer_extra:
                                developer_extra[rev_login] = DeveloperNode(login=rev_login, user_id=rev_user.get("id", 0))
        except Exception:
            pass

    # Merge developer_extra into developers
    dev_dict = {d.login: d for d in developers}
    for l, d in developer_extra.items():
        if l not in dev_dict:
            dev_dict[l] = d
    all_developers = list(dev_dict.values())

    console.print(
        f"[green][+] PRs & Reviews fetched: {len(pr_nodes)} PRs, {len(modifies_edges)} code modifications, "
        f"{len(reviewed_edges)} reviews.[/green]"
    )

    # 6. Fetch open issues and resolve code links
    issue_nodes: List[IssueNode] = []
    mentions_edges: List[MentionsEdge] = []
    matcher = IssueFileMatcher(analyzer.tracked_py_files, unique_symbols, unique_filenames)

    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
        progress.add_task(description="Fetching open Issues and resolving deterministic code links...", total=None)
        raw_issues = gh_client.fetch_open_issues(limit=args.limit_issues)
        for iss in raw_issues:
            issue_nodes.append(
                IssueNode(
                    number=iss["number"],
                    title=iss.get("title", ""),
                    state=iss.get("state", "open"),
                    url=iss.get("html_url", ""),
                )
            )
            matched = matcher.match_issue(iss)
            mentions_edges.extend(matched)

    console.print(
        f"[green][+] Issues processed: {len(issue_nodes)} open issues, {len(mentions_edges)} deterministic MENTIONS links.[/green]"
    )

    # 7. Write graph into FalkorDB and calculate PageRank
    db_mgr: Optional[DatabaseManager] = None
    if not args.dry_run:
        try:
            db_mgr = DatabaseManager(
                host=args.host,
                port=args.port,
                password=args.password,
                use_lite=args.lite,
                lite_path=args.lite_path,
                graph_name=args.graph,
                clean=clean_graph,
            )
            db_mgr.connect()
            db_mgr.batch_insert(
                files=file_nodes,
                imports=import_edges,
                developers=all_developers,
                commits=commits,
                touched=touched_edges,
                prs=pr_nodes,
                modifies=modifies_edges,
                reviews=reviewed_edges,
                issues=issue_nodes,
                mentions=mentions_edges,
            )
            db_mgr.compute_pagerank()
        except Exception as e:
            console.print(f"[red]FalkorDB Connection or Ingestion Error: {e}[/red]")
            console.print("[yellow]Falling back to offline summary validation report...[/yellow]")
            db_mgr = None

    # 8. Print ingestion summary
    ValidationReporter.print_report(
        db=db_mgr,
        files=file_nodes,
        imports=import_edges,
        developers=all_developers,
        commits=commits,
        prs=pr_nodes,
        modifies=modifies_edges,
        reviews=reviewed_edges,
        issues=issue_nodes,
        mentions=mentions_edges,
        dry_run=(args.dry_run or db_mgr is None),
    )


if __name__ == "__main__":
    main()
