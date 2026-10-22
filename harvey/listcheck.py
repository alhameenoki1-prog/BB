"""Checking an uploaded list before it is sent to.

Uploaded addresses are sent as given ("provided"). When a verifier key is
available (Reoon, ZeroBounce or Hunter in the environment) each new
address is checked first: a nonexistent one is marked invalid and never
written to, a confirmed one verified. A catch-all domain (the server
accepts every name, so nothing can be proven either way) is kept as
given and noted. A domain that does not exist, or publishes no mail
server, is invalid without spending a credit; a DNS lookup that merely
failed (timeout, resolver down) proves nothing and is tried again next
cycle. Inconclusive verifier answers are noted so an address is not
checked every cycle.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from harvey.integrations import email_finder

logger = logging.getLogger("harvey.listcheck")

VERIFY_PER_CYCLE = 150
INCONCLUSIVE_NOTE = "Verification: inconclusive"
CATCH_ALL_NOTE = "Verification: catch-all domain"
DOMAIN_NOTE = "Domain check"


def verifier_configured(env) -> bool:
    return bool(getattr(env, "reoon_api_key", "") or getattr(env, "zerobounce_api_key", "")
                or email_finder._hunter_api_key())


def mx_type_of(mx_host: str | None) -> str:
    """Which verifier path fits the domain's mail host. Anything unknown is
    treated as a gateway so only the API verifiers run: an SMTP probe from
    a laptop is slow and usually blocked."""
    host = (mx_host or "").lower()
    if "google" in host:
        return "google"
    if "outlook" in host or "microsoft" in host:
        return "microsoft"
    return "gateway"


def _lookup_domain_sync(domain: str) -> tuple[str, str | None]:
    """What DNS says about where mail for ``domain`` goes.

    ("mx", host)   a mail server is published;
    ("a", None)    no MX but the name resolves, so mail falls back to it;
    ("none", None) the domain does not exist, or publishes a null MX:
                   nothing could ever be delivered there;
    ("error", None) the lookup itself failed (timeout, resolver down):
                   proves nothing, try again later.
    """
    try:
        import dns.resolver
    except ImportError:
        return "error", None
    try:
        answers = dns.resolver.resolve(domain, "MX")
        records = sorted(answers, key=lambda x: x.preference)
        host = str(records[0].exchange).rstrip(".") if records else ""
        return ("mx", host) if host else ("none", None)
    except dns.resolver.NXDOMAIN:
        return "none", None
    except dns.resolver.NoAnswer:
        pass
    except Exception as e:
        logger.debug(f"listcheck: MX lookup for {domain} failed: {e}")
        return "error", None
    try:
        dns.resolver.resolve(domain, "A")
        return "a", None
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return "none", None
    except Exception as e:
        logger.debug(f"listcheck: A lookup for {domain} failed: {e}")
        return "error", None


async def lookup_domain(domain: str) -> tuple[str, str | None]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _lookup_domain_sync, domain)


async def check_domains(state, limit: int = VERIFY_PER_CYCLE) -> dict:
    """The check that needs no key: does each uploaded address's domain
    exist and take mail? A dead domain is marked invalid before anything
    is written to it (the mail server would refuse it at send time
    anyway, as a bounce that counts against the mailbox). Each address is
    looked at once; a lookup that failed is tried again next cycle."""
    counts = {"checked": 0, "invalid": 0, "deferred": 0}
    prospects = await state.get_prospects_by_status("new", limit=limit * 3, email_statuses=("provided",))
    domains: dict[str, tuple[str, str | None]] = {}
    today = datetime.now(timezone.utc).date().isoformat()
    for p in prospects:
        if counts["checked"] >= limit:
            break
        notes = p.personalization_notes or ""
        if DOMAIN_NOTE in notes or INCONCLUSIVE_NOTE in notes or CATCH_ALL_NOTE in notes or not p.email or "@" not in p.email:
            continue
        counts["checked"] += 1
        domain = p.email.rsplit("@", 1)[-1].lower()
        if domain not in domains:
            try:
                domains[domain] = await lookup_domain(domain)
            except Exception as e:
                logger.debug(f"listcheck: {domain}: {e}")
                domains[domain] = ("error", None)
        where, _mx = domains[domain]
        if where == "error":
            counts["deferred"] += 1
            continue
        if where == "none":
            counts["invalid"] += 1
            await state.update_prospect_email(p.id, p.email, "invalid")
            note = f"{DOMAIN_NOTE} ({today}): {domain} has no mail server, so nothing could be delivered there; never sent."
        else:
            note = f"{DOMAIN_NOTE} ({today}): ok."
        try:
            await state.update_prospect_notes(p.id, (notes.rstrip() + "\n" + note).strip())
        except Exception:
            pass
    if counts["invalid"]:
        logger.warning(f"listcheck: {counts['invalid']} uploaded address(es) are at domains with no mail server; "
                       "marked invalid and never sent.")
    return counts


async def verify_uploaded(state, env, limit: int = VERIFY_PER_CYCLE) -> dict:
    """Check up to ``limit`` uploaded, unchecked addresses. Returns counts.

    Only a sure answer changes an address: verified or invalid. A
    catch-all stays "provided" with a note (it sends, like every uploaded
    address); an inconclusive verifier answer is noted; a failed DNS
    lookup is left alone and retried next cycle.
    """
    counts = {"verified": 0, "invalid": 0, "risky": 0, "unknown": 0, "deferred": 0, "checked": 0}
    prospects = await state.get_prospects_by_status("new", limit=limit * 2, email_statuses=("provided",))
    domains: dict[str, tuple[str, str | None]] = {}
    for p in prospects:
        if counts["checked"] >= limit:
            break
        notes = p.personalization_notes or ""
        if INCONCLUSIVE_NOTE in notes or CATCH_ALL_NOTE in notes or not p.email or "@" not in p.email:
            continue
        counts["checked"] += 1
        domain = p.email.rsplit("@", 1)[-1].lower()
        if domain not in domains:
            try:
                domains[domain] = await lookup_domain(domain)
            except Exception as e:
                logger.debug(f"listcheck: {domain}: {e}")
                domains[domain] = ("error", None)
        where, mx = domains[domain]
        if where == "error":
            counts["deferred"] += 1       # nothing learned; next cycle asks again
            continue
        if where == "none":
            status = "invalid"            # no such domain, or it takes no mail
        else:
            try:
                status, _catch_all = await email_finder._verify_candidate(p.email, mx_type_of(mx), env)
            except Exception as e:
                logger.debug(f"listcheck: {p.email}: {e}")
                status = None
        today = datetime.now(timezone.utc).date().isoformat()
        if status in ("verified", "invalid"):
            await state.update_prospect_email(p.id, p.email, status)
            counts[status] += 1
            continue
        if status == "risky":
            counts["risky"] += 1
            note = f"{CATCH_ALL_NOTE} ({today}): the server accepts every name, so this one could not be proven; sent as given."
        else:
            counts["unknown"] += 1
            note = f"{INCONCLUSIVE_NOTE} ({today}); sent as given."
        try:
            await state.update_prospect_notes(p.id, (notes.rstrip() + "\n" + note).strip())
        except Exception:
            pass
    if counts["checked"]:
        logger.info(f"listcheck: {counts['checked']} uploaded address(es) checked: {counts['verified']} verified, "
                    f"{counts['invalid']} invalid (never sent), {counts['risky']} catch-all (sent as given), "
                    f"{counts['unknown']} inconclusive, {counts['deferred']} to retry.")
    return counts
