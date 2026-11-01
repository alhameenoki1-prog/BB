"""Authenticated Link Manager route regression tests."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from bb.simulation_link_routes import register_simulation_link_routes
from bb.simulation_link_store import list_links


@pytest.fixture
def environment(tmp_path):
    app = FastAPI()
    settings = SimpleNamespace(
        base_url="https://bb.example.com",
        training_destination_hostnames=("training.example.com",),
    )

    users = {
        "alice": {"id": "alice"},
        "bob": {"id": "bob"},
    }

    def current_user(request):
        identity = request.headers.get("x-test-user")
        if identity not in users:
            raise HTTPException(401, "Authentication required.")
        return users[identity]

    # This is an isolated test application, not BB's production app.
    from fastapi import Request

    async def authenticated_user(request: Request):
        return current_user(request)

    def base_workspace(user):
        root = tmp_path / user["id"]
        root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(user_data_dir=root)

    async def render(request, template_name, user, **context):
        return HTMLResponse(
            "LINK_MANAGER:" + user["id"] + ":" + str(len(context["links"]))
        )

    register_simulation_link_routes(
        app, settings, authenticated_user, base_workspace, render
    )

    return TestClient(app), tmp_path


def headers(user="alice", origin="https://bb.example.com"):
    result = {"x-test-user": user}
    if origin is not None:
        result["origin"] = origin
    return result


def create(client, user="alice", origin="https://bb.example.com", **changes):
    data = {
        "name": "Awareness lesson",
        "hostname": "bb.example.com",
        "path": "lesson",
        "destination": "https://training.example.com/",
    }
    data.update(changes)
    return client.post(
        "/links/create",
        data=data,
        headers=headers(user, origin),
        follow_redirects=False,
    )


def test_authentication_required(environment):
    client, _ = environment
    assert client.get("/links").status_code == 401
    assert client.post("/links/create", data={}).status_code == 401
    assert client.post("/links/status", data={}).status_code == 401


def test_create_requires_same_origin(environment):
    client, root = environment

    assert create(client, origin=None).status_code == 403
    assert create(client, origin="https://other.example.com").status_code == 403
    assert list_links(root / "alice") == []


def test_create_and_user_isolation(environment):
    client, root = environment

    response = create(client)
    assert response.status_code == 303
    assert len(list_links(root / "alice")) == 1
    assert list_links(root / "bob") == []

    assert client.get("/links", headers=headers("alice")).status_code == 200
    assert client.get("/links", headers=headers("bob")).status_code == 200


def test_unapproved_destination_rejected(environment):
    client, root = environment

    response = create(
        client,
        destination="https://unapproved.example.com/",
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert list_links(root / "alice") == []


def test_duplicate_path_rejected(environment):
    client, root = environment

    assert create(client).status_code == 303
    response = create(client)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert len(list_links(root / "alice")) == 1


def test_disable_and_enable(environment):
    client, root = environment

    create(client)
    record = list_links(root / "alice")[0]

    def update(status):
        return client.post(
            "/links/status",
            data={"link_id": record["id"], "status": status},
            headers=headers(),
            follow_redirects=False,
        )

    assert update("disabled").status_code == 303
    assert list_links(root / "alice")[0]["status"] == "disabled"

    assert update("active").status_code == 303
    assert list_links(root / "alice")[0]["status"] == "active"


def test_status_rejects_other_users_record(environment):
    client, root = environment

    create(client)
    record = list_links(root / "alice")[0]

    response = client.post(
        "/links/status",
        data={"link_id": record["id"], "status": "disabled"},
        headers=headers("bob"),
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert list_links(root / "alice")[0]["status"] == "active"


def test_status_requires_origin_and_valid_status(environment):
    client, root = environment

    create(client)
    record = list_links(root / "alice")[0]

    for origin in (None, "https://other.example.com"):
        response = client.post(
            "/links/status",
            data={"link_id": record["id"], "status": "disabled"},
            headers=headers(origin=origin),
            follow_redirects=False,
        )
        assert response.status_code == 403

    response = client.post(
        "/links/status",
        data={"link_id": record["id"], "status": "invalid"},
        headers=headers(),
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert list_links(root / "alice")[0]["status"] == "active"


def test_no_public_redirect_route(environment):
    client, _ = environment

    response = client.get("/lesson", follow_redirects=False)
    assert response.status_code == 404
