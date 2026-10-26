"""Free list-hygiene engine: verdicts from syntax + DNS + disposable + role,
with no mail sent and no verifier credit spent."""

import pytest

from harvey import listclean
from harvey.listclean import Verdict, summarize, sweep, verdict_for


def test_plain_mailbox_is_ok():
    assert verdict_for("jane@acme.com", "mx").status == "ok"
    assert verdict_for("jane@acme.com", "a").status == "ok"


def test_dead_domain_is_invalid():
    v = verdict_for("jane@acme.com", "none")
    assert v.status == "invalid"
    assert "no mail server" in v.reason


def test_lookup_error_is_unknown_not_removed():
    v = verdict_for("jane@acme.com", "error")
    assert v.status == "unknown"
    assert v.status not in listclean.REMOVE


def test_bad_syntax_is_invalid():
    assert verdict_for("not-an-email", "mx").status == "invalid"
    assert verdict_for("jane@", "mx").status == "invalid"
    assert verdict_for("jane@nodot", "mx").status == "invalid"
    assert verdict_for("", "mx").status == "invalid"


def test_disposable_domain_is_invalid_even_with_mx():
    # Burner providers have real MX records; the disposable list must win.
    v = verdict_for("someone@mailinator.com", "mx")
    assert v.status == "invalid"
    assert "disposable" in v.reason


def test_role_address_is_risky_and_kept():
    v = verdict_for("info@acme.com", "mx")
    assert v.status == "risky"
    assert v.status not in listclean.REMOVE
    assert verdict_for("sales@acme.com", "mx").status == "risky"


def test_dead_domain_beats_role():
    # A role address on a dead domain is still removed.
    assert verdict_for("info@acme.com", "none").status == "invalid"


def test_normalizes_case():
    assert verdict_for("Jane@ACME.com", "mx").status == "ok"


def test_only_invalid_is_removed():
    assert "invalid" in listclean.REMOVE
    for keep in ("ok", "risky", "unknown"):
        assert keep not in listclean.REMOVE


def test_summarize_counts():
    counts = summarize([
        Verdict("ok", ""), Verdict("ok", ""), Verdict("risky", ""),
        Verdict("invalid", ""), Verdict("unknown", ""),
    ])
    assert counts == {"ok": 2, "risky": 1, "invalid": 1, "unknown": 1}


@pytest.mark.asyncio
async def test_sweep_classifies_a_whole_list(monkeypatch):
    async def fake_lookup(domains):
        table = {"acme.com": ("mx", "mail.acme.com"), "dead.tld": ("none", None)}
        return {d: table.get(d, ("error", None)) for d in dict.fromkeys(domains)}

    monkeypatch.setattr(listclean, "_lookup_many", fake_lookup)

    out = await sweep([
        "jane@acme.com",      # ok
        "info@acme.com",      # risky (role)
        "ghost@dead.tld",     # invalid (dead domain)
        "burner@mailinator.com",  # invalid (disposable, not even looked up)
        "garbage",            # invalid (syntax)
        "JANE@ACME.COM",      # dedups with jane@acme.com after lower-casing
    ])

    assert out["jane@acme.com"].status == "ok"
    assert out["info@acme.com"].status == "risky"
    assert out["ghost@dead.tld"].status == "invalid"
    assert out["burner@mailinator.com"].status == "invalid"
    assert out["garbage"].status == "invalid"
    # Normalized key, single entry.
    assert "JANE@ACME.COM" not in out
    assert out["jane@acme.com"].status == "ok"

    counts = summarize(out.values())
    assert counts["invalid"] == 3
    assert counts["ok"] == 1
    assert counts["risky"] == 1


def test_reserved_tld_is_kept_not_removed():
    # RFC 6761 reserved/test TLDs are synthetic, never a real dead domain —
    # even with a 'none' DNS answer they must not be removed.
    for addr in ("sam@foo.example", "lee@host.test", "mo@a.invalid"):
        v = verdict_for(addr, "none")
        assert v.status == "unknown"
        assert v.status not in listclean.REMOVE
