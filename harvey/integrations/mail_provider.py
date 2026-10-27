"""Mail provider abstraction — Harvey owns its sending, Instantly optional.

A MailProvider gives Harvey full inbox control: send, read replies,
thread. Two native providers ship:

- ``gmail``  — a Google Workspace / Gmail mailbox via the Gmail REST API
  (recommended: a real mailbox on a dedicated secondary domain is the
  most deliverable way to send <50 cold emails/day).
- ``microsoft`` — a Microsoft 365 / Outlook mailbox via Microsoft Graph
  (OAuth; Exchange Online no longer accepts passwords over IMAP).
- ``smtp``   — any mailbox that speaks SMTP + IMAP (AgentMail, Fastmail,
  a Workspace app-mailbox, self-hosted). Configuration via .env.

``instantly`` remains supported through the legacy Sender/Handler path —
it isn't a MailProvider because Instantly owns sequencing/sending itself.
"""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger("harvey.mail")

# Every provider get_mail_provider can build: Harvey reads and sends through
# these itself (outbox, stop-on-reply, bounces). Anything else is Instantly.
NATIVE_PROVIDERS = ("gmail", "smtp", "microsoft", "microsoft_app")


class MailReadError(Exception):
    """The mailbox could not be read (network, a dead sign-in, a 5xx). Not
    the same as "no new mail": sending waits until it can be read again."""


@dataclass
class SendResult:
    ok: bool
    message_id: str = ""     # RFC Message-ID (or provider id) of the sent mail
    thread_ref: str = ""     # provider thread handle (Gmail threadId, etc.)
    error: str = ""
    retryable: bool = False  # a temporary refusal (throttling, 4xx, network): try again later
    scope: str = "row"       # "row": only this recipient is affected; "mailbox": the whole pass should stop
    auth_failed: bool = False  # the mailbox refused the login: nothing sends until it is reconnected
    daily_limit: bool = False  # the mailbox's own daily ceiling: it sends again tomorrow
    bad_address: bool = False  # the address itself was refused for good (5xx on RCPT): a bounce at send time


# Mail hosts that only send: there is no inbox to read there, so reply
# polling is skipped instead of failing every pass.
RELAY_HOST_HINTS = ("amazonaws.com", "sendgrid.net", "mailgun.org", "postmarkapp.com", "brevo.com",
                    "sendinblue.com", "sparkpostmail.com", "mailjet.com", "smtp2go.com", "elasticemail.com",
                    "mandrillapp.com", "resend.com")


def is_relay_host(host: str) -> bool:
    host = (host or "").lower().strip()
    return any(host == h or host.endswith("." + h) for h in RELAY_HOST_HINTS)


@dataclass
class InboundMessage:
    provider_id: str         # provider-unique id, used for reply dedup
    from_email: str
    subject: str = ""
    body: str = ""
    message_id: str = ""     # RFC Message-ID header
    in_reply_to: str = ""    # RFC In-Reply-To header (bounce/thread matching)
    thread_ref: str = ""
    date: str = ""
    is_bounce: bool = False
    headers: dict = field(default_factory=dict)
    uid: int = 0             # IMAP UID when read by position (0 elsewhere)


BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem",
                  "microsoftexchange329e71ec88ae4615bbc36ab6ce41109e")   # Exchange's own reports, inside the org
BOUNCE_SUBJECTS = (
    "delivery status notification", "undeliverable", "returned mail",
    "delivery failure", "failure notice", "could not be delivered",
    "delivery incomplete", "address not found", "message blocked",
    "message not delivered", "delivery has failed", "mail delivery failed",
    "undelivered mail returned", "returned to sender", "delivery notification: delivery has failed",
    "mail delivery failure", "recipient address rejected",
)

# What a bounce means and what to do about it, from the SMTP code or the
# wording in the report. Order matters: the first match wins.
SENDER_BLOCKED = "your mailbox is blocked from sending"
DAILY_LIMIT_HIT = "your provider's daily sending limit is reached"


def _code(*codes: str) -> str:
    """An enhanced status code as a whole token: "5.1.8" must not match
    inside an IP address like 45.1.8.22 quoted in a report."""
    return "|".join(r"(?<![\d.])" + c + r"(?![\d.])" for c in codes)


SENDER_BLOCK_HINT = (
    "Your own mail provider has stopped this mailbox from sending (Microsoft 365 does this when a mailbox looks "
    "like it is sending spam or bulk mail): every email bounces until an admin unblocks it. In Microsoft 365: "
    "security.microsoft.com → Email & collaboration → Review → Restricted entities → unblock the mailbox. Then "
    "send far fewer a day (30 to 50 per mailbox, warm-up on, Gentle speed) and press Resume sending.")

BOUNCE_HINTS = (
    # Order matters: the first match wins. The verdicts on the SENDER (a
    # block) come before "authentication", because a real DSN quotes the
    # original message's headers (DKIM-Signature, ARC-Authentication-Results)
    # further down, and those words must not turn a block into something else.
    # The sender's own provider refusing to send for it at all (Microsoft
    # 365's restricted entities, a banned sender, a tenant over its limit):
    # before the generic 5.1.x rule, because 5.1.8 is about the sender.
    (_code(r"5\.1\.8", r"5\.7\.750", r"5\.7\.705", r"5\.7\.50[123]")
     + r"|bad outbound sender|not allowed to send (?:messages|email)|suspected of sending spam|"
     r"restricted from sending|recognized as a valid sender|spam abuse detected|tenant has exceeded threshold|"
     r"client blocked from sending from unregistered domain",
     SENDER_BLOCKED, SENDER_BLOCK_HINT),
    # Microsoft, as the RECEIVING side, refusing a foreign sending server or
    # domain (its ban list): a spam verdict on us at one provider, so a
    # pause, not a stop, and the fix is Microsoft's delisting, not Defender.
    (_code(r"5\.7\.511", r"5\.7\.708", r"5\.7\.800", r"5\.7\.6[0-4]\d")
     + r"|banned sender|banned sending ip|traffic not accepted from this ip",
     "judged as unsolicited mail",
     "Microsoft's receiving filters have blocked your sending server or domain as a source of spam. Slow down "
     "(Gentle speed), send from a domain with SPF, DKIM and DMARC, and if you run your own mail server ask "
     "Microsoft to delist it at sender.office.com (or delist@microsoft.com for a 5.7.511)."),
    # The sender's own provider's limits for the day (Exchange's 10,000
    # recipients a day, a tenant's external recipient limit, a temporary
    # restriction while it looks at the traffic): wait, then slow down.
    # Microsoft 365's limits for the DAY: the whole tenant's cap on email to
    # outside recipients (it grows with the licences the tenant has), one
    # mailbox's 10,000 recipients. Everything bounces until the 24-hour
    # window moves on, so sending pauses a day.
    (_code(r"5\.7\.23[236]", r"5\.1\.90")
     + r"|tenant external recipient rate limit|daily limit for message recipients|"
     r"submission quota was exceeded|submissionquotaexceeded",
     DAILY_LIMIT_HIT,
     "Microsoft 365 has stopped the tenant (every mailbox in it) sending to outside addresses until the day's "
     "window moves on; the cap grows with the number of paid licences and cannot be raised by request. Sending "
     "pauses 24 hours, then the bounced first emails go again. To send more a day, add licensed mailboxes, "
     "or send from another provider; Microsoft says Exchange Online is not for bulk mail."),
    (_code(r"4\.7\.[5-8]\d\d", r"5\.7\.8[5-9]\d")
     + r"|please try again later|recipient rate limit|message rate limit",
     "sending too fast or too much",
     "Your mailbox provider throttled you. Switch the profile to the Gentle speed, keep the warm-up "
     "ramp on, and lower the daily maximum (30 to 50 a day for a mailbox under a month old)."),
    # The sender's own provider refusing the recipient (Vade Secure filters
    # OVH, Open-Xchange and other hosted mail): 407 is a domain that does
    # not exist, 406 a malformed address, 402 an address it will not deliver to.
    (r"vadesecure\.com/outbound_error_codes/#_407|oxsus\d*_407",
     "no such address (the recipient's domain does not exist)",
     "Your own mail provider refused it before sending: that domain has no mail server. The address came from the "
     "list; it is marked invalid and never written to again."),
    (r"vadesecure\.com/outbound_error_codes/#_406|oxsus\d*_406",
     "no such address (badly formed)",
     "Your own mail provider refused the address as malformed. It is marked invalid."),
    (r"vadesecure\.com/outbound_error_codes/#_402|oxsus\d*_402|5\.7\.1 recipient rejected",
     "no such address (your provider will not deliver to it)",
     "Your own mail provider's filter refuses to deliver to that address. It is marked invalid; if many addresses "
     "at one company fail this way, the list for that company is wrong."),
    (r"5\.4\.1.{0,80}(recipient address rejected|access denied)|recipient address rejected: access denied",
     "no such address (Microsoft 365 says it is not in their directory)",
     "The company uses Microsoft 365 and that address is not in its directory: the name or the address pattern "
     "is wrong. Gmail shows this as \"Message blocked\", but it is not a spam verdict and nothing is wrong with "
     "your mailbox. The address is marked invalid. Several of these from one upload mean the list was guessed "
     "(firstlast@) rather than verified; check the list before sending."),
    (r"5\.1\.1|5\.1\.[0-9]|user unknown|no such user|does not exist|address not found|"
     r"recipient address rejected|unknown recipient|no mailbox|invalid recipient",
     "no such address",
     "The address does not exist. Nothing to fix on your side; it has been marked invalid."),
    (r"unusual rate|rate.?limit|too many|sending quota|quota exceeded|4\.7\.0|5\.4\.5|"
     r"daily sending quota|exceeded the maximum",
     "sending too fast or too much",
     "Your mailbox provider throttled you. Switch the profile to the Gentle speed, keep the warm-up "
     "ramp on, and lower the daily maximum (30 to 50 a day for a mailbox under a month old)."),
    (r"likely unsolicited|unsolicited mail|rejected as spam|message rejected.{0,40}spam|identified as spam|"
     r"looks like spam|blocked using|blacklist|block list|poor reputation|low reputation|sending reputation|"
     r"5\.7\.1 (service unavailable|message rejected|client host|unfortunately)",
     "judged as unsolicited mail",
     "The receiving side treated the email as bulk or spam. Slow down (Gentle speed), send from a "
     "domain with SPF, DKIM and DMARC, keep links and attachments out of first emails, and keep "
     "the list to addresses you are sure exist."),
    (r"5\.7\.0|security issue|suspicious attachment|content presents",
     "content blocked",
     "The provider blocked the content itself. Remove attachments from first emails and check that "
     "the signature's links point at a plain, working site."),
    (r"5\.2\.2|4\.2\.2|mailbox full|over quota|mailbox is full",
     "mailbox full",
     "Their mailbox is full. The address is real; try again in a few weeks."),
    (r"5\.7\.26|5\.7\.25|not authenticated|unauthenticated|(spf|dkim|dmarc)[^\n]{0,40}(fail|none|missing|not pass|did not pass)",
     "sender authentication failed",
     "The receiving server could not verify your domain. Publish SPF, DKIM and DMARC for the "
     "sending domain (the Mailbox page's deliverability table shows which is missing)."),
    (r"4\.4\.1|4\.4\.7|timed out|timeout|connection refused|could not connect|no mx|not reachable",
     "their mail server could not be reached",
     "Their domain's mail server is down or missing. Nothing to fix on your side."),
)


# Bounce kinds that are a verdict on the SENDER, not the address: the
# receiving side (or the sender's own provider) treated the email as spam.
# The address is fine; sending more right away makes it worse.
BLOCK_KINDS = ("judged as unsolicited mail", "content blocked", "sending too fast or too much", SENDER_BLOCKED,
               DAILY_LIMIT_HIT)
# Our own side's limits: the email never left, so it goes again after the
# pause (a recipient's spam verdict is not retried: that makes it worse).
RETRY_KINDS = ("sending too fast or too much", DAILY_LIMIT_HIT)
# The kinds where the sender's OWN provider refuses to send for it: nothing
# leaves until a person unblocks the mailbox, so sending stops rather than
# pausing (the kill switch, with the fix in its text).
SENDER_BLOCK_KINDS = (SENDER_BLOCKED,)

_ADDRESS = r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
_RECIPIENT_PATTERNS = (
    # Exchange Online / Outlook.com: the newest layout, the "Original
    # Message Details" of the admin section, and the classic layout (the
    # line after the heading may be "Name (address)")
    r"(?i)your message to\s+<?" + _ADDRESS + r">?\s+(?:couldn't|could not|wasn't|was not) be delivered",
    r"(?im)^\s*recipient address:\s*<?" + _ADDRESS,
    r"(?im)delivery has failed to these recipients or groups:\s*\n+[^\n]*?\(?" + _ADDRESS + r"\)?\s*$",
    # A DSN's machine-readable part (every provider)
    r"(?im)^\s*final-recipient:\s*rfc822;\s*<?" + _ADDRESS,
    r"(?im)^\s*original-recipient:\s*rfc822;\s*<?" + _ADDRESS,
    # Exchange's "Diagnostic information for administrators" block: the
    # address on its own line, the server's answer within two lines
    # ("Remote server returned '...'", or "<time> - Server at ... returned")
    r"(?im)^\s*<?" + _ADDRESS + r">?\s*\n(?:[^\n]*\n){0,2}?[^\n]*?\bserver (?:at [^\n]*?)?returned",
    # Gmail
    r"(?i)wasn't delivered to\s+<?" + _ADDRESS,
    r"(?i)message (?:wasn't|was not) delivered to\s+<?" + _ADDRESS,
    # Anything else that names who it failed for
    r"(?i)(?:failed|undeliverable|rejected|could not be delivered|couldn't be delivered)[^\n]{0,60}?"
    r"\b(?:to|for|recipient)\s*:?\s*<?" + _ADDRESS + r">?",
    r"(?im)^\s*(?:to|recipient):\s*(?:[^\n<]*<)?" + _ADDRESS,
)


def bounced_recipient(body: str, headers: dict | None = None, exclude: str = "") -> str:
    """The address a delivery report says it failed for ("" when it does not
    say). Headers first (X-Failed-Recipients), then the wording providers
    use, then the DSN's machine-readable lines, then the quoted original's
    To:. ``exclude`` is the sender's own address, never the answer."""
    own = (exclude or "").strip().lower()
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    for key in ("x-failed-recipients", "x-original-to"):
        m = re.search(_ADDRESS, h.get(key, ""))
        if m and m.group(1).lower() != own:
            return m.group(1).lower()
    text = body or ""
    for pat in _RECIPIENT_PATTERNS:
        for m in re.finditer(pat, text):
            found = m.group(1).lower()
            if found != own:
                return found
    return ""


def original_message_ids(body: str) -> list[str]:
    """The Message-IDs a delivery report quotes from the original email
    (Exchange's "Original message headers", folded onto the next line or
    not; a DSN's message/rfc822 part; the InternetMessageId some
    diagnostics carry): what matches the report to the email that bounced
    when the report has no In-Reply-To of its own."""
    text = body or ""
    found = re.findall(r"(?im)^\s*message-id:\s*\r?\n?\s*(<[^>\s]+>)", text)
    found += re.findall(r"(?i)internetmessageid\s+(<[^>\s]+>)", text)
    found += re.findall(r"(?im)^\s*x-ms-tnef-correlator:\s*(<[^>\s]+>)", text)
    return list(dict.fromkeys(found))


def bounce_kind(body: str) -> str:
    """The label of the matching BOUNCE_HINTS row, or "" when none."""
    low = (body or "").lower()
    for pat, what, _hint in BOUNCE_HINTS:
        if re.search(pat, low):
            return what
    return ""


def bounce_reason(body: str) -> tuple[str, str]:
    """(what went wrong, what to do) from a bounce report. The first line of
    diagnostic text found is kept as the reason; the hint comes from the
    code or wording when one is recognised."""
    import re as _re

    text = body or ""
    diag = ""
    for pat in (r"(?im)^\s*diagnostic-code:\s*(?:smtp;)?\s*(.+)$",
                r"(?im)^\s*(?:the response was|the response from the remote server was|"
                r"the error that the other server returned was|"
                r"remote server returned|server said|technical details[^\n]*)[:\s]*\n?\s*(.+)$",
                r"(?im)^\s*(5\d\d[ -][^\n]{10,})$", r"(?im)^\s*(4\d\d[ -][^\n]{10,})$",
                r"(?i)(your message (?:to [^\n]{1,80} )?(?:has been|was) blocked[^\n]*)",
                r"(?i)(address not found[^\n]*|user unknown[^\n]*|does not exist[^\n]*)"):
        m = _re.search(pat, text)
        if m:
            diag = _re.sub(r"\s+", " ", m.group(1)).strip(" .'\"")[:220]
            break
    low = text.lower()
    for pat, what, hint in BOUNCE_HINTS:
        if _re.search(pat, low):
            return (diag or what), hint
    return (diag or "the receiving server rejected it (no reason given)"), (
        "Read the bounce in your mailbox for the exact wording; if several addresses bounce the "
        "same way, slow down and check the Mailbox page's deliverability table.")


AUTOMATED_LOCAL_PARTS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply", "notification", "notifications",
    "newsletter", "newsletters", "mailer", "bounce", "bounces", "alerts", "alert", "news", "marketing",
    "updates", "digest", "billing", "receipts", "receipt", "invoice", "invoices", "calendar",
    "calendar-notification", "jira", "github", "linkedin", "facebookmail", "noreply-",
    "system", "daemon", "robot", "bot", "automated", "auto",
)
AUTOMATED_SUBJECTS = (
    "automatic reply", "auto-reply", "autoreply", "out of office", "out-of-office", "unsubscribe",
    "your receipt", "your invoice", "password reset", "verify your email", "confirm your email",
    "security alert", "newsletter", "weekly digest",
)


def looks_automated(from_email: str, subject: str = "", headers: dict | None = None) -> bool:
    """Mail a person did not write by hand: notifications, newsletters,
    receipts, auto-replies, mailing lists. Never worth an auto-reply."""
    if looks_like_bounce(from_email, subject):
        return True                         # a bounce / rejection is never answered
    local = (from_email or "").lower().split("@", 1)[0]
    if any(local == m or local.startswith(m + "-") or local.startswith(m + ".") or local.startswith(m + "_")
           for m in AUTOMATED_LOCAL_PARTS) or "noreply" in local or "no-reply" in local:
        return True
    s = (subject or "").lower()
    if any(m in s for m in AUTOMATED_SUBJECTS):
        return True
    h = {str(k).lower(): str(v).lower() for k, v in (headers or {}).items()}
    if h.get("auto-submitted", "no") not in ("", "no"):
        return True
    if h.get("precedence") in ("bulk", "list", "junk"):
        return True
    if "list-id" in h or "list-unsubscribe" in h or h.get("x-auto-response-suppress"):
        return True
    return False


DELAY_SUBJECTS = ("delivery delayed", "delayed mail", "delivery status notification (delay)",
                  "delivery has been delayed", "delivery is delayed")
DELAY_BODIES = ("delivery is delayed to these recipients", "this is a warning message only",
                "will keep trying", "action: delayed", "has been delayed", "temporarily delayed",
                "delivery of this message has been delayed")


def is_delay_notice(subject: str, body: str) -> bool:
    """A mail system saying it is still trying (Exchange's "Delivery is
    delayed", a DSN with Action: delayed): not a bounce, the address is
    fine, and a final report follows if it gives up."""
    s = (subject or "").lower()
    b = (body or "").lower()
    return any(m in s for m in DELAY_SUBJECTS) or any(m in b for m in DELAY_BODIES)


def looks_like_bounce(from_email: str, subject: str) -> bool:
    f = (from_email or "").lower()
    s = (subject or "").lower()
    return any(m in f for m in BOUNCE_SENDERS) or any(m in s for m in BOUNCE_SUBJECTS)


class MailProvider(ABC):
    """A mailbox Harvey can send from and read replies out of."""

    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether required credentials are present (no network)."""

    @abstractmethod
    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
        attachments: list | None = None,
        plain: bool = False,
    ) -> SendResult:
        """Send one email. Reply threading when refs are given.
        ``attachments`` are filesystem paths to attach (see attach_files);
        ``plain`` sends text only, with a links-free signature (first emails)."""

    @abstractmethod
    async def get_replies(self, limit: int = 50) -> list[InboundMessage]:
        """Fetch recent inbound messages (dedup is the caller's job)."""

    async def get_unanswered(self, days: int = 30, limit: int = 100) -> list[InboundMessage]:
        """Inbox mail from the last ``days`` days that nobody has replied to
        from this mailbox (read or unread). Providers that can't tell
        return [] and the backlog scan does nothing."""
        return []

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """Cheap end-to-end credential check. Returns (ok, detail)."""


_TAG_RE = None


def html_to_text(html: str) -> str:
    """A readable plain-text version of signature HTML: line breaks kept,
    links shown as 'text (url)', tags dropped."""
    import html as _html
    import re

    text = html or ""
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|tr|li|h\d)>", "\n", text)
    text = re.sub(r'(?is)<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                  lambda m: (lambda label: m.group(1) if not label or label == m.group(1) else f"{label} ({m.group(1)})")(
                      re.sub(r'<[^>]+>', '', m.group(2)).strip()), text)
    text = re.sub(r"(?is)<img[^>]*>", "", text)
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = _html.unescape(text)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


_URL_RE = re.compile(r"https?://\S+|www\.\S+|\S+@\S+", re.I)
_DOMAIN_RE = re.compile(r"\b[a-z0-9-]{2,}(\.[a-z0-9-]{2,})+\b(/\S*)?", re.I)
_LINK_WORDS = {"linkedin", "website", "web", "site", "twitter", "x", "instagram", "facebook"}


def strip_links(text: str) -> str:
    """A signature without its web addresses: a line that was only a link
    (or a label and a link) goes; a line with words keeps the words."""
    out = []
    for line in (text or "").splitlines():
        cleaned = _DOMAIN_RE.sub("", _URL_RE.sub("", line))
        cleaned = re.sub(r"\(\s*\)|\[\s*\]|<\s*>", "", cleaned)          # the brackets a link sat in
        cleaned = re.sub(r"(^|\s)[·|•-]+(?=\s|$)", " ", cleaned)           # dangling separators
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ·|•-:,")
        words = [w for w in re.findall(r"[A-Za-z]+", cleaned)]
        if cleaned and words and not all(w.lower() in _LINK_WORDS for w in words):
            out.append(cleaned)
    return "\n".join(out).strip()


# A line that is only a link, or "label: link" (the label may hold colons).
_BARE_URL_LINE_RE = re.compile(r"^\s*(?:(?P<label>.{1,300}?):\s+)?(?P<url>(?:https?://|www\.)\S+)\s*$", re.I)
# A web address inside a sentence (matched on the raw text, escaped after).
_INLINE_URL_RE = re.compile(r"(?<![\w@/.\-])((?:https?://|www\.)[^\s<>\"'\]]+)", re.I)
# Words a sentence starts with but a link's name does not.
_SENTENCE_START = {"here", "this", "that", "it", "i", "we", "you", "please", "see", "check", "find", "grab", "pick",
                   "book", "click", "open", "read", "download", "there", "attached", "below", "above", "just", "feel"}
_PREPOSITIONS = {"at", "on", "via", "from", "in", "through", "with", "under", "to", "into", "using"}


def _split_url(token: str) -> tuple[str, str]:
    """(the address, the punctuation after it that belongs to the sentence):
    a final '.', ',', ';', ':', '!' or '?' is the sentence's, and so is a
    ')' that closes nothing inside the address."""
    url, tail = token, ""
    while url and (url[-1] in ".,;:!?" or (url[-1] == ")" and url.count("(") < url.count(")"))):
        tail = url[-1] + tail
        url = url[:-1]
    return url, tail


def _href(url: str) -> str:
    return url if url.lower().startswith(("http://", "https://")) else "https://" + url


def _norm_url(url: str) -> str:
    return _href(url).rstrip("/").lower()


def _looks_like_label(prefix: str) -> bool:
    """"Project brief" or "The Towson scope" names a link; "Pick any slot
    that suits you here" is a sentence, whose words must stay."""
    words = prefix.split()
    return (0 < len(words) <= 5 and not any(c in prefix for c in ";,.!?")
            and words[0].lower().strip("'\"") not in _SENTENCE_START)


def link_text(url: str, labels: dict | None = None, line_label: str = "", inline: bool = False, before: str = "") -> str:
    """What a recipient clicks: "View the Towson brief" on a line of its
    own; inside a sentence, the document's name, else "here" (or "this
    link" after a preposition: "a time at this link")."""
    label = (labels or {}).get(_norm_url(url), "") or line_label.strip()
    if label:
        label = label.strip().rstrip(".:")
        if inline or label.lower().startswith(("view", "open", "see", "read")):
            return label
        return f"View {label}"
    if not inline:
        return "View here"
    last = (before.strip().split() or [""])[-1].lower().strip("(\"'")
    return "this link" if last in _PREPOSITIONS else "here"


def _anchor(url: str, text: str, bold: bool = False) -> str:
    import html as _html
    weight = ";font-weight:600" if bold else ""
    return f'<a href="{_html.escape(_href(url), quote=True)}" style="color:#0a60c2{weight}">{_html.escape(text)}</a>'


# What a document is called in a sentence: "the project brief", "our
# scope", "the Towson drawings", "a two-page note". The words a link goes on.
_DOC_NOUN_WORDS = (r"brief|briefs|scope|spec|specs|specification|specifications|drawing|drawings|plan|plans|document|documents|"
                   r"doc|docs|proposal|deck|overview|summary|description|brochure|portfolio|details|page|website|link|"
                   r"listing|pack|packet|outline|requirements|rfp|rfq|tender|file|files|pdf|sheet|note|notes|folder|"
                   r"one-pager|write-up|writeup|report|presentation|slides|calendar|form|photos|pictures|video|set|package|"
                   r"breakdown|schedule|timeline|budget|quote|estimate")
_DOC_NOUNS = r"(?:" + _DOC_NOUN_WORDS + r")"
# (Up to three describing words between: "the full project brief"; never
# another "the", "and" or "of", which would run two things together, and
# English words only.)
_DOC_PHRASE_RE = re.compile(r"\b(?:the|our|this|my|that|a|an)\s+"
                            r"(?:(?!(?:the|a|an|and|or|of|to|for|with|in|on|at|your|their|his|her)\b)[A-Za-z0-9'’&-]+\s+){0,3}?"
                            + _DOC_NOUNS + r"(?:\s+" + _DOC_NOUNS + r")*\b", re.I)
# The core of a document's name: what a word like "schedule" or "budget"
# must be part of to count ("the delivery schedule" is content, not a link,
# unless a word below is in the phrase too).
_DOC_CORE_RE = re.compile(r"\b(?:brief|briefs|scope|spec|specs|specification|specifications|drawings?|plans?|documents?|docs?|"
                          r"proposal|deck|overview|brochure|portfolio|page|website|link|listing|pack|packet|outline|rfp|rfq|"
                          r"tender|file|files|pdf|sheet|notes?|folder|one-pager|write-up|writeup|report|presentation|slides|"
                          r"calendar|form|photos|pictures|video|package|set|summary|description|details|breakdown)\b", re.I)
# Words that hand the document over: the phrase right after them is the one.
_OFFER_CUE_RE = re.compile(r"(?:here(?:'|’)?s|here is|here are|attached|attaching|sending|sent|share|sharing|shared|"
                           r"find|see|look (?:at|through|over)|read|open|linked|link to|check|review|view|is|are|in|into|"
                           r"put|wrote|written up|grab|book|pick)\s*$", re.I)
# The phrase is theirs, not ours: "the proposal you sent", "the report you shared".
_THEIRS_AFTER_RE = re.compile(r"^\s*(?:that\s+)?(?:you|you've|you'd|they|he|she)\b(?:\s+\w+){0,2}?\s+"
                              r"(?:sent|shared|attached|wrote|mentioned|forwarded|made|put together)\b", re.I)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _doc_phrase(text: str, url: str, labels: dict) -> tuple[int, int] | None:
    """Where in ``text`` (the sentence before an address, or the words
    before "…: <address>") the document is named, so that name can become
    the link. Sentence by sentence from the one nearest the address back:
    the phrase right after "here's", "attached", "in", "see"… (nearest the
    end), else the link's own name from the profile, else any document
    named there. Someone else's document ("your proposal", "the report you
    sent") is never the link. None when nothing is named (then the address
    keeps a line of its own)."""
    if not text or _INLINE_URL_RE.search(text) or MD_LINK_RE.search(text):
        return None                      # a sentence with an address in it already has its link
    tail = text[-600:]
    base = len(text) - len(tail)
    label = (labels.get(_norm_url(url), "") or "").strip().rstrip(".:")
    bare = re.sub(r"^(?:the|our|this|my|a|an)\s+", "", label, flags=re.I) if label else ""

    def ours(start, end, sentence):
        return (_DOC_CORE_RE.search(sentence[start:end]) is not None
                and not _THEIRS_AFTER_RE.match(sentence[end:]))
    offsets, pos = [], 0
    for part in _SENTENCE_SPLIT_RE.split(tail):
        at = tail.find(part, pos)
        offsets.append((at, part))
        pos = at + len(part)
    for at, sentence in reversed(offsets):
        found = [(m.start(), m.end()) for m in _DOC_PHRASE_RE.finditer(sentence) if ours(m.start(), m.end(), sentence)]
        for start, end in reversed(found):
            if _OFFER_CUE_RE.search(sentence[:start]):
                return base + at + start, base + at + end
        if bare:
            hits = list(re.finditer(r"\b(?:(?:the|our|this|my|a|an)\s+)?" + re.escape(bare) + r"\b", sentence, re.I))
            if hits:
                return base + at + hits[-1].start(), base + at + hits[-1].end()
        if found:
            return base + at + found[-1][0], base + at + found[-1][1]
    return None


def _end_sentence(text: str) -> str:
    """A sentence that read on to an address ("…is in the brief:") ends
    where its link is: the colon goes, and a full stop comes only if it
    does not already end in one."""
    text = text.rstrip()
    if text.endswith(":"):
        text = text[:-1].rstrip()
    return text if text.endswith((".", "!", "?")) else text + "."


def _fold_links(paras: list[list[str]], labels: dict) -> tuple[list[list[str]], dict]:
    """An address alone on its line, after a sentence that names the
    document ("here's the project brief with the full scope"), goes into
    that sentence: the name becomes the link and the address's line goes.
    Returns the paragraphs left and {(para, line): (start, end, url)}."""
    links: dict = {}
    out = [list(p) for p in paras]
    for pi, lines in enumerate(out):
        for li, ln in enumerate(lines):
            m = _BARE_URL_LINE_RE.match(ln)
            if not m or m.group("label"):
                continue
            url, tail = _split_url(m.group("url"))
            # The sentence it belongs to: the line above in this paragraph,
            # else the last line of the paragraph before.
            if li > 0:
                where = (pi, li - 1)
            elif pi > 0 and out[pi - 1]:
                where = (pi - 1, len(out[pi - 1]) - 1)
            else:
                continue
            target = out[where[0]][where[1]]
            if where in links or not target or not target.strip():
                continue                    # (an address that already went into it, or a blank line)
            span = _doc_phrase(target, url, labels)
            if span is None:
                continue
            if target.rstrip().endswith(":"):
                # "Everything is in the brief:" read on to the address: now it ends where its link is.
                target = _end_sentence(target)
                out[where[0]][where[1]] = target
            links[where] = (span[0], span[1], url)
            lines[li] = None                                  # the address's own line goes
    kept: list[list[str]] = []
    moved: dict = {}
    for pi, lines in enumerate(out):
        row = []
        for li, ln in enumerate(lines):
            if ln is None:
                continue
            if (pi, li) in links:
                moved[(len(kept), len(row))] = links[(pi, li)]
            row.append(ln)
        if row:
            kept.append(row)
    return kept, moved


# [words](address): a link on its words, the form the editors keep and
# every email is built from (the HTML part shows the words, the text part
# "words (address)").
MD_LINK_RE = re.compile(r"\[([^\[\]\n]{1,300})\]\(((?:https?://|www\.)[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)\)", re.I)


def _md(text: str, url: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").replace("[", "(").replace("]", ")")).strip() or "here"
    return f"[{text}]({_href(url)})"


def _md_spans(line: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in MD_LINK_RE.finditer(line)]


def _inline_links(ln: str, labels: dict) -> str:
    """The addresses inside a sentence, each on words: the document's name,
    else "here" ("this link" after a preposition). Links already on words
    are left as they are; one whose words are only its own address gets
    the document's name."""
    def named(m):
        text, url = m.group(1), m.group(2)
        if _INLINE_URL_RE.fullmatch(text.strip()) or text.strip().rstrip("/").lower() == url.rstrip("/").lower():
            return _md(link_text(url, labels, inline=True), url)
        return m.group(0)
    ln = MD_LINK_RE.sub(named, ln)
    spans = _md_spans(ln)
    pieces, pos = [], 0
    for mm in _INLINE_URL_RE.finditer(ln):
        if any(a <= mm.start() < b for a, b in spans):
            continue                                    # inside a link that already has its words
        url, tail = _split_url(mm.group(1))
        if not url or "." not in url.split("//", 1)[-1]:
            continue
        start, end = mm.start(), mm.end()
        if start < pos:
            continue
        if not tail and start > pos and ln[start - 1] == "<" and ln[end:end + 1] == ">":
            start, end = start - 1, end + 1          # <https://…>: the brackets were only a wrapper
        pieces.append(ln[pos:start])
        pieces.append(_md(link_text(url, labels, inline=True, before=ln[:start]), url))
        pieces.append(tail)
        pos = end
    pieces.append(ln[pos:])
    return "".join(pieces)


def embed_links(body: str, labels: dict | None = None) -> str:
    """The body with every web address on words, as [words](address): an
    address alone on its line after a sentence that names the document goes
    into that sentence, on the document's name ("here's [the project
    brief](…)"); "Here's the brief: <address>" the same; a line that is
    only a link (or "label: link") becomes [View the project brief](…); an
    address inside a sentence goes on the document's name, else on "here".
    Idempotent: what is already on words stays."""
    labels = {_norm_url(k): v for k, v in (labels or {}).items() if k}
    body = re.sub(r"(?m)([.!?]):[ \t]*$", r"\1", normalize_newlines(body))   # "…we need.:" was only ever "…we need."
    paras = [p.split("\n") for p in re.split(r"\n[ \t]*\n", body.strip()) if p.strip()]
    paras, folded = _fold_links(paras, labels)
    out = []
    for pi, para_lines in enumerate(paras):
        lines = []
        for li, ln in enumerate(para_lines):
            if (pi, li) in folded:
                start, end, url = folded[(pi, li)]
                lines.append(ln[:start] + _md(ln[start:end], url) + ln[end:])
                continue
            m = _BARE_URL_LINE_RE.match(ln)
            if m:
                url, tail = _split_url(m.group("url"))
                prefix = (m.group("label") or "").strip()
                known = labels.get(_norm_url(url), "")
                span = _doc_phrase(prefix, url, labels) if prefix else None
                if span is not None and (span[0] > 0 or span[1] < len(prefix.rstrip())):
                    # "Here's the project brief: <address>": the name is the link, the address goes.
                    start, end = span
                    rest = prefix[end:]
                    ending = tail if tail.strip(".") else ("" if rest.rstrip().endswith((".", "!", "?")) else ".")
                    lines.append(prefix[:start] + _md(prefix[start:end], url) + rest + ending)
                elif not prefix or (known and prefix.lower().rstrip(".:") == known.lower().rstrip(".:")) or _looks_like_label(prefix):
                    lines.append(_md(link_text(url, labels, prefix), url) + tail)
                else:
                    lines.append(prefix + ": " + _md(link_text(url, labels), url) + tail)
                continue
            lines.append(_inline_links(ln, labels))
        out.append("\n".join(lines))
    return "\n\n".join(out)


def _links_line_html(ln: str) -> str:
    import html as _html
    pieces, pos = [], 0
    for m in MD_LINK_RE.finditer(ln):
        before, after = ln[:m.start()], ln[m.end():]
        # A link alone on its line (or after "…:") stands out; one inside a sentence reads with it.
        bold = not after.strip(" .,;:!?") and (not before.strip() or before.rstrip().endswith(":"))
        pieces.append(_html.escape(ln[pos:m.start()]))
        pieces.append(_anchor(m.group(2), m.group(1), bold=bold))
        pos = m.end()
    pieces.append(_html.escape(ln[pos:]))
    return "".join(pieces)


def links_to_html(md_body: str) -> str:
    """HTML paragraphs for a body whose links are already on words."""
    out = []
    for para in re.split(r"\n[ \t]*\n", normalize_newlines(md_body).strip()):
        if para.strip():
            out.append("<p style=\"margin:0 0 14px\">" + "<br>".join(_links_line_html(ln) for ln in para.split("\n")) + "</p>")
    return "".join(out)


def body_to_html(body: str, labels: dict | None = None) -> str:
    """The HTML paragraphs for a plain body, every web address a link on
    its words, never a raw address (see ``embed_links``)."""
    return links_to_html(embed_links(body, labels))


def normalize_newlines(text: str) -> str:
    """A browser posts a text box with CRLF line endings: one "\\n" for all."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def links_to_text(body: str) -> str:
    """The plain-text part: a link on words reads "words (address)" (plain
    text has nothing to click); an address that stands alone stays as it is."""
    def plain(m):
        text, url = m.group(1).strip(), m.group(2)
        if text.rstrip("/").lower() in (url.rstrip("/").lower(), _href(url).rstrip("/").lower()):
            return url
        return f"{text} ({url})"
    return MD_LINK_RE.sub(plain, normalize_newlines(body))


def link_labels(outreach, booking_url: str = "") -> dict:
    """URL → what to call it, from everything the profile names: the scope
    links, the inbox documents, the booking link."""
    labels = {}
    try:
        from harvey import enquiry
        for sl in enquiry.scope_links(outreach):
            labels[sl["url"]] = sl.get("label") or "the full scope"
    except Exception:
        pass
    for link in getattr(outreach, "inbox_links", None) or []:
        url, label = getattr(link, "url", ""), getattr(link, "label", "")
        if url:
            labels.setdefault(url, label or "the document")
    if booking_url:
        labels.setdefault(booking_url, "a time in my calendar")
    return labels


def booking_url(config) -> str:
    """The calendar link the profile offers (product.offer.booking_url), or ""."""
    try:
        return str(config.product.offer.booking_url or "").strip()
    except AttributeError:
        return ""


def render_parts(body: str, outreach, plain: bool = False, booking: str = "") -> tuple[str, str, object]:
    """(plain_text, html_or_empty, signature_image_path_or_None) for one
    outgoing email: the model-written plain body plus the operator's
    signature. HTML is produced only when a signature (or image) exists,
    so a plain configuration still sends plain text. ``plain`` (a first
    email) sends text only, with the signature's links and image left out."""
    import html as _html
    from pathlib import Path

    from harvey.paths import SIGNATURE_DIR

    sig_html = (getattr(outreach, "signature_html", "") or "").strip()
    sig_text = (getattr(outreach, "signature_text", "") or "").strip() or html_to_text(sig_html)
    if plain:
        sig_text = strip_links(sig_text)
        return links_to_text(body).rstrip() + (f"\n\n-- \n{sig_text}" if sig_text else ""), "", None
    image_name = Path(getattr(outreach, "signature_image", "") or "").name
    image_path = SIGNATURE_DIR / image_name if image_name else None
    if image_path is not None and not image_path.is_file():
        image_path = None

    plain_text = links_to_text(body).rstrip() + (f"\n\n-- \n{sig_text}" if sig_text else "")
    if not sig_html and image_path is None and not _URL_RE.search(body or ""):
        return plain_text, "", None     # nothing an HTML part would add

    # Every web address in the HTML part is a link on its own words
    # ("View the Towson brief"); the text part keeps the address, since
    # plain text has nothing else to click.
    html_body = body_to_html(body, link_labels(outreach, booking))
    img_tag = ('<img src="cid:signature-image" alt="" '
               'style="max-width:240px;max-height:120px;height:auto;width:auto;display:block;margin-top:8px">'
               if image_path is not None else "")
    if "{{logo}}" in sig_html:
        sig_html = sig_html.replace("{{logo}}", img_tag)
    elif img_tag:
        sig_html = sig_html + img_tag
    html = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,'
        'sans-serif;font-size:15px;line-height:1.5;color:#1d1d1f">'
        f"{html_body}"
        '<div style="margin-top:18px;font-size:14px;color:#3a3a3c">'
        f"{sig_html}</div></div>"
    )
    return plain_text, html, image_path


def set_body(msg, body: str, outreach, plain: bool = False, booking: str = "") -> None:
    """Put the body (and signature) on an EmailMessage: text only, or
    text + HTML alternative with the signature image embedded inline."""
    import mimetypes

    plain, html, image_path = render_parts(body, outreach, plain=plain, booking=booking)
    msg.set_content(plain)
    if html:
        msg.add_alternative(html, subtype="html")
        if image_path is not None:
            ctype, _ = mimetypes.guess_type(image_path.name)
            maintype, _, subtype = (ctype or "image/png").partition("/")
            try:
                msg.get_payload()[1].add_related(
                    image_path.read_bytes(), maintype=maintype, subtype=subtype,
                    cid="<signature-image>", filename=image_path.name)
            except OSError:
                pass


def add_deliverability_headers(msg, from_email: str, config) -> None:
    """List-Unsubscribe (mailto) so Gmail/Yahoo treat the mail as compliant
    bulk and show an Unsubscribe link instead of guessing; a reply with
    'unsubscribe' in the subject is honoured by the handler's opt-out rule."""
    email_cfg = getattr(getattr(config, "channels", None), "email", None)
    if from_email and getattr(email_cfg, "list_unsubscribe", True):
        msg["List-Unsubscribe"] = f"<mailto:{from_email}?subject=unsubscribe>"


def attach_files(msg, attachments: list | None) -> list[str]:
    """Attach each readable path to an EmailMessage with a guessed MIME
    type. Returns the names attached; unreadable paths are logged and
    skipped rather than failing the send."""
    import logging
    import mimetypes
    from pathlib import Path

    attached = []
    for raw in attachments or []:
        path = Path(raw)
        try:
            data = path.read_bytes()
        except OSError as e:
            logging.getLogger("harvey.mail").warning(f"Attachment {path.name} skipped: {e}")
            continue
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
        attached.append(path.name)
    return attached


def get_mail_provider(config, env) -> MailProvider | None:
    """Build the configured native provider, or None for instantly/unknown."""
    provider = (config.channels.email.provider or "").strip().lower()
    if provider == "gmail":
        from harvey.integrations.gmail import GmailProvider
        return GmailProvider(config, env)
    if provider == "smtp":
        from harvey.integrations.smtp_mail import SmtpImapProvider
        return SmtpImapProvider(config, env)
    if provider == "microsoft":
        from harvey.integrations.microsoft import MicrosoftProvider
        return MicrosoftProvider(config, env)
    if provider == "microsoft_app":
        from harvey.integrations.microsoft import MicrosoftAppProvider
        return MicrosoftAppProvider(config, env)
    return None
