"""
Фейковый транспорт для офлайн-тестов: полностью повторяет то, что Api.request ждёт от
requests.Session (метод request(...) -> объект с status_code/headers/json()/text/content).
Сеть не задействована.
"""

NO_JSON = object()


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
    """Записывает вызовы и отдаёт заранее подготовленные ответы."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = list(responses or [])
        self.closed = False

    def request(self, method, url, **kwargs):
        call = {'method': method, 'url': url}
        call.update(kwargs)
        self.calls.append(call)
        if self.responses:
            return self.responses.pop(0)
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
