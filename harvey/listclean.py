"""Free list hygiene: decide, without spending a verifier credit, whether an
address is worth keeping.

What it can prove for free, reliably:
  * the address is malformed             -> invalid
  * the domain publishes no mail server  -> invalid (nothing could ever arrive)
  * the domain is a disposable/throwaway  -> invalid (a burner, not a real lead)
And what it flags but keeps:
  * a role address (info@, sales@)        -> risky (deliverable, just not a
                                             named person)
  * every other deliverable domain        -> ok (the domain takes mail; the
                                             exact mailbox cannot be proven for
                                             free — Gmail and Microsoft 365
                                             accept every name from outside, so
                                             only a paid verifier can test a
                                             single mailbox)

Only an ``invalid`` verdict is removed, and removal means "move to the
Excluded list" (reversible), never a delete. This is the reputation-safe
sweep: it drops the provably dead and keeps everything it cannot disprove.

The verdict strings line up with ``harvey`` email_status vocabulary
(verified / risky / guess / invalid). ``ok`` maps to a kept, deliverable
address; ``unknown`` means the domain lookup itself failed and nothing was
proven, so the address is left exactly as it was and tried again later.

Adding a verifier key later does not replace this — ``harvey.listcheck``
already runs the hosted verifiers (Reoon / ZeroBounce / Hunter) for the one
thing free checks cannot do: prove a single mailbox is live on a catch-all
provider.
"""

from __future__ import annotations

import re
from collections import namedtuple

from harvey.listcheck import _lookup_many

# Deliberately a touch stricter than a send-time check: a lead list is worth
# cleaning hard. Requires a dotted domain and a 2+ letter TLD.
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

Verdict = namedtuple("Verdict", "status reason")

# Verdicts that mean "take this one out of the sending pool". Only the
# provably-dead; a catch-all or an unproven mailbox is never in here.
REMOVE = {"invalid"}

# Role / generic mailboxes: deliverable, but not a named person. Kept as
# 'risky' so it is the user's call, never auto-removed.
ROLE_LOCAL_PARTS = {
    "info", "sales", "support", "admin", "administrator", "contact", "hello",
    "help", "office", "mail", "email", "team", "enquiries", "inquiries", "enquiry",
    "inquiry", "marketing", "noreply", "no-reply", "donotreply", "do-not-reply",
    "billing", "accounts", "accounting", "finance", "hr", "jobs", "careers",
    "recruitment", "press", "media", "legal", "webmaster", "postmaster", "abuse",
    "privacy", "security", "feedback", "service", "customerservice", "orders",
    "order", "booking", "bookings", "reservations", "general", "reception",
    "frontdesk", "hi", "ask", "newsletter", "subscribe", "unsubscribe", "notifications",
}

# Reserved / special-use TLDs (RFC 2606, RFC 6761): never real deliverable
# domains, but synthetic placeholders, not addresses to "remove" — a list
# that uses them is a test or a sample. Left as-is rather than called dead.
RESERVED_TLDS = {"example", "invalid", "test", "localhost"}

# Disposable / throwaway providers: a burner address, never a real lead.
# Not exhaustive (there are thousands) — the common ones that actually show
# up in bought or scraped lists. Extend freely.
DISPOSABLE_DOMAINS = {
    "mailinator.com", "mailinator.net", "mailinator2.com", "guerrillamail.com",
    "guerrillamail.net", "guerrillamail.org", "guerrillamail.biz", "guerrillamail.info",
    "guerrillamail.de", "guerrillamailblock.com", "sharklasers.com", "grr.la", "spam4.me",
    "10minutemail.com", "10minutemail.net", "10minmail.com", "20minutemail.com",
    "temp-mail.org", "temp-mail.io", "tempmail.com", "tempmail.net", "tempmailo.com",
    "tempr.email", "tempmailaddress.com", "tmpmail.org", "tmpmail.net", "tmail.com",
    "tmails.net", "throwawaymail.com", "throwaway.email", "trashmail.com", "trashmail.net",
    "trash-mail.com", "trashmail.de", "yopmail.com", "yopmail.net", "yopmail.fr",
    "getnada.com", "nada.email", "dispostable.com", "maildrop.cc", "mailcatch.com",
    "mohmal.com", "fakeinbox.com", "fakemailgenerator.com", "emailondeck.com",
    "mintemail.com", "mytemp.email", "discard.email", "discardmail.com", "discardmail.de",
    "inboxkitten.com", "33mail.com", "spambog.com", "spambog.de", "spambog.ru",
    "mailnesia.com", "mailnull.com", "spamgourmet.com", "incognitomail.com", "incognitomail.org",
    "deadaddress.com", "meltmail.com", "kasmail.com", "spamfree24.org", "mailexpire.com",
    "tempemail.net", "moakt.com", "moakt.cc", "tempinbox.com", "burnermail.io",
    "pokemail.net", "trbvm.com", "mvrht.com", "nwytg.com", "tmpeml.com", "cdfaq.com",
    "armyspy.com", "cuvox.de", "dayrep.com", "einrot.com", "fleckens.hu", "gustr.com",
    "jourrapide.com", "rhyta.com", "superrito.com", "teleworm.us", "emltmp.com",
    "maileater.com", "harakirimail.com", "spamherelots.com", "wegwerfmail.de",
    "wegwerfmail.net", "wegwerfmail.org", "byom.de", "mail-temp.com", "luxusmail.org",
    "mailsac.com", "throwam.com", "33m.co", "tafmail.com", "crazymailing.com",
    "emailfake.com", "emailtemporario.com.br", "fakemail.net", "getairmail.com",
    "mailforspam.com", "mailtemp.net", "mytrashmail.com", "spamex.com", "spaml.com",
}


def _domain_of(email: str) -> str:
    email = (email or "").strip().lower()
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def verdict_for(email: str, where: str) -> Verdict:
    """The verdict for one address, given what DNS said about its domain
    (``where`` from ``listcheck.lookup_domain``: mx / a / none / error).

    Pure: all the DNS work is done up front by :func:`sweep`, so this is
    trivially testable.
    """
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        return Verdict("invalid", "not a valid email address")
    local, _, domain = email.partition("@")
    if domain.rsplit(".", 1)[-1] in RESERVED_TLDS:
        return Verdict("unknown", "a reserved or test domain (RFC 6761); left as-is")
    if domain in DISPOSABLE_DOMAINS:
        return Verdict("invalid", "disposable / throwaway email domain")
    if where == "none":
        return Verdict("invalid", "the domain has no mail server, so nothing can be delivered")
    if where == "error":
        return Verdict("unknown", "could not look up the domain just now; left as-is and retried later")
    # where in ("mx", "a"): the domain can receive mail.
    if local in ROLE_LOCAL_PARTS:
        return Verdict("risky", "a role address (shared mailbox like info@ or sales@), not a named person")
    return Verdict("ok", "the domain accepts mail; the exact mailbox can't be proven without a paid verifier")


def summarize(verdicts) -> dict:
    """Counts by status for a batch of :class:`Verdict` (or status strings)."""
    out = {"ok": 0, "risky": 0, "invalid": 0, "unknown": 0}
    for v in verdicts:
        status = v.status if isinstance(v, Verdict) else str(v)
        out[status] = out.get(status, 0) + 1
    return out


async def sweep(emails) -> dict:
    """Verdict for every address, one DNS lookup per distinct domain.

    Returns ``{email: Verdict}``. Addresses are lower-cased; duplicates
    collapse. Never sends mail and never spends a verifier credit.
    """
    emails = [(e or "").strip().lower() for e in emails if (e or "").strip()]
    domains = [d for d in (_domain_of(e) for e in emails) if d]
    looked = await _lookup_many(domains) if domains else {}
    out = {}
    for e in emails:
        where = looked.get(_domain_of(e), ("error", None))[0] if "@" in e else "none"
        out[e] = verdict_for(e, where)
    return out


async def clean_prospects(state, apply: bool = False, limit: int | None = None) -> dict:
    """Sweep every prospect's address and, when ``apply`` is set, move the
    provably-dead ones to the Excluded list. Reversible; never deletes.

    Returns a report — counts by verdict, how many were excluded this run,
    and a sample of what was removed — for the CLI and the web UI.
    """
    prospects = await state.get_prospects_with_email(limit=limit)
    verdicts = await sweep([p.email for p in prospects])
    counts = {"ok": 0, "risky": 0, "invalid": 0, "unknown": 0}
    excluded = 0
    removed = []
    for p in prospects:
        v = verdicts.get((p.email or "").strip().lower())
        if v is None:
            continue
        counts[v.status] = counts.get(v.status, 0) + 1
        if v.status in REMOVE:
            removed.append({"id": p.id, "email": p.email, "name": p.full_name(), "reason": v.reason})
            if apply:
                await state.exclude_prospect(p.id, v.reason)
                excluded += 1
    return {
        "checked": len(prospects),
        "counts": counts,
        "applied": apply,
        "excluded": excluded,
        "removable": len(removed),
        "removed": removed,
    }
