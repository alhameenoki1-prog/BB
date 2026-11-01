"""Domain Manager HTTP integration tests using the real BB application."""

import pytest
from fastapi.testclient import TestClient

from bb.app import create_app
from bb.domain_store import list_domains
from bb.settings import Settings
from bb.workspaces import Workspace
from tests.test_bb_web import ADMIN, USER, _admin, _as_user, _dns_stub, _make_user


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr("bb.app.check_domain", _dns_stub)

    settings = Settings(env={
        "BB_HOME": str(tmp_path / "bb"),
        "BB_BASE_URL": "http://testserver",
        "BB_SECRET_KEY": "domain-route-test-secret",
        "BB_ADMIN_EMAIL": ADMIN["email"],
        "BB_ADMIN_PASSWORD": ADMIN["password"],
    })

    with TestClient(create_app(settings), follow_redirects=False) as client:
        client.settings = settings
        yield client, settings


def post(client, route, data, origin="http://testserver"):
    headers = {"origin": origin} if origin is not None else {}
    return client.post(
        route,
        data=data,
        headers=headers,
        follow_redirects=False,
    )


def records(settings, email):
    from bb.accounts import Accounts

    user = Accounts(settings.accounts_db).get_by_email(email)
    return list_domains(Workspace(settings, user).user_data_dir)


def test_domain_routes_require_authentication(environment):
    client, _ = environment

    assert client.get("/domains").status_code == 303
    assert post(client, "/domains/add", {"hostname": "example.com"}).status_code == 303
    assert post(client, "/domains/verify", {"hostname": "example.com"}).status_code == 303


def test_domain_registration_requires_same_origin(environment):
    client, settings = environment
    _admin(client)

    for origin in (None, "https://other.example.com"):
        response = post(
            client,
            "/domains/add",
            {"hostname": "training.example.com"},
            origin=origin,
        )
        assert response.status_code == 403

    assert records(settings, ADMIN["email"]) == []


def test_domain_registration_and_duplicate_rejection(environment):
    client, settings = environment
    _admin(client)

    response = post(
        client,
        "/domains/add",
        {"hostname": "training.example.com", "provider": "cloudflare"},
    )
    assert response.status_code == 303

    saved = records(settings, ADMIN["email"])
    assert len(saved) == 1
    assert saved[0]["hostname"] == "training.example.com"
    assert saved[0]["status"] == "pending_verification"

    duplicate = post(
        client,
        "/domains/add",
        {"hostname": "training.example.com"},
    )
    assert duplicate.status_code == 303
    assert "error=" in duplicate.headers["location"]
    assert len(records(settings, ADMIN["email"])) == 1


def test_failed_dns_verification_keeps_domain_pending(environment, monkeypatch):
    client, settings = environment
    _admin(client)

    post(client, "/domains/add", {"hostname": "training.example.com"})

    monkeypatch.setattr(
        "bb.domain_verification.verify_domain",
        lambda record: {"verified": False, "message": "TXT record missing"},
    )

    response = post(
        client,
        "/domains/verify",
        {"hostname": "training.example.com"},
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert records(settings, ADMIN["email"])[0]["status"] == "pending_verification"


def test_successful_dns_verification_updates_domain(environment, monkeypatch):
    client, settings = environment
    _admin(client)

    post(client, "/domains/add", {"hostname": "training.example.com"})

    monkeypatch.setattr(
        "bb.domain_verification.verify_domain",
        lambda record: {"verified": True, "message": "Verified"},
    )

    response = post(
        client,
        "/domains/verify",
        {"hostname": "training.example.com"},
    )

    assert response.status_code == 303
    saved = records(settings, ADMIN["email"])[0]
    assert saved["status"] == "verified"
    assert saved["verified_at"] is not None


def test_user_cannot_verify_another_users_domain(environment, monkeypatch):
    client, settings = environment
    _admin(client)

    post(client, "/domains/add", {"hostname": "training.example.com"})
    _make_user(client)

    client.cookies.clear()
    assert client.post("/login", data=USER).status_code == 303

    calls = []

    def fake_verify(record):
        calls.append(record)
        return {"verified": True, "message": "Verified"}

    monkeypatch.setattr("bb.domain_verification.verify_domain", fake_verify)

    response = post(
        client,
        "/domains/verify",
        {"hostname": "training.example.com"},
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert calls == []
    assert records(settings, ADMIN["email"])[0]["status"] == "pending_verification"
    assert records(settings, USER["email"]) == []


def test_domain_verification_requires_same_origin(environment):
    client, settings = environment
    _admin(client)

    post(client, "/domains/add", {"hostname": "training.example.com"})

    for origin in (None, "https://other.example.com"):
        response = post(
            client,
            "/domains/verify",
            {"hostname": "training.example.com"},
            origin=origin,
        )
        assert response.status_code == 403

    assert records(settings, ADMIN["email"])[0]["status"] == "pending_verification"
