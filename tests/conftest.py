"""Shared test setup.

No test reaches real DNS: the domain lookup the list check uses is
stubbed to "a mail server exists" for every domain, so test addresses at
.example domains are not marked invalid before the code under test sees
them. A test that wants a particular answer monkeypatches it again.
"""
import pytest


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    from harvey import listcheck

    async def lookup(domain):
        return "mx", f"mx.{domain}"

    monkeypatch.setattr(listcheck, "lookup_domain", lookup)
