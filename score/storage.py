"""Persistent user preferences."""

import json
from pathlib import Path

from .events import Team


class FollowStore:
    def __init__(self, path: Path):
        self.path = path

    def list(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text())

    def add(self, team: Team, sport: str) -> None:
        records = self.list()
        record = {
            "provider": "espn", "sport": sport, "team_id": team.id,
            "name": team.name, "abbreviation": team.abbreviation,
        }
        if not any(item["provider"] == "espn" and item["sport"] == sport and item["team_id"] == team.id for item in records):
            records.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(records, indent=2) + "\n")
