# Running Ada's heartbeat always-on (macOS)

Without this, the heartbeat only beats while you have a terminal open, so a
reminder due at 3pm goes off whenever you next happen to start Ada. A launchd
agent fixes that: it starts at login, restarts if it dies, and keeps checking
whether or not you're talking to her.

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
```

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

For phone push, set `notifications.ntfy_topic` to an unguessable string and
subscribe to the same topic in the [ntfy](https://ntfy.sh) app. That topic
name is effectively a shared secret — anyone who knows it can read your
notices, so don't pick `ada`.

## Gotchas worth knowing

- **The agent runs with a minimal environment.** `PATH` is set explicitly in
  the plist for exactly this reason. If you add a channel that shells out to
  something outside `/usr/bin`, add its directory there too.
- **It uses the repo's `.venv`.** Recreate the venv and the agent keeps
  working; move the checkout and you have to regenerate the plist.
- **`.env` is loaded from the working directory**, which the plist sets to the
  repo root. The heartbeat makes no model calls today, so it doesn't need
  `ANTHROPIC_API_KEY` — but importing `ada.config` still exits without one.
