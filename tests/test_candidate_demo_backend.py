"""Controlled BE proxy contract; no credentials or real service are used."""

import unittest
from uuid import uuid4

import httpx

from app.candidate_detection.demo_backend import DemoBackend, validate_backend_url
from app.candidate_detection.demo_web import create_app


class BackendDemoTests(unittest.IsolatedAsyncioTestCase):
    def test_backend_url_is_local_and_explicit(self):
        self.assertEqual(validate_backend_url('http://127.0.0.1:8080'), 'http://127.0.0.1:8080')
        self.assertEqual(validate_backend_url('http://host.docker.internal:8080'),
                         'http://host.docker.internal:8080')
        for url in ('https://example.com:8080', 'http://example.com:8080',
                    'http://127.0.0.1:8080/internal/cases', 'http://127.0.0.1',
                    'http://user:secret@127.0.0.1:8080'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_backend_url(url)

    async def test_login_live_message_and_csrf_reach_only_allowed_be_paths(self):
        channel, live, message = (str(uuid4()) for _ in range(3))
        calls = []

        def backend(request):
            calls.append((request.method, request.url.path, dict(request.headers)))
            if request.url.path == '/api/auth/login':
                return httpx.Response(200, json={'data': {'channelIds': [channel]}, 'traceId': str(uuid4())},
                    headers=[('set-cookie', 'JSESSIONID=synthetic-session; Path=/; HttpOnly'),
                             ('set-cookie', 'XSRF-TOKEN=synthetic-csrf; Path=/')])
            assert request.headers.get('cookie', '').find('JSESSIONID=synthetic-session') >= 0
            if request.url.path == '/api/auth/me':
                return httpx.Response(200, json={'data': {'channelIds': [channel]}})
            if request.url.path == f'/api/channels/{channel}/lives/current':
                return httpx.Response(200, json={'data': {'liveId': live}})
            if request.url.path == f'/api/lives/{live}/messages':
                assert request.headers['x-xsrf-token'] == 'synthetic-csrf'
                assert request.content.find(b'synthetic chat') >= 0
                return httpx.Response(201, json={'data': {'messageId': message,
                    'visibility': 'HELD', 'moderationReasonCode': 'CONTEXT_PENDING'}})
            raise AssertionError(f'unexpected Backend path: {request.url.path}')

        app = create_app(None, backend=DemoBackend('http://127.0.0.1:8080',
                                                   transport=httpx.MockTransport(backend)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1:8765') as client:
            self.assertTrue((await client.get('/health')).json()['backendConfigured'])
            origin = {'Origin': 'http://127.0.0.1:8765'}
            login = await client.post('/api/backend/login', headers=origin,
                                      json={'username': 'synthetic', 'password': 'synthetic'})
            self.assertEqual(login.status_code, 200)
            self.assertEqual(len(login.headers.get_list('set-cookie')), 2)
            self.assertEqual((await client.get('/api/backend/me')).status_code, 200)
            current = await client.get(f'/api/backend/channels/{channel}/current-live')
            self.assertEqual(current.json()['data']['liveId'], live)
            sent = await client.post(f'/api/backend/lives/{live}/messages', headers={
                **origin, 'X-XSRF-TOKEN': 'synthetic-csrf'}, json={
                    'clientMessageId': str(uuid4()), 'text': 'synthetic chat'})
            self.assertEqual(sent.status_code, 201)
            self.assertEqual(sent.json()['data']['moderationReasonCode'], 'CONTEXT_PENDING')
        self.assertEqual([path for _, path, _ in calls], [
            '/api/auth/login', '/api/auth/me', f'/api/channels/{channel}/lives/current',
            f'/api/lives/{live}/messages'])

    async def test_origin_and_backend_failure_are_explicit(self):
        calls = []
        def backend(request):
            calls.append(request.url.path)
            return httpx.Response(403, json={'error': {'code': 'UNAUTHENTICATED'}})
        app = create_app(None, backend=DemoBackend('http://localhost:8080',
                                                   transport=httpx.MockTransport(backend)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://127.0.0.1:8765') as client:
            rejected = await client.post('/api/backend/login', headers={'Origin': 'http://evil.example'}, json={})
            self.assertEqual(rejected.status_code, 403)
            self.assertFalse(calls)
            denied = await client.get('/api/backend/me')
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()['error']['code'], 'UNAUTHENTICATED')
            invalid_id = await client.post('/api/backend/lives/not-a-uuid/messages',
                                           headers={'Origin': 'http://127.0.0.1:8765'}, json={})
            self.assertEqual(invalid_id.status_code, 422)
            self.assertEqual(calls, ['/api/auth/me'])

    async def test_browser_sessions_do_not_share_backend_cookies(self):
        observed = []
        def backend(request):
            observed.append(request.headers.get('cookie', ''))
            if request.url.path == '/api/auth/login':
                return httpx.Response(200, json={'data': {'channelIds': []}},
                                      headers={'set-cookie': 'JSESSIONID=first-user; Path=/; HttpOnly'})
            return httpx.Response(401, json={'error': {'code': 'UNAUTHENTICATED'}})
        app = create_app(None, backend=DemoBackend('http://127.0.0.1:8080',
                                                   transport=httpx.MockTransport(backend)))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8765') as first:
            await first.post('/api/backend/login', json={}, headers={'Origin': 'http://127.0.0.1:8765'})
            async with httpx.AsyncClient(transport=transport, base_url='http://127.0.0.1:8765') as second:
                self.assertEqual((await second.get('/api/backend/me')).status_code, 401)
            await first.get('/api/backend/me')
        self.assertEqual(observed, ['', '', 'JSESSIONID=first-user'])
