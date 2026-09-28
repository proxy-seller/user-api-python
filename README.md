# proxy-seller Python API

Client library for Proxy-Seller Client API v2.

```sh
pip install proxy-seller-user-api
```

Base URL — `https://proxy-seller.com/personal/api/v2/`, the API key goes **into the path**,
not into a header.

## Quick start

```python
from proxy_seller_user_api import Api, ApiError

try:
    with Api({'key': 'YOUR_API_KEY', 'timeout': 30}) as api:
        print(api.balance())
except ApiError as error:
    print(error.code, error.custom_data, error.http_status, error.errors)
```

Nothing else is required — the client talks to `https://proxy-seller.com/personal/api/v2/` by
default. Call `api.close()` when a context manager is not used. Configuration and payment/
authorization state belong to each `Api` instance; they are not shared by clients. The same
goes for the request queue: requests are paced by default so that the client stays within the
API limits — see [Rate limits and the request queue](#rate-limits-and-the-request-queue).

### Paying for orders

Orders and renewals are paid from the account balance or charged to the saved card, so they
accept exactly two payment codes: `balance` and `paddle_subscription`. Set one once:

```python
api.setPaymentCode('balance')   # or 'paddle_subscription' to charge the saved card
```

Any other payment system — a one-off card or crypto checkout — needs a browser redirect that a
programmatic client cannot complete, so `order/*` and `prolong/*` reject it. `order/make`
requires a payment; `order/calc` and `prolong/*` also work without one.

`balancePaymentsList()` is not the place to pick an order payment from: it lists the systems for
topping the balance up with `balanceAdd()` and never includes the balance itself. That top-up is
the one place where an id is unavoidable — several payment systems share the same code (a single
`cryptomus` covers "USDT (TRC-20)", "All cryptocurrencies" and more), so the code cannot tell
them apart; see [Balance and auto top-up](#balance-and-auto-top-up). Everywhere else you use
human-readable codes.

### Optional fingerprint

`order/make` accepts an optional `X-Fingerprint` header. When present it is used for anti-fraud
checks and affiliate attribution; **no section refuses an order without it**, residential and
scraper included. The SDK sends the header only when you give it a value:

```python
api = Api({'key': 'YOUR_API_KEY', 'fingerprint': 'my-installation-id'})
# or later:
api.setFingerprint('my-installation-id')
# or for a single call:
api.orderMakeResident('1-gb', fingerprint='my-installation-id')
```

Any opaque string is accepted — the server does not validate its shape — but if you send one,
make it a **stable identifier of your installation**. The SDK deliberately does not generate
one: a value randomized per process is useless for anti-fraud and attribution. Without a value
the header is simply omitted.

<details>
<summary>Pointing the client at another host, and extra headers</summary>

```python
with Api({
    'key': 'YOUR_API_KEY',
    'base_url': 'http://localhost:8080/personal/api/v2/',
    'headers': {'X-Request-Source': 'my-app'},
}) as api:
    ...
```

</details>

## Errors

Every JSON endpoint answers with the envelope `{status, data, errors}`. **The HTTP status is
almost always 200** — the failure lives inside `errors[]`, and the library raises `ApiError`
built from `errors[0]` while keeping the whole array in `error.errors`.

Access failures are the important special case. A revoked/invalid key, an IP outside the
key's allowlist and an exceeded rate limit all return **HTTP 200 with the same fixed triple**:

```python
[{'message': 'Error api key',           'code': 503},
 {'message': 'IP not allowed 1.2.3.4',  'code': 503},
 {'message': 'Request limit reached',   'code': 503}]
```

So `errors[0].message` is always `Error api key`, and it does not tell you which of the three
actually happened — always read the whole array. The API's own rate limit (1000 requests per
minute per key, calendar-minute window) arrives as this same triple, **not as HTTP 429**. An
HTTP 429 can only come from the edge in front of the API, before the request reached it; the
client retries those by itself — see
[Rate limits and the request queue](#rate-limits-and-the-request-queue).

```python
try:
    api.proxyList('ipv4')
except ApiError as error:
    if error.isAccessError():
        # key / IP allowlist / rate limit — the triple does not distinguish them
        print([item['message'] for item in error.errors])
    else:
        print(error.code, error.custom_data)
```

Two responses fall outside the envelope entirely:

* file endpoints (`proxy/download/*`, `resident/geo`, `resident/geo/isp`) return an
  attachment body;
* an invalid `ext` on a download is rejected with a bare plain-text HTTP 400. The library
  validates `ext` locally (`assertExt`) to avoid it: max 250 chars, no `CR`, `LF`, `/`, `\`.

## Rate limits and the request queue

The API accepts up to 1000 requests per minute per key. By default the client paces its own
requests so that you stay inside that limit without any throttling code of your own, and it
sends the requests that change something — above all the ones that spend money — one at a
time.

Every request falls into one of three groups by its endpoint path, not by HTTP method:
`order/calc` and the other `*/calc` endpoints are `POST`, but they only read.

| group | endpoints |
|---|---|
| money | `order/make`, `prolong/make/{type}`, `balance/add` |
| write | `autoprolong/enable/{type}`, `autoprolong/disable/{type}`, `auth/add`, `auth/add/ip`, `auth/change`, `auth/delete`, `proxy/replace`, `proxy/comment/set`, `balance/autotopup/set`, `resident/list` (the alias of `resident/list/add`), `resident/list/{add,delete,rename,rotation,tools}`, `residentsubuser/{create,update,delete}`, `residentsubuser/list/{add,delete,rename,rotation,tools}` |
| read | everything else: `*/list`, `*/get`, every `*/calc`, `reference/*`, `proxy/download/*`, `resident/package`, `resident/lists`, `resident/geo*`, `resident/consumption`, `resident/traffic/details`, `residentsubuser/packages`, `residentsubuser/lists`, `balance/payments/list`, `balance/autotopup/get` |

What the client does, with the defaults:

1. **A global window.** All requests together — read, write and money — start at most 1000
   times within any 60 seconds. It is a sliding window over the start times of the last 1000
   requests, not a token bucket: when it is full, the next request waits until the oldest of
   them is 60 seconds old, so no burst ever goes beyond 1000 in 60 seconds.
2. **One write at a time.** Write and money requests go through a single queue per client,
   first come first served. Only one of them is in flight; the next one starts after the
   previous one has finished, and no sooner than **1 second** after the previous write or
   money request *started*. A money request also waits until **2 seconds** have passed since
   the previous money request started. Reads never wait for this queue, only for the global
   window.
3. **HTTP 429 is retried.** A 429 comes from the edge in front of the API and means that the
   request never reached the API, so repeating it is safe even for money requests. The client
   waits for `Retry-After` (seconds or an HTTP date; 2 seconds when the header is missing or
   unreadable; never longer than 60 seconds) and tries again, at most **3** times. After that
   it raises the usual `ApiError` with `error.http_status == 429`. A retried write or money
   request keeps its place in the queue, and every retry counts as a new start in the global
   window.
4. **Nothing else is retried.** Business errors reach you exactly as before. That includes
   code `57`, `Prolong for this order is already in progress…`: another request is extending
   that order right now, and a retry could extend it twice. It also includes the access-denied
   triple (`Error api key` / `IP not allowed …` / `Request limit reached`, see
   [Errors](#errors)), which cannot be told apart from a wrong key or IP. Network errors are
   not retried either.

Waiting blocks the calling thread, and the time spent in the queue is not part of `timeout`.
The queue is thread-safe: threads that share one `Api` instance share its window and its write
queue — writes are serialized, reads run in parallel.

Change the numbers or switch the queue off with `rate_limit`:

```python
api = Api({
    'key': 'YOUR_API_KEY',
    'rate_limit': {
        'requests_per_minute': 600,   # default 1000
        'write_interval_ms': 1500,    # default 1000
        'money_interval_ms': 3000,    # default 2000
        'max_retries': 5,             # default 3; 0 turns the 429 retries off
    },
})

api = Api({'key': 'YOUR_API_KEY', 'rate_limit': {'enabled': False}})  # or 'rate_limit': False
```

`'enabled': False` restores the previous behaviour exactly: no waiting and no retries, so a 429
raises at once. The camelCase spellings (`requestsPerMinute`, `writeIntervalMs`,
`moneyIntervalMs`, `maxRetries`, and `rateLimit` for the option itself) are accepted too; an
unknown option raises `ValueError`.

**The queue belongs to one `Api` instance.** Separate instances — in one process or in several
processes, such as the workers of a multi-process server or several cron scripts — know
nothing about each other's requests, even with the same key, so together they can still go
over the limits. Share one instance per key where you can, or give each instance a share of
`requests_per_minute`. Where several processes use a key at the same time, the server can
still answer with code `57` or with the access-denied triple — handle them as described above.

## Identifiers

**IDs in v2 are ObjectId strings**, not numbers: `orderId`, `periodId`, `countryId`, `mixId`,
`paymentId`, `operatorId`, `tarifId`, auth ids, IP-address ids. Old numeric v1
IDs do not resolve, and nothing should be parsed into `int`.

Two things in this list are not ObjectIds:

* **resident list ids are numeric** (`Long`), both in the main package
  (`residentList`, `residentListRename`, `residentListRotation`, `residentListDelete`,
  `proxyDownloadResident(id=...)`) and inside subpackages (`residentSubUserListRename`,
  `residentSubUserListRotation`, `residentSubUserListDelete`);
* **`rotationId` is not an identifier at all** — it is the rotation interval in **minutes**,
  an integer, where `0` means `By Link`. It has neither an ObjectId nor a textual code, so
  `'5m'` / `'10m'` are always rejected (`Set existed [rotationCode] from reference`). Pass
  `5`, `10`, `0`. `referenceList()` reports the allowed values as
  `rotations: [{'id': 5, 'name': '5 minutes'}, ...]`, and `id` there is already the number of
  minutes.

## Codes instead of ids

Every reference argument of `order/*` and `prolong/*` takes **either an ObjectId or a code in
the same `*Id` argument**: when the value is not a valid id and the matching `*Code` field is
empty, the server resolves it as a code. The `*Code` keyword options are kept for
compatibility, but nothing needs them — a code goes straight into `countryId`, `periodId`,
`operatorId`, `mixId`, `tarifId`, `paymentId`.

`rotationCode` is the exception in the other direction: it has no resolution step at all, it
is only copied into `rotationId` after an integer check. Use `rotationId` and forget it.

### What can be passed and where to get it

Every field in `referenceList()` is called `id`, and its value is a readable code — not an
ObjectId. Read `id`, put it in the matching `*Id` argument. That is the whole rule:

| Argument | Pass this | Read it from |
| --- | --- | --- |
| `countryId` | alpha-3 country code, e.g. `USA` (upper-cased server-side, so `usa` works) | `country[].id` |
| `periodId` | period code, e.g. `1m` (lower-cased server-side) | `period[].id` |
| `operatorId` | mobile operator code — exact match, case-sensitive | `reference/list/mobile` → `country[].operators.dedicated[]` / `.shared[]` → `id` |
| `rotationId` | **minutes** as an integer, `0` = `By Link` — the one `id` that is a number, not a code | `country[].operators.*[].rotations[].id` *is* the minute value |
| `mixId` | mix package code — exact match | `reference/list/mix` → `quantities[].id`, e.g. `europe-2-mix_IPv4`. First argument of `orderCalcMix()`/`orderMakeMix()` |
| `tarifId` | resident tariff code — exact match, e.g. `1-gb` | `reference/list/resident` → `tarifs[].id` |
| `paymentId` / `paymentCode` | `balance` or `paddle_subscription` (the saved card) — nothing else pays for orders and renewals | fixed codes, see "Paying for orders" above; `balancePaymentsList()` ids are for `balanceAdd()` only |

ObjectIds are still accepted everywhere if you happen to have them; the reference simply no longer
publishes them. `balance/add` is the one endpoint that resolves no codes at all — it needs a real
`paymentId` (see `balanceAdd`).

## Orders

Reference values are passed positionally into the `*Id` arguments, as ObjectIds or as codes;
`authorization` and `coupon` are the only arguments normally left as `None`.

```python
api.setPaymentCode('balance')  # or 'paddle_subscription' for the saved card

# ipv4: customTargetName is mandatory, otherwise the call fails locally
api.orderCalcIpv4('USA', '1m', 2, customTargetName='seo', uptime=True)

# mobile: rotationId is minutes (0 = By Link), operatorId is an id or an operator tag
api.orderCalcMobile('USA', '1m', 1, operatorId='ee_unitedkingdom', rotationId=5)
api.orderMakeMobile('USA', '1m', 1, operatorId='ee_unitedkingdom', rotationId=10,
                    mobileServiceType='dedicated')

# MIX: the first argument is a package, not a country — take it from
# referenceList()['mix']['quantities'][0]['id']
api.orderCalcMix('europe-2-mix_IPv4', '1m', 1)

# resident: a tariff code from referenceList('resident')['items']['tarifs'][0]['id']
#           (the typed call wraps its single entry in 'items' — the untyped one does not)
api.orderCalcResident('1-gb')
```

The whole payload may still be passed as one dictionary (`api.orderCalcMobile({...})`) or as
keyword options — that form is useful for fields without a positional argument, such as
`protocol` or `uptime`. `orderCalcMixByCode()` / `orderMakeMixByCode()` remain thin aliases:
their `mixCode`/`periodCode` are exactly the values that `orderCalcMix()` already accepts
positionally.

`sectionCode` values: `ipv4`, `ipv6`, `mobile`, `isp`, `mix`, `mix_isp`, `resident`,
`scraper`. `mobileServiceType` is required by the API and defaults to `dedicated`. `uptime` is
available for supported IPv4/ISP combinations. `setGenerateAuth('Y')` affects only
`order/make`.

`customTargetName` is required for `ipv4`, `ipv6` and `isp` (the server answers
`Incorrect goal`, code 14, without it) and is checked locally before the request. For
`mix`/`mix_isp` it is only needed when the server cannot tell which package you mean, so naming
the package — `orderCalcMix('europe-2-mix_IPv4', '1m', 10)` — removes the need for it.

### Listing orders

```python
api.orderList(status='PAYED', sort_by='date_insert', order='desc', page=1, limit=20)
api.orderList()  # the same call with no filters at all
```

Every filter is optional. Query filters and response fields of `order/list` use snake_case
names such as `start_date` and `is_extend`, and the SDK sends them exactly as given:
`order_id`, `start_date`, `end_date`, `status` (`PAYED` | `NOT_PAYED` | `RETURN` — the
`status_type` of the response), `is_extend`, `auto_order`, `page`, `limit`, `sort_by`
(`date_insert` | `summ` | `status`) and `order` (`asc` | `desc`).

The result is not a flat list but a `metadata` + `items` pair, and `metadata` is always there:
without `limit` it reports `total_pages: 1`, `current_limit: 0` and the whole list in `items`.
`summ` and `items[]['price']` are **strings with the currency already in them** (`'$25.00'`),
`auto_order` and `is_extend` are `'Y'`/`'N'` rather than booleans, and the dates are ISO 8601
strings with offset (`2026-09-01T14:15:26+00:00`). `id` is a numeric order ID sent as a string;
the ObjectId is `order_id` — the same value `proxyList()` returns as `order_id`, and the one
`ipv6`, `mix` and `mix_isp` are renewed by (see [Renewing proxies](#renewing-proxies)).

## Renewing proxies

What you renew by depends on the proxy type, and every value comes straight out of
`proxyList()`:

| type | pass this | from a `proxyList()` item | sent as |
|---|---|---|---|
| `ipv4`, `isp` | the address `1.2.3.4`, or the proxy id | `item['ip']`, or `item['id']` | `ips` / `ipIds` |
| `mobile` | the address `ip:port_http:port_socks`, or the proxy id | `item['ip']`, `item['port_http']`, `item['port_socks']`, or `item['id']` | `ips` / `ipIds` |
| `ipv6`, `mix`, `mix_isp` | the order id | `item['order_id']` (also `order_id` in `orderList()`) | `orderIds` |

```python
ipv4 = api.proxyList('ipv4')['items']
ips = [item['ip'] for item in ipv4]          # ['1.2.3.4', '5.6.7.8']

api.prolongCalc('ipv4', ips, '1m')           # price first
api.prolongMake('ipv4', ips, '1m')           # deducts money
```

`prolongCalc()` shows the price; `prolongMake()` charges the balance. If the balance is short,
`prolongMake()` raises `ApiError` with the server's warning — it never reports a renewal that did
not happen.

A `mobile` address has to be assembled out of the three fields above:

```python
mobile = api.proxyList('mobile')['items']
addresses = ['{}:{}:{}'.format(item['ip'], item['port_http'], item['port_socks'])
             for item in mobile]
api.prolongCalc('mobile', addresses, '1m')
```

**`ipv6`, `mix` and `mix_isp` are renewed as whole orders by `orderIds`.** Pass the `order_id`
of each order: every active proxy of that type in it is renewed (for `mix`/`mix_isp` — the mix
packages of the order), and `quantity` / `items` of `prolongCalc()` show everything the quote
covers.

```python
ipv6 = api.proxyList('ipv6')['items']
orders = sorted({item['order_id'] for item in ipv6})

api.prolongCalc('ipv6', orders, '1m')
result = api.prolongMake('ipv6', orders, '1m')
result['orderIds']                           # every renewed order
```

If any of the orders is not yours or has no active proxy of that type, the whole request fails
with code 29 `Incorrect orderIds` and nothing is renewed. Addresses are not accepted for these
types — `ipv6` is no longer renewed by its `host:port` — and the server rejects a selection
field of the wrong kind with an error naming it, e.g.
`[ips] is not applicable for ipv6: prolong by [orderIds]`.

How the second argument is routed: a value with a dot or a colon is an address and goes to
`ips`; anything else is an id — `ipIds` for `ipv4`/`isp`/`mobile`, `orderIds` for
`ipv6`/`mix`/`mix_isp`. A list and a comma-separated string both work; empty values are
skipped, and an empty selection is not sent at all. Do not mix proxy ids and addresses in one
call: when `ipIds` is present the server ignores `ips`, so the SDK raises `ValueError` rather
than letting the addresses drop out silently. The argument is still named `ids`, so existing
positional and keyword calls keep working; it only means "what to renew" and never reaches the
wire under that name.

The period takes a code (`'1m'`), same fallback as `order/*`, and the fourth argument is a coupon.

`prolongMake()` returns `{orderId, orderIds, total, listBaseOrderNumbers, balance}`. One request
can renew several orders: `orderIds` lists every renewed one, `orderId` is the first of them and
stays for compatibility, and `listBaseOrderNumbers` holds one base order number per renewed order
(per package for `mix`/`mix_isp`), matching `base_order_number` in `orderList()`.

<details>
<summary>The selection fields as keywords</summary>

The request fields can also be passed as they go on the wire:

```python
api.prolongMake(
    'mix', orderIds=['ORDER_ID'],
    periodId='1m', paymentId='balance', coupon='SALE10')
```

Accepted: `ipIds`, `ips`, `orderIds`, `periodId`/`periodCode`, `paymentId`/`paymentCode` and
`coupon`; an explicit value wins over the one routed from the second argument. Types: `ipv4`,
`ipv6`, `mobile`, `isp`, `mix`, `mix_isp`.

`ids`, `orderSeparatorIds` and `orderSeparatorId` were removed from the API. Passed as keywords,
in `options` or in the payload dict, they raise `ValueError` naming the replacement instead of
being dropped: `ids` → `ipIds` (`ipv4`/`isp`/`mobile`) or `orderIds` (`ipv6`/`mix`/`mix_isp`),
`orderSeparatorIds`/`orderSeparatorId` → `orderIds`. This is only about those keys — the second
positional argument is still named `ids` and works as described above.

</details>

## Automatic renewal

`prolongMake()` charges you now. `autoprolong/*` only arms a charge that happens later, without
you present — a separate branch of the API, not a flag on prolong.

```python
api.autoProlongCalc('ipv4', ['1.2.3.4'], '1m', paymentId='balance')
api.autoProlongEnable('ipv4', ['1.2.3.4'], '1m', paymentId='balance')
api.autoProlongDisable('ipv4', ['1.2.3.4'])

api.autoProlongEnable('mix', ['ORDER_ID'], '1m', paymentId='balance')   # whole orders
```

The selection works exactly as in [Renewing proxies](#renewing-proxies): addresses or proxy ids
for `ipv4`, `isp` and `mobile`, the `order_id` for `ipv6`, `mix` and `mix_isp`.

`paymentId` is **mandatory** for `calc` and `enable` — the charge happens while you are away, so
the payment system cannot be guessed. Only `balance` and `paddle_subscription` are accepted: a
one-off Paddle checkout needs a browser redirect a headless client cannot complete. With
`paddle_subscription` also pass `subscriptionId`.

Residential packages renew as a package, not as addresses — send no selection:

```python
api.autoProlongCalc('resident', paymentId='balance')
api.autoProlongEnable('resident', paymentId='balance', tarifId='trial')
api.autoProlongDisable('resident')
```

A selection passed with `resident` raises `ValueError` locally (the server rejects it with
`[ipIds] is not applicable for resident: auto-prolong applies to the whole package`): dropping it
silently would switch the whole package while you meant single addresses.

Three things about the answers before you parse them:

* **`ipIds` is not an echo.** `enable` and `disable` report what was actually switched:
  `quantity`, `ipIds` (the proxies) and `orderIds` (their orders). For `ipv6`, `mix` and
  `mix_isp` that is every active proxy of the orders you sent; for `resident` `quantity` is 1
  and both lists are empty. The proxy list used to be called `ids`.
* **Not enough money is not an exception.** `calc` answers `status: "error"` with a *filled*
  `data` and an empty `errors[]` — the same shape `prolong/calc` uses. Read `data['warning']`.
* **Residential fills different fields.** `chargeDate` is `None` there (a package renews on
  expiry *or* on traffic exhaustion, so no single date describes it); `dateEnd`, `tarifId` and
  `days` — the tariff's own period — carry the meaning instead.

`scraper` has no auto-renewal: it is extended by buying traffic through `order/make`.

> Replaces `resident/autorenew/{enable,disable,calculate}`, **removed** from the server.

## Balance and auto top-up

```python
api.balancePaymentsList()          # payment systems; BALANCE itself is excluded
api.balanceAdd(10, 'PAYMENT_ID')   # -> payment page URL
```

`balance/add` accepts **only `paymentId`** — unlike order/prolong endpoints it does not
resolve `paymentCode`. Passing a code alone raises a local `ValueError` instead of sending
`paymentId: null`.

```python
state = api.balanceAutoTopupGet()
# {'configured': True, 'enabled': True, 'state': 'ACTIVE', 'threshold': 10, 'amount': 25,
#  'subscriptionId': 'sub_1', 'paymentMethod': {...},
#  'failCount': 0, 'lastAttemptAt': None, 'lastEvent': None}

api.balanceAutoTopupSet(threshold=20)                    # partial update: only threshold
api.balanceAutoTopupSet(enabled=False)                   # False is sent, not dropped
api.balanceAutoTopupSet({'amount': 50})
```

`state` is one of `NO_PAYMENT_METHOD`, `DISABLED`, `ACTIVE`, `PAYMENT_INVALID`,
`PAUSED_FAILURES`; `lastEvent.status` is one of `TRIGGERED`, `SUCCEEDED`, `FAILED`,
`SKIPPED_CAP`, `SETTINGS_SAVED`, `PAUSED`. `paymentMethod` (or `None`) carries
`{id, status, paymentMethod, brand, last4, exp}`, and its `id` is the Paddle subscription id
you can send back as `subscriptionId`.

`balance/autotopup/set` is a **partial update**: any field you do not pass keeps its stored
value, so the library sends only the fields you actually gave it. Both calls return the state
after saving — no second request needed. Validation is entirely server-side and applied to
the merged result; error codes are `49` (feature unavailable), `50` (threshold too low),
`51` (amount too low), `52` (amount below threshold), `53` (no saved payment method),
`56` (saved card expired). Boundary values arrive in `error.custom_data`: `minAmount`
and `minThreshold`.

> **`dailyCountCap` and `monthlyAmountCap` are gone.** Removed from the contract on 2026-08-18:
> the server silently ignores them and they are absent from the response, so passing them made a
> call that reported success and changed nothing. The SDK now raises `ValueError` on them. Codes
> `54` and `55` were removed with them and are not reused, and `custom_data` no longer carries
> `minDailyCountCap`.

## Proxies

```python
api.proxyList('ipv4', latest='Y')      # or proxyList() for every type at once
api.proxyDownload('ipv4', ext='csv', proto='socks5')
api.proxyReplace(['IP_ID'], 'NOT_WORK')
api.proxyCommentSet(['IP_ID'], 'main pool')
```

`proxyList()` without a type returns a dict keyed by `ipv4`, `ipv6`, `mobile`, `isp`, `mix`,
`mix_isp`, `resident`. The `orderId` filter is an ObjectId string.

`proxyReplace(ids, type, comment)` — **`type` is the replacement reason, not a proxy type**:
`NOT_WORK`, `INCORRECT_LOCATION`, `CANT_CHANGE_NETWORK`, `LOW_SPEED`, `CUSTOM`. It is
mandatory, and with `CUSTOM` a non-empty `comment` is mandatory too; both are validated
locally. `reason=` works as an alias of `type=`.

Downloads return a file, not the envelope: `text/plain` for `txt` and custom templates,
`text/csv` for `csv`. Custom `ext` templates accept `%ip%`, `%port%`, `%login%`, `%user%`,
`%password%`, `%protocol%`, `%rotation_link%`.

`package_key` is honoured **only** on `proxy/download/subresident`; the literal
`proxy/download/resident` route ignores it and exports the parent package, so
`proxyDownload('resident', package_key=...)` raises a `ValueError`. Use
`proxyDownload('subresident', package_key='KEY')` for a subpackage and
`proxyDownloadResident(id=...)` for the main package.

## Resident and subuser lists

`resident/lists` returns a **flat array** (no `items` wrapper).

`residentGeo()` returns the `geo.json` **file** (`application/json` +
`Content-Disposition: attachment`) with the `country -> regions -> cities -> ISPs` tree; it
is **not** a zip archive. `residentGeoIsp()` likewise returns `isp.json`. Both come back as
`bytes` — parse them with `json.loads`. Text exports return `str`.

```python
import json
geo = json.loads(api.residentGeo().decode('utf-8'))
```

`residentListAdd()` accepts positional arguments or the full dictionary with
`title`, `whitelist`, `geo`, `export`, and `rotation`. `geo` must be an **object**
(`{'country', 'region', 'city', 'isp'}`) — the server expects an object and rejects an array;
an empty geo is valid and means "no geo filter".

```python
api.residentSubUserCreate({'traffic_limit': '1073741824', 'rotation': 60})
api.residentSubUserUpdate({
    'package_key': 'PACKAGE_KEY', 'traffic_limit': '2147483648',
    'expired_at': '2026-12-31', 'is_active': True,
})
api.residentSubUserListAdd(
    'PACKAGE_KEY', title='US list', whitelist='127.0.0.1',
    geo={'country': 'US', 'region': 'Washington'},
    export={'ports': 1000, 'ext': 'txt'}, rotation=60)
```

`traffic_limit` is required when creating a subpackage; `package_key` is required for every
subuser call. `expired_at` is sent as a **string**, but comes back as a PHP-date **object**:
`{'date': '2026-12-31 00:00:00.000000', 'timezone_type': 3, 'timezone': 'UTC'}`. The main
package (`residentPackage()`) instead reports `expiredAt` as a `dd.MM.yyyy HH:mm:ss` string.

`residentTrafficDetails()` requires the package key under the name **`packageKey` or
`key`** — `package_key` is not read there and the server answers `key is required`.

Delete endpoints return their payload as a string inside a successful envelope; the library
normalizes it to a dict, so check the `status` field:

```python
api.residentSubUserListDelete('PACKAGE_KEY', 561)  # {'status': 'delete'} | {'status': 'not-found'}
```

## v1 migration notes

- IDs in v2 are ObjectId strings; old numeric v1 IDs do not resolve (resident list ids stay
  numeric, and `rotationId` is minutes, not an id).
- A code can be passed directly in the `*Id` argument of `order/*` and `prolong/*`, so the
  `*Code` options are optional; `rotationId` never takes a code.
- `authActive(id, "Y")` became `authChange(id, True)`.
- `ping()` and `proxyCheck()` have no v2 equivalent.
- `residentListDelete()` sends the ID in the request body.
- `balanceAdd()` uses its explicit `paymentId`, then falls back to `setPaymentId()`;
  `paymentCode` is not accepted here.
- `proxy/replace` takes the replacement reason in `type`, plus `comment` for `CUSTOM`.

## Keeping up with the server

Changes made after the 2.0 release, in the order the server shipped them:

- **`order/list` is new** — `orderList()`, see [Listing orders](#listing-orders). Its query
  filters and response fields use snake_case names such as `start_date` and `is_extend`, and its
  `data` is a `metadata` + `items` pair rather than a flat list.
- **`resident/autorenew/{enable,disable,calculate}` were removed** and replaced by
  `autoprolong/{calc,enable,disable}/{type}` — see [Automatic renewal](#automatic-renewal).
  `type='resident'` is the residential branch of the same three endpoints.
- **`order/make` gained the `X-Fingerprint` header.** The SDK can send it (config,
  `setFingerprint()` or per call). At the time the server refused residential and scraper orders
  without it and the SDK raised locally for those two sections; that requirement is gone — see
  the last entry.
- **`dailyCountCap` / `monthlyAmountCap` were removed** from `balance/autotopup/set`
  (2026-08-18). The server ignores them, so the SDK now raises `ValueError` rather than letting
  the call look successful while changing nothing. Error codes 54 and 55 are gone with them.
- **`*Code` no longer overrides a paired `*Id`** for `mixId`, `operatorId`, `rotationId` and
  `tarifId`. The server gives the *id* priority on those four, and the SDK was inverting it.
- **`generateAuth`, `highAvailability` and `isUptime` are no longer dropped.** They were missing
  from the internal whitelist, so passing them to an order helper silently lost the value —
  `generateAuth` was then overwritten by `setGenerateAuth()` (default `'N'`).
- **`prolongMake()` no longer second-guesses the envelope.** Insufficient funds now arrive as
  `errors[{code: 16}]` and raise like any other business error; a legitimate success with an
  empty `orderId` is returned intact instead of being turned into a false failure after the
  money has already been taken.
- **Renewal selection is per type now, and its fields were renamed (breaking).** `prolong/*` and
  `autoprolong/*` no longer read `ids`, `orderSeparatorIds` or `orderSeparatorId`. `ipv4`, `isp`
  and `mobile` are renewed per proxy by `ipIds` (the proxy `id`) or `ips` (addresses); `ipv6`,
  `mix` and `mix_isp` only as whole orders by `orderIds` (the `order_id`), so `ipv6` is no longer
  renewed by its `host:port`, and a MIX order is renewed with all of its packages rather than by
  separator ids. A selection field of the wrong kind is rejected by name
  (`[ipIds] is not applicable for ipv6: prolong by [orderIds]`), and an unknown or foreign order
  fails the whole request with code 29 `Incorrect orderIds`. The SDK routes the second argument
  of `prolongCalc()`, `prolongMake()` and `autoProlong*()` by type — the parameter keeps its name
  `ids` — and drops empty lists. It raises `ValueError` when the removed fields are passed as
  keywords, in `options` or in the payload dict (naming `ipIds`/`orderIds` as the replacement),
  on a mix of proxy ids and addresses, and on any selection for `resident` auto-renewal. Responses:
  `prolong/make` adds `orderIds`, every renewed order (`orderId` is the first of them);
  `autoprolong/enable|disable` renamed `ids` to `ipIds` and added `orderIds`. See
  [Renewing proxies](#renewing-proxies).
- **`X-Fingerprint` is optional.** The server no longer requires the header for API-key orders,
  residential and scraper included, so the SDK no longer raises when no fingerprint is set. It
  still sends the header whenever you provide a value — see
  [Optional fingerprint](#optional-fingerprint).
- **Behaviour change: requests are now paced by default** (see
  [Rate limits and the request queue](#rate-limits-and-the-request-queue)). The client starts
  at most 1000 requests in any 60 seconds, sends write and money requests one at a time (1 s
  apart, money requests 2 s apart) and retries an HTTP 429 from the edge up to 3 times after
  `Retry-After`, so a call can now block the calling thread for a while. Nothing else is
  retried. `'rate_limit': {'enabled': False}` in the config restores the previous behaviour
  exactly.

## Tests

Offline, no network access required:

```sh
python -m unittest discover
```
