"""Fulfillment options for checkout (Strategy pattern).

Each way of handing a car to the customer is a *strategy* with the same
interface: it validates the checkout data it needs and computes its own fee.
Checkout depends only on :class:`FulfillmentStrategy`, so adding a new option
(for example, a fee based on distance or zone) means adding a class and
registering it here, without changing the checkout route.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


class _PricedLine(Protocol):
    @property
    def line_total_cents(self) -> int: ...


@dataclass(frozen=True)
class FulfillmentRequest:
    """Checkout data a strategy needs to validate and price the delivery."""

    shipping_address: str
    subtotal_cents: int

    @classmethod
    def from_cart(cls, lines: Iterable[_PricedLine], shipping_address: str = "") -> FulfillmentRequest:
        return cls(
            shipping_address=shipping_address.strip(),
            subtotal_cents=sum(line.line_total_cents for line in lines),
        )


class FulfillmentStrategy(Protocol):
    code: str
    label: str
    eta: str
    requires_address: bool

    def validate(self, request: FulfillmentRequest) -> str | None:
        """Return an error message for the customer, or ``None`` if valid."""
        ...

    def fee_cents(self, request: FulfillmentRequest) -> int: ...


@dataclass(frozen=True)
class StorePickup:
    """The customer picks the car up at the agency. No address, no fee."""

    code: str = "pickup"
    label: str = "Recoger en agencia"
    eta: str = "Disponible en 1–2 días hábiles"
    requires_address: bool = False

    def validate(self, request: FulfillmentRequest) -> str | None:
        return None

    def fee_cents(self, request: FulfillmentRequest) -> int:
        return 0


@dataclass(frozen=True)
class FlatRateShipping:
    """Home delivery with a fixed fee. Standard and express are two instances."""

    code: str
    label: str
    eta: str
    flat_fee_cents: int
    requires_address: bool = True

    def validate(self, request: FulfillmentRequest) -> str | None:
        if not request.shipping_address:
            return "La dirección de entrega es obligatoria para envíos a domicilio."
        return None

    def fee_cents(self, request: FulfillmentRequest) -> int:
        return self.flat_fee_cents


DEFAULT_STRATEGY_CODE = "standard"

# Registry: display order in checkout follows insertion order.
STRATEGIES: dict[str, FulfillmentStrategy] = {
    strategy.code: strategy
    for strategy in (
        StorePickup(),
        FlatRateShipping(
            code="standard",
            label="Envío estándar",
            eta="5–7 días hábiles en camión portaautos",
            flat_fee_cents=250_000,
        ),
        FlatRateShipping(
            code="express",
            label="Envío express",
            eta="24–72 h en plataforma dedicada",
            flat_fee_cents=590_000,
        ),
    )
}


def available_strategies() -> list[FulfillmentStrategy]:
    return list(STRATEGIES.values())


def get_strategy(code: str | None) -> FulfillmentStrategy | None:
    """Look up a strategy by code.

    A missing or blank code falls back to standard shipping, which was the only
    option before this change (keeps existing clients and tests working).
    An unknown code returns ``None`` so the caller can reject it.
    """
    code = (code or "").strip() or DEFAULT_STRATEGY_CODE
    return STRATEGIES.get(code)


@dataclass(frozen=True)
class CheckoutTotals:
    subtotal_cents: int
    shipping_cents: int
    total_cents: int


def calculate_totals(strategy: FulfillmentStrategy, request: FulfillmentRequest) -> CheckoutTotals:
    shipping = strategy.fee_cents(request)
    return CheckoutTotals(
        subtotal_cents=request.subtotal_cents,
        shipping_cents=shipping,
        total_cents=request.subtotal_cents + shipping,
    )
