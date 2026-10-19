"""Does this email read like spam to a filter?

A deterministic second opinion on every draft before it is queued. It is
not a model and does not understand the email; it counts the things spam
filters and recipients actually react to in cold mail: marketing words,
shouting, links, money and urgency, fake-reply subjects, pleading
openers. Each finding carries a weight; the total is the risk score.

Thresholds: below REVIEW the email goes out as written; at or above
REVIEW it is held for a human (or, in the writer, rewritten once first);
the reasons are kept so the Outbox can say what tripped it.
"""

import re

REVIEW = 4   # at or above: rewrite once, then hold for review

# (pattern, weight, label). Patterns are matched case-insensitively on
# subject and body together unless prefixed with "subject:".
_RULES = (
    # marketing / sales vocabulary — the biggest tell in cold mail
    (r"\b(opportunit(y|ies)|proposal|collaborat\w*|synerg\w*|solutions?|offering|offer you|"
     r"our (services|products|company)|we (provide|offer|specializ\w*|specialis\w*)|"
     r"partnership opportunity|business opportunity|mutually beneficial|win-win|value proposition|"
     r"boost|grow your|increase your|scale your|maximi[sz]e|unlock|elevate|supercharge|game.?changer|"
     r"cutting.?edge|best.in.class|leading provider|one.stop)\b", 2, "sales vocabulary"),
    (r"\b(free|guarantee[ds]?|risk.?free|no (cost|obligation|risk)|discount|% ?off|save (big|money)|"
     r"cheap(est)?|lowest price|special (offer|deal)|exclusive|limited (time|offer)|act now|"
     r"don.t miss|last chance|hurry|urgent|immediately|asap|winner|congratulations|bonus|"
     r"earn (money|cash)|make money|\$\$+|cash)\b", 3, "classic spam words"),
    (r"\b(click (here|below)|sign up|subscribe|unsubscribe|opt.?out|visit (our|my) (site|website)|"
     r"learn more|find out more|check out|see attached|attached (is|are|please)|download)\b", 2,
     "call-to-click wording"),
    (r"\b(dear (sir|madam|sir/madam|customer|friend|valued)|to whom it may concern|"
     r"i hope this (email|message|note) finds you well|hope you.?re (doing )?well|"
     r"i am (reaching|writing) out|reaching out|just (wanted|checking|following)|"
     r"touch(ing)? base|circle back|quick question|right person|per my last)\b", 2,
     "template openers and clichés"),
    (r"\b(100%|guaranteed results|double your|triple your|\d+x (your|more)|\d{2,}% )", 3, "numbers that promise"),
    # shouting and punctuation
    (r"!{1,}", 2, "exclamation marks"),
    (r"cs:\b[A-Z]{4,}\b(?!\s*(Ltd|LLC|Inc|PLC|GmbH))", 2, "words in capitals"),
    (r"[?!]{2,}|\.{4,}", 2, "stacked punctuation"),
    # links, money, attachments in a first contact
    (r"https?://|www\.|\b[a-z0-9-]+\.(com|net|org|io|co|ng|uk|biz|info)\b(/\S*)?", 2, "a web link"),
    (r"[$€£₦]\s?\d|\b\d+\s?(usd|eur|gbp|ngn|dollars|pounds|euros)\b", 2, "money amounts"),
    # fake-reply and marketing subjects
    (r"subject:^\s*(re|fw|fwd)\s*:", 4, "a fake reply or forward subject"),
    (r"subject:[!?$%]|subject:\b(free|offer|deal|opportunity|proposal|partnership|collaboration|"
     r"urgent|important|attention|invitation|introduction|inquiry|enquiry|request|quick question|"
     r"right person|follow.?up|following up|checking in|touching base)\b", 2, "a marketing-style subject"),
    (r"subject:^.{60,}$", 1, "a very long subject"),
    (r"subject:^[a-z0-9 ,'.-]{1,25}$", 1, "a terse all-lowercase subject"),
    (r"subject:cs:\b[A-Z][a-z]+(\s+[A-Z][a-z]+){3,}\b", 1, "Title Case Every Word in the subject"),
)
_WORD_RE = re.compile(r"[A-Za-z']+")


def spam_risk(subject: str, body: str) -> tuple[int, list[str]]:
    """(score, reasons). Score 0 is clean; REVIEW or more means hold."""
    subject = (subject or "").strip()
    body = (body or "").strip()
    score = 0
    reasons: list[str] = []
    text = f"{subject}\n{body}"
    for pattern, weight, label in _RULES:
        target = text
        if pattern.startswith("subject:"):
            pattern, target = pattern[len("subject:"):], subject
        flags = re.M
        if pattern.startswith("cs:"):
            pattern = pattern[len("cs:"):]
        else:
            flags |= re.I
        hits = re.findall(pattern, target, flags)
        if hits:
            n = len(hits)
            score += weight * min(n, 3)
            reasons.append(f"{label} ({n})" if n > 1 else label)
    words = _WORD_RE.findall(body)
    if body and len(words) < 25:
        score += 2
        reasons.append("very short body (reads like phishing)")
    if len(words) > 220:
        score += 1
        reasons.append("long body")
    questions = body.count("?")
    if questions > 2:
        score += 1
        reasons.append(f"{questions} question marks")
    if body and body.count("\n\n") == 0 and len(words) > 60:
        score += 1
        reasons.append("one dense block of text")
    return score, reasons


def describe(reasons: list[str]) -> str:
    return "; ".join(reasons[:6]) + (" …" if len(reasons) > 6 else "")
