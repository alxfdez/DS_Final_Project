import sqlite3
from pathlib import Path

from sqlalchemy import inspect, select

from store import create_app
from store.db import get_session
from store.models import Order

OLD_ORDERS_DDL = """
CREATE TABLE orders (
    id INTEGER NOT NULL PRIMARY KEY,
    customer_name VARCHAR(120) NOT NULL,
    customer_email VARCHAR(160) NOT NULL,
    shipping_address TEXT NOT NULL,
    subtotal_cents INTEGER NOT NULL,
    shipping_cents INTEGER NOT NULL,
    total_cents INTEGER NOT NULL,
    payment_reference VARCHAR(80) NOT NULL,
    status VARCHAR(30) NOT NULL,
    created_at DATETIME NOT NULL
)
"""


def _new_order(**overrides) -> Order:
    data = dict(
        customer_name="Ada Lovelace",
        customer_email="ada@example.com",
        shipping_address="123 Analytical Engine Way",
        subtotal_cents=10_000,
        shipping_cents=250_000,
        total_cents=260_000,
        payment_reference="pay_test",
    )
    data.update(overrides)
    return Order(**data)


def _make_old_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(OLD_ORDERS_DDL)
    connection.execute(
        "INSERT INTO orders (customer_name, customer_email, shipping_address, "
        "subtotal_cents, shipping_cents, total_cents, payment_reference, status, created_at) "
        "VALUES ('Old Customer', 'old@example.com', 'Calle Vieja 1', 10000, 250000, "
        "260000, 'pay_old', 'confirmed', '2026-01-01 00:00:00.000000')"
    )
    connection.commit()
    connection.close()


def _app_for(path: Path):
    return create_app({"TESTING": True, "DATABASE": str(path)})


def test_saved_order_keeps_fulfillment_method_when_read_again(app):
    with app.app_context():
        db = get_session()
        db.add(_new_order(fulfillment_method="express"))
        db.commit()
        order_id = db.scalar(select(Order.id))

    # A brand-new session proves the value comes from the database,
    # not from an object cached in the previous session.
    fresh_session = app.extensions["store_session_factory"]()
    try:
        assert fresh_session.get(Order, order_id).fulfillment_method == "express"
    finally:
        fresh_session.close()


def test_order_without_explicit_method_defaults_to_standard(app):
    with app.app_context():
        db = get_session()
        db.add(_new_order())
        db.commit()
        assert db.scalar(select(Order.fulfillment_method)) == "standard"


def test_init_app_adds_missing_column_to_old_database(tmp_path):
    db_path = tmp_path / "old.db"
    _make_old_database(db_path)

    app = _app_for(db_path)

    with app.app_context():
        columns = {c["name"] for c in inspect(app.extensions["store_engine"]).get_columns("orders")}
        assert "fulfillment_method" in columns


def test_existing_orders_are_preserved_as_standard_after_migration(tmp_path):
    db_path = tmp_path / "old.db"
    _make_old_database(db_path)

    app = _app_for(db_path)

    with app.app_context():
        order = get_session().scalar(select(Order))
        assert order.customer_name == "Old Customer"
        assert order.total_cents == 260_000
        assert order.fulfillment_method == "standard"


def test_migrated_database_accepts_new_fulfillment_methods(tmp_path):
    db_path = tmp_path / "old.db"
    _make_old_database(db_path)
    app = _app_for(db_path)

    with app.app_context():
        db = get_session()
        db.add(_new_order(fulfillment_method="pickup", shipping_address=""))
        db.commit()
        methods = sorted(db.scalars(select(Order.fulfillment_method)))
        assert methods == ["pickup", "standard"]


def test_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "old.db"
    _make_old_database(db_path)

    _app_for(db_path)
    app = _app_for(db_path)  # second start must not fail on "duplicate column"

    with app.app_context():
        assert get_session().scalar(select(Order.fulfillment_method)) == "standard"