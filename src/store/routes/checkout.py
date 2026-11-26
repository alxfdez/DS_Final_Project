from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ..db import get_session
from ..fulfillment import (
    DEFAULT_STRATEGY_CODE,
    FulfillmentRequest,
    available_strategies,
    calculate_totals,
)
from ..ordering import (
    ChargeWithProcessor,
    InsufficientStock,
    OrderingError,
    OrderLine,
    PaymentDeclined,
    PlaceOrder,
    UnknownSku,
)
from ..ordering import place_order as place_order_service
from ..payments import FakePaymentProcessor
from .cart import get_cart_lines

bp = Blueprint("checkout", __name__, url_prefix="/checkout")


@bp.get("")
def checkout_form():
    db = get_session()
    lines = get_cart_lines(db)
    if not lines:
        flash("Tu carrito está vacío.", "error")
        return redirect(url_for("cart.view_cart"))
    fulfillment_request = FulfillmentRequest.from_cart(lines)
    fulfillment_options = []
    for strategy in available_strategies():
        totals = calculate_totals(strategy, fulfillment_request)
        fulfillment_options.append({
            "strategy": strategy,
            "shipping_cents": totals.shipping_cents,
            "total_cents": totals.total_cents,
        })
    default_option = next(
        option for option in fulfillment_options
        if option["strategy"].code == DEFAULT_STRATEGY_CODE
    )
    return render_template(
        "checkout.html",
        lines=lines,
        subtotal_cents=fulfillment_request.subtotal_cents,
        fulfillment_options=fulfillment_options,
        default_fulfillment_method=DEFAULT_STRATEGY_CODE,
        default_option=default_option,
    )


@bp.post("")
def place_order():
    """Storefront adapter: form + session cart -> ordering service -> flash/redirect.

    The ordering rules (fulfillment, stock, totals, payment, persistence) live
    in :mod:`store.ordering`; this route only translates its input and errors.
    """
    db = get_session()
    lines = get_cart_lines(db)
    if not lines:
        flash("Tu carrito está vacío.", "error")
        return redirect(url_for("cart.view_cart"))

    customer_name = request.form.get("customer_name", "").strip()
    customer_email = request.form.get("customer_email", "").strip()
    payment_token = request.form.get("payment_token", "").strip()

    # Form completeness is a storefront concern (the token only exists here).
    if not all([customer_name, customer_email, payment_token]):
        flash("Completa todos los campos del checkout.", "error")
        return redirect(url_for("checkout.checkout_form"))

    command = PlaceOrder(
        customer_name=customer_name,
        customer_email=customer_email,
        shipping_address=request.form.get("shipping_address", ""),
        fulfillment_method=request.form.get("fulfillment_method"),
        lines=tuple(OrderLine(sku=line.product.sku, quantity=line.quantity) for line in lines),
    )
    payment = ChargeWithProcessor(payment_token, processor=FakePaymentProcessor())

    try:
        order = place_order_service(db, command, payment)
    except (InsufficientStock, UnknownSku) as error:
        flash(str(error), "error")
        return redirect(url_for("cart.view_cart"))
    except PaymentDeclined:
        flash("El pago fue rechazado. Intenta con otro token de prueba.", "error")
        return redirect(url_for("checkout.checkout_form"))
    except OrderingError as error:
        flash(str(error), "error")
        return redirect(url_for("checkout.checkout_form"))

    session["cart"] = {}
    flash("¡Compra realizada con éxito!", "success")
    return redirect(url_for("orders.order_detail", order_id=order.id))
