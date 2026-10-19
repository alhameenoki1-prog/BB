"""A plain-English check of the DNS records that decide whether a sending
domain's mail is trusted: SPF, DKIM (common selectors), DMARC and MX.

It cannot see a mailbox's reputation or a recipient's filters; it catches
the setup gaps that get mail blocked outright.
"""

from __future__ import annotations

import asyncio

PUBLIC_MAILBOX_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "yahoo.co.uk", "icloud.com", "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com",
}
DKIM_SELECTORS = ("google", "selector1", "selector2", "default", "k1", "zoho", "fm1", "fm2", "fm3",
                  "mail", "dkim", "s1", "s2")


def _txt(name: str) -> list[str]:
    import dns.resolver

    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=4.0)
    except Exception:
        return []
    out = []
    for rdata in answers:
        try:
            out.append(b"".join(rdata.strings).decode("utf-8", "replace"))
        except Exception:
            out.append(str(rdata).strip('"'))
    return out


def _has_mx(domain: str) -> bool:
    import dns.resolver

    try:
        return len(dns.resolver.resolve(domain, "MX", lifetime=4.0)) > 0
    except Exception:
        return False


def check_domain_sync(domain: str) -> dict:
    domain = (domain or "").strip().lower().rstrip(".")
    result = {"domain": domain, "public": domain in PUBLIC_MAILBOX_DOMAINS, "checks": [], "score": 0}
    if not domain:
        return result
    if result["public"]:
        result["checks"].append({
            "name": "Authentication", "ok": True,
            "detail": f"{domain} is a public mail service; Google or Microsoft sign your mail for you.",
            "fix": ("A free mailbox shares its reputation with millions of strangers and gets flagged "
                    "fast when sending cold email. A mailbox on your own domain (Google Workspace or "
                    "Microsoft 365, about $7 a month) delivers far better."),
        })
        result["score"] = 2
        return result

    spf = [t for t in _txt(domain) if t.lower().startswith("v=spf1")]
    result["checks"].append({
        "name": "SPF", "ok": bool(spf),
        "detail": spf[0][:120] if spf else "No SPF record found.",
        "fix": "" if spf else ("Add a TXT record at your domain: for Google Workspace "
                               "\"v=spf1 include:_spf.google.com ~all\"; for Microsoft 365 "
                               "\"v=spf1 include:spf.protection.outlook.com -all\"."),
    })
    dkim_found = ""
    for selector in DKIM_SELECTORS:
        records = _txt(f"{selector}._domainkey.{domain}")
        if any("v=dkim1" in r.lower() or "p=" in r.lower() for r in records):
            dkim_found = selector
            break
    result["checks"].append({
        "name": "DKIM", "ok": bool(dkim_found),
        "detail": f"Signing key found (selector '{dkim_found}')." if dkim_found
                  else "No DKIM key found at the usual selectors.",
        "fix": "" if dkim_found else ("Turn on DKIM at your mail provider and publish the key it gives "
                                      "you: Google Workspace → Admin console → Apps → Google Workspace → "
                                      "Gmail → Authenticate email; Microsoft 365 → Defender portal → Email "
                                      "authentication settings → DKIM. Without it, Gmail and Yahoo "
                                      "increasingly reject bulk mail."),
    })
    dmarc = [t for t in _txt(f"_dmarc.{domain}") if t.lower().startswith("v=dmarc1")]
    result["checks"].append({
        "name": "DMARC", "ok": bool(dmarc),
        "detail": dmarc[0][:120] if dmarc else "No DMARC record found.",
        "fix": "" if dmarc else ("Add a TXT record at _dmarc.<your domain>: "
                                 "\"v=DMARC1; p=none; rua=mailto:dmarc@<your domain>\". Gmail and Yahoo "
                                 "require DMARC for anyone sending in volume."),
    })
    mx = _has_mx(domain)
    result["checks"].append({
        "name": "Can receive mail (MX)", "ok": mx,
        "detail": "MX records present." if mx else "No MX records: replies and bounces cannot come back.",
        "fix": "" if mx else "Point the domain's MX records at your mail provider.",
    })
    result["score"] = sum(1 for c in result["checks"] if c["ok"])
    return result


async def check_domain(domain: str) -> dict:
    return await asyncio.to_thread(check_domain_sync, domain)


def domain_of(address: str) -> str:
    return (address or "").rsplit("@", 1)[-1].strip().lower() if "@" in (address or "") else ""
