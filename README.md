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
authorization state belong to each `Api` instance; they are not shared by clients.

### Paying for orders

Every order and renewal needs a payment system. Take one from `balancePaymentsList()` and set it
once:

```python
payments = api.balancePaymentsList()   # [{'id': '69e7…', 'name': 'PayPal'}, …]
api.setPaymentId(payments[0]['id'])
```

This is the one place where an id is unavoidable: several payment systems share the same internal
code (a single `cryptomus` covers "USDT (TRC-20)", "All cryptocurrencies" and more), so the code
cannot tell them apart. Everywhere else you use human-readable codes.

### Residential and scraper orders need a fingerprint

`order/make` carries an `X-Fingerprint` header. Most sections ignore it, but **residential and
scraper orders are not created without it at all** — the order service answers `Header
X-Fingerprint is required` and nothing is ordered.

```python
api = Api({'key': 'YOUR_API_KEY', 'fingerprint': 'my-installation-id'})
# or later:
api.setFingerprint('my-installation-id')
# or for a single call:
api.orderMakeResident('1-gb', fingerprint='my-installation-id')
```

Any opaque string is accepted — the server does not validate its shape — but it must be a
**stable identifier of your installation**. The SDK deliberately does not generate one: a value
randomized per process would break the anti-fraud and affiliate attribution the header exists for.

Ordering resident or scraper without a fingerprint raises `ValueError` locally, rather than
spending a round trip on a request the server is certain to reject.

<details>
<summary>Pointing the client at another host, and extra headers</summary>

```python
with Api({
    'key': 'YOUR_API_KEY',
    'base_url': 'http://localhost:7995/personal/api/v2/',
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
actually happened — always read the whole array. There is **no HTTP 429**: the rate limit
(1000 requests per minute per key, calendar-minute window) arrives as the same triple.

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
empty, the server resolves it as a code (`normalizeOrderReferenceCodes` /
`normalizeProlongReferenceCodes`). The `*Code` keyword options are kept for compatibility, but
nothing needs them — a code goes straight into `countryId`, `periodId`, `operatorId`, `mixId`,
`tarifId`, `paymentId`.

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
| `paymentId` | payment-system ObjectId — the one unavoidable id | `balancePaymentsList()` → `id`, see "Paying for orders" above |

ObjectIds are still accepted everywhere if you happen to have them; the reference simply no longer
publishes them. `balance/add` is the one endpoint that resolves no codes at all — it needs a real
`paymentId` (see `balanceAdd`).

## Orders

Reference values are passed positionally into the `*Id` arguments, as ObjectIds or as codes;
`authorization` and `coupon` are the only arguments normally left as `None`.

```python
api.setPaymentId('PAYMENT_ID')  # or setPaymentCode('balance')

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
`scraper`. `mobileServiceType` is required by the API and defaults to legacy-compatible
`dedicated`. `uptime` is available for supported IPv4/ISP combinations.
`setGenerateAuth('Y')` affects only `order/make`.

`customTargetName` is required for `ipv4`, `ipv6` and `isp` (the server answers
`Incorrect goal`, code 14, without it) and is checked locally before the request. For
`mix`/`mix_isp` it is only needed when the server cannot tell which package you mean, so naming
the package — `orderCalcMix('europe-2-mix_IPv4', '1m', 10)` — removes the need for it.

## Renewing proxies

Renew by the addresses themselves — the same strings `proxyList()` gives you. No ids to look up:

```python
ipv4 = api.proxyList('ipv4')['items']
ips = [item['ip'] for item in ipv4]          # ['1.2.3.4', '5.6.7.8']

api.prolongCalc('ipv4', ips, '1m')           # price first
api.prolongMake('ipv4', ips, '1m')           # deducts money
```

`prolongCalc()` shows the price; `prolongMake()` charges the balance. If the balance is short,
`prolongMake()` raises `ApiError` with the server's warning — it never reports a renewal that did
not happen.

What to pass follows the proxy type, and every value comes straight out of `proxyList()`:

| type | pass this | built from |
|---|---|---|
| `ipv4`, `isp`, `mix`, `mix_isp` | the address, `1.2.3.4` | `item['ip']` |
| `ipv6` | the address, `host:port` — `1.2.3.4:26000` | `item['ip']` |
| `mobile` | the address, `ip:port_http:port_socks` | `item['ip']`, `item['port_http']`, `item['port_socks']` |

For `ipv6` the `ip` field already carries the gateway together with the port
(`1.2.3.4:26000`), while `ip_only` holds the bare gateway — what the API publishes is the
gateway, not the IPv6 address itself. So `ip` is passed as it comes, exactly like every other
type; only `mobile` has to be assembled, out of the three fields above:

```python
mobile = api.proxyList('mobile')['items']
addresses = ['{}:{}:{}'.format(item['ip'], item['port_http'], item['port_socks'])
             for item in mobile]
api.prolongCalc('mobile', addresses, '1m')
```

ObjectId strings work for every type, and a mixed list works — each value is routed by its shape
(an id is 24 hex characters, with no dot and no colon, so it goes into `ids` on its own). The
period takes a code (`'1m'`), same fallback as `order/*`, and the fourth argument is a coupon.

<details>
<summary>Renewing part of a MIX order</summary>

A MIX order can be split into parts that renew independently. Those parts are addressed by id,
passed as keywords:

```python
api.prolongMake(
    'mix', orderSeparatorIds=['SEPARATOR_ID'],
    periodId='1m', coupon='SALE10')
```

The keyword form also exposes the complete v2 payload: `ips`, `ids`, `orderSeparatorId`,
`orderSeparatorIds`, `periodId`, `paymentId`, and `coupon`. Types: `ipv4`, `ipv6`, `mobile`,
`isp`, `mix`, `mix_isp`.

</details>

## Automatic renewal

`prolongMake()` charges you now. `autoprolong/*` only arms a charge that happens later, without
you present — a separate branch of the API, not a flag on prolong.

```python
api.autoProlongCalc('ipv4', ['1.2.3.4'], '1m', paymentId='balance')
api.autoProlongEnable('ipv4', ['1.2.3.4'], '1m', paymentId='balance')
api.autoProlongDisable('ipv4', ['1.2.3.4'])
```

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

Three things about the answers before you parse them:

* **`ids` is not an echo.** For `ipv6` the whole order is switched at once, so `quantity` and
  `ids` can cover more proxies than you sent.
* **Not enough money is not an exception.** `calc` answers `status: "error"` with a *filled*
  `data` and an empty `errors[]` — the same shape `prolong/calc` uses. Read `data['warning']`.
* **Residential fills different fields.** `days` and `chargeDate` are `None` there (a package
  renews on expiry *or* on traffic exhaustion, so no single date describes it); `tarifId` and
  `dateEnd` carry the meaning instead.

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

`residentListAdd()` accepts the legacy positional form or the full dictionary with
`title`, `whitelist`, `geo`, `export`, and `rotation`. `geo` must be an **object**
(`{'country', 'region', 'city', 'isp'}`) — the server binds `GeoDto`, an array is rejected;
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

- **`resident/autorenew/{enable,disable,calculate}` were removed** and replaced by
  `autoprolong/{calc,enable,disable}/{type}` — see [Automatic renewal](#automatic-renewal).
  `type='resident'` is the residential branch of the same three endpoints.
- **`order/make` requires `X-Fingerprint`** for residential and scraper orders. The SDK can now
  send it; without a value those two sections raise locally instead of being rejected by the
  server.
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

## Tests

Offline, no network access required:

```sh
python -m unittest discover
```
