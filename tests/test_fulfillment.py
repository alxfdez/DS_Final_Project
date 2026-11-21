from dataclasses import dataclass

from store import fulfillment
from store.fulfillment import (
    FlatRateShipping,
    FulfillmentRequest,
    StorePickup,
    available_strategies,
    calculate_totals,
    get_strategy,
)


@dataclass
class _Line:
    line_total_cents: int


def _request(address="Av. Vallarta 123, Guadalajara", subtotal=100_000):
    return FulfillmentRequest(shipping_address=address, subtotal_cents=subtotal)


def test_registry_offers_the_three_required_options_in_order():
    assert [s.code for s in available_strategies()] == ["pickup", "standard", "express"]


def test_store_pickup_is_free_and_does_not_require_address():
    pickup = get_strategy("pickup")
    request = _request(address="")
    assert pickup.requires_address is False
    assert pickup.validate(request) is None
    assert pickup.fee_cents(request) == 0


def test_standard_shipping_fee_and_address_rule():
    standard = get_strategy("standard")
    assert standard.requires_address is True
    assert standard.fee_cents(_request()) == 250_000
    assert standard.validate(_request()) is None
    assert standard.validate(_request(address="")) is not None


def test_express_shipping_fee_and_address_rule():
    express = get_strategy("express")
    assert express.fee_cents(_request()) == 590_000
    assert express.validate(_request(address="")) is not None


def test_missing_code_defaults_to_standard_for_backward_compatibility():
    assert get_strategy(None).code == "standard"
    assert get_strategy("").code == "standard"
    assert get_strategy("  ").code == "standard"


def test_unknown_code_is_rejected():
    assert get_strategy("teleport") is None


def test_request_from_cart_sums_lines_and_strips_address():
    request = FulfillmentRequest.from_cart([_Line(100), _Line(250)], "  Calle 1  ")
    assert request.subtotal_cents == 350
    assert request.shipping_address == "Calle 1"


def test_whitespace_only_address_is_rejected_for_shipping():
    request = FulfillmentRequest.from_cart([_Line(100)], "   ")
    assert get_strategy("standard").validate(request) is not None


def test_calculate_totals_adds_the_strategy_fee():
    totals = calculate_totals(get_strategy("express"), _request(subtotal=44_990_000))
    assert totals.subtotal_cents == 44_990_000
    assert totals.shipping_cents == 590_000
    assert totals.total_cents == 45_580_000

    pickup_totals = calculate_totals(StorePickup(), _request(subtotal=44_990_000))
    assert pickup_totals.total_cents == 44_990_000


@dataclass(frozen=True)
class _DistanceShipping:
    """A strategy with its own pricing rule, defined outside the module."""

    code: str = "regional"
    label: str = "Envío regional"
    eta: str = "3 días"
    requires_address: bool = True

    def validate(self, request):
        return None if request.shipping_address else "Falta dirección"

    def fee_cents(self, request):
        return 1_000 if "Guadalajara" in request.shipping_address else 9_000


def test_new_strategy_can_be_registered_without_changing_existing_code(monkeypatch):
    monkeypatch.setitem(fulfillment.STRATEGIES, "regional", _DistanceShipping())

    strategy = get_strategy("regional")
    assert strategy is not None
    assert calculate_totals(strategy, _request(subtotal=100)).total_cents == 1_100
    assert calculate_totals(strategy, _request(address="Mérida", subtotal=100)).total_cents == 9_100
    assert "regional" in [s.code for s in available_strategies()]


def test_flat_rate_instances_share_behavior_but_not_fee():
    cheap = FlatRateShipping(code="a", label="A", eta="", flat_fee_cents=1)
    pricey = FlatRateShipping(code="b", label="B", eta="", flat_fee_cents=2)
    assert cheap.fee_cents(_request()) == 1
    assert pricey.fee_cents(_request()) == 2
    assert cheap.validate(_request(address="")) == pricey.validate(_request(address=""))
