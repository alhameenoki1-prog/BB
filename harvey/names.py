"""Who an email is addressed to, and how it opens.

Uploaded lists often carry an address and nothing else. Before writing,
Harvey works the person's name out from what it has, in order of trust:

1. the name on the row;
2. the LinkedIn URL (``/in/jane-doe-1a2b3c`` → Jane Doe);
3. the address itself, when it follows a first.last pattern
   (``jane.doe@``, ``jane_doe@``, ``jane-doe@``);
4. the company's team page, matched against the address
   (``jdoe@`` next to a card for "Jane Doe").

Shared inboxes (``info@``, ``sales@``, ``office@``) are recognised and
addressed to the team, never to an invented person. Every outgoing email
then gets a greeting line that fits what is known: "Hi Jane," / "Hello
team," / "Hi there,".
"""

import re

GREETING_WORDS = ("Hi", "Hello", "Hey", "Dear", "Good day")

# Local parts that are a function, not a person.
ROLE_LOCAL_PARTS = {
    "info", "infos", "information", "sales", "hello", "hi", "hey", "contact", "contactus", "contact-us",
    "admin", "administrator", "office", "support", "help", "helpdesk", "team", "hr", "humanresources",
    "careers", "career", "jobs", "job", "recruiting", "recruitment", "talent", "enquiries", "enquiry",
    "inquiries", "inquiry", "mail", "email", "post", "billing", "accounts", "accounting", "finance",
    "invoices", "invoice", "marketing", "press", "media", "pr", "partners", "partnerships", "partner",
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "reception", "frontdesk", "bookings",
    "booking", "reservations", "service", "services", "customerservice", "customer-service",
    "customercare", "care", "orders", "order", "shop", "store", "webmaster", "web", "it", "tech",
    "security", "legal", "compliance", "privacy", "newsletter", "news", "general", "office-manager",
    "studio", "agency", "clinic", "practice", "management", "manager", "director", "ceo", "cfo", "coo",
    "owner", "founder", "founders", "feedback", "quotes", "quote", "estimates", "estimate", "projects",
    "dispatch", "operations", "ops", "staff", "people", "welcome", "ask", "letstalk", "work", "business",
    "biz", "home", "main", "secretary", "assistant", "pa", "executive", "exec", "board", "events",
    "volunteer", "donate", "donations", "fundraising", "membership", "members", "alumni",
}

# No team page to look at for a personal address.
PUBLIC_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
    "yahoo.co.uk", "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "gmx.de", "mail.com", "zoho.com", "yandex.com",
}

_LINKEDIN_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.I)
_GREETING_LINE_RE = re.compile(
    r"^\s*(hi|hello|hey|dear|good\s+(?:morning|afternoon|evening|day)|greetings)\b[^\n]{0,60}$",
    re.I,
)
_NAMEISH_RE = re.compile(r"^[a-z][a-z'’]*(?:-[a-z][a-z'’]*)?$", re.I)


def _title(token: str) -> str:
    """jane → Jane; o'brien → O'Brien; mary-ann → Mary-Ann; mcdonald → McDonald."""
    token = token.strip().lower()
    if not token:
        return ""
    parts = []
    for part in token.split("-"):
        sub = "'".join(p[:1].upper() + p[1:] for p in part.split("'"))
        sub = "’".join(p[:1].upper() + p[1:] for p in sub.split("’"))
        if sub.lower().startswith("mc") and len(sub) > 3:
            sub = "Mc" + sub[2:3].upper() + sub[3:]
        parts.append(sub)
    return "-".join(parts)


def _nameish(token: str, minimum: int = 2) -> bool:
    return len(token) >= minimum and bool(_NAMEISH_RE.match(token))


def local_part(email: str) -> str:
    return (email or "").strip().lower().split("@", 1)[0]


def is_role_address(email: str) -> bool:
    """info@, sales@, office@ …: a shared inbox, not a person."""
    local = local_part(email)
    if not local:
        return False
    bare = re.sub(r"[^a-z]", "", local)
    if local in ROLE_LOCAL_PARTS or bare in ROLE_LOCAL_PARTS:
        return True
    tokens = [t for t in re.split(r"[._\-+]+", re.sub(r"\d+", "", local)) if t]
    return bool(tokens) and all(t in ROLE_LOCAL_PARTS for t in tokens)


def name_from_linkedin(url: str) -> tuple[str, str]:
    """/in/jane-doe-1a2b3c4 → ("Jane", "Doe"). Empty when the slug is a
    handle rather than a name (e.g. /in/jdoe99)."""
    m = _LINKEDIN_RE.search(url or "")
    if not m:
        return "", ""
    tokens = [t for t in re.split(r"[-_.]+", m.group(1).lower()) if t]
    # Drop LinkedIn's uniqueness suffixes: digits, hex blobs, one-letter bits.
    while tokens and (any(ch.isdigit() for ch in tokens[-1]) or len(tokens[-1]) < 2
                      or re.fullmatch(r"[0-9a-f]{5,}", tokens[-1])):
        tokens.pop()
    tokens = [t for t in tokens if _nameish(t, 1)]
    if len(tokens) < 2 or not _nameish(tokens[0], 2):
        return "", ""
    return _title(tokens[0]), " ".join(_title(t) for t in tokens[1:3])


def name_from_email(email: str) -> tuple[str, str]:
    """jane.doe@ / jane_doe@ / jane-doe@ → ("Jane", "Doe"). A single word
    (jane@, jdoe@) or an initial (j.doe@) is not enough to greet someone
    by, so those return empty."""
    local = local_part(email)
    if not local or is_role_address(email):
        return "", ""
    local = re.sub(r"\d+$", "", local)
    tokens = [t for t in re.split(r"[._\-+]+", local) if t]
    if len(tokens) < 2:
        return "", ""
    first, rest = tokens[0], tokens[1:]
    if not _nameish(first, 2) or not all(_nameish(t, 2) for t in rest):
        return "", ""
    if first in ROLE_LOCAL_PARTS or any(t in ROLE_LOCAL_PARTS for t in rest):
        return "", ""
    if len(rest) > 2:
        return "", ""
    return _title(first), " ".join(_title(t) for t in rest)


def match_team_member(email: str, members: list[dict]) -> dict | None:
    """The team-page card the address most plausibly belongs to: the local
    part contains the surname (jdoe, doe.j, janedoe) or equals the first
    name (jane@) or an initials+surname pattern. Exactly one match wins;
    none or several means we don't know."""
    local = re.sub(r"\d+$", "", local_part(email))
    if not local or is_role_address(email):
        return None
    bare = re.sub(r"[^a-z]", "", local)
    hits = []
    for m in members or []:
        first = re.sub(r"[^a-z]", "", (m.get("first_name") or "").lower())
        last = re.sub(r"[^a-z]", "", (m.get("last_name") or "").split(" ")[-1].lower())
        if not first or not last or len(last) < 3:
            continue
        candidates = {first + last, last + first, first[0] + last, last + first[0],
                      first + "." + last, first, last}
        if bare in candidates or local in {first + "." + last, first + "_" + last, first + "-" + last,
                                           last + "." + first, first[0] + "." + last}:
            hits.append(m)
            continue
        if len(last) >= 4 and last in bare and (first[0] in bare.replace(last, "") or bare == last):
            hits.append(m)
    return hits[0] if len(hits) == 1 else None


def resolve_name(first_name: str, last_name: str, email: str, linkedin_url: str = "") -> dict:
    """{"first_name", "last_name", "source"}: source is "given", "linkedin",
    "email", "role" (shared inbox) or "" (unknown)."""
    first, last = (first_name or "").strip(), (last_name or "").strip()
    if first or last:
        return {"first_name": first, "last_name": last, "source": "given"}
    if is_role_address(email):
        return {"first_name": "", "last_name": "", "source": "role"}
    first, last = name_from_linkedin(linkedin_url)
    if first:
        return {"first_name": first, "last_name": last, "source": "linkedin"}
    first, last = name_from_email(email)
    if first:
        return {"first_name": first, "last_name": last, "source": "email"}
    return {"first_name": "", "last_name": "", "source": ""}


def greeting_for(first_name: str, email: str = "", company: str = "", word: str = "Hi") -> str:
    """The opening line. "Hi Jane," when we know her; "Hello team," for a
    shared inbox; "Hi there," when we don't know who reads it."""
    word = word if word in GREETING_WORDS else "Hi"
    first = (first_name or "").strip().split(" ")[0]
    if first:
        return f"{word} {first},"
    if is_role_address(email):
        # A shared inbox is greeted as a team, without naming the company:
        # the emails never name the recipient's business.
        team_word = "Hello" if word in ("Hi", "Hey") else word
        return f"{team_word} team,"
    if word == "Dear":
        return "Hello,"
    return f"{word} there,"


_INLINE_GREETING_RE = re.compile(
    r"^\s*(?:hi|hello|hey|dear|greetings|good\s+(?:morning|afternoon|evening|day))"
    r"(?:\s+(?:there|all|team|[A-Z][\w'’-]*))?\s*[,:!—–-]+\s*(?P<rest>\S.*)$",
    re.I,
)


def strip_greeting(body: str) -> str:
    """Remove a greeting the model already wrote (whatever it put there),
    so the deterministic one can replace it. A greeting fused onto the first
    sentence ("Hi Jane — quick one…") keeps the sentence."""
    lines = (body or "").strip().split("\n")
    if not lines:
        return ""
    first = lines[0].rstrip()
    if _GREETING_LINE_RE.match(first) and (
        first.endswith((",", ":", "!", "—", "-")) or len(first.split()) <= 4
    ):
        lines = lines[1:]
    else:
        m = _INLINE_GREETING_RE.match(first)
        if m:
            rest = m.group("rest")
            lines[0] = rest[:1].upper() + rest[1:]
    return "\n".join(lines).lstrip("\n").strip()


def ensure_greeting(body: str, first_name: str, email: str = "", company: str = "", word: str = "Hi") -> str:
    """Body with exactly one greeting line on top, a blank line after it."""
    rest = strip_greeting(body)
    greeting = greeting_for(first_name, email, company, word)
    return f"{greeting}\n\n{rest}" if rest else greeting


# ── Company names ──

_GENERIC_TITLE_BITS = {
    "home", "homepage", "welcome", "official site", "official website", "website", "index",
    "main page", "untitled", "home page", "start", "default", "coming soon", "under construction",
}
_MULTI_TLDS = ("co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz", "co.za",
               "com.ng", "org.ng", "com.br", "com.mx", "co.in", "co.jp", "com.sg", "com.hk", "co.ke")


def looks_like_domain(text: str) -> bool:
    text = (text or "").strip().lower()
    return bool(text) and " " not in text and "." in text and bool(re.fullmatch(r"[a-z0-9.-]+", text))


def company_display_name(name: str, domain: str = "") -> str:
    """A name fit for a sentence. The given name when it is one; else the
    domain's own label made readable: north-west-build.co.uk → North West
    Build, 5b4me.com → 5b4me. Empty when nothing is known."""
    name = (name or "").strip()
    if name and not looks_like_domain(name):
        return name
    label = (domain or name).strip().lower().rstrip("/")
    label = re.sub(r"^https?://", "", label).split("/")[0]
    label = label[4:] if label.startswith("www.") else label
    for tld in _MULTI_TLDS:
        if label.endswith("." + tld):
            label = label[: -len(tld) - 1]
            break
    else:
        label = label.rsplit(".", 1)[0] if "." in label else label
    if not label:
        return ""
    words = [w for w in re.split(r"[-_.]+", label) if w]
    out = []
    for w in words:
        out.append(w.capitalize() if w.isalpha() else (w[:1].upper() + w[1:] if w[:1].isalpha() else w))
    return " ".join(out)


def site_name_from_html(html: str, domain: str = "") -> str:
    """What a site calls itself: og:site_name, application-name, else the
    best segment of <title>. Empty when nothing plausible is found."""
    if not html:
        return ""
    head = html[:60000]
    candidates = []
    for prop in ("og:site_name", "application-name", "twitter:site"):
        for m in re.finditer(
            r'<meta\s+[^>]*?(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]*?content=["\']([^"\']{2,80})["\']',
            head, re.I):
            candidates.append(m.group(1))
        for m in re.finditer(
            r'<meta\s+[^>]*?content=["\']([^"\']{2,80})["\'][^>]*?(?:property|name)=["\']' + re.escape(prop) + r'["\']',
            head, re.I):
            candidates.append(m.group(1))
    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    if m:
        import html as _html
        title = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
        segments = [seg.strip(" -|:·•") for seg in re.split(r"\s[|\-–—:·•»]\s|\s\|\s", title)]
        label = re.sub(r"[^a-z0-9]", "", company_display_name("", domain).lower())
        # Prefer the segment that echoes the domain, then the shortest sensible one.
        segments = [seg for seg in segments if seg]
        echo = [seg for seg in segments if label and label in re.sub(r"[^a-z0-9]", "", seg.lower())]
        candidates.extend(echo + sorted(segments, key=len))
    for cand in candidates:
        cand = cand.strip().lstrip("@").strip()
        low = cand.lower()
        if not (2 <= len(cand) <= 40) or low in _GENERIC_TITLE_BITS or looks_like_domain(cand):
            continue
        if "http" in low or len(cand.split()) > 6:
            continue
        return cand
    return ""


def meta_description_from_html(html: str) -> str:
    if not html:
        return ""
    for pat in (r'<meta\s+[^>]*?(?:name|property)=["\'](?:description|og:description)["\'][^>]*?content=["\']([^"\']{20,400})["\']',
                r'<meta\s+[^>]*?content=["\']([^"\']{20,400})["\'][^>]*?(?:name|property)=["\'](?:description|og:description)["\']'):
        m = re.search(pat, html[:60000], re.I)
        if m:
            import html as _html
            return _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    return ""
