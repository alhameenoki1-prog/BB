"""Getting past the spam verdict: plain first emails, rotated wordings in
segment mode, and a block that pauses the sender instead of marking a
good address invalid."""
from datetime import datetime, timedelta, timezone

import pytest

from harvey.integrations.mail_provider import InboundMessage, bounce_kind, render_parts
from harvey.models.campaign import Campaign, EmailStep
from harvey.models.prospect import Prospect

from tests.test_outbox_native import Cfg, Env, FakeProvider, StubBrain, make_sender, state  # noqa: F401

BLOCKED = ("550-5.7.1 [203.0.113.5] Our system has detected that this message is likely unsolicited mail. "
           "To reduce the amount of spam sent to Gmail, this message has been blocked. 550 5.7.1 "
           "https://support.google.com/mail/?p=UnsolicitedMessageError")
UNKNOWN = "550 5.1.1 The email account that you tried to reach does not exist."


class Outreach:
    signature_html = 'Thomas Ryan<br>CEO, Greystone Development<br><a href="https://greystone.example">greystone.example</a> · <a href="https://linkedin.com/in/thomas">LinkedIn</a>'
    signature_text = ""
    signature_image = ""


def test_a_first_email_is_plain_text_with_a_links_free_signature():
    plain, html, image = render_parts("Hi Dana,\n\nShort note.\n\nThomas", Outreach(), plain=True)
    assert html == "" and image is None
    assert plain.endswith("-- \nThomas Ryan\nCEO, Greystone Development") and "http" not in plain and "linkedin" not in plain.lower()
    full, html, _ = render_parts("Hi Dana,\n\nShort note.\n\nThomas", Outreach())
    assert "<a href" in html and "greystone.example" in full
    assert bounce_kind(BLOCKED) == "judged as unsolicited mail" and bounce_kind(UNKNOWN) == "no such address"


class RecordingProvider(FakeProvider):
    async def send_email(self, to_email, subject, body, thread_ref="", in_reply_to="", **kw):
        r = await super().send_email(to_email, subject, body, thread_ref, in_reply_to)
        self.sent[-1]["plain"] = bool(kw.get("plain"))
        return r


async def _campaign(state, steps, n=1, status="draft", prefix="v"):
    ids = []
    for i in range(n):
        p = Prospect(id=f"{prefix}{i}", first_name=f"P{i}", email=f"{prefix}{i}@x{i}.example", email_status="provided")
        await state.add_prospect(p)
        ids.append(p.id)
    c = Campaign(id="", name="seg", channel="email", sequence=steps, prospect_ids=ids, status=status)
    c.id = await state.add_campaign(c)
    return c


@pytest.mark.asyncio
async def test_only_the_first_email_goes_plain_and_segments_rotate_wordings(state):  # noqa: F811
    provider = RecordingProvider()
    sender = make_sender(state, provider, require_approval=False)
    steps = [EmailStep(step=1, subject="Rebar, around 40 tons a month", body="{{first_name}} one wording of the note",
                       subjects=["Rebar from spring, 40 tons a month", "40 tons of rebar a month from spring"],
                       bodies=["{{first_name}} a second wording", "{{first_name}} a third wording"]),
             EmailStep(step=2, subject="Still looking", body="{{first_name}} follow-up", delay_days=0)]
    await _campaign(state, steps, n=9)
    for _ in range(4):          # a pass sends a bounded batch
        await sender._run_native()
    first = [s for s in provider.sent if "wording" in s["body"]]
    second = [s for s in provider.sent if "follow-up" in s["body"]]
    assert len(first) == 9 and len(second) == 9
    assert all(s["plain"] for s in first) and not any(s["plain"] for s in second)
    for wording in ("one wording of the note", "a second wording", "a third wording"):
        assert any(wording in s["body"] for s in first), wording
    assert len({s["subject"] for s in first}) == 3
    # The same recipient always gets the same wording (follow-ups must match what they received).
    assert EmailStep(step=1, subject="A", body="a", subjects=["B"], bodies=["b"]).variant("v3") == \
        EmailStep(step=1, subject="A", body="a", subjects=["B"], bodies=["b"]).variant("v3")

    # Switched off on the profile: nothing goes plain.
    Cfg.channels.email.plain_first_email = False
    try:
        provider.sent.clear()
        await _campaign(state, [EmailStep(step=1, subject="S", body="{{first_name}} x")], n=1, prefix="w")
        await sender._run_native()
        assert provider.sent and not provider.sent[0]["plain"]
    finally:
        del Cfg.channels.email.plain_first_email


@pytest.mark.asyncio
async def test_a_block_pauses_the_sender_and_keeps_the_address_valid(state):  # noqa: F811
    from harvey.agents.handler import Handler
    from harvey.agents.sender import COOLOFF_KEY

    provider = FakeProvider()
    sender = make_sender(state, provider, require_approval=False)
    handler = Handler(StubBrain(), state, Cfg(), Env())
    handler.provider = provider
    await _campaign(state, [EmailStep(step=1, subject="S", body="{{first_name}} x"),
                            EmailStep(step=2, subject="S2", body="{{first_name}} y", delay_days=5)], n=1)
    await sender._run_native()
    assert len(provider.sent) == 1
    sent = (await state.get_outbox(status="sent"))[0]

    def bounce(body):
        return InboundMessage(provider_id="b1", from_email="mailer-daemon@googlemail.com", subject="Delivery Status Notification (Failure)",
                              body=body, message_id="<b1@g>", in_reply_to=sent["message_id"], is_bounce=True)

    await handler._handle_bounce(bounce(BLOCKED))
    p = await state.get_prospect("v0")
    assert p.email_status == "provided"                      # a spam verdict, not a bad address
    assert await state.count_outbox_queued() == 0            # the follow-up to them is dropped
    until = await state.get_setting(COOLOFF_KEY)
    assert until and datetime.fromisoformat(until) > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5)
    reason = await state.get_setting("sender_skip_reason")
    assert "judged as unsolicited mail" in reason and "pauses for 6 hours" in reason
    assert "Bounced: 550-5.7.1" in (await state.get_outbox_item(sent["id"]))["error"]
    assert (await state.get_setting("bounce_count") or "0") in ("", "0", None)   # not the bad-list counter

    # Third block of the day: a full day off.
    await handler._handle_bounce(bounce(BLOCKED))
    await handler._handle_bounce(bounce(BLOCKED))
    assert "pauses for 24 hours" in await state.get_setting("sender_skip_reason")
    assert datetime.fromisoformat(await state.get_setting(COOLOFF_KEY)) > datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=23)

    # While standing down nothing leaves, and the dashboard reason says so; afterwards sending resumes.
    before = len(provider.sent)
    await _campaign(state, [EmailStep(step=1, subject="S", body="{{first_name}} z")], n=1, prefix="w")
    await sender._run_native()
    assert len(provider.sent) == before and "stands down for another" in await state.get_setting("sender_skip_reason")
    await state.set_setting(COOLOFF_KEY, (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)).isoformat())
    await sender._run_native()
    assert len(provider.sent) == before + 1 and not await state.get_setting(COOLOFF_KEY)
    assert "stands down" not in (await state.get_setting("sender_skip_reason") or "")

    # An unknown address is still a bad address.
    sent2 = await state.find_outbox_by_message_id(f"<m{len(provider.sent)}@x>")
    assert sent2 and sent2["prospect_id"] == "w0"
    await handler._handle_bounce(InboundMessage(provider_id="b2", from_email="mailer-daemon@googlemail.com", subject="Undeliverable",
                                                body=UNKNOWN, message_id="<b2@g>", in_reply_to=sent2["message_id"], is_bounce=True))
    assert (await state.get_prospect("w0")).email_status == "invalid"
