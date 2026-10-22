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



@pytest.fixture(autouse=True)
def _beat_in_tmp(monkeypatch, tmp_path):
    """A test that runs the real heartbeat writes its beat (and would take
    a wake) in its own folder, never in the checkout's data/ folder that a
    real `harvey run` uses."""
    from harvey import wake
    monkeypatch.setattr(wake, "data_dir", lambda: tmp_path / "harvey-data")
    yield
    wake._pending.clear()
    wake._folder["path"] = None
