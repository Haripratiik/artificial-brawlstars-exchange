# The trading API

The exchange has a browser front end for people and this for programs. They are
different problems: a browser holds a cookie scoped to one machine and renders
numbers for eyes, while an algorithm needs a credential it can carry and rotate,
a stable machine-readable refusal, and numbers it can compute with. Both resolve
to the same seat at the venue, so a key trades the account its owner sees on
screen.

**No real money and no real securities are involved.** This is a simulated
exchange. The underlyings are public Brawl Stars battle statistics, every
counterparty is a simulated agent, the capital is imaginary, and nothing here
connects to any real venue. An order placed through this API buys nothing.

- [Getting started](#getting-started)
- [Conventions](#conventions)
- [Authentication](#authentication)
- [A worked signing example](#a-worked-signing-example)
- [Errors](#errors)
- [Endpoints](#endpoints)
- [The stream](#the-stream)
- [What is not verified here](#what-is-not-verified-here)

---

## Getting started

**1. Get a key.** A key is a `key_id` and a `secret`. The secret is shown once,
at the moment it is issued, and there is nowhere to read it from afterwards --
that is the property that makes a leaked key store the only way it escapes. The
key store is in memory alongside the market it authorises, so a venue restart
invalidates every key, deliberately: a key that outlived the exchange would
authenticate against an account that no longer exists.

`POST /v1/keys` is itself a signed endpoint, so it mints *additional* keys for a
seat that already has one, which is what rotation needs. How the **first** key
reaches a new seat is the dashboard's business, and is one of the things this
document could not check against a running server -- see
[What is not verified here](#what-is-not-verified-here).

**2. Install the client.**

```
pip install httpx
export PYTHONPATH=clients/python
```

**3. Read the market. This needs no credential.**

```python
from arena_client import ArenaClient

client = ArenaClient("http://localhost:8000")
book = client.book("SPIKE_WR_FUT", depth=5)
print(book["bids"][0].price)      # Decimal('4689.00')
```

**4. Place an order.**

```python
from decimal import Decimal
from arena_client import ArenaClient

client = ArenaClient("http://localhost:8000", key_id="ak_...", secret="...")
instrument = client.instrument("SPIKE_WR_FUT")
best_bid = client.book("SPIKE_WR_FUT")["bids"][0].price
order = client.place_order(
    "SPIKE_WR_FUT",
    "buy",
    1,
    price=best_bid - instrument["tick_size"],   # exact: Decimal, not float
    time_in_force="post_only",
    client_order_id="my-first-order",
)
```

The signing happens because the client was constructed with a key. There is no
separate login step and no token to refresh.

A longer version of the same path, which reads the book, rests an order behind
the touch, reads it back and cancels it, is in
[`clients/python/examples/quote_and_trade.py`](../clients/python/examples/quote_and_trade.py).

---

## Conventions

### Base URL and version

Every path begins `/v1/`. If the venue is mounted under a path prefix, that
prefix is part of the path the server receives and therefore part of what the
signature covers.

### Money and prices are strings, and must be parsed exactly

Every price, balance and PnL figure is published as a **JSON string** holding a
decimal numeral, and must be read into an exact decimal type. Not a float.

This is not fastidiousness. The exchange counts all cash in integer minor units
at a scale of 1,000,000 precisely so that its conservation check returns integer
zero rather than something within a tolerance, and it matches on an integer tick
grid so that price-time priority is unambiguous. A client that reads a price
with `float()` reintroduces exactly the error the venue was built to avoid, at
the last possible step, silently.

The size of it is measurable rather than theoretical. Average cost is a ratio of
two exact integers, so it is as long as it needs to be, and a real position in a
running market published this:

```
3479.328892044943820224719101      what the venue sent, 28 significant digits
3479.328892044944                  what float() keeps, 17
```

The eleven digits in between are what makes `basis - closed_basis` reconstruct
the original exactly when part of a position closes. Round them and roughly
1e-24 of value evaporates per fill: invisible in any single figure, and fatal to
the one check that makes a PnL statement believable.

Two shapes to expect:

- **Exponent notation is valid.** A future bounded at ten thousand publishes its
  upper bound as `"1E+4"`, because that is what a scaled decimal renders as.
  `Decimal("1E+4")` reads it exactly; `int("1E+4")` raises.
- **A price may be absent.** A market order has no price, and the field is
  `null` rather than `0`. Zero is a real price on most of these contracts.

Send prices as strings too, for the same reason: a JSON number is a float to
most readers, and a quote one part in 2^53 off the tick grid is a rejection
rather than a rounding.

### Quantities are whole lots

Integers, never fractional. Sent as JSON numbers.

### Timestamps

The signing timestamp is whole seconds since the Unix epoch, sent as a string.
Contract dates such as `expiry` are ISO 8601 in UTC, for example
`2026-09-28T00:00:00Z`.

---

## Authentication

Public market data needs nothing. Everything touching an account or an order is
signed.

**Requests are signed, not merely labelled.** A bearer token in a header is
replayable by anything that observes it once, and it says nothing about the
request it accompanies. Here the signature covers the timestamp, the method, the
path and the body together, so a captured signature cannot be moved onto a
different order.

The scheme is HMAC-SHA256 over a shared secret, which is what Alpaca and
Coinbase do and what a standard library gives you without a dependency. The cost
is worth naming rather than implying there is none: the server must hold the
secret in order to verify a signature, so anyone who can read the key store can
sign as the key's owner. Kalshi avoids this by taking an RSA *public* key and
verifying a signature it cannot itself produce, which is strictly better and
needs a crypto library this project does not otherwise want.

### Headers

| Header | Value |
| --- | --- |
| `arena-key-id` | the key id, for example `ak_0011223344556677` |
| `arena-timestamp` | whole seconds since the epoch, as a string |
| `arena-signature` | lowercase hex HMAC-SHA256, 64 characters |

### The canonical string

The bytes under the HMAC are four fields joined by a single newline (`0x0A`):

```
<timestamp> \n <METHOD> \n <path> \n <body>
```

- **timestamp** is the exact string sent in `arena-timestamp`. Not a
  re-rendering of the same instant -- read the clock once and use one string in
  both places.
- **METHOD** is uppercase.
- **path** is the request path **including its query string**, exactly as sent.
  `/v1/account/fills?limit=50`, not `/v1/account/fills`. This is what stops a
  signature obtained for `?limit=1` being lifted onto `?limit=100000`.
- **body** is the raw request body bytes. For a request with no body this is
  empty, so the canonical string ends with a trailing newline.

Newline-separated rather than concatenated, because concatenation lets two
different requests produce identical bytes: path `/v1/orders` with body `x` and
path `/v1/order` with body `sx` are indistinguishable once joined.

### Body bytes

A signature covers bytes, so both sides must produce the same bytes from the
same object. JSON bodies are serialised with **sorted keys and no incidental
whitespace**:

```python
json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
```

The reference implementation is `body_bytes` in
[`python/arena/api/keys.py`](../python/arena/api/keys.py). If you build the body
some other way, sign the bytes you are actually going to send, not a
re-serialisation of the object they came from.

### Clock skew

A request whose timestamp is more than **30 seconds** from the venue's clock is
refused. The window has to be wide enough to survive ordinary drift between two
machines and narrow enough that a captured signature is not replayable for long.

### Reference implementation

```python
import hmac, time
from hashlib import sha256

def sign(secret, method, path, timestamp, body=b""):
    message = b"\n".join([
        timestamp.encode(), method.upper().encode(), path.encode(), body or b"",
    ])
    return hmac.new(secret.encode(), message, sha256).hexdigest()

timestamp = str(int(time.time()))
headers = {
    "arena-key-id": key_id,
    "arena-timestamp": timestamp,
    "arena-signature": sign(secret, "GET", "/v1/account", timestamp),
}
```

---

## A worked signing example

Fixed inputs and the exact answers, so an implementation in another language can
be checked against a known result rather than against a 401. These values are
asserted in [`tests/test_api_client.py`](../tests/test_api_client.py) against
the venue's own `sign`, so this section cannot drift away from the code.

**The key.** A documentation vector, not a real credential:

```
key_id  ak_0011223344556677
secret  0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0
```

### Example 1: a signed POST with a body

Request:

```
POST /v1/orders
timestamp 1787000000
```

Body object, before serialisation:

```json
{"symbol": "SPIKE_WR_FUT", "side": "buy", "quantity": 5, "price": "4663.25"}
```

Body bytes, after sorting the keys and stripping whitespace (68 bytes):

```
{"price":"4663.25","quantity":5,"side":"buy","symbol":"SPIKE_WR_FUT"}
```

Canonical string, with the newlines written out (96 bytes):

```
1787000000\nPOST\n/v1/orders\n{"price":"4663.25","quantity":5,"side":"buy","symbol":"SPIKE_WR_FUT"}
```

Checkpoints:

```
sha256 of the canonical bytes
  cdbd935cf1f5853ac35b688c3d251d5e3df3ae761ef2e41e9d627037cd4c8bf5

arena-signature (HMAC-SHA256, hex)
  05d1de8528b12f2781fb9dc6f81359df95579f59245429c63a40cc047ed13ef2
```

The digest of the canonical bytes is there to bisect a mismatch. If your digest
differs, the bug is in how you built the string. If it matches and the signature
does not, the bug is in the HMAC or in how you encoded the secret -- it is the
UTF-8 bytes of the hex string above, not the 32 bytes it decodes to.

### Example 2: a signed GET with a query string and no body

```
GET /v1/instruments/SPIKE_WR_FUT/book?depth=5
timestamp 1787000000
```

Canonical string (57 bytes). Note the trailing newline: the empty body is still
a field, so the string ends with the separator.

```
1787000000\nGET\n/v1/instruments/SPIKE_WR_FUT/book?depth=5\n
```

```
sha256 of the canonical bytes
  78d2576d0db935a9849b6dd335453c02c4ab44a4bd6863523ad35a3620deefcd

arena-signature
  37e60b358fd1f467edd50d22df11ea23322c86e3ad404401b29b323751c0cc1a
```

### Example 3: the simplest signed request

```
GET /v1/account
timestamp 1787000000
```

Canonical string (27 bytes):

```
1787000000\nGET\n/v1/account\n
```

```
sha256 of the canonical bytes
  4c4a43e14aa874424dfd28576b1ba3c7cf30ce41642d582249182cb4c401f769

arena-signature
  d43201651053cbb3926d84cefc6ecba11c27e03ffa6bf49fab310385691fd89f
```

### Two ways to be wrong that look identical

Both of these produce a well-formed request that is refused, with nothing in the
response to tell them apart:

- **Signing a path your HTTP library then re-encodes.** If you hand a client
  parameters and let it build the query, the string it sends may not be the
  string you signed. Build the path yourself and sign that.
- **Signing an object rather than the bytes.** If you sign
  `json.dumps(payload, sort_keys=True)` and then let the library re-serialise
  `payload` with its own separators, the bytes differ by two spaces per key and
  every signature fails.

`arena_client` guards against the first by comparing the path it signed against
the path the request is about to send, and refusing locally with
`client_path_mismatch` rather than spending a round trip on a 401 that explains
nothing.

---

## Errors

Every failure has the same shape:

```json
{"error": {"code": "invalid_price", "message": "price must be a number on the instrument's tick grid"}}
```

Some carry a `detail` object with the specifics:

```json
{"error": {"code": "invalid_price", "message": "...", "detail": {"symbol": "SPIKE_WR_FUT", "tick_size": "0.25"}}}
```

**The code is the contract; the sentence is free to improve.** Branch on the
code. A trading client cannot branch on prose, and if a rejection arrives
sometimes as `{"detail": ...}` and sometimes as a bare 500, the only thing an
algorithm can reliably do with a failure is stop -- which is the wrong response
to "you are one tick off the grid" and the right one to "your signature is
invalid", and the client cannot tell those apart.

The full catalogue, from
[`python/arena/api/errors.py`](../python/arena/api/errors.py), grouped by what a
client should *do* about it:

| Code | Status | What it means | What to do |
| --- | --- | --- | --- |
| `auth_required` | 401 | this endpoint needs a signed request | sign it |
| `auth_invalid` | 401 | could not authenticate this request | stop; retrying cannot help |
| `not_found` | 404 | no such resource | stop |
| `invalid_request` | 400 | the request could not be read | fix and resend |
| `invalid_symbol` | 400 | no such instrument | fix and resend |
| `invalid_side` | 400 | side must be buy or sell | fix and resend |
| `invalid_quantity` | 400 | quantity must be a positive whole number | fix and resend |
| `invalid_price` | 400 | price must be on the instrument's tick grid | fix and resend |
| `invalid_time_in_force` | 400 | unknown time in force | fix and resend |
| `invalid_order_type` | 400 | unknown order type | fix and resend |
| `rejected_by_venue` | 422 | the venue understood and refused | the market may allow it later |
| `rate_limited` | 429 | too many requests | back off and retry |

### One code for every authentication failure

`auth_invalid` covers an unknown key, a bad signature, a stale timestamp and a
revoked key, with one sentence for all of them. That is deliberate. Saying
*which* of those went wrong tells a caller holding no valid key which key ids
exist, and a caller holding a valid one never needs the difference. The two
causes worth checking first are a revoked key and a clock more than 30 seconds
out.

### Codes the client adds

`arena_client` raises the same exception type for failures that never reached
the venue, under codes prefixed `client_` so a log can tell the two apart:
`client_credentials_missing`, `client_path_mismatch`, `client_transport`,
`client_unreadable_response`, `client_float_amount`. These never appear on the
wire.

---

## Endpoints

Auth column: **public** needs no credential, **signed** needs the three headers.

Every endpoint can also return `invalid_request` and `rate_limited`; those are
not repeated per row. The per-endpoint lists below name the codes from the
catalogue that are reachable at that path. Where the handler for a path was
still being written when this was documented, the list is what the catalogue
makes possible rather than a set observed from a running server.

### `GET /v1/exchange`

**Public.** The venue's description of itself: the fee schedule in force, the
session state of each symbol, the price band, and recent halts.

```
GET /v1/exchange
```

The closest existing serialiser is `MarketRunner.session_state()` in
`dashboard/state.py`, which produces this shape:

```json
{
  "fees": {"taker_bps": 2.0, "maker_bps": -1.0, "auction_bps": 2.0},
  "price_band": 0.05,
  "sessions": {"SPIKE_WR_FUT": "continuous", "CROW_DISP": "halted"},
  "halts": [
    {"symbol": "CROW_DISP", "reason": "price_band", "reference": "4495.75",
     "price": "4056.25", "band": 0.05}
  ]
}
```

Session values seen in a running market: `pre_open`, `continuous`, `halted`.
Halt reasons seen: `price_band`, `limit_state`, `manual`.

Errors: none specific.

### `GET /v1/instruments`

**Public.** Everything listed.

| Parameter | Type | Meaning |
| --- | --- | --- |
| `class` | string | filter by instrument class |
| `subject` | string | filter by the underlying's subject |

Classes, derived from the contract rather than declared, so they cannot disagree
with what the instrument pays: `future`, `event`, `spread`, `index`, `call`,
`put`, `commodity`, `equity`, `volatility`.

```
GET /v1/instruments?class=future&subject=SPIKE
```

Each entry is `Instrument.to_dict()` from
[`python/arena/market/instrument.py`](../python/arena/market/instrument.py),
which is exactly this, captured from a real listing:

```json
{
  "symbol": "SPIKE_WR_FUT",
  "class": "future",
  "contract_id": "SPIKE_WR_FUT",
  "spec_digest": "sha256:5118899e743c009f2680e08970bf545d3970e1b00e960f4b128aa9ab6428c41b",
  "tick_size": "0.25",
  "lot_size": 1,
  "settlement_bounds": ["0", "1E+4"],
  "expiry": "2026-09-28T00:00:00Z"
}
```

`spec_digest` is the content address of the contract specification. Two
instruments with the same digest settle by identical rules; a digest that
changes means the contract changed.

Errors: none specific. An unmatched filter is an empty list, not a 404.

### `GET /v1/instruments/{symbol}`

**Public.** One instrument, in the shape above.

```
GET /v1/instruments/SPIKE_WR_FUT
```

Errors: `not_found`, `invalid_symbol`.

### `GET /v1/instruments/{symbol}/book`

**Public.** The aggregated ladder.

| Parameter | Type | Meaning |
| --- | --- | --- |
| `depth` | integer | how many price levels per side |

```
GET /v1/instruments/SPIKE_WR_FUT/book?depth=5
```

Captured from a live book. Each level is `[price, quantity]`, price as a
decimal string, quantity as the total lots resting at that price:

```json
{
  "symbol": "SPIKE_WR_FUT",
  "bids": [["4689.00", 22], ["4688.75", 3], ["4684.50", 14], ["4671.75", 2]],
  "asks": [["4693.25", 6], ["4696.75", 30], ["4730.25", 4], ["4757.00", 6]],
  "session": "continuous"
}
```

Both sides are ordered best first: bids descending, asks ascending. A book
during an auction also carries `indicative`, the price the auction would clear
at, as a decimal string.

The quantity at a level is the *visible* quantity. An iceberg's reserve is not
published, which is what makes it an iceberg.

Errors: `not_found`, `invalid_symbol`.

### `GET /v1/instruments/{symbol}/trades`

**Public.** The tape: one entry per execution, regardless of how many orders saw
it.

| Parameter | Type | Meaning |
| --- | --- | --- |
| `limit` | integer | how many of the most recent prints |

```
GET /v1/instruments/SPIKE_WR_FUT/trades?limit=3
```

The engine's own trade record is `Traded.to_dict()` in
`python/arena/exchange/events.py`, with the price converted from ticks to a
price at the API boundary the way every published price is:

```json
{
  "type": "trade",
  "sequence": 14911,
  "quantity": 14,
  "price": "4694.00",
  "aggressor_side": "sell",
  "buy_order_id": 5083,
  "sell_order_id": 5086
}
```

`aggressor_side` is the trade's sign for order-flow purposes: a buy-side
aggressor is an uptick in demand. It cannot be recovered from prices after the
fact, which is why it is published, and nearly every microstructure measurement
worth making needs it.

Errors: `not_found`, `invalid_symbol`.

### `GET /v1/instruments/{symbol}/history`

**Public.** The recent price path, for charting or for a signal.

```
GET /v1/instruments/SPIKE_WR_FUT/history
```

The dashboard's equivalent publishes parallel arrays -- a timestamp array `t`
and a mid array `mid`, with `null` where the book had no two-sided market -- so
that a chart can be drawn without unpacking a list of objects.

Errors: `not_found`, `invalid_symbol`.

### `GET /v1/account`

**Signed.** Cash, collateral, PnL and equity for the seat the key belongs to.

```
GET /v1/account
arena-key-id: ak_0011223344556677
arena-timestamp: 1787000000
arena-signature: d43201651053cbb3926d84cefc6ecba11c27e03ffa6bf49fab310385691fd89f
```

`Account.to_dict()` from
[`python/arena/portfolio/account.py`](../python/arena/portfolio/account.py),
captured from a real seat:

```json
{
  "agent_id": "mm-1",
  "cash": "128672864.139831",
  "starting_cash": "135306150",
  "posted_collateral": "12178767.015264",
  "free_cash": "116494097.124567",
  "realized_pnl": "-6633285.860169",
  "unrealized_pnl": "-4646834.405264",
  "equity": "124026029.734567",
  "positions": []
}
```

`posted_collateral` is not a margin estimate. Every instrument here settles as a
known function of one bounded scalar, so a portfolio's worst case is evaluated
exactly rather than modelled, and `free_cash` is what is genuinely left to
trade with.

Errors: `auth_required`, `auth_invalid`.

### `GET /v1/account/positions`

**Signed.** What is held, marked to the market.

`Position.to_dict()` from `python/arena/portfolio/position.py`, captured from a
real position:

```json
{
  "symbol": "CROW_DISP",
  "quantity": 356,
  "cost_basis": "1238641.085568",
  "average_price": "3479.328892044943820224719101",
  "realized_pnl": "-290375.198532",
  "fees_paid": "183.2841",
  "volume": 3170,
  "mark": "4041.5",
  "unrealized_pnl": "200132.914432",
  "equity": "-90242.2841"
}
```

`quantity` is signed: negative is short. `average_price` is the twenty-eight
digit figure from the [Conventions](#conventions) section, and it is that long
because it is a ratio of two exact integers rather than a rounded average.
`mark`, `unrealized_pnl` and `equity` appear only when the venue has a mark for
the symbol.

Errors: `auth_required`, `auth_invalid`.

### `GET /v1/account/fills`

**Signed.** This account's executions.

| Parameter | Type | Meaning |
| --- | --- | --- |
| `limit` | integer | how many of the most recent fills |

A fill is one side's private view of an execution, emitted once per order per
trade. The engine's record is `Filled.to_dict()` in
`python/arena/exchange/events.py`, carrying `order_id`, `side`, `quantity`,
`price`, `remaining`, and `aggressor` -- whether this side crossed the spread or
was resting. The public tape entry for the same execution carries neither
account.

Errors: `auth_required`, `auth_invalid`.

### `GET /v1/orders`

**Signed.** Working orders across every symbol.

Errors: `auth_required`, `auth_invalid`.

### `POST /v1/orders`

**Signed.** Send an order.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `symbol` | string | yes | what to trade |
| `side` | string | yes | `buy` or `sell` |
| `quantity` | integer | yes | lots, positive |
| `price` | string | no | limit price; omit for a market order |
| `type` | string | no | `limit`, `market`, `stop`, `stop_limit`, `pegged` |
| `time_in_force` | string | no | `gtc`, `ioc`, `fok`, `post_only` |
| `stop` | string | no | trigger price for a stop |
| `display` | integer | no | visible size of an iceberg; 0 means no reserve |
| `client_order_id` | string | no | your own id, echoed back |

```
POST /v1/orders
content-type: application/json
arena-key-id: ak_0011223344556677
arena-timestamp: 1787000000
arena-signature: 05d1de8528b12f2781fb9dc6f81359df95579f59245429c63a40cc047ed13ef2

{"price":"4663.25","quantity":5,"side":"buy","symbol":"SPIKE_WR_FUT"}
```

Response: the accepted order, with the id the venue assigned. The engine's
acknowledgement is `Acknowledged.to_dict()` -- `order_id`, `side`, `quantity`,
`price` -- and the REST layer's exact body is one of the things
[not verified here](#what-is-not-verified-here).

Notes worth having before the first order:

- **Omitting `price` is how a market order is expressed.** The engine models a
  market order as a limit order with no price bound rather than as a separate
  matching path, and it is always immediate-or-cancel, because resting an
  unpriced order would leave a level in the book that matches anything.
- **`post_only` is rejected rather than crossed.** It exists because of
  maker-taker pricing: a maker that accidentally crosses pays the taker fee
  instead of earning the rebate, which can turn a profitable quote into a losing
  one.
- **The price must be on the instrument's grid.** Off-grid prices are refused,
  not rounded, because rounding would rest the order at a price nobody chose and
  then fill it there. Some contracts use a tick *table*, where the increment
  widens above a threshold, so read `tick_size` and do not assume it is constant
  across the whole range.
- **A price outside the settlement range is refused.** A limit buy below a
  contract's floor cannot settle where it rests, and the collateral model sizes
  the worst case from that range, so such an order looks *safer* than a sensible
  one and passes every check the venue would otherwise apply.
- **Use `client_order_id`.** It is the only thing that lets a retry after a
  timeout be distinguished from a second order, and a timeout is the one failure
  a live trading loop is guaranteed to meet.

Errors: `auth_required`, `auth_invalid`, `invalid_symbol`, `invalid_side`,
`invalid_quantity`, `invalid_price`, `invalid_order_type`,
`invalid_time_in_force`, `rejected_by_venue`.

### `GET /v1/orders/{symbol}/{order_id}`

**Signed.** One order. Addressed by symbol and id together because order ids are
assigned per book, so the id alone does not identify an order.

Errors: `auth_required`, `auth_invalid`, `not_found`, `invalid_symbol`.

### `DELETE /v1/orders/{symbol}/{order_id}`

**Signed.** Pull one order out of the book.

```
DELETE /v1/orders/SPIKE_WR_FUT/5083
```

Cancelling an order that has already filled or already been cancelled is a
`not_found`, not a success. A cancel that races a fill is the normal case, not
an exceptional one.

Errors: `auth_required`, `auth_invalid`, `not_found`, `invalid_symbol`.

### `DELETE /v1/orders`

**Signed.** Pull every working order this account has, on every symbol. The
blunt instrument, for a strategy that has decided to stand down.

Errors: `auth_required`, `auth_invalid`.

### `POST /v1/keys`

**Signed.** Mint another key for this seat, for rotation: create the
replacement, switch to it, then revoke the old one, with no window in which the
account has no working key.

The response carries the secret, and it is the only time it is ever shown.
`ApiKey.public()` in
[`python/arena/api/keys.py`](../python/arena/api/keys.py) is what a key looks
like everywhere else:

```json
{"key_id": "ak_0011223344556677", "agent_id": "mm-1", "label": "",
 "created_at": 1787000000.0, "revoked": false}
```

Errors: `auth_required`, `auth_invalid`.

### `GET /v1/keys`

**Signed.** The keys on this seat, in the shape above. Never the secrets.

Errors: `auth_required`, `auth_invalid`.

### `DELETE /v1/keys/{key_id}`

**Signed.** Revoke a key. The venue keeps the id rather than deleting it, so a
revoked id can never be reissued to somebody else and inherit the first owner's
history. Revoking the key you are signing with works, and is the last request
that key can make.

Errors: `auth_required`, `auth_invalid`, `not_found`.

---

## The stream

```
ws://localhost:8000/v1/stream
wss://... where the venue is served over TLS
```

Polling a book twenty times a second to notice a change that happened once is
the wrong shape for market data, and a client that misses an update has a
different book from the venue and does not know it. The socket exists for both
reasons.

### Frames

Client to server, one JSON object per message, with an `op`:

| `op` | Purpose |
| --- | --- |
| `subscribe` | start receiving one or more channels |
| `unsubscribe` | stop receiving them |
| `auth` | prove which account this connection is, for the private channels |
| `ping` | keep the connection alive and confirm it is live |

```json
{"op": "subscribe", "channels": ["ticker.*", "book.SPIKE_WR_FUT"]}
```

Server to client, every frame carries three fields:

| Field | Meaning |
| --- | --- |
| `type` | what kind of frame this is |
| `channel` | which subscription produced it |
| `seq` | a gapless per-connection sequence number |

**`seq` is per connection, not per channel, and it is gapless.** That is the
whole point of publishing it: a client can tell "nothing happened" apart from "I
missed something". A gap means the connection dropped frames, and the correct
response is to resynchronise from the REST snapshot rather than to keep applying
deltas to a book that is already wrong. Reconnecting starts the sequence again,
because it is a property of the connection.

### Channels

| Channel | Auth | Carries |
| --- | --- | --- |
| `ticker.<symbol>` | public | top-of-book and last-trade updates for one symbol |
| `ticker.*` | public | the same for every listed symbol |
| `book.<symbol>` | public | ladder updates for one symbol |
| `trades.<symbol>` | public | the public tape for one symbol |
| `orders` | signed | this account's order lifecycle: acks, rejects, cancels |
| `fills` | signed | this account's executions |

`orders` and `fills` are the private counterparts of `trades`: the tape says a
trade happened, and only the two participants learn that it was theirs.

### Authenticating a connection

Send an `auth` frame before subscribing to `orders` or `fills`. It is signed
over the same canonical string as a GET of the stream path with an empty body,
so one implementation of the signature serves both transports:

```
1787000000\nGET\n/v1/stream\n
```

```python
client = ArenaClient("http://localhost:8000", key_id="ak_...", secret="...")
socket.send(json.dumps(client.stream_auth()))
socket.send(json.dumps(client.subscribe("fills", "ticker.*")))
```

The exact field names of the `auth` frame are the one part of this document that
could not be checked against a running handler. See below.

### Why the Python client does not open the socket

`arena_client` builds the URL and the auth frame and stops there. Adding a
websocket dependency to a client whose only other dependency is an HTTP library
buys a reconnect loop the caller will want to own anyway -- backoff, resubscribe,
resynchronise on a sequence gap -- and every websocket library already takes a
URL and sends JSON. What is worth providing is the part that is easy to get
wrong, which is the signature.

---

## What is not verified here

The REST and websocket handlers were being written in parallel with this
document. Everything above marked as captured from a real market run, and every
signature vector, is verified: the payload shapes come from serialisers that
exist and run today, and the vectors are asserted in
`tests/test_api_client.py` against the venue's own signing code.

These are not verified, and are described from the endpoint contract rather than
from an observed response:

1. **The response body of `POST /v1/orders`.** The engine's acknowledgement
   carries `order_id`, `side`, `quantity` and `price` in ticks; what the REST
   layer wraps that in, and how it reports a partial fill on arrival, is the
   handler's to fix.
2. **The order record** returned by `GET /v1/orders` and
   `GET /v1/orders/{symbol}/{order_id}`: which status vocabulary it uses, and
   whether it reports remaining quantity, queue position or neither.
3. **The fill record** returned by `GET /v1/account/fills`, including whether it
   carries the fee charged on each fill. The engine's `Filled` event does not;
   the fee is applied by the venue's fee schedule.
4. **The exact key set of `GET /v1/exchange`.** The shape shown is
   `MarketRunner.session_state()`, which is the closest existing serialiser, not
   the endpoint itself.
5. **The `history` response.** Shown as the dashboard's parallel arrays.
6. **The `subject` filter.** `subject` is a field of the contract's underlying
   metric reference, not of the instrument payload, so filtering by it works on
   data that the instrument response does not currently echo back.
7. **How a seat gets its first key.** `POST /v1/keys` is signed, so it cannot be
   the answer.
8. **Every websocket frame shape**, including the field names of the `auth`
   frame and the `type` vocabulary. The signature inside the auth frame is
   computed the same way as for every other request, so a disagreement here
   would be about field names rather than about cryptography.
9. **One unit hazard to watch for.** In the dashboard's session payload,
   `fees_collected` is published in raw minor units while every neighbouring
   money field is converted to a price -- the front end divides by 1,000,000 in
   JavaScript. If `GET /v1/exchange` carries that field forward unchanged, it is
   a million times larger than it looks. This document does not include it in
   the example response for that reason.

Where a client here had to choose, it chose to hand back what the venue sent
rather than to guess: any field this client does not recognise arrives as the
exact string the venue published, which is lossless, and never as a float.
