import json
import math
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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
    (``Error api key`` / ``IP not allowed <ip>`` / ``Request limit reached``, все с code 503),
    поэтому по errors[0] нельзя отличить битый ключ от неразрешённого IP и от превышения
    лимита запросов.
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
        Сам API не отвечает HTTP 429: его лимит приходит той же тройкой с HTTP 200. HTTP 429
        бывает только от edge перед API (запрос до API не дошёл) — такие ответы SDK
        повторяет сам, см. RateLimiter; тройку он не повторяет никогда.
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


class RateLimiter:
    """
    Client-side request queue of one Api instance. Api builds it from the ``rate_limit``
    config option and sends every HTTP request through run(), so the client stays within the
    API limits without any throttling code on the caller's side (README, "Rate limits and the
    request queue").

    Правила, в скобках значения по умолчанию:

    1. Глобальное окно. Все запросы (read, write и money вместе) делят скользящее окно: не
       больше ``requests_per_minute`` [1000] стартов за любые 60 секунд. Это журнал стартов:
       когда в нём N записей, следующий запрос ждёт, пока старейшей не исполнится 60 секунд.
       Не token bucket — bucket пропускает всплески, при которых за 60 секунд уходит больше N.
    2. Полоса записи. write и money идут через ОДНУ очередь на клиента: в полёте не больше
       одного, следующий стартует только после завершения предыдущего и не раньше
       ``write_interval_ms`` [1000] после СТАРТА предыдущего write/money, а money — ещё и не
       раньше ``money_interval_ms`` [2000] после старта предыдущего money. Очередь FIFO.
       Чтения полосу не ждут — только окно.
    3. HTTP 429 — лимит edge перед API: запрос до API не дошёл, поэтому повтор безопасен даже
       для money. Ждём Retry-After (секунды либо HTTP-дата; заголовка нет или он не
       разбирается — 2 с; не дольше 60 с) и повторяем, не больше ``max_retries`` [3] раз;
       последний 429 разбирается как обычно и становится ApiError с http_status=429. Повтор
       write/money не уступает своё место в полосе (никто не проскакивает вперёд), и каждая
       попытка — новый старт в окне; интервалы полосы отсчитываются от старта последней
       попытки: предыдущие до API не дошли.
    4. Других автоматических повторов нет: ошибки конверта (code 57 "Prolong for this order
       is already in progress", тройка отказа в доступе с code 503) и транспортные ошибки
       уходят вызывающему как раньше.

    ``enabled=False`` возвращает прежнее поведение один в один: ни ожиданий, ни повторов.

    Состояние живёт в экземпляре (один Api — один ключ): другие экземпляры и процессы с тем
    же ключом о нём не знают. Потокобезопасен: ожидание блокирует вызывающий поток и идёт без
    busy-wait — очередь полосы ждёт на Condition, паузы по времени идут через sleep().
    ``clock`` и ``sleep`` подменяются в тестах (по умолчанию time.monotonic и time.sleep).
    """

    READ = 'read'
    WRITE = 'write'
    MONEY = 'money'
    CATEGORIES = (READ, WRITE, MONEY)

    #: Длина глобального окна, секунды.
    WINDOW_SECONDS = 60.0
    #: Пауза перед повтором 429, если Retry-After нет или он не разбирается, секунды.
    DEFAULT_RETRY_AFTER_SECONDS = 2.0
    #: Потолок паузы по Retry-After, секунды.
    MAX_RETRY_AFTER_SECONDS = 60.0

    #: Опции ключа ``rate_limit`` конфига Api.
    OPTIONS = ('enabled', 'requests_per_minute', 'write_interval_ms', 'money_interval_ms',
               'max_retries', 'clock', 'sleep')
    #: Те же опции в camelCase, как в остальных SDK, — алиасы.
    OPTION_ALIASES = {
        'requestsPerMinute': 'requests_per_minute', 'writeIntervalMs': 'write_interval_ms',
        'moneyIntervalMs': 'money_interval_ms', 'maxRetries': 'max_retries'}

    def __init__(self, enabled=True, requests_per_minute=1000, write_interval_ms=1000,
                 money_interval_ms=2000, max_retries=3, clock=None, sleep=None):
        if not isinstance(enabled, bool):
            raise ValueError(
                'rate_limit enabled must be True or False, got {!r}'.format(enabled))
        self.enabled = enabled
        self.requests_per_minute = self._option(
            'requests_per_minute', requests_per_minute, integer=True, minimum=1)
        self.write_interval_ms = self._option('write_interval_ms', write_interval_ms)
        self.money_interval_ms = self._option('money_interval_ms', money_interval_ms)
        self.max_retries = self._option('max_retries', max_retries, integer=True)
        for name, value in (('clock', clock), ('sleep', sleep)):
            if value is not None and not callable(value):
                raise TypeError('rate_limit {} must be callable'.format(name))
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._write_interval = self.write_interval_ms / 1000.0
        self._money_interval = self.money_interval_ms / 1000.0
        # Один замок на всё состояние; под ним никогда не спим и не отправляем.
        self._cond = threading.Condition(threading.Lock())
        self._starts = deque()          # старты в глобальном окне, по возрастанию
        self._lane = deque()            # очередь полосы записи; голова владеет полосой
        self._last_write_start = None   # старт последней попытки write/money
        self._last_money_start = None   # старт последней попытки money

    @staticmethod
    def _option(name, value, integer=False, minimum=0):
        kinds = (int,) if integer else (int, float)
        if (isinstance(value, bool) or not isinstance(value, kinds)
                or (isinstance(value, float) and not math.isfinite(value))
                or value < minimum):
            raise ValueError('rate_limit {} must be {} >= {}, got {!r}'.format(
                name, 'an integer' if integer else 'a number', minimum, value))
        return value

    @classmethod
    def _from_config(cls, value):
        """
        Очередь из опции ``rate_limit`` конфига Api: None или True — значения по умолчанию,
        False — выключена, dict — опции OPTIONS (camelCase из OPTION_ALIASES тоже годится).
        Неизвестная опция — ValueError, а не молча оставленное значение по умолчанию.
        """
        if value is None or value is True:
            return cls()
        if value is False:
            return cls(enabled=False)
        if not isinstance(value, dict):
            raise TypeError('rate_limit must be a dict of options or True/False, got {}'.format(
                type(value).__name__))
        options = {}
        for key, option in value.items():
            name = cls.OPTION_ALIASES.get(key, key)
            if name not in cls.OPTIONS:
                raise ValueError('unknown rate_limit option {!r}, expected one of: {}'.format(
                    key, ', '.join(cls.OPTIONS)))
            if name in options:
                alias = next(alias for alias, target in cls.OPTION_ALIASES.items()
                             if target == name)
                raise ValueError('pass either {} or {} in rate_limit, not both'.format(
                    name, alias))
            options[name] = option
        return cls(**options)

    def run(self, category, send):
        """
        Выполнить одну HTTP-отправку по правилам очереди.

        Args:
            category (str): READ | WRITE | MONEY — см. Api.requestCategory().
            send (callable): отправка без аргументов, возвращает ответ со ``status_code``.

        Returns:
            Ответ последней попытки: обычный — либо 429, если повторы исчерпаны.
        """
        if not self.enabled:
            return send()
        if category not in self.CATEGORIES:
            raise ValueError('unknown request category {!r}'.format(category))
        if category == self.READ:
            return self._send(category, send)
        token = self._enter_lane()
        try:
            self._wait_for_lane_interval(category == self.MONEY)
            return self._send(category, send)
        finally:
            self._leave_lane(token)

    def _send(self, category, send):
        """Попытки одного запроса: повторяется только HTTP 429, и не больше max_retries раз."""
        retries = 0
        while True:
            self._take_window_slot(category)
            response = send()
            if getattr(response, 'status_code', None) != 429 or retries >= self.max_retries:
                return response
            retries += 1
            delay = self._retry_after(response)
            self._discard(response)
            if delay > 0:
                self._sleep(delay)

    def _take_window_slot(self, category):
        """
        Дождаться места в глобальном окне и записать старт — атомарно, чтобы два потока не
        заняли одно место. Для write/money этот же момент — старт в полосе.
        """
        while True:
            with self._cond:
                now = self._clock()
                while self._starts and self._starts[0] + self.WINDOW_SECONDS <= now:
                    self._starts.popleft()
                if len(self._starts) < self.requests_per_minute:
                    self._starts.append(now)
                    if category != self.READ:
                        self._last_write_start = now
                        if category == self.MONEY:
                            self._last_money_start = now
                    return
                delay = self._starts[0] + self.WINDOW_SECONDS - now
            self._sleep(delay)

    def _wait_for_lane_interval(self, money):
        """Держатель полосы ждёт интервалы от стартов предыдущих write/money и money."""
        while True:
            with self._cond:
                now = self._clock()
                ready = now
                if self._last_write_start is not None:
                    ready = max(ready, self._last_write_start + self._write_interval)
                if money and self._last_money_start is not None:
                    ready = max(ready, self._last_money_start + self._money_interval)
                if ready <= now:
                    return
            self._sleep(ready - now)

    def _enter_lane(self):
        """Встать в очередь полосы и дождаться своей очереди (FIFO, без опроса)."""
        token = object()
        with self._cond:
            self._lane.append(token)
            try:
                while self._lane[0] is not token:
                    self._cond.wait()
            except BaseException:
                # Прерванное ожидание (KeyboardInterrupt и т.п.) не должно запереть полосу.
                self._remove_from_lane(token)
                raise
        return token

    def _leave_lane(self, token):
        with self._cond:
            self._remove_from_lane(token)

    def _remove_from_lane(self, token):
        """Вызывается под self._cond."""
        was_head = self._lane[0] is token
        self._lane.remove(token)
        if was_head:
            self._cond.notify_all()

    @classmethod
    def _retry_after(cls, response):
        """
        Пауза перед повтором 429 по Retry-After: целые секунды либо HTTP-дата. Дата
        отсчитывается от заголовка Date того же ответа (так не мешает расхождение часов
        клиента и сервера), без него — от текущего времени. Заголовка нет или он не
        разбирается — DEFAULT_RETRY_AFTER_SECONDS; результат ограничен [0,
        MAX_RETRY_AFTER_SECONDS].
        """
        value = cls._header(response, 'Retry-After')
        delay = None
        if value is not None:
            text = str(value).strip()
            if re.fullmatch(r'[0-9]+', text):
                try:
                    delay = float(int(text))
                except (ValueError, OverflowError):
                    # Число длиннее лимита int() — заведомо больше потолка.
                    delay = cls.MAX_RETRY_AFTER_SECONDS
            else:
                moment = cls._http_date(text)
                if moment is not None:
                    reference = cls._http_date(cls._header(response, 'Date'))
                    if reference is None:
                        reference = datetime.now(timezone.utc)
                    delay = (moment - reference).total_seconds()
        if delay is None:
            delay = cls.DEFAULT_RETRY_AFTER_SECONDS
        return min(max(delay, 0.0), cls.MAX_RETRY_AFTER_SECONDS)

    @staticmethod
    def _http_date(value):
        if value is None:
            return None
        try:
            moment = parsedate_to_datetime(str(value).strip())
        except Exception:
            # Python < 3.10 бросает TypeError, новее — ValueError; заголовок чужой, не падаем.
            return None
        if moment is None:
            return None
        if moment.tzinfo is None:
            # HTTP-дата всегда в GMT; asctime и "-0000" разбираются в наивное время.
            moment = moment.replace(tzinfo=timezone.utc)
        return moment

    @staticmethod
    def _header(response, name):
        headers = getattr(response, 'headers', None) or {}
        value = headers.get(name)
        if value is None:
            wanted = name.lower()
            for key, item in headers.items():
                if str(key).lower() == wanted:
                    return item
        return value

    @staticmethod
    def _discard(response):
        """Отброшенный 429 закрываем: при stream=True он иначе держит соединение пула."""
        close = getattr(response, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class Api:
    URL = 'https://proxy-seller.com/personal/api/v2/'

    #: Категория каждого запроса для очереди (RateLimiter) — по ПУТИ, а не по HTTP-методу:
    #: calc-ручки — POST, но только читают. {type} совпадает с любым одним сегментом пути.
    #: Всё, чего здесь нет (*/list, */get, все */calc, reference/*, proxy/download/*,
    #: resident/package|lists|geo*|consumption|traffic/details, residentsubuser/packages|lists,
    #: balance/payments/list, balance/autotopup/get, …), — read.
    REQUEST_CATEGORIES = {
        'order/make': RateLimiter.MONEY,
        'prolong/make/{type}': RateLimiter.MONEY,
        'balance/add': RateLimiter.MONEY,
        'autoprolong/enable/{type}': RateLimiter.WRITE,
        'autoprolong/disable/{type}': RateLimiter.WRITE,
        'auth/add': RateLimiter.WRITE,
        'auth/add/ip': RateLimiter.WRITE,
        'auth/change': RateLimiter.WRITE,
        'auth/delete': RateLimiter.WRITE,
        'proxy/replace': RateLimiter.WRITE,
        'proxy/comment/set': RateLimiter.WRITE,
        'balance/autotopup/set': RateLimiter.WRITE,
        # POST-алиас resident/list/add
        'resident/list': RateLimiter.WRITE,
        'resident/list/add': RateLimiter.WRITE,
        'resident/list/delete': RateLimiter.WRITE,
        'resident/list/rename': RateLimiter.WRITE,
        'resident/list/rotation': RateLimiter.WRITE,
        'resident/list/tools': RateLimiter.WRITE,
        'residentsubuser/create': RateLimiter.WRITE,
        'residentsubuser/update': RateLimiter.WRITE,
        'residentsubuser/delete': RateLimiter.WRITE,
        'residentsubuser/list/add': RateLimiter.WRITE,
        'residentsubuser/list/delete': RateLimiter.WRITE,
        'residentsubuser/list/rename': RateLimiter.WRITE,
        'residentsubuser/list/rotation': RateLimiter.WRITE,
        'residentsubuser/list/tools': RateLimiter.WRITE,
    }

    def __init__(self, config):
        """
        API key placed in https://proxy-seller.com/personal/api/.

        Args:
            config (dict): Configuration options: key (required), base_url, timeout, headers,
                request_options, session, fingerprint, rate_limit.

                rate_limit — очередь запросов, ВКЛЮЧЕНА по умолчанию (см. RateLimiter):
                dict с опциями enabled [True], requests_per_minute [1000],
                write_interval_ms [1000], money_interval_ms [2000], max_retries [3]
                (camelCase-написания тоже принимаются), либо False — выключить: прежнее
                поведение без ожиданий и повторов.

        Raises:
            ValueError: без key или при неверной опции rate_limit (неизвестное имя,
                недопустимое значение).
            TypeError: если rate_limit не dict и не True/False.
        """

        if 'key' not in config:
            raise ValueError(
                "Need key, placed in https://proxy-seller.com/personal/api/")
        if 'rate_limit' in config and 'rateLimit' in config:
            raise ValueError('pass either rate_limit or rateLimit, not both')
        # Очередь своя у каждого экземпляра: другие экземпляры с тем же ключом о ней не знают.
        self.rate_limiter = RateLimiter._from_config(
            config['rate_limit'] if 'rate_limit' in config else config.get('rateLimit'))
        api_root = config.get('base_url') or config.get('baseUrl') or config.get('baseURL') or self.URL
        self.base_uri = str(api_root).rstrip('/') + '/' + quote(str(config['key']), safe='') + '/'
        self.timeout = config.get('timeout', 30)
        self.headers = {'Content-Type': 'application/json', **config.get('headers', {})}
        self.request_options = dict(config.get('request_options', {}))
        self.session = config.get('session') or requests.Session()
        self.paymentId = None
        self.paymentCode = None
        self.generateAuth = 'N'
        # X-Fingerprint можно задать и прямо в headers — тогда он уйдёт со всеми запросами;
        # подхватываем это значение, чтобы getFingerprint() видел уже настроенный заголовок.
        self.fingerprint = config.get('fingerprint') or self.headers.get('X-Fingerprint')

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
        Payment system for order/*, prolong/* and balance/add.

        Заказы и продления оплачиваются только балансом или привязанной картой: здесь
        подходят 'balance' и 'paddle_subscription' — код (сервер, не найдя ObjectId,
        резолвит значение как код) либо ObjectId одной из этих двух систем; остальные
        платёжки order/* и prolong/* отвергают. Проще setPaymentCode('balance').

        balance/add, наоборот, принимает только настоящий ObjectId из balancePaymentsList()
        (самого баланса в этом списке нет), см. balanceAdd.
        """
        self.paymentId = id

    def getPaymentId(self):
        return self.paymentId

    def setPaymentCode(self, code):
        """
        Payment-system code for order/* and prolong/*: ``balance`` (the account balance) or
        ``paddle_subscription`` (the saved card) — other payment systems are rejected there.
        """
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

    def setFingerprint(self, fingerprint):
        """
        Значение заголовка X-Fingerprint для order/make.

        Заголовок НЕОБЯЗАТЕЛЕН: ни одна секция, включая resident и scraper, не отказывает в
        заказе без него. Если значение задано, SDK отправляет его с order/make, и сервер
        использует его для анти-фрода и affiliate-атрибуции; если нет — заголовок просто не
        уходит.

        Отправляете — пусть это будет СТАБИЛЬНЫЙ идентификатор установки: форма не
        проверяется (подойдёт любая строка), но значение, генерируемое заново на каждый
        процесс, для анти-фрода и атрибуции бесполезно. Поэтому SDK его не выдумывает —
        задайте свой и сохраните между запусками.
        """
        self.fingerprint = fingerprint

    def getFingerprint(self):
        return self.fingerprint

    def request(self, method, uri, **options):
        """
        Send a request to the server.

        Единственное место, где SDK отправляет HTTP: запрос проходит через очередь
        (self.rate_limiter) с категорией requestCategory(uri) — может подождать места в окне
        или в полосе записи, а HTTP 429 повторяется по Retry-After. Ожидание в очереди в
        timeout не входит.

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

        def send():
            try:
                return self.session.request(
                    method, self.base_uri + uri, headers=request_headers,
                    timeout=request_timeout, **request_options)
            except requests.RequestException as error:
                error_response = getattr(error, 'response', None)
                raise ApiError(
                    str(error), http_status=getattr(error_response, 'status_code', None),
                    body=getattr(error_response, 'content', None)) from error

        response = self.rate_limiter.run(self.requestCategory(uri), send)

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

    @classmethod
    def requestCategory(cls, uri):
        """
        Категория запроса для очереди — по пути, через таблицу REQUEST_CATEGORIES.

        Путь сравнивается без query-строки, без крайних и сдвоенных слэшей и без учёта
        регистра; {type} в шаблоне совпадает с любым одним сегментом.

        Returns:
            str: 'money', 'write' или 'read' (RateLimiter.MONEY / WRITE / READ).
        """
        path = str(uri).split('?', 1)[0].split('#', 1)[0]
        segments = [segment for segment in path.strip().lower().split('/') if segment]
        for pattern, category in cls.REQUEST_CATEGORIES.items():
            expected = pattern.split('/')
            if len(expected) == len(segments) and all(
                    want == '{type}' or want == got for want, got in zip(expected, segments)):
                return category
        return RateLimiter.READ

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
        geo в запросе resident/list/add и residentsubuser/list/add — ОБЪЕКТ
        (country/region/city/isp), не массив: массив сервер с объектом не свяжет и запрос
        отклонит.

        Пустой geo допустим (означает "без гео-фильтра"), поэтому None и пустая
        последовательность приводятся к {}. Прежнюю форму "массив из одного объекта"
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

    @staticmethod
    def _normalize_type(value):
        """
        Тип прокси из пути так, как его нормализует сервер: пробелы по краям отбрасываются,
        регистр не важен, '-' и ' ' становятся '_' ('MIX-ISP' -> 'mix_isp').
        """
        if value is None:
            return ''
        return str(value).strip().lower().replace('-', '_').replace(' ', '_')

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
                он принимает только поля summ и paymentId. Раньше в этом случае улетал
                paymentId=null и сервер отвечал "Payment id not exists".

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

    #: Поля запроса balance/autotopup/set.
    AUTO_TOPUP_FIELDS = ('enabled', 'threshold', 'amount', 'subscriptionId')

    #: Убраны из контракта 2026-08-18 вместе с кодами ошибок 54 и 55: лимиты списаний больше
    #: не настраиваются, действует один общий серверный (5 успешных пополнений в час).
    #: Присланные сервер молча игнорирует, поэтому отбиваем их локально — иначе вызов проходит,
    #: возвращает success и не делает НИЧЕГО.
    AUTO_TOPUP_REMOVED_FIELDS = ('dailyCountCap', 'monthlyAmountCap')

    def balanceAutoTopupGet(self):
        """
        Auto top-up configuration and current state.

        Returns:
            dict:
                configured (bool), enabled (bool),
                state (str): NO_PAYMENT_METHOD | DISABLED | ACTIVE | PAYMENT_INVALID |
                    PAUSED_FAILURES,
                threshold (float): списываем, когда баланс становится меньше этого значения,
                amount (float): сумма одного автопополнения,
                subscriptionId (str|None): закреплённая подписка Paddle (None у настроек,
                    созданных до 2026-08-14),
                paymentMethod (dict|None): {id, status ("active"/"expired"), paymentMethod
                    ("card"/"PayPal"/"Google Pay"/"Apple Pay"), brand, last4, exp ("MM/YYYY")},
                failCount (int): подряд идущие неудачные списания,
                lastAttemptAt: дата последней попытки (строка) или None,
                lastEvent (dict|None): {status (TRIGGERED | SUCCEEDED | FAILED | SKIPPED_CAP |
                    SETTINGS_SAVED | PAUSED), amount, at (дата), reason (None для
                    SUCCEEDED)}.

        Raises:
            ApiError: code 49 ("Auto top-up is not available"), если фича выключена
                на сервере.
        """
        return self.request('GET', 'balance/autotopup/get')

    @classmethod
    def _assert_auto_topup_fields(cls, values):
        """
        dailyCountCap и monthlyAmountCap удалены из контракта balance/autotopup/set
        2026-08-18: сервер их игнорирует, коды ошибок 54/55 сняты и не переиспользуются.
        Раньше вызов с ними проходил локальный гейт, возвращал success и не делал ничего —
        ровно тот тихий no-op, который белый список полей и должен предотвращать.

        Raises:
            ValueError: если в теле есть хотя бы одно из удалённых полей.
        """
        removed = [key for key in cls.AUTO_TOPUP_REMOVED_FIELDS if key in values]
        if removed:
            raise ValueError(
                "{} removed from balance/autotopup/set on 2026-08-18: the server ignores the "
                "field and a single shared limit applies instead (error codes 54/55 are gone "
                "too). Drop it from the call.".format(' and '.join(removed)))
        return values

    def balanceAutoTopupSet(self, enabled=_UNSET, threshold=_UNSET, amount=_UNSET,
                            subscriptionId=_UNSET, **unsupported):
        """
        Enable/disable auto top-up or update its thresholds.

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

        Returns:
            dict: состояние ПОСЛЕ сохранения, в той же форме, что у balanceAutoTopupGet() —
                второй запрос за актуальным state не нужен.

        Raises:
            ValueError: если передан dailyCountCap или monthlyAmountCap — оба удалены из
                контракта 2026-08-18, см. _assert_auto_topup_fields().
            ApiError: валидация целиком серверная и применяется к РЕЗУЛЬТАТУ мержа. Коды:
                49 — фича недоступна, 50 — threshold ниже минимума,
                51 — amount ниже минимума, 52 — amount не покрывает threshold,
                53 — нет привязанного способа оплаты, 56 — карта истекла.
                Коды 54 и 55 удалены вместе с полями лимитов. Граничные значения приходят
                в ``error.custom_data``: {"minAmount": ..., "minThreshold": ...}.
        """
        if isinstance(enabled, dict):
            data = self.filterNone(self._assert_auto_topup_fields(dict(enabled)))
        else:
            self._assert_auto_topup_fields(unsupported)
            if unsupported:
                raise TypeError(
                    'balanceAutoTopupSet() got an unexpected keyword argument {!r}'.format(
                        sorted(unsupported)[0]))
            values = {
                'enabled': enabled, 'threshold': threshold, 'amount': amount,
                'subscriptionId': subscriptionId}
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
                resident, scraper or None. Без типа приходят все разделы в фиксированном
                порядке ключей: ipv4, ipv6, mobile, isp, mix, mix_isp, resident, scraper.

        Returns:
            dict: The guide information for creating an order. Идентификаторы внутри —
                ObjectId-СТРОКИ, КРОМЕ ротации: rotations[].id — это ЧИСЛО МИНУТ
                (0 = "By Link").

                С указанным типом раздел приходит завёрнутым: {'items': {...}}; без типа —
                словарь разделов сразу.

                Что реально приходит в ответе (не больше и не меньше):
                    country[]: id, name → id и ЕСТЬ alpha3-код страны, отдельного поля
                        alpha3 сервер не отдаёт (у страны только id и name);
                    period[]: id, name ("1 month") → id и есть код периода;
                    mobile country[].operators.{dedicated,shared}[]: id, name, traffic,
                        rotations[{id, name}] → отдельного operatorCode нет, передавайте
                        полученный id как есть (сервер резолвит и ObjectId, и tag);
                    mix/mix_isp country[]: id, name → в id лежит тег mix-пакета, отдельных
                        полей alpha3 и tag здесь нет;
                    mix/mix_isp quantities[]: id, name, quantities;
                    resident/scraper tarifs[]: id, name, personal → id и есть код тарифа.
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

    #: Пары *Id/*Code для order/* и то, кто из них СТАРШЕ на сервере
    #: (True = старше *Code). Это все пары, которые сервер резолвит у order/*.
    ORDER_REFERENCE_PAIRS = (
        ('countryId', 'countryCode', True), ('periodId', 'periodCode', True),
        ('paymentId', 'paymentCode', True), ('mixId', 'mixCode', False),
        ('operatorId', 'operatorCode', False), ('rotationId', 'rotationCode', False),
        ('tarifId', 'tarifCode', False))

    #: У prolong/* и autoprolong/* сервер резолвит только период и платёжку, и у обеих пар
    #: старше *Code.
    PROLONG_REFERENCE_PAIRS = (
        ('periodId', 'periodCode', True), ('paymentId', 'paymentCode', True))

    @staticmethod
    def _resolveReferencePairs(payload, pairs):
        """
        Убирает лишнюю половину пары *Id/*Code — ровно по серверному приоритету.

        У payment/country/period старше *Code: сервер применяет код всегда, когда он задан.
        У operator/rotation/mix/tarif старше *Id: код применяется, ТОЛЬКО когда парный id пуст.
        Раньше SDK выбрасывал *Id при ЛЮБОМ заданном *Code, и клиент, заполнивший обе половины,
        молча получал не тот пакет/оператора/ротацию/тариф, который выбрал бы сервер.

        Пустая строка (и строка из одних пробелов) для сервера — "не задано", поэтому валидную
        парную половину она не стирает.
        """
        def filled(key):
            value = payload.get(key)
            return value is not None and str(value).strip() != ''

        for id_key, code_key, code_wins in pairs:
            if filled(id_key) and filled(code_key):
                payload.pop(id_key if code_wins else code_key, None)
        return payload

    def mergeOrderOptions(self, payload, options=None):
        """
        Merge v2 identifiers/codes while avoiding conflicting id/code pairs.

        Отдельные *Code-поля нужны только для совместимости: у каждого *Id есть серверный
        фолбэк — если значение не является валидным id, а соответствующий *Code пуст,
        значение резолвится КАК КОД. Поэтому код достаточно передать прямо в countryId /
        periodId / operatorId / mixId / tarifId / paymentId, цепочки None и словарь options
        ради кодов не нужны.

        rotationCode фолбэка не имеет вообще: он лишь проверяется на целое число и
        копируется в rotationId, так что пользуйтесь сразу rotationId (МИНУТЫ).

        Если заполнены обе половины пары, лишняя убирается по СЕРВЕРНОМУ приоритету —
        см. _resolveReferencePairs() и ORDER_REFERENCE_PAIRS.
        """
        values = options or {}
        if not isinstance(values, dict):
            raise TypeError('order options must be a dict')
        # generateAuth и алиасы uptime раньше в список не входили и молча терялись: первый
        # затирался значением setGenerateAuth() (по умолчанию 'N') в withGenerateAuth(), вторые
        # исчезали вовсе, хотя сервер принимает highAvailability и isUptime как алиасы uptime.
        allowed = (
            'countryId', 'countryCode', 'periodId', 'periodCode', 'paymentId', 'paymentCode',
            'mixId', 'mixCode', 'uptime', 'highAvailability', 'isUptime', 'protocol',
            'mobileServiceType', 'operatorId', 'operatorCode', 'rotationId', 'rotationCode',
            'tarifId', 'tarifCode', 'authorization', 'coupon', 'customTargetName', 'quantity',
            'generateAuth')
        payload.update({key: values[key] for key in allowed if key in values})
        return self.filterNone(
            self._resolveReferencePairs(payload, self.ORDER_REFERENCE_PAIRS))

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
        """
        generateAuth is accepted by order/make only, order/calc silently drops it.

        Значение из самого payload сильнее: setGenerateAuth() — это ДЕФОЛТ клиента, и раньше
        он затирал явно переданный generateAuth (тот и так терялся в белом списке
        mergeOrderOptions, так что до провода не доходил вовсе).
        """
        return {'generateAuth': self.getGenerateAuth(), **data}

    @staticmethod
    def _assert_target_name(data):
        """
        Повторяет серверную проверку цели: для ipv4/ipv6/isp заказ без цели не
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

        # Сервер распознаёт mix-пакет не только по mixId/mixCode, но и через countryId —
        # строкой "packageId:quantity" либо countryId=packageId вместе с quantity. Если mix
        # распознан, цель не требуется.
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

    def orderMake(self, data, fingerprint=None):
        """
        Create an order.

        Args:
            data (dict): Free format dictionary to send into endpoint. ``sectionCode``:
                ipv4 | ipv6 | mobile | isp | mix | mix_isp | resident | scraper.
                Идентификаторы — ObjectId-СТРОКИ (в том числе orderId в ответе), и в каждом
                *Id вместо ObjectId принимается код (см. orderCalc). rotationId — МИНУТЫ.
                Платёжка ОБЯЗАТЕЛЬНА: paymentCode / paymentId — 'balance' либо
                'paddle_subscription' (см. setPaymentCode).
            fingerprint (str): значение X-Fingerprint только для этого вызова; по умолчанию
                берётся заданное конфигом или setFingerprint(). Заголовок необязателен для
                любой секции: заданное значение уходит с запросом (анти-фрод и
                affiliate-атрибуция), пустое или не заданное — не уходит, и заказ создаётся
                так же.

        Returns:
            dict: The response from the endpoint.
        """
        self._assert_target_name(data)
        value = fingerprint if fingerprint is not None else self.getFingerprint()
        value = '' if value is None else str(value).strip()
        options = {'headers': {'X-Fingerprint': value}} if value else {}
        return self.request('POST', 'order/make', json=data, **options)

    def orderCalcIpv4(self, countryId=None, periodId=None, quantity=None, authorization=None,
                      coupon=None, customTargetName=None, options=None, **order_options):
        """
        Calculate IPv4.

        Args:
            countryId (str): код страны alpha3 из reference/list → country[].id
                (например 'USA'; сервер приводит к верхнему регистру). ObjectId тоже принимается.
            periodId (str): код периода из reference/list → period[].id (например '1m';
                сервер приводит к нижнему регистру). ObjectId тоже принимается.
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
            mixId (str): код mix-пакета (точное совпадение) из reference/list:
                mix/mix_isp -> quantities[].id (например 'europe-2-mix_IPv4') — рядом же
                доступные количества. ObjectId тоже принимается.
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

    def orderMakeResident(self, tarifId=None, coupon=None, options=None, fingerprint=None,
                          **order_options):
        """
        Create an order Resident. Attention! Deducts money from the balance.

        tarifId — ObjectId резидентского тарифа ЛИБО его code.

        ``fingerprint`` — необязательное значение X-Fingerprint для этого вызова (по
        умолчанию — из конфига или setFingerprint(), см. orderMake). Без него резидентский
        заказ создаётся так же.
        """
        return self.orderMake(self.prepareResident(
            tarifId, coupon, self._order_options(options, order_options)), fingerprint)

    def orderList(self, **filters):
        """
        List of orders.

        Args:
            filters: order_id (ObjectId-СТРОКА заказа), start_date / end_date (границы по
                дате создания; принимается и ISO, и 'dd.MM.yyyy'), status (PAYED | NOT_PAYED | RETURN — это
                status_type ответа, а не человекочитаемый status), is_extend ('Y'/'N' —
                только продления либо только первичные покупки), auto_order ('Y'/'N' — по
                включённому автопродлению), page, limit, sort_by (date_insert | summ |
                status), order (asc | desc). Все опциональны. Фильтры запроса и поля ответа
                order/list называются в snake_case (start_date, is_extend, …) — SDK передаёт
                имена как есть. Сервер значения не валидирует — неизвестное значение просто
                не применяется как фильтр.

        Returns:
            dict: не плоский список, а пара ``metadata`` + ``items``.
                ``metadata`` (total_orders, total_pages, current_page, current_limit) есть
                всегда: без ``limit`` там total_pages = 1, current_limit = 0, а весь список
                лежит в ``items``.

                id, order_id, order_number, base_order_number и items[]['order_part_id'] —
                СТРОКИ. id — числовой номер заказа, переданный строкой; ObjectId заказа лежит
                в order_id — тот же, что order_id в proxyList(), и именно его принимают
                prolongCalc() / prolongMake() для ipv6, mix и mix_isp. summ и вложенные
                items[]['price'] — тоже строки, уже с валютой ('$25.00'), auto_order /
                is_extend — 'Y'/'N', даты — ISO 8601 со смещением
                ('2026-09-01T14:15:26+00:00'), date_payed пуст, пока заказ не оплачен.
        """
        params = self.filterNone(filters)
        return self.request('GET', 'order/list', params=params)

    # --------------------------- Prolong ---------------------------

    #: Типы, которые продаются и продлеваются только ЦЕЛЫМ заказом. Их выбор — orderIds
    #: (order_id из proxy/list или order/list): продлевается всё активное этого типа в
    #: указанных заказах, у mix/mix_isp — mix-пакеты этих заказов. Остальные типы
    #: (ipv4 / isp / mobile) продлеваются по отдельным прокси — ids (id из proxy/list)
    #: либо ips (адреса).
    PROLONG_ORDER_TYPES = ('ipv6', 'mix', 'mix_isp')

    #: Поля выбора "что продлить" у prolong/* и autoprolong/*. Пустой список не
    #: отправляется: для сервера он ничем не отличается от отсутствующего поля.
    PROLONG_SELECTION_FIELDS = ('ids', 'ips', 'orderIds')

    #: Поля тела prolong/calc и prolong/make.
    PROLONG_FIELDS = (
        'ids', 'ips', 'orderIds', 'coupon', 'periodId', 'periodCode', 'paymentId',
        'paymentCode')

    #: Поля выбора, удалённые из контракта prolong/* и autoprolong/*. Сервер их больше не
    #: читает, и запрос ушёл бы без выбора, поэтому переданные в dict-форме, в options или
    #: именованными аргументами они отбиваются локально — с подсказкой, чем их заменить
    #: (см. _assert_no_removed_prolong_fields).
    PROLONG_REMOVED_FIELDS = ('orderSeparatorIds', 'orderSeparatorId')

    @classmethod
    def _assert_no_removed_prolong_fields(cls, values):
        """
        Удалённые из контракта поля выбора не выбрасываются молча: вызывающий, приславший
        их, решил бы, что продлевает выбранное, а сервер получил бы тело без выбора.

        Raises:
            ValueError: если в values есть orderSeparatorIds или orderSeparatorId — с
                названием замены (orderIds).
        """
        present = [key for key in cls.PROLONG_REMOVED_FIELDS if key in values]
        if present:
            raise ValueError(
                '{} {} removed: use orderIds (order_id from proxy/list or order/list) - '
                'mix/mix_isp are renewed as whole orders'.format(
                    '/'.join(present), 'were' if len(present) > 1 else 'was'))

    @staticmethod
    def _prolongItems(value):
        """
        Элементы выбора списком: list / tuple / set как есть, строка — список через запятую,
        одиночное значение — список из него. Строки обрезаются по краям, пустые строки и None
        выбрасываются.
        """
        if value is None:
            return []
        if isinstance(value, str):
            value = value.split(',')
        elif not isinstance(value, (list, tuple, set, frozenset)):
            value = [value]
        items = []
        for item in value:
            if isinstance(item, str):
                item = item.strip()
                if not item:
                    continue
            elif item is None:
                continue
            items.append(item)
        return items

    @classmethod
    def _splitProlongTargets(cls, targets, type=None):
        """
        Раскладывает "что продлить" по полям выбора — с учётом типа прокси из пути.

        Значение с точкой или двоеточием — адрес, он уходит в ips: у ipv4/isp это поле ip из
        proxy/list, у mobile — 'ip:port_http:port_socks'. Всё остальное — идентификатор: для
        ipv6 / mix / mix_isp это order_id заказа (orderIds — эти типы продлеваются только
        целым заказом), для остальных типов — id прокси (ids). Список, кортеж, множество
        или строка через запятую; пустые элементы пропускаются.

        Адрес для ipv6 / mix / mix_isp тоже уходит в ips, а не переосмысляется: сервер
        отвечает на него понятной ошибкой "[ips] is not applicable for ipv6: prolong by
        [orderIds]".

        Returns:
            dict: только непустые поля выбора — ips и/или ids либо orderIds.
        """
        ips, ids = [], []
        for item in cls._prolongItems(targets):
            if isinstance(item, str) and ('.' in item or ':' in item):
                ips.append(item)
            else:
                ids.append(item)
        selection = {}
        if ids:
            by_order = cls._normalize_type(type) in cls.PROLONG_ORDER_TYPES
            selection['orderIds' if by_order else 'ids'] = ids
        if ips:
            selection['ips'] = ips
        return selection

    def _prolongPayload(self, targets, type, defaults, values, fields, what,
                        package_types=()):
        """
        Общая сборка тела prolong/* и autoprolong/*: платёжка клиента, выбор из targets
        (_splitProlongTargets), значения по умолчанию и поля из белого списка fields —
        переданные явно, они сильнее. Пустые поля выбора не отправляются.

        Raises:
            TypeError: если values не dict.
            ValueError: если в values есть удалённые из контракта поля выбора
                (_assert_no_removed_prolong_fields); для типов из package_types — если выбор
                вообще передан; для ipv4 / isp / mobile — если в теле оказались сразу ids и
                ips: сервер продлевает по ids и игнорирует ips, и адреса выпали бы из
                продления молча.
        """
        if not isinstance(values, dict):
            raise TypeError('{} options must be a dict'.format(what))
        self._assert_no_removed_prolong_fields(values)
        payload = {
            **self.paymentOptions(), **self._splitProlongTargets(targets, type), **defaults}
        for key in fields:
            if key in values:
                payload[key] = values[key]
        for key in self.PROLONG_SELECTION_FIELDS:
            if key in payload:
                items = self._prolongItems(payload.pop(key))
                if items:
                    payload[key] = items
        normalized = self._normalize_type(type)
        if normalized in package_types:
            selected = [key for key in self.PROLONG_SELECTION_FIELDS if key in payload]
            if selected:
                # Сервер отвечает одним и тем же текстом про [ids], какое бы из полей выбора
                # ни пришло, — цитируем его дословно, а присланные поля называем отдельно.
                raise ValueError(
                    '{} for {} applies to the whole package and takes no proxy selection, '
                    'drop {} (client api answers "[ids] is not applicable for {}: auto-prolong '
                    'applies to the whole package")'.format(
                        what, normalized, ' / '.join(selected), normalized))
        elif ('ids' in payload and 'ips' in payload
                and normalized not in self.PROLONG_ORDER_TYPES):
            raise ValueError(
                'proxy ids and addresses cannot be combined{}: when both ids and ips are sent '
                'the server renews by ids and ignores ips, so the addresses would be skipped '
                'silently. Pass either the ids from proxy/list or the addresses.'.format(
                    ' for ' + normalized if normalized else ''))
        return self.filterNone(
            self._resolveReferencePairs(payload, self.PROLONG_REFERENCE_PAIRS))

    def prepareProlong(self, ids=None, periodId=None, coupon='', options=None, type=None):
        """
        Собрать тело prolong/calc и prolong/make.

        Args:
            ids: что продлить, см. prolongCalc(); раскладывается по полям выбора с учётом
                type. dict вместо значения — тело целиком, поля выбора в нём уже проводные
                (ids / ips / orderIds) и уходят как есть; удалённые orderSeparatorIds /
                orderSeparatorId в нём, как и в options, — ValueError.
            periodId (str): ObjectId периода ЛИБО код периода ('1m').
            coupon (str): Coupon code.
            options (dict): остальные поля тела (PROLONG_FIELDS); переданные явно, они сильнее.
            type (str): тип прокси из пути. Без него идентификаторы уходят в ids, как у
                ipv4 / isp / mobile.
        """
        if isinstance(ids, dict):
            return self._prolongPayload(
                None, type, {}, {**ids, **(options or {})}, self.PROLONG_FIELDS, 'prolong')
        return self._prolongPayload(
            ids, type, {'periodId': periodId, 'coupon': coupon}, options or {},
            self.PROLONG_FIELDS, 'prolong')

    def prolongCalc(self, type, ids=None, periodId=None, coupon='', options=None, **prolong_options):
        """
        Calculate the renewal.

        Args:
            type (str): The type of the order - ipv4, ipv6, mobile, isp, mix or mix_isp
                (регистр и '-'/' ' не важны: 'MIX-ISP' == 'mix_isp').
            ids (list): ЧТО продлить; значения раскладываются по типу
                (_splitProlongTargets):
                    ipv4 / isp — адрес '1.2.3.4' (поле 'ip' из proxy/list) либо 'id' прокси;
                    mobile — адрес 'ip:port_http:port_socks' (из полей ip, port_http и
                        port_socks proxy/list) либо 'id' прокси;
                    ipv6 / mix / mix_isp — 'order_id' заказа из proxy/list или order/list:
                        эти типы продлеваются только ЦЕЛЫМ заказом — всё активное этого
                        типа в заказе (у mix/mix_isp — mix-пакеты заказа).
                Адреса уходят в ips, id прокси — в ids, id заказов — в orderIds. Список
                либо строка через запятую. Адреса и id прокси в одном вызове не смешиваются:
                если пришли и ids, и ips, сервер продлевает по ids и игнорирует ips, поэтому
                такая смесь отбивается локально. Именованный ids= — этот же параметр, он
                тоже раскладывается по типу.
            periodId (str): ObjectId периода ЛИБО код периода ('1m') — сервер резолвит код
                прямо в этом поле, periodCode передавать не обязательно.
            coupon (str): Coupon code.
            options: paymentId (ObjectId ЛИБО код платёжной системы, например 'balance'),
                paymentCode, periodCode, а также поля выбора в проводном виде — ids, ips,
                orderIds: они уходят как есть, без раскладки, и явное значение сильнее
                разложенного из параметра ids (поле ids дословно — только через options или
                dict-форму). Пустые списки не отправляются. Удалённые из контракта
                orderSeparatorIds и orderSeparatorId здесь (как и в dict-форме) — ValueError
                с подсказкой замены (orderIds).

        Raises:
            ValueError: если для ipv4 / isp / mobile вместе переданы id прокси и адреса (ids
                и ips), или если в options / dict-форме есть orderSeparatorIds либо
                orderSeparatorId (замена — orderIds).
            ApiError: поле выбора не того вида для типа, code 0 — "[ids] is not applicable
                for ipv6: prolong by [orderIds]", "[ips] is not applicable for ipv6: prolong
                by [orderIds]", "[orderIds] is not applicable for ipv4: prolong by [ids]";
                code 29 "Incorrect orderIds" — заказ чужой, без активных прокси этого типа,
                или список пуст: запрос отбивается целиком.

        Returns:
            dict: warning, balance, total, quantity, currency, discount, orders, items[].
                quantity и items показывают, что продлевается на самом деле: у ipv6 / mix /
                mix_isp — все активные прокси выбранных заказов.
        """
        values = self._order_options(options, prolong_options)
        return self.request('POST', 'prolong/calc/' + type,
                            json=self.prepareProlong(ids, periodId, coupon, values, type))

    def prolongMake(self, type, ids=None, periodId=None, coupon='', options=None, **prolong_options):
        """
        Create a renewal order. Attention! Deducts money from the balance.

        Args:
            type (str): The type of the order - ipv4, ipv6, mobile, isp, mix or mix_isp.
            ids (list): что продлить — адреса либо id прокси для ipv4 / isp / mobile,
                order_id заказов для ipv6 / mix / mix_isp; см. prolongCalc().
            periodId (str): ObjectId периода ЛИБО код периода ('1m'), см. prolongCalc().
            coupon (str): Coupon code.
            options: paymentId (ObjectId ЛИБО код платёжной системы), ids / ips / orderIds —
                как в prolongCalc().

        Returns:
            dict: {'orderId', 'orderIds', 'total', 'listBaseOrderNumbers', 'balance'}.
                orderIds — ВСЕ продлённые заказы (order_id из proxy/list и order/list, без
                повторов: одним запросом продлевается несколько заказов), orderId — первый
                из них, оставлен для совместимости. listBaseOrderNumbers — базовые номера
                продлённых заказов, по одному на заказ (у mix/mix_isp — на пакет), как
                base_order_number в order/list.

        Raises:
            ValueError: как в prolongCalc().
            ApiError: при нехватке средств — продление НЕ состоялось. Причина приходит в
                errors[{code: 16, message: "Insufficient funds on balance"}], а calc-данные с
                дословным warning остаются в конверте — ``error.body['data']``. Ошибки
                выбора — как в prolongCalc().
        """
        values = self._order_options(options, prolong_options)
        # Прежняя обёртка _assert_prolong_made здесь УБРАНА. Она писалась под старую форму
        # нехватки средств ("status: error" + ПУСТОЙ errors[]) и с тех пор, как причина уехала
        # в errors[{code:16}], разбирается общим кодом конверта. Единственным оставшимся её
        # эффектом было превращать ЛЕГИТИМНЫЙ "status: success" с пустым orderId в фальшивую
        # ошибку — уже ПОСЛЕ списания денег, потеряв total/balance/listBaseOrderNumbers.
        return self.request('POST', 'prolong/make/' + type,
                            json=self.prepareProlong(ids, periodId, coupon, values, type))

    # --------------------------- Auto prolong ---------------------------

    #: Поля тела autoprolong/* — поля выбора и оплаты prolong/* (без купона) плюс
    #: subscriptionId и tarifId. Snake-написания сервер тоже принимает, но приоритет у
    #: camelCase, поэтому SDK шлёт каноническую форму; присланные вызывающим алиасы просто
    #: пропускаем дальше, мешать им незачем. Удалённые из контракта orderSeparatorIds и
    #: orderSeparatorId отбиваются ValueError, как и у prolong/* (PROLONG_REMOVED_FIELDS).
    AUTO_PROLONG_FIELDS = (
        'ids', 'ips', 'orderIds', 'periodId', 'periodCode',
        'paymentId', 'paymentCode', 'subscriptionId', 'tarifId',
        'payment_id', 'subscription_id', 'tarif_id', 'tariffId')

    #: Тип, которого у автопродления нет: скрапер добирает трафик новым заказом через
    #: order/make, и сервер отвечает "Create new order to add traffic, prolong options not
    #: available".
    AUTO_PROLONG_UNSUPPORTED_TYPES = ('scraper',)

    #: Типы, у которых автопродление адресуется всем пакетом: полей выбора в теле нет, а
    #: любое присланное — ids, ips или orderIds — сервер отбивает ("[ids] is not applicable
    #: for resident: auto-prolong applies to the whole package").
    AUTO_PROLONG_PACKAGE_TYPES = ('resident',)

    #: Платёжки, которые автопродление принимает. Разовый чекаут Paddle требует редиректа в
    #: браузер, которого у headless-клиента нет, поэтому он сюда не входит.
    AUTO_PROLONG_PAYMENT_CODES = ('balance', 'paddle_subscription')

    def prepareAutoProlong(self, ids=None, periodId=None, options=None, type=None):
        """
        Собрать тело autoprolong/*: тот же выбор, что у prolong/* (та же раскладка по типу —
        _splitProlongTargets), periodId/periodCode, paymentId/paymentCode, плюс
        subscriptionId и tarifId.

        Для type=resident выбора нет вовсе: единица правки — весь пакет, и тело состоит из
        платёжки (и, по желанию, tarifId).

        Купон СОЗНАТЕЛЬНО не отправляется, хотя тело autoprolong/* повторяет тело prolong/*:
        автопродление промокод не применяет нигде, и превью со скидкой врало бы ровно про ту
        сумму, ради которой ручку и зовут.

        Raises:
            ValueError: для type=resident с непустым выбором — сервер такой запрос отбивает, а
                молча выбросить выбор нельзя: клиент решил бы, что правит отдельные адреса,
                хотя правится весь пакет. Для ipv4 / isp / mobile — если вместе переданы id
                прокси и адреса (ids и ips). Для любого типа — если в options / dict-форме
                есть удалённые orderSeparatorIds или orderSeparatorId (см. _prolongPayload).
        """
        if isinstance(ids, dict):
            return self._prolongPayload(
                None, type, {}, {**ids, **(options or {})}, self.AUTO_PROLONG_FIELDS,
                'autoprolong', self.AUTO_PROLONG_PACKAGE_TYPES)
        return self._prolongPayload(
            ids, type, {'periodId': periodId}, options or {}, self.AUTO_PROLONG_FIELDS,
            'autoprolong', self.AUTO_PROLONG_PACKAGE_TYPES)

    @classmethod
    def _assert_auto_prolong_type(cls, type):
        """
        Скрапер автопродления не имеет: сервер отбивает его ДО резолва типа, тем же текстом,
        что и ручное продление. Проверяем локально, чтобы не платить сетевым запросом.

        Raises:
            ValueError: для type=scraper.
        """
        normalized = cls._normalize_type(type)
        if normalized in cls.AUTO_PROLONG_UNSUPPORTED_TYPES:
            raise ValueError(
                'autoprolong is not available for {}: client api answers "Create new order to '
                'add traffic, prolong options not available". Buy traffic with '
                'orderMake({{"sectionCode": "scraper", ...}}).'.format(normalized))

    @classmethod
    def _assert_auto_prolong_payment(cls, payload):
        """
        paymentId у autoprolong/calc и /enable ОБЯЗАТЕЛЕН — в отличие от prolong/*, где он
        необязателен: списание произойдёт без клиента, и "по умолчанию с баланса" было бы
        догадкой за него. Без поля сервер отвечает "Set [paymentId]".

        Какой ТИП платёжки стоит за ObjectId, знает только сервер, поэтому здесь проверяется
        наличие значения; а если код прислан дословно, то ещё и то, что он из разрешённой пары.
        subscriptionId не спрашиваем: с одной привязанной картой сервер берёт её сам, а сколько
        карт на аккаунте, видно только ему ("Set [subscriptionId]", если их несколько).

        Raises:
            ValueError: если платёжка не задана или задана неподдерживаемым кодом.
        """
        payment = payload.get('paymentId') or payload.get('paymentCode') or payload.get('payment_id')
        payment = '' if payment is None else str(payment).strip()
        if not payment:
            raise ValueError(
                'paymentId is required for autoprolong/calc and autoprolong/enable (client api '
                'answers "Set [paymentId]"): the charge happens while you are away, so the '
                'payment system cannot be guessed. Accepted: {}.'.format(
                    ' / '.join(cls.AUTO_PROLONG_PAYMENT_CODES)))
        if payment.lower() not in cls.AUTO_PROLONG_PAYMENT_CODES:
            # Значение похоже на ObjectId или на код другой платёжки — тип резолвит сервер,
            # локально отбиваем только заведомо чужой код.
            if payment.lower() in ('paddle', 'cryptomus', 'paypal'):
                raise ValueError(
                    'autoprolong accepts only {} ({!r} is a one-off checkout and needs a '
                    'browser redirect).'.format(
                        ' / '.join(cls.AUTO_PROLONG_PAYMENT_CODES), payment))

    def autoProlongCalc(self, type, ids=None, periodId=None, options=None, **autoprolong_options):
        """
        Calculate the upcoming automatic extension charge. Ничего не меняет.

        Args:
            type (str): ipv4 | ipv6 | mobile | isp | mix | mix_isp | resident. Для scraper
                автопродления нет (см. _assert_auto_prolong_type).
            ids (list): что поставить на автопродление — как в prolongCalc(): адрес либо id
                прокси для ipv4 / isp / mobile (уходят в ips / ids), order_id заказа для
                ipv6 / mix / mix_isp (уходят в orderIds, такие заказы автопродлеваются
                целиком). Для type=resident выбор не передаётся: единица правки — весь пакет
                (переданный выбор — ValueError).
            periodId (str): ObjectId периода ЛИБО код периода ('1m'). Обязателен для обычных
                прокси ("Set existed [periodId] from reference"), у резидентки периода нет.
            options: paymentId (ОБЯЗАТЕЛЕН, balance либо paddle_subscription),
                subscriptionId (при paddle_subscription, если привязанных карт несколько;
                единственную карту сервер берёт сам), tarifId (только resident —
                подтверждение тарифа самого пакета, сменить тариф автопродление не умеет),
                поля выбора в проводном виде — ids, ips, orderIds.

        Returns:
            dict: warning, balance, total, quantity, currency, discount, orders, items[],
                days (у резидентки — период тарифа), tarifId (только резидентка), chargeDate
                (null у резидентки — пакет продлевается по дате ОКОНЧАНИЯ ЛИБО по исчерпанию
                трафика), dateEnd, paymentId, autoProlong. Даты — строки "yyyy-MM-dd HH:mm:ss".

                НЕХВАТКА БАЛАНСА — это НЕ исключение: конверт приходит со status="error", но с
                ЗАПОЛНЕННЫМ data и ПУСТЫМ errors[] (та же форма, что у prolong/calc), поэтому
                метод возвращает данные, а warning объясняет разницу.

        Raises:
            ValueError: для type=scraper, при незаданной/неподдерживаемой платёжке, при
                выборе для resident, при смеси id прокси и адресов (ids и ips) и при
                удалённых orderSeparatorIds / orderSeparatorId в options (замена — orderIds).
        """
        self._assert_auto_prolong_type(type)
        values = self._order_options(options, autoprolong_options)
        payload = self.prepareAutoProlong(ids, periodId, values, type)
        self._assert_auto_prolong_payment(payload)
        return self.request('POST', 'autoprolong/calc/' + type, json=payload)

    def autoProlongEnable(self, type, ids=None, periodId=None, options=None, **autoprolong_options):
        """
        Enable automatic extension. Сейчас ничего не списывается — деньги нужны к chargeDate.

        Заменяет удалённый с сервера resident/autorenew/enable: то же самое теперь
        autoProlongEnable('resident', paymentId=...).

        Args:
            type (str): как в autoProlongCalc(). Для resident тело пакетное — достаточно
                paymentId (и опционально tarifId), выбор и periodId там не нужны.
            ids (list): что включить — как в autoProlongCalc(): адреса либо id прокси, для
                ipv6 / mix / mix_isp — order_id заказов.
            periodId (str): период, который будет покупаться при каждом продлении.
            options: paymentId (ОБЯЗАТЕЛЕН), subscriptionId, tarifId, ids / ips / orderIds, …

        Returns:
            dict: warning, autoProlong, quantity, ids[], orderIds[], days, paymentId,
                chargeDate, dateEnd. quantity, ids (id затронутых прокси) и orderIds (их
                заказы) — то, что РЕАЛЬНО затронуто, а не эхо запроса: у ipv6 / mix / mix_isp
                автопродление включается на все активные прокси присланных заказов. У
                резидентки quantity=1, а ids и orderIds пусты.

        Raises:
            ValueError: как в autoProlongCalc().
        """
        self._assert_auto_prolong_type(type)
        values = self._order_options(options, autoprolong_options)
        payload = self.prepareAutoProlong(ids, periodId, values, type)
        self._assert_auto_prolong_payment(payload)
        return self.request('POST', 'autoprolong/enable/' + type, json=payload)

    def autoProlongDisable(self, type, ids=None, options=None, **autoprolong_options):
        """
        Disable automatic extension. Сбрасывает и привязанный период, и платёжку, так что
        следующий autoProlongEnable() должен прислать их снова.

        Заменяет удалённый с сервера resident/autorenew/disable.

        Args:
            type (str): как в autoProlongCalc(). Для resident тело не нужно вовсе — пакет
                адресуется по apiKey.
            ids (list): что выключить — как в autoProlongCalc(): адреса либо id прокси, для
                ipv6 / mix / mix_isp — order_id заказов.
            options: ids / ips / orderIds. Ни periodId, ни paymentId здесь не требуются.

        Returns:
            dict: warning, autoProlong, quantity, ids[], orderIds[], dateEnd, а paymentId и
                chargeDate — null: выключение их и очищает. ids (id затронутых прокси) и
                orderIds (их заказы) — то, что РЕАЛЬНО выключено, как у autoProlongEnable();
                у резидентки оба пусты. days у обычных прокси тоже null, у резидентки —
                период тарифа (тариф на пакете остаётся).

        Raises:
            ValueError: для type=scraper, при выборе для resident, при смеси id прокси и
                адресов (ids и ips) и при удалённых orderSeparatorIds / orderSeparatorId в
                options.
        """
        self._assert_auto_prolong_type(type)
        values = self._order_options(options, autoprolong_options)
        return self.request('POST', 'autoprolong/disable/' + type,
                            json=self.prepareAutoProlong(ids, None, values, type))

    # --------------------------- Proxy ---------------------------

    def proxyList(self, type=None, **filters):
        """
        List of proxies.

        Args:
            type (str): ipv4 | ipv6 | mobile | isp | mix | mix_isp | resident | None.
                Без типа ответ — словарь с ключами ipv4, ipv6, mobile, isp, mix, mix_isp,
                resident.
            filters: latest, orderId, country (код страны), ends, page, per_page (пагинация
                работает только для запроса с типом).
                latest="Y" — только прокси последнего заказа среди тех, что вернул бы запрос:
                с типом — последнего заказа этого типа (mix / mix_isp — последнего MIX), без
                типа — один последний заказ на весь ответ. «Последний» — по покупке, продление
                не в счёт. С orderId игнорируется, на resident и scraper не действует.
                orderId — любой идентификатор заказа из ответов API: order_id (proxyList /
                orderList), числовой id строки orderList (id строки продления — её заказ) или
                номер: текущий order_number, base_order_number либо прежний номер продлённого
                заказа (_e_<hash>).

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

        Литеральный маршрут proxy/download/resident — отдельная ручка основного
        резидентского пакета, она специфичнее шаблона proxy/download/{type}. Лист в ней
        задаётся параметром listId либо его алиасом ``id`` — SDK отправляет ``id``.

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

    #: Допустимые значения type у proxy/replace — ПРИЧИНА замены.
    PROXY_REPLACE_TYPES = (
        'NOT_WORK', 'INCORRECT_LOCATION', 'CANT_CHANGE_NETWORK', 'LOW_SPEED', 'CUSTOM')

    def proxyReplace(self, ids, type=None, comment=None, reason=None):
        """
        Replace proxy IPs.

        Args:
            ids (list): Ids of the IP addresses to replace (ObjectId-строки). Одиночный id
                тоже принимается.
            type (str): ПРИЧИНА замены, а не тип прокси. Одно из:
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

        Ключи ответа — snake_case, а НЕ camelCase.
        Единственное имя, совпадающее в обоих написаниях, — ``rotation``.

        Returns:
            dict: package_key, user_id, is_active, tarif_id, is_link_date, traffic_limit,
                traffic_usage, traffic_left, их близнецы ``*_sub`` и ``*_formatted``,
                auto_renew, auto_renew_payment_id, rotation и ``expired_at`` — СТРОКА
                в формате "dd.MM.yyyy HH:mm:ss". Именно здесь эта ручка отличается от
                ``residentsubuser/*``, где тот же ключ приходит объектом PHP-даты.
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
            dict | list: обычно объект, СГРУППИРОВАННЫЙ по логину листа и времени. Но при
                пустом периоде сервер отдаёт ПУСТОЙ СПИСОК ``[]``, а не ``{}``. Код, идущий по
                ключам или ``.items()``, на этом штатном ответе упадёт: проверяйте результат
                перед разбором.
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
            ValueError: если в dict-форме ``geo`` не объект. Сервер ждёт объект
                (country/region/city/isp), массив он отклоняет. Пустой geo допустим —
                значит "без гео-фильтра". Раньше geo=[] падал AttributeError.

        Returns:
            dict: Created list model, ``id`` — число. В ОТВЕТЕ ``geo`` — МАССИВ
                (``geo[0].country``), пустой при листе без гео-фильтра; объектом оно бывает
                только в ЗАПРОСЕ. ``geo['country']`` даст TypeError.
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
            geo (dict): гео целиком — ОБЪЕКТ {country, region, city, isp}; поле для сервера
                обязательное, поэтому пустой объект отправляется всегда.
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
