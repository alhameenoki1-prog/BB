"""Regression tests for DNS domain ownership verification."""

import io
import json
from unittest.mock import patch
from urllib.error import URLError

from bb.domain_verification import verify_domain
from bb.domains import verification_record


CHALLENGE = "a" * 64
DOMAIN = "training.example.com"
RECORD = {"hostname": DOMAIN, "challenge": CHALLENGE}


def response_with_answers(answers, status=0):
    return io.BytesIO(
        json.dumps({"Status": status, "Answer": answers}).encode()
    )


def test_matching_txt_record_verifies():
    expected = verification_record(DOMAIN, CHALLENGE)

    answers = [{
        "type": 16,
        "data": '"' + expected["value"] + '"',
    }]

    with patch(
        "bb.domain_verification.urllib.request.urlopen",
        return_value=response_with_answers(answers),
    ) as mocked:
        result = verify_domain(RECORD)

    assert result["verified"] is True
    assert result["status"] == "verified"

    request = mocked.call_args.args[0]
    assert expected["name"] in request.full_url


def test_wrong_txt_value_does_not_verify():
    answers = [{
        "type": 16,
        "data": '"bb-domain-verification=incorrect"',
    }]

    with patch(
        "bb.domain_verification.urllib.request.urlopen",
        return_value=response_with_answers(answers),
    ):
        result = verify_domain(RECORD)

    assert result["verified"] is False
    assert result["status"] == "record_not_found"


def test_unrelated_dns_record_does_not_verify():
    expected = verification_record(DOMAIN, CHALLENGE)

    answers = [{
        "type": 1,
        "data": expected["value"],
    }]

    with patch(
        "bb.domain_verification.urllib.request.urlopen",
        return_value=response_with_answers(answers),
    ):
        result = verify_domain(RECORD)

    assert result["verified"] is False


def test_dns_error_does_not_verify():
    with patch(
        "bb.domain_verification.urllib.request.urlopen",
        side_effect=URLError("Network unavailable"),
    ):
        result = verify_domain(RECORD)

    assert result["verified"] is False
    assert result["status"] == "lookup_error"


def test_dns_nonzero_status_does_not_verify():
    with patch(
        "bb.domain_verification.urllib.request.urlopen",
        return_value=response_with_answers([], status=3),
    ):
        result = verify_domain(RECORD)

    assert result["verified"] is False
    assert result["status"] == "dns_not_ready"
