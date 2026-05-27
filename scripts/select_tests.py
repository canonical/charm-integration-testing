#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Select PR-affected leaf tests via AST-based static analysis.

This script computes which leaf tests are transitively affected by
the source files changed in a PR.  It outputs pytest nodeids suitable
for feeding into the scheduler, which handles prerequisite injection.

The script does **not** invoke pytest, does **not** require the repo's
runtime dependencies, and does **not** need prior coverage data.  It
operates on source files only using ``ast`` and ``git diff``.

The leaf-pattern regex is controlled by the caller (CI config), not
hard-coded here.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import os
import re
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path

# Directories excluded from the source walk.
_EXCLUDED_DIRS = frozenset({
    ".venv",
    "__pycache__",
    ".git",
    "build",
    "dist",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".eggs",
})

_MAX_TRAVERSAL_DEPTH = 50

# Leaf tests that must always run on every PR.  Bridge tests
# (bootstrap_controller, create_model) are included because the scheduler
# injects them as prerequisites for test_deploy, but listing them here
# ensures they are selected even if the scheduler graph changes.
_BASE_SUITE = frozenset({
    "charm_integration_testing/test_suite/test_build_bundle.py::test_build_bundle",
    "charm_integration_testing/test_suite/test_bootstrap_controller.py::test_bootstrap_controller",
    "charm_integration_testing/test_suite/test_create_model.py::test_create_model",
    "charm_integration_testing/test_suite/test_deploy.py::test_deploy",
    "charm_integration_testing/test_suite/test_scale_in_and_scale_out.py::test_scale_in_and_scale_out_charm",
    "charm_integration_testing/test_suite/test_teardown.py::test_teardown",
})

# If any changed file matches these globs, all leaves are selected
# (skip AST analysis).  fnmatch patterns matched against repo-relative paths.
_FORCE_FULL_GLOBS = (
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "**/conftest.py",
    "charm_integration_testing/test_suite/scheduler/**",
    "scripts/**",
)


def _discover_source_roots(repo_root: Path) -> list[Path]:
    """Find directories containing ``pyproject.toml`` that act as package source roots.

    Returns *repo_root* first, then any sub-directories that have their own
    ``pyproject.toml`` (e.g. ``charm_integration_testing/``, ``bundle_builder_x/``).
    Order matters: the repo root is tried first so that fully-qualified imports
    (``charm_integration_testing.juju.client``) resolve before short imports
    (``juju.client``) that rely on a sub-root.
    """
    roots = [repo_root]
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        if Path(dirpath) == repo_root:
            continue
        if "pyproject.toml" in filenames or "setup.py" in filenames:
            roots.append(Path(dirpath))
            # Don't descend further; nested pyproject.toml directories are
            # independent roots, not children of this one.
            dirnames.clear()
    return roots


# ---------------------------------------------------------------------------
# 1. Changed files
# ---------------------------------------------------------------------------


def _changed_py_files(repo_root: Path, base_ref: str) -> list[Path]:
    """Return repo-relative ``Path`` objects for ``.py`` files changed vs *base_ref*."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=repo_root,
        )
    except subprocess.CalledProcessError as exc:
        print(f"error: git diff failed (exit {exc.returncode}): {exc.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("error: git executable not found", file=sys.stderr)
        sys.exit(1)

    return [Path(line) for line in result.stdout.splitlines() if line.endswith(".py")]


# ---------------------------------------------------------------------------
# 2. Force-full check
# ---------------------------------------------------------------------------


def _matches_force_full(changed_files: list[Path], globs: tuple[str, ...]) -> str | None:
    """Return the first glob that matches any changed file, or ``None``."""
    for changed in changed_files:
        for pattern in globs:
            if fnmatch.fnmatch(str(changed), pattern):
                return pattern
    return None


# ---------------------------------------------------------------------------
# 3. Reverse-import graph
# ---------------------------------------------------------------------------


def _iter_py_files(repo_root: Path) -> list[Path]:
    """Return every ``.py`` file under *repo_root*, skipping excluded dirs."""
    results: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune excluded directories in-place so os.walk doesn't descend.
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for fname in filenames:
            if fname.endswith(".py"):
                results.append(Path(dirpath) / fname)
    return results


def _resolve_dotted_path(base: Path, parts: list[str]) -> Path | None:
    """Try to resolve *parts* as a module file or package under *base*."""
    as_file = base / Path(*parts[:-1]) / (parts[-1] + ".py") if len(parts) > 1 else base / (parts[0] + ".py")
    if as_file.is_file():
        return as_file
    as_pkg = base / Path(*parts) / "__init__.py"
    if as_pkg.is_file():
        return as_pkg
    return None


def _resolve_module(source_roots: list[Path], module_name: str) -> Path | None:
    """Resolve a dotted module name to a ``.py`` file, searching *source_roots* in order.

    Returns ``None`` for unresolvable (third-party / stdlib) modules.
    """
    parts = module_name.split(".")
    for root in source_roots:
        result = _resolve_dotted_path(root, parts)
        if result:
            return result
    return None


def _resolve_relative_import(importer: Path, level: int, module: str | None) -> Path | None:
    """Resolve a relative import to an absolute file path via direct path math."""
    pkg_dir = importer.parent
    for _ in range(level - 1):
        pkg_dir = pkg_dir.parent

    if not module:
        init = pkg_dir / "__init__.py"
        return init if init.is_file() else None

    return _resolve_dotted_path(pkg_dir, module.split("."))


def _extract_imports(source_roots: list[Path], filepath: Path, source: str) -> set[Path]:
    """Parse *source* and return absolute paths of in-repo modules it imports."""
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        print(f"warning: skipping {filepath} (syntax error: {exc})", file=sys.stderr)
        return set()

    resolved: set[Path] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve_module(source_roots, alias.name)
                if target:
                    resolved.add(target)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                target = _resolve_relative_import(filepath, node.level, node.module)
            elif node.module:
                target = _resolve_module(source_roots, node.module)
            else:
                continue
            if target:
                resolved.add(target)
    return resolved


def _build_reverse_import_graph(repo_root: Path, source_roots: list[Path]) -> dict[Path, set[Path]]:
    """Build ``imported_file → {files that import it}`` for all ``.py`` files."""
    forward: dict[Path, set[Path]] = {}
    all_files = _iter_py_files(repo_root)

    for filepath in all_files:
        try:
            source = filepath.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"warning: cannot read {filepath}: {exc}", file=sys.stderr)
            continue
        forward[filepath] = _extract_imports(source_roots, filepath, source)

    reverse: dict[Path, set[Path]] = defaultdict(set)
    for importer, imported_set in forward.items():
        for imported in imported_set:
            reverse[imported].add(importer)
    return reverse


# ---------------------------------------------------------------------------
# 4. Transitive closure (BFS on reverse graph)
# ---------------------------------------------------------------------------


def _affected_files(changed: list[Path], reverse_graph: dict[Path, set[Path]]) -> set[Path]:
    """BFS over the reverse-import graph to find all transitively affected files."""
    visited: set[Path] = set()
    queue: deque[tuple[Path, int]] = deque()
    for path in changed:
        if path not in visited:
            visited.add(path)
            queue.append((path, 0))

    while queue:
        current, depth = queue.popleft()
        if depth >= _MAX_TRAVERSAL_DEPTH:
            continue
        for dependent in reverse_graph.get(current, set()):
            if dependent not in visited:
                visited.add(dependent)
                queue.append((dependent, depth + 1))
    return visited


# ---------------------------------------------------------------------------
# 5. Leaf-test discovery
# ---------------------------------------------------------------------------


def _is_test_file(filepath: Path) -> bool:
    """Return whether *filepath* looks like a pytest test file."""
    name = filepath.name
    return name.startswith("test_") or name.endswith("_test.py")


def _find_leaves_in_file(repo_root: Path, filepath: Path, leaf_re: re.Pattern[str]) -> list[str]:
    """Return pytest nodeids for leaf functions/methods in *filepath*."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []

    rel = str(filepath.relative_to(repo_root))
    nodeids: list[str] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if leaf_re.search(node.name):
                nodeids.append(f"{rel}::{node.name}")
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    if leaf_re.search(child.name):
                        nodeids.append(f"{rel}::{node.name}::{child.name}")
    return nodeids


def _all_leaves_in_repo(repo_root: Path, leaf_re: re.Pattern[str]) -> list[str]:
    """Return every leaf nodeid across all test files in the repo."""
    nodeids: list[str] = []
    for filepath in _iter_py_files(repo_root):
        if _is_test_file(filepath):
            nodeids.extend(_find_leaves_in_file(repo_root, filepath, leaf_re))
    return nodeids


# ---------------------------------------------------------------------------
# 6. Main logic
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select PR-affected leaf tests via AST analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-ref",
        default="origin/main",
        help="Git ref to diff against (default: origin/main).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root directory (default: cwd).",
    )
    parser.add_argument(
        "--leaf-pattern",
        required=True,
        help=(
            "Regex matched against test function names to identify leaves.  "
            "The team controls this list via CI config, not this script."
        ),
    )
    return parser.parse_args(argv)


def select_tests(
    repo_root: Path,
    base_ref: str,
    leaf_pattern: str,
) -> list[str]:
    """Core selection logic.  Returns selected nodeids."""
    repo_root = repo_root.resolve()
    leaf_re = re.compile(leaf_pattern)

    # Changed files ------------------------------------------------------------
    changed_files = _changed_py_files(repo_root, base_ref)
    abs_changed = [full for rel in changed_files if (full := (repo_root / rel).resolve()).is_file()]

    # Force-full check ---------------------------------------------------------
    matched_glob = _matches_force_full(changed_files, _FORCE_FULL_GLOBS)
    if matched_glob is not None:
        print(f"info: full selection triggered — matched glob: {matched_glob}", file=sys.stderr)
        all_leaves = set(_all_leaves_in_repo(repo_root, leaf_re))
        return sorted(all_leaves | _BASE_SUITE)

    # Reverse-import graph & BFS ----------------------------------------------
    source_roots = _discover_source_roots(repo_root)
    reverse_graph = _build_reverse_import_graph(repo_root, source_roots)
    affected = _affected_files(abs_changed, reverse_graph)

    # Leaf discovery -----------------------------------------------------------
    diff_leaves: set[str] = set()
    for filepath in affected:
        if _is_test_file(filepath):
            diff_leaves.update(_find_leaves_in_file(repo_root, filepath, leaf_re))

    return sorted(diff_leaves | _BASE_SUITE)


def _build_k_expression(nodeids: list[str]) -> str:
    """Build a pytest ``-k`` expression that selects exactly *nodeids*.

    Uses ``file.py::func`` fragments for precision so that ``test_deploy``
    does not accidentally match ``test_deploy_target_old_revision``.
    """
    if not nodeids:
        return ""
    # Use the last path component + function for precision:
    # e.g. "charm_.../test_deploy.py::test_deploy" → "test_deploy.py::test_deploy"
    terms = {nid.split("/")[-1] for nid in nodeids}
    return " or ".join(sorted(terms))


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    nodeids = select_tests(
        repo_root=args.repo_root,
        base_ref=args.base_ref,
        leaf_pattern=args.leaf_pattern,
    )
    expr = _build_k_expression(nodeids)
    if expr:
        print(expr)


if __name__ == "__main__":
    main()
