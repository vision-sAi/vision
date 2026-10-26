from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from vision.config import Config, WeatherConfig
from vision import weather
from vision.weather import Place, WeatherError, WeatherKit, is_weather_request, make_token, place_in_context, requested_place, requested_scope, summarise


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sample(now: datetime) -> dict:
    hour = now.replace(minute=0, second=0, microsecond=0)
    hours = []
    for i in range(14):
        start = hour + timedelta(hours=i)
        hours.append({"forecastStart": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                      "conditionCode": "Rain" if i in (3, 4) else "Cloudy",
                      "precipitationChance": 0.7 if i in (3, 4) else 0.05, "temperature": 15})
    days = []
    for i, (code, hi, lo) in enumerate([("PartlyCloudy", 19.6, 11.2), ("Rain", 16, 9), ("Clear", 21, 10), ("Showers", 18, 9)]):
        day = (now + timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        days.append({"forecastStart": day.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                     "conditionCode": code, "temperatureMax": hi, "temperatureMin": lo,
                     "precipitationChance": 0.6 if code in ("Rain", "Showers") else 0.1,
                     "precipitationType": "rain" if code in ("Rain", "Showers") else "clear",
                     "sunrise": day.replace(hour=6, minute=41).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                     "sunset": day.replace(hour=19, minute=8).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")})
    return {"currentWeather": {"conditionCode": "MostlyCloudy", "temperature": 14.4, "temperatureApparent": 11.0,
                               "humidity": 0.82, "windSpeed": 21.3, "uvIndex": 2},
            "forecastHourly": {"hours": hours}, "forecastDaily": {"days": days},
            "weatherAlerts": {"alerts": [{"description": "Yellow warning of wind", "severity": "moderate", "source": "Met Office"}]}}


class RequestParsingTests(unittest.TestCase):
    def test_weather_questions_are_recognised(self):
        for text in ("What's the weather like?", "is it going to rain later", "Do I need an umbrella today?",
                     "how cold is it outside", "give me the forecast for tomorrow", "will it be windy this weekend"):
            self.assertTrue(is_weather_request(text), text)

    def test_other_requests_are_not(self):
        for text in ("fix the bug in Main Street", "I feel under the weather today", "what's the time", ""):
            self.assertFalse(is_weather_request(text), text)

    def test_everyday_phrasings_are_recognised(self):
        for text in ("what's it like outside", "how's it looking out there", "do I need a coat today", "should I bring a jacket",
                     "is it cold out", "what's the temp outside", "any frost tonight", "heatwave this week?", "what time is sunset",
                     "is it going to be dry tomorrow", "wheather tomorrow", "how's the forcast", "UV index today"):
            self.assertTrue(is_weather_request(text), text)

    def test_temperature_of_other_things_is_not_the_weather(self):
        for text in ("set the temperature to 0.2 for the model", "what temperature should I bake bread at",
                     "convert 30 degrees to radians"):
            self.assertFalse(is_weather_request(text), text)
        self.assertTrue(is_weather_request("what's the temperature in London"))


class WebGuardTests(unittest.TestCase):
    """The web is never the weather source: weather searches and weather sites are caught, ordinary
    lookups are not."""

    def test_weather_searches_are_caught_and_their_place_found(self):
        for query, place in (("London weather tomorrow", "London"), ("will it rain in Paris tomorrow", "Paris"),
                             ("current temperature New York City", "New York City"), ("10-day forecast Boston", "Boston"),
                             ("weather this weekend London", "London"), ("accuweather Lisbon", "Lisbon"),
                             ("snow today Denver", "Denver"), ("chance of rain tomorrow", None), ("Weather in Tokyo", "Tokyo")):
            self.assertTrue(weather.is_weather_search(query), query)
            self.assertEqual(weather.search_place(query), place, query)

    def test_ordinary_searches_pass(self):
        for query in ("weather API python", "Purple Rain lyrics", "sales forecast 2026", "how to weather a recession",
                      "python 3.14 release date", "LLM temperature sampling", "best university degrees",
                      "Weather Channel stock price", "OKC Thunder score tonight", "rain garden plants", "news today"):
            self.assertFalse(weather.is_weather_search(query), query)

    def test_weather_sites_are_caught(self):
        for url in ("https://weather.com/weather/today/l/abc", "https://forecast.weather.gov/MapClick.php?x",
                    "https://www.bbc.co.uk/weather/2643743", "https://www.timeanddate.com/weather/uk/london",
                    "https://api.open-meteo.com/v1/forecast?lat=1", "https://www.google.com/search?q=london+weather",
                    "https://wttr.in/London"):
            self.assertTrue(weather.is_weather_url(url), url)
        for url in ("https://github.com/foo/weather", "https://www.bbc.co.uk/news/world", "https://docs.python.org/3/"):
            self.assertFalse(weather.is_weather_url(url), url)

    def test_web_weather_judges_each_tool(self):
        self.assertEqual(weather.web_weather("WebSearch", {"query": "London weather"}), "London weather")
        self.assertIsNotNone(weather.web_weather("WebSearch", {"query": "today", "allowed_domains": ["accuweather.com"]}))
        self.assertIsNotNone(weather.web_weather("WebFetch", {"url": "https://weather.com/x", "prompt": "summarise"}))
        self.assertIsNone(weather.web_weather("WebSearch", {"query": "python news"}))
        self.assertIsNone(weather.web_weather("WebFetch", {"url": "https://example.com/", "prompt": "is it raining in London"}))

    def test_lookup_never_raises_and_never_points_at_the_web(self):
        wk = Mock()
        wk.report.side_effect = lambda place, **kw: "home" if place is None else (_ for _ in ()).throw(WeatherError("no such place"))
        cfg = WeatherConfig()
        with patch.object(weather, "shared", return_value=wk):
            self.assertEqual(weather.lookup(cfg), "home")
            self.assertIn("This is the home report", weather.lookup(cfg, "Mordor"))
            wk.report.side_effect = WeatherError("401")
            self.assertIn("Never look the weather up on the web", weather.lookup(cfg))
        wk.report.side_effect = lambda place, **kw: f"report for {place}" if place == "Springfield" else (_ for _ in ()).throw(WeatherError("x"))
        with patch.object(weather, "shared", return_value=wk):
            self.assertEqual(weather.lookup(cfg, "Springfield IL", "week"), "report for Springfield")  # state code dropped
        self.assertIn("switched off", weather.lookup(WeatherConfig(enabled=False)))

    def test_named_place(self):
        self.assertEqual(requested_place("Is it going to rain in Lisbon tomorrow?"), "Lisbon")
        self.assertEqual(requested_place("weather for New York City please"), "New York City")
        self.assertEqual(requested_place("what's it like in Saint Albans on Monday"), "Saint Albans")
        self.assertIsNone(requested_place("what's the weather like today"))
        self.assertIsNone(requested_place("will it rain in the morning"))

    def test_place_carries_over_from_recent_turns(self):
        recent = ["what time is it in NYC", "cheers"]
        self.assertEqual(place_in_context("and what's the weather", recent), "NYC")
        self.assertEqual(place_in_context("weather in Lisbon", recent), "Lisbon")  # the prompt wins
        self.assertIsNone(place_in_context("what's it like outside", recent))  # pointing home
        self.assertIsNone(place_in_context("what's the weather here", recent))
        self.assertIsNone(place_in_context("what's the weather", ["what time is it in NYC", "a", "b", "c"]))  # too long ago
        self.assertIsNone(place_in_context("what's the weather", []))

    def test_scope_follows_the_question(self):
        for text in ("what's the weather like?", "is it going to rain later", "how cold is it outside", "weather in Lisbon"):
            self.assertEqual(requested_scope(text), "today", text)
        for text in ("give me the forecast for tomorrow", "will it rain tomorrow morning in Leeds"):
            self.assertEqual(requested_scope(text), "tomorrow", text)
        for text in ("will it be windy this weekend", "what's the week looking like", "weather for the next few days",
                     "is Friday going to be dry", "any rain on Saturday"):
            self.assertEqual(requested_scope(text), "week", text)


class TokenTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key = ec.generate_private_key(ec.SECP256R1())
        pem = self.key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        self.key_file = Path(self.tmp.name) / "AuthKey_ABC123.p8"
        self.key_file.write_bytes(pem)
        self.cfg = WeatherConfig(team_id="TEAM123456", service_id="com.example.vision.weather", key_id="ABC123",
                                 key_file=str(self.key_file))

    def test_token_is_a_valid_es256_jwt_with_apple_claims(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        token = make_token(self.cfg, now=1_700_000_000)
        header, claims, sig = token.split(".")
        self.assertEqual(json.loads(_b64(header)), {"alg": "ES256", "kid": "ABC123", "id": "TEAM123456.com.example.vision.weather"})
        self.assertEqual(json.loads(_b64(claims)), {"iss": "TEAM123456", "iat": 1_700_000_000,
                                                    "exp": 1_700_000_000 + 3000, "sub": "com.example.vision.weather"})
        raw = _b64(sig)
        self.assertEqual(len(raw), 64)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        self.key.public_key().verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))

    def test_missing_setup_is_explained(self):
        with self.assertRaises(WeatherError) as ctx:
            make_token(WeatherConfig(team_id="T"))
        self.assertIn("service_id", str(ctx.exception))
        with self.assertRaises(WeatherError) as ctx:
            make_token(WeatherConfig(team_id="T", service_id="s", key_id="k", key_file=str(Path(self.tmp.name) / "nope.p8")))
        self.assertIn("nope.p8", str(ctx.exception))

    def test_token_is_cached_and_sent_as_bearer(self):
        calls = []

        def fetch(url, headers=None, timeout=8):
            calls.append((url, headers))
            return {"currentWeather": {"conditionCode": "Clear", "temperature": 20}}

        wk = WeatherKit(WeatherConfig(**{**self.cfg.__dict__, "latitude": 51.5, "longitude": -0.12,
                                         "timezone": "Europe/London", "country_code": "GB", "location": "London"}))
        wk._fetch = fetch
        wk.report()
        wk.report(refresh=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertTrue(calls[0][1]["Authorization"].startswith("Bearer ey"))
        self.assertIn("weatherkit.apple.com/api/v1/weather/en/51.5000/-0.1200?", calls[0][0])
        self.assertIn("weatherAlerts", calls[0][0])
        self.assertIn("countryCode=GB", calls[0][0])


class ReportCacheTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("vision.weather._configured"))
        self.fetch = Mock(return_value={"currentWeather": {"temperature": 20}})
        self.wk = WeatherKit(WeatherConfig(team_id="T", service_id="s", key_id="k"), fetch=self.fetch)
        self.wk.token = Mock(return_value="token")
        self.place = Place("London", 51.5, -0.12)

    def test_reports_are_reused_until_expired(self):
        with patch("vision.weather.time.monotonic", return_value=10):
            self.wk.raw(self.place)
            self.wk.raw(self.place)
        self.assertEqual(self.fetch.call_count, 1)
        with patch("vision.weather.time.monotonic", return_value=131):
            self.wk.raw(self.place)
        self.assertEqual(self.fetch.call_count, 2)

    def test_refresh_and_other_places_do_not_reuse_cached_data(self):
        self.wk.raw(self.place)
        self.wk.raw(self.place, refresh=True)
        self.wk.raw(Place("Paris", 48.86, 2.35))
        self.assertEqual(self.fetch.call_count, 3)

    def test_expired_report_is_not_used_after_network_failure(self):
        with patch("vision.weather.time.monotonic", return_value=10):
            self.wk.raw(self.place)
        self.fetch.side_effect = WeatherError("offline")
        with patch("vision.weather.time.monotonic", return_value=131):
            with self.assertRaises(WeatherError):
                self.wk.raw(self.place)

    def test_explicit_spoken_refresh_bypasses_cache(self):
        wk = Mock()
        wk.report.return_value = "fresh report"
        with patch.object(weather, "shared", return_value=wk):
            thread = weather.prefetch(WeatherConfig(), "refresh the weather")
            thread.join(1)
        wk.report.assert_called_once_with(None, scope="today", refresh=True)


class SummaryTests(unittest.TestCase):
    def test_report_reads_naturally_and_credits_apple(self):
        from zoneinfo import ZoneInfo

        place = Place("London, England", 51.5, -0.12, "Europe/London", "GB")
        text = summarise(_sample(datetime.now(ZoneInfo("Europe/London"))), place, "metric")
        self.assertIn("Live weather for London, England, GB at", text)
        self.assertIn("Current: mostly cloudy, 14°C, feels like 11°C.", text)  # mild humidity, breeze and UV go unsaid
        self.assertRegex(text, r"Rain: 70% chance, rain likely from about \d\d:00 until about \d\d:00")
        self.assertIn("Today: partly cloudy, high 20°C, low 11°C, dry, sun 06:41 to 19:08.", text)
        self.assertIn("Tomorrow: rain, high 16°C, low 9°C, 60% chance of rain.", text)
        self.assertIn("ALERT: Yellow warning of wind (moderate, Met Office).", text)
        self.assertTrue(text.endswith("Source: Apple Weather."))

    def test_scope_trims_the_days_nobody_asked_about(self):
        from zoneinfo import ZoneInfo

        place = Place("London", 51.5, -0.12, "Europe/London", "GB")
        data = _sample(datetime.now(ZoneInfo("Europe/London")))
        today = summarise(data, place, "metric", "today")
        self.assertIn("Today:", today)
        self.assertNotIn("Tomorrow:", today)
        self.assertNotIn("Then:", today)
        self.assertIn("ALERT:", today)  # alerts always ride along
        tomorrow = summarise(data, place, "metric", "tomorrow")
        self.assertIn("Tomorrow:", tomorrow)
        self.assertNotIn("Then:", tomorrow)
        self.assertIn("Then:", summarise(data, place, "metric", "week"))

    def test_imperial_units(self):
        from zoneinfo import ZoneInfo

        place = Place("Boston", 42.3, -71.0, "America/New_York", "US")
        text = summarise(_sample(datetime.now(ZoneInfo("America/New_York"))), place, "imperial")
        self.assertIn("Current: mostly cloudy, 58°F, feels like 52°F.", text)

    def test_rain_window_says_when_it_stops(self):
        from zoneinfo import ZoneInfo

        place = Place("London", 51.5, -0.12, "Europe/London", "GB")
        now = datetime.now(ZoneInfo("Europe/London"))
        data = _sample(now)
        for h in data["forecastHourly"]["hours"][2:]:  # wet from two hours out, right through the sample
            h["precipitationChance"] = 0.8
        text = summarise(data, place, "metric", "today")
        self.assertIn("rain likely from about", text)
        self.assertIn(", lasting the rest of the day.", text)
        for h in data["forecastHourly"]["hours"]:
            h["precipitationChance"] = 0.8
        data["forecastHourly"]["hours"][5]["precipitationChance"] = 0.0
        text = summarise(data, place, "metric", "today")
        self.assertRegex(text, r"Rain: rain now \(80% chance over the next hours\), easing off about \d\d:00")
        for h in data["forecastHourly"]["hours"]:
            h["precipitationChance"] = 0.0
        self.assertIn("Rain: none expected in the next 12 hours.", summarise(data, place, "metric", "today"))

    def test_notable_conditions_are_mentioned(self):
        from zoneinfo import ZoneInfo

        place = Place("London", 51.5, -0.12, "Europe/London", "GB")
        data = _sample(datetime.now(ZoneInfo("Europe/London")))
        data["currentWeather"].update({"temperature": 27, "temperatureApparent": 27, "humidity": 0.9, "windSpeed": 45, "uvIndex": 8})
        text = summarise(data, place, "metric", "today")
        self.assertIn("Current: mostly cloudy, 27°C, humid, windy, 45 km/h, UV 8, strong sun.", text)


class GeocodeTests(unittest.TestCase):
    def test_places_are_geocoded_once_and_cached(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fetch = Mock(return_value={"results": [{"name": "Lisbon", "admin1": "Lisbon", "latitude": 38.7, "longitude": -9.1,
                                                "timezone": "Europe/Lisbon", "country_code": "PT"}]})
        with patch.object(weather, "GEOCODE_CACHE", Path(tmp.name) / "places.json"):
            wk = WeatherKit(WeatherConfig(), fetch=fetch)
            place = wk.geocode("Lisbon")
            self.assertEqual((place.name, place.timezone, place.country), ("Lisbon", "Europe/Lisbon", "PT"))
            again = WeatherKit(WeatherConfig(), fetch=fetch).geocode("  lisbon ")
        self.assertEqual(again, place)
        self.assertEqual(fetch.call_count, 1)

    def test_unknown_place_and_no_default(self):
        with patch.object(weather, "GEOCODE_CACHE", Path(tempfile.mkdtemp()) / "places.json"):
            wk = WeatherKit(WeatherConfig(), fetch=Mock(return_value={"results": []}))
            with self.assertRaises(WeatherError):
                wk.geocode("Mordor")
            with self.assertRaises(WeatherError) as ctx:
                wk.default_place()
        self.assertIn("weather.location", str(ctx.exception))


class KeylessTests(unittest.TestCase):
    """No WeatherKit key: the Weather Service answers for the US, Open-Meteo for everywhere else."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(patch.object(weather, "GRID_CACHE", Path(tmp.name) / "grids.json"))
        self.now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        self.calls = []

    def _nws_hours(self):
        periods = []
        for i in range(30):
            wet = i in (3, 4)
            periods.append({"startTime": (self.now + timedelta(hours=i)).isoformat(), "isDaytime": True,
                            "temperature": 15 + i % 5, "windSpeed": "10 to 35 km/h", "relativeHumidity": {"value": 60},
                            "probabilityOfPrecipitation": {"value": 70 if wet else 10 if i == 6 else None},
                            "shortForecast": "Rain Showers" if wet else "Slight Chance Rain Showers" if i == 6 else "Mostly Sunny"})
        return {"properties": {"periods": periods}}

    def _fetch(self, url, headers=None, timeout=8):
        self.calls.append(url)
        if "/points/" in url:
            return {"properties": {"forecastHourly": "https://api.weather.gov/gridpoints/ILX/50,60/forecast/hourly"}}
        if "/alerts/" in url:
            return {"features": [{"properties": {"event": "Flood Watch", "headline": "Flood Watch until 6 PM",
                                                 "severity": "Moderate", "senderName": "NWS Lincoln IL"}}]}
        if "gridpoints" in url:
            return self._nws_hours()
        raise AssertionError(url)

    def test_a_us_place_is_answered_by_the_weather_service(self):
        wk = WeatherKit(WeatherConfig(), fetch=self._fetch)
        place = Place("Springfield, Illinois", 39.8, -89.64, "UTC", "US")
        self.assertEqual(wk.source(place), "nws")
        text = summarise(wk.raw(place), place)
        self.assertIn("- Current: mostly clear, 15°C, windy, 35 km/h.", text)
        self.assertIn("showers likely from about", text)
        self.assertIn("ALERT: Flood Watch until 6 PM (moderate, NWS Lincoln IL)", text)
        self.assertTrue(text.endswith("Source: US National Weather Service."))
        self.assertTrue(any(u.endswith("forecast/hourly?units=si") for u in self.calls))
        # The grid lookup is kept on disk: a new process asks for the forecast straight away.
        self.calls.clear()
        WeatherKit(WeatherConfig(), fetch=self._fetch).raw(place)
        self.assertFalse(any("/points/" in u for u in self.calls))

    def test_a_faint_chance_of_showers_is_a_grey_hour(self):
        self.assertEqual(weather._nws_condition("Slight Chance Rain Showers", 0.1), "MostlyCloudy")
        self.assertEqual(weather._nws_condition("Chance Rain Showers", 0.6), "Showers")
        self.assertEqual(weather._nws_condition("Patchy Frost", 0), "PatchyFrost")
        self.assertEqual(weather._condition("PatchyFrost"), "patchy frost")

    def _open_meteo(self):
        local = self.now.replace(tzinfo=None)
        times = [(local + timedelta(hours=i)).isoformat(timespec="minutes") for i in range(30)]
        day = local.date()
        return {"current": {"temperature_2m": 14.4, "apparent_temperature": 11.0, "relative_humidity_2m": 82,
                            "wind_speed_10m": 12, "weather_code": 3, "uv_index": 1.6},
                "hourly": {"time": times, "weather_code": [61 if i in (3, 4) else 3 for i in range(30)],
                           "precipitation_probability": [70 if i in (3, 4) else 5 for i in range(30)],
                           "temperature_2m": [15] * 30},
                "daily": {"time": [day.isoformat(), (day + timedelta(days=1)).isoformat()], "weather_code": [61, 71],
                          "temperature_2m_max": [19.6, 2], "temperature_2m_min": [11.2, -3],
                          "precipitation_probability_max": [70, 80],
                          "sunrise": [f"{day}T06:41", f"{day + timedelta(days=1)}T06:42"],
                          "sunset": [f"{day}T19:08", f"{day + timedelta(days=1)}T19:06"]}}

    def test_elsewhere_is_answered_by_open_meteo(self):
        fetch = Mock(return_value=self._open_meteo())
        wk = WeatherKit(WeatherConfig(), fetch=fetch)
        place = Place("London", 51.5, -0.12, "UTC", "GB")
        self.assertEqual(wk.source(place), "open-meteo")
        text = summarise(wk.raw(place), place, scope="tomorrow")
        self.assertIn("api.open-meteo.com/v1/forecast?latitude=51.5000&longitude=-0.1200", fetch.call_args[0][0])
        self.assertIn("- Current: cloudy, 14°C, feels like 11°C.", text)
        self.assertIn("rain likely from about", text)
        self.assertIn("- Today: rain, high 20°C, low 11°C, 70% chance of rain, sun 06:41 to 19:08.", text)
        self.assertIn("- Tomorrow: snow, high 2°C, low -3°C, 80% chance of snow.", text)
        self.assertTrue(text.endswith("Source: Open-Meteo."))

    def test_open_meteo_stands_in_when_the_weather_service_fails(self):
        def fetch(url, headers=None, timeout=8):
            if "weather.gov" in url:
                raise WeatherError("503 from api.weather.gov")
            return self._open_meteo()

        wk = WeatherKit(WeatherConfig(), fetch=fetch)
        self.assertEqual(wk.raw(Place("Springfield", 39.8, -89.64, "UTC", "US"))["source"], "open-meteo")

    def test_a_key_puts_weatherkit_in_charge_and_the_weather_is_on_without_one(self):
        from vision.config import weather_ready

        keyed = WeatherKit(WeatherConfig(team_id="T", service_id="s", key_id="k"))
        self.assertEqual(keyed.source(Place("Springfield", 39.8, -89.64, "UTC", "US")), "weatherkit")
        self.assertTrue(weather_ready(Config().brain))
        cfg = Config()
        cfg.weather.enabled = False
        self.assertFalse(weather_ready(cfg.brain))

    def test_a_state_after_the_town_picks_that_one(self):
        results = [{"name": "Springfield", "admin1": "Missouri", "country_code": "US", "feature_code": "PPLA2",
                    "latitude": 37.2, "longitude": -93.3, "timezone": "America/Chicago"},
                   {"name": "Springfield", "admin1": "Illinois", "country_code": "US", "feature_code": "PPLA",
                    "latitude": 39.8, "longitude": -89.6, "timezone": "America/Chicago"}]
        with patch.object(weather, "GEOCODE_CACHE", Path(tempfile.mkdtemp()) / "places.json"):
            fetch = Mock(return_value={"results": results})
            place = WeatherKit(WeatherConfig(), fetch=fetch).geocode("Springfield, IL")
            self.assertEqual(place.name, "Springfield, Illinois")
            self.assertIn("name=Springfield&", fetch.call_args[0][0])
            with self.assertRaises(WeatherError):
                WeatherKit(WeatherConfig(), fetch=fetch).geocode("Springfield, Oregon")

    def test_a_bare_state_is_not_a_town(self):
        fetch = Mock(return_value={"results": [{"name": "New Jersey", "admin1": "Siparia", "country_code": "TT",
                                                "latitude": 10.1, "longitude": -61.4, "timezone": "America/Port_of_Spain"}]})
        with patch.object(weather, "GEOCODE_CACHE", Path(tempfile.mkdtemp()) / "places.json"):
            with self.assertRaises(WeatherError) as ctx:
                WeatherKit(WeatherConfig(), fetch=fetch).geocode("New Jersey")
        self.assertIn("whole state", str(ctx.exception))
        fetch.assert_not_called()


class PrefetchTests(unittest.TestCase):
    def setUp(self):
        weather._shared.clear()
        self.cfg = WeatherConfig(team_id="T", service_id="s", key_id="k", location="London")

    def test_only_weather_requests_fetch(self):
        self.assertIsNone(weather.prefetch(self.cfg, "fix the tests"))
        self.assertIsNone(weather.prefetch(WeatherConfig(enabled=False), "what's the weather"))

    def test_report_and_fallback_to_home(self):
        wk = Mock()
        wk.report.side_effect = lambda place, **kw: "home report" if place is None else (_ for _ in ()).throw(WeatherError("no such place"))
        with patch.object(weather, "shared", return_value=wk):
            t = weather.prefetch(self.cfg, "weather in Mordor")
            t.join(5)
        self.assertEqual(t.result, "home report")
        self.assertEqual(t.error, "no such place")
        wk.report.side_effect = lambda place, **kw: "the report"
        with patch.object(weather, "shared", return_value=wk):
            t = weather.prefetch(self.cfg, "will it rain in Lisbon")
            t.join(5)
        self.assertEqual((t.result, t.error), ("the report", None))
        wk.report.assert_called_with("Lisbon", scope="today")
        with patch.object(weather, "shared", return_value=wk):
            weather.prefetch(self.cfg, "will it rain this weekend").join(5)
        wk.report.assert_called_with(None, scope="week")
        with patch.object(weather, "shared", return_value=wk):
            weather.prefetch(self.cfg, "what's the weather", recent=["what time is it in New York"]).join(5)
        wk.report.assert_called_with("New York", scope="today")

    def test_unexpected_errors_never_escape(self):
        wk = Mock()
        wk.report.side_effect = RuntimeError("boom")
        with patch.object(weather, "shared", return_value=wk):
            t = weather.prefetch(self.cfg, "what's the weather")
            t.join(5)
        self.assertIsNone(t.result)
        self.assertEqual(t.error, "RuntimeError: boom")


class ConversationInjectionTests(unittest.TestCase):
    def setUp(self):
        from vision.conversation import VoiceConversation

        self.cfg = Config()
        self.cfg.weather.team_id = self.cfg.weather.service_id = self.cfg.weather.key_id = "x"
        self.cfg.weather.location = "London"
        self.agent = SimpleNamespace(cfg=self.cfg.brain, provider="claude", session_id="s", workdir="/tmp", ask=Mock())
        self.voice = VoiceConversation(self.cfg, self.agent)
        for target, value in (("vision.conversation.local_handoff", ""), ("vision.memory.facts", [])):
            p = patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _model(self):
        model = Mock()
        model.usage = None
        model.complete.return_value = {"speech": "Grey and mild.", "task": None}
        self.voice.model = model
        return model

    def test_weather_report_rides_along_in_the_packet(self):
        model = self._model()
        wk = Mock()
        wk.report.return_value = "Live weather for London: now cloudy, 14°C.\nSource: Apple Weather."
        with patch.object(weather, "shared", return_value=wk):
            turn = self.voice.ask("what's the weather like?")
        self.assertFalse(turn.is_error)
        packet = model.complete.call_args[0][0]
        self.assertEqual(packet["weather"], wk.report.return_value)
        wk.report.assert_called_once_with(None, scope="today")

    def test_no_weather_field_for_ordinary_chat(self):
        model = self._model()
        with patch.object(weather, "shared") as shared:
            self.voice.ask("how's it going")
        shared.assert_not_called()
        self.assertNotIn("weather", model.complete.call_args[0][0])

    def test_failure_is_reported_as_data_not_speech(self):
        model = self._model()
        wk = Mock()
        wk.report.side_effect = WeatherError("401 from weatherkit.apple.com")
        with patch.object(weather, "shared", return_value=wk):
            turn = self.voice.ask("is it raining?")
        self.assertFalse(turn.is_error)
        self.assertIn("The weather service could not answer (401 from weatherkit.apple.com)", model.complete.call_args[0][0]["weather"])
        self.assertNotIn("web search", model.complete.call_args[0][0]["weather"])
        self.assertEqual(turn.text, "Grey and mild.")

    def test_the_model_can_ask_weatherkit_itself(self):
        model = self._model()
        model.complete.side_effect = [{"speech": "", "task": None, "weather": {"places": ["Lisbon"], "when": "tomorrow"}},
                                      {"speech": "Sunny in Lisbon tomorrow.", "task": None}]
        wk = Mock()
        wk.report.return_value = "Live weather for Lisbon.\nSource: Apple Weather."
        with patch.object(weather, "shared", return_value=wk):
            turn = self.voice.ask("and what about my trip")
        self.assertFalse(turn.is_error, turn.error)
        wk.report.assert_called_once_with("Lisbon", scope="tomorrow")
        events = model.complete.call_args[0][0]["turn"]["events"]  # the turn's events as they ended: the reply follows the report
        self.assertEqual(events[-2], {"weather": wk.report.return_value})
        self.assertEqual(turn.text, "Sunny in Lisbon tomorrow.")

    def test_many_places_are_one_weather_call(self):
        model = self._model()
        towns = ["Houston, TX", "Dallas, TX", "Austin, TX"]
        model.complete.side_effect = [{"speech": "", "task": None, "weather": {"places": towns}},
                                      {"speech": "Hot everywhere.", "task": None}]
        wk = Mock()
        wk.report.side_effect = lambda place, scope: f"Live weather for {place}."
        with patch.object(weather, "shared", return_value=wk):
            turn = self.voice.ask("what's it like in three Texas cities")
        self.assertFalse(turn.is_error, turn.error)
        self.assertEqual(wk.report.call_count, 3)
        self.assertEqual(model.complete.call_args[0][0]["turn"]["events"][-2],
                         {"weather": "\n\n".join(f"Live weather for {t}." for t in towns)})

    def test_a_weather_search_is_answered_from_weatherkit_and_never_reaches_the_web(self):
        model = self._model()
        model.complete.side_effect = [{"speech": "", "task": None, "search": {"query": "Paris weather tomorrow"}},
                                      {"speech": "Rain in Paris.", "task": None}]
        wk = Mock()
        wk.report.return_value = "Live weather for Paris."
        with patch.object(weather, "shared", return_value=wk), patch("vision.search.search") as search:
            turn = self.voice.ask("how's my trip looking")
        self.assertFalse(turn.is_error, turn.error)
        search.assert_not_called()
        wk.report.assert_called_once_with("Paris", scope="tomorrow")
        self.assertEqual(model.complete.call_args[0][0]["turn"]["events"][-2], {"weather": "Live weather for Paris."})

    def test_an_ordinary_search_still_searches(self):
        from vision.search import SearchResult

        model = self._model()
        model.complete.side_effect = [{"speech": "", "task": None, "search": {"query": "python 3.14 release date"}},
                                      {"speech": "October.", "task": None}]
        hit = [SearchResult("Python 3.14", "https://python.org/", "Released in October.", "python.org", "")]
        with patch.object(weather, "shared") as shared, patch("vision.search.search", return_value=hit) as search:
            turn = self.voice.ask("when did python 3.14 come out")
        self.assertFalse(turn.is_error, turn.error)
        search.assert_called_once()
        shared.assert_not_called()
        self.assertIn("search_results", model.complete.call_args[0][0]["turn"]["events"][-2])


class ClaudeWebToolGuardTests(unittest.TestCase):
    """The Claude voice model's own WebSearch/WebFetch calls come to Vision for permission: a weather
    call is denied with the WeatherKit report, anything else runs."""

    def setUp(self):
        from vision.conversation import ClaudeConversation

        self.cfg = Config()
        self.model = ClaudeConversation(self.cfg)
        self.proc = SimpleNamespace(stdin=Mock())

    def answer(self, tool, tool_input, subtype="can_use_tool"):
        self.proc.stdin.reset_mock()
        self.model._answer_control(self.proc, {"type": "control_request", "request_id": "r1",
                                               "request": {"subtype": subtype, "tool_name": tool, "input": tool_input}})
        msg = json.loads(self.proc.stdin.write.call_args[0][0])
        self.assertEqual((msg["type"], msg["response"]["request_id"]), ("control_response", "r1"))
        return msg["response"]

    def test_command_routes_every_web_call_through_vision(self):
        with patch("vision.conversation.find_claude", return_value="claude"):
            cmd = self.model._command()
        self.assertEqual(cmd[cmd.index("--permission-prompt-tool") + 1], "stdio")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "default")

    def test_weather_search_is_denied_with_the_report(self):
        wk = Mock()
        wk.report.return_value = "Live weather for Leeds: rain.\nSource: Apple Weather."
        with patch.object(weather, "shared", return_value=wk):
            reply = self.answer("WebSearch", {"query": "Leeds weather tomorrow"})
        self.assertEqual(reply["response"]["behavior"], "deny")
        self.assertIn("never takes the weather from the web", reply["response"]["message"])
        self.assertIn(wk.report.return_value, reply["response"]["message"])
        wk.report.assert_called_once_with("Leeds", scope="tomorrow")
        with patch.object(weather, "shared", return_value=wk):
            reply = self.answer("WebFetch", {"url": "https://www.accuweather.com/en/gb/leeds", "prompt": "forecast"})
        self.assertEqual(reply["response"]["behavior"], "deny")

    def test_ordinary_web_calls_run_and_other_tools_do_not(self):
        with patch.object(weather, "shared") as shared:
            reply = self.answer("WebSearch", {"query": "python 3.14 release date"})
            self.assertEqual(reply["response"], {"behavior": "allow", "updatedInput": {"query": "python 3.14 release date"}})
            reply = self.answer("WebFetch", {"url": "https://docs.python.org/3/whatsnew/", "prompt": "summarise"})
            self.assertEqual(reply["response"]["behavior"], "allow")
        shared.assert_not_called()
        self.assertEqual(self.answer("Bash", {"command": "ls"})["response"]["behavior"], "deny")
        self.assertEqual(self.answer("", {}, subtype="hook_callback")["subtype"], "error")

    def test_control_requests_are_answered_mid_turn(self):
        """A fake CLI asks permission for two searches in one turn, then replies once it has both answers."""
        import sys

        script = """
import json, sys
sys.stdin.readline()
for i, q in enumerate(["Leeds weather now", "python news"]):
    print(json.dumps({"type": "control_request", "request_id": f"r{i}", "request": {"subtype": "can_use_tool", "tool_name": "WebSearch", "input": {"query": q}}}), flush=True)
    answer = json.loads(sys.stdin.readline())
    print(json.dumps({"type": "assistant", "message": {"content": []}, "seen": answer}), flush=True)
    sys.stderr.write(answer["response"]["response"]["behavior"] + "\\n")
    sys.stderr.flush()
    open(sys.argv[1], "a").write(answer["response"]["response"]["behavior"] + "\\n")
print(json.dumps({"type": "result", "structured_output": {"speech": "done", "task": None}}), flush=True)
sys.stdin.readline()
"""
        out = Path(self.enterContext(tempfile.TemporaryDirectory())) / "answers"
        self.addCleanup(self.model.close)
        wk = Mock()
        wk.report.return_value = "Live weather for Leeds."
        with patch.object(self.model, "_command", return_value=[sys.executable, "-u", "-c", script, str(out)]), \
                patch.object(weather, "shared", return_value=wk):
            import threading

            reply = self.model.complete({"history": [], "turn": {"user": "hi", "events": []}}, threading.Event())
        self.assertEqual(reply["speech"], "done")
        self.assertEqual(out.read_text().split(), ["deny", "allow"])


if __name__ == "__main__":
    unittest.main()
