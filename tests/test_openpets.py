import json
import os
import socketserver
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from pathlib import Path

import score.cli as score_cli
from score.openpets import OpenPetsClient, OpenPetsError, resolve_openpets_socket
from score.cli import OpenPetsUnavailable, main, openpets_pin, openpets_unpin, pin_openpets_event, run_openpets_watch_loop
from score.events import Event, parse_espn_event
from tests.test_score import LIVE_EVENT


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        command = json.loads(self.rfile.readline())
        self.server.commands.append(command)
        thread_id = command.get("notification", {}).get("threadId") or "score-thread"
        self.wfile.write((json.dumps({"ok": True, "message": "updated", "threadId": thread_id}) + "\n").encode())


class OpenPetsCommandTests(unittest.TestCase):
    def test_missing_openpets_host_has_actionable_first_use_error(self):
        live = parse_espn_event(LIVE_EVENT)
        with patch("score.cli.discover_supported", return_value=[live]), \
             patch("score.cli.resolve_openpets_socket", return_value="/tmp/missing.sock"), \
             patch("score.cli.pin_openpets_event", side_effect=OpenPetsUnavailable()):
            with self.assertRaises(SystemExit) as raised:
                openpets_pin("arsenal")
        message = str(raised.exception)
        self.assertIn("OpenPets is not running", message)
        self.assertIn("https://github.com/alterhq/openpets/releases/latest", message)

    def test_openpets_command_routes_team_query(self):
        with patch("score.cli.openpets_pin") as pin:
            main(["openpets", "arsenal"])
        pin.assert_called_once_with("arsenal")

    def test_internal_openpets_watcher_preserves_exact_routing(self):
        arguments = [
            "1538630", "cricket", "24436", "/tmp/pet.sock", "score-thread",
            "owner-token", "/tmp/stop", "/tmp/start", "/tmp/ready", "1234",
        ]
        with patch("score.cli.openpets_watch") as watch:
            main(["_openpets_watch", *arguments])
        watch.assert_called_once_with(*arguments[:-1], 1234)

    def test_openpets_unpin_command_clears_global_pin(self):
        with patch("score.cli.openpets_unpin") as unpin, patch("score.cli.openpets_pin"):
            main(["openpets", "unpin"])
        unpin.assert_called_once_with()

    def test_unpin_never_signals_stale_pid_and_clears_exact_bubble(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "openpets.json"
            state_path.write_text(json.dumps({
                "pid": 4321,
                "socket_path": "/tmp/openpets-test.sock",
                "thread_id": "score-thread",
            }))

            class Pet:
                def clear(self, thread_id): self.cleared = thread_id

            pet = Pet()
            with patch("score.cli.OPENPETS_STATE_PATH", state_path), \
                 patch("score.cli.OpenPetsClient", return_value=pet), \
                 patch("score.cli.os.kill") as kill:
                openpets_unpin()

            kill.assert_not_called()
            self.assertEqual(pet.cleared, "score-thread")
            self.assertFalse(state_path.exists())

    def test_removed_owner_cannot_notify_after_fallback_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "private" / "openpets.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({"owner_token": "owner", "thread_id": "score-thread"}))

            class Pet:
                def __init__(self): self.operations = []
                def notify(self, title, thread_id=None): self.operations.append(("notify", thread_id))
                def clear(self, thread_id): self.operations.append(("clear", thread_id))

            pet = Pet()
            with patch("score.cli.OPENPETS_STATE_PATH", state_path):
                owned = score_cli._OwnedOpenPetsClient(pet, "owner")
                self.assertTrue(score_cli._cleanup_owned_state("owner", pet))
                with self.assertRaises(score_cli.OpenPetsOwnershipLost):
                    owned.notify("ARS 0–2 MCI · 73′", "score-thread")
            self.assertEqual(pet.operations, [("clear", "score-thread")])

    def test_old_watcher_cannot_clear_new_owners_bubble(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "private" / "openpets.json"
            state_path.parent.mkdir()
            state_path.write_text(json.dumps({"owner_token": "new-owner", "thread_id": "new-thread"}))

            class Pet:
                def __init__(self): self.cleared = []
                def clear(self, thread_id): self.cleared.append(thread_id)

            pet = Pet()
            with patch("score.cli.OPENPETS_STATE_PATH", state_path):
                cleaned = score_cli._cleanup_owned_state("old-owner", pet)
            self.assertFalse(cleaned)
            self.assertEqual(pet.cleared, [])
            self.assertTrue(state_path.exists())

    def test_readiness_failure_reaps_exact_child_and_removes_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "private" / "openpets.json"
            stop_path = state_path.parent / "openpets-owner.stop"
            start_path = state_path.parent / "openpets-owner.start"
            ready_path = state_path.parent / "openpets-owner.ready"
            for path in (start_path, ready_path):
                score_cli._write_private_file(path, "marker\n")
            process = Mock()
            process.wait.side_effect = [subprocess.TimeoutExpired("watcher", 1), None]
            with patch("score.cli.OPENPETS_STATE_PATH", state_path):
                score_cli._stop_spawned_watcher(process, stop_path, "owner")
            process.terminate.assert_called_once_with()
            self.assertEqual(process.wait.call_count, 2)
            self.assertFalse(stop_path.exists())
            self.assertFalse(start_path.exists())
            self.assertFalse(ready_path.exists())

    def test_unreaped_child_keeps_stop_marker_after_kill_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "private" / "openpets.json"
            stop_path = state_path.parent / "openpets-owner.stop"
            process = Mock()
            process.wait.side_effect = subprocess.TimeoutExpired("watcher", 1)
            with patch("score.cli.OPENPETS_STATE_PATH", state_path):
                with self.assertRaisesRegex(RuntimeError, "could not reap"):
                    score_cli._stop_spawned_watcher(process, stop_path, "owner")
            process.terminate.assert_called_once_with()
            process.kill.assert_called_once_with()
            self.assertTrue(stop_path.exists())

    def test_ready_publication_failure_still_removes_all_markers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "openpets.json"
            stop_path = root / "openpets-owner.stop"
            start_path = root / "openpets-owner.start"
            ready_path = root / "openpets-owner.ready"
            score_cli._write_private_file(stop_path, "stop\n")
            with patch("score.cli.OPENPETS_STATE_PATH", state_path), \
                 patch("score.cli._publish_private_marker", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    score_cli.openpets_watch(
                        "123", "soccer", "", "/tmp/pet.sock", "score-thread", "owner",
                        str(stop_path), str(start_path), str(ready_path), os.getppid(), interval=0,
                    )
            self.assertFalse(stop_path.exists())
            self.assertFalse(start_path.exists())
            self.assertFalse(ready_path.exists())

    def test_startup_timeout_claims_owned_state_and_cleans_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "openpets.json"
            stop_path = root / "owner.stop"
            start_path = root / "owner.start"
            ready_path = root / "owner.ready"
            state_path.write_text(json.dumps({
                "owner_token": "owner", "thread_id": "score-thread",
                "socket_path": "/tmp/pet.sock",
            }))

            class Pet:
                def __init__(self): self.cleared = []
                def clear(self, thread_id): self.cleared.append(thread_id)

            pet = Pet()
            with patch("score.cli.OPENPETS_STATE_PATH", state_path), \
                 patch("score.cli.OpenPetsClient", return_value=pet), \
                 patch.object(score_cli._FileStopEvent, "await_start", return_value="orphan"):
                score_cli.openpets_watch(
                    "123", "soccer", "", "/tmp/pet.sock", "score-thread",
                    "owner", str(stop_path), str(start_path), str(ready_path), os.getppid(), interval=0,
                )
            self.assertEqual(pet.cleared, ["score-thread"])
            self.assertFalse(state_path.exists())
            self.assertFalse(stop_path.exists())
            self.assertFalse(start_path.exists())

    def test_failed_start_marker_write_never_publishes_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "watcher.start"

            def fail_after_partial_write(path, content):
                path.write_text("partial")
                raise OSError("disk failure")

            with patch("score.cli._write_private_file", side_effect=fail_after_partial_write):
                with self.assertRaises(OSError):
                    score_cli._publish_private_marker(marker, "start\n")
            self.assertFalse(marker.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_failed_watcher_start_clears_initial_bubble_without_state(self):
        event = parse_espn_event(LIVE_EVENT)

        class Pet:
            def __init__(self): self.operations = []
            def notify(self, title, thread_id=None): self.operations.append("notify")
            def clear(self, thread_id): self.operations.append("clear")

        pet = Pet()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "private" / "openpets.json"
            with patch("score.cli.OPENPETS_STATE_PATH", state_path), \
                 patch("score.cli.OpenPetsClient", return_value=pet), \
                 patch("score.cli._openpets_unpin_locked"), \
                 patch("score.cli.subprocess.Popen", side_effect=OSError("cannot spawn")):
                with self.assertRaisesRegex(SystemExit, "could not start"):
                    pin_openpets_event(event, "/tmp/openpets-test.sock")
            self.assertEqual(pet.operations, [])
            self.assertFalse(state_path.exists())

    def test_pin_publishes_private_owned_state_before_releasing_watcher(self):
        event = parse_espn_event(LIVE_EVENT)

        class Pet:
            def notify(self, title, thread_id=None):
                self.seen = (title, thread_id)
                return thread_id

        class Process:
            def poll(self): return None

        pet = Pet()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "private" / "openpets.json"
            def mark_ready(_process, path, timeout=5.0):
                score_cli._write_private_file(path, "ready\n")
                return True

            with patch("score.cli.OPENPETS_STATE_PATH", state_path), \
                 patch("score.cli.OpenPetsClient", return_value=pet), \
                 patch("score.cli._openpets_unpin_locked"), \
                 patch("score.cli.subprocess.Popen", return_value=Process()) as popen, \
                 patch("score.cli._await_watcher_ready", side_effect=mark_ready), \
                 patch("score.cli.secrets.token_hex", return_value="owner-token"):
                pin_openpets_event(event, socket_path="/tmp/openpets-test.sock")

            state = json.loads(state_path.read_text())
            self.assertEqual(pet.seen, ("ARS 0–2 MCI · 73′", "score-owner-token"))
            self.assertEqual(
                popen.call_args.args[0][3:],
                [
                    "_openpets_watch", "123", "soccer", "", "/tmp/openpets-test.sock",
                    "score-owner-token", "owner-token", str(state_path.parent / "openpets-owner-token.stop"),
                    str(state_path.parent / "openpets-owner-token.start"),
                    str(state_path.parent / "openpets-owner-token.ready"), str(os.getpid()),
                ],
            )
            self.assertEqual(state["owner_token"], "owner-token")
            self.assertEqual(state["thread_id"], "score-owner-token")
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(state_path.parent.stat().st_mode & 0o777, 0o700)


class OpenPetsWatchTests(unittest.TestCase):
    def test_detached_watcher_waits_before_first_refresh(self):
        class Stop:
            def __init__(self): self.waits = []
            def wait(self, timeout): self.waits.append(timeout); return True
            def is_set(self): return False

        class Provider:
            def event(self, event_id): raise AssertionError("must not refresh immediately")

        stop = Stop()
        run_openpets_watch_loop(Provider(), "123", object(), "score-thread", stop, interval=10)
        self.assertEqual(stop.waits, [10])

    def test_manually_stopped_watcher_leaves_clear_to_unpin_command(self):
        class Provider:
            def event(self, event_id): raise AssertionError("stopped watcher must not fetch")

        class Pet:
            def __init__(self): self.cleared = []
            def clear(self, thread_id): self.cleared.append(thread_id)

        stop = threading.Event()
        stop.set()
        pet = Pet()
        run_openpets_watch_loop(Provider(), "123", pet, "score-thread", stop, interval=0, final_grace=0)
        self.assertEqual(pet.cleared, [])

    def test_provider_failures_keep_retrying_past_ten_refreshes(self):
        class Stop:
            def __init__(self): self.waits = 0
            def is_set(self): return False
            def wait(self, timeout):
                self.waits += 1
                return self.waits >= 12

        class Provider:
            def __init__(self): self.calls = 0
            def event(self, event_id):
                self.calls += 1
                raise TypeError("bad payload")

        provider = Provider()
        run_openpets_watch_loop(provider, "123", object(), "score-thread", Stop(), interval=10)
        self.assertEqual(provider.calls, 11)

    def test_structurally_malformed_provider_response_keeps_last_bubble(self):
        live = parse_espn_event(LIVE_EVENT)
        final = Event(**{**live.__dict__, "state": "post", "detail": "FT"})
        for failure in (TypeError("bad shape"), AttributeError("missing field")):
            with self.subTest(failure=type(failure).__name__):
                class Provider:
                    def __init__(self): self.step = 0
                    def event(self, event_id):
                        self.step += 1
                        if self.step == 2:
                            raise failure
                        return live if self.step == 1 else final

                class Pet:
                    def __init__(self): self.notifications = []
                    def notify(self, title, thread_id=None): self.notifications.append(title)

                pet = Pet()
                run_openpets_watch_loop(
                    Provider(), "123", pet, "score-thread", threading.Event(),
                    interval=0, final_grace=0,
                )
                self.assertEqual(pet.notifications, [
                    "ARS 0–2 MCI · 73′",
                    "ARS 0–2 MCI · FT",
                ])

    def test_transient_provider_failure_keeps_last_bubble_until_next_score(self):
        live = parse_espn_event(LIVE_EVENT)
        final = Event(**{**live.__dict__, "state": "post", "detail": "FT"})

        class Provider:
            def __init__(self): self.step = 0
            def event(self, event_id):
                self.step += 1
                if self.step == 2:
                    raise OSError("temporary provider failure")
                return live if self.step == 1 else final

        class Pet:
            def __init__(self): self.operations = []
            def notify(self, title, thread_id=None):
                self.operations.append(("notify", title))
                return thread_id
            def clear(self, thread_id): self.operations.append(("clear", thread_id))

        pet = Pet()
        run_openpets_watch_loop(
            Provider(), "123", pet, "score-thread", threading.Event(),
            interval=0, final_grace=0,
        )
        self.assertEqual(pet.operations, [
            ("notify", "ARS 0–2 MCI · 73′"),
            ("notify", "ARS 0–2 MCI · FT"),
        ])

    def test_stop_during_provider_fetch_never_notifies_after_unpin(self):
        live = parse_espn_event(LIVE_EVENT)
        stop = threading.Event()

        class Provider:
            def event(self, event_id):
                stop.set()
                return live

        class Pet:
            def __init__(self): self.notifications = []
            def notify(self, title, thread_id=None): self.notifications.append(title)

        pet = Pet()
        run_openpets_watch_loop(Provider(), "123", pet, "score-thread", stop, interval=0)
        self.assertEqual(pet.notifications, [])

    def test_live_score_updates_one_bubble_then_final_clears_it(self):
        live = parse_espn_event(LIVE_EVENT)
        final = Event(**{**live.__dict__, "state": "post", "detail": "FT"})

        class Provider:
            def __init__(self): self.events = iter((live, final))
            def event(self, event_id): return next(self.events)

        class Pet:
            def __init__(self): self.notifications, self.cleared = [], []
            def notify(self, title, thread_id=None):
                self.notifications.append((title, thread_id))
                return thread_id or "score-thread"
            def clear(self, thread_id): self.cleared.append(thread_id)

        pet = Pet()
        run_openpets_watch_loop(
            Provider(), "123", pet, "score-thread", threading.Event(),
            interval=0, final_grace=0,
        )
        self.assertEqual(pet.notifications, [
            ("ARS 0–2 MCI · 73′", "score-thread"),
            ("ARS 0–2 MCI · FT", "score-thread"),
        ])
        self.assertEqual(pet.cleared, [])


class OpenPetsClientTests(unittest.TestCase):
    def test_rejects_non_rfc_json_constants(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "openpets.sock")

                class ConstantHandler(socketserver.StreamRequestHandler):
                    def handle(self):
                        self.rfile.readline()
                        payload = '{"ok":true,"threadId":"thread","extra":' + constant + '}\n'
                        self.wfile.write(payload.encode())

                server = socketserver.UnixStreamServer(path, ConstantHandler)
                worker = threading.Thread(target=server.handle_request)
                worker.start()
                try:
                    with self.assertRaisesRegex(OpenPetsError, "invalid response"):
                        OpenPetsClient(path).notify("ARS 0–2 MCI · 73′")
                finally:
                    worker.join(timeout=2)
                    server.server_close()

    def test_rejects_wrong_pet_response_field_types(self):
        responses = (
            {"ok": "false", "threadId": "thread"},
            {"ok": True, "threadId": 123},
            {"ok": False, "message": []},
        )
        for response in responses:
            with self.subTest(response=response), tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "openpets.sock")

                class ResponseHandler(socketserver.StreamRequestHandler):
                    def handle(self):
                        self.rfile.readline()
                        self.wfile.write((json.dumps(response) + "\n").encode())

                server = socketserver.UnixStreamServer(path, ResponseHandler)
                worker = threading.Thread(target=server.handle_request)
                worker.start()
                try:
                    with self.assertRaisesRegex(OpenPetsError, "invalid response"):
                        OpenPetsClient(path).notify("ARS 0–2 MCI · 73′")
                finally:
                    worker.join(timeout=2)
                    server.server_close()

    def test_rejects_non_object_response_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "openpets.sock")

            class ListHandler(socketserver.StreamRequestHandler):
                def handle(self):
                    self.rfile.readline()
                    self.wfile.write(b"[]\n")

            server = socketserver.UnixStreamServer(path, ListHandler)
            worker = threading.Thread(target=server.handle_request)
            worker.start()
            try:
                with self.assertRaisesRegex(Exception, "invalid response"):
                    OpenPetsClient(path).notify("ARS 0–2 MCI · 73′")
            finally:
                worker.join(timeout=2)
                server.server_close()

    def test_resolves_custom_socket_from_openpets_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config_home = Path(directory)
            config_dir = config_home / "openpets"
            config_dir.mkdir()
            (config_dir / "config.json").write_text(json.dumps({"socketPath": "/tmp/custom-openpets.sock"}))
            self.assertEqual(resolve_openpets_socket(config_home=config_home, uid=501), "/tmp/custom-openpets.sock")

    def test_notify_updates_one_thread_and_clear_removes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "openpets.sock")
            server = socketserver.UnixStreamServer(path, _Handler)
            server.commands = []
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                client = OpenPetsClient(path)
                thread_id = client.notify("ARS 0–1 MCI · 32′")
                self.assertEqual(thread_id, "score-thread")
                self.assertEqual(client.notify("ARS 0–2 MCI · 73′", thread_id), thread_id)
                client.clear(thread_id)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(1)

        self.assertEqual(server.commands, [
            {
                "type": "notify",
                "notification": {
                    "title": "ARS 0–1 MCI · 32′",
                    "status": "message",
                },
            },
            {
                "type": "notify",
                "notification": {
                    "title": "ARS 0–2 MCI · 73′",
                    "status": "message",
                    "threadId": "score-thread",
                },
            },
            {"type": "clearMessage", "threadId": "score-thread"},
        ])


if __name__ == "__main__":
    unittest.main()
