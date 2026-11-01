"""Regression tests for concurrent workspace storage updates."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from bb.domain_store import add_domain, list_domains, mark_verified
from bb.simulation_link_store import create_link, list_links, set_link_status


def _parallel(functions):
    barrier = Barrier(len(functions))

    def execute(function):
        barrier.wait(timeout=10)
        return function()

    with ThreadPoolExecutor(max_workers=len(functions)) as executor:
        return list(executor.map(execute, functions))


def test_concurrent_domain_creates_preserve_all_records(tmp_path):
    count = 12

    _parallel([
        lambda i=i: add_domain(tmp_path, f"domain{i}.example.com")
        for i in range(count)
    ])

    records = list_domains(tmp_path)

    assert len(records) == count
    assert len({record["hostname"] for record in records}) == count


def test_concurrent_domain_verifications_preserve_all_updates(tmp_path):
    count = 12

    for i in range(count):
        add_domain(tmp_path, f"domain{i}.example.com")

    _parallel([
        lambda i=i: mark_verified(tmp_path, f"domain{i}.example.com")
        for i in range(count)
    ])

    records = list_domains(tmp_path)

    assert len(records) == count
    assert all(record["status"] == "verified" for record in records)


def test_concurrent_link_creates_preserve_all_records(tmp_path):
    count = 12

    _parallel([
        lambda i=i: create_link(
            tmp_path,
            name=f"Training {i}",
            hostname="training.example.com",
            path=f"campaign-{i}",
            destination="https://approved.example.com/training",
            allowed_hostnames=["training.example.com"],
            approved_destination_hostnames=["approved.example.com"],
        )
        for i in range(count)
    ])

    records = list_links(tmp_path)

    assert len(records) == count
    assert len({record["path"] for record in records}) == count


def test_concurrent_link_status_updates_preserve_all_changes(tmp_path):
    count = 12

    ids = [
        create_link(
            tmp_path,
            name=f"Training {i}",
            hostname="training.example.com",
            path=f"campaign-{i}",
            destination="https://approved.example.com/training",
            allowed_hostnames=["training.example.com"],
            approved_destination_hostnames=["approved.example.com"],
        )["id"]
        for i in range(count)
    ]

    _parallel([
        lambda link_id=link_id: set_link_status(
            tmp_path, link_id, "disabled"
        )
        for link_id in ids
    ])

    records = list_links(tmp_path)

    assert len(records) == count
    assert all(record["status"] == "disabled" for record in records)
