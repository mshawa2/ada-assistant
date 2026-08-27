# Smart home: what Ada could do, and what she can't

**Status:** plan only. Nothing here is built. Written 27 August 2026.

The question this answers: *if I build out an Apple smart home, what can Ada do
with it — cameras, temperature, motion alerts, lights?*

---

## The one decision that matters

**Run Home Assistant as the hub. Keep Apple Home as the interface.**

Everything below follows from this, so it's worth settling before buying
anything. HomeKit-native accessories bridge *into* Home Assistant (the HomeKit
Controller integration), and Home Assistant bridges back *out*, so the Home app
and Siri keep working normally for anyone else in the building. You lose
nothing from the Apple side and Ada gains read access to everything.

Choose HomeKit-only and roughly half of what you asked for becomes impossible
— not difficult, impossible.

## The hard wall: HomeKit alone can never do cameras

HomeKit Secure Video is **end-to-end encrypted** between the camera and your
Apple devices. There is no public API, no private API, and no Shortcuts action
that yields a frame. Apple built it so that Apple itself cannot see the
footage.

That is good privacy engineering and it completely rules out "Ada looks over the
cameras" on a HomeKit-only setup. No amount of code gets around it. Anyone who
tells you otherwise is describing a different camera system.

The same problem in smaller form applies to sensors: through the `shortcuts`
CLI, Ada can *do* things to HomeKit but cannot reliably *read* device state and
cannot enumerate what exists. Control without observation — which rules out
motion alerts, "is the garage still open", and anything conditional.

With Home Assistant in front, all of it opens up: `/api/states` for every
device, `/api/camera_proxy/...` for a JPEG snapshot, `/api/history/...` for the
past, and webhooks for real-time events.

## What Ada already has (don't rebuild these)

`ada/tools/home.py` was written with this in mind and already has the provider
seam:

- **Home Assistant** when `HASS_URL` and `HASS_TOKEN` are in `.env` — plain
  JSON over HTTP, no dependency, no OAuth.
- **Apple Shortcuts** otherwise, via the `shortcuts` CLI.

Working today: `list_home_devices`, `control_home_device`, `device_history`
("was the heating on last night" — needs Home Assistant), `run_shortcut`.

Risk is already tiered by *what the device is*: lights, switches and scenes run
freely; `lock`, `cover`, `alarm_control_panel`, `climate` and `water_heater`
always confirm. That list is config (`home.confirm_domains`), not code.

**So step one is not code.** Stand up Home Assistant, put `HASS_URL` and
`HASS_TOKEN` in `.env`, and the existing tools light up with no changes.

## Build order

### 1. Home Assistant running — *yours, blocks everything else*

Nothing below can be tested until this exists. Writing smart-home code against
an imaginary hub is exactly the mistake this project keeps not making: every
real bug found so far came from running things against real data, and blind
code is the opposite of that.

### 2. A vision path in `llm.py` — *the big one, and independent*

`ada/llm.py` has **no image support at all** today. `complete()` takes a string
and `stream_turn()` takes text messages. Adding images is the single largest
piece of work here, and it's useful on its own — screenshots, photos, receipts,
whiteboards — so it does **not** need to wait for the hub.

Then `camera_snapshot(name)`: fetch the JPEG from Home Assistant, hand it to the
model, get back a description. That's "is the car in the driveway", "did the
package arrive", "is the side gate shut".

Worth deciding early: snapshots cost a model call each. A camera check on a
timer would be a running bill, so this should be on demand, or triggered by a
motion event, never polled.

### 3. Motion and presence alerts — *reuses everything already built*

A heartbeat check polling Home Assistant for sensor changes, raising a notice,
pushing through ntfy. That path is already built and verified end to end: the
`leave_by` check is the working template, and the phone push works.

### 4. Home state in the daily brief

A section like the others: doors unlocked, windows open, thermostat setpoint,
batteries low. Fits the existing pattern exactly — quiet when there's nothing,
loud when it's broken.

### 5. Temperature control

`climate` is already in `CONTROLLABLE` and already confirm-gated.
`control_home_device` only understands on/off/toggle and brightness, so it needs
a temperature argument. Small.

### Not Ada's job: automatic lights

Build "turn the hall light on at sunset" **in Home Assistant**, not in Ada. HA
does realtime rules properly — it's local, it survives the Mac being asleep,
and it doesn't depend on a Python daemon. Ada should ask questions about the
home and raise alerts about it, not be the thing standing between a motion
sensor and a light bulb.

## The capability nothing else has

Ada now knows **where you are** (`ada/location.py`, via a Shortcut, because
CoreLocation can't work from an unbundled process) *and* what the home is doing.

- "You've left and the garage is still open."
- "Nobody's home and the heating has been on three hours."
- "You're ten minutes away and the house is at 16°C" → warm it up.

Neither HomeKit nor Home Assistant can do any of that alone, because neither
knows where *you* are. This is the reason to have Ada in the loop at all rather
than just using the Home app, and it's built on two things that already work.

## Open questions

- **Which cameras?** If cameras matter most, buy ones that speak RTSP or ONVIF
  natively rather than HomeKit-only ones. HomeKit compatibility is not the
  constraint to optimise for — Home Assistant integration is.
- **Where does Home Assistant run?** A Raspberry Pi or a small always-on box.
  Not the Mac: the whole point is that it survives the laptop being shut.
- **How much confirmation?** The current line (locks, covers, alarms, climate,
  water heater) was drawn for a hypothetical home. A real one with cameras and
  door sensors may want it moved. It's config.
- **Snapshot cost.** Decide the trigger model before building: on demand only,
  or motion-triggered with a rate cap.

## What to read

- `ada/tools/home.py` — the provider seam and the risk tiering.
- `ada/heartbeat/leave_check.py` — the template for any new alert check.
- `ada/brief.py` — the template for any new brief section.
- `AGENT.md` — "Leaving on time" and "The day in one call" for how the pieces
  above fit together.
