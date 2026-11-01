"""Read-only DNS ownership verification for BB."""

import json
import urllib.error
import urllib.parse
import urllib.request

from bb.domains import verification_record


def verify_domain(record, timeout=8):
    """Check whether the expected DNS TXT verification value is published."""

    expected = verification_record(
        record["hostname"],
        record["challenge"],
    )

    hostname = expected["name"]
    value = expected["value"]

    query = urllib.parse.urlencode({
        "name": hostname,
        "type": "TXT",
    })

    url = "https://cloudflare-dns.com/dns-query?" + query

    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/dns-json",
            "User-Agent": "BB-Domain-Verification/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return {
            "verified": False,
            "status": "lookup_error",
            "message": "DNS lookup failed. Please try again.",
        }

    if payload.get("Status") != 0:
        return {
            "verified": False,
            "status": "dns_not_ready",
            "message": "The DNS record is not available yet.",
        }

    answers = payload.get("Answer") or []

    for answer in answers:
        if answer.get("type") != 16:
            continue

        # DNS JSON responses may quote TXT strings.
        actual = str(answer.get("data", "")).strip()

        if actual.startswith('"') and actual.endswith('"'):
            actual = actual[1:-1]

        if actual == value:
            return {
                "verified": True,
                "status": "verified",
                "message": "Domain ownership verified successfully.",
            }

    return {
        "verified": False,
        "status": "record_not_found",
        "message": "The expected TXT verification record was not found.",
    }
