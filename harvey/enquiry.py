"""Inquiry mode: the sender is the customer.

What a customer asks each kind of business for, which of the sender's
needs applies to a given recipient, the link to the full scope that goes
with that need, and the small deterministic pieces that keep replies on
the subject: the ``[SCOPE LINK]`` marker a model writes where the link
belongs, resolved here so a weak model cannot send it twice or to the
wrong sector.
"""

from __future__ import annotations

import re
from pathlib import Path

# What a customer asks this kind of business for: the thing, the details
# that let them answer, the shape of the one question. Keyed by the
# writer's closed SECTORS vocabulary.
SECTOR_ASKS = {
    "construction": "a job you want built or renovated: what it is, its size, where, when it should start; ask whether they can take it on and whether you can send the drawings",
    "real_estate": "a property to buy, sell, let or manage: the type, the area, rough size, timing; ask whether they handle that and what they need from you",
    "architecture_engineering": "a design or engineering job: the building or site, where, the stage you are at (land bought, sketches, permit), when; ask whether they take it on and what to send over",
    "financial_advisory": "being taken on as a client: where the money came from (a sale, savings, an inheritance), roughly how much in words and, if the need says, what it is for; ask whether they take on clients at that size and what the first step is",
    "banking": "an account, a loan or a facility: for what, roughly how much in words, when; ask whether they do that kind of lending or account and what they need to see",
    "insurance": "cover for a specific thing: the building, fleet, site or business, where, from when; ask whether they can quote it and what they need from you",
    "accounting": "bookkeeping, payroll, tax or a year-end: company size, invoice volume, the software, the year end, when; ask whether they work with companies that size and what they need to start",
    "legal": "a matter to handle: a lease, a contract, a dispute, a formation; what it is, its size and, if the need gives one, the date; ask whether they can take it on",
    "healthcare": "becoming a patient or arranging care: for whom, what for, when; ask whether they are taking new patients and how to book",
    "education": "a place, a course or training: for whom, what, when; ask whether there is room and what the next step is",
    "hr_recruiting": "a hire or a staffing need: the role, how many, where, the start date; ask whether they recruit for that and what they need from you",
    "marketing_agency": "a piece of work: a site, a launch, a campaign, a brand; what, by when, what exists already; ask whether they take on jobs that size and what they need to quote",
    "software": "a build, an integration or licences: what it must do, how many users, when; ask whether they take on that kind of work and what they need to scope it",
    "it_services": "support, a migration or a setup: how many people or machines, which systems, where, when; ask whether they take on a company your size and what they need to quote",
    "manufacturing": "a product made or supplied: what, how many or how much, delivered where and, if the need gives them, the spec and the date; ask whether they can make or supply that volume and who the spec should go to",
    "logistics": "freight or deliveries: what, how many pallets or loads, from where to where, how often, from when; ask whether they cover that lane and what they need to quote",
    "retail": "a bulk order or a stockist arrangement: what, how many, where to, when; ask whether they sell at that volume and how to order",
    "ecommerce": "an order or a wholesale account: what, how many, where to, when; ask whether they supply at that volume and how to set it up",
    "hospitality": "a booking: an event, rooms, a group; how many people, which dates, what for; ask whether they have the space on those dates and how to hold it",
    "nonprofit": "working with them on one specific programme: a donation in kind, volunteers, a joint project; what, when, where; ask whether it fits what they run and who to talk to",
    "government": "a permit, a service or a programme: what, where, when; ask whether they handle it and what to submit",
    "media": "coverage or production: a story, a shoot, a placement; what, when, where; ask whether they take it on and what they need from you",
    "energy": "a supply or an installation: solar, a generator, a power contract; what load or size, where, when; ask whether they can supply or install that and what they need to quote",
    "consulting": "a piece of advice or a project: the question, the company size, the timing; ask whether they take on an engagement like that and what they need to scope it",
    "agriculture": "produce or supply: what, how much, how often, delivered where, from when; ask whether they can supply that and how to order",
    "automotive": "vehicles or servicing: how many, which, when, where; ask whether they can supply or service that and what they need from you",
    "other": "decide from the facts what this business sells or does and ask for that, with how much, where and when taken from the need",
}

SECTOR_NOUNS = {
    "construction": "a construction company", "real_estate": "a real estate agency",
    "architecture_engineering": "an architecture or engineering practice",
    "financial_advisory": "a financial advisory practice", "banking": "a bank",
    "insurance": "an insurance broker", "accounting": "an accounting firm", "legal": "a law firm",
    "healthcare": "a healthcare practice", "education": "a school or training provider",
    "hr_recruiting": "a recruitment firm", "marketing_agency": "a marketing agency",
    "software": "a software company", "it_services": "an IT services company",
    "manufacturing": "a manufacturer or materials supplier", "logistics": "a freight or logistics company",
    "retail": "a retailer", "ecommerce": "an online retailer", "hospitality": "a hotel, restaurant or venue",
    "nonprofit": "a non-profit", "government": "a public body", "media": "a media or publishing company",
    "energy": "an energy company", "consulting": "a consultancy", "agriculture": "a farm or agricultural supplier",
    "automotive": "a vehicle dealer or garage", "other": "a business",
}

# How the reader's position changes a customer's note (the sales version
# lives in the writer as TIER_GUIDANCE).
INQUIRY_TIER_NOTES = {
    "executive": "The reader owns or runs the business and will decide; ask them directly whether they will take it on.",
    "manager": "The reader runs the work; ask whether they can take it on and what they would need from you (drawings, a bar list, statements, dates).",
    "individual": "The reader does the work themselves; the same plain request, no change of register.",
    "admin": ("The reader is an assistant or a shared inbox; keep it to the need and ask whether they can take it on "
              "or pass it to the person who does. Never remark on the inbox or on their role; just ask."),
}

# Facts that exist only in the prompt's worked examples. A draft that
# carries one copied the example instead of writing from the need.
EXAMPLE_LEAK_TERMS = (
    "scarsdale", "riverside", "tulsa", "houston", "precast company", "quickbooks", "bridge deck", "december 31",
    "24-unit", "40 tons", "#5 and #6", "twelve people", "300 invoices", "landscaping business",
    "high school teacher", "concrete podium", "wood frame", "my mother's house", "college in six years",
    "bar list", "savings account doing nothing", "lowest number",
)

# How many words in a row make a sentence copied rather than echoed.
COPY_NGRAM = 6
_EXAMPLE_START_RE = re.compile(r"WRITTEN THE RIGHT WAY", re.I)
_EXAMPLE_END_RE = re.compile(r"^(THE RULES|OUTPUT)\s*$", re.M)
_examples_cache: dict[str, tuple[float, str]] = {}


def prompt_examples(name: str) -> str:
    """The worked examples in ``prompts/<name>.md`` (right and wrong), as
    one text: everything from the "WRITTEN THE RIGHT WAY" heading to THE
    RULES or OUTPUT. Cached by file time. "" when there are none."""
    from harvey import paths
    path = Path(paths.PROMPTS_DIR) / f"{name}.md"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return ""
    hit = _examples_cache.get(str(path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        text = path.read_text()
    except OSError:
        return ""
    m = _EXAMPLE_START_RE.search(text)
    if not m:
        _examples_cache[str(path)] = (mtime, "")
        return ""
    rest = text[m.end():]
    e = _EXAMPLE_END_RE.search(rest)
    section = rest[:e.start()] if e else rest
    _examples_cache[str(path)] = (mtime, section)
    return section


def _ngrams(text: str, n: int) -> dict[tuple, str]:
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return {tuple(words[i:i + n]): " ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def copied_phrases(text: str, examples: str, allowed: str = "", n: int = COPY_NGRAM) -> list[str]:
    """Runs of ``n`` words a draft shares with the worked examples and not
    with the sender's own facts: the sentences a model lifted instead of
    writing. Overlapping runs are merged into one phrase each."""
    if not text or not examples:
        return []
    # The link marker is structure, not words: it sits after "is in the brief:" in every reply.
    text, examples = SCOPE_MARKER_RE.sub(" ", text), SCOPE_MARKER_RE.sub(" ", examples)
    mine, theirs, ok = _ngrams(text, n), _ngrams(examples, n), _ngrams(allowed, n)
    shared = [g for g in mine if g in theirs and g not in ok]
    if not shared:
        return []
    words = re.findall(r"[a-z0-9']+", text.lower())
    hits = set(shared)
    phrases, i = [], 0
    while i <= len(words) - n:
        if tuple(words[i:i + n]) in hits:
            j = i
            while j + 1 <= len(words) - n and tuple(words[j + 1:j + 1 + n]) in hits:
                j += 1
            phrases.append(" ".join(words[i:j + n]))
            i = j + n
        else:
            i += 1
    return phrases


def example_copying(text: str, allowed: str, prompt_name: str) -> list[str]:
    """Everything a draft took from the worked examples of ``prompt_name``:
    their facts (EXAMPLE_LEAK_TERMS) and their sentences (copied_phrases)."""
    return example_leaks(text, allowed) + copied_phrases(text, prompt_examples(prompt_name), allowed)

# Housekeeping sentences the upload writes into a contact's notes; they
# are for the operator, never for the model.
_HOUSEKEEPING_RE = re.compile(r"^(No name was given|Name (taken|read|matched)|Shared inbox|Verification:|Domain check)", re.I)

SCOPE_MARKER_RE = re.compile(r"\[\s*scope\s+link(?:\s*\d+)?\s*\]", re.I)
_LEAD_IN_RE = re.compile(r"\b(brief|scope|spec|note|document|folder|link|here|attached|page|sheet|details)\b", re.I)
_CLOSER_RE = re.compile(r"^(thanks|thank you|many thanks|best|best regards|kind regards|regards|warm regards|"
                        r"cheers|sincerely|all the best|yours|looking forward.*)[,.!]?$", re.I)


def plain_signoff(body: str, first: str) -> str:
    """A customer signs with a first name alone: drop a "Thanks," line
    above it and make sure the last line is that name."""
    first = (first or "").strip()
    lines = (body or "").rstrip().split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not first:
        return "\n".join(lines)
    last = lines[-1].strip()
    if last.lower() in (first.lower(), first.lower() + ","):
        lines[-1] = first
        j = len(lines) - 2
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0 and _CLOSER_RE.match(lines[j].strip()):
            del lines[j:len(lines) - 1]
        return "\n".join(lines).rstrip()
    if _CLOSER_RE.match(last):
        lines.pop()
        return "\n".join(lines).rstrip() + f"\n\n{first}"
    if re.match(rf"^(?:[A-Za-z ,]*?\b)?{re.escape(first)}(?:\s+\w+)?[.,]?$", last) and len(last.split()) <= 4:
        lines[-1] = first
        return "\n".join(lines).rstrip()
    return "\n".join(lines).rstrip() + f"\n\n{first}"


def split_signoff(body: str, first: str) -> tuple[str, str]:
    """(body without its last-line sign-off, the sign-off line or "")."""
    text = (body or "").rstrip()
    lines = text.split("\n")
    if lines and first and lines[-1].strip().lower() in (first.lower(), first.lower() + ","):
        return "\n".join(lines[:-1]).rstrip(), lines[-1].strip()
    return text, ""
_URL_RE = re.compile(r"https?://\S+", re.I)

# What a recipient writes when they want the scope: the cues that let code
# add the link even when the model forgot the marker.
_DETAIL_CUES = re.compile(
    r"\b(send (me |us |over |through )?(the |some |more |any )?(details|specs?|specifications?|scope|brief|"
    r"drawings|plans|documents?|info|information|requirements?|more)|tell (me|us) (a bit |a little )?more|"
    r"more (details?|information|about)|what (exactly|precisely) (do you|are you|is it)|what do you (need|require|have in mind)|"
    r"what('s| is) (the|your) (scope|spec|project|requirement)|can (you|i) see|share (the |your )?(scope|brief|specs?|details|drawings)|"
    r"full scope|happy to (help|take a look|quote)|send (it|them|that) (over|through|across))\b", re.I)


def sender_line(config) -> str:
    """Who is writing, in their own words; built from the persona when the
    profile has no line of its own."""
    outreach = getattr(config, "outreach", None)
    line = (getattr(outreach, "sender_line", "") or "").strip()
    if line:
        return line
    persona = config.persona
    role, company = (persona.role or "").strip(), (persona.company or "").strip()
    if role and company:
        return f"{persona.name}, {role} at {company}."
    if company:
        return f"{persona.name} at {company}."
    return f"{persona.name}."


def need_for(outreach, sector: str = "") -> dict:
    """The sender's need as it applies to one kind of business: the first
    sector row that covers ``sector``, else the general need. Returns
    {"need", "question", "url", "label", "sectors"}; strings may be empty."""
    sector = (sector or "").strip().lower()
    general = {
        "need": (getattr(outreach, "intent", "") or "").strip(),
        "question": "",
        "url": (getattr(outreach, "details_url", "") or "").strip(),
        "label": (getattr(outreach, "details_label", "") or "").strip(),
        "sectors": [],
    }
    for row in getattr(outreach, "sector_needs", None) or []:
        need = (getattr(row, "need", "") or "").strip()
        sectors = [s.lower() for s in (getattr(row, "sectors", None) or [])]
        if need and sector and sector in sectors:
            return {
                "need": need,
                "question": (getattr(row, "question", "") or "").strip(),
                # A row without its own link falls back to the general one.
                "url": (getattr(row, "details_url", "") or "").strip() or general["url"],
                "label": (getattr(row, "details_label", "") or "").strip() or general["label"],
                "sectors": sectors,
            }
    return general


def scope_links(outreach) -> list[dict]:
    """Every distinct scope link the sender has, with the sectors and the
    need it belongs to; the general link last. For the inbox auto-reply
    and the command center, which have no classified sector."""
    out, seen = [], set()
    for row in getattr(outreach, "sector_needs", None) or []:
        url = (getattr(row, "details_url", "") or "").strip()
        if url and url not in seen:
            seen.add(url)
            out.append({"url": url, "label": (getattr(row, "details_label", "") or "").strip(),
                        "need": (getattr(row, "need", "") or "").strip(),
                        "sectors": [s.lower() for s in (getattr(row, "sectors", None) or [])]})
    general = (getattr(outreach, "details_url", "") or "").strip()
    if general and general not in seen:
        out.append({"url": general, "label": (getattr(outreach, "details_label", "") or "").strip(),
                    "need": (getattr(outreach, "intent", "") or "").strip(), "sectors": []})
    return out


def sector_ask(sector: str) -> str:
    return SECTOR_ASKS.get((sector or "").strip().lower()) or SECTOR_ASKS["other"]


def sector_noun(sector: str) -> str:
    return SECTOR_NOUNS.get((sector or "").strip().lower()) or SECTOR_NOUNS["other"]


def clean_notes(text: str) -> str:
    """The operator's notes without the upload's housekeeping sentences."""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    return "\n".join(ln for ln in lines if ln and not _HOUSEKEEPING_RE.match(ln))


def example_leaks(text: str, allowed: str = "") -> list[str]:
    """Example-only facts that appear in a draft and not in the sender's
    own need or line."""
    low, ok = (text or "").lower(), (allowed or "").lower()
    return [t for t in EXAMPLE_LEAK_TERMS if t in low and t not in ok]


def wants_details(text: str) -> bool:
    """Does this message ask for the scope, specs, details or documents?"""
    return bool(_DETAIL_CUES.search(text or ""))


# Two kinds of no. A HARD no (an opt-out, a complaint, hostility, a legal
# threat) goes on the suppression list and gets nothing, ever: these are
# the replies that get a domain blacklisted when ignored. A SOFT no (not
# interested, booked up, not what we do) gets one closing note that
# leaves the brief with them and asks nothing, then silence.
_OPT_OUT_CUES = re.compile(
    r"\b(unsubscribe|opt[- ]?out|remove (me|us)|take (me|us) off|stop (emailing|contacting|sending|writing)|"
    r"do(n't| not) (contact|email|write to) (me|us)( again)?|never (contact|email) (me|us)|delete my (data|info|information)|"
    r"(this is|it's|its|you're|you are) spam|spam(ming)?( me| us)?|scam(mer)?|phishing|report(ed|ing)? (you|this)|harass(ing|ment)?|"
    r"lawyer|attorney|legal action|lawsuit|cease and desist|blacklist|gdpr|f+u+c+k|shit|piss off|get lost|go away|leave (me|us) alone|"
    r"how (did|do) you get (my|this) (email|address)\?? ?(stop|remove|unsubscribe))\b", re.I)
_SOFT_NO_CUES = re.compile(
    r"\b(not interested|no thanks|no thank you|not (able|in a position) to take|can(not|'t) take (this|it|that|on)|"
    r"(we|i)('re| are|'m| am) not (taking|accepting|looking|able|in a position)|not for us|not something we (do|offer|handle)|"
    r"(we|i) (don't|do not) (do|offer|handle|take)|wrong (company|number|address|person to ask)|(fully )?booked( up| out)?|"
    r"at capacity|no capacity|too busy|not (right now|at the moment|at this time|this year|a fit|a good fit)|"
    r"(have to|will|'ll|going to) (pass|decline)|(we|i) (must|will have to) pass|maybe (later|next year)|"
    r"(don't|do not) (have|take) (the )?(capacity|bandwidth|room)|not taking (on )?(new|any) (work|clients|projects|jobs))\b", re.I)


def is_opt_out(text: str) -> bool:
    """A hard no: never contact them again, send nothing."""
    return bool(_OPT_OUT_CUES.search(text or ""))


def is_soft_no(text: str) -> bool:
    """A polite no that still gets the one closing note."""
    return bool(_SOFT_NO_CUES.search(text or ""))


def is_decline(text: str) -> bool:
    """Did they say no, in either way?"""
    return is_opt_out(text) or is_soft_no(text)


def closing_scope_sentence(label: str = "") -> str:
    """The lead-in for the one closing note after a soft no: the brief is
    left with them, nothing is asked."""
    what = label.strip() or "the brief"
    return f"No action needed on your side. I'll leave {what} with you in case it's ever relevant:"


def already_shared(url: str, texts) -> bool:
    """Has this link already gone out in this conversation? ``texts`` are
    the bodies already sent or queued (ours), any of which may hold it."""
    if not url:
        return True
    needle = url.strip().rstrip("/").lower()
    return any(needle in (t or "").lower() for t in texts)


def scope_sentence(label: str = "", need: str = "") -> str:
    """The one line that introduces the link when the model wrote none:
    what it is, and that it is the quickest way to see whether this is
    something they would take on."""
    what = label.strip() or "the brief"
    return (f"Everything you'd need to judge it is in {what}: what's needed, how much, where and when. "
            "Have a look and tell me whether it's something you'd take on:")


def pick_scope_link(outreach, text: str = "") -> dict | None:
    """The scope link that fits a message with no classified sector: the
    row whose need shares the most words with the text, else the general
    link (last), else the only one."""
    links = scope_links(outreach)
    if not links:
        return None
    if len(links) == 1:
        return links[0]
    low = (text or "").lower()
    best, hits = None, 0
    for link in links:
        n = sum(1 for w in need_nouns(link["need"]) if w in low)
        if n > hits:
            best, hits = link, n
    return best or links[-1]


def resolve_scope_marker(body: str, url: str, shared: bool, wants: bool = False, label: str = "",
                         need: str = "", sentence: str = "") -> tuple[str, bool]:
    """Turn the model's ``[SCOPE LINK]`` marker into the link, once.

    - marker present, link known, not yet shared → the URL on its own line
      (with a default introducing sentence when the model wrote none);
    - marker present but already shared or no link → the marker goes, and
      so does a dangling "here it is:" line before it;
    - no marker, but they asked for details, a link exists and was never
      sent → the sentence and the link are appended.
    Returns (body, link_included)."""
    text = (body or "").rstrip()
    had_marker = bool(SCOPE_MARKER_RE.search(text))
    if had_marker:
        lines = text.split("\n")
        out, included = [], False
        for ln in lines:
            if not SCOPE_MARKER_RE.search(ln):
                out.append(ln)
                continue
            stripped = SCOPE_MARKER_RE.sub("", ln).strip()
            if url and not shared and not included:
                prev = next((p for p in reversed(out) if p.strip()), "")
                if stripped:
                    out.append(stripped.rstrip(" :") + ":")
                elif not prev or not (prev.rstrip().endswith(":") or _LEAD_IN_RE.search(prev)):
                    # No lead-in of the model's own: add the default one.
                    out.append(sentence or scope_sentence(label, need))
                out.append(url)
                included = True
            else:
                # Drop the marker, and the "it's all here:" lead-in before it
                # (the rest of that line, if it said anything else, stays).
                if not stripped and out and out[-1].strip().endswith(":"):
                    head, sep, _tail = out[-1].rstrip(" :").rpartition(". ")
                    if sep:
                        out[-1] = head + "."
                    else:
                        out.pop()
                elif stripped:
                    out.append(stripped.rstrip(" :") + ".")
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
        return text, included
    if wants and url and not shared and url.lower() not in text.lower():
        return f"{text}\n\n{sentence or scope_sentence(label, need)}\n{url}", True
    return text, False


def need_nouns(need: str) -> set[str]:
    from harvey.spamcheck import need_nouns as _nn
    return _nn(need)


def need_is_concrete(text: str) -> bool:
    """A need a reader could repeat back: it carries a number, a month or
    season, or at least twelve words of plain description."""
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"\d", t):
        return True
    if re.search(r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
                 r"spring|summer|autumn|fall|winter|next (week|month|year)|this (month|year))\b", t, re.I):
        return True
    return len(re.findall(r"[A-Za-z']+", t)) >= 12


GENERIC_NEED_RE = re.compile(
    r"\b(work(ing)? with (you|your team|your firm)|start(ing)? work|working together|partner(ing)? with you|"
    r"your (services|team|firm)|new client|collaborat\w*|explor(e|ing) (options|a partnership|working))\b", re.I)
