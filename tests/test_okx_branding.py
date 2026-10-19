"""The word OKX must never reach the user.

Internal identifiers (``OKX_*`` error codes), the ``okx`` package name, the
``TransactionRecord.source == "okx"`` field, and docstrings are fine, but any
user-visible message the backend emits must not name the provider.
"""

import ast
import pathlib

_USER_FACING_MODULES = [
    "src/wallet_agent/graph/nodes.py",
    "src/wallet_agent/api/app.py",
    "src/wallet_agent/portfolio.py",
    "src/wallet_agent/graph/read_only.py",
    "src/wallet_agent/okx/client.py",
    "src/wallet_agent/okx/wallet.py",
    "src/wallet_agent/okx/explorer.py",
    "src/wallet_agent/okx/errors.py",
    "src/wallet_agent/prices/okx.py",
    "src/wallet_agent/models/registry.py",
    "src/wallet_agent/models/contracts.py",
]


def _docstring_ids(tree: ast.AST) -> set[int]:
    docstrings: set[int] = set()
    for container in ast.walk(tree):
        if not isinstance(
            container,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        body = getattr(container, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))
    return docstrings


def test_no_user_facing_string_names_okx():
    repo = pathlib.Path(__file__).resolve().parents[1]
    hits: list[str] = []
    for rel in _USER_FACING_MODULES:
        path = repo / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if id(node) in docstrings:
                continue
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            if "OKX" in value and "OKX_" not in value:
                hits.append(f"{rel}:{getattr(node, 'lineno', '?')}: {value!r}")
    assert not hits, "user-facing strings must not mention OKX:\n" + "\n".join(hits)
