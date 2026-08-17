import json
import requests
from urllib.parse import quote


class _Unset:
    """
    Маркер "поле не передано" для partial update (balance/autotopup/set): None использовать
    нельзя, потому что False и 0 — валидные значения полей, а опущенное поле уезжать не
    должно. Свой класс, а не object(), только ради читаемого repr в help()/сигнатурах.
    """

    __slots__ = ()

    def __repr__(self):
        return 'UNSET'


_UNSET = _Unset()


class ApiError(Exception):
    """
    Typed client/API error with both business and HTTP response details.

    Client API v2 почти всегда отвечает HTTP 200, а ошибка лежит в конверте
    ``{status, data, errors}``. ``message``/``code``/``custom_data`` — это errors[0],
    но ошибки доступа приходят фиксированной ТРОЙКОЙ
    (``Error api key`` / ``IP not allowed <ip>`` / ``Request limit reached``, все с code 503,
    см. LegacyClientApiErrorHelper.legacyAccessErrors на бэкенде), поэтому по errors[0]
    нельзя отличить битый ключ от неразрешённого IP и от превышения лимита запросов.
    Полный массив доступен в ``errors``.
    """

    def __init__(self, message, code=None, custom_data=None, http_status=None, body=None,
                 errors=None):
        super().__init__(message or 'Client API request failed')
        self.code = code
        self.custom_data = custom_data
        self.customData = custom_data
        self.http_status = http_status
        self.httpStatus = http_status
        self.body = body
        # Весь массив errors, а не только первый элемент.
        self.errors = list(errors) if isinstance(errors, (list, tuple)) else []

    def isAccessError(self):
        """
        True, если это тройка ошибок доступа (битый ключ / IP не разрешён / rate limit).
        HTTP 429 в v2 не существует: лимит приходит теми же HTTP 200 + "Error api key".
        """
        messages = [
            str((item or {}).get('message') or '')
            for item in self.errors if isinstance(item, dict)
        ]
        if not messages:
            messages = [str(self.args[0] if self.args else '')]
        return any(
            message == 'Error api key'
            or message == 'Request limit reached'
            or message.startswith('IP not allowed')
            or message.startswith('Error auth.')
            for message in messages)


class Api:
    URL = 'https://proxy-seller.com/personal/api/v2/'

    def __init__(self, config):
        """
        API key placed in https://proxy-seller.com/personal/api/.

        Args:
            config (dict): Configuration options.

        Raises:
            Exception: If an error occurs during the configuration process.
        """

        if 'key' not in config:
            raise ValueError(
                "Need key, placed in https://proxy-seller.com/personal/api/")
        api_root = config.get('base_url') or config.get('baseUrl') or config.get('baseURL') or self.URL
        self.base_uri = str(api_root).rstrip('/') + '/' + quote(str(config['key']), safe='') + '/'
        self.timeout = config.get('timeout', 30)
        self.headers = {'Content-Type': 'application/json', **config.get('headers', {})}
        self.request_options = dict(config.get('request_options', {}))
        self.session = config.get('session') or requests.Session()
        self.paymentId = None
        self.paymentCode = None
        self.generateAuth = 'N'

    def close(self):
        """Close the underlying requests session."""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def setPaymentId(self, id):
        """
        Payment system id (MongoDB ObjectId from balance/payments/list).

        В order/* и prolong/* сюда можно положить и КОД платёжной системы (например
        'balance'): сервер, не найдя ObjectId, резолвит значение как код. Исключение —
        balance/add, он принимает только настоящий ObjectId (см. balanceAdd).
        """
        self.paymentId = id

    def getPaymentId(self):
        return self.paymentId

    def setPaymentCode(self, code):
        """Set a stable payment-system code (for example ``balance``)."""
        self.paymentCode = code

    def getPaymentCode(self):
        return self.paymentCode

    def setGenerateAuth(self, yn):
        """
        Generate new auths Y/N, default N.
        Only applied to order/make, the order/calc endpoint ignores the field.
        """
        if yn == 'Y':
            self.generateAuth = 'Y'
        else:
            self.generateAuth = 'N'

    def getGenerateAuth(self):
        return self.generateAuth

    def request(self, method, uri, **options):
        """
        Send a request to the server.

        Args:
            method (str): The HTTP method to use for the request.
            uri (str): The URI to send the request to.
            options (dict): Additional options for the request.

        Returns:
            mixed: The response from the server.

        Raises:
            Exception: If an error occurs during the request.
        """
        request_options = {**self.request_options, **options}
        request_headers = {**self.headers, **request_options.pop('headers', {})}
        request_timeout = request_options.pop('timeout', self.timeout)
        try:
            response = self.session.request(
                method, self.base_uri + uri, headers=request_headers,
                timeout=request_timeout, **request_options)
        except requests.RequestException as error:
            error_response = getattr(error, 'response', None)
            raise ApiError(
                str(error), http_status=getattr(error_response, 'status_code', None),
                body=getattr(error_response, 'content', None)) from error

        content_type = response.headers.get('Content-Type', '').lower()
        content_disposition = response.headers.get('Content-Disposition', '').lower()
        is_json = 'json' in content_type
        is_attachment = 'attachment' in content_disposition
        if is_json and is_attachment:
            data = response.content
        elif is_json:
            try:
                data = response.json()
            except ValueError:
                data = response.content
        elif content_type.startswith('text/') or 'csv' in content_type:
            data = response.text
        else:
            data = response.content

        if isinstance(data, dict):
            is_envelope = 'status' in data and ('data' in data or 'errors' in data)
            if is_envelope:
                if data.get('status') == 'success':
                    return data.get('data')
                errors = data.get('errors')
                if isinstance(errors, list) and errors:
                    raise self._api_error(errors[0], response.status_code, data, errors)
                if 200 <= response.status_code < 300 and data.get('data') is not None:
                    return data.get('data')
                raise ApiError(
                    'Client API returned an error',
                    http_status=response.status_code, body=data)
            if not 200 <= response.status_code < 300:
                raise self._api_error(data, response.status_code, data)
        elif not 200 <= response.status_code < 300:
            message = data if isinstance(data, str) and data else 'Client API HTTP {}'.format(response.status_code)
            raise ApiError(message, http_status=response.status_code, body=data)

        return data

    @staticmethod
    def _api_error(error, http_status, body, errors=None):
        item = error if isinstance(error, dict) else {}
        return ApiError(
            item.get('message') or item.get('error') or 'Client API HTTP {}'.format(http_status),
            code=item.get('code'),
            custom_data=item.get('customData', item.get('custom_data')),
            http_status=http_status,
            body=body,
            errors=errors if errors is not None else ([item] if item else []))

    @staticmethod
    def filterNone(params):
        """Drop None values, used for optional query filters."""
        return {k: v for k, v in params.items() if v is not None}

    @staticmethod
    def _delete_result(data):
        """
        data delete-эндпоинтов приходит СТРОКОЙ, а не объектом: resident/list/delete → "delete",
        residentsubuser/delete и residentsubuser/list/delete → JSON внутри строки (например
        {"status": "not-found"} при конверте status="success"). Раньше методы возвращали сырую
        строку при docstring 'dict', и неудавшееся удаление было неотличимо от успешного.
        """
        if isinstance(data, dict):
            return data
        if data is None:
            return {}
        if isinstance(data, str):
            trimmed = data.strip()
            if trimmed.startswith('{'):
                try:
                    parsed = json.loads(trimmed)
                    if isinstance(parsed, dict):
                        return parsed
                except ValueError:
                    pass
            return {'status': trimmed}
        return {'status': data}

    @staticmethod
    def _normalize_geo(geo):
        """
        geo на бэкенде — ОБЪЕКТ (GeoDto: country/region/city/isp), не массив: см.
        ListAddRequestDto.geo и CreatePackageListRequestDto.geo. Массив Jackson не свяжет,
        и сервер ответит голым HTTP 400 мимо конверта.

        Пустой geo допустим (означает "без гео-фильтра"), поэтому None и пустая
        последовательность приводятся к {}. Легаси-форму "массив из одного объекта"
        разворачиваем в объект. Всё остальное — понятная локальная ошибка вместо
        AttributeError на .items().
        """
        if geo is None:
            return {}
        if isinstance(geo, dict):
            return Api.filterNone(geo)
        if isinstance(geo, (list, tuple)):
            if not geo:
                return {}
            if len(geo) == 1 and isinstance(geo[0], dict):
                return Api.filterNone(geo[0])
        raise ValueError(
            "geo must be a dict with country/region/city/isp keys "
            "(client api expects a JSON object, not {})".format(type(geo).__name__))

    @staticmethod
    def assertExt(ext):
        """
        The server rejects ext with a plain text 400 instead of the usual envelope,
        so it is validated on the client side.
        """
        if ext is None:
            return None
        if len(ext) > 250:
            raise ValueError("ext is too long (max 250)")
        if any(c in ext for c in ('\r', '\n', '/', '\\')):
            raise ValueError("ext contains forbidden characters")
        return ext

    # --------------------------- Auth ---------------------------

    def authList(self):
        """
        Get auths

        Returns:
            array list auths
        """
        return self.request('GET', 'auth/list')

    def authAdd(self, orderNumber, generateAuth='N'):
        """
        Create login/password authorization.

        Args:
            orderNumber (str): Order number.
            generateAuth (str): Y/N

        Returns:
            dict: Created auth.
        """
        return self.request('POST', 'auth/add', json={'orderNumber': orderNumber, 'generateAuth': generateAuth})

    def authAddIp(self, orderNumber, ip):
        """
        Create IP authorization.

        Args:
            orderNumber (str): Order number.
            ip (str): IP address.

        Returns:
            dict: Created auth.
        """
        return self.request('POST', 'auth/add/ip', json={'orderNumber': orderNumber, 'ip': ip})

    def authChange(self, id, active, login=None, password=None, ip=None):
        """
        Change authorization.
        Replaces the v1 auth/active method, the active flag is a boolean now.

        Args:
            id (str): Auth id (ObjectId-СТРОКА, не число).
            active (bool): Active state.
            login (str): New login.
            password (str): New password.
            ip (str): New ip.

        Returns:
            dict: Current auth.
        """
        return self.request('POST', 'auth/change', json={
            'id': id, 'active': active, 'login': login, 'password': password, 'ip': ip})

    def authDelete(self, id):
        """
        Delete authorization.

        Args:
            id (str): Auth id (ObjectId-СТРОКА, не число).

        Returns:
            dict: Result.
        """
        return self.request('DELETE', 'auth/delete', json={'id': id})

    # --------------------------- Balance ---------------------------

    def balance(self):
        """
        Get balance statistic

        Returns:
            float: Balance value
        """
        return self.request('GET', 'balance/get')['summ']

    def balanceAdd(self, summ=5, paymentId=None, paymentCode=None):
        """
        Replenish the balance.

        Args:
            summ (float): Amount. Минимум настраиваемый на сервере, сумма, равная минимуму,
                проходит.
            paymentId (str): Payment system id (ObjectId-строка из balance/payments/list).
            paymentCode (str): Не поддерживается этим эндпоинтом, см. Raises.

        Raises:
            ValueError: если paymentId не задан, а задан только paymentCode. В отличие от
                order/* и prolong/* эндпоинт balance/add НЕ резолвит код платёжной системы:
                BalanceAddRequestClientDto знает только поля summ и paymentId, и
                normalizeOrderReferenceCodes для него не вызывается. Раньше в этом случае
                улетал paymentId=null и сервер отвечал "Payment id not exists".

        Returns:
            str: A link to the payment page.
        """
        if paymentId is None:
            paymentId = self.getPaymentId()
        if paymentId is None:
            code = paymentCode or self.getPaymentCode()
            if code:
                raise ValueError(
                    "balance/add does not resolve paymentCode ({!r}): the endpoint accepts "
                    "only summ and paymentId. Take the id from balancePaymentsList() and pass "
                    "paymentId (or setPaymentId(...)).".format(code))
        elif paymentCode:
            raise ValueError(
                "balance/add accepts only paymentId, drop paymentCode ({!r})".format(paymentCode))
        return self.request('POST', 'balance/add', json={'summ': summ, 'paymentId': paymentId})['url']

    def balancePaymentsList(self):
        """
        List of payment systems for balance replenishing.

        Returns:
            list: Payment system items. BALANCE из списка исключён — пополнить баланс
                балансом нельзя.
        """
        return self.request('GET', 'balance/payments/list')['items']

    # --------------------------- Balance auto top-up ---------------------------

    #: Поля запроса balance/autotopup/set (AutoTopupSetRequestClientDto).
    AUTO_TOPUP_FIELDS = (
        'enabled', 'threshold', 'amount', 'subscriptionId', 'dailyCountCap', 'monthlyAmountCap')

    def balanceAutoTopupGet(self):
        """
        Auto top-up configuration and current state.

        Returns:
            dict: AutoTopupStateClientDto:
                configured (bool), enabled (bool),
                state (str): NO_PAYMENT_METHOD | DISABLED | ACTIVE | PAYMENT_INVALID |
                    PAUSED_FAILURES,
                threshold (float): списываем, когда баланс становится меньше этого значения,
                amount (float): сумма одного автопополнения,
                subscriptionId (str|None): закреплённая подписка Paddle (None у настроек,
                    созданных до 2026-08-14),
                paymentMethod (dict|None): {id, status ("active"/"expired"), paymentMethod
                    ("card"/"PayPal"/"Google Pay"/"Apple Pay"), brand, last4, exp ("MM/YYYY")},
                dailyCountCap (int), monthlyAmountCap (float): действующие анти-абьюз лимиты
                    (свои или серверные дефолты),
                failCount (int): подряд идущие неудачные списания,
                lastAttemptAt: дата последней попытки или None (java.util.Date, при
                    дефолтной сериализации Spring — строка ISO-8601),
                lastEvent (dict|None): {status (TRIGGERED | SUCCEEDED | FAILED | SKIPPED_CAP |
                    SETTINGS_SAVED | PAUSED), amount, at (дата), reason (None для
                    SUCCEEDED)}.

        Raises:
            ApiError: code 49 ("Auto top-up is not available"), если фича выключена
                серверным флагом enabled_autopopup_balance.
        """
        return self.request('GET', 'balance/autotopup/get')

    def balanceAutoTopupSet(self, enabled=_UNSET, threshold=_UNSET, amount=_UNSET,
                            subscriptionId=_UNSET, dailyCountCap=_UNSET,
                            monthlyAmountCap=_UNSET):
        """
        Enable/disable auto top-up or update its thresholds and caps.

        PARTIAL UPDATE: любое непереданное поле сервер не меняет, поэтому в тело уходят
        ТОЛЬКО реально переданные поля — чтобы поменять один порог, достаточно
        ``balanceAutoTopupSet(threshold=20)``. Значения False и 0 передаются как есть,
        а не отбрасываются.

        Первым позиционным аргументом можно передать dict — тогда его ключи уходят как есть
        (None-значения выбрасываются, потому что "null" и "не передано" для сервера
        неотличимы).

        Args:
            enabled (bool): включить/выключить.
            threshold (float): списываем, когда баланс становится меньше этого значения.
            amount (float): сумма одного автопополнения; минимум $5 и не меньше threshold.
            subscriptionId (str): подписка Paddle, которой списывать — ``paymentMethod.id``
                из balanceAutoTopupGet(). Пока карта одна, можно не передавать.
            dailyCountCap (int): свой (более строгий) лимит числа списаний в сутки.
            monthlyAmountCap (float): свой лимит суммы списаний за 30 дней.

        Returns:
            dict: состояние ПОСЛЕ сохранения, в той же форме, что у balanceAutoTopupGet() —
                второй запрос за актуальным state не нужен.

        Raises:
            ApiError: валидация целиком серверная и применяется к РЕЗУЛЬТАТУ мержа. Коды:
                49 — фича недоступна, 50 — threshold ниже минимума,
                51 — amount ниже минимума, 52 — amount не покрывает threshold,
                53 — нет привязанного способа оплаты, 54 — dailyCountCap ниже минимума,
                55 — monthlyAmountCap меньше одного пополнения, 56 — карта истекла.
                Граничные значения приходят в ``error.custom_data``:
                {"minAmount": ..., "minThreshold": ..., "minDailyCountCap": ...}.
        """
        if isinstance(enabled, dict):
            data = self.filterNone(dict(enabled))
        else:
            values = {
                'enabled': enabled, 'threshold': threshold, 'amount': amount,
                'subscriptionId': subscriptionId, 'dailyCountCap': dailyCountCap,
                'monthlyAmountCap': monthlyAmountCap}
            # _UNSET и None означают "поле не передано": null для сервера равен отсутствию
            # ключа, и отправлять его вместо сохранённого значения незачем. False и 0
            # сохраняются, поэтому filterNone здесь не годится.
            data = {
                key: values[key] for key in self.AUTO_TOPUP_FIELDS
                if values[key] is not _UNSET and values[key] is not None}
        return self.request('POST', 'balance/autotopup/set', json=data)

    # --------------------------- Order ---------------------------

    def referenceList(self, type=None):
        """
        Retrieve necessary guides for creating an order.

        Args:
            type (str): The type of the proxy - ipv4, ipv6, mobile, isp, mix, mix_isp,
                resident, scraper or None. Без типа приходят все разделы в том же порядке
                ключей, что в v1: ipv4, ipv6, mobile, isp, mix, mix_isp, resident, scraper.

        Returns:
            dict: The guide information for creating an order. Идентификаторы внутри —
                ObjectId-СТРОКИ, КРОМЕ ротации: rotations[].id — это ЧИСЛО МИНУТ
                (0 = "By Link").

                С указанным типом раздел приходит завёрнутым: {'items': {...}}; без типа —
                словарь разделов сразу.

                Что реально приходит в ответе (не больше и не меньше):
                    country[]: id, name, alpha3 → alpha3 и есть countryCode;
                    period[]: id, name ("1 month") → КОДА периода здесь НЕТ;
                    mobile country[].operators.{dedicated,shared}[]: id, name,
                        rotations[{id, name}] → отдельного operatorCode нет, передавайте
                        полученный id как есть (сервер резолвит и ObjectId, и tag);
                    mix/mix_isp country[]: id, name, alpha3 (null), tag → tag и есть mixCode;
                    mix/mix_isp quantities[]: id, name, quantities — без tag;
                    resident tarifs[]: id, name, personal → КОДА тарифа здесь НЕТ.
        """
        if type is None:
            return self.request('GET', 'reference/list')
        return self.request('GET', 'reference/list/' + str(type))

    def prepare(self, **kwargs):
        return self.filterNone(dict(kwargs))

    def paymentOptions(self):
        if self.getPaymentCode():
            return {'paymentCode': self.getPaymentCode()}
        return {'paymentId': self.getPaymentId()}

    def mergeOrderOptions(self, payload, options=None):
        """
        Merge v2 identifiers/codes while avoiding conflicting id/code pairs.

        Отдельные *Code-поля нужны только для совместимости: у каждого *Id есть серверный
        фолбэк (normalizeOrderReferenceCodes) — если значение не является валидным id, а
        соответствующий *Code пуст, значение резолвится КАК КОД. Поэтому код достаточно
        передать прямо в countryId / periodId / operatorId / mixId / tarifId / paymentId,
        цепочки None и словарь options ради кодов не нужны.

        rotationCode фолбэка не имеет вообще: он лишь проверяется на целое число и
        копируется в rotationId, так что пользуйтесь сразу rotationId (МИНУТЫ).
        """
        values = options or {}
        if not isinstance(values, dict):
            raise TypeError('order options must be a dict')
        allowed = (
            'countryId', 'countryCode', 'periodId', 'periodCode', 'paymentId', 'paymentCode',
            'mixId', 'mixCode', 'uptime', 'protocol', 'mobileServiceType', 'operatorId',
            'operatorCode', 'rotationId', 'rotationCode', 'tarifId', 'tarifCode',
            'authorization', 'coupon', 'customTargetName', 'quantity')
        payload.update({key: values[key] for key in allowed if key in values})
        for id_key, code_key in (
                ('countryId', 'countryCode'), ('periodId', 'periodCode'),
                ('paymentId', 'paymentCode'), ('mixId', 'mixCode'),
                ('operatorId', 'operatorCode'), ('rotationId', 'rotationCode'),
                ('tarifId', 'tarifCode')):
            if code_key in values and values[code_key] is not None:
                payload.pop(id_key, None)
            elif id_key in values and values[id_key] is not None:
                payload.pop(code_key, None)
        return self.filterNone(payload)

    @staticmethod
    def _order_options(options, extra):
        if options is None:
            options = {}
        if not isinstance(options, dict):
            raise TypeError('options must be a dict')
        return {**options, **extra}

    def prepareRegular(self, sectionCode, countryId=None, periodId=None, quantity=None,
                       authorization=None, coupon=None, customTargetName=None, options=None):
        if isinstance(countryId, dict):
            return self.mergeOrderOptions(
                {**self.paymentOptions(), 'sectionCode': sectionCode},
                {**countryId, **(options or {})})
        return self.mergeOrderOptions({
            **self.paymentOptions(), 'sectionCode': sectionCode, 'countryId': countryId,
            'periodId': periodId, 'quantity': quantity, 'authorization': authorization,
            'coupon': coupon, 'customTargetName': customTargetName}, options)

    def prepareMix(self, mixId=None, periodId=None, quantity=None, authorization=None,
                   coupon=None, customTargetName=None, options=None):
        if isinstance(mixId, dict):
            return self.mergeOrderOptions(
                {**self.paymentOptions(), 'sectionCode': 'mix'},
                {**mixId, **(options or {})})
        return self.mergeOrderOptions({
            **self.paymentOptions(), 'sectionCode': 'mix', 'mixId': mixId,
            'periodId': periodId, 'quantity': quantity, 'authorization': authorization,
            'coupon': coupon, 'customTargetName': customTargetName}, options)

    def prepareIpv6(self, countryId=None, periodId=None, quantity=None, authorization=None,
                    coupon=None, customTargetName=None, protocol=None, options=None):
        if isinstance(countryId, dict):
            return self.mergeOrderOptions(
                {**self.paymentOptions(), 'sectionCode': 'ipv6'},
                {**countryId, **(options or {})})
        return self.mergeOrderOptions({
            **self.paymentOptions(), 'sectionCode': 'ipv6', 'countryId': countryId,
            'periodId': periodId, 'quantity': quantity, 'authorization': authorization,
            'coupon': coupon, 'customTargetName': customTargetName, 'protocol': protocol}, options)

    def prepareMobile(self, countryId=None, periodId=None, quantity=None, authorization=None,
                      coupon=None, operatorId=None, rotationId=None,
                      mobileServiceType='dedicated', options=None):
        """
        Собрать payload мобильного заказа. countryId / periodId / operatorId принимают
        ObjectId ЛИБО код, rotationId — ЧИСЛО МИНУТ (0 = "By Link"), кодов у него нет.
        """
        if isinstance(countryId, dict):
            return self.mergeOrderOptions({
                **self.paymentOptions(), 'sectionCode': 'mobile',
                'mobileServiceType': 'dedicated'},
                {**countryId, **(options or {})})
        if isinstance(mobileServiceType, dict):
            options = mobileServiceType
            mobileServiceType = 'dedicated'
        return self.mergeOrderOptions({
            **self.paymentOptions(), 'sectionCode': 'mobile', 'countryId': countryId,
            'periodId': periodId, 'quantity': quantity, 'authorization': authorization,
            'coupon': coupon, 'operatorId': operatorId, 'rotationId': rotationId,
            'mobileServiceType': mobileServiceType}, options)

    def prepareResident(self, tarifId=None, coupon=None, options=None):
        if isinstance(tarifId, dict):
            return self.mergeOrderOptions(
                {**self.paymentOptions(), 'sectionCode': 'resident'},
                {**tarifId, **(options or {})})
        return self.mergeOrderOptions({
            **self.paymentOptions(), 'sectionCode': 'resident',
            'tarifId': tarifId, 'coupon': coupon}, options)

    def withGenerateAuth(self, data):
        """generateAuth is accepted by order/make only, order/calc silently drops it."""
        return {**data, 'generateAuth': self.getGenerateAuth()}

    @staticmethod
    def _assert_target_name(data):
        """
        Повторяет проверку цели из client-api v1: для ipv4/ipv6/isp заказ без цели не
        принимается. В v1 цель задавалась targetId+targetSectionId либо своим текстом,
        в v2 остался только customTargetName. Для mix проверка не нужна, если передан
        mixId/mixCode — иначе сервер резолвит тип в ipv4, и цель снова обязательна.

        Проверяем локально, чтобы не платить сетевым запросом за "Incorrect goal" (код 14).

        Raises:
            ValueError: если цель не задана.
        """
        data = data or {}
        section = data.get('sectionCode')
        if section not in ('ipv4', 'ipv6', 'isp', 'mix', 'mix_isp'):
            return

        def filled(key):
            value = data.get(key)
            return value is not None and str(value).strip() != ''

        # Повторяет ClientApiService.parseMixSelection: сервер распознаёт mix не только по
        # mixId/mixCode, но и через countryId — строкой "packageId:quantity" либо
        # countryId=packageId вместе с quantity. Если mix распознан, цель не требуется.
        # Раньше проверялись только mixId/mixCode, и mix через countryId блокировался локально.
        if section in ('mix', 'mix_isp'):
            if filled('mixId') or filled('mixCode'):
                return
            country_id = data.get('countryId')
            country_id = '' if country_id is None else str(country_id).strip()
            if country_id:
                if ':' in country_id:
                    return
                try:
                    if int(str(data.get('quantity')).strip()) > 0:
                        return
                except (TypeError, ValueError):
                    pass
        if filled('customTargetName'):
            return
        raise ValueError(
            'customTargetName is required for {} orders '
            '(client api returns "Incorrect goal", code 14)'.format(section))

    def orderCalc(self, data):
        """
        Calculate the order.

        Args:
            data (dict): Free format dictionary to send into endpoint. ``sectionCode``:
                ipv4 | ipv6 | mobile | isp | mix | mix_isp | resident | scraper (значение
                нормализуется: регистр и '-'/' ' -> '_').

                countryId / periodId / operatorId / mixId / tarifId / paymentId — ObjectId-
                строка ЛИБО код: сервер резолвит код прямо в этих полях, отдельные *Code
                передавать не обязательно.

                rotationId — ЧИСЛО МИНУТ (0 = "By Link"), ни ObjectId, ни кода у него нет.

        Returns:
            dict: The response from the endpoint.
        """
        self._assert_target_name(data)
        return self.request('POST', 'order/calc', json=data)

    def orderMake(self, data):
        """
        Create an order.

        Args:
            data (dict): Free format dictionary to send into endpoint. ``sectionCode``:
                ipv4 | ipv6 | mobile | isp | mix | mix_isp | resident | scraper.
                Идентификаторы — ObjectId-СТРОКИ (в том числе orderId в ответе), и в каждом
                *Id вместо ObjectId принимается код (см. orderCalc). rotationId — МИНУТЫ.

        Returns:
            dict: The response from the endpoint.
        """
        self._assert_target_name(data)
        return self.request('POST', 'order/make', json=data)

    def orderCalcIpv4(self, countryId=None, periodId=None, quantity=None, authorization=None,
                      coupon=None, customTargetName=None, options=None, **order_options):
        """
        Calculate IPv4.

        Args:
            countryId (str): ObjectId страны ЛИБО её код — alpha3 из reference/list
                (country[].alpha3, например 'USA'; сервер приводит к верхнему регистру).
            periodId (str): ObjectId периода ЛИБО код периода (например '1m'; сервер
                приводит к нижнему регистру). Кода периода в reference/list нет — там только
                id и name, так что либо берите id, либо используйте известный вам код.
            quantity (int): Количество прокси.
            authorization (str): Необязательно.
            coupon (str): Необязательно.
            customTargetName (str): Цель заказа. ОБЯЗАТЕЛЬНА для ipv4 (иначе локальный
                ValueError вместо серверного "Incorrect goal", code 14).
            options (dict): Остальные поля payload (uptime, paymentId, …). То же самое можно
                передать именованными аргументами. Для передачи кодов options НЕ нужен —
                коды принимаются прямо в countryId/periodId.

        Returns:
            dict: The response from the endpoint.
        """
        return self.orderCalc(self.prepareRegular(
            'ipv4', countryId, periodId, quantity, authorization, coupon, customTargetName,
            self._order_options(options, order_options)))

    def orderCalcIsp(self, countryId=None, periodId=None, quantity=None, authorization=None,
                     coupon=None, customTargetName=None, options=None, **order_options):
        """
        Calculate ISP. Аргументы как у orderCalcIpv4(): countryId и periodId принимают
        ObjectId ЛИБО код, customTargetName обязателен.
        """
        return self.orderCalc(self.prepareRegular(
            'isp', countryId, periodId, quantity, authorization, coupon, customTargetName,
            self._order_options(options, order_options)))

    def orderCalcMix(self, mixId=None, periodId=None, quantity=None, authorization=None,
                     coupon=None, customTargetName=None, options=None, **order_options):
        """
        Calculate MIX. The first argument is a MIX package, not a country.

        Args:
            mixId (str): ObjectId mix-пакета ЛИБО его tag (точное совпадение). tag есть в
                reference/list: mix/mix_isp -> country[].tag (например
                'usa-europe-mix_IPv4'); в quantities[] его нет.
            periodId (str): ObjectId периода ЛИБО код периода ('1m').
            quantity (int): Количество прокси в пакете (из quantities[].quantities).
            customTargetName (str): Для mix не требуется, если пакет распознан
                (mixId/mixCode либо countryId='PACKAGE_ID:QUANTITY').

        Returns:
            dict: The response from the endpoint.
        """
        return self.orderCalc(self.prepareMix(
            mixId, periodId, quantity, authorization, coupon, customTargetName,
            self._order_options(options, order_options)))

    def orderCalcMixByCode(self, mixCode, periodCode, quantity, **options):
        """
        Псевдоним orderCalcMix() через явные *Code-поля. Те же значения принимаются
        позиционно: orderCalcMix(mixCode, periodCode, quantity).
        """
        return self.orderCalcMix(
            mixCode=mixCode, periodCode=periodCode, quantity=quantity, **options)

    def orderCalcIpv6(self, countryId=None, periodId=None, quantity=None, authorization=None,
                      coupon=None, customTargetName=None, protocol=None, options=None, **order_options):
        """
        Calculate the order IPv6.

        Args:
            countryId (str): ObjectId страны ЛИБО alpha3-код ('USA').
            periodId (str): ObjectId периода ЛИБО код периода ('1m').
            customTargetName (str): ОБЯЗАТЕЛЕН для ipv6.
            protocol (str): http | https | socks | socks5 (регистр не важен); прочие значения
                сервер отвергает ошибкой "Incorrect protocol".

        Returns:
            dict: The response from the endpoint.
        """
        return self.orderCalc(self.prepareIpv6(
            countryId, periodId, quantity, authorization, coupon, customTargetName, protocol,
            self._order_options(options, order_options)))

    def orderCalcMobile(self, countryId=None, periodId=None, quantity=None, authorization=None,
                        coupon=None, operatorId=None, rotationId=None,
                        mobileServiceType='dedicated', options=None, **order_options):
        """
        Calculate mobile.

        Args:
            countryId (str): ObjectId страны ЛИБО её alpha3-код ('USA').
            periodId (str): ObjectId периода ЛИБО код периода ('1m').
            quantity (int): Количество прокси.
            authorization (str): Необязательно.
            coupon (str): Необязательно.
            operatorId (str): ObjectId мобильного оператора ЛИБО его tag (используется КАК
                ЕСТЬ, регистр важен). Берите значение из reference/list
                country[].operators.dedicated[].id (или .shared[].id) и передавайте как есть —
                сервер резолвит и ObjectId, и tag; отдельного operatorCode в справочнике нет.
            rotationId (int): Интервал ротации в МИНУТАХ, 0 = "By Link". Ни ObjectId, ни кода
                у поля нет: '5m'/'10m' сервер отвергает ("Set existed [rotationCode] from
                reference"). Допустимые значения — rotations[].id из reference/list, это уже
                минуты.
            mobileServiceType (str): shared | dedicated (по умолчанию dedicated).

        Returns:
            dict: The response from the endpoint.
        """
        return self.orderCalc(self.prepareMobile(
            countryId, periodId, quantity, authorization, coupon, operatorId, rotationId,
            mobileServiceType, self._order_options(options, order_options)))

    def orderCalcResident(self, tarifId=None, coupon=None, options=None, **order_options):
        """
        Calculate the order Resident.

        Args:
            tarifId (str): ObjectId резидентского тарифа ЛИБО его code (точное совпадение).
                В reference/list у tarifs[] есть только id, name и personal — кода тарифа
                там нет, так что берите id.
            coupon (str): Необязательно.

        Returns:
            dict: The response from the endpoint.
        """
        return self.orderCalc(self.prepareResident(
            tarifId, coupon, self._order_options(options, order_options)))

    def orderMakeIpv4(self, countryId=None, periodId=None, quantity=None, authorization=None,
                      coupon=None, customTargetName=None, options=None, **order_options):
        """
        Create an order IPv4. Attention! Deducts money from the balance.

        Аргументы как у orderCalcIpv4(): countryId и periodId принимают ObjectId ЛИБО код,
        customTargetName обязателен.
        """
        return self.orderMake(self.withGenerateAuth(self.prepareRegular(
            'ipv4', countryId, periodId, quantity, authorization, coupon, customTargetName,
            self._order_options(options, order_options))))

    def orderMakeIsp(self, countryId=None, periodId=None, quantity=None, authorization=None,
                     coupon=None, customTargetName=None, options=None, **order_options):
        """
        Create an order ISP. Attention! Deducts money from the balance.

        Аргументы как у orderCalcIpv4(): countryId и periodId принимают ObjectId ЛИБО код,
        customTargetName обязателен.
        """
        return self.orderMake(self.withGenerateAuth(self.prepareRegular(
            'isp', countryId, periodId, quantity, authorization, coupon, customTargetName,
            self._order_options(options, order_options))))

    def orderMakeMix(self, mixId=None, periodId=None, quantity=None, authorization=None,
                     coupon=None, customTargetName=None, options=None, **order_options):
        """
        Create an order MIX. Attention! Deducts money from the balance.

        Аргументы как у orderCalcMix(): mixId — ObjectId пакета ЛИБО его tag,
        periodId — ObjectId периода ЛИБО код периода.
        """
        return self.orderMake(self.withGenerateAuth(self.prepareMix(
            mixId, periodId, quantity, authorization, coupon, customTargetName,
            self._order_options(options, order_options))))

    def orderMakeMixByCode(self, mixCode, periodCode, quantity, **options):
        """
        Псевдоним orderMakeMix() через явные *Code-поля. Те же значения принимаются
        позиционно: orderMakeMix(mixCode, periodCode, quantity).
        """
        return self.orderMakeMix(
            mixCode=mixCode, periodCode=periodCode, quantity=quantity, **options)

    def orderMakeIpv6(self, countryId=None, periodId=None, quantity=None, authorization=None,
                      coupon=None, customTargetName=None, protocol=None, options=None, **order_options):
        """
        Create an order IPv6. Attention! Deducts money from the balance.

        Аргументы как у orderCalcIpv6(): countryId и periodId принимают ObjectId ЛИБО код,
        customTargetName обязателен.
        """
        return self.orderMake(self.withGenerateAuth(self.prepareIpv6(
            countryId, periodId, quantity, authorization, coupon, customTargetName, protocol,
            self._order_options(options, order_options))))

    def orderMakeMobile(self, countryId=None, periodId=None, quantity=None, authorization=None,
                        coupon=None, operatorId=None, rotationId=None,
                        mobileServiceType='dedicated', options=None, **order_options):
        """
        Create a mobile order. Attention! Deducts money from the balance.

        Аргументы как у orderCalcMobile(): countryId / periodId / operatorId принимают
        ObjectId ЛИБО код, rotationId — ЧИСЛО МИНУТ (0 = "By Link"), кодов у него нет.
        """
        return self.orderMake(self.withGenerateAuth(self.prepareMobile(
            countryId, periodId, quantity, authorization, coupon, operatorId, rotationId,
            mobileServiceType, self._order_options(options, order_options))))

    def orderMakeResident(self, tarifId=None, coupon=None, options=None, **order_options):
        """
        Create an order Resident. Attention! Deducts money from the balance.

        tarifId — ObjectId резидентского тарифа ЛИБО его code.
        """
        return self.orderMake(self.prepareResident(
            tarifId, coupon, self._order_options(options, order_options)))

    # --------------------------- Prolong ---------------------------

    def prepareProlong(self, ids=None, periodId=None, coupon='', options=None):
        if isinstance(ids, dict):
            payload = self.paymentOptions()
            values = {**ids, **(options or {})}
        else:
            payload = {
                **self.paymentOptions(), 'ids': ids,
                'periodId': periodId, 'coupon': coupon}
            values = options or {}
        if not isinstance(values, dict):
            raise TypeError('prolong options must be a dict')
        for key in (
                'ids', 'orderSeparatorIds', 'orderSeparatorId', 'coupon',
                'periodId', 'periodCode', 'paymentId', 'paymentCode'):
            if key in values:
                payload[key] = values[key]
        for id_key, code_key in (('periodId', 'periodCode'), ('paymentId', 'paymentCode')):
            if code_key in values and values[code_key] is not None:
                payload.pop(id_key, None)
            elif id_key in values and values[id_key] is not None:
                payload.pop(code_key, None)
        return self.filterNone(payload)

    def prolongCalc(self, type, ids=None, periodId=None, coupon='', options=None, **prolong_options):
        """
        Calculate the renewal.

        Args:
            type (str): The type of the order - ipv4, ipv6, mobile, isp, mix or mix_isp.
            ids (list): A list of identifiers proxy (ObjectId-СТРОКИ).
            periodId (str): ObjectId периода ЛИБО код периода ('1m') — у prolong тот же
                серверный фолбэк, что у order (normalizeProlongReferenceCodes), поэтому
                periodCode передавать не обязательно.
            coupon (str): Coupon code.
            options: paymentId (ObjectId ЛИБО код платёжной системы, например 'balance'),
                orderSeparatorId / orderSeparatorIds, ids.

        Returns:
            dict: The response from the endpoint.
        """
        values = self._order_options(options, prolong_options)
        return self.request('POST', 'prolong/calc/' + type,
                            json=self.prepareProlong(ids, periodId, coupon, values))

    def prolongMake(self, type, ids=None, periodId=None, coupon='', options=None, **prolong_options):
        """
        Create a renewal order. Attention! Deducts money from the balance.

        Args:
            type (str): The type of the order - ipv4, ipv6, mobile, isp, mix or mix_isp.
            ids (list): A list of identifiers proxy (ObjectId-СТРОКИ).
            periodId (str): ObjectId периода ЛИБО код периода ('1m'), см. prolongCalc().
            coupon (str): Coupon code.
            options: paymentId (ObjectId ЛИБО код платёжной системы), orderSeparatorId /
                orderSeparatorIds, ids.

        Returns:
            dict: {'orderId': ObjectId-строка, 'total', 'balance', 'listBaseOrderNumbers'}.

        Raises:
            ApiError: при нехватке средств — продление НЕ состоялось.
        """
        values = self._order_options(options, prolong_options)
        return self._assert_prolong_made(
            self.request('POST', 'prolong/make/' + type,
                         json=self.prepareProlong(ids, periodId, coupon, values)))

    @staticmethod
    def _assert_prolong_made(data):
        """
        При нехватке средств prolong/make отдаёт конверт status="error" с ПУСТЫМ errors[] и
        calc-данными в data (ProlongMakeResponseClientDto.ofInsufficientFunds,
        ClientApiService.groovy:3185) — ровно ту же форму, что легитимный warning у
        prolong/calc. Из-за этого общий разбор конверта возвращал данные как успех, и
        несостоявшееся продление выглядело как состоявшееся. Успех — непустой orderId.

        order/make этим не страдает: у OrderMakeResponseClientDto только ofSuccess/ofError,
        и при ошибке errors[] всегда заполнен.
        """
        if not isinstance(data, dict):
            return data
        order_id = data.get('orderId')
        if order_id is not None and str(order_id).strip() != '':
            return data
        warning = data.get('warning')
        warning = str(warning).strip() if warning is not None else ''
        raise ApiError(
            warning or 'prolong/make did not create an order (insufficient funds)',
            http_status=200,
            body=data)

    # --------------------------- Proxy ---------------------------

    def proxyList(self, type=None, **filters):
        """
        List of proxies.

        Args:
            type (str): ipv4 | ipv6 | mobile | isp | mix | mix_isp | resident | None.
                Без типа ответ — словарь с ключами ipv4, ipv6, mobile, isp, mix, mix_isp,
                resident.
            filters: latest ("Y" — последний заказ), orderId (ObjectId-СТРОКА, не число),
                country (код страны), ends, page, per_page (пагинация работает только для
                запроса с типом).

        Returns:
            dict: The list of proxies.
        """
        params = self.filterNone(filters)
        if type is None:
            return self.request('GET', 'proxy/list', params=params)
        return self.request('GET', 'proxy/list/' + str(type), params=params)

    def proxyDownload(self, type, ext=None, proto=None, listId=None, **filters):
        """
        Export a proxy of a certain type in txt or csv format.

        Отдаётся ФАЙЛОМ (Content-Disposition: attachment), а не конвертом
        {status, data, errors}: text/plain для txt и кастомного шаблона, text/csv для csv.

        Args:
            type (str): ipv4 | ipv6 | mobile | isp | mix | mix_isp | subresident
            ext (str): txt | csv | кастомный шаблон строки с плейсхолдерами
                %ip% %port% %login% %user% %password% %protocol% %rotation_link%
                (по умолчанию txt — '%login%:%password%@%ip%:%port%',
                csv — '%ip%;%port%;%login%;%password%'). Ограничения см. assertExt().
            proto (str): https | socks5 | None (иные значения сервер игнорирует)
            listId (str): only for resident-family types
            filters: package_key (ТОЛЬКО subresident), country (код страны), ends

        Note:
            Для резидентских листов основного пакета используйте proxyDownloadResident().
            package_key поддерживается сервером ТОЛЬКО на маршруте subresident: путь
            proxy/download/resident его игнорирует и молча отдаёт выгрузку родительского
            пакета, поэтому такая комбинация запрещена здесь явно.

        Returns:
            str: The exported proxies.
        """
        if type == 'resident' and filters.get('package_key'):
            raise ValueError(
                "proxy/download/resident ignores package_key and returns the parent package. "
                "For a subpackage use proxyDownload('subresident', package_key=...); "
                "for the main resident package use proxyDownloadResident().")
        params = {'ext': self.assertExt(ext), 'proto': proto, 'listId': listId}
        params.update(filters)
        return self.request('GET', 'proxy/download/' + type, params=self.filterNone(params))

    def proxyDownloadResident(self, id=None, ext=None, maxLine=None):
        """
        Export the resident proxy list (файл, attachment).

        Литеральный маршрут proxy/download/resident обслуживает ResidentUserController, он
        специфичнее шаблона {type} и перехватывает запрос на себя. Имя параметра листа там —
        listId, ``id`` оставлен алиасом, поэтому отправляем именно его.

        Args:
            id (str): List id. Идентификаторы резидентских листов ЧИСЛОВЫЕ (Long),
                это не ObjectId.
            ext (str): txt | csv | кастомный шаблон (см. proxyDownload)
            maxLine (int): Maximum number of lines.

        Returns:
            str: The exported proxies.
        """
        return self.request('GET', 'proxy/download/resident', params=self.filterNone(
            {'id': id, 'ext': self.assertExt(ext), 'maxLine': maxLine}))

    #: Допустимые значения type у proxy/replace — ПРИЧИНА замены (enum ProxyReplaceType).
    PROXY_REPLACE_TYPES = (
        'NOT_WORK', 'INCORRECT_LOCATION', 'CANT_CHANGE_NETWORK', 'LOW_SPEED', 'CUSTOM')

    def proxyReplace(self, ids, type=None, comment=None, reason=None):
        """
        Replace proxy IPs.

        Args:
            ids (list): Ids of the IP addresses to replace (ObjectId-строки). Одиночный id
                тоже принимается.
            type (str): ПРИЧИНА замены, а не тип прокси. Enum ProxyReplaceType:
                NOT_WORK | INCORRECT_LOCATION | CANT_CHANGE_NETWORK | LOW_SPEED | CUSTOM.
                Сервер сам приводит значение к верхнему регистру. Обязателен: без него
                ответ — "Set coorect type: ..." (опечатка серверная).
            comment (str): Свой текст причины. ОБЯЗАТЕЛЕН и не может быть пустым при
                type=CUSTOM (иначе сервер отвечает "Set comment", code 503); для остальных
                значений необязателен — комментарий подставляется по причине
                ("Does not work", "Incorrect location", "I want to change the network",
                "Low speed").
            reason (str): Алиас для ``type`` — по смыслу поле является причиной. На провод
                уходит ключ ``type``.

        Raises:
            ValueError: если причина не задана, не входит в enum, или при CUSTOM не передан
                непустой comment. Проверяем локально, чтобы не платить сетевым запросом.

        Returns:
            dict: The response from the endpoint.
        """
        replaceType = type if type is not None else reason
        if type is not None and reason is not None and str(type) != str(reason):
            raise ValueError("pass either type or reason, not both with different values")
        normalized = '' if replaceType is None else str(replaceType).strip().upper()
        if not normalized:
            raise ValueError(
                "type is required and must be a replacement reason: {}".format(
                    ' | '.join(self.PROXY_REPLACE_TYPES)))
        if normalized not in self.PROXY_REPLACE_TYPES:
            raise ValueError(
                "type must be a replacement reason ({}), got {!r}. It is not a proxy type.".format(
                    ' | '.join(self.PROXY_REPLACE_TYPES), replaceType))
        if normalized == 'CUSTOM' and not (comment or '').strip():
            raise ValueError("comment is required and must not be empty when type=CUSTOM")
        return self.request('POST', 'proxy/replace',
                            json={'ids': ids, 'type': normalized, 'comment': comment})

    def proxyCommentSet(self, ids, comment=None):
        """
        Set a comment for a proxy.

        Args:
            ids (list): Any id (ObjectId-СТРОКИ), regardless of the type of proxy.
            comment (str): The comment.

        Returns:
            int: The number of proxies updated.
        """
        return self.request('POST', 'proxy/comment/set', json={'ids': ids, 'comment': comment})['updated']

    # --------------------------- Resident ---------------------------

    def residentPackage(self):
        """
        Package Information. Remaining traffic, end date.

        Returns:
            dict: packageKey, isActive, tarifId, isLinkDate, trafficLimit, trafficUsage,
                trafficLeft, trafficLimitSub, trafficUsageSub, trafficLeftSub, autoRenew,
                rotation и ``expiredAt`` — СТРОКА в формате "dd.MM.yyyy HH:mm:ss".
                (Не путать с субпакетным expired_at: там объект PHP-даты.)
        """
        return self.request('GET', 'resident/package')

    def residentConsumption(self, filter=None):
        """
        Traffic consumption of the resident package.

        Args:
            filter (dict): login, date_start, date_end. Пакет берётся свой, по apiKey —
                package_key здесь не передаётся.

        Returns:
            dict: Consumption information.
        """
        return self.request('POST', 'resident/consumption', json=filter or {})

    def residentTrafficDetails(self, filter=None):
        """
        Detailed traffic statistics of the resident package.

        Args:
            filter (dict): ключ пакета ОБЯЗАТЕЛЕН и называется ``packageKey`` либо ``key``
                (НЕ package_key — сервер читает только эти два имени, иначе ответ
                "key is required"). Дополнительно: login, date_start, date_end.

        Returns:
            dict: Traffic details.
        """
        return self.request('POST', 'resident/traffic/details', json=filter or {})

    def residentGeo(self):
        """
        Database of geo locations.

        Отдаётся ФАЙЛОМ geo.json: Content-Type application/json +
        Content-Disposition: attachment; filename="geo.json". Это НЕ zip-архив (README
        прошлых версий и других SDK врут про "zip ~300Kb / unzip ~3Mb").
        Структура — массив стран: country -> regions -> cities -> ISPs.

        Returns:
            bytes: Содержимое файла geo.json. Разбирать через json.loads().
        """
        return self.request('GET', 'resident/geo')

    def residentGeoIsp(self):
        """
        Database of ISP codes.

        Тоже файл: application/json + attachment; filename="isp.json".

        Returns:
            bytes: Содержимое файла isp.json. Разбирать через json.loads().
        """
        return self.request('GET', 'resident/geo/isp')

    def residentGeoCount(self):
        """
        Number of available IPs by geo.

        Returns:
            list: Geo counters.
        """
        return self.request('GET', 'resident/geo/count')

    def residentList(self):
        """
        List of existing ip list in a package.

        data приходит ПЛОСКИМ массивом (обёртки items здесь нет).

        Returns:
            list: Lists in the package. ``id`` листа — ЧИСЛО (Long), а не ObjectId-строка.
        """
        return self.request('GET', 'resident/lists')

    def residentListAdd(self, title, whitelist=None, country=None, region=None, city=None,
                        isp=None, rotation=None, export=None):
        """
        Create list in package.

        Первым позиционным аргументом можно передать dict целиком: title, whitelist, geo,
        export, rotation.

        Args:
            title (str): List title.
            whitelist (str): Comma separated ip list.
            country (str): Geo country (ISO-код, сервер приводит к верхнему регистру).
            region (str): Geo region.
            city (str): Geo city.
            isp (str): Geo isp.
            rotation (int): -1 sticky, 0 per request, 1-3600 seconds.
            export (dict): {'ports': int, 'ext': str}.

        Raises:
            ValueError: если в dict-форме ``geo`` не объект. Сервер ждёт объект GeoDto
                (country/region/city/isp); массив Jackson не свяжет. Пустой geo допустим —
                значит "без гео-фильтра". Раньше geo=[] падал AttributeError.

        Returns:
            dict: Created list model. Гео в ответе — объект, ``id`` — число.
        """
        if isinstance(title, dict):
            data = dict(title)
            if 'geo' in data:
                data['geo'] = self._normalize_geo(data['geo'])
        else:
            data = self.filterNone({
                'title': title, 'whitelist': whitelist,
                'rotation': rotation, 'export': export})
            data['geo'] = self.filterNone({
                'country': country, 'region': region, 'city': city, 'isp': isp})
        return self.request('POST', 'resident/list/add', json=data)

    def residentListRename(self, id, title):
        """
        Rename list in user package.

        Args:
            id (int): List ID — ЧИСЛОВОЙ (Long), это исключение из правила "id в v2 строки".
            title (str): Title list.

        Returns:
            dict: Updated list model.
        """
        return self.request('POST', 'resident/list/rename', json={'id': id, 'title': title})

    def residentListRotation(self, id, rotation):
        """
        Change the rotation interval of a list.

        Args:
            id (int): List ID (числовой).
            rotation (int): -1 sticky, 0 per request, 1-3600 seconds. За пределами
                [-1, 3600] сервер отвечает ошибкой валидации.

        Returns:
            dict: Updated list model.
        """
        return self.request('POST', 'resident/list/rotation', json={'id': id, 'rotation': rotation})

    def residentListTools(self):
        """
        Create the tools list for the package.

        Returns:
            dict: Created list model.
        """
        return self.request('PUT', 'resident/list/tools')

    def residentListDelete(self, id):
        """
        Remove list from user package.

        Args:
            id (int): List ID (числовой, обязателен).

        Returns:
            dict: {'status': 'delete'} — сервер отдаёт data строкой "delete", здесь она
                нормализуется в dict.
        """
        return self._delete_result(self.request('DELETE', 'resident/list/delete', json={'id': id}))

    # --------------------------- Resident subpackages ---------------------------

    def residentSubUserCreate(self, is_link_date=None, rotation=None, traffic_limit=None, expired_at=None):
        """
        Create a resident subpackage.

        Первым позиционным аргументом можно передать dict целиком.

        Args:
            is_link_date (bool): привязать дату окончания к родительскому пакету.
            rotation (int): -1 sticky, 0 per request, 1-3600 seconds.
            traffic_limit (str): байты, ОБЯЗАТЕЛЕН.
            expired_at (str): дата окончания СТРОКОЙ (в запросе — строка, в ответе объект).

        Returns:
            dict: Created subpackage: package_key, rotation, traffic_limit, traffic_usage,
                traffic_left, traffic_limit_sub, traffic_usage_sub, traffic_left_sub,
                is_link_date, is_active и ``expired_at`` — ОБЪЕКТ PHP-даты
                {'date': '2026-12-31 00:00:00.000000', 'timezone_type': 3,
                'timezone': 'UTC'}, а не строка.
        """
        if isinstance(is_link_date, dict):
            data = self.filterNone(dict(is_link_date))
        else:
            data = self.filterNone({
                'is_link_date': is_link_date, 'rotation': rotation,
                'traffic_limit': traffic_limit, 'expired_at': expired_at})
        if data.get('traffic_limit') is None:
            raise ValueError('traffic_limit is required')
        return self.request('POST', 'residentsubuser/create', json=data)

    def residentSubUserUpdate(self, package_key, is_link_date=None, rotation=None,
                              traffic_limit=None, is_active=None, expired_at=None):
        """
        Update a resident subpackage.

        Первым позиционным аргументом можно передать dict целиком.

        Args:
            package_key (str): ключ субпакета.
            is_link_date (bool): привязать дату окончания к родительскому пакету.
            rotation (int): -1 sticky, 0 per request, 1-3600 seconds.
            traffic_limit (str): байты.
            is_active (bool): статус субпакета.
            expired_at (str): дата окончания СТРОКОЙ.

        Returns:
            dict: Updated subpackage; ``expired_at`` в ответе — ОБЪЕКТ
                {'date', 'timezone_type', 'timezone'} (см. residentSubUserCreate).
        """
        if isinstance(package_key, dict):
            data = self.filterNone(dict(package_key))
        else:
            data = self.filterNone({
                'package_key': package_key, 'is_link_date': is_link_date,
                'rotation': rotation, 'traffic_limit': traffic_limit,
                'is_active': is_active, 'expired_at': expired_at})
        return self.request('POST', 'residentsubuser/update', json=data)

    def residentSubUserDelete(self, package_key):
        """
        Delete a resident subpackage.

        Returns:
            dict: {'status': 'delete'} либо {'status': 'not-found'}.
        """
        return self._delete_result(self.request('DELETE', 'residentsubuser/delete', json={'package_key': package_key}))

    def residentSubUserPackages(self):
        """
        List of resident subpackages.

        Returns:
            list: Subpackages; у каждого ``expired_at`` — ОБЪЕКТ
                {'date', 'timezone_type', 'timezone'}, а не строка.
        """
        return self.request('GET', 'residentsubuser/packages')

    def residentSubUserLists(self, package_key=None):
        """
        List of existing ip lists in a subpackage.

        Args:
            package_key (str): ключ субпакета, обязателен (уходит query-параметром).

        Returns:
            list: Lists in the subpackage; ``id`` листа — число.
        """
        return self.request('GET', 'residentsubuser/lists',
                            params=self.filterNone({'package_key': package_key}))

    def residentSubUserListAdd(self, package_key, title=None, whitelist=None,
                               country=None, region=None, city=None, isp=None,
                               rotation=None, export=None, geo=None):
        """
        Create a list inside a subpackage.

        Args:
            package_key (str): ключ субпакета (либо dict со всем телом первым аргументом).
            title (str): List title.
            whitelist (str): Comma separated ip list.
            country/region/city/isp (str): гео по частям.
            geo (dict): гео целиком — ОБЪЕКТ {country, region, city, isp}; сервер объявляет
                поле @NotNull, поэтому пустой объект отправляется всегда.
            rotation (int): -1 sticky, 0 per request, 1-3600 seconds.
            export (dict): {'ports': int, 'ext': str}.

        Raises:
            ValueError: если geo передан не объектом (массив сервер не свяжет).

        Returns:
            dict: Created list model.
        """
        if isinstance(package_key, dict):
            data = dict(package_key)
            data['geo'] = self._normalize_geo(data.get('geo'))
        else:
            data = self.filterNone({
                'package_key': package_key, 'title': title, 'whitelist': whitelist,
                'rotation': rotation, 'export': export})
            data['geo'] = self._normalize_geo(geo) or self.filterNone({
                'country': country, 'region': region, 'city': city, 'isp': isp})
        return self.request('POST', 'residentsubuser/list/add', json=data)

    def residentSubUserListRename(self, package_key, id, title):
        """
        Rename a list inside a subpackage.

        Args:
            package_key (str): ключ субпакета.
            id (int): List ID — ЧИСЛОВОЙ (не ObjectId).
            title (str): новое имя.

        Returns:
            dict: Updated list model.
        """
        return self.request('POST', 'residentsubuser/list/rename',
                            json={'package_key': package_key, 'id': id, 'title': title})

    def residentSubUserListRotation(self, package_key, id, rotation):
        """
        Change the rotation interval of a list inside a subpackage.

        Args:
            package_key (str): ключ субпакета.
            id (int): List ID — ЧИСЛОВОЙ.
            rotation (int): -1 sticky, 0 per request, 1-3600 seconds.

        Returns:
            dict: Updated list model.
        """
        return self.request('POST', 'residentsubuser/list/rotation',
                            json={'package_key': package_key, 'id': id, 'rotation': rotation})

    def residentSubUserListTools(self, package_key):
        """
        Create the tools list inside a subpackage.

        Returns:
            dict: Created list model.
        """
        return self.request('PUT', 'residentsubuser/list/tools', json={'package_key': package_key})

    def residentSubUserListDelete(self, package_key, id):
        """
        Delete a list inside a subpackage.

        Args:
            package_key (str): ключ субпакета.
            id (int): List ID — ЧИСЛОВОЙ.

        Returns:
            dict: {'status': 'delete'} либо {'status': 'not-found'} — сервер отдаёт not-found
                  внутри успешного конверта, проверяйте поле status.
        """
        return self._delete_result(self.request('DELETE', 'residentsubuser/list/delete',
                            json={'package_key': package_key, 'id': id}))
