"""Category 9: Governance invariant tests.

These tests are load-bearing infrastructure. They must pass after every change that
touches executor.py, sub_agent.py, or any connector. The intent: verify that no code
path reaches a connector module directly — all calls MUST route through tool_broker.execute.
"""
from __future__ import annotations

import ast
import pathlib
import textwrap

# Connectors that may only be imported by specific gateway modules.
_CONNECTOR_MODULES = {
    "connectors.gmail",
    "connectors.browser",
    "connectors.filesystem",
    "connectors.code",
    "connectors.mcp_client",
    "connectors.registry",
}

# Only these modules are allowed to import connectors directly.
_ALLOWED_DIRECT_IMPORTERS = {
    "core/tool_broker.py",        # the gateway itself
    "connectors/registry.py",     # registry wires connectors together
    "connectors/__init__.py",
    "connectors/framework",       # framework internals
    "tests/",                     # tests may import connectors for mocking
}

API_ROOT = pathlib.Path(__file__).resolve().parents[1]  # apps/api/


def _is_allowed(rel_path: str) -> bool:
    return any(rel_path.startswith(prefix) for prefix in _ALLOWED_DIRECT_IMPORTERS)


def _get_direct_connector_imports(source: str) -> list[str]:
    """Return connector module names imported in this source file."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == m or alias.name.startswith(m + ".") for m in _CONNECTOR_MODULES):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and any(
                node.module == m or node.module.startswith(m + ".") for m in _CONNECTOR_MODULES
            ):
                found.append(node.module)
    return found


def test_no_direct_connector_imports_outside_gateway():
    """No module other than the broker and registry may import connectors directly."""
    violations: list[str] = []

    for py_file in API_ROOT.rglob("*.py"):
        rel = str(py_file.relative_to(API_ROOT))
        if _is_allowed(rel):
            continue

        source = py_file.read_text(errors="replace")
        imports = _get_direct_connector_imports(source)
        if imports:
            violations.append(f"{rel}: imports {imports}")

    assert not violations, (
        "The following files import connectors directly (must go through tool_broker.execute):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_tool_broker_execute_signature_is_frozen():
    """tool_broker.execute must accept exactly (agent, tool, args) — never add optional kwargs.

    We use AST analysis (not runtime import) because tool_broker imports DB at module level
    which requires a live DB connection.
    """
    broker_path = API_ROOT / "core" / "tool_broker.py"
    source = broker_path.read_text()
    tree = ast.parse(source)

    # Find the top-level `async def execute(...)` function.
    execute_node: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute":
            # Must be top-level (not a method inside a class).
            execute_node = node
            break

    assert execute_node is not None, "tool_broker.execute function not found"
    params = [arg.arg for arg in execute_node.args.args]
    assert params == ["agent", "tool", "args"], (
        f"tool_broker.execute signature changed — expected ['agent', 'tool', 'args'], got {params}. "
        "This function has 200+ call sites; the signature is frozen."
    )


def test_audit_log_table_is_append_only():
    """Verify audit_log migrations do not add UPDATE or DELETE grants."""
    migrations_dir = API_ROOT / "migrations" / "versions"
    for migration in migrations_dir.glob("*.py"):
        source = migration.read_text()
        # Look for GRANT UPDATE or GRANT DELETE on audit_log
        lowered = source.lower()
        if "audit_log" in lowered:
            assert "grant update" not in lowered, (
                f"{migration.name} grants UPDATE on audit_log — audit log must be append-only."
            )
            assert "grant delete" not in lowered, (
                f"{migration.name} grants DELETE on audit_log — audit log must be append-only."
            )
