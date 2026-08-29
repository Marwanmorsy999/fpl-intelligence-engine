from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fpl_intelligence.api.performance import RequestProfilingMiddleware

def test_request_id_is_generated_and_returned():
    app = FastAPI()
    app.add_middleware(RequestProfilingMiddleware)

    @app.get('/probe')
    def probe():
        return {'ok': True}

    with TestClient(app) as client:
        response = client.get('/probe')

    assert response.status_code == 200
    request_id = response.headers.get('x-request-id')
    assert request_id and len(request_id) == 32

def test_supplied_request_id_is_preserved():
    app = FastAPI()
    app.add_middleware(RequestProfilingMiddleware)

    @app.get('/probe')
    def probe():
        return {'ok': True}

    with TestClient(app) as client:
        response = client.get('/probe', headers={'x-request-id': 'phase0-test'})

    assert response.headers['x-request-id'] == 'phase0-test'
