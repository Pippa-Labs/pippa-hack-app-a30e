"""Tests for Golden Hour, through LocalRunner, with the network stubbed.

The payloads below were captured from Open-Meteo by hand (November 2026).
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest
from pippa_agentapp_sdk import (
    LocalRunner,
    OutputShapeViolation,
    RateLimitedError,
    ServiceUnavailableError,
)

import app as app_module
from app import (
    App,
    CannotReadDate,
    DateOutOfRange,
    NoPlace,
    NoSunriseOrSunset,
    PlaceNotFound,
    parse_date,
)

# Friday 20 November 2026, 21:00 UTC.
NOW = datetime(2026, 11, 20, 21, 0, tzinfo=timezone.utc)
OUTCOME = "daylight.golden_hour"

EDINBURGH = {"results": [{
    "id": 2650225, "name": "Edinburgh", "latitude": 55.95206,
    "longitude": -3.19648, "country_code": "GB", "timezone": "Europe/London",
    "country": "United Kingdom", "admin1": "Scotland", "admin2": "Edinburgh",
}]}
BRIGHTONS = {"results": [
    {"name": "Brighton", "latitude": -37.9, "longitude": 145.0,
     "country_code": "AU", "timezone": "Australia/Melbourne",
     "country": "Australia", "admin1": "Victoria"},
    {"name": "Brighton", "latitude": 50.8284, "longitude": -0.13947,
     "country_code": "GB", "timezone": "Europe/London",
     "country": "United Kingdom", "admin1": "England"},
]}
TROMSO = {"results": [{
    "name": "Tromsø", "latitude": 69.6489, "longitude": 18.95508,
    "country_code": "NO", "timezone": "Europe/Oslo", "country": "Norway",
    "admin1": "Troms",
}]}
NOTHING = {"generationtime_ms": 0.08}


def sun(day: str, rise: str, set_: str, zone: str = "Europe/London") -> dict:
    return {
        "latitude": 55.94, "longitude": -3.17, "utc_offset_seconds": 0,
        "timezone": zone, "timezone_abbreviation": "GMT",
        "daily_units": {"time": "iso8601", "sunrise": "iso8601", "sunset": "iso8601"},
        "daily": {"time": [day], "sunrise": [f"{day}T{rise}"], "sunset": [f"{day}T{set_}"]},
    }


def http_error(code: int, headers: dict | None = None):
    return urllib.error.HTTPError("https://example.com", code, "err", headers or {}, io.BytesIO(b""))


class Stub:
    """Answers each URL by its host, and records what was asked for."""

    def __init__(self, geocode=EDINBURGH, forecast=None, geocode_retry=None):
        self.geocode = geocode
        self.forecast = forecast or sun("2026-11-21", "08:00", "15:55")
        self.geocode_retry = geocode_retry
        self.urls: list[str] = []

    def __call__(self, url: str, timeout: float = 5.0) -> bytes:
        assert timeout and timeout <= 6
        self.urls.append(url)
        if url.startswith(app_module.GEOCODE_URL):
            answer = self.geocode
            if self.geocode_retry is not None and len(self.urls) > 1:
                answer = self.geocode_retry
        elif url.startswith(app_module.FORECAST_URL):
            answer = self.forecast
        else:
            raise AssertionError(f"unexpected url {url}")
        if isinstance(answer, BaseException):
            raise answer
        return json.dumps(answer).encode()


def run(inputs: dict, stub: Stub | None = None, *, explicit: bool = False):
    application = App()
    application._fetch = stub or Stub()
    return LocalRunner(application).call(
        OUTCOME, inputs, granted_secrets={}, now=NOW, request_id="test-1",
        explicit=explicit,
    ).result


def test_the_manifest_is_valid():
    application = App()
    assert application.manifest.app_id == "hack.app_a30e"
    assert [o.id for o in application.manifest.outcomes] == [OUTCOME]


def test_golden_and_blue_hour_are_worked_out_from_sunrise_and_sunset():
    stub = Stub()
    result = run({"place": "Edinburgh", "date": "Saturday"}, stub)
    assert result.data == {
        "sunrise": "08:00",
        "sunset": "15:55",
        "morning_golden_hour_start": "08:00",
        "morning_golden_hour_end": "09:00",
        "evening_golden_hour_start": "14:55",
        "evening_golden_hour_end": "15:55",
        "evening_blue_hour_end": "16:25",
        "day_length_hours": 7.92,
        "place_used": "Edinburgh, Scotland, United Kingdom",
        "date_used": "2026-11-21",
        "timezone_used": "Europe/London",
    }
    assert "start_date=2026-11-21" in stub.urls[-1]
    assert "timezone=auto" in stub.urls[-1]
    # The rule is stated, so the answer is honest about being approximate.
    assert "first hour after sunrise" in result.narrative
    assert "30 minutes after sunset" in result.narrative
    assert "Edinburgh" in result.narrative


def test_no_date_means_the_places_own_today_and_is_not_invented():
    stub = Stub(forecast=sun("2026-11-20", "07:58", "15:57"))
    result = run({"place": "Edinburgh", "date": ""}, stub)
    assert result.data["date_used"] == "2026-11-20"
    stub = Stub(forecast=sun("2026-11-20", "07:58", "15:57"))
    result = run({"place": "Edinburgh"}, stub)
    assert result.data["date_used"] == "2026-11-20"


def test_today_is_the_places_today_not_utc():
    """21:00 UTC on the 20th is already the 21st in Auckland."""
    nz = {"results": [{"name": "Auckland", "latitude": -36.8, "longitude": 174.7,
                       "timezone": "Pacific/Auckland", "country": "New Zealand"}]}
    stub = Stub(geocode=nz, forecast=sun("2026-11-21", "06:00", "20:10", "Pacific/Auckland"))
    result = run({"place": "Auckland"}, stub)
    assert result.data["date_used"] == "2026-11-21"
    assert result.data["timezone_used"] == "Pacific/Auckland"


def test_place_name_is_url_encoded():
    stub = Stub()
    run({"place": "New York"}, stub)
    assert "name=New+York" in stub.urls[0]


def test_a_country_after_a_comma_is_used_to_pick_the_match():
    stub = Stub(geocode=NOTHING, geocode_retry=BRIGHTONS)
    result = run({"place": "Brighton, UK", "date": "2026-11-21"}, stub)
    assert result.data["place_used"] == "Brighton, England, United Kingdom"
    assert "name=Brighton&" in stub.urls[1]


def test_a_place_that_does_not_exist_is_named_back():
    with pytest.raises(PlaceNotFound) as failure:
        run({"place": "Qwxzzy"}, Stub(geocode=NOTHING))
    assert failure.value.user_message == "I could not find anywhere called Qwxzzy."


def test_no_place_is_asked_for():
    with pytest.raises(NoPlace):
        run({"place": ""})
    with pytest.raises(NoPlace):
        run({"place": "   ", "date": "Saturday"})


def test_an_unreadable_date_is_refused_not_guessed():
    with pytest.raises(CannotReadDate):
        run({"place": "Edinburgh", "date": "a week on Thursday-ish"})


def test_a_date_outside_the_forecast_window_is_explained():
    stub = Stub(forecast=http_error(400))
    with pytest.raises(DateOutOfRange):
        run({"place": "Edinburgh", "date": "2027-06-21"}, stub)


def test_polar_night_has_no_golden_hour():
    stub = Stub(geocode=TROMSO, forecast=sun("2026-12-02", "00:00", "00:00", "Europe/Oslo"))
    with pytest.raises(NoSunriseOrSunset) as failure:
        run({"place": "Tromsø", "date": "2026-12-02"}, stub)
    assert "Tromsø" in failure.value.user_message


def test_empty_sun_times_are_not_indexed_blindly():
    empty = {"timezone": "Europe/London", "daily": {"time": [], "sunrise": [], "sunset": []}}
    with pytest.raises(NoSunriseOrSunset):
        run({"place": "Edinburgh"}, Stub(forecast=empty))


def test_very_short_days_keep_the_windows_apart():
    stub = Stub(forecast=sun("2026-11-21", "11:00", "12:30"))
    data = run({"place": "Edinburgh", "date": "2026-11-21"}, stub).data
    assert data["morning_golden_hour_end"] == "11:45"
    assert data["evening_golden_hour_start"] == "11:45"
    assert data["day_length_hours"] == 1.5


def test_rate_limit_is_retryable():
    with pytest.raises(RateLimitedError):
        run({"place": "Edinburgh"}, Stub(geocode=http_error(429)))


def test_server_error_is_service_unavailable():
    with pytest.raises(ServiceUnavailableError):
        run({"place": "Edinburgh"}, Stub(forecast=http_error(503)))


def test_timeout_is_service_unavailable():
    with pytest.raises(ServiceUnavailableError):
        run({"place": "Edinburgh"}, Stub(geocode=TimeoutError("slow")))


def test_unreachable_is_service_unavailable():
    with pytest.raises(ServiceUnavailableError):
        run({"place": "Edinburgh"}, Stub(geocode=urllib.error.URLError("dns")))


def test_picked_by_name_it_answers_and_refuses_as_usual():
    picked = run({"place": "Edinburgh", "date": "Saturday"}, explicit=True)
    assert picked.data == run({"place": "Edinburgh", "date": "Saturday"}).data
    with pytest.raises(NoPlace):
        run({"place": ""}, explicit=True)


@pytest.mark.parametrize("text, expected", [
    ("Saturday", "2026-11-21"),
    ("on saturday", "2026-11-21"),
    ("sat", "2026-11-21"),
    ("Friday", "2026-11-20"),
    ("next Friday", "2026-11-27"),
    ("tomorrow", "2026-11-21"),
    ("today", "2026-11-20"),
    ("tonight", "2026-11-20"),
    ("this weekend", "2026-11-21"),
    ("next weekend", "2026-11-28"),
    ("2026-11-25", "2026-11-25"),
    ("25 November", "2026-11-25"),
    ("November 25th", "2026-11-25"),
    ("3 January", "2027-01-03"),
    ("21/11/2026", "2026-11-21"),
])
def test_dates_people_write(text, expected):
    from datetime import date
    assert parse_date(text, date(2026, 11, 20)).isoformat() == expected


def test_the_runner_enforces_the_manifest(monkeypatch):
    from pippa_agentapp_sdk import Result

    monkeypatch.setattr(App, "handle", lambda self, c: Result(data={"surprise": "hello"}))
    with pytest.raises(OutputShapeViolation):
        run({"place": "Edinburgh"})
