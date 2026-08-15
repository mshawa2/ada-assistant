"""Email triage.

No IMAP connection is ever made: `imaplib.IMAP4_SSL` is replaced with a fake
that speaks just enough of the protocol. Credentials are synthetic and the
real environment is cleared first, so a developer's password is never read.
"""

import email.message
import imaplib

import pytest

from ada import settings, store
from ada.tools import email_tool


def _message(sender, subject, date="Fri, 14 Aug 2026 09:00:00 +0000", body="hello", html=None):
    msg = email.message.EmailMessage()
    msg["From"] = sender
    msg["To"] = "user@example.com"
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content(body)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    return msg


class FakeIMAP:
    """Enough of imaplib.IMAP4_SSL for these tools, and nothing more."""

    instances: list = []
    next_messages: dict = {}
    next_unseen: set = set()
    next_login_error = None

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.messages = dict(FakeIMAP.next_messages)
        self.unseen = set(FakeIMAP.next_unseen)
        self.login_error = FakeIMAP.next_login_error
        self.selected_readonly = None
        self.fetch_commands = []
        self.logged_out = False
        FakeIMAP.instances.append(self)

    def login(self, address, password):
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        self.address = address

    def select(self, mailbox, readonly=False):
        self.selected_readonly = readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "SEARCH":
            criteria = args[1:]
            if criteria and criteria[0] == "UNSEEN":
                uids = sorted(self.unseen)
            elif criteria and criteria[0] == "TEXT":
                needle = criteria[1].strip('"').lower()
                uids = sorted(uid for uid, m in self.messages.items() if needle in str(m).lower())
            else:
                uids = sorted(self.messages)
            return "OK", [b" ".join(str(u).encode() for u in uids)]

        if command == "FETCH":
            uid = int(args[0])
            self.fetch_commands.append(args[1])
            if uid not in self.messages:
                return "OK", [None]
            return "OK", [(b"1", self.messages[uid].as_bytes())]

        raise AssertionError(f"unexpected IMAP command: {command}")

    def close(self):
        pass

    def logout(self):
        self.logged_out = True


@pytest.fixture(autouse=True)
def mailbox(monkeypatch):
    """A configured, fake mailbox. Returns the uid->message dict to shape."""
    FakeIMAP.instances = []
    FakeIMAP.next_messages = {}
    FakeIMAP.next_unseen = set()
    FakeIMAP.next_login_error = None

    monkeypatch.setattr(email_tool.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setenv(email_tool.PASSWORD_KEY_NAME, "synthetic-app-password")
    store.write_json(settings._CONFIG_FILE, {"email": {"address": "user@example.com"}})
    settings._cache["stamp"] = None

    return FakeIMAP.next_messages


# --- setup ------------------------------------------------------------------


def test_without_an_address_it_explains_the_setup():
    store.write_json(settings._CONFIG_FILE, {"email": {"address": ""}})
    settings._cache["stamp"] = None

    with pytest.raises(email_tool.EmailUnavailable, match="App Password"):
        email_tool.list_email()


def test_without_a_password_it_explains_the_setup(monkeypatch):
    monkeypatch.delenv(email_tool.PASSWORD_KEY_NAME, raising=False)

    with pytest.raises(email_tool.EmailUnavailable, match="isn't set up yet"):
        email_tool.list_email()


def test_a_rejected_login_says_it_needs_an_app_password():
    FakeIMAP.next_login_error = "Invalid credentials"

    with pytest.raises(email_tool.EmailUnavailable, match="App Password, not the"):
        email_tool.list_email()


def test_the_configured_server_is_used(mailbox):
    store.write_json(
        settings._CONFIG_FILE,
        {"email": {"address": "user@example.com", "imap_host": "imap.fastmail.com", "imap_port": 993}},
    )
    settings._cache["stamp"] = None
    mailbox[1] = _message("a@b.com", "Hi")
    FakeIMAP.next_unseen.add(1)

    email_tool.list_email()

    assert FakeIMAP.instances[0].host == "imap.fastmail.com"


# --- read-only, structurally ------------------------------------------------


def test_the_mailbox_is_opened_read_only(mailbox):
    """The server itself refuses mutations from this session — not just a
    promise in the tool descriptions."""
    mailbox[1] = _message("a@b.com", "Hi")
    FakeIMAP.next_unseen.add(1)

    email_tool.list_email()

    assert FakeIMAP.instances[0].selected_readonly is True


def test_fetching_never_marks_mail_as_read(mailbox):
    """BODY.PEEK rather than BODY — reading a message in Ada must not change
    its state in the real mailbox."""
    mailbox[1] = _message("a@b.com", "Hi")

    email_tool.read_email("1")

    assert all("PEEK" in command for command in FakeIMAP.instances[0].fetch_commands)


def test_there_is_still_no_delete_tool():
    """Archiving moves a message; nothing here destroys one."""
    from ada.tools import specs

    names = {spec["name"] for spec in specs()}
    assert {n for n in names if "email" in n} == {
        "list_email",
        "search_email",
        "read_email",
        "mark_email_read",
        "archive_email",
        "reply_email",
    }
    assert not [n for n in names if "email" in n and "delete" in n]


def test_every_email_mutation_is_gated():
    """Reads are free; anything that changes the mailbox asks first — and it's
    gated in DEFAULTS too, so a missing config can't ungate a send."""
    from ada import safety

    for name in ("mark_email_read", "archive_email", "reply_email"):
        assert safety.requires_confirmation(name), name
        assert settings.DEFAULTS["autonomy"]["tools"][name] == settings.CONFIRM, name

    for name in ("list_email", "search_email", "read_email"):
        assert not safety.requires_confirmation(name), name


def test_the_connection_is_closed_even_when_nothing_is_found(mailbox):
    with pytest.raises(ValueError, match="No email with id"):
        email_tool.read_email("999")

    assert FakeIMAP.instances[-1].logged_out is True


# --- listing and searching --------------------------------------------------


def test_unread_mail_is_listed_with_ids(mailbox):
    mailbox[101] = _message("landlord@example.com", "Boiler service")
    mailbox[102] = _message("spam@example.com", "You won")
    FakeIMAP.next_unseen.update({101, 102})

    result = email_tool.list_email()

    assert "[101]" in result and "Boiler service" in result
    assert "landlord@example.com" in result
    assert "untrusted content" in result


def test_an_empty_inbox_says_so():
    assert email_tool.list_email() == "No unread email."


def test_read_mail_is_excluded_unless_asked_for(mailbox):
    mailbox[101] = _message("a@b.com", "Already read")

    assert email_tool.list_email(unread_only=True) == "No unread email."
    assert "Already read" in email_tool.list_email(unread_only=False)


def test_newest_mail_comes_first(mailbox):
    mailbox[1] = _message("a@b.com", "Oldest")
    mailbox[2] = _message("a@b.com", "Newest")
    FakeIMAP.next_unseen.update({1, 2})

    result = email_tool.list_email()

    assert result.index("Newest") < result.index("Oldest")


def test_the_count_is_clamped(mailbox):
    for i in range(1, 60):
        mailbox[i] = _message("a@b.com", f"Subject {i}")
        FakeIMAP.next_unseen.add(i)

    result = email_tool.list_email(count=999)

    assert result.count("from:") == email_tool.MAX_RESULTS


def test_searching_matches_content(mailbox):
    mailbox[1] = _message("landlord@example.com", "Boiler service", body="about the boiler")
    mailbox[2] = _message("airline@example.com", "Flight confirmation")

    result = email_tool.search_email("boiler")

    assert "Boiler service" in result
    assert "Flight confirmation" not in result


def test_an_empty_search_is_refused():
    with pytest.raises(ValueError, match="No search query"):
        email_tool.search_email("  ")


def test_a_quote_in_a_search_is_refused():
    """It would break out of the quoted IMAP search term."""
    with pytest.raises(ValueError, match="double quotes"):
        email_tool.search_email('boiler" OR SUBJECT "')


def test_no_search_matches_says_so(mailbox):
    mailbox[1] = _message("a@b.com", "Hello")

    assert "No email matching" in email_tool.search_email("nonexistent")


# --- reading one message ----------------------------------------------------


def test_reading_returns_the_headers_and_body(mailbox):
    mailbox[42] = _message("sam@example.com", "Lunch?", body="Are you free Thursday?")

    result = email_tool.read_email("42")

    assert "sam@example.com" in result
    assert "Lunch?" in result
    assert "Are you free Thursday?" in result
    assert "not instructions" in result


def test_html_only_mail_is_converted_to_text(mailbox):
    msg = email.message.EmailMessage()
    msg["From"] = "news@example.com"
    msg["Subject"] = "Newsletter"
    msg["Date"] = "Fri, 14 Aug 2026 09:00:00 +0000"
    msg.set_content("<html><body><p>Real content</p><script>bad()</script></body></html>", subtype="html")
    mailbox[7] = msg

    result = email_tool.read_email("7")

    assert "Real content" in result
    assert "bad()" not in result, "scripts must be stripped, like fetch_url does"


def test_plain_text_is_preferred_over_the_html_alternative(mailbox):
    mailbox[8] = _message("a@b.com", "Both", body="the plain version", html="<p>the html version</p>")

    result = email_tool.read_email("8")

    assert "the plain version" in result


def test_encoded_headers_are_decoded(mailbox):
    mailbox[9] = _message("=?utf-8?B?SsO2cmc=?= <jorg@example.com>", "=?utf-8?B?UsOpc3Vtw6k=?=")

    result = email_tool.read_email("9")

    assert "Jörg" in result
    assert "Résumé" in result


def test_a_very_long_body_is_truncated(mailbox):
    mailbox[10] = _message("a@b.com", "Long", body="x" * (email_tool.MAX_BODY_CHARS + 5000))

    result = email_tool.read_email("10")

    assert "[...truncated]" in result
    assert len(result) < email_tool.MAX_BODY_CHARS + 2000


def test_a_bad_id_is_refused_before_connecting():
    with pytest.raises(ValueError, match="isn't an email id"):
        email_tool.read_email("../../etc/passwd")

    assert FakeIMAP.instances == [], "nothing should connect for an obviously bad id"


def test_a_missing_message_is_a_clear_error():
    with pytest.raises(ValueError, match="may have been moved"):
        email_tool.read_email("999")


def test_email_is_rate_limited():
    """Each call is a fresh IMAP login; a loop looks like an attack to the
    provider and gets the account throttled."""
    assert settings.get("autonomy.limits.list_email.per_minute")
    assert settings.get("autonomy.limits.read_email.per_minute")


# --- mutations --------------------------------------------------------------


def _support_mutations(monkeypatch):
    """Teach the fake IMAP the mutation verbs, recording what it was asked."""
    FakeIMAP.stored = []
    FakeIMAP.moved = []
    FakeIMAP.expunged = False

    original_uid = FakeIMAP.uid

    def uid(self, command, *args):
        if command == "STORE":
            FakeIMAP.stored.append((args[0], args[1], args[2]))
            return "OK", [b""]
        if command in ("MOVE", "COPY"):
            FakeIMAP.moved.append((command, args[0], args[1]))
            return "OK", [b""]
        return original_uid(self, command, *args)

    monkeypatch.setattr(FakeIMAP, "uid", uid, raising=False)
    monkeypatch.setattr(FakeIMAP, "expunge", lambda self: setattr(FakeIMAP, "expunged", True), raising=False)
    monkeypatch.setattr(FakeIMAP, "capabilities", (b"IMAP4REV1", b"MOVE"), raising=False)


def test_marking_read_asks_the_server_to_set_the_seen_flag(monkeypatch, mailbox):
    _support_mutations(monkeypatch)
    mailbox[101] = _message("a@b.com", "One")
    mailbox[102] = _message("a@b.com", "Two")

    result = email_tool.mark_email_read(["101", "102"])

    assert FakeIMAP.stored == [("101", "+FLAGS", "(\\Seen)"), ("102", "+FLAGS", "(\\Seen)")]
    assert "2 emails" in result


def test_mutations_open_a_writable_session_while_reads_do_not(monkeypatch, mailbox):
    _support_mutations(monkeypatch)
    mailbox[101] = _message("a@b.com", "One")

    email_tool.list_email(unread_only=False)
    assert FakeIMAP.instances[-1].selected_readonly is True, "reads must stay read-only"

    email_tool.mark_email_read(["101"])
    assert FakeIMAP.instances[-1].selected_readonly is False, "a mutation needs write access"


def test_archiving_moves_rather_than_deletes(monkeypatch, mailbox):
    _support_mutations(monkeypatch)
    mailbox[101] = _message("a@b.com", "Newsletter")

    result = email_tool.archive_email(["101"])

    assert FakeIMAP.moved == [("MOVE", "101", '"[Gmail]/All Mail"')]
    assert "Archived" in result
    assert FakeIMAP.stored == [], "nothing should be flagged deleted when MOVE is available"


def test_archiving_falls_back_to_copy_and_expunge_without_move(monkeypatch, mailbox):
    _support_mutations(monkeypatch)
    monkeypatch.setattr(FakeIMAP, "capabilities", (b"IMAP4REV1",), raising=False)
    mailbox[101] = _message("a@b.com", "Newsletter")

    email_tool.archive_email(["101"])

    assert FakeIMAP.moved[0][0] == "COPY", "copy first, so a failure leaves the message"
    assert ("101", "+FLAGS", "(\\Deleted)") in FakeIMAP.stored
    assert FakeIMAP.expunged is True


def test_a_failed_archive_names_the_folder_to_check(monkeypatch, mailbox):
    _support_mutations(monkeypatch)
    monkeypatch.setattr(FakeIMAP, "uid", lambda self, c, *a: ("NO", [b""]), raising=False)
    mailbox[101] = _message("a@b.com", "Newsletter")

    with pytest.raises(ValueError, match="archive folder"):
        email_tool.archive_email(["101"])


@pytest.mark.parametrize("ids", [[], ["abc"], ["101", "../etc"], ""])
def test_bad_ids_are_refused_before_touching_the_mailbox(monkeypatch, ids):
    _support_mutations(monkeypatch)

    with pytest.raises(ValueError):
        email_tool.mark_email_read(ids)

    assert FakeIMAP.stored == []


def test_a_comma_separated_string_is_accepted(monkeypatch, mailbox):
    """Models reach for a string as often as a list; the meaning is clear."""
    _support_mutations(monkeypatch)
    mailbox[101] = _message("a@b.com", "One")
    mailbox[102] = _message("a@b.com", "Two")

    email_tool.mark_email_read("101, 102")

    assert len(FakeIMAP.stored) == 2


def test_the_batch_confirmation_names_the_messages(monkeypatch, mailbox):
    """'[101, 102]' is uncheckable; subjects are the difference between
    approving and rubber-stamping."""
    from ada import agent

    _support_mutations(monkeypatch)
    mailbox[101] = _message("landlord@example.com", "Boiler service")

    description = agent._describe_action("archive_email", {"email_ids": ["101"]})

    assert "ARCHIVE" in description
    assert "Boiler service" in description
    assert "landlord@example.com" in description


def test_the_confirmation_degrades_to_ids_if_the_mailbox_is_unreachable(monkeypatch):
    from ada import agent

    monkeypatch.delenv(email_tool.PASSWORD_KEY_NAME, raising=False)

    description = agent._describe_action("archive_email", {"email_ids": ["101"]})

    assert "[101]" in description, "a failed lookup must not block the gate"


# --- replying ---------------------------------------------------------------


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.sent = []
        self.logged_in = None
        FakeSMTP.instances.append(self)

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.tls = True

    def login(self, address, password):
        self.logged_in = (address, password)

    def send_message(self, message):
        self.sent.append(message)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(email_tool.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_tool.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP.instances


def test_a_reply_goes_to_the_original_sender(mailbox, smtp):
    mailbox[55] = _message("sam@example.com", "Lunch?")

    result = email_tool.reply_email("55", "Thursday works")

    sent = smtp[0].sent[0]
    assert sent["To"] == "sam@example.com"
    assert sent["Subject"] == "Re: Lunch?"
    assert "Thursday works" in sent.get_content()
    assert "sam@example.com" in result


def test_reply_to_header_wins_over_from(mailbox, smtp):
    msg = _message("noreply@example.com", "Ticket update")
    msg["Reply-To"] = "support@example.com"
    mailbox[56] = msg

    email_tool.reply_email("56", "thanks")

    assert smtp[0].sent[0]["To"] == "support@example.com"


def test_a_reply_threads_under_the_original(mailbox, smtp):
    msg = _message("sam@example.com", "Lunch?")
    msg["Message-ID"] = "<abc123@example.com>"
    mailbox[57] = msg

    email_tool.reply_email("57", "yes")

    sent = smtp[0].sent[0]
    assert sent["In-Reply-To"] == "<abc123@example.com>"
    assert "<abc123@example.com>" in sent["References"]


def test_an_existing_re_prefix_is_not_doubled(mailbox, smtp):
    mailbox[58] = _message("sam@example.com", "Re: Lunch?")

    email_tool.reply_email("58", "ok")

    assert smtp[0].sent[0]["Subject"] == "Re: Lunch?"


def test_an_empty_reply_is_refused(mailbox, smtp):
    mailbox[59] = _message("sam@example.com", "Lunch?")

    with pytest.raises(ValueError, match="empty reply"):
        email_tool.reply_email("59", "   ")

    assert smtp == []


def test_a_bad_id_is_refused_before_sending(smtp):
    with pytest.raises(ValueError, match="isn't an email id"):
        email_tool.reply_email("../etc", "hi")

    assert smtp == []


def test_the_reply_confirmation_shows_the_resolved_recipient(mailbox):
    """The address comes from headers the sender controls — a phishing mail
    can point Reply-To anywhere, so the user has to see where it's going."""
    from ada import agent

    msg = _message("noreply@bank.example", "Verify your account")
    msg["Reply-To"] = "attacker@evil.example"
    mailbox[60] = msg

    description = agent._describe_action("reply_email", {"email_id": "60", "text": "sure"})

    assert "SEND AN EMAIL REPLY" in description
    assert "cannot be unsent" in description
    assert "attacker@evil.example" in description, "the real recipient must be visible"


def test_the_send_uses_exactly_what_was_approved(mailbox, smtp):
    """The confirmation resolves the recipient and the send reuses it, so the
    address shown and the address used can't be two different lookups."""
    from ada import agent

    msg = _message("sam@example.com", "Lunch?")
    mailbox[61] = msg

    agent._describe_action("reply_email", {"email_id": "61", "text": "yes"})
    # The mailbox changes underneath — the approved target must still win.
    mailbox[61] = _message("someone-else@example.com", "Different")

    email_tool.reply_email("61", "yes")

    assert smtp[0].sent[0]["To"] == "sam@example.com"


def test_replying_is_rate_limited_like_any_other_send():
    assert settings.get("autonomy.limits.reply_email.per_hour")


def test_a_rejected_smtp_login_explains_the_app_password(monkeypatch, mailbox):
    import smtplib as real_smtplib

    mailbox[62] = _message("sam@example.com", "Lunch?")

    class RejectingSMTP(FakeSMTP):
        def login(self, address, password):
            raise real_smtplib.SMTPAuthenticationError(535, b"bad creds")

    monkeypatch.setattr(email_tool.smtplib, "SMTP", RejectingSMTP)

    with pytest.raises(email_tool.EmailUnavailable, match="App Password"):
        email_tool.reply_email("62", "hi")
