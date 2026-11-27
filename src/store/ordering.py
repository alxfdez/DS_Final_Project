"""Order placement shared by every entry point (storefront, marketplace, ...).

This module owns the business rules that turn "these SKUs for this customer"
into a persisted order:

0. if the external order was already placed, return it (idempotency);
1. resolve the fulfillment strategy and validate it;
2. resolve SKUs against the catalog and check inventory;
3. compute totals;
4. collect payment through a :class:`PaymentMethod`;
5. persist ``Order`` + ``OrderItem`` rows and decrement stock in ONE commit.

It deliberately does not import Flask: entry points (HTTP routes) translate
their own input (a form + session cart, a JSON payload) into a
:class:`PlaceOrder` command and translate :class:`OrderingError` subclasses
back into their own responses (flash + redirect, JSON status codes).

Dependency direction::

    routes/checkout.py ─┐
                        ├─► ordering ─► models, fulfillment, payments
    routes/marketplace ─┘
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .fulfillment import FulfillmentRequest, calculate_totals, get_strategy
from .models import SOURCE_STOREFRONT, Order, OrderItem, Product
from .payments import FakePaymentProcessor, PaymentDeclined

__all__ = [
    "AlreadyPaid",
    "ChargeWithProcessor",
    "InsufficientStock",
    "InvalidFulfillment",
    "InvalidOrder",
    "OrderLine",
    "OrderOrigin",
    "OrderingError",
    "PaymentDeclined",
    "PaymentMethod",
    "PlaceOrder",
    "PlacedOrder",
    "STOREFRONT_ORIGIN",
    "UnknownSku",
    "place_order",
]


# --------------------------------------------------------------------------- #
# Command (input)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int


@dataclass(frozen=True)
class OrderOrigin:
    """Which entry point sent the order and, for external channels, its own ID.

    ``(source, external_order_id)`` is the idempotency key: placing the same
    external order twice returns the order that already exists.
    """

    source: str
    external_order_id: str | None = None


STOREFRONT_ORIGIN = OrderOrigin(SOURCE_STOREFRONT)


@dataclass(frozen=True)
class PlaceOrder:
    """Everything the core needs to place an order, independent of the channel."""

    customer_name: str
    customer_email: str
    lines: tuple[OrderLine, ...]
    fulfillment_method: str | None = None
    shipping_address: str = ""
    origin: OrderOrigin = STOREFRONT_ORIGIN


@dataclass(frozen=True)
class PlacedOrder:
    """Result of :func:`place_order`. ``created`` is False for a repeated external order."""

    order: Order
    created: bool


# --------------------------------------------------------------------------- #
# Payment: the one step that differs between channels
# --------------------------------------------------------------------------- #
class PaymentMethod(Protocol):
    def collect(self, amount_cents: int) -> str:
        """Collect ``amount_cents`` and return a payment reference.

        Raises :class:`PaymentDeclined` when payment cannot be collected.
        """
        ...


@dataclass(frozen=True)
class ChargeWithProcessor:
    """Storefront: charge the customer through our payment processor."""

    token: str
    processor: FakePaymentProcessor = field(default_factory=FakePaymentProcessor)

    def collect(self, amount_cents: int) -> str:
        return self.processor.charge(amount_cents, self.token)


@dataclass(frozen=True)
class AlreadyPaid:
    """Marketplace: payment already happened elsewhere; never charge again."""

    reference: str

    def collect(self, amount_cents: int) -> str:
        return self.reference


# --------------------------------------------------------------------------- #
# Errors (each entry point maps these to its own response)
# --------------------------------------------------------------------------- #
class OrderingError(Exception):
    """Base class for business-rule violations. ``str(error)`` is user-facing."""


class InvalidFulfillment(OrderingError):
    pass


class InvalidOrder(OrderingError):
    pass


class UnknownSku(OrderingError):
    def __init__(self, sku: str):
        self.sku = sku
        super().__init__(f"El SKU {sku} no existe o no está disponible.")


class InsufficientStock(OrderingError):
    def __init__(self, product: Product, requested: int):
        self.sku = product.sku
        self.requested = requested
        self.available = product.stock_quantity
        super().__init__(f"No hay suficiente inventario de {product.name}.")


# --------------------------------------------------------------------------- #
# Use case
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _ResolvedLine:
    product: Product
    quantity: int

    @property
    def line_total_cents(self) -> int:
        return self.product.price_cents * self.quantity


def place_order(db: Session, command: PlaceOrder, payment: PaymentMethod) -> PlacedOrder:
    """Validate, collect payment and persist an order atomically.

    Nothing is written (and stock is untouched) if any step fails.

    When the origin carries an external order ID that was already stored, the
    existing order is returned with ``created=False`` *before* any validation
    or payment: a retry must get the original order even if, for example, the
    car has sold out since.
    """
    origin = command.origin
    if origin.external_order_id is not None:
        existing = _find_by_origin(db, origin)
        if existing is not None:
            return PlacedOrder(existing, created=False)

    strategy = get_strategy(command.fulfillment_method)
    if strategy is None:
        raise InvalidFulfillment("Selecciona un método de entrega válido.")

    lines = _resolve_lines(db, command.lines)
    fulfillment_request = FulfillmentRequest.from_cart(lines, command.shipping_address)
    error = strategy.validate(fulfillment_request)
    if error:
        raise InvalidFulfillment(error)

    if not command.customer_name.strip() or not command.customer_email.strip():
        raise InvalidOrder("Faltan los datos del cliente.")

    for line in lines:
        if line.quantity > line.product.stock_quantity:
            raise InsufficientStock(line.product, line.quantity)

    totals = calculate_totals(strategy, fulfillment_request)
    payment_reference = payment.collect(totals.total_cents)

    order = Order(
        customer_name=command.customer_name.strip(),
        customer_email=command.customer_email.strip(),
        shipping_address=fulfillment_request.shipping_address if strategy.requires_address else "",
        fulfillment_method=strategy.code,
        subtotal_cents=totals.subtotal_cents,
        shipping_cents=totals.shipping_cents,
        total_cents=totals.total_cents,
        payment_reference=payment_reference,
        status="confirmed",
        source=origin.source,
        external_order_id=origin.external_order_id,
    )
    try:
        db.add(order)
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
    except IntegrityError:
        # Another request stored the same external order between our lookup
        # and this commit; the unique index rejected ours. Return the winner.
        db.rollback()
        existing = _find_by_origin(db, origin) if origin.external_order_id is not None else None
        if existing is None:
            raise
        return PlacedOrder(existing, created=False)
    except Exception:
        db.rollback()
        raise
    return PlacedOrder(order, created=True)


def _find_by_origin(db: Session, origin: OrderOrigin) -> Order | None:
    return db.scalar(
        select(Order).where(
            Order.source == origin.source,
            Order.external_order_id == origin.external_order_id,
        )
    )


def _resolve_lines(db: Session, requested: Iterable[OrderLine]) -> list[_ResolvedLine]:
    """Merge repeated SKUs and load active products, preserving first-seen order."""
    quantities: dict[str, int] = {}
    for line in requested:
        if line.quantity <= 0:
            raise InvalidOrder("Las cantidades deben ser mayores a cero.")
        quantities[line.sku] = quantities.get(line.sku, 0) + line.quantity
    if not quantities:
        raise InvalidOrder("El pedido no tiene artículos.")

    products = {
        product.sku: product
        for product in db.scalars(
            select(Product).where(Product.sku.in_(quantities), Product.active == 1)
        )
    }
    resolved: list[_ResolvedLine] = []
    for sku, quantity in quantities.items():
        product = products.get(sku)
        if product is None:
            raise UnknownSku(sku)
        resolved.append(_ResolvedLine(product=product, quantity=quantity))
    return resolved
