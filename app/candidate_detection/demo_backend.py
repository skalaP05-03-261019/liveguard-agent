"""Narrow, same-origin proxy for the local demo's real Backend chat path."""

from urllib.parse import urlsplit

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse


def validate_backend_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', 'host.docker.internal'}
            or parsed.port is None or parsed.path not in {'', '/'}
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError('Backend demo URL must be local HTTP with an explicit port')
    return f'http://{parsed.hostname}:{parsed.port}'


class DemoBackend:
    def __init__(self, base_url: str, *, transport=None):
        self.base_url = validate_backend_url(base_url)
        self.transport = transport

    async def relay(self, request: Request, method: str, path: str):
        headers = {'Accept': 'application/json'}
        if request.headers.get('cookie'):
            headers['Cookie'] = request.headers['cookie']
        if request.headers.get('x-xsrf-token'):
            headers['X-XSRF-TOKEN'] = request.headers['x-xsrf-token']
        body = None
        if method == 'POST':
            headers['Content-Type'] = 'application/json'
            body = await request.body()
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=8.0,
                                         follow_redirects=False, trust_env=False,
                                         transport=self.transport) as client:
                upstream = await client.request(method, path, headers=headers, content=body)
        except httpx.HTTPError:
            return JSONResponse(status_code=503, content={'error': {'code': 'BACKEND_UNAVAILABLE'}})
        try:
            payload = upstream.json()
        except ValueError:
            return JSONResponse(status_code=502, content={'error': {'code': 'BACKEND_INVALID_RESPONSE'}})
        if not isinstance(payload, dict) or upstream.status_code >= 500:
            return JSONResponse(status_code=502, content={'error': {'code': 'BACKEND_INVALID_RESPONSE'}})
        response = JSONResponse(status_code=upstream.status_code, content=payload)
        for cookie in upstream.headers.get_list('set-cookie'):
            response.headers.append('set-cookie', cookie)
        return response
