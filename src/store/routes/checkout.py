from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from ..db import get_session
from ..fulfillment import (
    DEFAULT_STRATEGY_CODE,
    FulfillmentRequest,
    available_strategies,
    calculate_totals,
    get_strategy,
)
from ..models import Order, OrderItem
from ..payments import FakePaymentProcessor, PaymentDeclined
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
    db = get_session()
    lines = get_cart_lines(db)
    if not lines:
        flash("Tu carrito está vacío.", "error")
        return redirect(url_for("cart.view_cart"))

    customer_name = request.form.get("customer_name", "").strip()
    customer_email = request.form.get("customer_email", "").strip()
    shipping_address = request.form.get("shipping_address", "").strip()
    payment_token = request.form.get("payment_token", "").strip()
    fulfillment_method = request.form.get("fulfillment_method")

    strategy = get_strategy(fulfillment_method)
    if strategy is None:
        flash("Selecciona un método de entrega válido.", "error")
        return redirect(url_for("checkout.checkout_form"))

    fulfillment_request = FulfillmentRequest.from_cart(lines, shipping_address)
    validation_error = strategy.validate(fulfillment_request)
    if validation_error:
        flash(validation_error, "error")
        return redirect(url_for("checkout.checkout_form"))

    if not all([customer_name, customer_email, payment_token]):
        flash("Completa todos los campos del checkout.", "error")
        return redirect(url_for("checkout.checkout_form"))

    for line in lines:
        if line.quantity > line.product.stock_quantity:
            flash(f"No hay suficiente inventario de {line.product.name}.", "error")
            return redirect(url_for("cart.view_cart"))

    totals = calculate_totals(strategy, fulfillment_request)

    try:
        payment_reference = FakePaymentProcessor().charge(totals.total_cents, payment_token)
    except PaymentDeclined:
        flash("El pago fue rechazado. Intenta con otro token de prueba.", "error")
        return redirect(url_for("checkout.checkout_form"))

    order = Order(
        customer_name=customer_name,
        customer_email=customer_email,
        shipping_address=fulfillment_request.shipping_address if strategy.requires_address else "",
        fulfillment_method=strategy.code,
        subtotal_cents=totals.subtotal_cents,
        shipping_cents=totals.shipping_cents,
        total_cents=totals.total_cents,
        payment_reference=payment_reference,
        status="confirmed",
    )
    db.add(order)
    db.flush()

    for line in lines:
        order.items.append(
            OrderItem(
                product_id=line.product.id,
                sku=line.product.sku,
                product_name=line.product.name,
                unit_price_cents=line.product.price_cents,
                quantity=line.quantity,
            )
        )
        line.product.stock_quantity -= line.quantity

    db.commit()
    session["cart"] = {}
    flash("¡Compra realizada con éxito!", "success")
    return redirect(url_for("orders.order_detail", order_id=order.id))
