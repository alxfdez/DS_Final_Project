from sqlalchemy import func, select

from store.db import get_session
from store.models import Order, Product


def _checkout(
    client,
    product_id,
    token="tok_success",
    quantity=1,
    fulfillment_method="standard",
    address="123 Analytical Engine Way",
):
    client.post(f"/cart/add/{product_id}")
    if quantity != 1:
        client.post(f"/cart/update/{product_id}", data={"quantity": str(quantity)})
    data = {
        "customer_name": "Ada Lovelace",
        "customer_email": "ada@example.com",
        "shipping_address": address,
        "payment_token": token,
    }
    if fulfillment_method is not None:
        data["fulfillment_method"] = fulfillment_method
    return client.post(
        "/checkout",
        data=data,
        follow_redirects=True,
    )


def test_checkout_form_displays_fulfillment_options_and_costs(client, first_product_id):
    client.post(f"/cart/add/{first_product_id}")

    response = client.get("/checkout")

    assert response.status_code == 200
    assert b"Recoger en agencia" in response.data
    assert "Envío estándar".encode() in response.data
    assert "Envío express".encode() in response.data
    assert b"$2,500.00 MXN" in response.data
    assert b"$5,900.00 MXN" in response.data


def test_checkout_happy_path_creates_order(client, app, first_product_id):
    response = _checkout(client, first_product_id)
    assert response.status_code == 200
    assert b"Pedido #1" in response.data
    with app.app_context():
        order = get_session().scalar(select(Order))
        assert get_session().scalar(select(func.count()).select_from(Order)) == 1
        assert order.fulfillment_method == "standard"
        assert order.shipping_cents == 250_000
        assert order.total_cents == order.subtotal_cents + 250_000


def test_checkout_defaults_missing_fulfillment_method_to_standard(client, app, first_product_id):
    _checkout(client, first_product_id, fulfillment_method=None)

    with app.app_context():
        order = get_session().scalar(select(Order))
        assert order.fulfillment_method == "standard"
        assert order.shipping_cents == 250_000


def test_express_checkout_persists_strategy_fee(client, app, first_product_id):
    _checkout(client, first_product_id, fulfillment_method="express")

    with app.app_context():
        order = get_session().scalar(select(Order))
        assert order.fulfillment_method == "express"
        assert order.shipping_cents == 590_000
        assert order.total_cents == order.subtotal_cents + 590_000


def test_pickup_checkout_does_not_require_or_store_address(client, app, first_product_id):
    response = _checkout(
        client,
        first_product_id,
        fulfillment_method="pickup",
        address="",
    )

    assert "Compra realizada con éxito".encode() in response.data
    with app.app_context():
        order = get_session().scalar(select(Order))
        assert order.fulfillment_method == "pickup"
        assert order.shipping_address == ""
        assert order.shipping_cents == 0
        assert order.total_cents == order.subtotal_cents


def test_pickup_discards_submitted_shipping_address(client, app, first_product_id):
    _checkout(client, first_product_id, fulfillment_method="pickup")

    with app.app_context():
        order = get_session().scalar(select(Order))
        assert order.shipping_address == ""


def test_invalid_fulfillment_method_is_rejected(client, app, first_product_id):
    response = _checkout(client, first_product_id, fulfillment_method="teleport")

    assert b"m\xc3\xa9todo de entrega v\xc3\xa1lido" in response.data
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(Order)) == 0
        assert get_session().get(Product, first_product_id).stock_quantity == 5


def test_shipping_method_requires_address(client, app, first_product_id):
    response = _checkout(client, first_product_id, address="")

    assert b"direcci\xc3\xb3n de entrega es obligatoria" in response.data
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(Order)) == 0


def test_checkout_persists_order_items(client, app, first_product_id):
    _checkout(client, first_product_id, quantity=2)
    with app.app_context():
        order = get_session().scalar(select(Order))
        assert len(order.items) == 1
        assert order.items[0].quantity == 2
        assert order.items[0].unit_price_cents == 10000


def test_checkout_decreases_inventory(client, app, first_product_id):
    _checkout(client, first_product_id, quantity=2)
    with app.app_context():
        product = get_session().get(Product, first_product_id)
        assert product.stock_quantity == 3


def test_insufficient_inventory_is_rejected(client, app, first_product_id):
    response = _checkout(client, first_product_id, quantity=8)
    assert b"No hay suficiente inventario" in response.data
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(Order)) == 0
        assert get_session().get(Product, first_product_id).stock_quantity == 5


def test_payment_failure_does_not_create_order(client, app, first_product_id):
    response = _checkout(client, first_product_id, token="tok_fail")
    assert b"El pago fue rechazado" in response.data
    with app.app_context():
        assert get_session().scalar(select(func.count()).select_from(Order)) == 0
        assert get_session().get(Product, first_product_id).stock_quantity == 5


def test_order_history_displays_persisted_order(client, first_product_id):
    _checkout(client, first_product_id)
    response = client.get("/orders")
    assert response.status_code == 200
    assert b"Pedido #1" in response.data
    assert b"Ada Lovelace" in response.data
