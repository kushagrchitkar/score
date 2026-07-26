"""Command-line interface and per-terminal title watcher."""

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .espn import ESPNClient
from .events import find_events, format_event
from .storage import FollowStore
from .title import osc_title

STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "score"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "score"


def render_once(client, event_id: str, stream) -> str:
    title = format_event(client.event(event_id))
    stream.write(osc_title(title))
    stream.flush()
    return title


def _dates(days=7):
    today = datetime.now(timezone.utc).date()
    return [(today + timedelta(days=offset)).strftime("%Y%m%d") for offset in range(days + 1)]


def discover(client, days=7):
    dates = _dates(days)
    with ThreadPoolExecutor(max_workers=4) as pool:
        groups = list(pool.map(client.events, dates))
    unique = {}
    for event in (event for group in groups for event in group):
        unique[event.id] = event
    return sorted(unique.values(), key=lambda event: event.start)


def _choose(items, label):
    if not items:
        raise SystemExit(f"No {label} found.")
    if len(items) == 1:
        return items[0]
    if not sys.stdin.isatty():
        raise SystemExit(f"Multiple {label} found; run in an interactive terminal.")
    for index, item in enumerate(items, 1):
        text = format_event(item) if hasattr(item, "home") else f"{item.name} ({item.abbreviation})"
        print(f"{index:>2}. {text}")
    while True:
        try:
            selected = int(input("Pin: "))
            return items[selected - 1]
        except (ValueError, IndexError):
            print("Choose a listed number.", file=sys.stderr)


def _tty():
    if not sys.stdout.isatty():
        raise SystemExit("score pin must be run inside a terminal such as Ghostty.")
    return os.ttyname(sys.stdout.fileno())


def _state_path(tty: str) -> Path:
    key = hashlib.sha256(tty.encode()).hexdigest()[:16]
    return STATE_HOME / f"{key}.json"


def _write_state(tty, data):
    STATE_HOME.mkdir(parents=True, exist_ok=True)
    _state_path(tty).write_text(json.dumps(data, indent=2) + "\n")


def _restore_title(stream, title):
    stream.write(osc_title(title))
    stream.flush()


def run_watch_loop(client, event_id: str, stream, restore: str, interval: int, stop_event) -> None:
    failures = 0
    try:
        while not stop_event.is_set():
            try:
                event = client.event(event_id)
                _restore_title(stream, format_event(event))
                failures = 0
                if event.state == "post":
                    break
            except (OSError, LookupError, ValueError):
                failures += 1
                if failures >= 10:
                    break
            if stop_event.wait(interval):
                break
    finally:
        _restore_title(stream, restore)


def watch(event_id: str, tty: str, restore: str, interval: int = 20):
    state_path = _state_path(tty)
    stream = open(tty, "w", buffering=1)
    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        run_watch_loop(ESPNClient(), event_id, stream, restore, interval, stop_event)
    finally:
        state_path.unlink(missing_ok=True)
        stream.close()


def pin_event(event, once=False):
    if once:
        print(format_event(event))
        return
    tty = _tty()
    unpin(quiet=True)
    title = format_event(event)
    sys.stdout.write(osc_title(title))
    sys.stdout.flush()
    restore = Path.cwd().name or "Ghostty"
    process = subprocess.Popen(
        [sys.executable, "-m", "score.cli", "_watch", event.id, tty, restore],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _write_state(tty, {"pid": process.pid, "event_id": event.id, "title": title, "restore": restore})
    print(f"Pinned {title}")


def pin(query: str, once=False):
    client = ESPNClient()
    live = [event for event in discover(client) if event.state == "in"]
    event = _choose(find_events(live, query), "matching live events")
    pin_event(event, once)


def unpin(quiet=False):
    try:
        tty = _tty()
    except SystemExit:
        if quiet:
            return
        raise
    path = _state_path(tty)
    if not path.exists():
        if not quiet:
            print("Nothing is pinned in this terminal.")
        return
    state = json.loads(path.read_text())
    try:
        os.kill(int(state["pid"]), signal.SIGTERM)
    except ProcessLookupError:
        pass
    _restore_title(sys.stdout, state.get("restore", Path.cwd().name or "Ghostty"))
    path.unlink(missing_ok=True)
    if not quiet:
        print("Unpinned.")


def list_events(pin_interactively=False):
    events = [event for event in discover(ESPNClient(), days=1) if event.state == "in"]
    if not events:
        print("No live football matches found.")
        return
    if pin_interactively and sys.stdin.isatty():
        pin_event(_choose(events, "live events"))
        return
    for event in events:
        print(format_event(event))


def follow(query: str):
    team = _choose(ESPNClient().search_teams(query), "matching teams")
    FollowStore(CONFIG_HOME / "follows.json").add(team, "soccer")
    print(f"Following {team.name} ({team.abbreviation}).")


def following():
    records = FollowStore(CONFIG_HOME / "follows.json").list()
    if not records:
        print("No followed teams.")
    for item in records:
        print(f"{item['abbreviation']}  {item['name']}")


def parser():
    result = argparse.ArgumentParser(prog="score", description="Pin live sports scores to your terminal title.")
    sub = result.add_subparsers(dest="command")
    pin_parser = sub.add_parser("pin", help="pin a match to this terminal")
    pin_parser.add_argument("query", nargs="+")
    pin_parser.add_argument("--once", action="store_true", help="print the match once without changing the title")
    sub.add_parser("unpin", help="stop watching this terminal")
    follow_parser = sub.add_parser("follow", help="follow a team")
    follow_parser.add_argument("query", nargs="+")
    sub.add_parser("following", help="list followed teams")
    watch_parser = sub.add_parser("_watch")
    watch_parser.add_argument("event_id")
    watch_parser.add_argument("tty")
    watch_parser.add_argument("restore")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command is None:
        list_events(pin_interactively=True)
    elif args.command == "pin":
        pin(" ".join(args.query), args.once)
    elif args.command == "unpin":
        unpin()
    elif args.command == "follow":
        follow(" ".join(args.query))
    elif args.command == "following":
        following()
    elif args.command == "_watch":
        watch(args.event_id, args.tty, args.restore)


if __name__ == "__main__":
    main()
