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


SALVAGED_REASON = "the model did not answer in the form asked for; its text was kept, so read it before it goes"
# A follow-up the model declined in prose instead of JSON.
_NOT_SUITABLE_RE = re.compile(r"(?i)[\"']suitable[\"']\s*:\s*false|\bnot\s+(?:a\s+)?suitable\b|\bunsuitable\b|"
                              r"\bI\s+(?:won't|will\s+not|wouldn't|would\s+not|shouldn't|should\s+not)\s+(?:write|send|follow)\b|"
                              r"\bno\s+natural\s+bridge\b|\b(?:personal|private)\s+(?:exchange|thread|conversation)\b")


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
              f"as that person.\nPERSONA: {settings.persona_notes.strip() or 'a straightforward professional'}\n"
              "These are ordinary business emails that arrived in this person's own inbox, from people they are "
              "corresponding with about their work. Write a normal, professional reply to a genuine business contact.\n\n"
              + QUALITY_STANDARD + scope_mod.context_block(scope)
              + "\n\nReturn ONLY JSON: {\"body\": \"...\", \"confidence\": \"high\"|\"medium\"|\"low\", "
                "\"confidence_reason\": \"...\", \"stance\": \"interested\"|\"question\"|\"soft_no\"|\"opt_out\"|\"other\"}. "
                "Confidence is low when their ask is unclear, when answering needs "
                "a fact or decision you do not have (money, dates, legal, commitments), or when a person should look first. "
                "stance is what THEIR last message is: opt_out when they ask not to be contacted, call it spam, or are "
                "hostile (then the body does not matter: nothing will be sent); soft_no when they decline politely.")
    from harvey import enquiry as _enq
    their_text = them.get("body_text") or them.get("body") or them.get("snippet") or ""
    closing = bool(scope and scope.get("mode") == "inquiry" and _enq.is_soft_no(their_text) and not _enq.is_opt_out(their_text))
    user = (f"THREAD: \"{thread.get('subject', '')}\" with {them.get('from_name') or them.get('from_email', '')}\n\n"
            f"{_mail_turns(messages, me)}\n\n"
            + (f"INSTRUCTIONS FROM {settings.display_name or 'the sender'}: {instructions.strip()}\n\n" if instructions.strip() else "")
            + ("THEY HAVE SAID NO. Write a CLOSING NOTE, not a reply that keeps the conversation going: thank them in one line "
               "for the straight answer, say in one plain sentence that there is nothing to do on their side and that you are "
               "leaving the brief with them in case it is ever relevant, then put the document marker alone on the next line as "
               "the rules say. No question, no ask, no next step, no \"if anything changes\". Under 60 words.\n\n" if closing else
               "Write the reply to the last message from THEM. Match their register and language. Answer what they "
               "actually asked. Under 160 words unless they asked for more."))
    result = {"body": "", "confidence": "low", "confidence_reason": "", "qa": {}, "attempts": 0, "scope_link": "", "stance": ""}
    outbound = [m.get("body_text") or m.get("body") or m.get("snippet") or "" for m in messages if m.get("direction") == "outbound"]
    thread_text = " ".join((m.get("body_text") or m.get("body") or m.get("snippet") or "") for m in messages)
    for attempt in (1, 2):
        result["attempts"] = attempt
        data = await brain.complete_json(system, user, salvage="body")
        if not isinstance(data, dict):
            raise BrainError("The model did not return a draft.")
        if data.get("_salvaged"):
            # Not the JSON asked for, but a usable answer: their stance is read
            # here instead, and a person looks at it before it goes.
            said = str(data.get("stance") or "").strip().lower()
            data = {**data, "confidence": "low", "confidence_reason": SALVAGED_REASON,
                    "stance": "opt_out" if said == "opt_out" or _enq.is_opt_out(their_text) else
                              "soft_no" if _enq.is_soft_no(their_text) else said or "other"}
        stance = str(data.get("stance") or "").strip().lower()
        result["stance"] = stance if stance in ("interested", "question", "soft_no", "opt_out", "other") else ""
        if result["stance"] == "opt_out":
            result.update(body="", confidence="low", confidence_reason="they asked not to be contacted", qa={"ok": False, "reasons": ["opt-out"]})
            return result
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


_STOP_WORDS = {"about", "after", "again", "also", "because", "before", "being", "below", "between", "could", "every",
               "first", "found", "great", "having", "hello", "other", "really", "should", "since", "still", "thanks",
               "their", "there", "these", "thing", "think", "those", "through", "under", "until", "where", "which",
               "while", "would", "write", "wrote", "yours", "regards", "please", "thank", "today", "tomorrow", "morning"}


def _topic_words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]{4,}", text or "") if w.lower() not in _STOP_WORDS}


async def draft_reconnect(brain: MailBrain, settings: MailboxSettings, mailbox: dict, thread: dict, messages: list[dict],
                          scope: dict | None = None, instructions: str = "") -> dict:
    """A follow-up months later in a thread we already had with someone:
    picks the conversation up where it left off, bridges to the project
    the way this thread allows, and carries the brief's link once.
    Returns {suitable, why, body, confidence, confidence_reason, qa,
    attempts, scope_link}; ``suitable`` is False for personal or private
    threads, or when no natural bridge exists, and nothing is written."""
    me = mailbox.get("address", "")
    first = (settings.display_name or me).split()[0] if (settings.display_name or me) else "I"
    them = next((m for m in reversed(messages) if m.get("direction") == "inbound" and not m.get("automated")), messages[-1] if messages else {})
    inbound_text = " ".join((m.get("body_text") or m.get("body") or m.get("snippet") or "") for m in messages if m.get("direction") == "inbound")
    thread_text = " ".join((m.get("body_text") or m.get("body") or m.get("snippet") or "") for m in messages)
    system = (f"You write email on behalf of {settings.display_name or me} <{me}>, in the first person, as that person.\n"
              f"PERSONA: {settings.persona_notes.strip() or 'a straightforward professional'}\n\n"
              + QUALITY_STANDARD + scope_mod.context_block(scope)
              + "\n\nTHE SITUATION: this is a conversation " + (settings.display_name or "you") + " already had with this person, "
                "months ago, in this same thread. You are writing the next message in it. First decide whether it is a thread "
                "to continue at all: a personal or private exchange (family, friends, a doctor, a bank, a landlord, a receipt, "
                "HR, a job application, a complaint, anything where news about " + first + "'s work would be out of place), "
                "or one with no natural bridge from what it was about to the project, is NOT suitable: say so and write "
                "nothing. Otherwise write the next message, under 140 words, that: (1) picks the thread up where it left "
                "off, naming the specific thing it was about and, where it fits, how it ended, in words a person who "
                "remembers it would use; (2) bridges in one or two sentences to what " + first + " has been building "
                "since (WHAT YOU NEED and the campaign goal above), from the angle THIS thread allows (what they do, what "
                "they asked, what they offered); (3) puts the brief's marker [SCOPE LINK n] alone on its own line where "
                "the link belongs, introduced in your own words as the quickest way to see the whole thing; (4) ends with at "
                "most one open question, or none. Not a template: every sentence comes from this thread and these facts. "
                "Never \"reaching out\", \"I hope this finds you well\", \"following up\", \"touch base\", \"circle back\".\n\n"
                "Return ONLY JSON: {\"suitable\": true|false, \"why\": \"...\", \"body\": \"...\", "
                "\"confidence\": \"high\"|\"medium\"|\"low\", \"confidence_reason\": \"...\"}. Confidence is high only "
                "when the bridge is natural and every fact in the body comes from the thread or the facts above.")
    user = (f"THREAD: \"{thread.get('subject', '')}\" with {them.get('from_name') or them.get('from_email', '')}, last message "
            f"{(messages[-1].get('sent_at') if messages else '') or ''}\n\n{_mail_turns(messages, me)}\n\n"
            + (f"INSTRUCTIONS FROM {settings.display_name or 'the sender'} FOR THESE FOLLOW-UPS: {instructions.strip()}\n\n" if instructions.strip() else "")
            + "Decide whether this thread is suitable, then write the next message in it as described. Same JSON.")
    result = {"suitable": True, "why": "", "body": "", "confidence": "low", "confidence_reason": "", "qa": {}, "attempts": 0, "scope_link": ""}
    outbound = [m.get("body_text") or m.get("body") or m.get("snippet") or "" for m in messages if m.get("direction") == "outbound"]
    for attempt in (1, 2):
        result["attempts"] = attempt
        data = await brain.complete_json(system, user, salvage="body")
        if not isinstance(data, dict):
            raise BrainError("The model did not return a draft.")
        if data.get("_salvaged"):
            raw = str(data.get("_raw") or "")
            if _NOT_SUITABLE_RE.search(raw) or _NOT_SUITABLE_RE.search(str(data.get("body") or "")[:200]):
                # It said, in its own words, that this thread is not one to write in.
                result.update(suitable=False, why=(str(data.get("why") or "") or raw.strip())[:200])
                return result
            data = {**data, "confidence": "low", "confidence_reason": SALVAGED_REASON}
        if data.get("suitable") is False or (not str(data.get("body") or "").strip() and data.get("why")):
            result.update(suitable=False, why=str(data.get("why") or "")[:200])
            return result
        body = strip_greeting_and_signoff(str(data.get("body") or "").strip(), settings.display_name)
        if scope and scope.get("mode") == "inquiry":
            # The link always goes in a follow-up (a hard no in the thread was
            # ruled out before drafting); months-old cues in the history are
            # not read as an answer to this message.
            body, result["scope_link"] = scope_mod.apply_scope_link(body, scope, outbound, inbound_text="",
                                                                     thread_text=thread_text, always=True)
        qa = qa_check("Re: " + thread.get("subject", ""), body, allowed_links=[result["scope_link"]] if result["scope_link"] else None)
        confidence = str(data.get("confidence") or "medium").lower()
        reason = str(data.get("confidence_reason") or "")[:300]
        # The bridge must stand on the thread: a body that shares nothing with
        # what they wrote is a template, whatever the model says (judged only
        # when their side gave it something to stand on).
        theirs = _topic_words(inbound_text)
        if theirs and not (_topic_words(body) & theirs):
            qa = {**qa, "ok": False, "reasons": qa.get("reasons", []) + ["does not pick up anything from the thread"]}
        result.update(body=body, confidence=confidence, confidence_reason=reason, qa=qa)
        if qa["ok"]:
            break
        user += ("\n\nYour previous draft failed the check: " + describe(qa["reasons"])
                 + ". Rewrite it from the thread itself, keeping the substance. Same JSON.")
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
        data = await brain.complete_json(system, user, salvage="body")
        if not isinstance(data, dict):
            raise BrainError("The model did not return a draft.")
        if data.get("_salvaged"):
            data = {**data, "confidence": "low", "confidence_reason": SALVAGED_REASON}
        body = strip_greeting_and_signoff(str(data.get("body") or "").strip(), settings.display_name)
        if scope and scope.get("mode") == "inquiry":
            body, result["scope_link"] = scope_mod.apply_scope_link(body, scope, [], inbound_text=instruction, always=False)
        subject = re.sub(r"(?i)^\s*(re|fwd?)\s*:\s*", "", str(data.get("subject") or "").strip())[:120]
        qa = qa_check(subject, body, allowed_links=[result["scope_link"]] if result["scope_link"] else None)
        from harvey.spamcheck import SUBJECT_REVIEW, subject_risk
        s_score, s_reasons = subject_risk(subject, body)
        if s_score >= SUBJECT_REVIEW:
            qa = {**qa, "ok": False, "reasons": qa.get("reasons", []) + ["subject: " + "; ".join(s_reasons)]}
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


def finalize(body: str, settings: MailboxSettings, greeting_name: str = "", link_labels: dict | None = None) -> tuple[str, str]:
    """(plain, html) with the greeting on top and the signature block, which
    always carries the project link, at the bottom. Enforced here, at
    send time, whatever the model wrote. In the HTML part every web
    address in the body is a link on its own words ("View the brief"),
    never a raw address; ``link_labels`` (url → name) says what to call
    each one."""
    import html as _html

    from harvey.integrations.mail_provider import body_to_html, html_to_text, links_to_text

    greeting = greeting_for(greeting_name)
    core = (body or "").strip()
    sig_html = (settings.signature_html or "").strip()
    link = (settings.project_link or "").strip()
    if link and link.rstrip("/").lower() in (body or "").lower():
        link = ""   # the body already carries it (a scope link that is also the project link): never twice
    sig_text = html_to_text(sig_html)           # the text part shows the address itself: nothing else to click there
    if link and link not in sig_text:
        sig_text = (sig_text + "\n" if sig_text else "") + link
    if link and link.lower() not in sig_html.lower():
        sig_html = (sig_html + "<br>" if sig_html else "") + f'<a href="{_html.escape(link)}">View the project</a>'
    plain = f"{greeting}\n\n{links_to_text(core)}\n\n-- \n{sig_text}".rstrip()
    labels = dict(link_labels or {})
    if settings.project_link:
        labels.setdefault(settings.project_link, "the project")
    html_body = body_to_html(core, labels)
    html = ('<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif;font-size:15px;'
            f'line-height:1.5;color:#1d1d1f"><p style="margin:0 0 14px">{_html.escape(greeting)}</p>{html_body}'
            f'<div style="margin-top:18px;font-size:14px;color:#3a3a3c">{sig_html}</div></div>')
    return plain, html
