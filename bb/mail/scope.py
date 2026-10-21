"""What the mail command center knows about the sender's needs.

The web profile writes them into the workspace's harvey.yaml (outreach:
sender_line, intent, sector_needs with their full-scope links). The
command center drafts replies from the same facts, so a reply run can be
told "send the scope link when they ask" and the right one goes in,
once per thread.
"""

from __future__ import annotations

import re
from pathlib import Path

from harvey import enquiry

SECTOR_WORDS = {
    "construction": ("build", "builder", "construction", "contractor", "site", "block", "units", "drawings"),
    "financial_advisory": ("advisor", "adviser", "portfolio", "savings", "invest", "wealth", "retirement"),
    "manufacturing": ("steel", "rebar", "supply", "supplier", "tons", "tonnes", "fabricat", "manufactur", "spec"),
    "logistics": ("freight", "pallet", "shipping", "haul", "deliver", "truck", "lane"),
    "accounting": ("bookkeeping", "payroll", "tax", "accounts", "year-end", "audit"),
    "legal": ("lease", "contract", "lawyer", "attorney", "legal", "dispute"),
    "real_estate": ("property", "realtor", "estate", "letting", "tenant"),
    "architecture_engineering": ("architect", "engineer", "design", "structural"),
    "insurance": ("insur", "cover", "policy", "premium"),
    "banking": ("loan", "bank", "facility", "credit"),
    "marketing_agency": ("marketing", "brand", "campaign", "website", "social"),
    "software": ("software", "app", "integration", "licen"),
    "it_services": ("it support", "migration", "servers", "network"),
    "hr_recruiting": ("hire", "recruit", "staffing", "candidate"),
    "energy": ("solar", "generator", "power", "energy"),
    "hospitality": ("venue", "rooms", "event", "catering"),
}


def from_config(cfg: dict | None) -> dict:
    """The scope the command center drafts from, from a harvey.yaml dict."""
    cfg = cfg or {}
    outreach = cfg.get("outreach") or {}
    persona = cfg.get("persona") or {}
    needs = []
    links, seen = [], set()
    for row in outreach.get("sector_needs") or []:
        if not isinstance(row, dict) or not (row.get("need") or "").strip():
            continue
        sectors = [str(s).lower() for s in (row.get("sectors") or [])]
        url = (row.get("details_url") or "").strip()
        item = {"sectors": sectors, "need": row["need"].strip(), "question": (row.get("question") or "").strip(),
                "url": url, "label": (row.get("details_label") or "").strip()}
        needs.append(item)
        if url and url not in seen:
            seen.add(url)
            links.append({"url": url, "label": item["label"], "need": item["need"], "sectors": sectors})
    general_url = (outreach.get("details_url") or "").strip()
    if general_url and general_url not in seen:
        links.append({"url": general_url, "label": (outreach.get("details_label") or "").strip(),
                      "need": (outreach.get("intent") or "").strip(), "sectors": []})
    name = (persona.get("name") or "").strip()
    sender = (outreach.get("sender_line") or "").strip()
    if not sender and name:
        role, company = (persona.get("role") or "").strip(), (persona.get("company") or "").strip()
        sender = f"{name}, {role} at {company}." if role and company else (f"{name} at {company}." if company else name)
    return {
        "mode": outreach.get("mode") or "offer",
        "name": name,
        "first": name.split()[0] if name else "",
        "sender_line": sender,
        "intent": (outreach.get("intent") or "").strip(),
        "needs": needs,
        "links": links,
        "ask": (outreach.get("ask") or "").strip(),
        "ask_detail": (outreach.get("ask_detail") or "").strip(),
        "instructions": (outreach.get("reply_instructions") or "").strip(),
    }


def load(root: Path | str) -> dict:
    """The scope from a workspace folder (its harvey.yaml); empty when
    there is none."""
    path = Path(root) / "harvey.yaml"
    try:
        import yaml
        return from_config(yaml.safe_load(path.read_text()) or {})
    except Exception:
        return from_config({})


def context_block(scope: dict | None) -> str:
    """The lines added to a drafting prompt in inquiry mode: who is
    writing, what they need, the documents and the link rule."""
    if not scope or scope.get("mode") != "inquiry":
        return ""
    from harvey.config import ASK_THEMES
    lines = [f"YOU ARE THE CUSTOMER HERE, NOT A SELLER. Who is writing, in their words: {scope['sender_line'] or scope['name']}"]
    if scope["needs"]:
        lines.append("WHAT YOU NEED, BY KIND OF BUSINESS:")
        for n in scope["needs"]:
            lines.append(f"- {', '.join(s.replace('_', ' ') for s in n['sectors']) or 'anyone'}: {n['need']}")
    if scope["intent"]:
        lines.append(f"WHAT YOU NEED IN GENERAL: {scope['intent']}")
    if scope["links"]:
        lines.append("DOCUMENTS WITH THE FULL SCOPE (refer to one by number; the link is filled in for you, once per thread):")
        for i, l in enumerate(scope["links"], 1):
            lines.append(f"- [{i}] {l['label'] or 'The full scope'} for {', '.join(s.replace('_', ' ') for s in l['sectors']) or 'anyone'}: {l['need'][:140]}")
        lines.append("The first reply in a thread always carries the matching document, whatever they asked: write one plain "
                     "sentence saying what is in it and why it is the quickest way for them to see whether this is something "
                     "they would take on, then put [SCOPE LINK n] alone on the next line (n = the number above), and end by "
                     "pointing them to it. Never write the address yourself. Once it has gone out in a thread it is not sent "
                     "again (refer to it as the brief they have); nothing is sent when they have said no.")
    else:
        lines.append("There is no document with the full scope; when they want details, give the facts above and offer to send the rest.")
    lines.append("Answer what they asked first, in the first person, with facts from above only. Never invent a number, date, "
                 "grade, place or commitment; when asked for one you do not have, say it is in the brief or for the call. "
                 "If they pitch something of their own, say in one line it is not what you are after and return to your need.")
    if scope["ask"] in ASK_THEMES:
        lines.append(f"WHAT YOU WOULD LIKE NEXT: {ASK_THEMES[scope['ask']]}" + (f" ({scope['ask_detail']})" if scope["ask_detail"] else "")
                     + ". Offer it once, plainly, when they are interested; take their step if they proposed one.")
    if scope["instructions"]:
        lines.append(f"STANDING INSTRUCTIONS FROM {scope['name'] or 'the sender'}: {scope['instructions']}")
    return "\n\n" + "\n".join(lines)


_NUMBERED = re.compile(r"\[\s*scope\s+link\s*(\d+)\s*\]", re.I)


def pick_link(scope: dict | None, body: str, thread_text: str = "") -> dict | None:
    """Which document a draft means: the numbered marker, the only link,
    the sector the thread talks about, else the general one."""
    links = (scope or {}).get("links") or []
    if not links:
        return None
    m = _NUMBERED.search(body or "")
    if m:
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(links):
            return links[idx]
    if len(links) == 1:
        return links[0]
    low = (thread_text or "").lower() + " " + (body or "").lower()
    best, best_hits = None, 0
    for link in links:
        hits = 0
        for sector in link["sectors"]:
            hits += sum(1 for w in SECTOR_WORDS.get(sector, ()) if w in low)
        hits += sum(1 for w in enquiry.need_nouns(link["need"]) if w in low) if hasattr(enquiry, "need_nouns") else 0
        if hits > best_hits:
            best, best_hits = link, hits
    return best or links[-1]


def apply_scope_link(body: str, scope: dict | None, outbound_texts, inbound_text: str = "",
                     thread_text: str = "", always: bool = True) -> tuple[str, str]:
    """Resolve the draft's marker into the right link, once per thread.
    With ``always`` (replies) the link goes in whether or not the model
    put a marker, as long as it has not gone out in this thread and the
    last message was not a no, an opt-out or a complaint (those get an
    answer, never the brief); a new email (``always=False``) carries it
    when the model asked or the instruction mentions the scope.
    Returns (body, url_included_or_empty)."""
    link = pick_link(scope, body, thread_text)
    url = link["url"] if link else ""
    shared = enquiry.already_shared(url, outbound_texts) if url else True
    text = _NUMBERED.sub("[SCOPE LINK]", body or "")
    declined = enquiry.is_decline(inbound_text)
    if declined:
        # Whatever the model wrote, no link goes to someone who said no.
        text, _ = enquiry.resolve_scope_marker(text, url, True)
        return text, ""
    out, included = enquiry.resolve_scope_marker(
        text, url, shared, wants=bool(url) and (always or enquiry.wants_details(inbound_text)),
        label=(link or {}).get("label", ""), need=(link or {}).get("need", ""))
    return out, (url if included else "")
