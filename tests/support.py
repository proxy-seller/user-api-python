"""
Фейковый транспорт для офлайн-тестов: полностью повторяет то, что Api.request ждёт от
requests.Session (метод request(...) -> объект с status_code/headers/json()/text/content).
Сеть не задействована. FakeClock — фейковое время для очереди запросов (RateLimiter).
"""

import threading

NO_JSON = object()


class FakeClock:
    """
    Фейковые часы для очереди запросов: monotonic() отдаёт текущее время, sleep() мгновенно
    сдвигает его вперёд и запоминает каждую паузу в sleeps. Потокобезопасны. Отсчёт с нуля —
    так заметен баг "нет предыдущего старта" == 0.
    """

    def __init__(self, start=0.0):
        self._now = float(start)
        self.sleeps = []
        self._lock = threading.Lock()

    def monotonic(self):
        with self._lock:
            return self._now

    def sleep(self, seconds):
        with self._lock:
            self.sleeps.append(seconds)
            self._now += seconds

    def advance(self, seconds):
        """Время, прошедшее само (пользователь ничего не отправлял) — в sleeps не пишется."""
        with self._lock:
            self._now += seconds


class FakeResponse:
    def __init__(self, payload=None, status_code=200, headers=None, text='', content=b''):
        self.status_code = status_code
        self.headers = headers if headers is not None else {'Content-Type': 'application/json'}
        self._payload = payload
        self.text = text
        self.content = content

    def json(self):
        if self._payload is NO_JSON:
            raise ValueError('no json body')
        return self._payload


class FakeSession:
    """
    Записывает вызовы и отдаёт заранее подготовленные ответы. Исключение в списке ответов
    бросается вместо ответа (транспортная ошибка).

    С clock каждый вызов получает поле 'at' — момент старта по фейковым часам, а latency
    сдвигает часы на длительность запроса.
    """

    def __init__(self, responses=None, clock=None, latency=0.0):
        self.calls = []
        self.responses = list(responses or [])
        self.closed = False
        self.clock = clock
        self.latency = latency
        self._lock = threading.Lock()

    def request(self, method, url, **kwargs):
        call = {'method': method, 'url': url}
        call.update(kwargs)
        with self._lock:
            if self.clock is not None:
                call['at'] = self.clock.monotonic()
            self.calls.append(call)
            response = self.responses.pop(0) if self.responses else None
        if self.clock is not None and self.latency:
            self.clock.advance(self.latency)
        if isinstance(response, BaseException):
            raise response
        if response is not None:
            return response
        return FakeResponse({'status': 'success', 'data': {}, 'errors': []})

    def close(self):
        self.closed = True

    @property
    def last(self):
        return self.calls[-1]


def envelope(data, status='success', errors=None):
    return FakeResponse({'status': status, 'data': data, 'errors': errors or []})


def error_envelope(errors):
    return FakeResponse({'status': 'error', 'data': None, 'errors': errors})


def text_file(body, content_type='text/plain'):
    return FakeResponse(
        NO_JSON, headers={'Content-Type': content_type,
                          'Content-Disposition': 'attachment; filename="proxy.txt"'},
        text=body, content=body.encode('utf-8'))


def json_file(body, filename='geo.json'):
    return FakeResponse(
        NO_JSON,
        headers={'Content-Type': 'application/json',
                 'Content-Disposition': 'attachment; filename="{}"'.format(filename)},
        content=body.encode('utf-8'), text=body)
