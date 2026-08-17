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

| Argument | Accepts | Available from `referenceList()`? |
| --- | --- | --- |
| `countryId` | country ObjectId, or the alpha3 code (uppercased before lookup, so `usa` == `USA`) | **Yes** — `country[].alpha3` next to `country[].id` |
| `periodId` | period ObjectId, or the period code (lowercased, e.g. `1m`) | **No** — `period[]` only carries `id` and `name` (`"1 month"`). Take `id`, or use a code you already know |
| `operatorId` | mobile operator ObjectId, or its tag (used **as is**, case-sensitive) | **Not as a separate field** — take `country[].operators.dedicated[].id` / `.shared[].id` and pass it unchanged; depending on the data source that value is already either the id or the tag, and both resolve |
| `rotationId` | **minutes** as an integer, `0` = `By Link`. No codes exist | **Yes** — `country[].operators.*[].rotations[].id` *is* the minute value |
| `mixId` | mix package ObjectId, or its tag (exact match) | **Yes, for `mix`/`mix_isp`** — `country[].tag` (for example `usa-europe-mix_IPv4`); `quantities[]` gives `id`/`name`/`quantities` without a tag |
| `tarifId` | resident tariff ObjectId, or its code (exact match) | **No** — `tarifs[]` returns `id`, `name`, `personal`. Take `id` |
| `paymentId` in `order/*`, `prolong/*` | payment-system ObjectId, its code, or a payment type name (`balance`) | **No** — `balancePaymentsList()` returns `id` and `name`. Take `id`, or use a known code |
| `paymentId` in `balance/add` | **ObjectId only** — this endpoint does not resolve codes (see `balanceAdd`) | — |

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

# MIX: the first argument is a package, not a country — ObjectId or the tag from
# referenceList()['mix']['country'][0]['tag']
api.orderCalcMix('usa-europe-mix_IPv4', '1m', 1)

# resident: a tariff (ObjectId or code) and an optional coupon
api.orderCalcResident('TARIF_ID')
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
`mix`/`mix_isp` it is not required once the package is resolved — by `mixId`/`mixCode`, by
`countryId='PACKAGE_ID:QUANTITY'`, or by `countryId='PACKAGE_ID'` plus `quantity > 0`.

## Prolongation

The object/keyword form exposes the complete v2 payload: `ids`,
`orderSeparatorId`, `orderSeparatorIds`, `periodId`, `paymentId`, and `coupon`. Types: `ipv4`,
`ipv6`, `mobile`, `isp`, `mix`, `mix_isp`. Here too `periodId` and `paymentId` accept a code
instead of an ObjectId, so `periodCode`/`paymentCode` are never required.

```python
api.prolongMake(
    'mix', orderSeparatorIds=['SEPARATOR_ID'],
    periodId='1m', paymentId='balance', coupon='SALE10')

api.prolongCalc('ipv4', ['ORDER_ID'], '3m')
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
  numeric, and `rotationId` is minutes, not an id).
- A code can be passed directly in the `*Id` argument of `order/*` and `prolong/*`, so the
  `*Code` options are optional; `rotationId` never takes a code.
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
