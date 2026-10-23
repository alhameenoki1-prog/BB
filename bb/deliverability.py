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


# The mail a big provider sends comes from the provider's own IPs, which
# already have reverse DNS and a clean reputation; the IP checks are only
# worth running for a custom or self-hosted SMTP server.
MANAGED_SMTP_HOSTS = ("gmail.com", "googlemail.com", "google.com", "office365.com", "outlook.com",
                      "protection.outlook.com", "sendgrid.net", "amazonaws.com", "mailgun.org",
                      "sparkpostmail.com", "postmarkapp.com", "zoho.com", "zoho.eu", "mail.me.com",
                      "messagingengine.com", "mailjet.com", "sendinblue.com", "brevo.com", "smtp2go.com")
IP_BLOCKLISTS = (("Spamhaus ZEN", "zen.spamhaus.org"),)
DOMAIN_BLOCKLISTS = (("Spamhaus DBL", "dbl.spamhaus.org"),)


def _resolve_ips(host: str) -> list[str]:
    """The IPv4 addresses a host sends from (the host itself when it is
    already an IP)."""
    import ipaddress

    import dns.resolver
    host = (host or "").strip().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        return [str(r) for r in dns.resolver.resolve(host, "A", lifetime=4.0)]
    except Exception:
        return []


def _ptr(ip: str) -> str:
    """The reverse-DNS name of an IP, or "" when it has none."""
    import dns.resolver
    import dns.reversename
    try:
        rev = dns.reversename.from_address(ip)
        return str(dns.resolver.resolve(rev, "PTR", lifetime=4.0)[0]).rstrip(".")
    except Exception:
        return ""


def _dnsbl(value: str, zone: str, reverse_ip: bool) -> bool | None:
    """Is ``value`` (an IP, reversed, or a domain) listed on ``zone``?
    True listed, False not listed, None could not be checked (a blocklist
    that refuses queries from a shared DNS resolver answers with a
    127.255.255.x code, which is not a listing)."""
    import dns.resolver
    if reverse_ip:
        parts = value.split(".")
        if len(parts) != 4:
            return None                      # only IPv4 is checked here
        query = ".".join(reversed(parts)) + "." + zone
    else:
        query = value.strip(".") + "." + zone
    try:
        answers = [str(r) for r in dns.resolver.resolve(query, "A", lifetime=4.0)]
    except Exception as e:
        return False if e.__class__.__name__ == "NXDOMAIN" else None
    if not answers or any(a.startswith("127.255.255.") for a in answers):
        return None
    return True


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
    for label, zone in DOMAIN_BLOCKLISTS:
        listed = _dnsbl(domain, zone, reverse_ip=False)
        if listed is None:
            continue                         # could not check from here: say nothing rather than cry wolf
        result["checks"].append({
            "name": f"Not on {label}", "ok": not listed,
            "detail": (f"{domain} is listed on {label}, a domain blocklist mail systems trust." if listed
                       else f"{domain} is not on {label}."),
            "fix": (f"Being on {label} sends your mail straight to spam. It usually means the domain was used "
                    f"for spam or is very new. Check and request removal at spamhaus.org/lookup, and send only "
                    f"wanted mail from it." if listed else ""),
        })
    result["score"] = sum(1 for c in result["checks"] if c["ok"])
    return result


def check_sending_host_sync(host: str) -> dict:
    """The sending server's own reputation basics — reverse DNS and the IP
    blocklists — for a custom or self-hosted SMTP server. Skipped for the big
    providers, whose IPs are their concern, not yours."""
    host = (host or "").strip().lower().rstrip(".")
    out = {"host": host, "ip": "", "checks": [], "skipped": ""}
    if not host:
        out["skipped"] = "no SMTP server set"
        return out
    if any(host == m or host.endswith("." + m) for m in MANAGED_SMTP_HOSTS):
        out["skipped"] = f"{host} is a managed provider; it owns its sending IPs and their reputation."
        return out
    ips = _resolve_ips(host)
    if not ips:
        out["skipped"] = f"{host} does not resolve to an address."
        return out
    out["ip"] = ips[0]
    ptr = _ptr(ips[0])
    out["checks"].append({
        "name": "Reverse DNS (PTR)", "ok": bool(ptr),
        "detail": f"{ips[0]} resolves back to {ptr}." if ptr else f"{ips[0]} has no reverse-DNS name.",
        "fix": "" if ptr else ("A sending IP with no PTR record is rejected or junked by most inboxes. Set a "
                               "reverse-DNS (PTR) record for the IP, at whoever provides it, pointing to a hostname "
                               "on your sending domain (and make the mail server greet with that same name)."),
    })
    for label, zone in IP_BLOCKLISTS:
        listed = _dnsbl(ips[0], zone, reverse_ip=True)
        if listed is None:
            continue
        out["checks"].append({
            "name": f"Not on {label}", "ok": not listed,
            "detail": (f"{ips[0]} is listed on {label}." if listed else f"{ips[0]} is not on {label}."),
            "fix": (f"An IP on {label} cannot deliver to most inboxes. A fresh cloud or residential IP is often "
                    f"listed before you send a thing. Check it at spamhaus.org/lookup; if it is a shared or dirty "
                    f"IP, send through a mailbox provider or a dedicated IP you can warm instead." if listed else ""),
        })
    out["score"] = sum(1 for c in out["checks"] if c["ok"])
    return out


async def check_domain(domain: str) -> dict:
    return await asyncio.to_thread(check_domain_sync, domain)


async def check_sending_host(host: str) -> dict:
    return await asyncio.to_thread(check_sending_host_sync, host)


def domain_of(address: str) -> str:
    return (address or "").rsplit("@", 1)[-1].strip().lower() if "@" in (address or "") else ""
