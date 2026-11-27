# ADR 0001 — One ordering service shared by the storefront and the marketplace

- **Status:** Accepted
- **Date:** 2026-11-26
- **Milestone:** M3 — Marketplace orders Change Request
- **Related PRs:** #9, #10, #11, #12, #13

## Context

Semiusados.com signed an agreement with an external marketplace. Orders can now
enter the system from two places:

1. the existing web storefront (session cart + HTML form);
2. a marketplace integration that sends an external order ID, buyer, shipping
   data, and SKUs with quantities.

The marketplace has already charged the buyer, so we must not charge again.
The marketplace may send the same order more than once (retries), and that must
not create duplicate orders. Both channels must use the same catalog, inventory,
fulfillment options and order history.

Before this change, the **whole ordering workflow lived inside one Flask route**,
`routes/checkout.py::place_order`. In that single function it:

- read the form and the session cart;
- chose and validated the fulfillment strategy;
- checked stock;
- calculated totals;
- charged `FakePaymentProcessor`;
- built `Order` and `OrderItem`;
- decremented stock and committed;
- cleared the cart and flashed a message.

A marketplace endpoint had two options at that point:

- **copy** that workflow, which duplicates every business rule; or
- **call the route**, which is impossible because the route depends on Flask's
  `session`, `request.form` and `flash`.

That is the design pressure: one workflow needs a second entry point, and the
current boundary (the HTTP route) is the wrong place for business rules.

## Decision

1. **Extract the workflow into `store/ordering.py`, a module that does not use Flask.**
   `place_order(db, PlaceOrder, PaymentMethod) -> PlacedOrder` owns:
   - fulfillment validation;
   - SKU resolution;
   - the stock check;
   - totals;
   - payment collection;
   - persistence in a single commit.

   Errors are domain exceptions (`InvalidFulfillment`, `InvalidOrder`,
   `UnknownSku`, `InsufficientStock`, `PaymentDeclined`).

2. **Make payment the only point of variation, behind a small protocol.**
   `PaymentMethod.collect(amount) -> reference` has two implementations:
   - `ChargeWithProcessor(token)` for the storefront;
   - `AlreadyPaid(reference)` for the marketplace.

   This is the same idea we applied in M2 with `FulfillmentStrategy`: model the
   behavior that varies, not the channel.

3. **Turn both entry points into thin adapters.**
   - `routes/checkout.py` translates the form and cart into a `PlaceOrder`, and
     translates exceptions into flash messages and redirects.
   - `routes/marketplace.py` translates JSON into a `PlaceOrder`, and
     translates exceptions into HTTP status codes.

   Neither adapter contains business rules, and neither imports the other.

4. **Make idempotency part of the order's identity, enforced by the database.**
   - `Order` gets `source` and `external_order_id`, plus a unique index on
     `(source, external_order_id)`.
   - `place_order` returns the existing order **before** validating or paying
     when the external ID is already stored.
   - If two requests race, the unique index rejects the second commit. The
     service catches `IntegrityError`, rolls back and returns the order that
     was stored first.

5. **Use an executable dependency direction.** The rule is "entry points →
   ordering → models / fulfillment / payments". It is checked by
   `tests/test_architecture.py`, which reads the imports with `ast`.

## Alternatives considered

| Alternative | Why not |
|---|---|
| **Copy the checkout workflow into a marketplace route** | Fastest today, but stock, totals, fulfillment and persistence rules would exist twice and drift. The Change Request explicitly forbids it. |
| **Add `if source == "marketplace"` inside the existing route** | Keeps one function, but mixes JSON parsing, form parsing, two kinds of payment and two kinds of response in the most overloaded function of the system (already flagged in M1 §5 and M2 §4). |
| **Full hexagonal / ports-and-adapters layout** (repositories, unit of work, DTO mappers, a package per layer) | It would give the same dependency direction with many more files and concepts. SQLAlchemy's `Session` already is our unit of work. One second entry point does not justify a repository abstraction on top of it. The course rule applies: do not adopt a style because it has a name. |
| **Separate marketplace microservice or message queue** | The marketplace calls us synchronously and retries on its side. A queue would add infrastructure, eventual consistency and a second deployment for a problem a unique index already solves. |
| **Idempotency in memory or in a separate `idempotency_keys` table** | In memory is lost on restart and not shared between processes. A separate table means two writes that must stay consistent. The key is a natural attribute of the order, so it lives on the order row. |
| **Reject repeated IDs with 409 instead of returning the order** | A retry is a normal event for an integration, not an error. Returning the original order (200) lets the marketplace treat "created" and "already created" the same way. |

## Consequences

### Positive

- **The ordering rules exist once.** The storefront and the marketplace share
  the same stock check, totals, fulfillment strategies and transaction.
- **The marketplace cannot charge by construction.** Its route does not import
  the payment processor, and an architecture test checks it.
- **Duplicates are prevented by the database.** This holds across retries, app
  restarts and simultaneous requests.
- **The core can be tested without HTTP.** `tests/test_ordering.py` drives the
  service directly, with no Flask client.
- **Storefront behavior is preserved.** The M2 storefront tests pass without
  edits.

### Negative / costs

- **One more module and four small concepts:** command, payment protocol,
  result, and domain errors. Each entry point must map domain errors to its own
  response.
- **`place_order` changed its return type** (`Order` → `PlacedOrder`), so
  callers had to adapt.
- **User-facing Spanish messages are produced by the core.** That is
  convenient now, but it mixes presentation with business rules (see technical
  debt).
- **SQLite migrations stay hand-written in `db.py`.** There are now three added
  columns and one index, all without a migration framework.

## Decisions recorded with this ADR

- **Same external ID with a different payload** returns the **original** order
  (200). The first request wins. Detecting mismatches is deferred.
- **External IDs:** whitespace is trimmed, case is kept (`MKT-1` ≠ `mkt-1`),
  and the maximum length is 64 characters.
- **Shipping fee** is calculated with our fulfillment strategies for
  marketplace orders too. We record the amounts; the marketplace collected the
  money.
- **Authentication** uses a shared key in `X-Marketplace-Key`, compared in
  constant time, with an empty key failing closed.
