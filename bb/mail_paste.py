"""Connect a mailbox by pasting its login, in whatever shape you have it.

A person rarely has their mail settings as four tidy fields. They have
the block their provider's help page shows ("IMAP server: imap.x.com,
Port: 993, Username: you@x.com, Password: ..."), or a line a colleague
sent them ("you@x.com  app-password"), or a connection URL from a tool,
or a little JSON. This reads any of those into the same four things the
rest of the site already connects with: the address, the password, and
(when they are not the usual ones for that address) the SMTP and IMAP
servers. Missing servers are worked out from the address afterwards, by
``bb.mail_autoconfig.guess_settings``, and the login is then tested and
sealed exactly like the typed form: nothing new touches the mail servers.

It is for a mailbox's own credentials (an app password, or the account
password where the provider allows it), not for anything that stands in
for a browser session.
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlsplit

# A label on the left of ``:`` or ``=`` → which field it sets. Matched on
# the whole label, lowercased, with runs of spaces/underscores/dashes as one.
_LABELS = {
    "email": "email", "e mail": "email", "address": "email", "mail address": "email",
    "email address": "email", "account": "email", "account name": "email", "login": "email",
    "login name": "email", "user": "email", "username": "email", "user name": "email",
    "password": "password", "pass": "password", "passwd": "password", "pwd": "password",
    "app password": "password", "app specific password": "password", "application password": "password",
    "smtp": "smtp_host", "smtp server": "smtp_host", "smtp host": "smtp_host", "smtp hostname": "smtp_host",
    "outgoing server": "smtp_host", "outgoing mail server": "smtp_host", "outgoing host": "smtp_host",
    "sending server": "smtp_host", "server smtp": "smtp_host",
    "smtp port": "smtp_port", "outgoing port": "smtp_port", "port smtp": "smtp_port",
    "imap": "imap_host", "imap server": "imap_host", "imap host": "imap_host", "imap hostname": "imap_host",
    "incoming server": "imap_host", "incoming mail server": "imap_host", "incoming host": "imap_host",
    "receiving server": "imap_host", "server imap": "imap_host",
    "imap port": "imap_port", "incoming port": "imap_port", "port imap": "imap_port",
    "smtp username": "smtp_username", "smtp user": "smtp_username", "smtp login": "smtp_username",
    "imap username": "imap_username", "imap user": "imap_username", "imap login": "imap_username",
}
# Labels that only say which server a bare "Port:" / "Host:" belongs to.
_CONTEXT = {"imap": "imap", "incoming": "imap", "incoming mail": "imap", "receiving": "imap",
            "smtp": "smtp", "outgoing": "smtp", "outgoing mail": "smtp", "sending": "smtp"}
_HOSTISH = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
_EMAIL = re.compile(r"[^\s,;:<>()\"']+@[^\s,;:<>()\"']+\.[^\s,;:<>()\"']+")


def _norm_label(label: str) -> str:
    label = re.sub(r"\(.*?\)", " ", label)       # drop a "(IMAP)" / "(SSL)" hint in the label
    return re.sub(r"[\s_\-]+", " ", label.strip().strip("*").lower()).strip()


def _classify(label: str) -> str:
    """A label not spelled exactly like one in _LABELS, read by its words:
    which field it most likely sets ("" when none)."""
    imap = any(w in label for w in ("imap", "incoming", "receiving"))
    smtp = any(w in label for w in ("smtp", "outgoing", "sending"))
    if "port" in label:
        return "imap_port" if imap else "smtp_port" if smtp else ""
    if any(w in label for w in ("server", "host")) or imap or smtp:
        if imap and "user" in label:
            return "imap_username"
        if smtp and "user" in label:
            return "smtp_username"
        return "imap_host" if imap else "smtp_host" if smtp else ""
    if "pass" in label:
        return "password"
    if any(w in label for w in ("user", "login", "email", "account", "address", "mail")):
        return "email"
    return ""


def _host_port(value: str) -> tuple[str, str]:
    """``host``, ``host:port`` or ``host port`` → (host, port)."""
    value = value.strip().strip("<>").rstrip("/")
    value = re.sub(r"^\w+://", "", value)          # a scheme someone left on
    m = re.match(r"^([A-Za-z0-9.\-]+?)(?:[:\s]+(\d{1,5}))?$", value)
    if not m:
        return value, ""
    return m.group(1), m.group(2) or ""


def _from_url(text: str, out: dict):
    """Pull imap(s):// and smtp(s):// connection URLs out of the text."""
    for m in re.finditer(r"\b(imaps?|smtps?)://[^\s,;'\"<>]+", text, re.I):
        parts = urlsplit(m.group(0))
        kind = "imap" if parts.scheme.lower().startswith("imap") else "smtp"
        if parts.hostname:
            out.setdefault(f"{kind}_host", parts.hostname)
            if parts.port:
                out.setdefault(f"{kind}_port", str(parts.port))
        if parts.username:
            user = unquote(parts.username)
            out.setdefault(f"{kind}_username", user)
            if "@" in user:
                out.setdefault("email", user)
        if parts.password:
            out.setdefault("password", unquote(parts.password))


def _from_json(text: str, out: dict):
    """A JSON object, with a handful of common key spellings."""
    try:
        data = json.loads(text)
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    alias = {"email": "email", "address": "email", "user": "email", "username": "email", "login": "email",
             "password": "password", "pass": "password", "pwd": "password", "app_password": "password",
             "smtp_host": "smtp_host", "smtp_server": "smtp_host", "smtp": "smtp_host", "host": "smtp_host",
             "smtp_port": "smtp_port", "port": "smtp_port",
             "imap_host": "imap_host", "imap_server": "imap_host", "imap": "imap_host",
             "imap_port": "imap_port", "smtp_username": "smtp_username", "imap_username": "imap_username"}
    for key, value in data.items():
        field = alias.get(str(key).strip().lower().replace("-", "_"))
        if field and value not in (None, ""):
            out.setdefault(field, str(value).strip())
    return bool(out)


def _one_line(line: str, out: dict):
    """``address password [smtp[:port]] [imap[:port]]`` on a single line."""
    m = re.match(r"^\s*([^\s,;:\t<>]+@[^\s,;:\t<>]+)[\s,;:\t]+(.+)$", line)
    if not m:
        return
    out.setdefault("email", m.group(1).lower())
    rest = m.group(2).strip()
    servers = []
    for _ in range(2):
        tail = re.search(r"[\s,;\t]+([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?::\d{1,5})?)\s*$", rest)
        if not tail:
            break
        servers.insert(0, tail.group(1))
        rest = rest[:tail.start()]
    if rest.strip():
        out.setdefault("password", rest.strip())
    for field, value in zip(("smtp_host", "imap_host"), servers):
        host, port = _host_port(value)
        out.setdefault(field, host)
        if port:
            out.setdefault(field.replace("host", "port"), port)
    if "smtp_host" in out and "imap_host" not in out:
        out["imap_host"] = out["smtp_host"]


def parse_pasted_login(text: str) -> tuple[dict | None, str]:
    """Read a pasted mailbox login into connect fields. Returns
    ({email, password, and any of smtp_host/smtp_port/imap_host/imap_port/
    smtp_username/imap_username}, note) or (None, why not). The servers
    that are left out are worked out from the address later; only the
    address and password are required here."""
    text = (text or "").strip()
    if not text:
        return None, "Paste the mailbox's login first."
    out: dict = {}

    _from_url(text, out)
    if text.lstrip().startswith("{"):
        _from_json(text, out)

    # Labelled lines ("IMAP server: imap.x.com", "Port = 993"), and a bare
    # host or port under the server named just before it.
    last_server = ""
    for raw in text.splitlines():
        line = raw.strip().strip(",;")
        if not line or "://" in line:
            continue
        m = re.match(r"^\s*([A-Za-z][\w ()\-/]*?)\s*[:=]\s*(.+?)\s*$", line)
        if not m:
            continue
        label, value = _norm_label(m.group(1)), m.group(2).strip().strip("<>")
        value = re.sub(r"\s*\(.*?\)\s*$", "", value)        # drop a "(recommended)" note
        field = _LABELS.get(label) or _classify(label)
        if field in ("smtp_host", "imap_host"):
            last_server = field[:4]
            host, port = _host_port(value)
            out.setdefault(field, host)
            if port:
                out.setdefault(f"{last_server}_port", port)
        elif field:
            out.setdefault(field, value)
            if field in ("smtp_port", "imap_port"):
                last_server = field[:4]
        elif label in ("port", "server port") and value.isdigit() and last_server:
            out.setdefault(f"{last_server}_port", value)
        elif label in ("host", "hostname", "server") and _HOSTISH.match(value) and last_server:
            out.setdefault(f"{last_server}_host", value)
        elif label in _CONTEXT:
            last_server = _CONTEXT[label]
            host, port = _host_port(value)
            if _HOSTISH.match(host):
                out.setdefault(f"{last_server}_host", host)
                if port:
                    out.setdefault(f"{last_server}_port", port)

    # A one-line "address password [servers]" paste.
    if not out.get("password") and "\n" not in text.strip():
        _one_line(text.strip(), out)

    # An address anywhere in the text, if the labels did not name one.
    if not out.get("email"):
        for cand in (out.get("smtp_username"), out.get("imap_username")):
            if cand and "@" in cand:
                out["email"] = cand.lower()
                break
    if not out.get("email"):
        found = _EMAIL.search(text)
        if found:
            out["email"] = found.group(0).lower()

    out = {k: v for k, v in out.items() if v}
    for key in ("email",):
        out[key] = str(out.get(key, "")).strip().lower()
    if not out.get("email"):
        return None, ("No email address found in what you pasted. Include the mailbox address, "
                      "for example a line like \"Email: you@company.com\".")
    if "@" not in out["email"]:
        return None, f"\"{out['email']}\" is not an email address."
    if not out.get("password"):
        return None, ("No password found. Paste the mailbox's password (an app password where the provider "
                      "needs one) on a line like \"Password: ...\", or after the address on one line.")
    # Ports must be numbers; a username defaults to the address at connect time.
    for port in ("smtp_port", "imap_port"):
        if out.get(port) and not str(out[port]).isdigit():
            out.pop(port)
    named = [n for n, k in (("SMTP server", "smtp_host"), ("IMAP server", "imap_host")) if out.get(k)]
    if named:
        note = "Read the address, the password and the " + " and ".join(named) + " from what you pasted."
    else:
        note = "Read the address and password; the servers will be worked out from the address."
    return out, note


def to_smtp_fields(parsed: dict, guess) -> tuple[dict, str]:
    """Turn a parsed login into the dict ``service.connect_imap`` wants,
    filling any server not pasted from ``guess`` (``guess_settings``).
    Returns (smtp, note) or raises ValueError when no server is known."""
    email = parsed["email"]
    base, note = guess(email)
    if base is None and not (parsed.get("smtp_host") and parsed.get("imap_host")):
        raise ValueError(note)
    base = dict(base or {})
    smtp = {
        "smtp_host": parsed.get("smtp_host") or base.get("smtp_host", ""),
        "smtp_port": parsed.get("smtp_port") or base.get("smtp_port", "587"),
        "imap_host": parsed.get("imap_host") or base.get("imap_host") or parsed.get("smtp_host", ""),
        "imap_port": parsed.get("imap_port") or base.get("imap_port", "993"),
        "smtp_username": parsed.get("smtp_username") or email,
        "imap_username": parsed.get("imap_username") or "",
        "imap_password": "",
        "smtp_password": parsed["password"],
    }
    if not smtp["smtp_host"] or not smtp["imap_host"]:
        raise ValueError("The SMTP and IMAP servers could not be worked out from the address; paste them too "
                         "(for example \"SMTP server: smtp.company.com\" and \"IMAP server: imap.company.com\").")
    return smtp, note
