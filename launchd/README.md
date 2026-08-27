# Running Ada always-on (macOS)

Without this, the heartbeat only beats while you have a terminal open, so a
reminder due at 3pm goes off whenever you next happen to start Ada — and a
background job only runs while you're sitting there watching it, which rather
defeats the point. A launchd agent fixes that: it starts at login, restarts if
it dies, and keeps working whether or not you're talking to her.

**The dashboard comes with it.** `heartbeat_main.py` serves the face at
<http://127.0.0.1:4173>, so it's up from login. That's one agent rather than
two on purpose: `ada/status.py` is process memory, so a dashboard in its own
process would report `idle` with an empty tool list forever while this one ran
the jobs. The dashboard belongs to the process actually doing something.

It stays bound to `127.0.0.1`. Nothing on your network can reach it, which is
what makes it safe to have no authentication on `/api/approvals/<id>/approve` —
a click there authorises a background job to send an email or spend money, and
the only thing standing between that and the rest of the world is the loopback
bind. **Don't change the bind address without putting auth in front of it.**

Running the agent and a REPL at the same time is safe — `ada/heartbeat/singleton.py`
gives the heartbeat to exactly one process, and the other just reads notices.

## Install

```sh
# From the repo root. Substitutes your real path into the template.
mkdir -p ~/Library/LaunchAgents
sed "s|ABSOLUTE_PATH_TO_ADA|$PWD|g" launchd/com.ada.heartbeat.plist \
  > ~/Library/LaunchAgents/com.ada.heartbeat.plist

launchctl load ~/Library/LaunchAgents/com.ada.heartbeat.plist
```

Check it came up:

```sh
launchctl list | grep com.ada.heartbeat   # second column 0 = healthy
tail -f data/heartbeat.out
open http://127.0.0.1:4173
```

## Running a REPL at the same time

Safe, and expected. The daemon holds the heartbeat lock and the dashboard port,
so the REPL prints a line saying it's using the daemon's dashboard rather than
starting its own. Your conversation still shows up there — the chat panel falls
back to the transcript on disk and marks it `from disk`. What won't show is the
REPL's *live* state: the streaming reply, the armed tool list, and a
confirmation waiting on a keypress. Those are process memory, and they belong
to the terminal you're typing in.

## Stop / uninstall

```sh
launchctl unload ~/Library/LaunchAgents/com.ada.heartbeat.plist
rm ~/Library/LaunchAgents/com.ada.heartbeat.plist
```

To pause it without uninstalling, just ask Ada to pause the heartbeat — the
Tier 6 kill switch works on the daemon exactly as it does in a session.

## After changing the plist

launchd caches it. Unload and load again; editing the file alone does nothing.

## Notifications

Desktop banners need `notifications.channels` to include `"macos"` in
`heartbeat_config.json` (it does by default). **The first banner will not
appear until you allow it**: macOS asks once, and for a launchd agent the
prompt is attributed to Script Editor / osascript rather than to Ada. Check
System Settings → Notifications if you never see one.

For phone push, put an unguessable string in `ADA_NTFY_TOPIC` in `.env` and
subscribe to the same topic in the [ntfy](https://ntfy.sh) app. That topic name
is effectively a shared secret — anyone who knows it can read your notices and
post to them, so don't pick `ada`. **Use the environment, not
`notifications.ntfy_topic` in `ada_config.json`**: that file is tracked in git,
and the env var takes precedence for exactly this reason. The config key still
works as a fallback.

The daily brief is pushed here too, once each morning — see "The day in one
call" in AGENT.md. It's on by default at 08:00. The time is
`heartbeat.checks.morning_brief.at` in `ada_config.json`, and `enabled: false`
turns it off. **Adding or enabling a check needs the agent restarted** — the
config reloads every tick, the check registry doesn't.

## Gotchas worth knowing

- **The agent runs with a minimal environment.** `PATH` is set explicitly in
  the plist for exactly this reason. If you add a channel that shells out to
  something outside `/usr/bin`, add its directory there too.
- **It uses the repo's `.venv`.** Recreate the venv and the agent keeps
  working; move the checkout and you have to regenerate the plist.
- **`.env` is loaded from the working directory**, which the plist sets to the
  repo root. The heartbeat makes no model calls today, so it doesn't need
  `ANTHROPIC_API_KEY` — but importing `ada.config` still exits without one.
