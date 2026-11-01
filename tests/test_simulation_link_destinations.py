import pytest

from bb.simulation_link_destinations import validate_approved_destination


def test_approved_destination():
    url = "https://training.example.com/awareness"
    assert validate_approved_destination(
        url, ["training.example.com"]
    ) == url


def test_approved_hostname_case_insensitive():
    assert validate_approved_destination(
        "https://training.example.com/lesson",
        ["TRAINING.EXAMPLE.COM"],
    ) == "https://training.example.com/lesson"


@pytest.mark.parametrize("url", [
    "https://unapproved.example.com/lesson",
    "https://training.example.com.evil.test/lesson",
    "https://evil.test/?next=training.example.com",
    "http://training.example.com/lesson",
    "https://user:password@training.example.com/",
])
def test_unapproved_or_invalid_destinations(url):
    with pytest.raises(ValueError):
        validate_approved_destination(
            url, ["training.example.com"]
        )


def test_empty_approval_list():
    with pytest.raises(ValueError):
        validate_approved_destination(
            "https://training.example.com/lesson", []
        )
