"""Unit tests for the channel-independent ordering service (no HTTP involved)."""

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select

import store.ordering as ordering_module
from store.db import get_session
from store.models import Order, Product
from store.ordering import (
    AlreadyPaid,
    ChargeWithProcessor,
    InsufficientStock,
    InvalidFulfillment,
    InvalidOrder,
    OrderLine,
    PaymentDeclined,
    PlaceOrder,
    UnknownSku,
    place_order,
)


def _command(lines=(OrderLine("TEST-1", 1),), **overrides) -> PlaceOrder:
    data = dict(
        customer_name="Ada Lovelace",
        customer_email="ada@example.com",
        lines=tuple(lines),
        fulfillment_method="standard",
        shipping_address="123 Analytical Engine Way",
    )
    data.update(overrides)
    return PlaceOrder(**data)


class _SpyProcessor:
    def __init__(self):
        self.calls = []

    def charge(self, amount_cents, token):
        self.calls.append((amount_cents, token))
        return "pay_spy"


class _ExplodingPayment:
    """Fails the test if the service ever tries to collect payment."""

    def collect(self, amount_cents):
        raise AssertionError("payment must not be collected")


def _order_count(db):
    return db.scalar(select(func.count()).select_from(Order))


def _stock(db, sku):
    return db.scalar(select(Product.stock_quantity).where(Product.sku == sku))


@pytest.fixture()
def db(app):
    with app.app_context():
        yield get_session()


def test_place_order_persists_order_items_and_decrements_stock(db):
    order = place_order(
        db,
        _command([OrderLine("TEST-1", 2), OrderLine("TEST-2", 1)]),
        ChargeWithProcessor("tok_success"),
    )

    assert order.id is not None
    assert order.status == "confirmed"
    assert order.subtotal_cents == 2 * 10_000 + 25_000
    assert order.shipping_cents == 250_000
    assert order.total_cents == order.subtotal_cents + 250_000
    assert [(i.sku, i.quantity) for i in order.items] == [("TEST-1", 2), ("TEST-2", 1)]
    assert _stock(db, "TEST-1") == 3
    assert _stock(db, "TEST-2") == 1


def test_charge_with_processor_charges_the_order_total(db):
    spy = _SpyProcessor()

    order = place_order(db, _command(), ChargeWithProcessor("tok_x", processor=spy))

    assert spy.calls == [(order.total_cents, "tok_x")]
    assert order.payment_reference == "pay_spy"


def test_already_paid_records_external_reference_without_charging(db, monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("processor must not be called")

    monkeypatch.setattr("store.payments.FakePaymentProcessor.charge", _fail)

    order = place_order(db, _command(), AlreadyPaid("mkt_ABC-123"))

    assert order.payment_reference == "mkt_ABC-123"
    assert _order_count(db) == 1


def test_pickup_discards_address_and_has_no_fee(db):
    order = place_order(
        db,
        _command(fulfillment_method="pickup", shipping_address="Somewhere"),
        AlreadyPaid("mkt_1"),
    )

    assert order.fulfillment_method == "pickup"
    assert order.shipping_address == ""
    assert order.shipping_cents == 0


def test_missing_fulfillment_method_defaults_to_standard(db):
    order = place_order(db, _command(fulfillment_method=None), AlreadyPaid("mkt_1"))

    assert order.fulfillment_method == "standard"


def test_repeated_skus_are_merged_into_one_item(db):
    order = place_order(
        db,
        _command([OrderLine("TEST-1", 1), OrderLine("TEST-1", 2)]),
        AlreadyPaid("mkt_1"),
    )

    assert [(i.sku, i.quantity) for i in order.items] == [("TEST-1", 3)]
    assert _stock(db, "TEST-1") == 2


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (_command(fulfillment_method="teleport"), InvalidFulfillment),
        (_command(shipping_address=""), InvalidFulfillment),
        (_command(customer_name=" "), InvalidOrder),
        (_command(customer_email=""), InvalidOrder),
        (_command(lines=[]), InvalidOrder),
        (_command(lines=[OrderLine("TEST-1", 0)]), InvalidOrder),
        (_command(lines=[OrderLine("NOPE", 1)]), UnknownSku),
        (_command(lines=[OrderLine("TEST-2", 3)]), InsufficientStock),
        # merged quantity (3) exceeds stock (2) even though each line fits
        (_command(lines=[OrderLine("TEST-2", 2), OrderLine("TEST-2", 1)]), InsufficientStock),
    ],
)
def test_invalid_orders_are_rejected_before_payment_and_write_nothing(db, command, error):
    with pytest.raises(error):
        place_order(db, command, _ExplodingPayment())

    assert _order_count(db) == 0
    assert _stock(db, "TEST-1") == 5
    assert _stock(db, "TEST-2") == 2


def test_inactive_products_are_unknown(db):
    db.scalar(select(Product).where(Product.sku == "TEST-1")).active = 0
    db.commit()

    with pytest.raises(UnknownSku):
        place_order(db, _command(), _ExplodingPayment())


def test_declined_payment_writes_nothing(db):
    with pytest.raises(PaymentDeclined):
        place_order(db, _command(), ChargeWithProcessor("tok_fail"))

    assert _order_count(db) == 0
    assert _stock(db, "TEST-1") == 5


def test_insufficient_stock_message_is_user_facing(db):
    with pytest.raises(InsufficientStock) as excinfo:
        place_order(db, _command([OrderLine("TEST-2", 9)]), _ExplodingPayment())

    assert str(excinfo.value) == "No hay suficiente inventario de Second Product."
    assert (excinfo.value.sku, excinfo.value.requested, excinfo.value.available) == ("TEST-2", 9, 2)


def test_ordering_core_does_not_depend_on_flask():
    """Dependency direction: entry points depend on the core, never the reverse."""
    tree = ast.parse(Path(ordering_module.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(("." * node.level) + (node.module or ""))

    assert not any(name.split(".")[0] == "flask" for name in imported)
    assert not any("routes" in name for name in imported)
