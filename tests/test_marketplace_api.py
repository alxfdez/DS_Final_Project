"""HTTP tests for the marketplace entry point (POST /api/marketplace/orders)."""

import pytest
from sqlalchemy import func, select

from store.db import get_session
from store.models import Order, Product

URL = "/api/marketplace/orders"
KEY = {"X-Marketplace-Key": "test-marketplace-key"}


@pytest.fixture()
def app(app):
    app.config["MARKETPLACE_API_KEY"] = "test-marketplace-key"
    return app


@pytest.fixture(autouse=True)
def processor_must_not_be_called(monkeypatch):
    """Marketplace orders are already paid: any charge attempt fails the test."""

    def _fail(*args, **kwargs):
        raise AssertionError("marketplace orders must not charge the payment processor")

    monkeypatch.setattr("store.payments.FakePaymentProcessor.charge", _fail)


def _payload(**overrides):
    data = {
        "marketplace_order_id": "MKT-1001",
        "buyer": {"name": "Grace Hopper", "email": "grace@example.com"},
        "shipping": {"method": "standard", "address": "Av. Vallarta 123"},
        "items": [{"sku": "TEST-1", "quantity": 2}],
    }
    data.update(overrides)
    return data


def _post(client, payload, headers=KEY):
    return client.post(URL, json=payload, headers=headers)


def _order_count(app):
    with app.app_context():
        return get_session().scalar(select(func.count()).select_from(Order))


def _stock(app, sku):
    with app.app_context():
        return get_session().scalar(select(Product.stock_quantity).where(Product.sku == sku))


def test_marketplace_order_is_created_with_shared_inventory_and_fulfillment(client, app):
    response = _post(client, _payload())

    assert response.status_code == 201
    body = response.get_json()
    assert body["created"] is True
    assert body["marketplace_order_id"] == "MKT-1001"
    assert body["source"] == "marketplace"
    assert body["fulfillment_method"] == "standard"
    assert body["shipping_cents"] == 250_000
    assert body["total_cents"] == 2 * 10_000 + 250_000
    assert body["payment_reference"] == "mkt_MKT-1001"
    assert body["items"] == [{"sku": "TEST-1", "quantity": 2, "unit_price_cents": 10_000}]
    assert _stock(app, "TEST-1") == 3

    with app.app_context():
        order = get_session().get(Order, body["order_id"])
        assert (order.source, order.external_order_id) == ("marketplace", "MKT-1001")
        assert order.customer_name == "Grace Hopper"
        assert order.shipping_address == "Av. Vallarta 123"


def test_repeated_request_returns_the_same_order_without_duplicating(client, app):
    first = _post(client, _payload())
    second = _post(client, _payload())

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.get_json()["created"] is False
    assert second.get_json()["order_id"] == first.get_json()["order_id"]
    assert _order_count(app) == 1
    assert _stock(app, "TEST-1") == 3


def test_external_id_whitespace_is_ignored_for_deduplication(client, app):
    _post(client, _payload(marketplace_order_id="MKT-1001"))
    response = _post(client, _payload(marketplace_order_id="  MKT-1001  "))

    assert response.status_code == 200
    assert _order_count(app) == 1


def test_marketplace_order_shows_up_in_storefront_order_history(client):
    _post(client, _payload())

    html = client.get("/orders").get_data(as_text=True)

    assert "Grace Hopper" in html
    assert "Marketplace · MKT-1001" in html


def test_pickup_without_address_is_accepted(client):
    response = _post(client, _payload(shipping={"method": "pickup"}))

    assert response.status_code == 201
    assert response.get_json()["shipping_cents"] == 0


@pytest.mark.parametrize("headers", [{}, {"X-Marketplace-Key": "wrong"}])
def test_requests_without_a_valid_api_key_are_rejected(client, app, headers):
    response = _post(client, _payload(), headers=headers)

    assert response.status_code == 401
    assert _order_count(app) == 0


@pytest.mark.parametrize(
    "payload",
    [
        _payload(marketplace_order_id=""),
        _payload(marketplace_order_id="X" * 65),
        _payload(buyer={"name": "Grace"}),
        _payload(items=[]),
        _payload(items=[{"sku": "TEST-1", "quantity": 0}]),
        _payload(items=[{"sku": "TEST-1", "quantity": True}]),
        _payload(items=[{"quantity": 1}]),
        _payload(items=["TEST-1"]),
        _payload(marketplace_order_id=1001),
        _payload(buyer="Grace"),
        _payload(shipping="standard"),
        _payload(shipping={"method": 1, "address": "Av. Vallarta 123"}),
        _payload(shipping={"method": "standard", "address": ["Av. Vallarta"]}),
    ],
    ids=[
        "blank-id", "long-id", "no-email", "no-items", "zero-qty", "bool-qty", "no-sku",
        "item-not-object", "numeric-id", "buyer-not-object", "shipping-not-object",
        "method-not-string", "address-not-string",
    ],
)
def test_malformed_payloads_are_rejected_with_400(client, app, payload):
    response = _post(client, payload)

    assert response.status_code == 400
    assert response.get_json()["details"]
    assert _order_count(app) == 0


def test_non_json_body_is_rejected(client):
    response = client.post(URL, data="not json", headers=KEY)

    assert response.status_code == 400


def test_unknown_sku_is_rejected_with_422(client, app):
    response = _post(client, _payload(items=[{"sku": "NOPE", "quantity": 1}]))

    assert response.status_code == 422
    assert _order_count(app) == 0


def test_insufficient_stock_is_rejected_with_409_and_writes_nothing(client, app):
    response = _post(client, _payload(items=[{"sku": "TEST-2", "quantity": 3}]))

    assert response.status_code == 409
    assert "TEST-2" in response.get_json()["details"][0]
    assert _order_count(app) == 0
    assert _stock(app, "TEST-2") == 2


def test_shipping_without_address_is_rejected_with_422(client, app):
    response = _post(client, _payload(shipping={"method": "express"}))

    assert response.status_code == 422
    assert _order_count(app) == 0
