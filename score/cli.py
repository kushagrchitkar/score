"""Command-line interface and per-terminal title watcher."""

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .espn import ESPNClient, ESPNCricketClient
from .events import find_events, format_event
from .openpets import OpenPetsClient, OpenPetsError, resolve_openpets_socket
from .storage import FollowStore
from .title import osc_title

STATE_HOME = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "score"
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "score"
OPENPETS_STATE_PATH = STATE_HOME / "openpets.json"
DEMO_TITLES = (
    "ARS 0–0 MCI · 72′",
    "ARS 0–1 MCI · 73′",
    "ARS 1–1 MCI · 74′",
    "ARS 1–2 MCI · 75′",
    "ARS 2–2 MCI · FT",
)


def render_once(client, event_id: str, stream) -> str:
    title = format_event(client.event(event_id))
    stream.write(osc_title(title))
    stream.flush()
    return title


def _dates(days=7, today=None):
    today = today or datetime.now(timezone.utc).date()
    return [(today + timedelta(days=offset)).strftime("%Y%m%d") for offset in range(-1, days + 1)]


def discover(client, days=7):
    dates = _dates(days)
    if getattr(client, "current_snapshot_only", False):
        dates = [datetime.now(timezone.utc).strftime("%Y%m%d")]
    with ThreadPoolExecutor(max_workers=min(8, len(dates))) as pool:
        groups = list(pool.map(client.events, dates))
    unique = {}
    for event in (event for group in groups for event in group):
        unique[event.id] = event
    return sorted(unique.values(), key=lambda event: event.start)


def supported_clients():
    return (
        ESPNClient(sport="soccer", league="all"),
        ESPNClient(sport="baseball", league="mlb"),
        ESPNCricketClient(),
    )


def client_for_sport(sport: str, league: str = ""):
    if sport == "soccer":
        return ESPNClient(sport="soccer", league="all")
    if sport == "baseball":
        return ESPNClient(sport="baseball", league="mlb")
    if sport == "cricket" and league:
        return ESPNClient(sport="cricket", league=league)
    raise ValueError(f"Unsupported sport or missing league: {sport}")


def discover_supported(days=7):
    unique = {}
    failures = []
    succeeded = 0
    for client in supported_clients():
        try:
            events = discover(client, days)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            failures.append(exc)
            continue
        succeeded += 1
        for event in events:
            unique[(event.sport, event.id)] = event
    if not succeeded and failures:
        raise failures[-1]
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


def watch(event_id: str, sport: str, league: str, tty: str, restore: str, interval: int = 10):
    state_path = _state_path(tty)
    stream = open(tty, "w", buffering=1)
    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        run_watch_loop(client_for_sport(sport, league), event_id, stream, restore, interval, stop_event)
    finally:
        state_path.unlink(missing_ok=True)
        stream.close()


class OpenPetsUnavailable(RuntimeError):
    """Raised only when the local OpenPets host cannot accept the initial bubble."""


@contextmanager
def _openpets_lock(name: str):
    directory = OPENPETS_STATE_PATH.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / f"openpets-{name}.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_openpets_state_unlocked():
    try:
        state = json.loads(OPENPETS_STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def _read_openpets_state():
    with _openpets_lock("state"):
        return _read_openpets_state_unlocked()


def _write_private_file(path: Path, content: str = "") -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(content)


def _publish_private_marker(path: Path, content: str) -> None:
    """Publish a marker atomically so readers never observe a partial write."""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        _write_private_file(temporary, content)
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _write_openpets_state_unlocked(data):
    temporary = OPENPETS_STATE_PATH.with_name(
        f"{OPENPETS_STATE_PATH.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        _write_private_file(temporary, json.dumps(data, indent=2) + "\n")
        temporary.replace(OPENPETS_STATE_PATH)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _state_is_owned(token: str) -> bool:
    state = _read_openpets_state()
    return bool(state and state.get("owner_token") == token)


class OpenPetsOwnershipLost(RuntimeError):
    """Raised when a superseded watcher tries to update a bubble it no longer owns."""


class _OwnedOpenPetsClient:
    def __init__(self, client: OpenPetsClient, token: str):
        self.client = client
        self.token = token

    def notify(self, title: str, thread_id=None):
        with _openpets_lock("state"):
            state = _read_openpets_state_unlocked()
            if not state or state.get("owner_token") != self.token:
                raise OpenPetsOwnershipLost
            return self.client.notify(title, thread_id)


def _cleanup_owned_state(token: str, pet: OpenPetsClient) -> bool:
    """Atomically clear and remove state only while this watcher still owns it."""
    with _openpets_lock("state"):
        state = _read_openpets_state_unlocked()
        if not state or state.get("owner_token") != token:
            return False
        try:
            pet.clear(str(state["thread_id"]))
        except (KeyError, OSError, OpenPetsError):
            pass
        try:
            OPENPETS_STATE_PATH.unlink(missing_ok=True)
        except OSError:
            try:
                OPENPETS_STATE_PATH.unlink(missing_ok=True)
            except OSError:
                return False
        return True


def _stop_path(token: str) -> Path:
    return OPENPETS_STATE_PATH.parent / f"openpets-{token}.stop"


def _start_path(token: str) -> Path:
    return OPENPETS_STATE_PATH.parent / f"openpets-{token}.start"


def _ready_path(token: str) -> Path:
    return OPENPETS_STATE_PATH.parent / f"openpets-{token}.ready"


def _clear_owned_markers(token: str) -> None:
    _stop_path(token).unlink(missing_ok=True)
    _start_path(token).unlink(missing_ok=True)
    _ready_path(token).unlink(missing_ok=True)


def _clear_stale_state(state) -> None:
    removed = False
    with _openpets_lock("state"):
        current = _read_openpets_state_unlocked()
        if current != state:
            return
        try:
            OpenPetsClient(str(state["socket_path"])).clear(str(state["thread_id"]))
        except (KeyError, OSError, OpenPetsError):
            pass
        OPENPETS_STATE_PATH.unlink(missing_ok=True)
        removed = True
    if removed:
        token = state.get("owner_token")
        if isinstance(token, str) and token:
            _clear_owned_markers(token)


def _openpets_unpin_locked(quiet=False):
    state = _read_openpets_state()
    if not state:
        with _openpets_lock("state"):
            OPENPETS_STATE_PATH.unlink(missing_ok=True)
        if not quiet:
            print("Nothing is pinned to OpenPets.")
        return

    token = state.get("owner_token")
    if isinstance(token, str) and token:
        _write_private_file(_stop_path(token), "stop\n")
        deadline = time.monotonic() + 12
        while _state_is_owned(token) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not _state_is_owned(token):
            if not quiet:
                print("Unpinned from OpenPets.")
            return

    _clear_stale_state(state)
    if not quiet:
        print("Unpinned from OpenPets.")


def _await_watcher_ready(process, ready_path: Path, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_path.exists():
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.02)
    return ready_path.exists()


def _stop_spawned_watcher(process, stop_path: Path, token: str) -> None:
    """Stop and reap the exact child created by this pin attempt."""
    reaped = False
    try:
        _publish_private_marker(stop_path, "stop\n")
    except OSError:
        pass
    try:
        process.wait(timeout=1)
        reaped = True
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
            reaped = True
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
                reaped = True
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("score could not reap its background watcher") from exc
    finally:
        if reaped:
            _clear_owned_markers(token)


def pin_openpets_event(event, socket_path: str):
    with _openpets_lock("command"):
        _openpets_unpin_locked(quiet=True)
        title = format_event(event)
        token = secrets.token_hex(16)
        thread_id = f"score-{token}"
        stop_path = _stop_path(token)
        start_path = _start_path(token)
        ready_path = _ready_path(token)
        _clear_owned_markers(token)
        try:
            process = subprocess.Popen(
                [
                    sys.executable, "-m", "score.cli", "_openpets_watch",
                    event.id, event.sport, event.league, socket_path, thread_id,
                    token, str(stop_path), str(start_path), str(ready_path), str(os.getpid()),
                ],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise SystemExit("OpenPets connected, but score could not start its background watcher.") from exc
        if not _await_watcher_ready(process, ready_path):
            _stop_spawned_watcher(process, stop_path, token)
            raise SystemExit("OpenPets connected, but score could not start its background watcher.")

        pet = OpenPetsClient(socket_path)
        failure = None
        failure_kind = "watcher"
        with _openpets_lock("state"):
            try:
                if process.poll() is not None or not ready_path.exists():
                    raise OSError("background watcher exited during startup")
                _write_openpets_state_unlocked({
                    "owner_token": token,
                    "event_id": event.id,
                    "sport": event.sport,
                    "league": event.league,
                    "socket_path": socket_path,
                    "thread_id": thread_id,
                    "title": title,
                })
                failure_kind = "host"
                returned_thread = pet.notify(title, thread_id)
                if returned_thread != thread_id:
                    raise OpenPetsError("OpenPets returned an invalid thread ID")
                failure_kind = "watcher"
                _publish_private_marker(start_path, "start\n")
            except (OSError, OpenPetsError) as exc:
                failure = exc
                try:
                    _publish_private_marker(stop_path, "stop\n")
                except OSError:
                    pass
                # The exact child owns state cleanup; the parent reclaims it after reaping only if needed.
        if failure is not None:
            _stop_spawned_watcher(process, stop_path, token)
            _cleanup_owned_state(token, pet)
            if failure_kind == "host":
                raise OpenPetsUnavailable from failure
            raise SystemExit("OpenPets connected, but score could not start its background watcher.")
        print(f"Pinned to OpenPets: {title}")


def openpets_unpin(quiet=False):
    with _openpets_lock("command"):
        _openpets_unpin_locked(quiet)


def openpets_pin(query: str):
    live = [event for event in discover_supported() if event.state == "in"]
    matches = find_events(live, query) if query else live
    event = _choose(matches, "matching live events")
    try:
        pin_openpets_event(event, resolve_openpets_socket())
    except OpenPetsUnavailable as exc:
        raise SystemExit(
            "OpenPets is not running.\n\n"
            "1. Install it: https://github.com/alterhq/openpets/releases/latest\n"
            "2. Open OpenPets and wake your pet.\n"
            "3. Run this command again."
        ) from exc


class _FileStopEvent:
    def __init__(self, stop_path: Path, start_path: Path):
        self.stop_path = stop_path
        self.start_path = start_path
        self.local = threading.Event()

    def set(self):
        self.local.set()

    def is_set(self):
        return self.local.is_set() or self.stop_path.exists()

    def wait(self, timeout):
        deadline = time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.local.wait(min(0.05, remaining))
        return True

    def await_start(self, parent_pid: int):
        while True:
            if self.is_set():
                return "stop"
            if self.start_path.exists():
                return "start"
            if os.getppid() != parent_pid:
                return "orphan"
            self.local.wait(0.05)


def openpets_watch(
    event_id: str, sport: str, league: str, socket_path: str, thread_id: str,
    token: str, stop_path: str, start_path: str, ready_path: str, parent_pid: int,
    interval: int = 10, final_grace: int = 600,
):
    stop_event = _FileStopEvent(Path(stop_path), Path(start_path))

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    pet = OpenPetsClient(socket_path)
    owned_pet = _OwnedOpenPetsClient(pet, token)
    try:
        _publish_private_marker(Path(ready_path), "ready\n")
        gate = stop_event.await_start(parent_pid)
        if gate == "start":
            run_openpets_watch_loop(
                client_for_sport(sport, league), event_id, owned_pet,
                thread_id, stop_event, interval, final_grace,
            )
    finally:
        try:
            _cleanup_owned_state(token, pet)
        finally:
            _clear_owned_markers(token)


def run_openpets_watch_loop(
    client, event_id: str, pet: OpenPetsClient, thread_id: str, stop_event,
    interval: int = 10, final_grace: int = 600,
) -> None:
    if stop_event.wait(interval):
        return
    while not stop_event.is_set():
        try:
            event = client.event(event_id)
            if stop_event.is_set():
                break
            pet.notify(format_event(event), thread_id)
        except OpenPetsOwnershipLost:
            break
        except (OSError, LookupError, ValueError, TypeError, AttributeError, OpenPetsError):
            pass
        else:
            if event.state == "post":
                stop_event.wait(final_grace)
                break
        if stop_event.wait(interval):
            break

def run_demo_loop(stream, restore: str, interval: int, stop_event, titles=DEMO_TITLES) -> None:
    try:
        for title in titles:
            if stop_event.is_set():
                break
            _restore_title(stream, title)
            if stop_event.wait(interval):
                break
    finally:
        _restore_title(stream, restore)


def demo_watch(tty: str, restore: str, interval: int = 2) -> None:
    state_path = _state_path(tty)
    stream = open(tty, "w", buffering=1)
    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        run_demo_loop(stream, restore, interval, stop_event)
    finally:
        state_path.unlink(missing_ok=True)
        stream.close()


def demo() -> None:
    tty = _tty()
    unpin(quiet=True)
    restore = Path.cwd().name or "Ghostty"
    process = subprocess.Popen(
        [sys.executable, "-m", "score.cli", "_demo_watch", tty, restore],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _write_state(tty, {"pid": process.pid, "event_id": "demo", "title": DEMO_TITLES[0], "restore": restore})
    print("Demo pinned. Watch the Ghostty title; use `score unpin` to stop it.")


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
        [sys.executable, "-m", "score.cli", "_watch", event.id, event.sport, event.league, tty, restore],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _write_state(tty, {"pid": process.pid, "event_id": event.id, "sport": event.sport, "league": event.league, "title": title, "restore": restore})
    print(f"Pinned {title}")


def pin(query: str, once=False):
    live = [event for event in discover_supported() if event.state == "in"]
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
    events = [event for event in discover_supported(days=1) if event.state == "in"]
    if not events:
        print("No live football, MLB, or cricket events found.")
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
    sub.add_parser("demo", help="simulate a match in this terminal title")
    openpets_parser = sub.add_parser("openpets", help="pin a live score to an OpenPets bubble")
    openpets_parser.add_argument("query", nargs="*", help="team names, or `unpin`")
    follow_parser = sub.add_parser("follow", help="follow a team")
    follow_parser.add_argument("query", nargs="+")
    sub.add_parser("following", help="list followed teams")
    watch_parser = sub.add_parser("_watch")
    watch_parser.add_argument("event_id")
    watch_parser.add_argument("sport")
    watch_parser.add_argument("league")
    watch_parser.add_argument("tty")
    watch_parser.add_argument("restore")
    demo_watch_parser = sub.add_parser("_demo_watch")
    demo_watch_parser.add_argument("tty")
    demo_watch_parser.add_argument("restore")
    openpets_watch_parser = sub.add_parser("_openpets_watch")
    openpets_watch_parser.add_argument("event_id")
    openpets_watch_parser.add_argument("sport")
    openpets_watch_parser.add_argument("league")
    openpets_watch_parser.add_argument("socket_path")
    openpets_watch_parser.add_argument("thread_id")
    openpets_watch_parser.add_argument("token")
    openpets_watch_parser.add_argument("stop_path")
    openpets_watch_parser.add_argument("start_path")
    openpets_watch_parser.add_argument("ready_path")
    openpets_watch_parser.add_argument("parent_pid", type=int)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command is None:
        list_events(pin_interactively=True)
    elif args.command == "pin":
        pin(" ".join(args.query), args.once)
    elif args.command == "unpin":
        unpin()
    elif args.command == "demo":
        demo()
    elif args.command == "openpets":
        query = " ".join(args.query)
        if query == "unpin":
            openpets_unpin()
        else:
            openpets_pin(query)
    elif args.command == "follow":
        follow(" ".join(args.query))
    elif args.command == "following":
        following()
    elif args.command == "_watch":
        watch(args.event_id, args.sport, args.league, args.tty, args.restore)
    elif args.command == "_demo_watch":
        demo_watch(args.tty, args.restore)
    elif args.command == "_openpets_watch":
        openpets_watch(
            args.event_id, args.sport, args.league, args.socket_path, args.thread_id,
            args.token, args.stop_path, args.start_path, args.ready_path, args.parent_pid,
        )


if __name__ == "__main__":
    main()
