"""`harvey verify/clean --csv`: free-check a CSV file and split it into kept
and invalid rows, preserving the original columns."""

import csv

import pytest

from harvey import cli, listclean


@pytest.mark.asyncio
async def test_clean_csv_splits_rows(tmp_path, monkeypatch):
    async def fake_lookup(domains):
        table = {"acme.com": ("mx", "m"), "dead.tld": ("none", None)}
        return {d: table.get(d, ("error", None)) for d in dict.fromkeys(domains)}
    monkeypatch.setattr(listclean, "_lookup_many", fake_lookup)

    p = tmp_path / "list.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "email", "company"])
        w.writerow(["Jane", "jane@acme.com", "Acme"])       # ok
        w.writerow(["Info", "info@acme.com", "Acme"])       # role -> risky (kept)
        w.writerow(["Ghost", "ghost@dead.tld", "Gone"])     # dead domain -> removed
        w.writerow(["Burn", "b@mailinator.com", "X"])       # disposable -> removed
        w.writerow(["Bad", "not-an-email", "Y"])            # bad syntax -> removed

    report = await cli._clean_csv(str(p), apply=True)
    assert report["column"] == "email"
    assert report["checked"] == 5
    assert report["counts"]["invalid"] == 3 and report["counts"]["ok"] == 1 and report["counts"]["risky"] == 1
    assert report["kept"] == 2 and report["removable"] == 3

    cleaned = list(csv.DictReader(open(report["cleaned_path"])))
    assert {r["email"] for r in cleaned} == {"jane@acme.com", "info@acme.com"}
    # Original columns preserved.
    assert set(cleaned[0].keys()) == {"name", "email", "company"}

    invalid = list(csv.DictReader(open(report["invalid_path"])))
    assert {r["email"] for r in invalid} == {"ghost@dead.tld", "b@mailinator.com", "not-an-email"}
    assert all(r["clean_reason"] for r in invalid)


@pytest.mark.asyncio
async def test_clean_csv_detects_email_column_by_values(tmp_path, monkeypatch):
    async def fake_lookup(domains):
        return {d: ("mx", "m") for d in dict.fromkeys(domains)}
    monkeypatch.setattr(listclean, "_lookup_many", fake_lookup)

    p = tmp_path / "x.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["who", "contact_point"])   # no column literally named "email"
        w.writerow(["A", "a@acme.com"])

    report = await cli._clean_csv(str(p), apply=False)
    assert report["column"] == "contact_point" and report["checked"] == 1
    assert "cleaned_path" not in report   # report-only


@pytest.mark.asyncio
async def test_clean_csv_missing_file():
    report = await cli._clean_csv("/no/such/file.csv", apply=False)
    assert "no such file" in report["error"]
