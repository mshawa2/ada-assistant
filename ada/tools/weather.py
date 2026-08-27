import json
import urllib.error
import urllib.parse
import urllib.request

from ada.tools.registry import tool

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 10

_WEATHER_CODES = {
    0: "clear sky",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "slight rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "slight snow",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    80: "slight rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    85: "slight snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def _get_json(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the weather service: {e.reason if hasattr(e, 'reason') else e}") from e
    except OSError as e:
        raise ValueError(f"Network problem reaching the weather service: {e}") from e


@tool(
    name="get_weather",
    description=(
        "Get the current weather for a city or place. Use this whenever the user asks about "
        "the weather, temperature, or conditions somewhere."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "location": {"type": "string", "description": "A city or place name, e.g. 'Austin, TX' or 'Paris'."},
        },
        "required": ["location"],
    },
)
def get_weather(location: str) -> str:
    geocode_url = f"{_GEOCODE_URL}?{urllib.parse.urlencode({'name': location, 'count': 1})}"
    geocode = _get_json(geocode_url)
    results = geocode.get("results")
    if not results:
        raise ValueError(f"Couldn't find a place called '{location}'.")

    place = results[0]
    label = ", ".join(part for part in (place.get("name"), place.get("admin1"), place.get("country")) if part)
    return report_at(place["latitude"], place["longitude"], label)


def report_at(lat: float, lon: float, label: str) -> str:
    """The forecast for a point, skipping geocoding.

    Split out so a device location — which already knows exactly where it is —
    doesn't take a round trip to a geocoder to ask what its own coordinates are
    called, and then get back a nearest-named-place answer that may be a
    different suburb.
    """
    forecast_params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,relative_humidity_2m,wind_speed_10m",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
    }
    forecast_url = f"{_FORECAST_URL}?{urllib.parse.urlencode(forecast_params)}"
    forecast = _get_json(forecast_url)

    current = forecast["current"]
    condition = _WEATHER_CODES.get(current["weather_code"], "unknown conditions")
    return (
        f"{label}: {current['temperature_2m']}°F (feels like {current['apparent_temperature']}°F), "
        f"{condition}, humidity {current['relative_humidity_2m']}%, wind {current['wind_speed_10m']} mph."
    )
