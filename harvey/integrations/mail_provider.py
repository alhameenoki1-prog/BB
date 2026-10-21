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

NATIVE_PROVIDERS = ("gmail", "smtp", "microsoft")


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


BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail delivery subsystem")
BOUNCE_SUBJECTS = (
    "delivery status notification", "undeliverable", "returned mail",
    "delivery failure", "failure notice", "could not be delivered",
    "delivery incomplete", "address not found", "message blocked",
    "message not delivered", "delivery has failed",
)

# What a bounce means and what to do about it, from the SMTP code or the
# wording in the report. Order matters: the first match wins.
BOUNCE_HINTS = (
    # Order matters: the first match wins. The verdicts on the SENDER (a
    # block) come before "authentication", because a real DSN quotes the
    # original message's headers (DKIM-Signature, ARC-Authentication-Results)
    # further down, and those words must not turn a block into something else.
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
BLOCK_KINDS = ("judged as unsolicited mail", "content blocked", "sending too fast or too much")


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
            diag = _re.sub(r"\s+", " ", m.group(1)).strip(" .")[:220]
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


def render_parts(body: str, outreach, plain: bool = False) -> tuple[str, str, object]:
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
        return body.rstrip() + (f"\n\n-- \n{sig_text}" if sig_text else ""), "", None
    image_name = Path(getattr(outreach, "signature_image", "") or "").name
    image_path = SIGNATURE_DIR / image_name if image_name else None
    if image_path is not None and not image_path.is_file():
        image_path = None

    plain_text = body.rstrip() + (f"\n\n-- \n{sig_text}" if sig_text else "")
    if not sig_html and image_path is None:
        return plain_text, "", None

    paragraphs = [p.strip() for p in body.strip().split("\n\n") if p.strip()]
    html_body = "".join(
        "<p style=\"margin:0 0 14px\">" + _html.escape(p).replace("\n", "<br>") + "</p>"
        for p in paragraphs)
    img_tag = ('<img src="cid:signature-image" alt="" '
               'style="max-width:320px;height:auto;display:block;margin-top:6px">'
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


def set_body(msg, body: str, outreach, plain: bool = False) -> None:
    """Put the body (and signature) on an EmailMessage: text only, or
    text + HTML alternative with the signature image embedded inline."""
    import mimetypes

    plain, html, image_path = render_parts(body, outreach, plain=plain)
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
