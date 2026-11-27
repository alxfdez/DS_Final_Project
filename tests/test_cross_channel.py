"""Scenarios that involve both entry points (storefront + marketplace).

They prove the two channels share one catalog, one inventory and one order
history, and that marketplace idempotency is durable (stored in the database,
not in memory).
"""

import pytest
from sqlalchemy import func, select

from store import create_app
from store.db import get_session
from store.models import Order, Product

MARKETPLACE_URL = "/api/marketplace/orders"
KEY = {"X-Marketplace-Key": "test-marketplace-key"}


@pytest.fixture()
def app(app):
    app.config["MARKETPLACE_API_KEY"] = "test-marketplace-key"
    return app


def _marketplace_payload(external_id="MKT-1001", items=None, **overrides):
    data = {
        "marketplace_order_id": external_id,
        "buyer": {"name": "Grace Hopper", "email": "grace@example.com"},
        "shipping": {"method": "standard", "address": "Av. Vallarta 123"},
        "items": items or [{"sku": "TEST-1", "quantity": 1}],
    }
    data.update(overrides)
    return data


def _marketplace(client, **kwargs):
    return client.post(MARKETPLACE_URL, json=_marketplace_payload(**kwargs), headers=KEY)


def _storefront_checkout(client, product_id, quantity=1):
    client.post(f"/cart/add/{product_id}")
    if quantity != 1:
        client.post(f"/cart/update/{product_id}", data={"quantity": str(quantity)})
    return client.post(
        "/checkout",
        data={
            "customer_name": "Ada Lovelace",
            "customer_email": "ada@example.com",
            "shipping_address": "123 Analytical Engine Way",
            "payment_token": "tok_success",
            "fulfillment_method": "standard",
        },
        follow_redirects=True,
    )


def _product_id(app, sku):
    with app.app_context():
        return get_session().scalar(select(Product.id).where(Product.sku == sku))


def _stock(app, sku):
    with app.app_context():
        return get_session().scalar(select(Product.stock_quantity).where(Product.sku == sku))


def _orders_by_source(app):
    with app.app_context():
        rows = get_session().execute(
            select(Order.source, func.count()).group_by(Order.source)
        ).all()
        return dict(rows)


# --------------------------------------------------------------------------- #
# Shared inventory
# --------------------------------------------------------------------------- #
def test_marketplace_sale_reduces_stock_seen_by_the_storefront(client, app):
    response = _marketplace(client, items=[{"sku": "TEST-2", "quantity": 2}])
    assert response.status_code == 201

    html = _storefront_checkout(client, _product_id(app, "TEST-2")).get_data(as_text=True)

    assert "No hay suficiente inventario de Second Product" in html
    assert _orders_by_source(app) == {"marketplace": 1}
    assert _stock(app, "TEST-2") == 0


def test_storefront_sale_reduces_stock_seen_by_the_marketplace(client, app):
    _storefront_checkout(client, _product_id(app, "TEST-2"), quantity=2)

    response = _marketplace(client, items=[{"sku": "TEST-2", "quantity": 1}])

    assert response.status_code == 409
    assert _orders_by_source(app) == {"storefront": 1}
    assert _stock(app, "TEST-2") == 0


def test_both_channels_share_one_order_history(client, app):
    _storefront_checkout(client, _product_id(app, "TEST-1"))
    _marketplace(client, external_id="MKT-55")

    html = client.get("/orders").get_data(as_text=True)

    assert "Ada Lovelace" in html
    assert "Grace Hopper" in html
    assert "Marketplace · MKT-55" in html
    assert _orders_by_source(app) == {"storefront": 1, "marketplace": 1}
    assert _stock(app, "TEST-1") == 3


# --------------------------------------------------------------------------- #
# Idempotency edge cases
# --------------------------------------------------------------------------- #
def test_same_id_with_different_payload_returns_the_original_order(client, app):
    """Documented decision: the first request wins; later variants are ignored."""
    first = _marketplace(client, items=[{"sku": "TEST-1", "quantity": 1}])

    second = _marketplace(client, items=[{"sku": "TEST-2", "quantity": 2}])

    assert second.status_code == 200
    assert second.get_json()["order_id"] == first.get_json()["order_id"]
    assert second.get_json()["items"] == [{"sku": "TEST-1", "quantity": 1, "unit_price_cents": 10_000}]
    assert _stock(app, "TEST-2") == 2


def test_idempotency_survives_an_app_restart(client, app):
    first = _marketplace(client)

    restarted = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATABASE": app.config["DATABASE"],
        "MARKETPLACE_API_KEY": "test-marketplace-key",
    })
    again = restarted.test_client().post(MARKETPLACE_URL, json=_marketplace_payload(), headers=KEY)

    assert again.status_code == 200
    assert again.get_json()["order_id"] == first.get_json()["order_id"]
    assert _orders_by_source(app) == {"marketplace": 1}
    assert _stock(app, "TEST-1") == 4


def test_case_is_significant_in_marketplace_order_ids(client, app):
    _marketplace(client, external_id="MKT-abc")
    response = _marketplace(client, external_id="MKT-ABC")

    assert response.status_code == 201
    assert _orders_by_source(app) == {"marketplace": 2}


def test_inactive_product_cannot_be_ordered_from_the_marketplace(client, app):
    with app.app_context():
        db = get_session()
        db.scalar(select(Product).where(Product.sku == "TEST-1")).active = 0
        db.commit()

    response = _marketplace(client)

    assert response.status_code == 422
    assert _orders_by_source(app) == {}
