import pytest
from sqlalchemy import func, select

from store import create_app
from store.db import get_session
from store.models import Order, Product

NEW_METHODS = ["pickup", "express"]
LABELS = {"pickup": "Recoger en agencia", "standard": "Envío estándar", "express": "Envío express"}
FEES_CENTS = {"pickup": 0, "standard": 250_000, "express": 590_000}


def _checkout(client, product_id, method, quantity=1, token="tok_success"):
    client.post(f"/cart/add/{product_id}")
    if quantity != 1:
        client.post(f"/cart/update/{product_id}", data={"quantity": str(quantity)})
    return client.post(
        "/checkout",
        data={
            "customer_name": "Ada Lovelace",
            "customer_email": "ada@example.com",
            "shipping_address": "" if method == "pickup" else "Av. Vallarta 123",
            "payment_token": token,
            "fulfillment_method": method,
        },
        follow_redirects=True,
    )


def _order_count(app):
    with app.app_context():
        return get_session().scalar(select(func.count()).select_from(Order))


def _stock(app, product_id):
    with app.app_context():
        return get_session().get(Product, product_id).stock_quantity


@pytest.mark.parametrize("method", NEW_METHODS)
def test_payment_is_charged_the_total_including_the_fee(client, first_product_id, monkeypatch, method):
    charged = []

    class SpyProcessor:
        def charge(self, amount_cents, token):
            charged.append(amount_cents)
            return "pay_spy"

    monkeypatch.setattr("store.routes.checkout.FakePaymentProcessor", SpyProcessor)

    _checkout(client, first_product_id, method)

    assert charged == [10_000 + FEES_CENTS[method]]


@pytest.mark.parametrize("method", NEW_METHODS)
def test_inventory_decreases_with_new_options(client, app, first_product_id, method):
    _checkout(client, first_product_id, method, quantity=2)

    assert _stock(app, first_product_id) == 3


@pytest.mark.parametrize("method", NEW_METHODS)
def test_declined_payment_creates_no_order_with_new_options(client, app, first_product_id, method):
    response = _checkout(client, first_product_id, method, token="tok_fail")

    assert "El pago fue rechazado" in response.get_data(as_text=True)
    assert _order_count(app) == 0
    assert _stock(app, first_product_id) == 5


@pytest.mark.parametrize("method", NEW_METHODS)
def test_confirmation_page_shows_chosen_option(client, first_product_id, method):
    html = _checkout(client, first_product_id, method).get_data(as_text=True)

    assert LABELS[method] in html
    if method == "pickup":
        assert "Dirección de entrega" not in html
    else:
        assert "Dirección de entrega" in html


def test_order_history_shows_the_option_of_each_order(client, first_product_id):
    for method in LABELS:
        _checkout(client, first_product_id, method)

    html = client.get("/orders").get_data(as_text=True)

    for label in LABELS.values():
        assert label in html


def test_chosen_option_survives_an_app_restart(client, app, first_product_id):
    _checkout(client, first_product_id, "express")

    restarted = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATABASE": app.config["DATABASE"],
        "STORE_NAME": "Test Store",
    })
    html = restarted.test_client().get("/orders/1").get_data(as_text=True)

    assert LABELS["express"] in html