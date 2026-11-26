import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from store import create_app
from store.db import get_session
from store.models import ORDER_ORIGIN_UNIQUE_INDEX, Order

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

def test_every_registered_fulfillment_option_can_be_persisted(app):
    from store.fulfillment import available_strategies

    codes = [strategy.code for strategy in available_strategies()]
    with app.app_context():
        db = get_session()
        db.add_all(_new_order(fulfillment_method=code) for code in codes)
        db.commit()
        assert sorted(db.scalars(select(Order.fulfillment_method))) == sorted(codes)


def test_order_default_matches_the_default_fulfillment_strategy(app):
    from store.fulfillment import DEFAULT_STRATEGY_CODE

    with app.app_context():
        db = get_session()
        db.add(_new_order())
        db.commit()
        assert db.scalar(select(Order.fulfillment_method)) == DEFAULT_STRATEGY_CODE

# --------------------------------------------------------------------------- #
# Order origin: source + external_order_id (marketplace idempotency key)
# --------------------------------------------------------------------------- #
M2_ORDERS_DDL = OLD_ORDERS_DDL.replace(
    "payment_reference VARCHAR(80) NOT NULL,",
    "payment_reference VARCHAR(80) NOT NULL,\n"
    "    fulfillment_method VARCHAR(30) NOT NULL DEFAULT 'standard',",
)


def _marketplace_order(external_id="MKT-1001", **overrides) -> Order:
    return _new_order(source="marketplace", external_order_id=external_id, **overrides)


def _unique_indexes(app) -> dict[str, list[str]]:
    indexes = inspect(app.extensions["store_engine"]).get_indexes("orders")
    return {i["name"]: i["column_names"] for i in indexes if i["unique"]}


def test_new_orders_default_to_storefront_without_external_id(app):
    with app.app_context():
        db = get_session()
        db.add(_new_order())
        db.commit()
        order = db.scalar(select(Order))
        assert (order.source, order.external_order_id) == ("storefront", None)


def test_storefront_checkout_persists_storefront_origin(client, app, first_product_id):
    client.post(f"/cart/add/{first_product_id}")
    client.post(
        "/checkout",
        data={
            "customer_name": "Ada Lovelace",
            "customer_email": "ada@example.com",
            "shipping_address": "123 Analytical Engine Way",
            "payment_token": "tok_success",
            "fulfillment_method": "standard",
        },
    )

    with app.app_context():
        order = get_session().scalar(select(Order))
        assert (order.source, order.external_order_id) == ("storefront", None)


def test_many_storefront_orders_can_coexist_with_null_external_id(app):
    with app.app_context():
        db = get_session()
        db.add_all([_new_order(), _new_order(), _new_order()])
        db.commit()
        assert len(db.scalars(select(Order)).all()) == 3


def test_marketplace_order_keeps_its_external_id_when_read_again(app):
    with app.app_context():
        db = get_session()
        db.add(_marketplace_order("MKT-42"))
        db.commit()
        order_id = db.scalar(select(Order.id))

    fresh_session = app.extensions["store_session_factory"]()
    try:
        order = fresh_session.get(Order, order_id)
        assert (order.source, order.external_order_id) == ("marketplace", "MKT-42")
    finally:
        fresh_session.close()


def test_database_rejects_duplicate_external_order_id_for_same_source(app):
    with app.app_context():
        db = get_session()
        db.add(_marketplace_order("MKT-1001"))
        db.commit()

        db.add(_marketplace_order("MKT-1001"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        assert len(db.scalars(select(Order)).all()) == 1


def test_same_external_id_is_allowed_for_different_sources(app):
    with app.app_context():
        db = get_session()
        db.add_all([
            _marketplace_order("EXT-1"),
            _new_order(source="other-marketplace", external_order_id="EXT-1"),
        ])
        db.commit()
        assert len(db.scalars(select(Order)).all()) == 2


def test_new_database_has_unique_origin_index(app):
    assert _unique_indexes(app)[ORDER_ORIGIN_UNIQUE_INDEX] == ["source", "external_order_id"]


@pytest.mark.parametrize("ddl", [OLD_ORDERS_DDL, M2_ORDERS_DDL], ids=["m1-schema", "m2-schema"])
def test_old_databases_get_origin_columns_and_unique_index(tmp_path, ddl):
    db_path = tmp_path / "old.db"
    connection = sqlite3.connect(db_path)
    connection.execute(ddl)
    connection.execute(
        "INSERT INTO orders (customer_name, customer_email, shipping_address, "
        "subtotal_cents, shipping_cents, total_cents, payment_reference, status, created_at) "
        "VALUES ('Old Customer', 'old@example.com', 'Calle Vieja 1', 10000, 250000, "
        "260000, 'pay_old', 'confirmed', '2026-01-01 00:00:00.000000')"
    )
    connection.commit()
    connection.close()

    app = _app_for(db_path)

    columns = {c["name"] for c in inspect(app.extensions["store_engine"]).get_columns("orders")}
    assert {"fulfillment_method", "source", "external_order_id"} <= columns
    assert _unique_indexes(app)[ORDER_ORIGIN_UNIQUE_INDEX] == ["source", "external_order_id"]
    with app.app_context():
        order = get_session().scalar(select(Order))
        assert order.customer_name == "Old Customer"
        assert (order.source, order.external_order_id) == ("storefront", None)


def test_migrated_database_enforces_uniqueness(tmp_path):
    db_path = tmp_path / "old.db"
    _make_old_database(db_path)
    _app_for(db_path)
    app = _app_for(db_path)  # second start: migration must be re-runnable

    with app.app_context():
        db = get_session()
        db.add(_marketplace_order("MKT-7"))
        db.commit()
        db.add(_marketplace_order("MKT-7"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_uniqueness_survives_an_app_restart(app):
    with app.app_context():
        db = get_session()
        db.add(_marketplace_order("MKT-9"))
        db.commit()

    restarted = _app_for(Path(app.config["DATABASE"]))
    with restarted.app_context():
        db = get_session()
        db.add(_marketplace_order("MKT-9"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert len(db.scalars(select(Order)).all()) == 1
