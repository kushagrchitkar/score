import json
import copy
import inspect
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from unittest.mock import patch
from datetime import date, datetime, timezone
from pathlib import Path

from score.cli import _dates, discover, discover_supported, list_events, pin_event, render_once, run_demo_loop, run_watch_loop, watch
from score.events import Event, Team, find_events, format_event, parse_espn_event
from score.espn import ESPNClient, ESPNCricketClient, _get_json
from score.storage import FollowStore
from score.title import osc_title


LIVE_EVENT = {
    "id": "123",
    "date": "2026-07-25T15:00Z",
    "status": {
        "displayClock": "73'",
        "type": {"state": "in", "completed": False, "shortDetail": "73'"},
    },
    "competitions": [{
        "status": {
            "displayClock": "73'",
            "type": {"state": "in", "completed": False, "shortDetail": "73'"},
        },
        "competitors": [
            {"homeAway": "home", "score": "0", "team": {"id": "359", "displayName": "Arsenal", "abbreviation": "ARS"}},
            {"homeAway": "away", "score": "2", "team": {"id": "382", "displayName": "Manchester City", "abbreviation": "MCI"}},
        ],
    }],
}

LIVE_MLB_EVENT = {
    "id": "mlb-123",
    "date": "2026-07-26T23:00Z",
    "status": {
        "displayClock": "0:00", "period": 7,
        "type": {"state": "in", "completed": False, "shortDetail": "Top 7th"},
    },
    "competitions": [{
        "status": {
            "displayClock": "0:00", "period": 7,
            "type": {"state": "in", "completed": False, "shortDetail": "Top 7th"},
        },
        "competitors": [
            {"homeAway": "home", "score": "2", "team": {"id": "2", "displayName": "Boston Red Sox", "abbreviation": "BOS"}},
            {"homeAway": "away", "score": "3", "team": {"id": "10", "displayName": "New York Yankees", "abbreviation": "NYY"}},
        ],
    }],
}

LIVE_CRICKET_EVENT = {
    "id": "1538630",
    "date": "2026-07-25T13:30Z",
    "leagueId": "24436",
    "status": {"type": {"state": "in", "description": "Stumps"}},
    "competitions": [{
        "status": {
            "session": "Day 1",
            "type": {"state": "in", "description": "Stumps", "shortDetail": "Live"},
        },
        "competitors": [
            {
                "homeAway": "home", "score": "194/3 (67 ov)",
                "team": {"id": "4", "displayName": "West Indies", "abbreviation": "WI"},
                "linescores": [{"isBatting": True, "isCurrent": 1}],
            },
            {
                "homeAway": "away", "score": "",
                "team": {"id": "7", "displayName": "Pakistan", "abbreviation": "PAK"},
                "linescores": [{"isBatting": False, "isCurrent": 1}],
            },
        ],
    }],
}


class EventTests(unittest.TestCase):
    def test_parses_and_formats_live_football_event(self):
        event = parse_espn_event(LIVE_EVENT)
        self.assertEqual(event.id, "123")
        self.assertEqual(format_event(event), "ARS 0–2 MCI · 73′")

    def test_parses_and_formats_live_baseball_away_team_first(self):
        event = parse_espn_event(LIVE_MLB_EVENT, sport="baseball")
        self.assertEqual(event.sport, "baseball")
        self.assertEqual(format_event(event), "NYY 3–2 BOS · Top 7th")

    def test_formats_final_baseball_game(self):
        event = parse_espn_event(LIVE_MLB_EVENT, sport="baseball")
        event = Event(**{**event.__dict__, "state": "post", "detail": "Final"})
        self.assertEqual(format_event(event), "NYY 3–2 BOS · Final")

    def test_parses_and_formats_live_cricket_batting_side_first(self):
        event = parse_espn_event(LIVE_CRICKET_EVENT, sport="cricket")
        self.assertEqual(event.league, "24436")
        self.assertEqual(event.active_side, "home")
        self.assertEqual(format_event(event), "WI 194/3 (67 ov) · PAK · Stumps")

    def test_cricket_batting_team_id_places_away_batting_side_first(self):
        raw = copy.deepcopy(LIVE_CRICKET_EVENT)
        competition = raw["competitions"][0]
        competition["status"]["battingTeamId"] = "7"
        competition["competitors"][0]["score"] = "250"
        competition["competitors"][1]["score"] = "120/3 (22 ov)"
        for competitor in competition["competitors"]:
            competitor["linescores"] = []
        event = parse_espn_event(raw, sport="cricket")
        self.assertEqual(event.active_side, "away")
        self.assertEqual(format_event(event), "PAK 120/3 (22 ov) · WI 250 · Stumps")

    def test_cricket_without_a_batting_side_keeps_home_team_first(self):
        event = parse_espn_event(LIVE_CRICKET_EVENT, sport="cricket")
        event = Event(**{**event.__dict__, "home_score": "", "away_score": "", "active_side": "", "detail": "Live"})
        self.assertEqual(format_event(event), "WI · PAK · Live")

    def test_formats_full_time(self):
        event = parse_espn_event(LIVE_EVENT)
        event = Event(**{**event.__dict__, "state": "post", "detail": "FT"})
        self.assertEqual(format_event(event), "ARS 0–2 MCI · FT")

    def test_formats_upcoming_in_local_time(self):
        event = Event(
            id="9", sport="soccer", start=datetime(2026, 7, 25, 17, 30, tzinfo=timezone.utc),
            home=Team("1", "Arsenal", "ARS"), away=Team("2", "Chelsea", "CHE"),
            home_score=None, away_score=None, state="pre", detail="Scheduled",
        )
        self.assertEqual(format_event(event, timezone.utc), "ARS – CHE · 17:30")

    def test_fuzzy_match_finds_team_name_and_abbreviation(self):
        arsenal = parse_espn_event(LIVE_EVENT)
        other_data = json.loads(json.dumps(LIVE_EVENT))
        other_data["id"] = "456"
        other_data["competitions"][0]["competitors"][0]["team"].update(
            {"id": "10", "displayName": "Liverpool", "abbreviation": "LIV"}
        )
        other = parse_espn_event(other_data)
        self.assertEqual([e.id for e in find_events([other, arsenal], "arsenal")], ["123"])
        self.assertEqual([e.id for e in find_events([other, arsenal], "ars mci")], ["123"])


class ESPNClientTests(unittest.TestCase):
    def test_retries_transient_provider_failure(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"events": []}'

        transient = HTTPError("https://example.test", 502, "Bad Gateway", {}, None)
        with patch("score.espn.urllib.request.urlopen", side_effect=[transient, Response()]) as urlopen, \
             patch("score.espn.time", create=True):
            self.assertEqual(_get_json("https://example.test"), {"events": []})
        self.assertEqual(urlopen.call_count, 2)

    def test_discovers_events_from_scoreboard(self):
        requested = []
        def transport(url):
            requested.append(url)
            return {"events": [LIVE_EVENT]}
        events = ESPNClient(transport=transport).events("20260725")
        self.assertEqual([event.id for event in events], ["123"])
        self.assertIn("dates=20260725", requested[0])

    def test_uses_sport_specific_scoreboard(self):
        requested = []
        client = ESPNClient(sport="baseball", league="mlb", transport=lambda url: requested.append(url) or {"events": [LIVE_MLB_EVENT]})
        events = client.events("20260726")
        self.assertIn("/sports/baseball/mlb/scoreboard", requested[0])
        self.assertEqual(events[0].sport, "baseball")

    def test_discovers_all_series_cricket_events_with_dynamic_league(self):
        requested = []
        raw = {**LIVE_CRICKET_EVENT}
        raw.pop("leagueId")
        payload = {"scores": [{"leagues": [{"id": "24436"}], "events": [raw]}]}
        client = ESPNCricketClient(transport=lambda url: requested.append(url) or payload)
        events = client.events("20260726")
        self.assertIn("/sports/cricket/scorepanel", requested[0])
        self.assertIn("dates=20260726", requested[0])
        self.assertEqual(events[0].league, "24436")
        self.assertEqual(events[0].sport, "cricket")

    def test_cricket_discovery_uses_one_current_scorepanel_request(self):
        requested = []
        payload = {"scores": [{"leagues": [{"id": "24436"}], "events": [LIVE_CRICKET_EVENT]}]}
        client = ESPNCricketClient(transport=lambda url: requested.append(url) or payload)
        discover(client, days=1)
        self.assertEqual(len(requested), 1)

    def test_searches_for_stable_team_identity(self):
        def transport(url):
            return {"items": [{
                "id": "359", "displayName": "Arsenal", "abbreviation": "ARS",
                "sport": "soccer", "type": "team",
            }]}
        teams = ESPNClient(transport=transport).search_teams("arsenal")
        self.assertEqual(teams, [Team("359", "Arsenal", "ARS")])

    def test_fetches_exact_pinned_event_when_summary_date_is_nested(self):
        summary = json.loads(json.dumps(LIVE_EVENT))
        del summary["date"]
        summary["competitions"][0]["date"] = "2026-07-25T15:00Z"
        def transport(url):
            return {"header": summary}
        event = ESPNClient(transport=transport).event("123")
        self.assertEqual(event.id, "123")
        self.assertEqual(event.start, datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc))


class TitleTests(unittest.TestCase):
    def test_osc_title_uses_standard_terminal_sequence(self):
        self.assertEqual(osc_title("ARS 0–2 MCI · 73′"), "\x1b]0;ARS 0–2 MCI · 73′\x07")

    def test_title_strips_control_characters(self):
        self.assertEqual(osc_title("ARS\x07 hacked\n"), "\x1b]0;ARS hacked\x07")


class CLITests(unittest.TestCase):
    def test_one_provider_failure_does_not_hide_other_live_sports(self):
        live = parse_espn_event(LIVE_EVENT)

        class FailingClient:
            current_snapshot_only = True
            def events(self, date): raise OSError("provider unavailable")

        class WorkingClient:
            current_snapshot_only = True
            def events(self, date): return [live]

        with patch("score.cli.supported_clients", return_value=(FailingClient(), WorkingClient())):
            self.assertEqual(discover_supported(days=1), [live])

    def test_malformed_provider_does_not_hide_other_live_sports(self):
        live = parse_espn_event(LIVE_EVENT)

        class MalformedClient:
            current_snapshot_only = True
            def events(self, date): raise ValueError("malformed provider payload")

        class WorkingClient:
            current_snapshot_only = True
            def events(self, date): return [live]

        with patch("score.cli.supported_clients", return_value=(MalformedClient(), WorkingClient())):
            self.assertEqual(discover_supported(days=1), [live])

    def test_discovery_dates_cover_late_games_across_utc_midnight(self):
        self.assertEqual(_dates(days=1, today=date(2026, 7, 26)), ["20260725", "20260726", "20260727"])

    def test_render_once_writes_formatted_event_as_title(self):
        class Client:
            def event(self, event_id):
                self.seen = event_id
                return parse_espn_event(LIVE_EVENT)
        class Stream:
            def __init__(self): self.value = ""
            def write(self, value): self.value += value
            def flush(self): pass
        client, stream = Client(), Stream()
        title = render_once(client, "123", stream)
        self.assertEqual(client.seen, "123")
        self.assertEqual(title, "ARS 0–2 MCI · 73′")
        self.assertEqual(stream.value, osc_title(title))


    def test_cricket_pin_preserves_series_for_exact_refresh(self):
        class Process:
            pid = 4321

        class Stream:
            def write(self, value): return len(value)
            def flush(self): pass

        event = parse_espn_event(LIVE_CRICKET_EVENT, sport="cricket")
        with patch("score.cli._tty", return_value="/dev/pts/9"), \
             patch("score.cli.unpin"), \
             patch("score.cli.sys.stdout", Stream()), \
             patch("score.cli.subprocess.Popen", return_value=Process()) as popen, \
             patch("score.cli._write_state"):
            pin_event(event)
        command = popen.call_args.args[0]
        self.assertEqual(command[4:8], [event.id, "cricket", "24436", "/dev/pts/9"])

    def test_real_watch_refresh_interval_is_ten_seconds(self):
        self.assertEqual(inspect.signature(watch).parameters["interval"].default, 10)

    def test_demo_writes_sequence_and_restores_title(self):
        class Stream:
            def __init__(self): self.value = ""
            def write(self, value): self.value += value
            def flush(self): pass
        stream, stop = Stream(), threading.Event()
        titles = ["ARS 0–0 MCI · 72′", "ARS 0–1 MCI · 73′", "ARS 1–1 MCI · FT"]
        run_demo_loop(stream, "RESTORED", 0, stop, titles=titles)
        expected = "".join(osc_title(title) for title in titles) + osc_title("RESTORED")
        self.assertEqual(stream.value, expected)

    def test_demo_can_be_unpinned_before_sequence_finishes(self):
        class Stream:
            def __init__(self): self.value = ""
            def write(self, value): self.value += value
            def flush(self): pass
        stream, stop = Stream(), threading.Event()
        stop.set()
        run_demo_loop(stream, "RESTORED", 30, stop)
        self.assertEqual(stream.value, osc_title("RESTORED"))

    def test_selector_shows_only_live_events_and_pins_exact_selection(self):
        live = parse_espn_event(LIVE_EVENT)
        upcoming = Event(**{**live.__dict__, "id": "pre", "state": "pre", "detail": "Scheduled"})
        finished = Event(**{**live.__dict__, "id": "post", "state": "post", "detail": "FT"})
        with patch("score.cli.discover_supported", return_value=[upcoming, live, finished]), \
             patch("score.cli.pin_event") as pin_event, \
             patch("score.cli.sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="1"):
            list_events(pin_interactively=True)
        pin_event.assert_called_once_with(live)

    def test_watch_loop_can_be_stopped_without_waiting_for_refresh_interval(self):
        class Client:
            def __init__(self): self.called = threading.Event()
            def event(self, _event_id):
                self.called.set()
                return parse_espn_event(LIVE_EVENT)
        class Stream:
            def __init__(self): self.value = ""
            def write(self, value): self.value += value
            def flush(self): pass
        client, stream, stop = Client(), Stream(), threading.Event()
        worker = threading.Thread(target=run_watch_loop, args=(client, "123", stream, "RESTORED", 30, stop))
        worker.start()
        self.assertTrue(client.called.wait(1))
        started = time.monotonic()
        stop.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn("RESTORED", stream.value)


class FollowStoreTests(unittest.TestCase):
    def test_follow_is_persistent_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FollowStore(Path(directory) / "follows.json")
            store.add(Team("359", "Arsenal", "ARS"), "soccer")
            store.add(Team("359", "Arsenal", "ARS"), "soccer")
            self.assertEqual(store.list(), [{
                "provider": "espn", "sport": "soccer", "team_id": "359",
                "name": "Arsenal", "abbreviation": "ARS",
            }])


if __name__ == "__main__":
    unittest.main()
