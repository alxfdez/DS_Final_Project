"""Dependency-direction rules, checked from the source code.

    routes/checkout.py ─┐
                        ├─► ordering ─► models, fulfillment, payments
    routes/marketplace ─┘

Entry points depend on the ordering core; the core never depends on them, and
entry points never depend on each other.
"""

import ast
from pathlib import Path

import pytest

import store

PACKAGE = Path(store.__file__).parent


def _imports(relative_path: str) -> set[str]:
    """Return imported modules and imported names, e.g. {'..payments', 'FakePaymentProcessor'}."""
    tree = ast.parse((PACKAGE / relative_path).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add("." * node.level + (node.module or ""))
            found.update(alias.name for alias in node.names)
    return found


def test_marketplace_route_cannot_charge_the_payment_processor():
    """'Marketplace orders must not be charged' is guaranteed by structure:
    the route has no way to reach the processor."""
    imported = _imports("routes/marketplace.py")

    assert "..payments" not in imported
    assert "FakePaymentProcessor" not in imported
    assert "ChargeWithProcessor" not in imported
    assert "AlreadyPaid" in imported


def test_both_entry_points_use_the_shared_ordering_service():
    for route in ("routes/checkout.py", "routes/marketplace.py"):
        assert "..ordering" in _imports(route), route


@pytest.mark.parametrize(
    ("route", "other"),
    [("routes/checkout.py", "marketplace"), ("routes/marketplace.py", "checkout")],
)
def test_entry_points_do_not_depend_on_each_other(route, other):
    assert not any(other in name for name in _imports(route))


@pytest.mark.parametrize("core_module", ["ordering.py", "fulfillment.py", "payments.py", "models.py"])
def test_core_modules_do_not_depend_on_flask_or_routes(core_module):
    imported = _imports(core_module)

    assert not any(name.split(".")[0] == "flask" for name in imported)
    assert not any("routes" in name for name in imported)
