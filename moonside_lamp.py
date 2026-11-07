"""A Moonside lamp as a status light for Claude Code on Windows.

The lamp follows every live Claude Code session on this machine at once:
  input    at least one session is waiting for an approval or an answer
  working  at least one session is running, none is waiting
  idle     nothing is running
  off      nothing has run for OFF_AFTER_S seconds

Session state comes from the files Claude Code keeps in ~/.claude/sessions. No hook
is installed, so a lamp fault cannot touch a session. If those files stop carrying a
status field, the daemon reads the documented `claude agents --json` instead.

Protocol: https://developer.moonside.design (plain ASCII written to a BLE characteristic).

Inside the night hours set with `quiet`, once nobody has touched the computer for 15 minutes,
the lamp is dark and released, pink included; any input brings it back.

Commands: run, status, scan, off, on, quiet, pin, demo.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

HOME = Path(__file__).resolve().parent
STATE = HOME / "state"
PAUSE_FLAG = STATE / "paused"
STATUS_FILE = STATE / "status.json"
LAMP_FILE = STATE / "lamp.json"
LOG_FILE = STATE / "lamp.log"
QUIET_FILE = STATE / "quiet.json"

WRITE_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
READ_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
NAME_PREFIX = "MOONSIDE"

# What each state looks like. Relative brightness lives in the RGB values, so a state
# change is one write and the lamp's own brightness buttons keep working.
LOOKS = {
    "input": "COLOR255000110",  # strong pink
    "working": "THEME.WAVE1.0,50,255,0,170,255,",  # slow blue wave
    "idle": "COLOR120075030",  # soft warm white
    "off": "LEDOFF",
}

SETTLE_S = 3.0  # a state must hold this long before the lamp changes; "input" skips the wait
OFF_AFTER_S = 30 * 60
POLL_S = 1.0
FALLBACK_POLL_S = 15.0  # `claude agents --json` costs about 1.3 s per call
EMPTY_RECHECK_S = 300.0  # no session files at all: usually nothing runs, so ask rarely
WRITE_GAP_S = 0.15  # the protocol page warns that rapid writes interrupt animations
SCAN_S = 10.0
RETRY_MIN_S, RETRY_MAX_S = 15.0, 300.0
DARKEN_RETRY_S = 120.0  # a failed power-off is retried this long; after that the lamp may be in someone's hands
HEARTBEAT_S = 3600.0
MUTEX_WAIT_S = 20
IDLE_MINUTES = 15  # inside the night hours, the lamp goes dark after this long without input

# Colours per theme, from the catalog at developer.moonside.design. -1 means the single
# parameter is a speed; 0 means the documented placeholder "0,".
THEME_COLOURS = {
    "RAINBOW1": -1, "RAINBOW2": -1, "RAINBOW3": 0, "FIRE1": 0, "PALETTE1": 0,
    "THEME1": 2, "THEME2": 2, "THEME4": 2, "THEME5": 2, "COLORDROP1": 2,
    "GRADIENT1": 2, "GRADIENT3": 2, "TWINKLE1": 2, "PULSING1": 2, "BEAT2": 2, "WAVE1": 2,
    "GRADIENT2": 3, "BEAT1": 3, "BEAT3": 3, "LAVA1": 3,
    "FIRE2": 4, "THEME3": 6, "PALETTE2": 6,
    "M1": 2, "M2": 3, "M3": 2, "M4": 2, "M5": 2, "M6": 2, "M7": 2, "M8": 3, "M9": 2, "M10": 3, "M11": 3, "M12": 2,
}
PIXEL_MAX = 171

log = logging.getLogger("moonside-lamp")


# ---------------------------------------------------------------- commands

def valid_command(cmd: str) -> bool:
    """True for commands the protocol page documents; the lamp reports no errors itself."""
    if re.fullmatch(r"LEDON|LEDOFF|MODEPIXEL|BTN[1-5]", cmd):
        return True
    m = re.fullmatch(r"COLOR(\d{3})(\d{3})(\d{3})", cmd) or re.fullmatch(r"BACKLED[12],0,(\d{3})(\d{3})(\d{3}),", cmd)
    if m:
        return all(int(v) <= 255 for v in m.groups())
    m = re.fullmatch(r"PIXEL,(\d{1,3}),(\d{3})(\d{3})(\d{3}),", cmd)
    if m:
        return int(m.group(1)) <= PIXEL_MAX and all(int(v) <= 255 for v in m.groups()[1:])
    m = re.fullmatch(r"BRIGH(\d{1,3})", cmd)
    if m:
        return 10 <= int(m.group(1)) <= 100
    m = re.fullmatch(r"THEME\.([A-Z0-9]+)\.((?:\d{1,3},)+)", cmd)
    if not m or m.group(1) not in THEME_COLOURS:
        return False
    values = [int(v) for v in m.group(2).rstrip(",").split(",")]
    colours = THEME_COLOURS[m.group(1)]
    if colours <= 0:
        return len(values) == 1
    return len(values) == colours * 3 and all(v <= 255 for v in values)


def writes_for(command: str, lit: bool | None) -> list[str]:
    """The writes that put `command` on the lamp. lit: True on, False off, None unknown."""
    if command == "LEDOFF":
        return [] if lit is False else [command]
    if command.startswith("THEME."):
        # A theme applied to a lit lamp blends with what it is showing.
        return [command] if lit is False else ["LEDOFF", command]
    if command.startswith("COLOR") and lit is not True:
        # The protocol page names LEDON as what powers the lamp on; a dark lamp must light for an alert.
        return ["LEDON", command]
    return [command]


def commands_for(target: str, lit: bool | None) -> list[str]:
    return writes_for(LOOKS[target], lit)


# ---------------------------------------------------------------- session state

@dataclass(frozen=True)
class Session:
    pid: int
    status: str | None
    waiting_for: str | None
    name: str | None


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
if _kernel32:
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    _kernel32.GetTickCount.restype = wintypes.DWORD


def process_alive(pid: int, proc_start: str | int | None = None) -> bool:
    """True if pid runs and, when proc_start is given, is that same process (pids get reused)."""
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        # Access denied: it exists under another account, so it cannot be the session a file records.
        return proc_start is None and ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return False
        if proc_start is None:
            return True
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return True
        started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return started == int(proc_start)
    finally:
        _kernel32.CloseHandle(handle)


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


class Registry:
    """Reads the session files in the Claude Code config folder, parsing a file again only when it has changed."""

    def __init__(self, root: Path | None = None, alive=process_alive):
        self.root = root or config_dir() / "sessions"
        self.alive = alive
        self._cache: dict[str, tuple[int, dict]] = {}

    def read(self) -> tuple[list[Session], bool]:
        """Returns the live sessions and whether the files still look as expected.

        A session just started has no status for a second or two, so only files that
        all lack one mean the format has changed.
        """
        sessions: list[Session] = []
        present = set()
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return [], False
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            present.add(entry.path)
            try:
                stamp = entry.stat().st_mtime_ns
                cached = self._cache.get(entry.path)
                if cached and cached[0] == stamp:
                    data = cached[1]
                else:
                    with open(entry.path, encoding="utf-8") as f:
                        data = json.load(f)
                    self._cache[entry.path] = (stamp, data)
            except (OSError, ValueError):
                # Mid-rewrite: use the last good copy; a new file waits for the next poll.
                cached = self._cache.get(entry.path)
                if not cached:
                    continue
                data = cached[1]
            try:
                pid = data.get("pid")
                if not isinstance(pid, int) or not self.alive(pid, data.get("procStart")):
                    continue
                sessions.append(Session(pid, data.get("status"), data.get("waitingFor"), data.get("name")))
            except (ValueError, TypeError, AttributeError):  # a file in a shape this version does not know
                continue
        for path in set(self._cache) - present:
            del self._cache[path]
        return sessions, not sessions or any(isinstance(s.status, str) for s in sessions)


_DESKTOP_VARS = re.compile(r"^(CLAUDE_CODE_|CLAUDE_AGENT_SDK_)|^(CLAUDECODE|CLAUDE_PID|CLAUDE_PREVIEW_CLASSIFIER_FLOOR|"
                           r"ANTHROPIC_BASE_URL|USE_LOCAL_OAUTH|USE_STAGING_OAUTH)$", re.I)


def standalone_env() -> dict[str, str]:
    """The environment minus what a desktop session hands its children, which ties a child CLI to the desktop's login."""
    return {k: v for k, v in os.environ.items() if k.upper() == "CLAUDE_CODE_GIT_BASH_PATH" or not _DESKTOP_VARS.search(k)}


def claude_exe() -> str | None:
    path = shutil.which("claude.exe") or shutil.which("claude")
    if path and path.lower().endswith(".cmd"):  # the WinGet shim; run the program it points at
        with open(path, encoding="utf-8", errors="replace") as f:
            m = re.search(r'"([^"]+claude\.exe)"', f.read(), re.I)
        path = m.group(1) if m else None
    return path


def read_agents_command() -> list[Session]:
    """The documented way to read session state; slow, so used only as a fallback."""
    claude = claude_exe()
    if not claude:
        return []
    done = subprocess.run([claude, "agents", "--json"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=30, env=standalone_env(),
                          creationflags=subprocess.CREATE_NO_WINDOW)
    if done.returncode != 0:
        return []
    sessions = []
    for entry in json.loads(done.stdout):
        # A background session with no running process reports only `state`.
        status = entry.get("status") or {"blocked": "waiting", "working": "busy"}.get(entry.get("state"))
        if entry.get("pid") or status:
            sessions.append(Session(entry.get("pid", 0), status, entry.get("waitingFor"), entry.get("name")))
    return sessions


def aggregate(sessions: list[Session]) -> str:
    if any(s.status == "waiting" or s.waiting_for for s in sessions):
        return "input"
    if any(s.status == "busy" for s in sessions):
        return "working"
    return "idle"


class Decider:
    """Turns the raw state into what the lamp should show: settles flicker, goes dark when quiet."""

    def __init__(self, settle: float = SETTLE_S, off_after: float = OFF_AFTER_S):
        self.settle = settle
        self.off_after = off_after
        self.shown: str | None = None
        self._candidate: str | None = None
        self._candidate_since = 0.0
        self._idle_since = 0.0

    def feed(self, state: str, now: float) -> str:
        if state != self._candidate:
            self._candidate, self._candidate_since = state, now
        settled = state == "input" or self.shown is None or now - self._candidate_since >= self.settle
        if settled and state != self.shown and not (self.shown == "off" and state == "idle"):
            self.shown = state
            if state == "idle":
                self._idle_since = now
        if self.shown == "idle" and now - self._idle_since >= self.off_after:
            self.shown = "off"
        return self.shown


# ---------------------------------------------------------------- night hours

def parse_clock(text: str) -> int:
    """Minutes since midnight from "H:MM" or "HH:MM"."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"not a time of day: {text!r}")
    return int(m.group(1)) * 60 + int(m.group(2))


def in_window(minute: int, start: int, end: int) -> bool:
    """True if the minute of the day falls in [start, end), which may cross midnight."""
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def seconds_since_input() -> float:
    """Time since the last keyboard or mouse input in this logon session."""
    info = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return ((_kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


class QuietHours:
    """The window in state/quiet.json; the file is parsed again only when it changes."""

    def __init__(self, path: Path | None = None):
        self.path = path or QUIET_FILE
        self._stamp: int | None = None
        self.window: tuple[int, int] | None = None
        self.label = ""
        self.idle_minutes = IDLE_MINUTES

    def active(self, minute: int | None = None) -> bool:
        try:
            stamp = self.path.stat().st_mtime_ns
        except OSError:
            self._stamp, self.window, self.label, self.idle_minutes = None, None, "", IDLE_MINUTES
            return False
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.window = parse_clock(data["from"]), parse_clock(data["to"])
                self.label = f'{data["from"]}-{data["to"]}'
                self.idle_minutes = max(0.0, float(data.get("idle_minutes", IDLE_MINUTES)))
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.warning("ignoring %s: %s", self.path.name, e)
                self.window, self.label = None, ""
        if self.window is None:
            return False
        if minute is None:
            now = time.localtime()
            minute = now.tm_hour * 60 + now.tm_min
        return in_window(minute, *self.window)

    def dark_now(self) -> bool:
        """Inside the window and nobody has touched the computer for idle_minutes."""
        return self.active() and seconds_since_input() >= self.idle_minutes * 60


# ---------------------------------------------------------------- the lamp

def load_pinned() -> dict:
    try:
        data = json.loads(LAMP_FILE.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("ignoring %s: %s", LAMP_FILE.name, e)
        return {}
    return data if isinstance(data, dict) else {}


async def find_lamp(seconds: float = SCAN_S):
    """The pinned lamp if set; otherwise the strongest Moonside in range."""
    from bleak import BleakScanner
    pinned = str(load_pinned().get("address") or "").upper()
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    lamps = [(adv.rssi, dev, adv.local_name or dev.name or "") for dev, adv in found.values()
             if (adv.local_name or dev.name or "").upper().startswith(NAME_PREFIX)]
    if pinned:
        lamps = [lamp for lamp in lamps if lamp[1].address.upper() == pinned]
    if not lamps:
        return None, ""
    _, device, name = max(lamps, key=lambda lamp: lamp[0])
    return device, name


class Link:
    """One BLE connection to the lamp, or a log line per write in a dry run."""

    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.client = None
        self.name = ""
        self._last_write = 0.0

    @property
    def connected(self) -> bool:
        return self.dry_run or (self.client is not None and self.client.is_connected)

    async def connect(self) -> bool:
        if self.dry_run:
            self.name = "dry-run"
            return True
        from bleak import BleakClient
        device, name = await find_lamp()
        if device is None:
            return False
        client = BleakClient(device, timeout=20.0)
        await client.connect()
        self.client, self.name = client, name
        return True

    async def write(self, command: str) -> None:
        if not valid_command(command):
            raise ValueError(f"not a documented lamp command: {command}")
        wait = WRITE_GAP_S - (time.monotonic() - self._last_write)
        if wait > 0:
            await asyncio.sleep(wait)
        log.info("write %s", command)
        if not self.dry_run:
            await self.client.write_gatt_char(WRITE_CHAR, command.encode("ascii"))
        self._last_write = time.monotonic()

    async def close(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:  # the link is already gone
                log.info("disconnect: %s", e)


class Daemon:
    def __init__(self, dry_run: bool = False):
        self.registry = Registry()
        self.decider = Decider()
        self.quiet = QuietHours()
        self.link = Link(dry_run)
        self.target: str | None = None
        self.paused = PAUSE_FLAG.exists() or self.quiet.dark_now()  # known before the lamp task first runs
        self.lamp_state = "starting"
        self.source = "files"
        self.sessions: list[Session] = []

    async def watch_sessions(self) -> None:
        last_status = 0.0
        last_beat = time.monotonic()
        last_fallback = 0.0
        last_raw = None
        while True:
            now = time.monotonic()
            sessions, complete = self.registry.read()
            if complete and sessions:
                if self.source != "files":
                    log.info("session files carry a status")
                self.source = "files"
                self.sessions = sessions
            else:
                # Either the format changed or nothing is live; the documented command tells which.
                wait = FALLBACK_POLL_S if not complete or self.sessions else EMPTY_RECHECK_S
                if now - last_fallback >= wait:
                    last_fallback = now
                    if not complete and self.source != "command":
                        log.warning("session files lack a status; using claude agents --json")
                    self.source = "command"
                    try:
                        self.sessions = await asyncio.to_thread(read_agents_command)
                    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError) as e:
                        log.warning("claude agents --json failed: %s", e)
                        self.sessions = sessions
            raw = aggregate(self.sessions)
            target = self.decider.feed(raw, now)
            quiet = self.quiet.dark_now()
            paused = quiet or PAUSE_FLAG.exists()
            if paused != self.paused:
                log.info("taking the lamp back" if not paused else
                         f"releasing the lamp: night hours {self.quiet.label}" if quiet else "releasing the lamp")
                self.paused = paused
            if raw != last_raw or target != self.target:
                busy, waiting, idle = self.counts()
                log.info("state %s, lamp %s (busy %d, waiting %d, idle %d)", raw, target, busy, waiting, idle)
                last_raw = raw
            self.target = target
            if now - last_beat >= HEARTBEAT_S:
                last_beat = now
                busy, waiting, idle = self.counts()
                log.info("alive: lamp %s, %s (busy %d, waiting %d, idle %d)", target, self.lamp_state, busy, waiting, idle)
            if now - last_status >= 5.0:
                last_status = now
                self.write_status()
            await asyncio.sleep(POLL_S)

    def counts(self) -> tuple[int, int, int]:
        waiting = sum(1 for s in self.sessions if s.status == "waiting" or s.waiting_for)
        busy = sum(1 for s in self.sessions if s.status == "busy" and not s.waiting_for)
        return busy, waiting, len(self.sessions) - busy - waiting

    def write_status(self) -> None:
        busy, waiting, idle = self.counts()
        body = {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid(), "lamp": self.lamp_state,
                "lamp_name": self.link.name, "shown": self.target, "paused": self.paused,
                "night_hours": self.quiet.label, "input_idle_s": round(seconds_since_input()),
                "source": self.source, "busy": busy, "waiting": waiting, "idle": idle}
        tmp = STATUS_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, STATUS_FILE)
        except OSError as e:  # Windows refuses the replace while someone is reading the file; the next write catches up
            log.info("status file not written: %s", e)

    async def drive_lamp(self) -> None:
        retry = RETRY_MIN_S
        applied: str | None = None
        lit: bool | None = None
        darken_until = 0.0  # released while lit: keep trying to darken the lamp until this moment
        while True:
            try:
                if self.paused:
                    if lit:
                        darken_until = time.monotonic() + DARKEN_RETRY_S
                    applied, lit = None, None
                    self.lamp_state = "released"
                    if time.monotonic() < darken_until:
                        if not (self.link.connected or await self.link.connect()):
                            await asyncio.sleep(RETRY_MIN_S)
                            continue
                        await self.link.write("LEDOFF")
                        darken_until = 0.0
                    if self.link.connected and not self.link.dry_run:
                        await self.link.close()
                    await asyncio.sleep(POLL_S)
                    continue
                darken_until = 0.0
                if not self.link.connected:
                    self.lamp_state = "searching"
                    if not await self.link.connect():
                        if retry == RETRY_MIN_S:
                            log.info("no lamp in range; it stays silent while it is unplugged or joined to a phone")
                        await asyncio.sleep(retry)
                        retry = min(retry * 2, RETRY_MAX_S)
                        continue
                    log.info("connected to %s", self.link.name)
                    retry = RETRY_MIN_S
                    applied, lit = None, None
                    if self.paused:  # released while connecting: the lamp stays as it is
                        continue
                self.lamp_state = "connected"
                target = self.target  # it may change during the writes; the next pass catches up
                if target is not None and target != applied:
                    for command in commands_for(target, lit):
                        await self.link.write(command)
                    applied, lit = target, target != "off"
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # BLE faults come in many types; the answer to each is to reconnect
                log.warning("lamp link: %s: %s", type(e).__name__, e)
                await self.link.close()
                self.lamp_state = "searching"
                await asyncio.sleep(RETRY_MIN_S)

    async def run(self) -> None:
        await asyncio.gather(self.watch_sessions_forever(), self.drive_lamp())

    async def watch_sessions_forever(self) -> None:
        """A long-running daemon outlives a surprise in a session file or the file system."""
        while True:
            try:
                await self.watch_sessions()
            except Exception:
                log.exception("watching sessions failed; starting over in %.0f s", RETRY_MIN_S)
                await asyncio.sleep(RETRY_MIN_S)


# ---------------------------------------------------------------- commands for people

def setup_logging(to_file: bool) -> None:
    STATE.mkdir(exist_ok=True)
    handler: logging.Handler
    if to_file:
        handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def cmd_run(args) -> int:
    setup_logging(to_file=sys.stdout is None or args.log_file)
    bad = [name for name, command in LOOKS.items() if not valid_command(command)]
    if bad:
        log.error("LOOKS holds commands the lamp does not document: %s", ", ".join(bad))
        return 2
    _kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(), 0x4000)  # below normal
    # After a restart the old process may hold the name for a moment, so wait before giving way.
    for _ in range(MUTEX_WAIT_S):
        ctypes.set_last_error(0)
        mutex = _kernel32.CreateMutexW(None, False, "Local\\moonside-lamp-daemon")
        if ctypes.get_last_error() != 183:  # ERROR_ALREADY_EXISTS
            break
        _kernel32.CloseHandle(mutex)
        time.sleep(1)
    else:
        log.info("another daemon is running; leaving it in charge")
        return 0
    log.info("daemon start, pid %d, dry_run=%s", os.getpid(), args.dry_run)
    try:
        asyncio.run(Daemon(dry_run=args.dry_run).run())
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        _kernel32.CloseHandle(mutex)
    return 0


def cmd_status(_args) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sessions, complete = Registry().read()
    print(f"sessions now: {len(sessions)} live, state {aggregate(sessions)}, files complete: {complete}")
    for s in sessions:
        if s.status == "waiting" or s.waiting_for:
            print(f"  waiting ({s.waiting_for or 'unspecified'}): {s.name or s.pid}")
    for s in sessions:
        if s.status == "busy" and not s.waiting_for:
            print(f"  busy: {s.name or s.pid}")
    try:
        print("daemon:", STATUS_FILE.read_text(encoding="utf-8"))
    except OSError:
        print("daemon: no status file yet")
    print("paused:", PAUSE_FLAG.exists())
    print(night_hours_line())
    return 0


def cmd_scan(args) -> int:
    from bleak import BleakScanner

    async def scan() -> int:
        found = await BleakScanner.discover(timeout=args.seconds, return_adv=True)
        lamps = [(adv.rssi, dev.address, adv.local_name or dev.name or "") for dev, adv in found.values()
                 if (adv.local_name or dev.name or "").upper().startswith(NAME_PREFIX)]
        print(f"{len(found)} Bluetooth devices seen in {args.seconds:.0f} s; lamps: {len(lamps)}")
        for rssi, address, name in sorted(lamps, reverse=True):
            print(f"  {name}  {address}  signal {rssi} dBm")
        return 0 if lamps else 1

    return asyncio.run(scan())


def cmd_off(_args) -> int:
    STATE.mkdir(exist_ok=True)
    PAUSE_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    print("released until `on`: the daemon turns the lamp dark if it is connected, then leaves it to the phone app")
    return 0


def cmd_on(_args) -> int:
    PAUSE_FLAG.unlink(missing_ok=True)
    print("lamp follows Claude Code again")
    print(night_hours_line())
    return 0


def night_hours_line() -> str:
    quiet = QuietHours()
    active = quiet.active()
    if not quiet.label:
        return "night hours: none"
    rule = f"dark after {quiet.idle_minutes:g} min without input" if quiet.idle_minutes else "dark throughout"
    return f"night hours: {quiet.label}, {rule}" + (" (in them now)" if active else "")


def cmd_quiet(args) -> int:
    STATE.mkdir(exist_ok=True)
    if args.start is None:
        print(night_hours_line())
        return 0
    if args.start == "off":
        QUIET_FILE.unlink(missing_ok=True)
        print("no night hours: the lamp follows Claude Code all day")
        return 0
    try:
        start, end = parse_clock(args.start), parse_clock(args.end or "")
    except ValueError as e:
        print(f"{e}; use `quiet 00:30 08:00` or `quiet off`")
        return 2
    label = {"from": f"{start // 60:02d}:{start % 60:02d}", "to": f"{end // 60:02d}:{end % 60:02d}",
             "idle_minutes": args.idle}
    QUIET_FILE.write_text(json.dumps(label), encoding="utf-8")
    print(night_hours_line())
    return 0


def cmd_pin(args) -> int:
    STATE.mkdir(exist_ok=True)
    if args.address is None:
        print("pinned lamp:", load_pinned().get("address") or "none; the strongest signal wins")
        return 0
    if args.address == "off":
        LAMP_FILE.unlink(missing_ok=True)
        print("no pinned lamp; the strongest signal wins")
        return 0
    if not re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", args.address):
        print(f"not a Bluetooth address: {args.address!r}; `scan` lists them")
        return 2
    LAMP_FILE.write_text(json.dumps({"address": args.address.upper()}), encoding="utf-8")
    print("pinned lamp:", args.address.upper(), "(a running daemon uses it from its next connection; restart the task to apply it now)")
    return 0


def cmd_demo(args) -> int:
    commands = args.commands or [LOOKS["idle"], LOOKS["working"], LOOKS["input"]]

    async def demo() -> int:
        from bleak import BleakClient
        for command in commands:
            if not valid_command(command):
                print(f"refused, not a documented command: {command}")
                return 2
        device, name = await find_lamp(args.scan)
        if device is None:
            print("no lamp found; unplugged, joined to a phone, or held by the daemon (run `off` first)")
            return 1
        print(f"lamp: {name} {device.address}")
        async with BleakClient(device, timeout=20.0) as client:
            try:
                print("firmware|mac:", bytes(await client.read_gatt_char(READ_CHAR)).decode("ascii", "replace"))
            except Exception as e:
                print("could not read firmware:", e)
            lit = None
            for command in commands:
                for write in writes_for(command, lit):
                    await client.write_gatt_char(WRITE_CHAR, write.encode("ascii"))
                    await asyncio.sleep(WRITE_GAP_S)
                lit = command != "LEDOFF"
                print(f"showing {command} for {args.each:.0f} s", flush=True)
                await asyncio.sleep(args.each)
            if args.hold:
                await asyncio.sleep(args.hold)
        return 0

    return asyncio.run(demo())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="follow Claude Code and drive the lamp")
    run.add_argument("--dry-run", action="store_true", help="no Bluetooth; print each write")
    run.add_argument("--log-file", action="store_true", help="log to state/lamp.log")
    run.set_defaults(func=cmd_run)
    sub.add_parser("status", help="what the sessions and the daemon are doing").set_defaults(func=cmd_status)
    scan = sub.add_parser("scan", help="look for lamps; sends nothing")
    scan.add_argument("--seconds", type=float, default=15.0)
    scan.set_defaults(func=cmd_scan)
    sub.add_parser("off", help="turn the lamp dark and release it").set_defaults(func=cmd_off)
    sub.add_parser("on", help="follow Claude Code again").set_defaults(func=cmd_on)
    quiet = sub.add_parser("quiet", help="night hours: `quiet 01:00 10:00` sets them, `quiet off` clears them")
    quiet.add_argument("start", nargs="?")
    quiet.add_argument("end", nargs="?")
    quiet.add_argument("--idle", type=float, default=IDLE_MINUTES,
                       help="minutes without input before the lamp goes dark; 0 keeps it dark all through the hours")
    quiet.set_defaults(func=cmd_quiet)
    pin = sub.add_parser("pin", help="use one lamp: `pin <address from scan>`; `pin off` goes back to the strongest signal")
    pin.add_argument("address", nargs="?")
    pin.set_defaults(func=cmd_pin)
    demo = sub.add_parser("demo", help="show looks on the lamp; the daemon must have released it")
    demo.add_argument("commands", nargs="*", help="lamp commands; default: the three status looks")
    demo.add_argument("--each", type=float, default=8.0)
    demo.add_argument("--scan", type=float, default=10.0)
    demo.add_argument("--hold", type=float, default=0.0)
    demo.set_defaults(func=cmd_demo)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
