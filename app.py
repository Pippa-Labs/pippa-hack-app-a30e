"""Golden Hour: when the best light for photos is at a named place.

Looks the place up with Open-Meteo's geocoder, asks Open-Meteo's forecast API
for that day's sunrise and sunset (in the place's own time zone), and works
out the golden and blue hour windows from them with a simple, stated rule:

    morning golden hour   sunrise  ->  sunrise + 60 min
    evening golden hour   sunset - 60 min  ->  sunset
    evening blue hour     sunset  ->  sunset + 30 min

That is an approximation, not a sun-angle calculation, and the narrative says
so. On very short days each golden window is capped at half the daylight, so
the morning and evening windows never overlap.

All times are local clock times ("HH:MM") with no UTC offset, which is why
they are `string` outputs, and `timezone_used` names the zone they are in.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pippa_agentapp_sdk import (
    AgentAppError,
    Call,
    Manifest,
    RateLimitedError,
    Result,
    ServiceUnavailableError,
    load_manifest_file,
    validate_manifest,
)

MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")

OUTCOME = "daylight.golden_hour"
USER_AGENT = "pippa-agent-app-golden-hour/0.1 (https://github.com/Pippa-Labs)"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search?"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast?"

#: Each request gets this many seconds. At most three requests per call
#: (geocode, a retry without the ", country" part, forecast) stays well
#: inside the 20-second budget.
REQUEST_TIMEOUT = 5.0

GOLDEN = timedelta(minutes=60)
BLUE = timedelta(minutes=30)

RULE = (
    "Golden hour here is taken as roughly the first hour after sunrise and "
    "the last hour before sunset, and blue hour as about 30 minutes after "
    "sunset; it is a rule of thumb, not a sun-angle measurement."
)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday"]

#: Country words people add after a comma that the geocoder's own fields do
#: not spell the same way.
COUNTRY_ALIASES = {"uk": "gb", "england": "gb", "scotland": "gb",
                   "wales": "gb", "usa": "us", "america": "us"}


# ── Errors about this app's own subject ─────────────────────────────────────

class NoPlace(AgentAppError):
    user_message = "Which place do you want the golden hour for? Name a town or city."
    retryable = False


class PlaceNotFound(AgentAppError):
    user_message = "I could not find anywhere with that name."
    retryable = False


class CannotReadDate(AgentAppError):
    user_message = (
        "I could not work out which date you meant. Try a weekday like "
        "Saturday, or a date like 21 November or 2026-11-21."
    )
    retryable = False


class DateOutOfRange(AgentAppError):
    user_message = (
        "I can only give sun times from about three months back to about "
        "two weeks ahead. Try a date closer to now."
    )
    retryable = False


class NoSunriseOrSunset(AgentAppError):
    user_message = (
        "The sun does not rise and set normally there on that date (polar "
        "night or midnight sun), so there is no golden hour to give."
    )
    retryable = False


class App:
    def __init__(self) -> None:
        self._manifest = validate_manifest(load_manifest_file(MANIFEST_PATH))

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    # ── The one network seam ────────────────────────────────────────────────

    def _fetch(self, url: str, timeout: float = REQUEST_TIMEOUT) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _get_json(self, url: str, what: str) -> tuple[dict, int | None]:
        """Fetch and parse JSON. Returns (body, http_status_if_4xx)."""
        try:
            raw = self._fetch(url, timeout=REQUEST_TIMEOUT)
        except urllib.error.HTTPError as exc:      # MUST come first: it IS a URLError
            if exc.code == 429:
                retry = exc.headers.get("Retry-After") if exc.headers else None
                raise RateLimitedError(
                    f"{what} rate limited",
                    retry_after_seconds=int(retry) if retry and retry.isdigit() else None,
                )
            if exc.code in (400, 404, 422):
                return {}, exc.code
            raise ServiceUnavailableError(f"{what} returned {exc.code}")
        except TimeoutError as exc:                 # NOT a URLError
            raise ServiceUnavailableError(f"{what} did not answer in time: {exc}")
        except urllib.error.URLError as exc:
            raise ServiceUnavailableError(f"could not reach {what}: {exc.reason}")
        except OSError as exc:
            raise ServiceUnavailableError(f"{what} connection failed: {exc}")
        try:
            body = json.loads(raw)
        except ValueError:
            raise ServiceUnavailableError(f"{what} sent something that is not JSON")
        if not isinstance(body, dict):
            raise ServiceUnavailableError(f"{what} sent an unexpected shape")
        return body, None

    # ── The outcome ─────────────────────────────────────────────────────────

    def handle(self, call: Call) -> Result:
        if call.outcome_id != OUTCOME:
            raise AgentAppError(f"this app cannot do {call.outcome_id!r}")

        place = " ".join((call.inputs.get("place") or "").split())
        if not place:
            raise NoPlace("no place in the message")
        date_text = (call.inputs.get("date") or "").strip()

        found = self._geocode(place)
        zone = found.get("timezone") or ""

        # "Saturday" means Saturday where the photos are being taken.
        now: datetime = call.context.now
        try:
            local_today = now.astimezone(ZoneInfo(zone)).date() if zone else now.date()
        except Exception:
            local_today = now.date()
        day = parse_date(date_text, local_today) if date_text else local_today

        sunrise, sunset, forecast_zone = self._sun_times(found, day, place)
        zone = zone or forecast_zone

        if sunset <= sunrise:
            error = NoSunriseOrSunset(f"sunrise {sunrise} sunset {sunset}")
            error.user_message = (
                f"The sun does not rise and set normally in {found['name']} on "
                f"{pretty_date(day)} (polar night or midnight sun), so there is "
                "no golden hour to give."
            )
            raise error

        daylight = sunset - sunrise
        golden = min(GOLDEN, daylight / 2)
        morning_end = sunrise + golden
        evening_start = sunset - golden
        blue_end = sunset + BLUE
        hours = round(daylight.total_seconds() / 3600, 2)
        place_used = describe(found)

        data = {
            "sunrise": hhmm(sunrise),
            "sunset": hhmm(sunset),
            "morning_golden_hour_start": hhmm(sunrise),
            "morning_golden_hour_end": hhmm(morning_end),
            "evening_golden_hour_start": hhmm(evening_start),
            "evening_golden_hour_end": hhmm(sunset),
            "evening_blue_hour_end": hhmm(blue_end),
            "day_length_hours": hours,
            "place_used": place_used,
            "date_used": day.isoformat(),
            "timezone_used": zone,
        }
        narrative = (
            f"Golden hour in {place_used} on {pretty_date(day)} ({zone} time): "
            f"morning {data['morning_golden_hour_start']}–{data['morning_golden_hour_end']}, "
            f"evening {data['evening_golden_hour_start']}–{data['evening_golden_hour_end']}, "
            f"then blue hour until {data['evening_blue_hour_end']}. "
            f"Sunrise {data['sunrise']}, sunset {data['sunset']}, "
            f"{hours:g} hours of daylight. {RULE}"
        )
        return Result(data=data, narrative=narrative)

    # ── Steps ───────────────────────────────────────────────────────────────

    def _geocode(self, place: str) -> dict:
        results = self._search(place, count=1)
        if not results and "," in place:
            # Open-Meteo's geocoder does not understand "Brighton, UK", so try
            # the town alone and prefer a match in the country they named.
            town, _, hint = place.partition(",")
            results = self._search(town.strip(), count=10)
            hint = hint.strip().lower()
            hint = COUNTRY_ALIASES.get(hint, hint)
            for r in results:
                fields = {str(r.get(k, "")).lower()
                          for k in ("country", "country_code", "admin1", "admin2")}
                if hint in fields:
                    results = [r]
                    break
        if not results:
            error = PlaceNotFound(f"geocoder found nothing for {place!r}")
            error.user_message = f"I could not find anywhere called {place}."
            raise error
        first = results[0]
        if not isinstance(first, dict) or "latitude" not in first or "longitude" not in first:
            raise ServiceUnavailableError("geocoder result had no coordinates")
        first.setdefault("name", place)
        return first

    def _search(self, name: str, count: int) -> list:
        if not name:
            return []
        url = GEOCODE_URL + urllib.parse.urlencode(
            {"name": name, "count": count, "language": "en", "format": "json"})
        body, status = self._get_json(url, "the place lookup")
        if status is not None:
            return []
        results = body.get("results")          # absent entirely when nothing matched
        return results if isinstance(results, list) else []

    def _sun_times(self, found: dict, day: date, place: str):
        url = FORECAST_URL + urllib.parse.urlencode({
            "latitude": found["latitude"],
            "longitude": found["longitude"],
            "daily": "sunrise,sunset",
            "timezone": "auto",
            "start_date": day.isoformat(),
            "end_date": day.isoformat(),
        })
        body, status = self._get_json(url, "the sun times service")
        if status is not None:
            # Open-Meteo answers 400 for a date outside its window; that is
            # the only thing in this request the user's words can make wrong.
            raise DateOutOfRange(f"forecast refused {day.isoformat()} ({status})")
        daily = body.get("daily") or {}
        sunrises = daily.get("sunrise") or []
        sunsets = daily.get("sunset") or []
        if not sunrises or not sunsets or not sunrises[0] or not sunsets[0]:
            error = NoSunriseOrSunset(f"no sunrise/sunset for {place!r} on {day}")
            error.user_message = (
                f"I could not get a sunrise and sunset for {found.get('name', place)} "
                f"on {pretty_date(day)}."
            )
            raise error
        try:
            sunrise = datetime.fromisoformat(sunrises[0])
            sunset = datetime.fromisoformat(sunsets[0])
        except (TypeError, ValueError):
            raise ServiceUnavailableError("sun times were not readable")
        return sunrise, sunset, str(body.get("timezone") or "")


# ── Helpers ─────────────────────────────────────────────────────────────────

def hhmm(moment: datetime) -> str:
    return f"{moment.hour:02d}:{moment.minute:02d}"


def pretty_date(day: date) -> str:
    # Built by hand: `%-d` is a GNU extension and raises on Windows.
    return f"{day.strftime('%A')} {day.day} {day.strftime('%B')} {day.year}"


def describe(found: dict) -> str:
    parts = []
    for key in ("name", "admin1", "country"):
        value = str(found.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts)


def parse_date(text: str, today: date) -> date:
    """Turn what the user wrote into a date, relative to the place's today.

    Handles YYYY-MM-DD, "21 November", "November 21", "21/11/2026", "today",
    "tomorrow", "tonight", weekday names (with or without "this"/"next"/"on")
    and "this weekend" (the coming Saturday, or today if it is Sunday).
    """
    cleaned = re.sub(r"[,.]", " ", text.lower())
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", cleaned)
    cleaned = " ".join(cleaned.split())

    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%B %d %Y",
                "%d %b %Y", "%b %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue

    for fmt in ("%d %B", "%B %d", "%d %b", "%b %d"):
        try:
            # Parse with a leap year so "29 February" is not refused outright.
            parsed = datetime.strptime(f"2000 {cleaned}", f"%Y {fmt}").date()
        except ValueError:
            continue
        for year in (today.year, today.year + 1):
            try:
                candidate = parsed.replace(year=year)
            except ValueError:
                continue
            if candidate >= today:
                return candidate
        raise CannotReadDate(f"could not place {text!r} in a year")

    words = [w for w in cleaned.split() if w not in ("on", "this", "coming", "the")]
    phrase = " ".join(words)
    if phrase in ("today", "tonight", "now", "this evening", "evening", "morning"):
        return today
    if phrase in ("tomorrow", "tomorrow morning", "tomorrow evening", "tomorrow night"):
        return today + timedelta(days=1)
    if phrase in ("weekend", "next weekend"):
        if today.weekday() == 6:            # Sunday: this weekend is today
            return today if phrase == "weekend" else today + timedelta(days=6)
        ahead = (5 - today.weekday()) % 7
        return today + timedelta(days=ahead + (7 if phrase == "next weekend" else 0))

    nxt = bool(words) and words[0] == "next"
    if nxt:
        words = words[1:]
    if len(words) == 1:
        name = words[0]
        for i, day_name in enumerate(WEEKDAYS):
            if name == day_name or (len(name) >= 3 and day_name.startswith(name)):
                ahead = (i - today.weekday()) % 7
                if nxt and ahead == 0:
                    ahead = 7
                return today + timedelta(days=ahead)

    raise CannotReadDate(f"could not parse {text!r} as a date")
