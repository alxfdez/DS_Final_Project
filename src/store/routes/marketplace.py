"""Marketplace entry point: JSON in, ordering service, JSON out.

The external marketplace has already charged the buyer, so orders placed here
use :class:`~store.ordering.AlreadyPaid` and never reach our payment processor.
Repeated requests with the same ``marketplace_order_id`` return the existing
order (``200``) instead of creating a new one (``201``).

Request::

    POST /api/marketplace/orders
    X-Marketplace-Key: <MARKETPLACE_API_KEY>
    {
      "marketplace_order_id": "MKT-1001",
      "buyer":    {"name": "Ada Lovelace", "email": "ada@example.com"},
      "shipping": {"method": "standard", "address": "Av. Vallarta 123"},
      "items":    [{"sku": "SEMI-0001", "quantity": 1}]
    }

``shipping.method`` is optional and defaults to the storefront default.
"""

from __future__ import annotations

import hmac
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from ..db import get_session
from ..models import SOURCE_MARKETPLACE, Order
from ..ordering import (
    AlreadyPaid,
    InsufficientStock,
    OrderingError,
    OrderLine,
    OrderOrigin,
    PlaceOrder,
    UnknownSku,
    place_order,
)

bp = Blueprint("marketplace", __name__, url_prefix="/api/marketplace")

API_KEY_HEADER = "X-Marketplace-Key"
# payment_reference is "mkt_<id>" in a VARCHAR(80) column, and SQLite does not
# enforce declared lengths, so the limit is checked here.
MAX_EXTERNAL_ID_LENGTH = 64


class PayloadError(Exception):
    def __init__(self, details: list[str]):
        self.details = details
        super().__init__("; ".join(details))


@bp.post("/orders")
def create_order():
    if not _is_authorized():
        return _error(401, "Missing or invalid API key.")

    try:
        external_id, command = _parse_payload(request.get_json(silent=True))
    except PayloadError as error:
        return _error(400, "Invalid order payload.", error.details)

    try:
        placed = place_order(get_session(), command, AlreadyPaid(f"mkt_{external_id}"))
    except UnknownSku as error:
        return _error(422, str(error), [f"unknown sku: {error.sku}"])
    except InsufficientStock as error:
        return _error(
            409,
            str(error),
            [f"sku {error.sku}: requested {error.requested}, available {error.available}"],
        )
    except OrderingError as error:
        return _error(422, str(error))

    return jsonify(_order_json(placed.order, created=placed.created)), 201 if placed.created else 200


# --------------------------------------------------------------------------- #
# Translation helpers (input/output format belongs to this entry point only)
# --------------------------------------------------------------------------- #
def _is_authorized() -> bool:
    expected = current_app.config.get("MARKETPLACE_API_KEY") or ""
    provided = request.headers.get(API_KEY_HEADER, "")
    return bool(expected) and hmac.compare_digest(provided.encode(), expected.encode())


def _parse_payload(payload: Any) -> tuple[str, PlaceOrder]:
    if not isinstance(payload, dict):
        raise PayloadError(["body must be a JSON object"])
    errors: list[str] = []

    external_id = payload.get("marketplace_order_id")
    if not isinstance(external_id, str) or not external_id.strip():
        errors.append("marketplace_order_id is required")
    elif len(external_id.strip()) > MAX_EXTERNAL_ID_LENGTH:
        errors.append(f"marketplace_order_id must be at most {MAX_EXTERNAL_ID_LENGTH} characters")
    else:
        # Whitespace is trimmed; case is kept (the marketplace owns the format).
        external_id = external_id.strip()

    buyer = payload.get("buyer")
    buyer = buyer if isinstance(buyer, dict) else {}
    name, email = buyer.get("name"), buyer.get("email")
    if not isinstance(name, str) or not name.strip():
        errors.append("buyer.name is required")
    if not isinstance(email, str) or not email.strip():
        errors.append("buyer.email is required")

    shipping = payload.get("shipping", {})
    if not isinstance(shipping, dict):
        errors.append("shipping must be an object")
        shipping = {}
    method, address = shipping.get("method"), shipping.get("address", "")
    if method is not None and not isinstance(method, str):
        errors.append("shipping.method must be a string")
    if not isinstance(address, str):
        errors.append("shipping.address must be a string")

    items = payload.get("items")
    lines: list[OrderLine] = []
    if not isinstance(items, list) or not items:
        errors.append("items must be a non-empty list")
    else:
        for index, item in enumerate(items):
            sku = item.get("sku") if isinstance(item, dict) else None
            quantity = item.get("quantity") if isinstance(item, dict) else None
            if not isinstance(sku, str) or not sku.strip():
                errors.append(f"items[{index}].sku is required")
            # bool is a subclass of int in Python; reject true/false explicitly.
            elif isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                errors.append(f"items[{index}].quantity must be a positive integer")
            else:
                lines.append(OrderLine(sku=sku.strip(), quantity=quantity))

    if errors:
        raise PayloadError(errors)

    return external_id, PlaceOrder(
        customer_name=name,
        customer_email=email,
        lines=tuple(lines),
        fulfillment_method=method,
        shipping_address=address,
        origin=OrderOrigin(SOURCE_MARKETPLACE, external_id),
    )


def _order_json(order: Order, *, created: bool) -> dict[str, Any]:
    return {
        "created": created,
        "order_id": order.id,
        "marketplace_order_id": order.external_order_id,
        "source": order.source,
        "status": order.status,
        "fulfillment_method": order.fulfillment_method,
        "subtotal_cents": order.subtotal_cents,
        "shipping_cents": order.shipping_cents,
        "total_cents": order.total_cents,
        "payment_reference": order.payment_reference,
        "items": [
            {"sku": item.sku, "quantity": item.quantity, "unit_price_cents": item.unit_price_cents}
            for item in order.items
        ],
    }


def _error(status: int, message: str, details: list[str] | None = None):
    body: dict[str, Any] = {"error": message}
    if details:
        body["details"] = details
    return jsonify(body), status
