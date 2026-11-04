"""Regression tests for BB without credential-collection simulation routes."""

from bb.app import create_app
from bb.settings import Settings


def test_removed_phishing_routes_are_not_registered(tmp_path):
    settings = Settings(env={
        "BB_HOME": str(tmp_path / "bb"),
        "BB_BASE_URL": "http://testserver",
        "BB_SECRET_KEY": "test-secret",
        "BB_ADMIN_EMAIL": "admin@example.com",
        "BB_ADMIN_PASSWORD": "correct-horse-battery",
    })
    app = create_app(settings)
    assert not any(route.path.startswith("/phish") for route in app.routes)


def test_regular_application_routes_remain_available(tmp_path):
    settings = Settings(env={
        "BB_HOME": str(tmp_path / "bb"),
        "BB_BASE_URL": "http://testserver",
        "BB_SECRET_KEY": "test-secret",
        "BB_ADMIN_EMAIL": "admin@example.com",
        "BB_ADMIN_PASSWORD": "correct-horse-battery",
    })
    app = create_app(settings)
    paths = {route.path for route in app.routes}
    assert "/login" in paths
    assert "/admin/users" in paths
