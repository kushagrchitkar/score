"""ESPN scoreboard adapter.

These endpoints are public-facing but undocumented; all provider-specific behavior
is kept here so it can be replaced without changing the CLI or title watcher.
"""

import json
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

from .events import Team, parse_espn_event


BASE = "https://site.api.espn.com/apis/site/v2/sports"
CRICKET_PANEL = "https://site.web.api.espn.com/apis/site/v2/sports/cricket/scorepanel"
SEARCH = "https://site.web.api.espn.com/apis/common/v3/search"


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "score/0.5"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
        except URLError:
            if attempt == 2:
                raise
        time.sleep(0.25 * (2 ** attempt))
    raise RuntimeError("unreachable")


class ESPNClient:
    def __init__(self, sport="soccer", league="all", transport=None):
        self.sport = sport
        self.league = league
        self.transport = transport or _get_json

    @property
    def base_url(self):
        return f"{BASE}/{self.sport}/{self.league}"

    def events(self, date: str) -> list:
        url = f"{self.base_url}/scoreboard?{urllib.parse.urlencode({'dates': date, 'limit': 1000})}"
        return [parse_espn_event(item, sport=self.sport) for item in self.transport(url).get("events", [])]

    def event(self, event_id: str):
        url = f"{self.base_url}/summary?{urllib.parse.urlencode({'event': event_id})}"
        data = self.transport(url)
        if not data.get("header"):
            raise LookupError(f"Event {event_id} was not found")
        return parse_espn_event(data["header"], sport=self.sport)

    def search_teams(self, query: str) -> list[Team]:
        params = {"query": query, "limit": 20, "type": "team", "sport": self.sport}
        data = self.transport(f"{SEARCH}?{urllib.parse.urlencode(params)}")
        teams = []
        for item in data.get("items", []):
            if item.get("type") == "team" and item.get("sport") == self.sport:
                teams.append(Team(str(item["id"]), item["displayName"], item.get("abbreviation") or item["displayName"][:3].upper()))
        return teams


class ESPNCricketClient(ESPNClient):
    """Discover cricket across ESPN's dynamically changing series IDs."""

    current_snapshot_only = True

    def __init__(self, transport=None):
        super().__init__(sport="cricket", league="", transport=transport)

    def events(self, date: str) -> list:
        params = {
            "dates": date,
            "contentorigin": "espn",
            "lang": "en",
            "region": "us",
            "tz": "UTC",
        }
        data = self.transport(f"{CRICKET_PANEL}?{urllib.parse.urlencode(params)}")
        events = []
        for group in data.get("scores", []):
            league = str((group.get("leagues") or [{}])[0].get("id", ""))
            for raw in group.get("events", []):
                enriched = {**raw, "leagueId": raw.get("leagueId") or league}
                events.append(parse_espn_event(enriched, sport="cricket"))
        return events
