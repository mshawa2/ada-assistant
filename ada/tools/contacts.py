"""Looking someone up by name — carefully.

`send_message` deliberately refuses a name and demands a phone number or
email, because resolving "Mom" to a number is a guess and guessing wrong texts
a stranger. This module doesn't remove that rule; it gives Ada a way to do the
lookup *explicitly*, with the result visible, instead of inventing one.

The distinction matters, and it shapes the whole design:

- **This tool never sends.** It returns candidates. `send_message` still only
  accepts a real handle, so a name always passes through a step where the
  actual number is named out loud.
- **Ambiguity is surfaced, never resolved.** The very first contact on this
  machine to be tested — "Mom" — has *two* phone numbers. Picking one would be
  a coin flip on which device someone reads. Every handle is returned, each
  with its label, and the tool description tells the model to ask rather than
  choose.
- **Search only, never a dump.** There's no "list all contacts": an assistant
  that can recite someone's whole address book is a privacy problem waiting
  for a prompt injection. Results are capped.
- **Read-only.** No write API is exposed, so Ada cannot edit or delete a
  contact even if asked.

Access is requested lazily on first use, like the calendar, so someone who
never asks about a person never sees a permission prompt.
"""

import threading

from ada.tools.registry import tool

_ACCESS_TIMEOUT_SECONDS = 30
MAX_RESULTS = 8

_lock = threading.Lock()
_store = None
_denied_reason: str | None = None

_PERMISSION_HELP = (
    "Ada doesn't have permission to read your contacts. Grant it in "
    "System Settings > Privacy & Security > Contacts, for whichever app you run Ada "
    "from (Terminal, iTerm, etc), then try again."
)


class ContactsUnavailable(Exception):
    """Raised when contacts can't be reached — no framework, or no permission."""


def _contact_store():
    """An authorized CNContactStore, or a clear explanation of why not."""
    global _store, _denied_reason

    with _lock:
        if _store is not None:
            return _store
        if _denied_reason is not None:
            raise ContactsUnavailable(_denied_reason)

        try:
            import Contacts
        except ImportError as e:
            _denied_reason = (
                "Contacts support needs the Contacts bindings, which aren't installed "
                "(pip install pyobjc-framework-Contacts). It's macOS-only."
            )
            raise ContactsUnavailable(_denied_reason) from e

        store = Contacts.CNContactStore.alloc().init()
        finished = threading.Event()
        outcome: dict = {}

        def on_result(granted, _error):
            outcome["granted"] = bool(granted)
            finished.set()

        store.requestAccessForEntityType_completionHandler_(Contacts.CNEntityTypeContacts, on_result)

        if not finished.wait(_ACCESS_TIMEOUT_SECONDS):
            _denied_reason = "Asking for contacts permission timed out. " + _PERMISSION_HELP
            raise ContactsUnavailable(_denied_reason)

        if not outcome.get("granted"):
            _denied_reason = _PERMISSION_HELP
            raise ContactsUnavailable(_denied_reason)

        _store = store
        return _store


def _label_for(labeled) -> str:
    """A human label like 'mobile' or 'home' for a phone/email entry."""
    import Contacts

    raw = labeled.label()
    if not raw:
        return "other"
    return (Contacts.CNLabeledValue.localizedStringForLabel_(raw) or "other").lower()


def _display_name(contact) -> str:
    parts = [contact.givenName(), contact.familyName()]
    name = " ".join(p for p in parts if p).strip()
    return name or (contact.organizationName() or "").strip() or "(no name)"


def _handles(contact) -> list[dict]:
    handles = []
    for phone in contact.phoneNumbers() or []:
        value = phone.value().stringValue()
        if value:
            handles.append({"label": _label_for(phone), "value": value})
    for email in contact.emailAddresses() or []:
        value = str(email.value())
        if value:
            handles.append({"label": f"{_label_for(email)} email", "value": value})
    return handles


def _all_contacts() -> list:
    import Contacts

    store = _contact_store()
    keys = [
        Contacts.CNContactGivenNameKey,
        Contacts.CNContactFamilyNameKey,
        Contacts.CNContactOrganizationNameKey,
        Contacts.CNContactPhoneNumbersKey,
        Contacts.CNContactEmailAddressesKey,
    ]
    request = Contacts.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)

    found: list = []
    store.enumerateContactsWithFetchRequest_error_usingBlock_(
        request, None, lambda contact, _stop: found.append(contact)
    )
    return found


def search(name: str) -> list[dict]:
    """Contacts whose name matches `name`, each with all their handles.

    Exact and prefix matches rank ahead of substring ones, so searching "Sam"
    puts Sam before Samantha — but both are still returned, because which one
    was meant is the user's call, not this function's.
    """
    needle = name.strip().lower()
    if not needle:
        return []

    scored = []
    for contact in _all_contacts():
        display = _display_name(contact)
        lowered = display.lower()
        if needle == lowered:
            rank = 0
        elif lowered.startswith(needle) or any(word.startswith(needle) for word in lowered.split()):
            rank = 1
        elif needle in lowered:
            rank = 2
        else:
            continue

        handles = _handles(contact)
        if handles:  # someone with no number or email can't be messaged
            scored.append((rank, display, {"name": display, "handles": handles}))

    scored.sort(key=lambda item: (item[0], item[1]))
    return [entry for _rank, _display, entry in scored]


@tool(
    name="find_contact",
    description=(
        "Look up someone in the user's contacts by name, to find the phone number or "
        "email to message them on. Use this when the user says 'text Mom' or 'message "
        "Sam' and you don't already have the handle.\n\n"
        "This does NOT send anything, and it does not choose for you. It returns every "
        "match with every number they have. If there is more than one person, or one "
        "person with more than one number, ASK the user which one — do not pick. "
        "Getting this wrong sends a private message to a stranger. Pass the exact "
        "number you get back to send_message as `to`, and pass the person's name as "
        "`recipient_label` so the user can see who it's going to when they confirm."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The name to search for, e.g. 'Mom' or 'Sam'."}
        },
        "required": ["name"],
    },
)
def find_contact(name: str) -> str:
    matches = search(name)

    if not matches:
        return (
            f"No contact matching '{name}' with a phone number or email. "
            "Ask the user for the number directly."
        )

    shown = matches[:MAX_RESULTS]
    hidden = len(matches) - len(shown)

    lines = []
    for match in shown:
        lines.append(match["name"])
        for handle in match["handles"]:
            lines.append(f"  {handle['label']}: {handle['value']}")

    if hidden:
        lines.append(f"({hidden} more match — ask the user to narrow it down.)")

    if len(matches) > 1 or len(matches[0]["handles"]) > 1:
        lines.append("More than one option here — ask the user which before sending anything.")

    return "\n".join(lines)
