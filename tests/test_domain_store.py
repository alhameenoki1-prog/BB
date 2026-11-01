"""Regression tests for workspace-scoped domain storage."""

import json
import stat

import pytest

from bb.domain_store import add_domain, list_domains, mark_verified


def test_add_domain_creates_pending_record(tmp_path):
    record = add_domain(tmp_path, "Training.Example.com", "cloudflare")

    assert record["hostname"] == "training.example.com"
    assert record["status"] == "pending_verification"
    assert record["verified_at"] is None
    assert len(record["challenge"]) == 64


def test_duplicate_domain_rejected(tmp_path):
    add_domain(tmp_path, "training.example.com")

    with pytest.raises(ValueError, match="already registered"):
        add_domain(tmp_path, "TRAINING.EXAMPLE.COM")

    assert len(list_domains(tmp_path)) == 1


def test_invalid_provider_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unsupported"):
        add_domain(tmp_path, "training.example.com", "unknown")


def test_workspace_isolation(tmp_path):
    alice = tmp_path / "alice"
    bob = tmp_path / "bob"

    add_domain(alice, "alice.example.com")
    add_domain(bob, "bob.example.com")

    assert [r["hostname"] for r in list_domains(alice)] == [
        "alice.example.com"
    ]
    assert [r["hostname"] for r in list_domains(bob)] == [
        "bob.example.com"
    ]


def test_mark_verified_updates_only_target(tmp_path):
    add_domain(tmp_path, "first.example.com")
    add_domain(tmp_path, "second.example.com")

    mark_verified(tmp_path, "first.example.com")

    records = {
        record["hostname"]: record
        for record in list_domains(tmp_path)
    }

    assert records["first.example.com"]["status"] == "verified"
    assert records["first.example.com"]["verified_at"] is not None
    assert records["second.example.com"]["status"] == "pending_verification"


def test_cannot_verify_unregistered_domain(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        mark_verified(tmp_path, "unknown.example.com")


def test_storage_file_permissions(tmp_path):
    add_domain(tmp_path, "training.example.com")

    path = tmp_path / "managed_domains.json"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 1
