"""Work out a mailbox's SMTP and IMAP servers from its address alone, so a
user can connect with just their email and password (an app password for
Gmail, Yahoo and iCloud; the account password elsewhere)."""

KNOWN = {
    "gmail.com": ("smtp.gmail.com", 587, "imap.gmail.com", 993),
    "googlemail.com": ("smtp.gmail.com", 587, "imap.gmail.com", 993),
    "outlook.com": ("smtp.office365.com", 587, "outlook.office365.com", 993),
    "hotmail.com": ("smtp.office365.com", 587, "outlook.office365.com", 993),
    "live.com": ("smtp.office365.com", 587, "outlook.office365.com", 993),
    "msn.com": ("smtp.office365.com", 587, "outlook.office365.com", 993),
    "yahoo.com": ("smtp.mail.yahoo.com", 465, "imap.mail.yahoo.com", 993),
    "yahoo.co.uk": ("smtp.mail.yahoo.com", 465, "imap.mail.yahoo.com", 993),
    "ymail.com": ("smtp.mail.yahoo.com", 465, "imap.mail.yahoo.com", 993),
    "icloud.com": ("smtp.mail.me.com", 587, "imap.mail.me.com", 993),
    "me.com": ("smtp.mail.me.com", 587, "imap.mail.me.com", 993),
    "mac.com": ("smtp.mail.me.com", 587, "imap.mail.me.com", 993),
    "zoho.com": ("smtp.zoho.com", 465, "imap.zoho.com", 993),
    "fastmail.com": ("smtp.fastmail.com", 465, "imap.fastmail.com", 993),
    "fastmail.fm": ("smtp.fastmail.com", 465, "imap.fastmail.com", 993),
    "gmx.com": ("mail.gmx.com", 587, "imap.gmx.com", 993),
    "gmx.de": ("mail.gmx.net", 587, "imap.gmx.net", 993),
    "aol.com": ("smtp.aol.com", 465, "imap.aol.com", 993),
    "proton.me": None, "protonmail.com": None,   # no SMTP without the Proton bridge
}
# MX host fragment → the provider's servers (a custom domain on Workspace or 365).
MX_HINTS = (
    ("google.com", ("smtp.gmail.com", 587, "imap.gmail.com", 993)),
    ("googlemail.com", ("smtp.gmail.com", 587, "imap.gmail.com", 993)),
    ("outlook.com", ("smtp.office365.com", 587, "outlook.office365.com", 993)),
    ("protection.outlook.com", ("smtp.office365.com", 587, "outlook.office365.com", 993)),
    ("zoho.", ("smtp.zoho.com", 465, "imap.zoho.com", 993)),
    ("messagingengine.com", ("smtp.fastmail.com", 465, "imap.fastmail.com", 993)),
    ("yahoodns.net", ("smtp.mail.yahoo.com", 465, "imap.mail.yahoo.com", 993)),
    ("icloud.com", ("smtp.mail.me.com", 587, "imap.mail.me.com", 993)),
)


def _mx_hosts(domain: str) -> list[str]:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=4.0)
        return sorted((str(r.exchange).rstrip(".").lower() for r in answers))
    except Exception:
        return []


def guess_settings(email: str) -> tuple[dict | None, str]:
    """(settings for the SMTP form, note) or (None, why not)."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return None, "Enter the full email address."
    domain = email.rsplit("@", 1)[-1]
    if domain in KNOWN:
        if KNOWN[domain] is None:
            return None, f"{domain} mailboxes have no plain SMTP access; use another mailbox."
        smtp, sport, imap, iport = KNOWN[domain]
        note = f"{domain}: known servers."
    else:
        found = None
        mx = _mx_hosts(domain)
        for fragment, servers in MX_HINTS:
            if any(fragment in h for h in mx):
                found = servers
                break
        if found:
            smtp, sport, imap, iport = found
            note = f"{domain} is hosted at {smtp.split('.', 1)[1]} (from its mail records)."
        else:
            smtp, sport, imap, iport = f"smtp.{domain}", 587, f"imap.{domain}", 993
            note = (f"No known provider for {domain}; trying smtp.{domain} and imap.{domain}. If that "
                    "fails, use the full settings form with the servers from your mail host.")
    return {"smtp_host": smtp, "smtp_port": str(sport), "smtp_username": email, "from_email": "",
            "reply_to": "", "imap_host": imap, "imap_port": str(iport), "imap_username": email,
            "imap_password": ""}, note
