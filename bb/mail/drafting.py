"""Writing replies and new messages that read as written by a person for
this person: the full thread as context, the mailbox's persona, a
writing-quality standard in the prompt, and a pre-send check after it
(a phrase blocklist plus the spam-risk score, then a model judge).
A draft that fails is regenerated once with the findings; a second
failure routes it to review instead of sending."""

import re

from bb.mail import scope as scope_mod
from bb.mail.brain import BrainError, MailBrain
from bb.mail.models import MailboxSettings
from harvey.spamcheck import REVIEW, describe, spam_risk

BOILERPLATE = (
    r"i hope this (email|message|note) finds you well", r"i trust (this|you)", r"per my last (email|message)",
    r"please do not hesitate", r"do not hesitate to", r"as an ai", r"as a language model", r"i am an ai",
    r"i'm reaching out", r"i am reaching out", r"reaching out to", r"touch(ing)? base", r"circle back",
    r"just (wanted|checking|following) (to|in|up)", r"i wanted to (reach out|follow up|touch base)",
    r"looking forward to hearing from you", r"please find attached", r"at your earliest convenience",
    r"thank you for your (email|message)\.?$", r"kindly", r"hereby", r"as per", r"in today's fast-paced",
    r"i would like to take this opportunity", r"it is with great",
)
SENSITIVE = (
    r"\b(refund|invoice|payment|pay(ing)? you|wire|bank|account number|iban|\$\s?\d|€\s?\d|£\s?\d|₦\s?\d)\b",
    r"\b(contract|agreement|lawyer|legal|lawsuit|liab(le|ility)|terminate|breach)\b",
    r"\b(guarantee|promise|commit|deadline by|sign (this|the))\b",
    r"\b(salary|offer letter|fired|resign|complaint|harass)\b",
)

QUALITY_STANDARD = """WRITING STANDARD (every line of it applies):
- Sound like a person: varied sentence lengths and openings, contractions where natural, no corporate filler. Length matches the situation; a two-line question gets a short answer.
- Specific, never generic: refer to what they actually wrote or asked, by name, date, amount or detail. If the email could be sent to anyone, it is wrong.
- Never: "I hope this finds you well", "I trust this message", "per my last email", "please do not hesitate", "reaching out", "touch base", "circle back", "just wanted to", "as an AI", numbered lists in a conversational reply, restating their question back at them, over-hedging, three adjectives in a row.
- No spam signals: nothing in capitals, no exclamation marks, no urgency or pressure, no links other than what you are told to include, no manufactured enthusiasm.
- Say who you are only when the thread does not already make it obvious.
- Do not write a greeting line or a sign-off: both are added for you."""


def _mail_turns(messages: list[dict], me: str) -> str:
    out = []
    for m in messages[-12:]:
        who = "ME" if m.get("direction") == "outbound" else f"THEM ({m.get('from_name') or m.get('from_email')})"
        body = (m.get("body") or m.get("snippet") or "").strip()
        out.append(f"[{m.get('sent_at', '')}] {who}:\n{body[:2500]}")
    return "\n\n".join(out)


def qa_check(subject: str, body: str, allowed_links: list[str] | None = None) -> dict:
    """Deterministic pre-send check. {ok, reasons, score, sensitive}.
    ``allowed_links`` are the scope links the system itself put in; they
    are not counted as a web link."""
    for link in allowed_links or []:
        if link:
            body = (body or "").replace(link, "")
    low = (body or "").lower()
    reasons = [f"boilerplate: '{re.search(p, low).group(0)}'" for p in BOILERPLATE if re.search(p, low)]
    # A reply inherits their subject, so only the body is scored, and a
    # short answer to a short question is fine where a short cold email is not.
    score, spam_reasons = spam_risk("", body)
    words = len(re.findall(r"[A-Za-z']+", body or ""))
    if words >= 12 and "very short body (reads like phishing)" in spam_reasons:
        spam_reasons.remove("very short body (reads like phishing)")
        score -= 2
    reasons += spam_reasons
    if words < 12:
        reasons.append("too short to be a real answer")
    if re.search(r"(?m)^\s*(\d+[.)]|[-*•])\s", body or ""):
        reasons.append("list formatting in a conversational email")
    sensitive = [p for p in SENSITIVE if re.search(p, low)]
    return {"ok": not reasons and score < REVIEW, "reasons": reasons, "score": score, "sensitive": bool(sensitive)}


def greeting_for(name: str) -> str:
    first = (name or "").strip().split(" ")[0] if name else ""
    return f"Hi {first}," if first and first[0].isalpha() else "Hi,"


async def draft_reply(brain: MailBrain, settings: MailboxSettings, mailbox: dict, thread: dict,
                      messages: list[dict], instructions: str = "", scope: dict | None = None) -> dict:
    """{body, confidence, confidence_reason, qa, attempts, scope_link}. The
    body has no greeting or signature; ``finalize`` adds them. In an
    inquiry workspace ``scope`` (bb.mail.scope) puts the sender's needs and
    full-scope documents in front of the model, and the link it asks for
    is filled in here, once per thread."""
    me = mailbox.get("address", "")
    them = next((m for m in reversed(messages) if m.get("direction") == "inbound"), messages[-1] if messages else {})
    system = (f"You write email replies on behalf of {settings.display_name or me} <{me}>, in the first person, "
              f"as that person.\nPERSONA: {settings.persona_notes.strip() or 'a straightforward professional'}\n\n"
              + QUALITY_STANDARD + scope_mod.context_block(scope)
              + "\n\nReturn ONLY JSON: {\"body\": \"...\", \"confidence\": \"high\"|\"medium\"|\"low\", "
                "\"confidence_reason\": \"...\"}. Confidence is low when their ask is unclear, when answering needs "
                "a fact or decision you do not have (money, dates, legal, commitments), or when a person should look first.")
    user = (f"THREAD: \"{thread.get('subject', '')}\" with {them.get('from_name') or them.get('from_email', '')}\n\n"
            f"{_mail_turns(messages, me)}\n\n"
            + (f"INSTRUCTIONS FROM {settings.display_name or 'the sender'}: {instructions.strip()}\n\n" if instructions.strip() else "")
            + "Write the reply to the last message from THEM. Match their register and language. Answer what they "
              "actually asked. Under 160 words unless they asked for more.")
    result = {"body": "", "confidence": "low", "confidence_reason": "", "qa": {}, "attempts": 0, "scope_link": ""}
    outbound = [m.get("body_text") or m.get("body") or m.get("snippet") or "" for m in messages if m.get("direction") == "outbound"]
    thread_text = " ".join((m.get("body_text") or m.get("body") or m.get("snippet") or "") for m in messages)
    for attempt in (1, 2):
        result["attempts"] = attempt
        data = await brain.complete_json(system, user)
        if not isinstance(data, dict):
            raise BrainError("The model did not return a draft.")
        body = str(data.get("body") or "").strip()
        body = strip_greeting_and_signoff(body, settings.display_name)
        if scope and scope.get("mode") == "inquiry":
            body, result["scope_link"] = scope_mod.apply_scope_link(
                body, scope, outbound, inbound_text=them.get("body_text") or them.get("body") or them.get("snippet") or "",
                thread_text=thread_text)
        qa = qa_check("Re: " + thread.get("subject", ""), body, allowed_links=[result["scope_link"]] if result["scope_link"] else None)
        result.update(body=body, confidence=str(data.get("confidence") or "medium").lower(),
                      confidence_reason=str(data.get("confidence_reason") or "")[:300], qa=qa)
        if qa["ok"]:
            break
        user += ("\n\nYour previous draft failed the pre-send check: " + describe(qa["reasons"])
                 + ". Rewrite it without those, keeping the substance. Same JSON.")
    if result["confidence"] not in ("high", "medium", "low"):
        result["confidence"] = "medium"
    if result["qa"].get("sensitive") and result["confidence"] == "high":
        result["confidence"] = "medium"
        result["confidence_reason"] = (result["confidence_reason"] + " Touches money, legal or commitments.").strip()
    return result


async def draft_new(brain: MailBrain, settings: MailboxSettings, mailbox: dict, to_name: str, to_email: str,
                    instruction: str, scope: dict | None = None) -> dict:
    me = mailbox.get("address", "")
    system = (f"You write emails on behalf of {settings.display_name or me} <{me}>, in the first person, as that person.\n"
              f"PERSONA: {settings.persona_notes.strip() or 'a straightforward professional'}\n\n" + QUALITY_STANDARD
              + scope_mod.context_block(scope)
              + "\n\nReturn ONLY JSON: {\"subject\": \"...\", \"body\": \"...\", \"confidence\": \"high\"|\"medium\"|\"low\", "
                "\"confidence_reason\": \"...\"}. The subject is plain and honest, sentence case, 3 to 8 words, no 'Re:'.")
    user = (f"Write a new email to {to_name or to_email} <{to_email}>.\n\nWHAT TO SAY, in the sender's words: {instruction.strip()}\n\n"
            "One-to-one, specific to this person, under 160 words.")
    result = {"subject": "", "body": "", "confidence": "low", "confidence_reason": "", "qa": {}, "attempts": 0, "scope_link": ""}
    for attempt in (1, 2):
        result["attempts"] = attempt
        data = await brain.complete_json(system, user)
        if not isinstance(data, dict):
            raise BrainError("The model did not return a draft.")
        body = strip_greeting_and_signoff(str(data.get("body") or "").strip(), settings.display_name)
        if scope and scope.get("mode") == "inquiry":
            body, result["scope_link"] = scope_mod.apply_scope_link(body, scope, [], inbound_text=instruction)
        subject = re.sub(r"(?i)^\s*(re|fwd?)\s*:\s*", "", str(data.get("subject") or "").strip())[:120]
        qa = qa_check(subject, body, allowed_links=[result["scope_link"]] if result["scope_link"] else None)
        result.update(subject=subject, body=body, confidence=str(data.get("confidence") or "medium").lower(),
                      confidence_reason=str(data.get("confidence_reason") or "")[:300], qa=qa)
        if qa["ok"]:
            break
        user += ("\n\nYour previous draft failed the pre-send check: " + describe(qa["reasons"])
                 + ". Rewrite it without those, keeping the substance. Same JSON.")
    return result


async def judge(brain: MailBrain, body: str, context: str) -> dict:
    """Optional model judge: would a normal person believe a human wrote
    this specifically for them? {score 1-5, verdict}. Never raises."""
    try:
        data = await brain.complete_json(
            "You judge emails. Answer ONLY JSON: {\"score\": 1-5, \"why\": \"...\"}. 5 = clearly written by a "
            "person for this exact recipient; 1 = generic, templated or machine-sounding.",
            f"CONTEXT:\n{context[:1500]}\n\nEMAIL:\n{body[:2000]}", max_tokens=200)
        score = int(data.get("score", 3)) if isinstance(data, dict) else 3
        return {"score": max(1, min(5, score)), "why": str((data or {}).get("why", ""))[:200]}
    except Exception:
        return {"score": 0, "why": "judge unavailable"}


_SIGNOFF_RE = re.compile(r"(?im)^\s*(best( regards| wishes)?|regards|kind regards|warm regards|thanks( again)?|thank you|cheers|sincerely|yours[^\n]*|all the best|talk soon|speak soon)[,.!]?\s*$")


def strip_greeting_and_signoff(body: str, name: str = "") -> str:
    from harvey.names import strip_greeting
    body = strip_greeting(body or "")
    lines = body.rstrip().split("\n")
    # Drop a trailing sign-off and the name line(s) after it.
    for i in range(len(lines) - 1, max(-1, len(lines) - 5), -1):
        if _SIGNOFF_RE.match(lines[i]):
            lines = lines[:i]
            break
    first = (name or "").split(" ")[0]
    while lines and (not lines[-1].strip() or (first and lines[-1].strip().lower() in (first.lower(), (name or "").lower()))):
        lines.pop()
    return "\n".join(lines).strip()


def finalize(body: str, settings: MailboxSettings, greeting_name: str = "") -> tuple[str, str]:
    """(plain, html) with the greeting on top and the signature block, which
    always carries the project link, at the bottom. Enforced here, at
    send time, whatever the model wrote."""
    import html as _html

    from harvey.integrations.mail_provider import html_to_text

    greeting = greeting_for(greeting_name)
    core = (body or "").strip()
    sig_html = (settings.signature_html or "").strip()
    link = (settings.project_link or "").strip()
    if link and link.rstrip("/").lower() in (body or "").lower():
        link = ""   # the body already carries it (a scope link that is also the project link): never twice
    if link and link.lower() not in sig_html.lower():
        sig_html = (sig_html + "<br>" if sig_html else "") + f'<a href="{_html.escape(link)}">{_html.escape(link)}</a>'
    sig_text = html_to_text(sig_html)
    if link and link not in sig_text:
        sig_text = (sig_text + "\n" if sig_text else "") + link
    plain = f"{greeting}\n\n{core}\n\n-- \n{sig_text}".rstrip()
    paragraphs = [p.strip() for p in core.split("\n\n") if p.strip()]
    html_body = "".join(f'<p style="margin:0 0 14px">{_html.escape(p).replace(chr(10), "<br>")}</p>' for p in paragraphs)
    html = ('<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif;font-size:15px;'
            f'line-height:1.5;color:#1d1d1f"><p style="margin:0 0 14px">{_html.escape(greeting)}</p>{html_body}'
            f'<div style="margin-top:18px;font-size:14px;color:#3a3a3c">{sig_html}</div></div>')
    return plain, html
