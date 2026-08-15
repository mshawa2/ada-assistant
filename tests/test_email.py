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


def test_there_is_no_send_or_delete_tool():
    from ada.tools import specs

    names = {spec["name"] for spec in specs()}
    assert {"list_email", "search_email", "read_email"} <= names
    assert {n for n in names if "email" in n} == {"list_email", "search_email", "read_email"}


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
