"""The per-user send ledger: one account's daily limit split between the
campaigns working on it, atomically, and a suppression list for all."""

import threading

from harvey.ledger import ACTIVE_SECONDS, WINDOW_SECONDS, Ledger, from_env

T = 1_800_000_000.0


def test_an_account_limit_is_split_and_the_unclaimed_part_is_shared(tmp_path):
    lg = Ledger(tmp_path / "sendlog.db")
    lg.set_limit("Me@Acme.com", 10)
    # Alone, outreach may take all ten.
    for i in range(10):
        slot, pic = lg.reserve("me@acme.com", "outreach-a", "outreach", "Outreach A", now=T + i)
        assert slot, (i, pic)
    slot, pic = lg.reserve("me@acme.com", "outreach-a", "outreach", now=T + 11)
    assert slot is None and "daily limit of 10 is reached" in pic["reason"]

    lg2 = Ledger(tmp_path / "two.db")
    lg2.set_limit("me@acme.com", 10)
    lg2.join("me@acme.com", "scan-a", "scan", "Scan A", now=T)       # a quiet mailbox scan
    sent = 0
    for i in range(10):
        slot, pic = lg2.reserve("me@acme.com", "outreach-a", "outreach", now=T + i)
        sent += bool(slot)
    # Outreach gets its five; the other five wait for the scan.
    assert sent == 5 and "share of the account's daily limit is used (5 of 5)" in pic["reason"] and "Scan A" in pic["reason"]
    for i in range(5):
        assert lg2.reserve("me@acme.com", "scan-a", "scan", now=T + 20 + i)[0]
    assert lg2.reserve("me@acme.com", "scan-a", "scan", now=T + 30)[0] is None
    # A person pressing Send is held to the account's limit only, which is now reached.
    assert lg2.reserve("me@acme.com", "manual", "manual", priority=True, now=T + 31)[0] is None

    # The scan leaves (paused): its unused part goes back at once.
    lg3 = Ledger(tmp_path / "three.db")
    lg3.set_limit("me@acme.com", 10)
    lg3.join("me@acme.com", "scan-a", "scan", now=T)
    for i in range(5):
        assert lg3.reserve("me@acme.com", "outreach-a", "outreach", now=T + i)[0]
    assert lg3.reserve("me@acme.com", "outreach-a", "outreach", now=T + 6)[0] is None
    lg3.leave("me@acme.com", "scan-a")
    assert lg3.reserve("me@acme.com", "outreach-a", "outreach", now=T + 7)[0]
    # A member that stops checking in drops out by itself.
    lg3.join("me@acme.com", "scan-a", "scan", now=T)
    assert lg3.allowance("me@acme.com", "outreach-a", now=T + 8)["others"] == 1
    assert lg3.allowance("me@acme.com", "outreach-a", now=T + ACTIVE_SECONDS + 9)["others"] == 0
    # Sends older than a day no longer count.
    assert lg3.allowance("me@acme.com", "outreach-a", now=T + WINDOW_SECONDS + 100)["total"] == 0


def test_a_failed_send_gives_its_slot_back_and_no_limit_means_no_split(tmp_path):
    lg = Ledger(tmp_path / "s.db")
    lg.set_limit("a@x.com", 1)
    slot, _ = lg.reserve("a@x.com", "c1", "outreach", now=T)
    assert slot and lg.reserve("a@x.com", "c1", "outreach", now=T + 1)[0] is None
    lg.release(slot)
    assert lg.reserve("a@x.com", "c1", "outreach", now=T + 2)[0]
    # No limit known for an account: everything may go.
    for i in range(50):
        assert lg.reserve("b@x.com", "c1", "outreach", now=T + i)[0]
    assert lg.summary("b@x.com", now=T + 60)["total"] == 50 and lg.used_by("c1", now=T + 60) == 51


def _race(path, names, tries=20):
    got = []

    def worker(name):
        lg = Ledger(path)
        for _ in range(tries):
            slot, _ = lg.reserve("a@x.com", name, "outreach")
            if slot:
                got.append(slot)

    threads = [threading.Thread(target=worker, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return got


def test_two_processes_cannot_both_take_the_last_slot(tmp_path):
    # Four processes of one campaign: exactly the limit, never a slot twice.
    path = tmp_path / "race.db"
    Ledger(path).set_limit("a@x.com", 20)
    got = _race(path, ["c"] * 4)
    assert len(got) == 20 and len(set(got)) == 20
    # Four campaigns at once: never over the limit (each keeps a claim on its part).
    path2 = tmp_path / "race2.db"
    Ledger(path2).set_limit("a@x.com", 20)
    got = _race(path2, [f"c{i}" for i in range(4)])
    assert 5 <= len(got) <= 20 and len(set(got)) == len(got)
    assert Ledger(path2).summary("a@x.com")["total"] == len(got)


def test_suppression_holds_for_every_campaign_and_env_wiring(tmp_path):
    lg = Ledger(tmp_path / "s.db")
    lg.suppress("Dana@Steel.example", "asked not to be contacted", "outreach-a")
    lg.suppress("not-an-address", "x")
    assert lg.is_suppressed("dana@steel.example") and not lg.is_suppressed("sam@steel.example")
    assert lg.suppressed_among(["DANA@steel.example", "sam@steel.example", ""]) == {"dana@steel.example"}
    assert from_env({}) == (None, "", "", "")
    led, account, campaign, label = from_env({"HARVEY_LEDGER": str(tmp_path / "s.db"), "HARVEY_LEDGER_ACCOUNT": "Me@X.com",
                                              "HARVEY_LEDGER_CAMPAIGN": "c9", "HARVEY_LEDGER_LABEL": "Spring outreach"})
    assert led.is_suppressed("dana@steel.example") and account == "me@x.com" and campaign == "c9" and label == "Spring outreach"


def test_replies_have_no_limit_and_do_not_use_it_up(tmp_path):
    lg = Ledger(tmp_path / "r.db")
    lg.set_limit("me@acme.com", 4)
    lg.join("me@acme.com", "reply-a", "reply", "Replies A", now=T)       # a reply campaign: it only answers people
    # It takes no part of the limit: outreach may use all four.
    for i in range(4):
        assert lg.reserve("me@acme.com", "outreach-a", "outreach", now=T + i)[0]
    slot, pic = lg.reserve("me@acme.com", "outreach-a", "outreach", now=T + 5)
    assert slot is None and "daily limit of 4 is reached" in pic["reason"]
    # The limit is reached, yet every answer still goes: the campaign's own,
    # outreach's answers to its leads, and one a person sends by hand.
    for i in range(10):
        assert lg.reserve("me@acme.com", "reply-a", "reply", now=T + 10 + i, free=True)[0]
    assert lg.reserve("me@acme.com", "outreach-a", "outreach", kind="reply", now=T + 30, free=True)[0]
    assert lg.reserve("me@acme.com", "manual", "manual", priority=True, kind="reply", now=T + 31, free=True)[0]
    # They are written down, apart, and use none of the limit.
    summary = lg.summary("me@acme.com", now=T + 40)
    assert summary["total"] == 4 and summary["answers"] == 12 and summary["share"] == 4
    member = {m["campaign"]: m for m in summary["members"]}
    assert member["reply-a"]["answers"] == 10 and member["reply-a"]["answers_only"] and member["outreach-a"]["used"] == 4
    assert lg.allowance("me@acme.com", "outreach-a", now=T + 41)["total"] == 4
    # A scan still takes its part beside outreach.
    lg2 = Ledger(tmp_path / "r2.db")
    lg2.set_limit("me@acme.com", 4)
    lg2.join("me@acme.com", "reply-a", "reply", now=T)
    lg2.join("me@acme.com", "scan-a", "scan", now=T)
    assert lg2.allowance("me@acme.com", "outreach-a", now=T + 1)["share"] == 2


def test_replies_keep_to_what_the_mailbox_itself_allows(tmp_path):
    lg = Ledger(tmp_path / "h.db")
    lg.set_limit("me@gmail.com", 450, hard=20)          # (small numbers: a mailbox that allows 20 a day)
    # First emails stop a tenth short of it: the last two are for replies.
    sent = sum(bool(lg.reserve("me@gmail.com", "out", "outreach", now=T + i)[0]) for i in range(30))
    assert sent == 18
    slot, pic = lg.reserve("me@gmail.com", "out", "outreach", now=T + 40)
    assert slot is None and "kept for replies" in pic["reason"]
    # Replies take those two, then the mailbox's own maximum stops them too.
    assert lg.reserve("me@gmail.com", "rc", "reply", free=True, now=T + 50)[0]
    assert lg.reserve("me@gmail.com", "rc", "reply", free=True, now=T + 51)[0]
    slot, pic = lg.reserve("me@gmail.com", "rc", "reply", free=True, now=T + 52)
    assert slot is None and "own daily maximum of 20 is reached" in pic["reason"]
    # An answer never makes its campaign a member that holds a part.
    assert [m["campaign"] for m in lg.members("me@gmail.com", now=T + 60)] == ["out"]
    assert lg.used_split("me@gmail.com", "rc", now=T + 60) == (0, 2)
    # Setting the limit again without the maximum keeps the maximum.
    lg.set_limit("me@gmail.com", 450)
    assert lg.summary("me@gmail.com", now=T + 61)["hard"] == 20


def test_an_old_ledger_gets_the_new_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE limits (account TEXT PRIMARY KEY, daily INTEGER NOT NULL, updated_at REAL NOT NULL)")
    con.execute("INSERT INTO limits VALUES ('a@x.com', 5, 0)")
    con.commit(); con.close()
    lg = Ledger(path)
    assert lg.limit("a@x.com") == 5
    lg.set_limit("a@x.com", 5, hard=9)
    assert lg.summary("a@x.com")["hard"] == 9
