# Running Ada on its own machine

**Status:** plan only. Written 27 August 2026. Ada currently runs on the
laptop under launchd (`launchd/README.md`).

The question: *can this run on a Raspberry Pi or similar instead of the
laptop?*

Short answer: **yes, but you'd lose about a fifth of the tools and most of the
personal ones.** Whether that's the right trade depends on what you want the
always-on box for, and there's a cheaper-than-you'd-think option that loses
nothing.

---

## What Ada actually needs

Almost nothing. There is no local model — inference is the Anthropic API over
HTTPS. Ada is Python, JSON files on disk, and HTTP calls. A Pi 4 with 2GB is
already overkill on CPU and RAM.

**The one real hardware concern is the SD card.** The heartbeat writes
`heartbeat_state.json` on every tick, plus notices, jobs, positions and the
audit log. That is a constant trickle of small writes, which is exactly what
kills SD cards, and a corrupted card means silently losing the notice log and
job queue. **Boot from a USB SSD**, not a card. This matters more than which Pi
you buy.

## What breaks off macOS

Ten modules touch macOS-only APIs. Audited, not guessed:

| Lost | Why |
|---|---|
| `list_events`, `create_event`, `delete_event`, `find_events`, `list_calendars` | EventKit |
| `find_free_time` | Built on the calendar |
| `shopping_list` | Kept in Reminders, via EventKit |
| `find_contact` | Contacts framework |
| `send_message`, `read_recent_messages` | iMessage — `chat.db` and AppleScript |
| `play_music`, `search_music`, `media_control`, `system_control` | AppleScript |
| `run_shortcut` | `shortcuts` CLI |
| Device location | `ada/location.py` runs a Shortcut |
| Leave-by alerts | Needs device location for the origin |
| macOS notification banners | `osascript` |

That's roughly **15 of 79 tools**, plus two of the features built most
recently. Notably it's the *personal* half: your calendar, your messages, your
contacts, your music.

## What keeps working

Everything that is Python and HTTP, which is most of it:

- Reminders, memory, notes, drafts, shopping-adjacent lists — all JSON on disk
- **Email** — IMAP/SMTP, pure standard library, no macOS anywhere
- Portfolio, positions, brokerage sync, price alerts, `rule_options`
- Web search, `fetch_url`, weather (with a fixed `locale.location`)
- Background jobs, schedules, approvals, notices, budget, the whole heartbeat
- **ntfy push** — the channel that actually reaches your phone
- The morning brief, minus its calendar section
- The dashboard at `:4173`

So a Pi-hosted Ada is a good **money, mail and alerts daemon**. It is not the
assistant that knows your calendar.

## The three options

### 1. Raspberry Pi — cheap, loses the personal tools

£60–£90 plus an SSD. Right if what you want always-on is portfolio alerts,
email, reminders and the morning push, and you're content to ask the laptop
about your calendar.

Also the correct home for **Home Assistant** (see `SMART_HOME.md`) — that's a Pi's
sweet spot, and it's needed for the smart-home plan regardless of where Ada
lives.

### 2. Used Mac mini (M1) — keeps everything

£250–£400 used. Runs **100% of Ada** with no code changes at all: the launchd
plist in `launchd/` already works, EventKit and Shortcuts and iMessage all
behave, and it idles at a few watts.

If the goal is "Ada, but not on my laptop", this is the honest answer. Every
other option is a trade; this one isn't.

### 3. Mini PC (N100) or an old laptop — middle ground

£120–£180 new, or free if something's in a drawer. Faster than a Pi, x86 Linux,
same macOS losses as option 1. Sensible if you also want it doing other things.

**A cloud VPS is the option I'd avoid.** It can't reach Home Assistant on your
LAN without a tunnel, and it means your brokerage credentials and mail password
live on a machine you don't physically control. The whole design so far keeps
secrets in a local `.env`; a VPS quietly undoes that.

## What I'd actually do

**Both, and split by what each is good at:**

- **Pi → Home Assistant.** Local, always on, survives everything, and it's the
  blocking step for the smart-home plan anyway.
- **Mac mini → Ada.** Nothing else keeps the calendar, iMessage and contacts,
  and those are the parts a generic assistant can't replace.

Do *not* run Ada on both machines against a shared data directory.
`ada/heartbeat/singleton.py` holds the heartbeat lock per-machine, and the JSON
stores would race — two heartbeats would double-fire alerts and interleave
writes to the same files. If you ever want a split, it needs a real decision
about which machine owns the data, not a shared folder.

## Porting checklist, if you go Linux

1. **Python 3.10+.** The code uses `match` and `X | None` annotations. Raspberry
   Pi OS (bookworm) ships 3.11 — fine, but the suite has only ever been run on
   3.14, so run `pytest` first and expect a couple of surprises.
2. **`requirements.txt` already handles it.** Both pyobjc lines are marked
   `sys_platform == "darwin"`, so `pip install -r` on Linux skips them
   correctly.
3. **The macOS tools need to fail gracefully, not at import.** Worth checking
   before trusting it — a tool that raises on import would take the whole
   registry down rather than just being unavailable. Untested on Linux today.
4. **Replace launchd with systemd.** A `--user` unit with `Restart=always`,
   plus `loginctl enable-linger` so it runs without a session. `launchd/README.md`
   documents the equivalent behaviour to reproduce.
5. **`notifications.channels`** — drop `"macos"`, keep `"ntfy"`. As of the
   silent-channel fix, leaving `macos` in on Linux reports itself as degraded in
   the daily brief rather than failing quietly, which is the right behaviour but
   noisy every morning.
6. **`locale.location_source`** — set back to `"config"` with a fixed
   `locale.location`, since the device-location Shortcut won't exist.
7. **Move `.env` carefully.** Seven secrets live in it. Copy it out of band,
   don't commit it, and consider rotating the brokerage keys on the way.
8. **Data directory.** `ADA_DATA_DIR` — copy `data/` across to keep reminders,
   memory, positions and the notice log.

## Open questions

- Is losing the calendar acceptable? It's the single biggest loss, and
  `find_free_time` and the brief's best section both go with it.
- Would a Pi *and* the laptop be enough — Pi for the always-on money/mail/alert
  half, laptop for the personal half, accepting two Adas that don't share
  state?
- Is the Mac mini worth ~£300 to avoid the entire question?

## See also

- `launchd/README.md` — how the always-on setup works today.
- `SMART_HOME.md` — why a Pi is wanted for Home Assistant regardless.
