"""The reply-state engine: what a thread needs from me, from the messages
in it. Deterministic and cheap, so it runs on every ingest.

needs_reply       the last real message is theirs and I have not answered
awaiting_them     I spoke last in a two-way conversation
replied           I sent the last message and they have never written in it
no_reply_needed   the last message is automated or bulk mail
snoozed/dismissed set by hand, kept until the thread changes
"""

from harvey.integrations.mail_provider import looks_automated

STATES = ("needs_reply", "awaiting_them", "replied", "no_reply_needed", "snoozed", "dismissed", "unknown")
COUNTED = ("needs_reply", "awaiting_them", "replied", "no_reply_needed")


def message_is_automated(msg: dict) -> bool:
    if msg.get("automated"):
        return True
    headers = msg.get("headers") or {}
    return looks_automated(msg.get("from_email", ""), msg.get("subject", ""), headers)


def compute_state(messages: list[dict], current: str = "") -> tuple[str, str]:
    """(state, reason) for a thread whose messages are in time order.
    A manual snooze or dismiss survives until a new inbound message."""
    if not messages:
        return "unknown", "no messages"
    last = messages[-1]
    inbound = [m for m in messages if m.get("direction") == "inbound"]
    if current in ("snoozed", "dismissed"):
        return current, "set by hand"
    if last.get("direction") == "outbound":
        if inbound:
            return "awaiting_them", "you sent the last message; they have not answered yet"
        return "replied", "you sent the last message and nobody has written back"
    if message_is_automated(last):
        return "no_reply_needed", "automated or bulk mail"
    if all(message_is_automated(m) for m in inbound):
        return "no_reply_needed", "every message in this thread is automated"
    return "needs_reply", f"{last.get('from_name') or last.get('from_email')} wrote last"
