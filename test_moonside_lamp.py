import argparse
import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

import moonside_lamp as ml
from moonside_lamp import Decider, Registry, Session, aggregate, commands_for, valid_command


SESSIONS = ml.config_dir() / "sessions"


def session_files():
    """Session files whose process runs, by pid alone."""
    found = []
    for path in SESSIONS.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if ml.process_alive(data["pid"]):
                found.append(data)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found


def live_sessions():
    return [d for d in session_files() if ml.process_alive(d["pid"], d.get("procStart"))]


needs_live_claude = unittest.skipUnless(ml.claude_exe() and live_sessions(), "needs Claude Code running on this machine")

# Every command block and catalog row on developer.moonside.design.
PROTOCOL_EXAMPLES = """
LEDON
LEDOFF
BRIGH80
COLOR255120000
THEME.THEME5.200,20,150,0,60,255,
MODEPIXEL
PIXEL,42,255000128,
BACKLED1,0,050050050,
BACKLED2,0,000050120,
BTN1
BTN2
BTN3
BTN4
BTN5
THEME.RAINBOW1.20,
THEME.RAINBOW2.20,
THEME.RAINBOW3.0,
THEME.FIRE1.0,
THEME.FIRE2.0,0,0,255,0,0,0,0,255,255,255,255,
THEME.THEME1.255,80,0,0,120,255,
THEME.THEME2.255,80,0,0,120,255,
THEME.THEME3.0,0,200,0,100,200,100,0,200,200,0,0,200,0,100,120,200,0,
THEME.THEME4.255,80,0,0,120,255,
THEME.COLORDROP1.255,0,80,0,120,255,
THEME.GRADIENT1.255,80,0,0,120,255,
THEME.GRADIENT2.255,80,0,0,120,255,255,255,255,
THEME.GRADIENT3.255,80,0,0,120,255,
THEME.TWINKLE1.255,255,255,0,120,255,
THEME.PULSING1.255,80,0,0,120,255,
THEME.PALETTE1.0,
THEME.PALETTE2.0,0,200,0,100,200,100,0,200,200,0,0,200,0,100,120,200,0,
THEME.BEAT1.255,0,0,0,255,0,0,0,255,
THEME.BEAT2.255,80,0,0,120,255,
THEME.BEAT3.255,0,0,0,255,0,0,0,255,
THEME.WAVE1.255,80,0,0,120,255,
THEME.LAVA1.255,60,0,180,0,0,255,180,40,
THEME.M1.255,140,0,255,0,128,
THEME.M2.255,0,0,0,255,0,0,0,255,
THEME.M3.255,140,0,255,0,128,
THEME.M4.255,140,0,255,0,128,
THEME.M5.255,140,0,255,0,128,
THEME.M6.255,140,0,255,0,128,
THEME.M7.255,140,0,255,0,128,
THEME.M8.255,0,0,0,255,0,0,0,255,
THEME.M9.255,140,0,255,0,128,
THEME.M10.255,0,0,0,255,0,0,0,255,
THEME.M11.255,0,0,0,255,0,0,0,255,
THEME.M12.255,140,0,255,0,128,
""".split()


def session(status, waiting_for=None):
    return Session(1, status, waiting_for, "s")


class Commands(unittest.TestCase):
    def test_every_example_on_the_protocol_page_passes(self):
        self.assertEqual(len(PROTOCOL_EXAMPLES), 48)
        for cmd in PROTOCOL_EXAMPLES:
            self.assertTrue(valid_command(cmd), cmd)

    def test_malformed_commands_are_refused(self):
        for cmd in ("", "LEDOFFX", "BRIGHTNESS80", "BRIGH5", "COLOR25512000", "COLOR999000000",
                    "THEME.NOPE.1,2,3,", "THEME.WAVE1.255,0,0,", "THEME.WAVE1.255,0,0,0,0,999,",
                    "THEME.WAVE1.255,0,0,0,0,255", "COLOR255120000\nLEDOFF", "BTN6", "MODEPIXELS",
                    "PIXEL,172,255000128,", "PIXEL,42,255000128", "PIXEL,42,999000128,", "BACKLED3,0,050050050,",
                    "BACKLED1,0,050050999,", "THEME.M9.255,140,0,", "THEME.M2.255,140,0,255,0,128,"):
            self.assertFalse(valid_command(cmd), cmd)

    def test_every_look_is_a_documented_command(self):
        for name, cmd in ml.LOOKS.items():
            self.assertTrue(valid_command(cmd), name)

    def test_theme_on_a_lit_lamp_goes_through_off(self):
        self.assertEqual(commands_for("working", True), ["LEDOFF", ml.LOOKS["working"]])
        self.assertEqual(commands_for("working", None), ["LEDOFF", ml.LOOKS["working"]])
        self.assertEqual(commands_for("working", False), [ml.LOOKS["working"]])

    def test_solid_colour_on_a_lit_lamp_is_one_write(self):
        self.assertEqual(commands_for("input", True), [ml.LOOKS["input"]])

    def test_a_lamp_that_may_be_dark_is_powered_on_before_an_alert(self):
        self.assertEqual(commands_for("input", False), ["LEDON", ml.LOOKS["input"]])
        self.assertEqual(commands_for("input", None), ["LEDON", ml.LOOKS["input"]])

    def test_other_commands_go_as_they_are(self):
        self.assertEqual(ml.writes_for("BTN3", None), ["BTN3"])
        self.assertEqual(ml.writes_for("PIXEL,42,255000128,", True), ["PIXEL,42,255000128,"])

    def test_off_writes_nothing_when_already_dark(self):
        self.assertEqual(commands_for("off", False), [])
        self.assertEqual(commands_for("off", True), ["LEDOFF"])


class Aggregate(unittest.TestCase):
    def test_waiting_outranks_busy_outranks_idle(self):
        self.assertEqual(aggregate([session("idle"), session("busy"), session("waiting")]), "input")
        self.assertEqual(aggregate([session("idle"), session("busy")]), "working")
        self.assertEqual(aggregate([session("idle"), session("idle")]), "idle")
        self.assertEqual(aggregate([]), "idle")

    def test_waiting_for_alone_counts_as_waiting(self):
        self.assertEqual(aggregate([session("busy", "permission prompt")]), "input")


class Decide(unittest.TestCase):
    def test_first_state_shows_at_once(self):
        self.assertEqual(Decider(settle=3).feed("idle", 0), "idle")

    def test_input_needs_no_settling(self):
        d = Decider(settle=3)
        d.feed("working", 0)
        self.assertEqual(d.feed("input", 10), "input")

    def test_short_flicker_is_ignored(self):
        d = Decider(settle=3)
        d.feed("idle", 0)
        self.assertEqual(d.feed("working", 10), "idle")
        self.assertEqual(d.feed("idle", 11), "idle")
        self.assertEqual(d.feed("working", 12), "idle")
        self.assertEqual(d.feed("working", 15), "working")

    def test_leaving_input_settles(self):
        d = Decider(settle=3)
        d.feed("input", 0)
        self.assertEqual(d.feed("working", 1), "input")
        self.assertEqual(d.feed("working", 4), "working")

    def test_quiet_lamp_goes_dark_and_stays_dark_until_work(self):
        d = Decider(settle=3, off_after=100)
        d.feed("idle", 0)
        self.assertEqual(d.feed("idle", 99), "idle")
        self.assertEqual(d.feed("idle", 100), "off")
        self.assertEqual(d.feed("idle", 500), "off")
        self.assertEqual(d.feed("working", 600), "off")
        self.assertEqual(d.feed("working", 603), "working")

    def test_quiet_timer_restarts_after_work(self):
        d = Decider(settle=0, off_after=100)
        d.feed("idle", 0)
        d.feed("working", 50)
        d.feed("idle", 90)
        self.assertEqual(d.feed("idle", 189), "idle")
        self.assertEqual(d.feed("idle", 190), "off")


class ReadRegistry(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def put(self, name, body):
        (self.root / name).write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")

    def test_dead_process_is_ignored(self):
        self.put("1.json", {"pid": 1, "status": "busy", "procStart": "5"})
        self.put("2.json", {"pid": 2, "status": "idle", "procStart": "6"})
        sessions, complete = Registry(self.root, alive=lambda pid, start: pid == 2).read()
        self.assertEqual([s.pid for s in sessions], [2])
        self.assertTrue(complete)

    def test_other_files_and_broken_json_are_skipped(self):
        self.put("1.key", "x")
        self.put("2.json", "{not json")
        self.put("3.json", {"pid": 3, "status": "idle"})
        sessions, complete = Registry(self.root, alive=lambda *_: True).read()
        self.assertEqual([s.pid for s in sessions], [3])
        self.assertTrue(complete)

    def test_a_rewrite_in_progress_keeps_the_last_good_copy(self):
        self.put("1.json", {"pid": 1, "status": "busy"})
        reg = Registry(self.root, alive=lambda *_: True)
        reg.read()
        self.put("1.json", "{half written")
        os.utime(self.root / "1.json", (1, 1))
        sessions, _ = reg.read()
        self.assertEqual(sessions[0].status, "busy")

    def test_a_changed_file_is_read_again(self):
        self.put("1.json", {"pid": 1, "status": "busy"})
        reg = Registry(self.root, alive=lambda *_: True)
        reg.read()
        self.put("1.json", {"pid": 1, "status": "waiting", "waitingFor": "permission prompt"})
        os.utime(self.root / "1.json", (2, 2))
        sessions, _ = reg.read()
        self.assertEqual((sessions[0].status, sessions[0].waiting_for), ("waiting", "permission prompt"))

    def test_missing_status_field_flags_the_format_change(self):
        self.put("1.json", {"pid": 1, "state": "busy"})
        _, complete = Registry(self.root, alive=lambda *_: True).read()
        self.assertFalse(complete)

    def test_a_file_in_an_unknown_shape_is_skipped(self):
        self.put("1.json", "[1, 2, 3]")
        self.put("2.json", "null")
        self.put("3.json", {"pid": os.getpid(), "procStart": "soon", "status": "busy"})
        self.put("4.json", {"pid": os.getpid(), "status": "idle"})
        sessions, complete = Registry(self.root).read()
        self.assertEqual([s.status for s in sessions], ["idle"])
        self.assertTrue(complete)

    def test_config_folder_follows_the_environment(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root)}):
            self.assertEqual(ml.config_dir(), self.root)
            self.assertEqual(Registry().root, self.root / "sessions")
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(ml.config_dir(), Path.home() / ".claude")

    def test_a_session_just_started_is_no_format_change(self):
        self.put("1.json", {"pid": 1, "status": "busy"})
        self.put("2.json", {"pid": 2})
        sessions, complete = Registry(self.root, alive=lambda *_: True).read()
        self.assertEqual(len(sessions), 2)
        self.assertTrue(complete)

    def test_missing_folder_flags_the_format_change(self):
        _, complete = Registry(self.root / "gone", alive=lambda *_: True).read()
        self.assertFalse(complete)

    def test_an_empty_folder_means_nothing_is_running(self):
        sessions, complete = Registry(self.root, alive=lambda *_: True).read()
        self.assertEqual(sessions, [])
        self.assertTrue(complete)


class NightHours(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.quiet_file = self.root / "quiet.json"
        for name, value in (("QUIET_FILE", self.quiet_file), ("STATUS_FILE", self.root / "status.json"),
                            ("PAUSE_FLAG", self.root / "paused"), ("STATE", self.root)):
            patcher = mock.patch.object(ml, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, start, end):
        self.quiet_file.write_text(json.dumps({"from": start, "to": end}), encoding="utf-8")

    def test_clock_times(self):
        self.assertEqual(ml.parse_clock("0:30"), 30)
        self.assertEqual(ml.parse_clock("08:00"), 480)
        self.assertEqual(ml.parse_clock("23:59"), 1439)
        for bad in ("24:00", "12:60", "noon", "", "8", "08:0"):
            with self.assertRaises(ValueError):
                ml.parse_clock(bad)

    def test_window_within_a_day(self):
        self.assertTrue(ml.in_window(60, 30, 480))
        self.assertTrue(ml.in_window(30, 30, 480))
        self.assertFalse(ml.in_window(480, 30, 480))
        self.assertFalse(ml.in_window(29, 30, 480))

    def test_window_across_midnight(self):
        start, end = ml.parse_clock("23:00"), ml.parse_clock("08:00")
        for clock in ("23:00", "23:59", "00:00", "07:59"):
            self.assertTrue(ml.in_window(ml.parse_clock(clock), start, end), clock)
        for clock in ("08:00", "12:00", "22:59"):
            self.assertFalse(ml.in_window(ml.parse_clock(clock), start, end), clock)

    def test_no_file_means_no_night_hours(self):
        self.assertFalse(ml.QuietHours().active(60))

    def test_file_sets_clears_and_changes_the_window(self):
        quiet = ml.QuietHours()
        self.write("00:30", "08:00")
        self.assertTrue(quiet.active(60))
        self.assertFalse(quiet.active(600))
        self.assertEqual(quiet.label, "00:30-08:00")
        self.write("09:00", "11:00")
        os.utime(self.quiet_file, (5, 5))
        self.assertTrue(quiet.active(600))
        self.assertFalse(quiet.active(60))
        self.quiet_file.unlink()
        self.assertFalse(quiet.active(600))
        self.assertEqual(quiet.label, "")

    def test_a_broken_file_leaves_the_lamp_on_duty(self):
        for body in ("{half", '{"from": "25:00", "to": "08:00"}', '{"from": 1, "to": 2}', "{}"):
            self.quiet_file.write_text(body, encoding="utf-8")
            self.assertFalse(ml.QuietHours().active(60), body)

    def test_command_sets_shows_clears_and_refuses(self):
        run = lambda *args: ml.main(["quiet", *args])
        with mock.patch("builtins.print") as shown:
            saved = {"from": "00:30", "to": "08:00", "idle_minutes": 15}
            self.assertEqual(run("0:30", "8:00"), 0)
            self.assertEqual(json.loads(self.quiet_file.read_text(encoding="utf-8")), saved)
            self.assertEqual(run("25:00", "8:00"), 2)
            self.assertEqual(run("23:00"), 2)
            self.assertEqual(json.loads(self.quiet_file.read_text(encoding="utf-8")), saved)
            self.assertEqual(run(), 0)
            self.assertIn("00:30-08:00", shown.call_args.args[0])
            self.assertIn("15 min", shown.call_args.args[0])
            self.assertEqual(run("0:30", "8:00", "--idle", "0"), 0)
            self.assertEqual(json.loads(self.quiet_file.read_text(encoding="utf-8"))["idle_minutes"], 0)
            self.assertIn("dark throughout", shown.call_args.args[0])
            self.assertEqual(run("off"), 0)
        self.assertFalse(self.quiet_file.exists())

    def test_input_clock_reads_this_session(self):
        self.assertGreaterEqual(ml.seconds_since_input(), 0.0)
        self.assertLess(ml.seconds_since_input(), 50 * 86400)

    async def daemon_after_a_moment(self):
        daemon = ml.Daemon(dry_run=True)
        daemon.registry = Registry(self.root)
        (self.root / "1.json").write_text(json.dumps({"pid": os.getpid(), "status": "waiting"}), encoding="utf-8")
        try:
            await asyncio.wait_for(daemon.watch_sessions(), 0.5)
        except asyncio.TimeoutError:
            pass
        return daemon

    def write_around_now(self, **extra):
        now = time.localtime()
        minute = now.tm_hour * 60 + now.tm_min
        clock = lambda m: f"{(m % 1440) // 60:02d}:{(m % 1440) % 60:02d}"
        self.quiet_file.write_text(json.dumps({"from": clock(minute - 5), "to": clock(minute + 5), **extra}), encoding="utf-8")

    async def test_inside_the_hours_an_idle_computer_releases_the_lamp(self):
        self.write_around_now()
        with mock.patch.object(ml, "seconds_since_input", lambda: 15 * 60):
            self.assertTrue((await self.daemon_after_a_moment()).paused)

    async def test_inside_the_hours_a_computer_in_use_keeps_the_lamp(self):
        self.write_around_now()
        with mock.patch.object(ml, "seconds_since_input", lambda: 15 * 60 - 1):
            daemon = await self.daemon_after_a_moment()
        self.assertFalse(daemon.paused)
        self.assertEqual(daemon.target, "input")

    async def test_idle_zero_keeps_the_lamp_dark_through_the_hours(self):
        self.write_around_now(idle_minutes=0)
        with mock.patch.object(ml, "seconds_since_input", lambda: 0.0):
            self.assertTrue((await self.daemon_after_a_moment()).paused)

    def test_a_bad_idle_value_ignores_the_file(self):
        self.write_around_now(idle_minutes="soon")
        self.assertFalse(ml.QuietHours().active())

    async def test_outside_the_hours_the_lamp_follows_sessions(self):
        now = time.localtime()
        minute = now.tm_hour * 60 + now.tm_min
        clock = lambda m: f"{(m % 1440) // 60:02d}:{(m % 1440) % 60:02d}"
        self.write(clock(minute + 60), clock(minute + 70))
        daemon = await self.daemon_after_a_moment()
        self.assertFalse(daemon.paused)
        self.assertEqual(daemon.target, "input")


class LampDriving(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        for name, value in (("QUIET_FILE", self.root / "quiet.json"), ("STATUS_FILE", self.root / "status.json"),
                            ("PAUSE_FLAG", self.root / "paused"), ("LAMP_FILE", self.root / "lamp.json"),
                            ("LOG_FILE", self.root / "lamp.log"), ("STATE", self.root)):
            patcher = mock.patch.object(ml, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def drive(self, daemon, seconds=0.8):
        try:
            await asyncio.wait_for(daemon.drive_lamp(), seconds)
        except asyncio.TimeoutError:
            pass

    async def test_a_state_change_during_the_writes_still_reaches_the_lamp(self):
        daemon = ml.Daemon(dry_run=True)
        daemon.target = "working"
        writes = []

        async def write(command):
            writes.append(command)
            if len(writes) == 1:
                daemon.target = "input"  # arrives while the first look is going out

        daemon.link.write = write
        await self.drive(daemon)
        self.assertEqual(writes, ["LEDOFF", ml.LOOKS["working"], ml.LOOKS["input"]])

    async def lit_then_released(self, write, seconds=0.6):
        """Lights the lamp, then releases it with `write` as the link's write."""
        daemon = ml.Daemon(dry_run=True)
        daemon.target = "input"
        task = asyncio.create_task(daemon.drive_lamp())
        await asyncio.sleep(0.05)
        daemon.link.write = write
        daemon.paused = True
        await asyncio.sleep(seconds)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return daemon

    async def test_a_failed_power_off_is_tried_again(self):
        writes = []

        async def write(command):
            writes.append(command)
            if len(writes) == 1:
                raise OSError("link dropped")

        with mock.patch.object(ml, "RETRY_MIN_S", 0.01), mock.patch.object(ml, "POLL_S", 0.01), \
                self.assertLogs(ml.log, "WARNING"):
            await self.lit_then_released(write)
        self.assertEqual(writes, ["LEDOFF", "LEDOFF"])

    async def test_a_power_off_that_keeps_failing_is_given_up(self):
        attempts = []

        async def write(command):
            attempts.append(time.monotonic())
            raise OSError("link dropped")

        with mock.patch.object(ml, "RETRY_MIN_S", 0.01), mock.patch.object(ml, "POLL_S", 0.01), \
                mock.patch.object(ml, "DARKEN_RETRY_S", 0.1), self.assertLogs(ml.log, "WARNING"):
            await self.lit_then_released(write, seconds=0.9)  # runs well past the retry window
        self.assertGreater(len(attempts), 1)
        self.assertLess(attempts[-1] - attempts[0], 0.4)

    async def test_a_restart_while_released_leaves_the_lamp_alone(self):
        ml.PAUSE_FLAG.write_text("x", encoding="utf-8")
        daemon = ml.Daemon(dry_run=True)
        daemon.target = "input"
        writes = []

        async def write(command):
            writes.append(command)

        daemon.link.write = write
        await self.drive(daemon, 0.3)
        self.assertEqual(writes, [])

    async def test_release_during_a_connection_writes_nothing(self):
        daemon = ml.Daemon(dry_run=False)
        daemon.target = "input"

        class FakeLink:
            dry_run = False
            name = "fake"
            connected = False
            writes = []
            closed = 0

            async def connect(self):
                self.connected = True
                daemon.paused = True
                return True

            async def write(self, command):
                self.writes.append(command)

            async def close(self):
                self.connected = False
                self.closed += 1

        daemon.link = FakeLink()
        with mock.patch.object(ml, "POLL_S", 0.01):
            await self.drive(daemon, 0.2)
        self.assertEqual(daemon.link.writes, [])
        self.assertEqual(daemon.link.closed, 1)

    def test_background_sessions_with_no_process_count_through_the_command(self):
        listing = json.dumps([{"state": "blocked", "name": "a"}, {"state": "working", "name": "b"}, {"state": "done", "name": "c"},
                              {"pid": 7, "status": "idle", "name": "d"}])
        with mock.patch.object(ml, "claude_exe", lambda: "claude.exe"), \
                mock.patch.object(ml.subprocess, "run", lambda *a, **k: mock.Mock(returncode=0, stdout=listing)):
            sessions = ml.read_agents_command()
        self.assertEqual([(s.name, s.status) for s in sessions], [("a", "waiting"), ("b", "busy"), ("d", "idle")])
        self.assertEqual(aggregate(sessions), "input")

    def test_a_released_lamp_is_known_before_the_lamp_task_runs(self):
        ml.PAUSE_FLAG.write_text("x", encoding="utf-8")
        self.assertTrue(ml.Daemon(dry_run=True).paused)

    def test_a_fresh_daemon_is_not_released(self):
        self.assertFalse(ml.Daemon(dry_run=True).paused)

    def test_an_undocumented_look_stops_the_daemon_at_the_start(self):
        args = argparse.Namespace(dry_run=True, log_file=True)
        handlers = list(ml.log.handlers)
        try:
            with mock.patch.dict(ml.LOOKS, {"working": "THEME.NOPE.1,2,3,"}):
                self.assertEqual(ml.cmd_run(args), 2)
        finally:
            for handler in [h for h in ml.log.handlers if h not in handlers]:
                handler.close()
                ml.log.removeHandler(handler)

    def test_pin_sets_shows_clears_and_refuses(self):
        run = lambda *args: ml.main(["pin", *args])
        with mock.patch("builtins.print") as shown:
            self.assertEqual(run("aa:bb:cc:dd:ee:ff"), 0)
            self.assertEqual(ml.load_pinned(), {"address": "AA:BB:CC:DD:EE:FF"})
            self.assertEqual(run("lamp"), 2)
            self.assertEqual(ml.load_pinned(), {"address": "AA:BB:CC:DD:EE:FF"})
            self.assertEqual(run(), 0)
            self.assertEqual(shown.call_args.args[1], "AA:BB:CC:DD:EE:FF")
            self.assertEqual(run("off"), 0)
        self.assertEqual(ml.load_pinned(), {})

    def test_a_pin_saved_by_windows_tools_still_reads(self):
        ml.LAMP_FILE.write_bytes(b"\xef\xbb\xbf" + b'{"address": "AA:BB:CC:DD:EE:FF"}')
        self.assertEqual(ml.load_pinned()["address"], "AA:BB:CC:DD:EE:FF")

    def test_an_unreadable_pin_is_ignored(self):
        for body in ('{"address": "AA"}'.encode("utf-16"), b"{half", b"[1]"):
            ml.LAMP_FILE.write_bytes(body)
            with self.assertLogs(ml.log, "WARNING") if body != b"[1]" else nullcontext():
                self.assertEqual(ml.load_pinned(), {})


class FallbackToDocumentedCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        self.saved = ml.STATUS_FILE, ml.PAUSE_FLAG
        ml.STATUS_FILE, ml.PAUSE_FLAG = self.root / "status.json", self.root / "paused"
        self.addCleanup(lambda: (setattr(ml, "STATUS_FILE", self.saved[0]), setattr(ml, "PAUSE_FLAG", self.saved[1])))
        patcher = mock.patch.object(ml, "QUIET_FILE", self.root / "quiet.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    async def watch(self, daemon, seconds):
        try:
            await asyncio.wait_for(daemon.watch_sessions(), seconds)
        except asyncio.TimeoutError:
            pass

    async def test_files_without_status_switch_to_the_command(self):
        (self.root / "1.json").write_text(json.dumps({"pid": os.getpid(), "state": "busy"}), encoding="utf-8")
        daemon = ml.Daemon(dry_run=True)
        daemon.registry = Registry(self.root)
        calls = []
        with mock.patch.object(ml, "read_agents_command", lambda: calls.append(1) or [session("busy")]):
            await self.watch(daemon, 1.5)
        self.assertEqual(daemon.source, "command")
        self.assertEqual(calls, [1])
        self.assertEqual(daemon.target, "working")

    @needs_live_claude
    async def test_real_command_feeds_the_daemon(self):
        (self.root / "1.json").write_text(json.dumps({"pid": os.getpid(), "state": "busy"}), encoding="utf-8")
        daemon = ml.Daemon(dry_run=True)
        daemon.registry = Registry(self.root)
        await self.watch(daemon, 6)
        self.assertEqual(daemon.source, "command")
        self.assertTrue(daemon.sessions)

    async def test_no_sessions_asks_the_command_only_once_in_a_while(self):
        daemon = ml.Daemon(dry_run=True)
        daemon.registry = Registry(self.root)
        calls = []
        with mock.patch.object(ml, "read_agents_command", lambda: calls.append(1) or []):
            with mock.patch.object(ml, "POLL_S", 0.01):
                await self.watch(daemon, 0.5)
        self.assertEqual(calls, [1])
        self.assertEqual(daemon.target, "idle")

    def test_a_locked_status_file_is_survived(self):
        daemon = ml.Daemon(dry_run=True)
        with mock.patch.object(ml.os, "replace", side_effect=PermissionError(5, "in use")):
            daemon.write_status()

    async def test_a_surprise_while_watching_is_survived(self):
        daemon = ml.Daemon(dry_run=True)
        daemon.registry = Registry(self.root)
        calls = []

        def surprise():
            calls.append(1)
            raise RuntimeError("unexpected")

        daemon.registry.read = surprise
        with mock.patch.object(ml, "RETRY_MIN_S", 0.01), self.assertLogs(ml.log, "ERROR"):
            try:
                await asyncio.wait_for(daemon.watch_sessions_forever(), 0.2)
            except asyncio.TimeoutError:
                pass
        self.assertGreater(len(calls), 1)

    async def test_healthy_files_never_call_the_command(self):
        (self.root / "1.json").write_text(json.dumps({"pid": os.getpid(), "status": "waiting"}), encoding="utf-8")
        daemon = ml.Daemon(dry_run=True)
        daemon.registry = Registry(self.root)
        calls = []
        with mock.patch.object(ml, "read_agents_command", lambda: calls.append(1) or []):
            await self.watch(daemon, 1.5)
        self.assertEqual(calls, [])
        self.assertEqual(daemon.source, "files")
        self.assertEqual(daemon.target, "input")


class Liveness(unittest.TestCase):
    def test_this_process_is_alive_and_a_wrong_start_time_is_not(self):
        self.assertTrue(ml.process_alive(os.getpid()))
        self.assertFalse(ml.process_alive(os.getpid(), 123))

    def test_a_live_claude_session_matches_its_recorded_start(self):
        found = session_files()
        if not found:
            self.skipTest("needs Claude Code running on this machine")
        self.assertTrue(any(ml.process_alive(d["pid"], d["procStart"]) for d in found))

    def test_access_denied_exists_but_is_no_recorded_session(self):
        with mock.patch.object(ml._kernel32, "OpenProcess", lambda *_: None), \
                mock.patch.object(ml.ctypes, "get_last_error", lambda: 5):
            self.assertTrue(ml.process_alive(4))
            self.assertFalse(ml.process_alive(4, 123))

    def test_a_gone_process_is_dead(self):
        with mock.patch.object(ml._kernel32, "OpenProcess", lambda *_: None), \
                mock.patch.object(ml.ctypes, "get_last_error", lambda: 87):
            self.assertFalse(ml.process_alive(99999))


class AgentsCommand(unittest.TestCase):
    def test_child_cli_gets_no_desktop_login_variables(self):
        planted = {"CLAUDE_CODE_OAUTH_TOKEN": "x", "CLAUDECODE": "1", "ANTHROPIC_BASE_URL": "http://x",
                   "CLAUDE_AGENT_SDK_VERSION": "1", "CLAUDE_CODE_GIT_BASH_PATH": "kept", "PATH": os.environ["PATH"]}
        with mock.patch.dict(os.environ, planted):
            env = ml.standalone_env()
        self.assertEqual(env["CLAUDE_CODE_GIT_BASH_PATH"], "kept")
        self.assertIn("PATH", env)
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE", "ANTHROPIC_BASE_URL", "CLAUDE_AGENT_SDK_VERSION"):
            self.assertNotIn(name, env)

    @needs_live_claude
    def test_the_command_resolves_to_the_program_itself(self):
        self.assertTrue(ml.claude_exe().lower().endswith("claude.exe"))

    @needs_live_claude
    def test_documented_command_returns_sessions_with_status(self):
        sessions = ml.read_agents_command()
        self.assertTrue(sessions)
        self.assertTrue(all(s.status in ("busy", "idle", "waiting") for s in sessions))


if __name__ == "__main__":
    unittest.main()
