"""Manual Studio send must not require a campaign-only project link."""
from bb.mail.service import ready_to_send


def test_manual_sender_can_send_with_enabled_connected_account():
    mailbox = {"address": "sender@example.test", "enabled": 1, "status": "ok",
               "settings": {"signature_html": "", "project_link": ""}}
    assert ready_to_send(mailbox, manual=True) == (True, "")
    ok, reason = ready_to_send(mailbox)
    assert not ok and "signature" in reason and "project link" in reason


def test_manual_send_still_blocks_disabled_or_disconnected_sender():
    mailbox = {"address": "sender@example.test", "enabled": 0, "status": "ok",
               "settings": {}}
    assert not ready_to_send(mailbox, manual=True)[0]
    mailbox["enabled"] = 1
    mailbox["status"] = "reconnect"
    assert not ready_to_send(mailbox, manual=True)[0]


def test_campaign_sender_still_requires_profile():
    mailbox = {"address": "sender@example.test", "enabled": 1, "status": "ok",
               "settings": {"signature_html": "<b>Sender</b>",
                            "project_link": "https://example.test/brief"}}
    assert ready_to_send(mailbox) == (True, "")
