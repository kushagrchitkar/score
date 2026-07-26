import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

from score.cli import list_events, render_once, run_watch_loop
from score.events import Event, Team, find_events, format_event, parse_espn_event
from score.espn import ESPNClient
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


class EventTests(unittest.TestCase):
    def test_parses_and_formats_live_football_event(self):
        event = parse_espn_event(LIVE_EVENT)
        self.assertEqual(event.id, "123")
        self.assertEqual(format_event(event), "ARS 0–2 MCI · 73′")

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
    def test_discovers_events_from_scoreboard(self):
        requested = []
        def transport(url):
            requested.append(url)
            return {"events": [LIVE_EVENT]}
        events = ESPNClient(transport=transport).events("20260725")
        self.assertEqual([event.id for event in events], ["123"])
        self.assertIn("dates=20260725", requested[0])

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


    def test_selector_shows_only_live_events_and_pins_exact_selection(self):
        live = parse_espn_event(LIVE_EVENT)
        upcoming = Event(**{**live.__dict__, "id": "pre", "state": "pre", "detail": "Scheduled"})
        finished = Event(**{**live.__dict__, "id": "post", "state": "post", "detail": "FT"})
        with patch("score.cli.discover", return_value=[upcoming, live, finished]), \
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
