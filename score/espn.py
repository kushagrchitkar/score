"""ESPN scoreboard adapter.

These endpoints are public-facing but undocumented; all provider-specific behavior
is kept here so it can be replaced without changing the CLI or title watcher.
"""

import json
import urllib.parse
import urllib.request

from .events import Team, parse_espn_event


BASE = "https://site.api.espn.com/apis/site/v2/sports"
SEARCH = "https://site.web.api.espn.com/apis/common/v3/search"


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "score-cli/0.1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


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
