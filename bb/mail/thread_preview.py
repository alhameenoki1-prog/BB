"""Fictional conversation previews for BB Studio.

These are visual-only examples, not RFC 5322 messages. Never write them to
mailboxes, enqueue them, or generate Message-ID / References headers.
"""
from datetime import date, timedelta
from html import escape

from bb.mail.composer import recipient_fields


def fictional_conversation(*, recipient: dict, sender_name: str,
                           subject: str, topic: str, today: date | None = None) -> str:
    """Return safe, clearly marked HTML for a fictional project conversation."""
    if len(subject) > 200 or len(topic) > 2000 or len(sender_name) > 150:
        raise ValueError("Preview fields exceed their maximum length.")
    fields = recipient_fields(recipient)
    name = fields["name"] or "Recipient"
    sender = sender_name.strip() or "Sender"
    subject = subject.strip() or "Project scope discussion"
    topic = topic.strip() or "the proposed project scope"
    day = today or date.today()
    messages = [
        (day - timedelta(days=3), sender, name,
         f"Hi {name}, I'd like to discuss {topic}. Would you be available to review the scope?"),
        (day - timedelta(days=2), name, sender,
         "Hi, thanks for reaching out. Could you share the main objectives and expected timeline?"),
        (day - timedelta(days=1), sender, name,
         "Certainly. We can review the objectives, deliverables, and timeline together."),
        (day, sender, name,
         f"Hi {name}, following up on {topic}. Would a calendar meeting work this week?"),
    ]
    items = []
    for when, frm, to, body in messages:
        items.append(
            '<article style="border:1px solid #ddd;border-radius:8px;padding:14px;margin:12px 0">'
            f'<div><strong>{escape(frm)}</strong> → {escape(to)}</div>'
            f'<small>{when.isoformat()} · SIMULATED — NOT SENT</small>'
            f'<p style="white-space:pre-wrap">{escape(body)}</p></article>'
        )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="referrer" content="no-referrer"><title>Fictional thread preview</title>'
        '</head><body style="font:15px system-ui;max-width:720px;margin:32px auto;padding:16px">'
        '<h1>BB Studio — Fictional conversation preview</h1>'
        '<p role="status" style="font-weight:bold;background:#fff3cd;padding:12px">'
        'SIMULATED CONVERSATION — NO PRIOR EMAILS EXIST. NOTHING HAS BEEN SENT.</p>'
        f'<h2>{escape(subject)}</h2>'
        + "".join(items)
        + '<p>This is a visual-only preview. It cannot be sent as a fabricated email history.</p>'
        '</body></html>'
    )
