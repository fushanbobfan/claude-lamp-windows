# claude-lamp-windows

[中文说明](README.zh-CN.md)

A [Moonside](https://developer.moonside.design/) lamp as a status light for [Claude Code](https://code.claude.com/docs/en/overview) on Windows. A small background program, the daemon, follows every Claude Code session on the computer and shows the result on one lamp, so a glance tells you whether anything needs you.

| Lamp | Meaning |
|---|---|
| Strong pink | At least one session is blocked on you: a permission prompt or a question prompt |
| Slow blue wave | At least one session is working, and none is blocked |
| Soft warm white | Nothing is running |
| Dark | Nothing has run for 30 minutes; the lamp lights again as soon as a session starts working or needs you |

This is an unofficial project with no connection to Moonside or Anthropic.

## How it differs from claude-lamp

[claude-lamp](https://github.com/bobek-balinek/claude-lamp) does this on macOS, and the idea here comes from it. This is a separate implementation that shares no code with it:

- **Windows.** The daemon runs from a per-user task in Windows Task Scheduler.
- **No hooks.** claude-lamp registers Claude Code hooks, including one before each tool call. This project reads the status files Claude Code already keeps in `~/.claude/sessions`, so `settings.json` stays as it is, nothing extra runs inside your sessions, and a lamp fault cannot reach them.
- **Many sessions at once.** The lamp shows the most urgent state across all sessions: blocked over working over idle.
- **Night hours.** An optional time window in which the lamp goes dark once nobody is using the computer.

## Setup

You need Windows with Bluetooth LE, Git, Python 3.10 or newer, Claude Code, and a Moonside lamp.

**1. Install.**

```powershell
git clone https://github.com/fushanbobfan/claude-lamp-windows
cd claude-lamp-windows
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

**2. Free the lamp.** While anything is connected to the lamp, Bluetooth scans cannot see it, so the computer cannot find it while the phone app holds it. Swipe the Moonside app away, or turn off Bluetooth on the phone for a moment.

**3. Check that it works.** `scan` lists the lamps in range and sends them nothing. `demo` shows each of the three looks for 8 seconds.

```powershell
.venv\Scripts\python moonside_lamp.py scan
.venv\Scripts\python moonside_lamp.py demo
```

**4. Start it at every logon.** This registers a scheduled task named `moonside-lamp` for your user and starts it now.

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
```

To undo it, run this and then delete the folder:

```powershell
Stop-ScheduledTask -TaskName moonside-lamp; Unregister-ScheduledTask -TaskName moonside-lamp -Confirm:$false
```

## Everyday use

Run each command as `.venv\Scripts\python moonside_lamp.py <command>`.

| Command | Effect |
|---|---|
| `status` | Names the sessions that are blocked or working, and shows what the lamp is doing |
| `off` | Turns the lamp dark and disconnects from it, so the phone app can connect; holds across restarts until `on` |
| `on` | Follows Claude Code again |
| `quiet 01:00 10:00` | Sets night hours, in local time: inside them, after 15 minutes without keyboard or mouse input, the lamp goes dark, pink included, and the daemon disconnects from it; any input brings it back. Add `--idle 0` to keep it dark for the whole window, in use or not. `quiet off` clears them |
| `pin <address>` | Uses that lamp only, with the address `scan` shows, from the daemon's next connection; restart the task to apply it now. `pin off` goes back to the strongest Bluetooth signal, the default |
| `demo "<command>" ...` | Shows lamp commands of your choice, 8 seconds each; run `off` first |

To change the looks, edit `LOOKS` at the top of `moonside_lamp.py` using commands from the [protocol page](https://developer.moonside.design/), then restart the task:

```powershell
Stop-ScheduledTask -TaskName moonside-lamp; Start-ScheduledTask -TaskName moonside-lamp
```

## What to expect

- **The phone app and the daemon take turns.** While the daemon is connected, the lamp is invisible to the app. Run `off` before using the app, and swipe the app away before `on`.
- **Pink means a session is blocked on you.** A session that has finished replying and asked you something in its text shows as idle, since nothing is blocked.
- **The daemon replaces the colour or effect saved in the lamp.** Pick it again in the app if you want it back.
- **Brightness stays yours.** The daemon sends no brightness command, so the lamp's own controls set it.
- **Only Claude Code on Windows counts.** Other coding agents do not affect the lamp, and sessions inside WSL keep their files in Linux, out of the daemon's view. Background sessions count while their process runs. If you set `CLAUDE_CONFIG_DIR`, the daemon reads that folder.
- **The session files are undocumented,** so a future Claude Code may change them. If they all stop carrying a `status` field, the daemon logs a warning and switches to `claude agents --json`, Claude Code's [documented command](https://code.claude.com/docs/en/agent-view) for reading session state from outside, every 15 seconds.
- **When the daemon stops, the lamp stays as it was.** That covers sleep, shutdown and stopping the task. The lamp stores its own state, so expect it to keep its last look until the daemon runs again.

## How it works

Once a second the daemon reads `~/.claude/sessions/*.json`, ignores processes that have exited, and takes the most urgent state. Changes wait 3 seconds to ignore flicker, except pink, which shows at once. Commands are plain ASCII written over Bluetooth LE, and only on a change, since any write interrupts a running animation. The lamp reports nothing back, so every command is first checked against the command list on the [protocol page](https://developer.moonside.design/).

Measured with 38 live sessions, running at Windows' below-normal process priority: under 1% of one CPU core and under 30 MB of memory. The log is in `state/lamp.log`.

Tested with a `MOONSIDE-L1` lamp (firmware 1.0), Windows 11, Python 3.14 and Claude Code 2.1.317 in the desktop app. The protocol page documents one command set for Moonside lamps; other models are untested.

## Tests

```powershell
.venv\Scripts\python -X utf8 -m unittest -v test_moonside_lamp
```

Tests that need Claude Code running on the machine skip themselves otherwise.

## Credits

- [claude-lamp](https://github.com/bobek-balinek/claude-lamp) (MIT), where the idea comes from.
- [moonside-ha](https://github.com/diotav/moonside-ha) (MIT) for the on-device finding that an animation applied to a lit lamp blends with what it shows, so it should follow the power-off command `LEDOFF`.

## License

MIT
