"""The worker reads the IMAP inbox by position and never marks a message
read: the person's own mail client keeps its unread badges, and a reply
they opened on their phone first is still seen by the worker."""
import imaplib
import json

from harvey import paths
from harvey.integrations import smtp_mail


def _raw(mid, sender, subj):
    return (f"From: {sender}\r\nTo: me@mine.example\r\nSubject: {subj}\r\nMessage-ID: <{mid}@m>\r\n"
            f"Date: Mon, 1 Jan 2026 10:00:00 +0000\r\nContent-Type: text/plain\r\n\r\nbody {mid}\r\n").encode()


class FakeIMAP:
    """UID-aware: three messages, UIDs 11 (read), 12 (unread), 13 (unread)."""
    msgs = {11: (_raw("a", "ann@x.example", "Old, read"), True),
            12: (_raw("b", "bob@x.example", "Unread one"), False),
            13: (_raw("c", "cat@x.example", "Unread two"), False)}
    calls: list = []
    stored_flags: list = []
    readonly = None

    def __init__(self, host, port, timeout=None, **kw):
        pass

    def login(self, u, p):
        pass

    def select(self, folder, readonly=False):
        FakeIMAP.readonly = readonly
        return "OK", [b"3"]

    def response(self, code):
        return code, [b"777"] if code == "UIDVALIDITY" else [None]

    def uid(self, cmd, *args):
        FakeIMAP.calls.append((cmd, args))
        if cmd == "SEARCH":
            query = args[-1]
            if query == "UNSEEN":
                ids = [u for u, (_r, seen) in self.msgs.items() if not seen]
            else:
                lo = int(query.split()[1].split(":")[0])
                ids = [u for u in self.msgs if u >= lo] or [max(self.msgs)]   # IMAP: "n:*" never returns nothing
            return "OK", [" ".join(str(u) for u in ids).encode()]
        if cmd == "FETCH":
            assert "PEEK" in args[-1], "a fetch that would mark the message read"
            raw = self.msgs[int(args[0])][0]
            return "OK", [(b"1 (UID %d BODY[] {%d})" % (int(args[0]), len(raw)), raw)]
        raise AssertionError(cmd)

    def store(self, *a):
        FakeIMAP.stored_flags.append(a)
        return "OK", []

    def logout(self):
        pass


class Env:
    smtp_host = "smtp.mine.example"; smtp_port = 587; smtp_username = "me@mine.example"; smtp_password = "pw"
    smtp_reply_to = ""; imap_host = "imap.mine.example"; imap_port = 993; imap_username = ""; imap_password = ""


def test_replies_are_read_by_position_and_left_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
    p = smtp_mail.SmtpImapProvider(config=None, env=Env())

    # First read: what is still unread, opened read-only, bodies PEEKed; the position is saved.
    out = p._fetch_unseen(limit=50)
    assert [m.from_email for m in out] == ["bob@x.example", "cat@x.example"]
    assert FakeIMAP.readonly is True and FakeIMAP.stored_flags == []
    cursor = json.loads((tmp_path / "imap_cursor.json").read_text())
    assert cursor["imap.mine.example:me@mine.example:INBOX"] == {"validity": "777", "uid": 13}

    # The person opens the next reply on their phone first (it is "read"): the worker still sees it.
    FakeIMAP.msgs[14] = (_raw("d", "dan@x.example", "Read on the phone"), True)
    FakeIMAP.calls.clear()
    out = p._fetch_unseen(limit=50)
    assert [m.from_email for m in out] == ["dan@x.example"]
    assert FakeIMAP.calls[0] == ("SEARCH", (None, "UID 14:*"))
    assert json.loads((tmp_path / "imap_cursor.json").read_text())["imap.mine.example:me@mine.example:INBOX"]["uid"] == 14

    # Nothing new: the server's "n:*" quirk returns the newest message again; it is not re-read.
    assert p._fetch_unseen(limit=50) == []

    # A mailbox that was rebuilt (new UIDVALIDITY) starts over from what is still unread.
    FakeIMAP.response = lambda self, code: (code, [b"778"])
    for u in (12, 13):
        FakeIMAP.msgs[u] = (FakeIMAP.msgs[u][0], True)     # the person has read those by now
    FakeIMAP.msgs[15] = (_raw("e", "eve@x.example", "After the rebuild"), False)
    out = p._fetch_unseen(limit=50)
    assert [m.from_email for m in out] == ["eve@x.example"]
