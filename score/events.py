"""Sport-neutral event model and compact renderers."""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional


@dataclass(frozen=True)
class Team:
    id: str
    name: str
    abbreviation: str


@dataclass(frozen=True)
class Event:
    id: str
    sport: str
    start: datetime
    home: Team
    away: Team
    home_score: Optional[str]
    away_score: Optional[str]
    state: str
    detail: str


def parse_espn_event(data: dict, sport: str = "soccer") -> Event:
    competition = data["competitions"][0]
    competitors = {item["homeAway"]: item for item in competition["competitors"]}

    def team(side: str) -> Team:
        raw = competitors[side]["team"]
        return Team(str(raw["id"]), raw["displayName"], raw.get("abbreviation") or raw["displayName"][:3].upper())

    status = competition.get("status") or data.get("status", {})
    status_type = status.get("type", {})
    if sport == "baseball":
        detail = status_type.get("shortDetail") or status_type.get("description") or status.get("displayClock", "")
    else:
        detail = status.get("displayClock") or status_type.get("shortDetail") or status_type.get("description", "")
    start_date = data.get("date") or competition.get("date") or competition.get("startDate")
    if not start_date:
        raise ValueError("ESPN event has no start date")
    return Event(
        id=str(data["id"]),
        sport=sport,
        start=datetime.fromisoformat(start_date.replace("Z", "+00:00")),
        home=team("home"),
        away=team("away"),
        home_score=competitors["home"].get("score"),
        away_score=competitors["away"].get("score"),
        state=status_type.get("state", "pre"),
        detail=detail,
    )


def _minute(detail: str) -> str:
    return detail.replace("'", "′")


def format_event(event: Event, tz=None) -> str:
    if event.sport == "baseball":
        first, second = event.away, event.home
        first_score, second_score = event.away_score, event.home_score
        final = "Final"
    else:
        first, second = event.home, event.away
        first_score, second_score = event.home_score, event.away_score
        final = "FT"
    if event.state == "pre":
        start = event.start.astimezone(tz) if tz else event.start.astimezone()
        return f"{first.abbreviation} – {second.abbreviation} · {start:%H:%M}"
    phase = final if event.state == "post" else (_minute(event.detail) if event.sport == "soccer" else event.detail)
    return f"{first.abbreviation} {first_score}–{second_score} {second.abbreviation} · {phase}"


def find_events(events: Iterable[Event], query: str) -> list[Event]:
    terms = query.casefold().split()
    ranked = []
    for event in events:
        text = " ".join((event.home.name, event.home.abbreviation, event.away.name, event.away.abbreviation)).casefold()
        if all(term in text for term in terms):
            exact = sum(term in (event.home.abbreviation.casefold(), event.away.abbreviation.casefold()) for term in terms)
            ranked.append((exact, event))
    return [event for _, event in sorted(ranked, key=lambda item: (-item[0], item[1].start))]
