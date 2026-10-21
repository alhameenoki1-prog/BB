"""Checking an uploaded list before it is sent to.

Uploaded addresses are sent as given ("provided"). When a verifier key is
available (Reoon, ZeroBounce or Hunter in the environment) each new
address is checked first: a nonexistent one is marked invalid and never
written to, a catch-all is marked risky, a confirmed one verified. A
domain with no mail server at all is invalid without spending a credit.
Inconclusive answers are noted so an address is not checked every cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from harvey.integrations import email_finder

logger = logging.getLogger("harvey.listcheck")

VERIFY_PER_CYCLE = 150
INCONCLUSIVE_NOTE = "Verification: inconclusive"


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


async def verify_uploaded(state, env, limit: int = VERIFY_PER_CYCLE) -> dict:
    """Check up to ``limit`` uploaded, unchecked addresses. Returns counts."""
    counts = {"verified": 0, "invalid": 0, "risky": 0, "unknown": 0, "checked": 0}
    prospects = await state.get_prospects_by_status("new", limit=limit * 2, email_statuses=("provided",))
    for p in prospects:
        if counts["checked"] >= limit:
            break
        if INCONCLUSIVE_NOTE in (p.personalization_notes or "") or not p.email or "@" not in p.email:
            continue
        counts["checked"] += 1
        domain = p.email.rsplit("@", 1)[-1].lower()
        try:
            mx = await email_finder.get_mx_host(domain)
        except Exception:
            mx = None
        if mx is None:
            status = "invalid"      # no mail server: nothing could ever be delivered there
        else:
            try:
                status, _catch_all = await email_finder._verify_candidate(p.email, mx_type_of(mx), env)
            except Exception as e:
                logger.debug(f"listcheck: {p.email}: {e}")
                status = None
        if status in ("verified", "invalid", "risky"):
            await state.update_prospect_email(p.id, p.email, status)
            counts[status] += 1
        else:
            counts["unknown"] += 1
            note = f"{INCONCLUSIVE_NOTE} ({datetime.now(timezone.utc).date().isoformat()}); sent as given."
            try:
                await state.update_prospect_notes(p.id, ((p.personalization_notes or "").rstrip() + "\n" + note).strip())
            except Exception:
                pass
    if counts["checked"]:
        logger.info(f"listcheck: {counts['checked']} uploaded address(es) checked: {counts['verified']} verified, "
                    f"{counts['invalid']} invalid (never sent), {counts['risky']} catch-all, {counts['unknown']} inconclusive.")
    return counts
