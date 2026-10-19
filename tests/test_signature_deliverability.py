"""Signature rendering (plain + HTML + inline image), deliverability headers,
the warm-up ramp, the writer's engaging style, and the DNS checks."""

from datetime import datetime
from email import message_from_bytes
from email.message import EmailMessage

import pytest

import harvey.integrations.mail_provider as mp
from bb import deliverability
from harvey.agents.sender import daily_allowance
from harvey.agents.writer import Writer
from harvey.config import EnvConfig, HarveyConfig, OutreachConfig

from tests.test_outreach import BASE, _Brain, state  # noqa: F401

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)


def _cfg(**kw) -> HarveyConfig:
    return HarveyConfig(**{**BASE, "outreach": kw})


def test_plain_signature_keeps_the_email_plain_text(tmp_path, monkeypatch):
    monkeypatch.setattr(mp, "SIGNATURE_DIR", tmp_path, raising=False)
    plain, html, image = mp.render_parts("Hi Ada,\n\nQuick one.", OutreachConfig())
    assert plain == "Hi Ada,\n\nQuick one." and html == "" and image is None

    out = OutreachConfig(signature_text="Jane\nAcme")
    plain, html, image = mp.render_parts("Body", out)
    assert plain.endswith("-- \nJane\nAcme") and html == ""


def test_html_signature_with_links_and_inline_image(tmp_path, monkeypatch):
    import harvey.paths
    monkeypatch.setattr(harvey.paths, "SIGNATURE_DIR", tmp_path)
    (tmp_path / "logo.png").write_bytes(PNG)
    out = OutreachConfig(
        signature_html='Jane Doe<br><a href="https://acme.test">acme.test</a>{{logo}}',
        signature_image="logo.png")
    plain, html, image = mp.render_parts("Hi Ada,\n\nQuick <question> & more.", out)
    assert image == tmp_path / "logo.png"
    assert "Jane Doe" in plain and "acme.test" in plain and "<a" not in plain
    assert "&lt;question&gt; &amp; more." in html
    assert 'href="https://acme.test"' in html and 'src="cid:signature-image"' in html
    assert html.count("cid:signature-image") == 1  # placed where {{logo}} was

    msg = EmailMessage()
    msg["To"] = "ada@example.com"
    mp.set_body(msg, "Hi Ada,\n\nQuick one.", out)
    msg = message_from_bytes(msg.as_bytes())
    assert msg.get_content_type() == "multipart/alternative"
    parts = list(msg.walk())
    types = [p.get_content_type() for p in parts]
    assert "text/plain" in types and "text/html" in types and "image/png" in types
    img = next(p for p in parts if p.get_content_type() == "image/png")
    assert img["Content-ID"] == "<signature-image>"

    # Missing image file: HTML still renders, nothing embedded.
    out2 = OutreachConfig(signature_html="Jane", signature_image="gone.png")
    plain, html, image = mp.render_parts("B", out2)
    assert image is None and "Jane" in html and "cid:" not in html


def test_list_unsubscribe_header_follows_config():
    cfg = HarveyConfig(**BASE)
    msg = EmailMessage()
    mp.add_deliverability_headers(msg, "jane@acme.test", cfg)
    assert msg["List-Unsubscribe"] == "<mailto:jane@acme.test?subject=unsubscribe>"
    cfg2 = HarveyConfig(**{**BASE, "channels": {"email": {**BASE["channels"]["email"],
                                                          "list_unsubscribe": False}}})
    msg2 = EmailMessage()
    mp.add_deliverability_headers(msg2, "jane@acme.test", cfg2)
    assert msg2["List-Unsubscribe"] is None


def test_email_config_speed_and_ramp_fields():
    email = HarveyConfig(**BASE).channels.email
    assert email.sends_per_cycle == 8 and email.warmup_ramp is False and email.list_unsubscribe is True
    with pytest.raises(ValueError):
        HarveyConfig(**{**BASE, "channels": {"email": {**BASE["channels"]["email"], "sends_per_cycle": 0}}})
    with pytest.raises(ValueError):
        OutreachConfig(style="shouty")
    assert OutreachConfig(signature_image="../etc/passwd").signature_image == ""
    assert OutreachConfig(signature_image="logo.png").signature_image == "logo.png"


def test_daily_allowance_ramps_from_the_first_send():
    now = datetime(2026, 10, 19, 12, 0)
    assert daily_allowance(None, now, 0, False) == 0            # no cap, no ramp
    assert daily_allowance(None, now, 50, False) == 50
    assert daily_allowance(None, now, 0, True) == 20            # nothing sent yet: day 0
    assert daily_allowance("2026-10-19T08:00:00", now, 0, True) == 20
    assert daily_allowance("2026-10-16T23:59:00", now, 0, True) == 50
    assert daily_allowance("2026-10-16T23:59:00", now, 40, True) == 40   # cap still wins
    assert daily_allowance("2026-10-16T23:59:00Z", now, 1000, True) == 50
    assert daily_allowance("2026-07-01T00:00:00", now, 0, True) == 0     # ramp climbed past 500: no limit
    assert daily_allowance("garbage", now, 0, True) == 20


@pytest.mark.asyncio
async def test_sender_uses_per_cycle_size_and_ramp(state, monkeypatch):  # noqa: F811
    import harvey.agents.sender as sender_mod
    from harvey.models.prospect import Prospect

    class Provider:
        name = "smtp"
        def is_configured(self):
            return True
        async def send_email(self, *a, **kw):
            from harvey.integrations.mail_provider import SendResult
            return SendResult(ok=True, message_id="<x@y>", thread_ref="")

    for i in range(6):
        await state.add_prospect(Prospect(id=f"p{i}", first_name=f"P{i}", last_name="X",
                                          email=f"p{i}@b{i}.co", email_status="provided",
                                          company_id="", title="CEO"))
        await state.add_outbox_item(prospect_id=f"p{i}", campaign_id="", step=1,
                                    to_email=f"p{i}@b{i}.co", subject="s", body="b",
                                    send_at="2000-01-01T00:00:00", status="approved")
    cfg = HarveyConfig(**{**BASE, "channels": {"email": {
        **BASE["channels"]["email"], "max_daily_sends": 0, "sends_per_cycle": 2}}})
    sender = sender_mod.Sender(_Brain(), state, cfg, EnvConfig())
    sender.provider = Provider()
    sender.send_pacing = False
    sender.precleared = None
    await sender._drain_due()
    assert await state.count_outbox_sent_today() == 2

    # Warm-up: on its first day the mailbox gets 20; here max_daily=3 binds at 3 total.
    cfg2 = HarveyConfig(**{**BASE, "channels": {"email": {
        **BASE["channels"]["email"], "max_daily_sends": 3, "sends_per_cycle": 10, "warmup_ramp": True}}})
    sender.config = cfg2
    await sender._drain_due()
    assert await state.count_outbox_sent_today() == 3


@pytest.mark.asyncio
async def test_writer_engaging_style_changes_the_brief(state):  # noqa: F811
    brain = _Brain()
    concise = Writer(brain, state, _cfg(style="concise"), EnvConfig())
    assert concise._style_block() == "" and concise._length_rule == "Under 75 words"
    engaging = Writer(brain, state, _cfg(style="engaging"), EnvConfig())
    block = engaging._style_block()
    assert "ENGAGING" in block and "ONE easy question" in block
    assert engaging._length_rule == "Between 90 and 130 words"
    assert HarveyConfig(**BASE).outreach.style == "concise"


def test_dns_checks_with_fixes(monkeypatch):
    records = {
        "good.test": ["v=spf1 include:_spf.google.com ~all"],
        "google._domainkey.good.test": ["v=DKIM1; k=rsa; p=MIIB"],
        "_dmarc.good.test": ["v=DMARC1; p=none"],
    }
    monkeypatch.setattr(deliverability, "_txt", lambda name: records.get(name, []))
    monkeypatch.setattr(deliverability, "_has_mx", lambda d: d == "good.test")
    good = deliverability.check_domain_sync("Good.Test.")
    assert good["domain"] == "good.test" and good["score"] == 4 and not good["public"]
    assert all(c["ok"] for c in good["checks"]) and good["checks"][1]["detail"].endswith("'google').")

    bad = deliverability.check_domain_sync("bare.test")
    assert bad["score"] == 0
    names = {c["name"]: c for c in bad["checks"]}
    assert "spf.protection.outlook.com" in names["SPF"]["fix"]
    assert "DKIM" in names["DKIM"]["fix"] and "_dmarc" in names["DMARC"]["fix"]
    assert not names["Can receive mail (MX)"]["ok"]

    public = deliverability.check_domain_sync("gmail.com")
    assert public["public"] and public["checks"][0]["ok"] and "own domain" in public["checks"][0]["fix"]
    assert deliverability.check_domain_sync("") == {"domain": "", "public": False, "checks": [], "score": 0}
    assert deliverability.domain_of("Jane@Acme.Test") == "acme.test" and deliverability.domain_of("nope") == ""
