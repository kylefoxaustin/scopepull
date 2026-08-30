from __future__ import annotations

import httpx
import pytest

from scopepull.client import ScopeClient
from tests.mock_scope import create_app


@pytest.fixture
def mock_app():
    return create_app()


@pytest.fixture
def scope_state(mock_app):
    return mock_app.state.scope


@pytest.fixture
async def client(mock_app):
    transport = httpx.ASGITransport(app=mock_app)
    async with ScopeClient("http://192.168.100.1", settle=0.0, transport=transport) as c:
        yield c
