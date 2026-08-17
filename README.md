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
    with Api({
        'key': 'YOUR_API_KEY',
        # Optional root before the API key (useful for local/dev environments):
        'base_url': 'https://proxy-seller.com/personal/api/v2/',
        'timeout': 30,
        'headers': {'X-Request-Source': 'my-app'},
    }) as api:
        print(api.balance())
except ApiError as error:
    print(error.code, error.custom_data, error.http_status, error.errors)
```

Call `api.close()` when a context manager is not used. Configuration and payment/
authorization state belong to each `Api` instance; they are not shared by clients.

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
`paymentId`, `operatorId`, `rotationId`, `tarifId`, auth ids, IP-address ids. Old numeric v1
IDs do not resolve, and nothing should be parsed into `int`.

The one exception: **resident list ids are numeric** (`Long`), both in the main package
(`residentList`, `residentListRename`, `residentListRotation`, `residentListDelete`,
`proxyDownloadResident(id=...)`) and inside subpackages (`residentSubUserListRename`,
`residentSubUserListRotation`, `residentSubUserListDelete`). Those are not ObjectIds.

Stable codes (`countryCode`, `periodCode`, `mixCode`, `operatorCode`, `rotationCode`,
`tarifCode`, `paymentCode`) are the id-free alternative for order/prolong calls and come from
`referenceList()`.

## Orders

Legacy positional ID calls remain supported. Stable codes can be passed as keyword
options or in one dictionary and are available from `referenceList()`.

```python
api.setPaymentCode('balance')  # or setPaymentId('...')

api.orderCalcIpv4(
    countryCode='USA', periodCode='1m', quantity=2, uptime=True)

api.orderMakeMobile({
    'countryCode': 'USA', 'periodCode': '1m', 'quantity': 1,
    'mobileServiceType': 'dedicated', 'operatorCode': 'operator-code',
    'rotationCode': '10m',
})

# MIX uses a package identifier, not countryId:
api.orderCalcMix('MIX_ID', 'PERIOD_ID', 1)
api.orderCalcMixByCode('mix-us-eu', '1m', 1)
```

`sectionCode` values: `ipv4`, `ipv6`, `mobile`, `isp`, `mix`, `mix_isp`, `resident`,
`scraper`. `mobileServiceType` is required by the API and defaults to legacy-compatible
`dedicated`. `uptime` is available for supported IPv4/ISP combinations.
`setGenerateAuth('Y')` affects only `order/make`.

`customTargetName` is required for `ipv4`, `ipv6` and `isp` (the server answers
`Incorrect goal`, code 14, without it) and is checked locally before the request. For
`mix`/`mix_isp` it is not required once the package is resolved — by `mixId`/`mixCode`, by
`countryId='PACKAGE_ID:QUANTITY'`, or by `countryId='PACKAGE_ID'` plus `quantity > 0`.

## Prolongation

The object/keyword form exposes the complete v2 payload: `ids`,
`orderSeparatorId`, `orderSeparatorIds`, `periodId`/`periodCode`,
`paymentId`/`paymentCode`, and `coupon`. Types: `ipv4`, `ipv6`, `mobile`, `isp`, `mix`,
`mix_isp`.

```python
api.prolongMake(
    'mix', orderSeparatorIds=['SEPARATOR_ID'],
    periodCode='1m', paymentCode='balance', coupon='SALE10')
```

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
#  'subscriptionId': 'sub_1', 'paymentMethod': {...}, 'dailyCountCap': 3,
#  'monthlyAmountCap': 300, 'failCount': 0, 'lastAttemptAt': None, 'lastEvent': None}

api.balanceAutoTopupSet(threshold=20)                    # partial update: only threshold
api.balanceAutoTopupSet(enabled=False)                   # False is sent, not dropped
api.balanceAutoTopupSet({'amount': 50, 'dailyCountCap': 2})
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
`54` (daily cap too low), `55` (monthly cap below one top-up), `56` (saved card expired).
Boundary values arrive in `error.custom_data`: `minAmount`, `minThreshold`,
`minDailyCountCap`.

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
  numeric).
- `authActive(id, "Y")` became `authChange(id, True)`.
- `ping()` and `proxyCheck()` have no v2 equivalent.
- `residentListDelete()` sends the ID in the request body.
- `balanceAdd()` uses its explicit `paymentId`, then falls back to `setPaymentId()`;
  `paymentCode` is not accepted here.
- `proxy/replace` takes the replacement reason in `type`, plus `comment` for `CUSTOM`.

## Tests

Offline, no network access required:

```sh
python -m unittest discover
```
