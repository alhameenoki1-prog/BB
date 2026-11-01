import pytest

from bb.simulation_link_store import create_link, list_links


def test_unapproved_destination_rejected(tmp_path):
    with pytest.raises(ValueError, match="approved"):
        create_link(
            tmp_path,
            name="Training",
            hostname="bb.example.com",
            path="lesson",
            destination="https://unapproved.example.org/lesson",
            allowed_hostnames=["bb.example.com"],
            approved_destination_hostnames=["training.example.com"],
        )

    assert list_links(tmp_path) == []


def test_empty_approval_list_rejected(tmp_path):
    with pytest.raises(ValueError, match="approved"):
        create_link(
            tmp_path,
            name="Training",
            hostname="bb.example.com",
            path="lesson",
            destination="https://training.example.com/lesson",
            allowed_hostnames=["bb.example.com"],
        )

    assert list_links(tmp_path) == []


def test_approved_destination_creates_record(tmp_path):
    record = create_link(
        tmp_path,
        name="Security Awareness",
        hostname="bb.example.com",
        path="awareness",
        destination="https://training.example.com/lesson",
        allowed_hostnames=["bb.example.com"],
        approved_destination_hostnames=["training.example.com"],
    )

    assert record["destination"] == "https://training.example.com/lesson"
    assert record["hostname"] == "bb.example.com"
    assert len(list_links(tmp_path)) == 1
