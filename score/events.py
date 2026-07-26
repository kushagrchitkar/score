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
    league: str = ""
    active_side: str = ""


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
    elif sport == "cricket":
        detail = status_type.get("description") or status.get("session") or status_type.get("shortDetail", "")
    else:
        detail = status.get("displayClock") or status_type.get("shortDetail") or status_type.get("description", "")
    active_side = next((side for side, item in competitors.items() if any(line.get("isBatting") for line in item.get("linescores", []))), "")
    batting_team_id = str(status.get("battingTeamId") or "")
    if not active_side and batting_team_id:
        active_side = next(
            (
                side
                for side, item in competitors.items()
                if str(item.get("id") or item.get("team", {}).get("id") or "") == batting_team_id
            ),
            "",
        )
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
        league=str(data.get("leagueId") or data.get("season", {}).get("type") or "") if sport == "cricket" else "",
        active_side=active_side,
    )


def _minute(detail: str) -> str:
    return detail.replace("'", "′")


def format_event(event: Event, tz=None) -> str:
    if event.sport == "cricket":
        if event.state == "pre":
            start = event.start.astimezone(tz) if tz else event.start.astimezone()
            return f"{event.home.abbreviation} – {event.away.abbreviation} · {start:%H:%M}"
        first_side = event.active_side or ("away" if event.away_score and not event.home_score else "home")
        first = event.home if first_side == "home" else event.away
        second = event.away if first_side == "home" else event.home
        first_score = event.home_score if first_side == "home" else event.away_score
        second_score = event.away_score if first_side == "home" else event.home_score
        first_text = f"{first.abbreviation} {first_score}" if first_score else first.abbreviation
        second_text = f"{second.abbreviation} {second_score}" if second_score else second.abbreviation
        phase = "Result" if event.state == "post" else event.detail
        return " · ".join(part for part in (first_text, second_text, phase) if part)
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
