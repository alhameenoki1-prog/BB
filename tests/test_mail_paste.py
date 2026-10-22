"""Connecting a mailbox by pasting its login, in whatever shape."""

import json

import pytest

from bb.mail_autoconfig import guess_settings
from bb.mail_paste import parse_pasted_login, to_smtp_fields
from tests.test_mail_foundation import mail_client  # noqa: F401


def test_a_one_line_login():
    parsed, note = parse_pasted_login("you@company.com   my-app-password")
    assert parsed == {"email": "you@company.com", "password": "my-app-password"}
    smtp, _ = to_smtp_fields(parsed, guess_settings)        # servers worked out from the address
    assert smtp["smtp_host"] == "smtp.company.com" and smtp["imap_host"] == "imap.company.com"
    assert smtp["smtp_username"] == "you@company.com" and smtp["smtp_password"] == "my-app-password"
    # A Gmail app password with spaces, comma-separated, stays whole.
    parsed, _ = parse_pasted_login("jane@gmail.com, abcd efgh ijkl mnop")
    assert parsed["password"] == "abcd efgh ijkl mnop" and parsed["email"] == "jane@gmail.com"
    # The address and the servers on one line.
    parsed, _ = parse_pasted_login("bob@corp.com\tsecret\tsmtp.corp.com:587\timap.corp.com:993")
    assert parsed["smtp_host"] == "smtp.corp.com" and parsed["smtp_port"] == "587"
    assert parsed["imap_host"] == "imap.corp.com" and parsed["imap_port"] == "993" and parsed["password"] == "secret"


def test_a_providers_labelled_settings_block():
    parsed, note = parse_pasted_login(
        "Here are your settings:\n"
        "Username: me@work.example\n"
        "Password: s3cr3t pass\n"
        "Incoming mail server (IMAP): imap.work.example\n"
        "Port: 993 (SSL)\n"
        "Outgoing mail server (SMTP): smtp.work.example\n"
        "Port: 465\n")
    assert parsed["email"] == "me@work.example" and parsed["password"] == "s3cr3t pass"
    assert parsed["imap_host"] == "imap.work.example" and parsed["imap_port"] == "993"      # the bare Port binds to IMAP
    assert parsed["smtp_host"] == "smtp.work.example" and parsed["smtp_port"] == "465"       # then to SMTP
    assert "SMTP server" in note and "IMAP server" in note
    smtp, _ = to_smtp_fields(parsed, guess_settings)
    assert smtp["imap_port"] == "993" and smtp["smtp_port"] == "465"


def test_a_connection_url_and_json():
    parsed, _ = parse_pasted_login("imaps://jane%40acme.com:se%40cret@imap.acme.com:993")
    assert parsed["email"] == "jane@acme.com" and parsed["password"] == "se@cret"      # both un-escaped
    assert parsed["imap_host"] == "imap.acme.com" and parsed["imap_port"] == "993"
    parsed, _ = parse_pasted_login(json.dumps({"email": "a@b.com", "pass": "pw", "imap_host": "imap.b.com", "smtp_host": "smtp.b.com"}))
    assert parsed == {"email": "a@b.com", "password": "pw", "imap_host": "imap.b.com", "smtp_host": "smtp.b.com"}


def test_what_cannot_be_read_is_said_in_words():
    assert parse_pasted_login("") == (None, "Paste the mailbox's login first.")
    assert parse_pasted_login("just a note, no login")[0] is None
    assert parse_pasted_login("Username: bob (no at sign)\nPassword: x")[1].endswith("is not an email address.")
    assert "No password" in parse_pasted_login("Email: lonely@x.com")[1]
    # A custom domain with no password's servers still needs the servers, said plainly.
    with pytest.raises(ValueError, match="could not be worked out"):
        to_smtp_fields({"email": "x@no-such-provider-xyz.test", "password": "p"},
                       lambda e: (None, "No known provider; could not be worked out."))


def test_the_command_center_connects_from_a_pasted_login(mail_client, monkeypatch):  # noqa: F811
    import asyncio

    import bb.mail.service as service
    from tests.test_mail_foundation import _FakeAccount
    from tests.test_bb_web import _as_user
    client = mail_client
    _as_user(client)
    _FakeAccount.outcome = "ok"
    block = ("Email: dana@acme-mail.example\nPassword: drop-word\n"
             "IMAP server: imap.acme-mail.example\nSMTP server: smtp.acme-mail.example\n")
    r = client.post("/mail/accounts/paste", data={"pasted": block})
    assert r.status_code == 303 and "/mail/accounts/" in r.headers["location"] and "pasted" in r.headers["location"]
    mid = r.headers["location"].split("/mail/accounts/")[1].split("?")[0]
    from bb.accounts import Accounts
    from bb.workspaces import Workspace
    from tests.test_bb_web import USER
    ws = Workspace(client.settings, Accounts(client.settings.accounts_db).get_by_email(USER["email"]))
    box = next(m for m in asyncio.run(ws.mail_db().mailboxes()) if m["id"] == mid)
    creds = service.open_creds(client.settings.secret_key(), box)
    assert box["address"] == "dana@acme-mail.example" and creds["smtp_host"] == "imap.acme-mail.example".replace("imap", "smtp")
    assert creds["imap_host"] == "imap.acme-mail.example" and creds["password"] == "drop-word"
    # A bad paste is refused in words, nothing stored.
    r = client.post("/mail/accounts/paste", data={"pasted": "no login here"})
    assert "No%20email%20address" in r.headers["location"]
    _FakeAccount.outcome = "auth"
    r = client.post("/mail/accounts/paste", data={"pasted": "x@gmail.com secret"})
    assert "Sign-in%20refused" in r.headers["location"] and "app%20password" in r.headers["location"]
    _FakeAccount.outcome = "ok"
