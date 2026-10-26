"""The Excluded list on Harvey prospects: a free sweep moves provably-dead
addresses out of the sending pool, reversibly, and the writer-facing fetches
skip them."""

import pytest
import pytest_asyncio

from harvey import listclean
from harvey.models.prospect import Prospect
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "test.db"))
    await sm.init_db()
    return sm


def _fake_lookup():
    async def lookup(domains):
        table = {
            "acme.com": ("mx", "mail.acme.com"),
            "mailinator.com": ("mx", "mail.mailinator.com"),
            "dead.tld": ("none", None),
        }
        return {d: table.get(d, ("error", None)) for d in dict.fromkeys(domains)}
    return lookup


async def _seed(state):
    await state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="CEO", email="jane@acme.com"))
    await state.add_prospect(Prospect(first_name="Info", last_name="Desk", title="Team", email="info@acme.com"))
    await state.add_prospect(Prospect(first_name="Gone", last_name="Away", title="Mgr", email="x@dead.tld"))
    await state.add_prospect(Prospect(first_name="Burner", last_name="Acct", title="X", email="b@mailinator.com"))


@pytest.mark.asyncio
async def test_verify_reports_without_changing(state, monkeypatch):
    monkeypatch.setattr(listclean, "_lookup_many", _fake_lookup())
    await _seed(state)

    report = await listclean.clean_prospects(state, apply=False)

    assert report["checked"] == 4
    assert report["counts"]["ok"] == 1          # jane
    assert report["counts"]["risky"] == 1       # info@ (role)
    assert report["counts"]["invalid"] == 2     # dead domain + disposable
    assert report["removable"] == 2
    assert report["excluded"] == 0              # report only
    # Nothing was actually excluded.
    assert await state.count_prospects_excluded() == 0


@pytest.mark.asyncio
async def test_clean_moves_invalid_to_excluded_reversibly(state, monkeypatch):
    monkeypatch.setattr(listclean, "_lookup_many", _fake_lookup())
    await _seed(state)

    report = await listclean.clean_prospects(state, apply=True)
    assert report["excluded"] == 2

    # The two invalid ones are now excluded; the two good ones remain.
    assert await state.count_prospects_excluded() == 2
    kept = await state.get_prospects_with_email()
    kept_emails = {p.email for p in kept}
    assert kept_emails == {"jane@acme.com", "info@acme.com"}

    excluded = await state.get_excluded_prospects()
    assert {p.email for p in excluded} == {"x@dead.tld", "b@mailinator.com"}
    assert all(p.excluded and p.excluded_reason for p in excluded)


@pytest.mark.asyncio
async def test_excluded_are_hidden_from_the_writer_fetch(state, monkeypatch):
    monkeypatch.setattr(listclean, "_lookup_many", _fake_lookup())
    await _seed(state)
    await listclean.clean_prospects(state, apply=True)

    # The writer fetches 'new' prospects; excluded ones must not appear.
    visible = await state.get_prospects_by_status("new")
    assert {p.email for p in visible} == {"jane@acme.com", "info@acme.com"}

    # ...unless explicitly asked for.
    everything = await state.get_prospects_by_status("new", include_excluded=True)
    assert len(everything) == 4


@pytest.mark.asyncio
async def test_restore_returns_a_prospect_to_the_pool(state, monkeypatch):
    monkeypatch.setattr(listclean, "_lookup_many", _fake_lookup())
    await _seed(state)
    await listclean.clean_prospects(state, apply=True)

    gone = await state.get_excluded_prospects()
    victim = next(p for p in gone if p.email == "x@dead.tld")
    await state.restore_prospect(victim.id)

    assert await state.count_prospects_excluded() == 1
    back = await state.get_prospect(victim.id)
    assert back.excluded is False
    assert back.excluded_reason == ""
    visible = await state.get_prospects_by_status("new")
    assert "x@dead.tld" in {p.email for p in visible}
