"""The dashboard, on made-up data.

For screenshots and for anyone trying Ada without handing her a calendar, an
inbox and a brokerage login. Everything is seeded into a throwaway data
directory through Ada's own modules, so the face renders exactly what it would
for a real user — and nothing touches `data/`, the running daemon, the network
or your phone.

    ./.venv/bin/python demo.py          # http://127.0.0.1:4174
    ./.venv/bin/python demo.py --port 5000 --no-open
"""

import argparse
import json
import os
import random
import tempfile
import time
import webbrowser
from pathlib import Path

# Both have to be in place before anything under `ada` is imported: the data
# directory is resolved at import time, and an empty push topic set here wins
# over the real one in .env, because load_dotenv never overrides.
_DEMO_DIR = Path(tempfile.mkdtemp(prefix="ada-demo-"))
os.environ["ADA_DATA_DIR"] = str(_DEMO_DIR)
os.environ["ADA_NTFY_TOPIC"] = ""


def _demo_config() -> Path:
    """The shipped config, with every way out of the machine closed.

    Notices stay in the terminal and on the page. The approval this seeds is
    raised at `urgent`, which a real config would push to a phone.
    """
    config = json.loads((Path(__file__).parent / "ada_config.json").read_text())
    config["identity"]["user_name"] = "Alex"
    config["email"]["address"] = "alex@example.com"
    config["notifications"]["channels"] = ["terminal"]
    path = _DEMO_DIR / "ada_config.json"
    path.write_text(json.dumps(config, indent=2))
    return path


os.environ["ADA_CONFIG_FILE"] = str(_demo_config())

from werkzeug.serving import make_server  # noqa: E402

from ada import approvals, audit, cost, jobs, positions, schedules, session, store  # noqa: E402
from ada.confirm import Action  # noqa: E402
from ada.heartbeat.notices import add_notice  # noqa: E402

DAY = 86400
HOUR = 3600

# Symbol, shares, cost, last price, stop, target, thesis.
HOLDINGS = [
    ("NVDA", 12, 118.40, 131.25, 112.00, 150.00, "Data-centre demand outrunning supply through next year."),
    ("MSFT", 8, 402.10, 418.60, 385.00, 460.00, "Azure AI attach rate keeps climbing."),
    ("AAPL", 15, 221.30, 214.80, 205.00, 245.00, "Services margin carries the hardware cycle."),
    ("COST", 3, 860.00, 902.40, None, None, ""),
    ("VOO", 20, 488.50, 512.10, None, None, ""),
]


def _walk(end: float, days: int = 90) -> list[float]:
    """A plausible price history that lands exactly on today's price."""
    rng = random.Random(end)
    closes = [end]
    for _ in range(days - 1):
        closes.append(closes[-1] / (1 + rng.gauss(0.0006, 0.015)))
    return [round(c, 2) for c in reversed(closes)]


def seed_holdings() -> None:
    now = time.time()
    history = {}
    for symbol, shares, paid, price, stop, target, thesis in HOLDINGS:
        positions.add(symbol, shares, cost=paid, stop=stop, target=target, thesis=thesis)
        positions.record_reading(symbol, price)
        # Fresh in the cache, so the sparklines never go to the network.
        history[symbol] = {"at": now, "closes": _walk(price)}
    store.write_json(store.DATA_DIR / "price_history.json", history)

    store.write_json(
        store.DATA_DIR / "portfolio.json",
        {
            "at": now - 12 * 60,
            "accounts": [
                {
                    "id": "demo-brokerage",
                    "name": "Individual Brokerage",
                    "institution": "Demo Brokerage",
                    "number": "••••4821",
                    "cash": 3240.18,
                    "buying_power": 3240.18,
                    "total": 31877.60,
                    "holdings": [{"symbol": h[0]} for h in HOLDINGS],
                },
                {
                    "id": "demo-roth",
                    "name": "Roth IRA",
                    "institution": "Demo Brokerage",
                    "number": "••••1907",
                    "cash": 812.55,
                    "buying_power": 812.55,
                    "total": 10242.00,
                    "holdings": [{"symbol": "VOO"}],
                },
            ],
        },
    )


def seed_work() -> None:
    """A queue that looks like a week of real use: done, failed, waiting, upcoming."""
    done = [
        ("Summarize unread email from the last 24h", "6 unread — one needs a reply today (the lease renewal)."),
        ("Find two free hours for deep work this week", "Thursday 9–11am and Friday 2–4pm are both clear."),
        ("Compare flights to Austin for the 14th", "Cheapest direct is $212 on the 7:05am; the 5:40pm is $238."),
    ]
    for task, result in done:
        job = jobs.create(task, tools=["search_email", "find_free_time", "web_search"], budget_usd=0.25)
        jobs.claim()
        jobs.record_spend(job["id"], round(random.uniform(0.01, 0.06), 3))
        jobs.finish(job["id"], result)

    job = jobs.create("Check the weather for Saturday's hike", tools=["get_weather"], budget_usd=0.05)
    jobs.claim()
    jobs.fail(job["id"], "Weather service timed out twice — will try again on the next run.")

    job = jobs.create("Text Sam the dinner plan once it's confirmed", tools=["send_message"], budget_usd=0.10)
    jobs.claim()
    approvals.park(
        job["id"],
        Action(
            tool="send_message",
            input={"to": "Sam", "text": "Dinner's confirmed — 7:30 at Avec. See you there!"},
            description="Text Sam: \"Dinner's confirmed — 7:30 at Avec. See you there!\"",
        ),
    )

    jobs.create("Draft the weekly status update", run_at=time.time() + 5 * HOUR, tools=["save_draft"])

    schedules.create("Morning brief: weather, calendar, unread mail", {"repeat": "daily", "at": "08:00"})
    schedules.create("Review open positions against their stops", {"repeat": "weekly", "on": "friday", "at": "15:30"})
    schedules.create("Clear out snoozed reminders", {"repeat": "weekly", "on": "sunday", "at": "19:00"})


def seed_conversation() -> None:
    session.record(
        [
            {"role": "user", "content": "What's my day look like?"},
            {
                "role": "assistant",
                "content": "Three things on the calendar: standup at 9:30, lunch with Priya at 12:15, "
                "and the dentist at 4. Rain from about 3pm, so leave for the dentist by 3:35. "
                "Two reminders are due today, and NVDA is up 2.1% pre-market.",
            },
            {"role": "user", "content": "Move the dentist reminder to an hour before, and text Sam I'll be late tonight."},
            {
                "role": "assistant",
                "content": "Reminder moved to 3:00pm. For the text, I need a yes from you first:\n\n"
                "To: Sam (mobile)\n\"Running late tonight — closer to 8. Sorry!\"",
            },
            {"role": "user", "content": "yes"},
            {"role": "assistant", "content": "Sent. Anything else before standup?"},
        ]
    )


def seed_activity() -> None:
    for tool in ("daily_brief", "list_events", "get_weather", "check_price", "snooze_reminder", "send_message"):
        audit.log("tool_call", tool=tool, input={}, result="ok")
    add_notice("morning_brief", "Morning brief is ready: 3 events, rain from 3pm, 2 reminders due.")
    add_notice("positions", "AAPL is 4.5% above its stop at $205.00 — closest of your guarded positions.")


def seed_spend() -> None:
    """Thirty days of model spend, so the chart has a shape rather than one bar."""
    rng = random.Random(7)
    now = time.time()
    entries = []
    for day in range(30, 0, -1):
        for _ in range(rng.randint(4, 14)):
            entries.append({"at": now - day * DAY + rng.uniform(0, DAY), "amount": round(rng.uniform(0.004, 0.03), 4), "note": "demo"})
    store.write_json(store.DATA_DIR / "spend.json", {"model": entries})
    # This process's token meter, which the face reads live.
    # Sized to a typical day, so today doesn't spike off the end of the chart.
    cost.record(38_400, 4_210, cache_read_tokens=121_800, cache_write_tokens=9_050)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=4174)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = parser.parse_args()

    seed_holdings()
    seed_work()
    seed_conversation()
    seed_activity()
    seed_spend()

    from ada.face.app import app

    url = f"http://127.0.0.1:{args.port}"
    print(f"Ada demo dashboard on made-up data: {url}\nData: {_DEMO_DIR}\nCtrl-C to stop.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        make_server("127.0.0.1", args.port, app, threaded=True).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
