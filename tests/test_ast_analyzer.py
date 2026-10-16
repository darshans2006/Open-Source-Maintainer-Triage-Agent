import tempfile
from pathlib import Path
import pytest
from ingest import ASTCodeAnalyzer, ImportEdge, FileNode


def test_ast_discover_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "pkg").mkdir()
        (root / "pkg" / "module_a.py").write_text("x = 1\n", encoding="utf-8")
        (root / "pkg" / "module_b.py").write_text("from .module_a import x\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "test_dummy.py").write_text("def test_ok(): pass\n", encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "ignored.py").write_text("# ignored\n", encoding="utf-8")

        analyzer = ASTCodeAnalyzer(root)
        files = analyzer.discover_files()

        assert "pkg/module_a.py" in files
        assert "pkg/module_b.py" in files
        assert ".git/ignored.py" not in files


def test_ast_analyze_imports_and_symbols():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        pkg = root / "mypackage"
        pkg.mkdir()

        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "core.py").write_text(
            "class CoreClient:\n"
            "    def request(self):\n"
            "        pass\n"
            "\n"
            "def initialize_client():\n"
            "    return CoreClient()\n",
            encoding="utf-8",
        )
        (pkg / "service.py").write_text(
            "from mypackage.core import CoreClient\n"
            "class ServiceLayer:\n"
            "    def __init__(self):\n"
            "        self.client = CoreClient()\n",
            encoding="utf-8",
        )

        analyzer = ASTCodeAnalyzer(root)
        file_nodes, import_edges, unique_symbols, unique_filenames = analyzer.analyze()

        # Check file nodes
        paths = {f.path for f in file_nodes}
        assert "mypackage/core.py" in paths
        assert "mypackage/service.py" in paths

        # Check import edge
        edges = {(e.source, e.target) for e in import_edges}
        assert ("mypackage/service.py", "mypackage/core.py") in edges

        # Check unique symbols
        assert "CoreClient" in unique_symbols
        assert unique_symbols["CoreClient"] == "mypackage/core.py"
        assert "ServiceLayer" in unique_symbols
        assert unique_symbols["ServiceLayer"] == "mypackage/service.py"
