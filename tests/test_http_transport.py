"""Tests for the Streamable HTTP transport and the token that guards it."""

import pytest
from starlette.testclient import TestClient

from mcp_markdown_repositories.http_server import DEFAULT_PORT, HEALTH_PATH, MCP_PATH, build_app

TOKEN = "jeton-de-test"


@pytest.fixture
def client():
    # The transport rejects unknown Host headers to block DNS rebinding, and
    # TestClient's default host ("testserver") is exactly that — hence a base
    # URL that matches what a real local client sends.
    base_url = f"http://127.0.0.1:{DEFAULT_PORT}"
    with TestClient(build_app(TOKEN), base_url=base_url) as running:
        yield running


def test_health_endpoint_needs_no_token(client):
    response = client.get(HEALTH_PATH)
    assert response.status_code == 200
    assert response.text == "ok"


def test_mcp_endpoint_refuses_a_request_without_a_token(client):
    response = client.post(MCP_PATH, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401


def test_mcp_endpoint_refuses_a_wrong_token(client):
    response = client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": "Bearer mauvais-jeton"},
    )
    assert response.status_code == 401


def test_mcp_endpoint_refuses_a_bare_token_without_the_scheme(client):
    response = client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": TOKEN},
    )
    assert response.status_code == 401


def test_valid_token_reaches_the_transport(client):
    """The transport then applies its own rules — what matters is that the
    request got past the guard, so anything but 401 proves the point."""
    response = client.post(
        MCP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code != 401


def test_mcp_path_is_served_without_a_redirect(client):
    """Clients POST to "/mcp" and do not follow redirects."""
    response = client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
        headers={"Authorization": f"Bearer {TOKEN}"},
        follow_redirects=False,
    )
    assert response.status_code != 307


def test_initialize_is_accepted_by_the_transport(client):
    response = client.post(
        MCP_PATH,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/json, text/event-stream",
        },
    )
    assert response.status_code == 200
