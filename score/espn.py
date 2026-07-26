"""ESPN scoreboard adapter.

These endpoints are public-facing but undocumented; all provider-specific behavior
is kept here so it can be replaced without changing the CLI or title watcher.
"""

import json
import urllib.parse
import urllib.request

from .events import Team, parse_espn_event


SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard"
SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary"
SEARCH = "https://site.web.api.espn.com/apis/common/v3/search"


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "score-cli/0.1"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


class ESPNClient:
    def __init__(self, transport=None):
        self.transport = transport or _get_json

    def events(self, date: str) -> list:
        url = f"{SCOREBOARD}?{urllib.parse.urlencode({'dates': date, 'limit': 1000})}"
        return [parse_espn_event(item) for item in self.transport(url).get("events", [])]

    def event(self, event_id: str):
        url = f"{SUMMARY}?{urllib.parse.urlencode({'event': event_id})}"
        data = self.transport(url)
        if not data.get("header"):
            raise LookupError(f"Event {event_id} was not found")
        return parse_espn_event(data["header"])

    def search_teams(self, query: str) -> list[Team]:
        params = {"query": query, "limit": 20, "type": "team", "sport": "soccer"}
        data = self.transport(f"{SEARCH}?{urllib.parse.urlencode(params)}")
        teams = []
        for item in data.get("items", []):
            if item.get("type") == "team" and item.get("sport") == "soccer":
                teams.append(Team(str(item["id"]), item["displayName"], item.get("abbreviation") or item["displayName"][:3].upper()))
        return teams
