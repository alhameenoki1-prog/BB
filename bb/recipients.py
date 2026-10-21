"""Recipient upload: a CSV (or pasted lines) becomes prospects.

Each row needs an email; a name, company, title, LinkedIn URL, website and
notes make the email better. Addresses the user supplies are stored with
``email_status = "provided"`` — deliverability is their call, so Harvey
writes to them and sends — and the company's website (or the email's
domain when it isn't a public mailbox) gives the free profiler something to
read, which is the "background check" the first line of each email draws on.
"""

import csv
import io
import re
from urllib.parse import urlparse

from harvey.models.company import Company
from harvey import names
from harvey.models.prospect import Prospect

MAX_ROWS = 10_000
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Public mailbox providers: an address there says nothing about a company.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "hotmail.co.uk",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "proton.me", "pm.me", "gmx.com", "gmx.de", "mail.com", "zoho.com",
    "yandex.com", "yandex.ru", "fastmail.com", "hey.com", "qq.com", "163.com", "126.com",
}

COLUMN_ALIASES = {
    "email": "email", "e-mail": "email", "email_address": "email", "mail": "email",
    "first_name": "first_name", "firstname": "first_name", "first": "first_name", "given_name": "first_name",
    "last_name": "last_name", "lastname": "last_name", "last": "last_name", "surname": "last_name",
    "family_name": "last_name",
    "name": "name", "full_name": "name", "fullname": "name", "contact": "name", "person": "name",
    "company": "company", "company_name": "company", "organization": "company", "organisation": "company",
    "employer": "company", "business": "company",
    "title": "title", "job_title": "title", "role": "title", "position": "title", "job": "title",
    "linkedin": "linkedin", "linkedin_url": "linkedin", "profile": "linkedin", "linkedin_profile": "linkedin",
    "website": "website", "url": "website", "domain": "website", "site": "website", "web": "website",
    "notes": "notes", "note": "notes", "context": "notes", "comment": "notes", "comments": "notes",
    "why": "notes", "reason": "notes",
    "industry": "industry", "sector": "industry", "category": "industry", "business_type": "industry",
    "type": "industry", "trade": "industry", "city": "city", "location": "city", "town": "city",
}


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def parse_rows(text: str) -> list[dict]:
    """CSV text (with or without a header row) or one address per line →
    normalised rows: email, first_name, last_name, company, title,
    linkedin, website, notes. Blank rows are dropped; nothing is validated."""
    text = (text or "").lstrip("﻿")
    if not text.strip():
        return []
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return []
    header = [_norm_header(c) for c in rows[0]]
    has_header = any(COLUMN_ALIASES.get(h) == "email" for h in header) or not any("@" in c for c in rows[0])
    if has_header:
        keys = [COLUMN_ALIASES.get(h, "") for h in header]
        body = rows[1:]
    else:
        # Headerless: the column holding addresses is the email column, the
        # rest ride along as notes.
        email_col = next((i for i, c in enumerate(rows[0]) if "@" in c), 0)
        keys = ["notes"] * len(rows[0])
        keys[email_col] = "email"
        body = rows
    out = []
    for raw in body:
        row = {"email": "", "first_name": "", "last_name": "", "name": "", "company": "",
               "title": "", "linkedin": "", "website": "", "notes": ""}
        for key, value in zip(keys, raw):
            value = (value or "").strip()
            if not key or not value:
                continue
            row[key] = (row[key] + " " + value).strip() if key == "notes" and row[key] else value
        if row["name"] and not (row["first_name"] or row["last_name"]):
            first, _, last = row.pop("name").partition(" ")
            row["first_name"], row["last_name"] = first, last.strip()
        row.pop("name", None)
        out.append(row)
    return out


def _domain_from(website: str, email: str) -> str:
    site = (website or "").strip().lower()
    if site:
        if "://" not in site:
            site = "https://" + site
        host = urlparse(site).netloc.split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if host:
            return host
    domain = email.rsplit("@", 1)[-1].lower()
    return "" if domain in FREE_MAIL_DOMAINS else domain


async def confirm_free_signals(state) -> int:
    """Turn on every signal that costs nothing (what a company's own
    website, sitemap, robots.txt, careers and team pages say), so uploaded
    companies are researched before their email is written. The CLI asks
    the operator to pick signals one by one; a web user has no signals
    page, and nothing here spends money. Signals a user already decided
    on are left alone. Returns how many were confirmed now."""
    from harvey.signals import seed_signal_catalog

    await seed_signal_catalog(state)
    changed = 0
    for sig in await state.get_signal_codes():
        if sig["status"] == "proposed" and (sig["cost_note"] or "").lower().startswith("free"):
            if await state.set_signal_status(sig["code"], "confirmed"):
                changed += 1
    return changed


async def import_rows(state, rows: list[dict]) -> dict:
    """Insert rows as companies + prospects. Returns counts."""
    result = {"added": 0, "duplicates": 0, "invalid": 0, "too_many": 0}
    if len(rows) > MAX_ROWS:
        result["too_many"] = len(rows) - MAX_ROWS
        rows = rows[:MAX_ROWS]
    await state.init_db()
    await confirm_free_signals(state)
    # Addresses already on the list count as duplicates whatever the schema
    # enforces; one query up front keeps a 10,000-row upload cheap.
    seen: set[str] = set()
    async with state._connect() as db:
        async with db.execute("SELECT email FROM prospects WHERE email != ''") as cur:
            seen.update(r[0].lower() for r in await cur.fetchall())
    for row in rows:
        email = (row.get("email") or "").strip().lower()
        if not EMAIL_RE.match(email):
            result["invalid"] += 1
            continue
        if email in seen:
            result["duplicates"] += 1
            continue
        seen.add(email)
        domain = _domain_from(row.get("website", ""), email)
        company_name = (row.get("company") or "").strip() or names.company_display_name("", domain)
        company_id = ""
        if domain or company_name:
            company = Company(
                name=company_name, domain=domain,
                website=f"https://{domain}" if domain else "",
                external_id="" if domain else f"upload:{company_name.lower()}",
                source="upload",
            )
            company_id = await state.add_company(company)
        notes = []
        if row.get("notes"):
            notes.append(row["notes"])
        if row.get("industry"):
            notes.append(f"Sector given in the upload: {row['industry']}")
        if row.get("city"):
            notes.append(f"Location: {row['city']}")
        if row.get("linkedin"):
            notes.append(f"LinkedIn: {row['linkedin']}")
        resolved = names.resolve_name(row.get("first_name", ""), row.get("last_name", ""),
                                      email, row.get("linkedin", ""))
        if resolved["source"] == "linkedin":
            notes.append("Name taken from the LinkedIn URL; the row gave none.")
        elif resolved["source"] == "email":
            notes.append("Name read from the email address (first.last pattern); the row gave none.")
        elif resolved["source"] == "role":
            notes.append(f"Shared inbox ({email.split('@')[0]}@): address the team, not a person.")
        elif resolved["source"] == "":
            notes.append("No name was given; open without one rather than guessing.")
        prospect = Prospect(
            company_id=company_id,
            first_name=resolved["first_name"],
            last_name=resolved["last_name"],
            email=email,
            email_status="provided",
            linkedin_url=(row.get("linkedin") or "").strip(),
            title=(row.get("title") or "").strip(),
            source="upload",
            status="new",
            personalization_notes="\n".join(notes),
            company=company_name,
        )
        # add_prospect resolves a conflict to the existing row's id (on the
        # object itself), so remember the id we asked for.
        new_id = prospect.id = prospect.id or __import__("secrets").token_hex(8)
        if await state.add_prospect(prospect) == new_id:
            result["added"] += 1
        else:
            result["duplicates"] += 1
    return result
