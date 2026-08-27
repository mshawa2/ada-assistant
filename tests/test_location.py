"""Reading the Mac's own location, and what the brief does when it can't."""

import subprocess

import pytest

from ada import brief, location, settings


@pytest.fixture(autouse=True)
def shortcuts_exists(monkeypatch):
    monkeypatch.setattr(location.shutil, "which", lambda _name: "/usr/bin/shortcuts")
    monkeypatch.setattr(location, "_known_shortcuts", lambda: [location.DEFAULT_SHORTCUT, "Where Am I"])


def _run(stdout="", returncode=0, stderr=""):
    def fake(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return fake


# --- either shape of answer -------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("30.2672, -97.7431", (30.2672, -97.7431)),
        ("  51.5074,-0.1278  ", (51.5074, -0.1278)),
        ("Austin, TX", None),
        ("London", None),
        ("", None),
        ("300.0, 0.0", None),  # not a real latitude
        ("0.0, 200.0", None),  # not a real longitude
    ],
)
def test_a_coordinate_pair_is_told_apart_from_a_place_name(text, expected):
    """A shortcut ending in 'Get Details of Location → City' returns a name; one
    returning the location itself stringifies to coordinates. Both are useful."""
    assert location.looks_like_coordinates(text) == expected


# --- reading it -------------------------------------------------------------


def test_the_shortcut_output_is_returned_as_text(monkeypatch):
    monkeypatch.setattr(location.subprocess, "run", _run(stdout="30.2672, -97.7431\n"))

    assert location.here() == "30.2672, -97.7431"


def test_the_output_path_flag_is_passed(monkeypatch):
    """Without it `shortcuts run` succeeds and prints nothing, which would look
    exactly like a shortcut that returned nothing."""
    seen = {}

    def fake(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "x", "")

    monkeypatch.setattr(location.subprocess, "run", fake)
    location.here()

    assert "--output-path" in seen["command"]
    assert "-" in seen["command"]


def test_a_missing_shortcut_says_how_to_make_one(monkeypatch):
    """The moment you find out this isn't set up is the moment you want to be
    told how to set it up."""
    monkeypatch.setattr(location.subprocess, "run", _run(returncode=1, stderr="not found"))

    with pytest.raises(location.LocationUnavailable) as raised:
        location.here()

    assert "Get Current Location" in str(raised.value)


def test_an_empty_result_is_not_mistaken_for_a_location(monkeypatch):
    monkeypatch.setattr(location.subprocess, "run", _run(stdout="   \n"))

    with pytest.raises(location.LocationUnavailable):
        location.here()


def test_a_slow_fix_times_out_rather_than_hanging_the_brief(monkeypatch):
    def fake(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 20)

    monkeypatch.setattr(location.subprocess, "run", fake)

    with pytest.raises(location.LocationUnavailable) as raised:
        location.here()

    assert "didn't finish" in str(raised.value)


def test_a_shortcut_that_does_not_exist_fails_fast_instead_of_hanging(monkeypatch):
    """`shortcuts run` on a name that doesn't exist doesn't fail — it sits there
    until the timeout, and then the brief blames a slow location fix for a
    shortcut nobody ever created."""
    monkeypatch.setattr(location, "_known_shortcuts", lambda: ["Music Quiz"])
    monkeypatch.setattr(
        location.subprocess, "run", lambda *a, **k: pytest.fail("must not run a shortcut that isn't there")
    )

    with pytest.raises(location.LocationUnavailable) as raised:
        location.here()

    assert "no shortcut called" in str(raised.value)
    assert "Get Current Location" in str(raised.value)


def test_the_shortcut_name_is_configurable():
    assert location.shortcut_name() == location.DEFAULT_SHORTCUT

    settings.set_path("locale.location_shortcut", "Where Am I")
    assert location.shortcut_name() == "Where Am I"


# --- the third shape: a postal address ---------------------------------------

ADDRESS = "1234 N Sample Ave\nChicago IL 60601\nUnited States"


def test_a_postal_address_yields_the_locality_first():
    """What a real Mac actually returns from 'Stop and Output [Current
    Location]'. Measured against Open-Meteo: the whole string, the
    'Chicago IL 60601' line and even 'Chicago IL' all geocode to nothing.
    Only 'Chicago' resolves, so it has to be among the candidates."""
    candidates = location.place_candidates(ADDRESS)

    assert "Chicago" in candidates
    assert candidates.index("Chicago") < candidates.index("N Sample Ave"), "the street is a last resort"
    assert candidates[-1] == "United States", "the country is the final fallback, not the first guess"


def test_house_numbers_and_postcodes_are_dropped():
    assert not any(any(c.isdigit() for c in candidate) for candidate in location.place_candidates(ADDRESS))


def test_a_single_line_answer_is_left_alone():
    assert location.place_candidates("Austin, TX") == ["Austin, TX"]
    assert location.place_candidates("") == []


def test_candidates_are_not_repeated():
    candidates = location.place_candidates("Berlin\nBerlin\nGermany")

    assert len(candidates) == len(set(c.lower() for c in candidates))


def test_the_first_candidate_that_geocodes_wins(monkeypatch):
    tried = []

    def geocode(place):
        tried.append(place)
        if place != "Chicago":
            raise ValueError(f"Couldn't find a place called '{place}'.")
        return "Chicago: mild"

    monkeypatch.setattr(location, "here", lambda: ADDRESS)
    monkeypatch.setattr(brief.weather, "get_weather", geocode)
    settings.set_path("locale.location_source", "device")

    assert "Chicago: mild" in brief.compose()
    assert tried[0] == "Chicago IL", "the fuller form is tried before the barer one"
    assert "N Sample Ave" not in tried, "it stopped as soon as one worked"


def test_an_address_that_geocodes_to_nothing_at_all_is_reported(monkeypatch):
    def never(place):
        raise ValueError("nope")

    monkeypatch.setattr(location, "here", lambda: ADDRESS)
    monkeypatch.setattr(brief.weather, "get_weather", never)
    settings.set_path("locale.location_source", "device")

    assert "didn't recognise as a place" in brief.compose()


# --- what the brief does with it --------------------------------------------


def test_coordinates_skip_the_geocoder(monkeypatch):
    """A device that already knows where it is shouldn't ask a geocoder what its
    own coordinates are called."""
    seen = {}

    def report_at(lat, lon, label):
        seen["point"] = (lat, lon)
        return "hot"

    monkeypatch.setattr(location, "here", lambda: "30.2672, -97.7431")
    monkeypatch.setattr(brief.weather, "report_at", report_at)
    monkeypatch.setattr(
        brief.weather, "get_weather", lambda _p: pytest.fail("a coordinate pair must not be geocoded")
    )
    settings.set_path("locale.location_source", "device")

    assert "hot" in brief.compose()
    assert seen["point"] == (30.2672, -97.7431)


def test_a_place_name_from_the_shortcut_is_geocoded(monkeypatch):
    monkeypatch.setattr(location, "here", lambda: "Austin, TX")
    monkeypatch.setattr(brief.weather, "get_weather", lambda place: f"fine in {place}")
    settings.set_path("locale.location_source", "device")

    assert "fine in Austin, TX" in brief.compose()


def test_a_failed_lookup_with_no_fallback_says_what_went_wrong(monkeypatch):
    def unavailable():
        raise location.LocationUnavailable("no permission")

    monkeypatch.setattr(location, "here", unavailable)
    settings.set_path("locale.location_source", "device")

    result = brief.compose()

    assert "couldn't read this Mac's location" in result
    assert "no permission" in result


def test_a_failed_lookup_falls_back_to_the_fixed_location_and_labels_it(monkeypatch):
    """'It's fine in Austin' is actively misleading when you're in Berlin."""

    def unavailable():
        raise location.LocationUnavailable("no permission")

    monkeypatch.setattr(location, "here", unavailable)
    monkeypatch.setattr(brief.weather, "get_weather", lambda place: f"fine in {place}")
    settings.set_path("locale.location_source", "device")
    settings.set_path("locale.location", "Austin, TX")

    result = brief.compose()

    assert "not necessarily where you are" in result
    assert "fine in Austin, TX" in result


def test_the_fixed_location_is_still_the_default(monkeypatch):
    """Nobody who hasn't asked for device location should start running a
    Shortcut every morning."""
    monkeypatch.setattr(location, "here", lambda: pytest.fail("device location was not asked for"))
    monkeypatch.setattr(brief.weather, "get_weather", lambda place: f"fine in {place}")
    settings.set_path("locale.location", "Austin, TX")

    assert "fine in Austin, TX" in brief.compose()


def test_no_weather_at_all_when_nothing_is_configured():
    assert brief.compose() == brief.NOTHING
