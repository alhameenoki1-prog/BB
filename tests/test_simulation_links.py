import pytest

from bb.simulation_links import (
    normalize_path,
    validate_destination,
    validate_name,
)


@pytest.mark.parametrize("value", [
    "training",
    "campaign/lesson-1",
    "awareness_2026",
])
def test_valid_paths(value):
    assert normalize_path(value) == value


@pytest.mark.parametrize("value", [
    "",
    "../admin",
    "training//lesson",
    "training?token=123",
])
def test_invalid_paths(value):
    with pytest.raises(ValueError):
        normalize_path(value)


def test_https_destination_accepted():
    url = "https://example.com/security-awareness"
    assert validate_destination(url) == url


@pytest.mark.parametrize("url", [
    "http://example.com",
    "https://user:password@example.com",
    "https://example.com:8080/",
    "javascript:alert(1)",
    "",
])
def test_invalid_destinations(url):
    with pytest.raises(ValueError):
        validate_destination(url)


def test_blank_name_rejected():
    with pytest.raises(ValueError):
        validate_name("")
