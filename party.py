#!/usr/bin/env python3
"""Audio scheduler: plays files on the host's default audio output.

Subcommands:
  daemon       run the scheduler in the foreground (use nohup/systemd to detach)
  status       show what the daemon is doing and what's next
  list         show the configured schedule
  next         show the next event with countdown
  play <name>  play one scheduled event right now (bypasses daemon)
  play-next    play the next scheduled event immediately (testing)
  reload       tell the daemon to reread schedule.json (SIGHUP)
  clock ...    test clock: `clock set "YYYY-MM-DD HH:MM[:SS]"` (or HH:MM),
               `clock off`, `clock` to show
  stop         stop the daemon (SIGTERM)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import random
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python <3.9; daemon target is Ubuntu, so this is just a safety net
    ZoneInfo = None  # type: ignore
    ZoneInfoNotFoundError = Exception  # type: ignore

AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus"}


def parse_hhmm(s: str) -> str:
    """Validate and normalize a 24h time. '5:0' -> '05:00'. Rejects AM/PM, >23h, >59m."""
    s = s.strip()
    if ":" not in s:
        raise ValueError(f"time '{s}' must be HH:MM (24h)")
    hh_s, mm_s = s.split(":", 1)
    if not hh_s.isdigit() or not mm_s.isdigit():
        raise ValueError(f"time '{s}' must be HH:MM (24h, digits only — no AM/PM)")
    hh, mm = int(hh_s), int(mm_s)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"time '{s}' out of range (hours 0-23, minutes 0-59)")
    return f"{hh:02d}:{mm:02d}"


def parse_time_field(s: str) -> str:
    """Normalize a time field that may be 'HH:MM' or 'HH:MM-HH:MM'."""
    s = s.strip()
    if "-" in s:
        a, b = s.split("-", 1)
        return f"{parse_hhmm(a)}-{parse_hhmm(b)}"
    return parse_hhmm(s)


def parse_days(raw) -> list:
    """Normalize day names to lowercase 3-letter form; reject unknown values."""
    out = []
    for d in raw:
        norm = str(d).strip().lower()[:3]
        if norm not in DAYS:
            raise ValueError(f"unknown day '{d}' (use {DAYS})")
        out.append(norm)
    return out

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "schedule.json"
STATE_PATH = ROOT / "state.json"
PID_PATH = ROOT / "party.pid"
LOG_PATH = ROOT / "party.log"
CLOCK_PATH = ROOT / "clock.json"   # test clock, see clock_offset()

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Display/scheduling zone. Set from schedule.json's "timezone" field.
# `None` means "use the system's local time" (the host's /etc/localtime).
_TZ: Optional["ZoneInfo"] = None
_TZ_NAME: str = "local"


def set_timezone(name: Optional[str]) -> None:
    """Configure the zone used by every wall-clock comparison and timestamp.

    Accepts an IANA name like 'Europe/Chisinau'. Pass None / '' / 'local' to
    fall back to the host's local time. Invalid names are logged and ignored
    (we keep the previous setting) so a typo in schedule.json doesn't kill
    the daemon."""
    global _TZ, _TZ_NAME
    if not name or name.lower() == "local":
        _TZ = None
        _TZ_NAME = "local"
        return
    if ZoneInfo is None:
        log(f"zoneinfo module unavailable; ignoring timezone={name!r}")
        return
    try:
        _TZ = ZoneInfo(name)
        _TZ_NAME = name
    except ZoneInfoNotFoundError as e:
        log(f"unknown timezone {name!r}: {e}; keeping {_TZ_NAME!r}")


def real_now_local() -> datetime:
    """Real wall-clock time in the configured zone, ignoring the test clock."""
    if _TZ is None:
        return datetime.now()
    return datetime.now(tz=_TZ).replace(tzinfo=None)


def now_local() -> datetime:
    """Naive wall-clock datetime in the configured zone.

    Returned naive (no tzinfo) so it composes with the existing `HH:MM`
    arithmetic (`replace(hour=...)`, `timedelta(days=...)`, direct
    subtraction). All scheduling and persisted timestamps go through here,
    so the test clock (clock.json) shifts all of them."""
    off = clock_offset()
    return real_now_local() + timedelta(seconds=off) if off else real_now_local()


# ---------- Test clock -------------------------------------------------------
# clock.json holds {"offset": seconds, "set_to": "..."}: while it exists the
# scheduler runs `offset` seconds ahead of (or behind) real time. The clock
# keeps ticking from the time you set. It's a file rather than a global so an
# external `party.py daemon` and the web UI see the same clock.

_clock_cache: tuple = (None, 0.0)   # (clock.json mtime_ns, offset)


def clock_offset() -> float:
    """Seconds the test clock is ahead of real time; 0.0 when it's off.
    Cached on mtime — now_local() calls this in every scheduler loop."""
    global _clock_cache
    try:
        mtime = CLOCK_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        return 0.0
    if _clock_cache[0] != mtime:
        try:
            off = float(json.loads(CLOCK_PATH.read_text()).get("offset", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            off = 0.0
        _clock_cache = (mtime, off)
    return _clock_cache[1]


def test_clock_on() -> bool:
    return CLOCK_PATH.exists()


_CLOCK_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                  "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")


def parse_clock_time(s: str) -> datetime:
    """'YYYY-MM-DD HH:MM[:SS]', or just 'HH:MM[:SS]' meaning today (real date)."""
    s = (s or "").strip()
    for fmt in _CLOCK_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return real_now_local().replace(hour=t.hour, minute=t.minute,
                                        second=t.second, microsecond=0)
    raise ValueError(f"time {s!r} must be 'YYYY-MM-DD HH:MM[:SS]' or 'HH:MM[:SS]' (24h)")


def apply_config_timezone() -> None:
    """Apply schedule.json's timezone (which the test clock is relative to)
    without validating the rest of the schedule."""
    try:
        set_timezone(json.loads(CONFIG_PATH.read_text()).get("timezone"))
    except (OSError, ValueError, AttributeError):
        pass


def set_test_clock(target: Optional[datetime]) -> None:
    """Start the test clock at `target` (it keeps ticking), or turn it off with
    None. The caller tells the daemon to reload so it re-plans from the new time."""
    if target is None:
        CLOCK_PATH.unlink(missing_ok=True)
        log("test clock off — back to real time")
        return
    data = {"offset": (target - real_now_local()).total_seconds(),
            "set_to": target.strftime("%Y-%m-%d %H:%M:%S")}
    tmp = CLOCK_PATH.with_suffix(CLOCK_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, CLOCK_PATH)
    log(f"test clock set to {data['set_to']}")


def log(msg: str) -> None:
    tag = " test-clock" if clock_offset() else ""
    line = f"[{now_local().isoformat(timespec='seconds')}{tag}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run(cmd: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """Run a shell command. Always time-boxed: a child that never exits would
    otherwise freeze the scheduler thread until the process is restarted."""
    try:
        return subprocess.run(cmd, shell=True, text=True, capture_output=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        log(f"command timed out after {timeout:.0f}s: {cmd}")
        return subprocess.CompletedProcess(cmd, 124, "", "timed out")


# ---------- Audio ------------------------------------------------------------
# Everything plays on the system's default output (headphone jack / built-in
# speakers, whatever the OS has selected). Linux goes through pipewire/pulse
# (pw-play, pactl); macOS through afplay and osascript.

DEFAULT_SINK = "@DEFAULT_SINK@"


def set_volume(percent: int) -> bool:
    """Set the default output's (master) volume. play_event() sets it to
    each event's volume."""
    if sys.platform == "darwin":
        # macOS has no boost past 100%.
        pct = max(0, min(100, int(percent)))
        res = run(f"osascript -e 'set volume output volume {pct}'")
    else:
        pct = max(0, min(150, int(percent)))
        res = run(f"pactl set-sink-volume {DEFAULT_SINK} {pct}%")
    if res.returncode != 0:
        log(f"set volume failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.returncode == 0


def get_volume() -> Optional[int]:
    """Return the default output's volume as a percent, or None if unreadable."""
    if sys.platform == "darwin":
        res = run("osascript -e 'output volume of (get volume settings)'")
        # 'missing value' for outputs with no software volume (e.g. HDMI).
        out = res.stdout.strip()
        return int(out) if res.returncode == 0 and out.isdigit() else None
    res = run(f"pactl get-sink-volume {DEFAULT_SINK}")
    if res.returncode != 0:
        return None
    # Output line looks like:  Volume: front-left: 32768 / 50% / -18.06 dB, front-right: ...
    m = re.search(r"(\d+)%", res.stdout)
    return int(m.group(1)) if m else None


def _player_cmds(path: Path, volume_pct: Optional[int]) -> list[list[str]]:
    """Command lines for every installed player, best first. Each applies
    volume_pct as a stream gain (aplay can't, so it plays at full level)."""
    vol = None if volume_pct is None else max(0, min(150, int(volume_pct))) / 100.0
    cmds = []
    if shutil.which("pw-play"):
        cmds.append(["pw-play"] + ([f"--volume={vol:.3f}"] if vol is not None else []))
    if shutil.which("afplay"):
        cmds.append(["afplay"] + (["-v", f"{vol:.3f}"] if vol is not None else []))
    if shutil.which("ffplay"):
        cmds.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
                    + (["-volume", str(min(100, round(vol * 100)))] if vol is not None else []))
    if shutil.which("aplay"):
        cmds.append(["aplay"])
    return [c + [str(path)] for c in cmds]


_play_proc: Optional[subprocess.Popen] = None
# Set by _terminate_play() so the caller knows the player exited because we
# killed it, not because of a program error. Cleared on each _spawn() entry.
# Without this, the SIGTERM that stops the daemon (or a preempt) kills the
# player with rc<0, play_file treats that as 'player failed', and restarts the
# audio on the next player — leaving it playing until the whole file finishes.
_aborted: bool = False


def _spawn(cmd: list[str]) -> int:
    """Run a player as a tracked subprocess so we can terminate it on signal."""
    global _play_proc, _aborted
    if _stop:
        # Shutting down — don't bother starting another player.
        return 0
    _aborted = False
    try:
        _play_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log(f"{cmd[0]}: {e}")
        return 127
    try:
        rc = _play_proc.wait()
    finally:
        _play_proc = None
    return rc


def play_file(path: Path, volume_pct: Optional[int] = None) -> bool:
    """Play a file on the default output. volume_pct is applied as a STREAM
    gain by the player, so each event's loudness doesn't touch the master
    volume."""
    if not path.exists():
        log(f"file not found: {path}")
        return False
    cmds = _player_cmds(path, volume_pct)
    if not cmds:
        log("no audio player found (install pipewire, ffmpeg or alsa-utils)")
        return False
    rc = 1
    for cmd in cmds:
        rc = _spawn(cmd)
        # Move on to the next player only when this one *actually failed*
        # (e.g. afplay can't decode .ogg) — not when we deliberately killed it
        # (shutdown / preempt / clock change). rc < 0 means killed by signal.
        if rc == 0 or rc < 0 or _aborted or _stop:
            break
        log(f"{cmd[0]} failed (rc={rc}) on {path.name}")
    return rc == 0


# ---------- Config + scheduling ---------------------------------------------

@dataclass
class Event:
    name: str
    time: str                       # "HH:MM" or "HH:MM-HH:MM" (window)
    file: Optional[str] = None      # single file to play
    folder: Optional[str] = None    # OR a folder — random pick per play
    volume: int = 80                # percent, set as the system volume when the event starts
    repeat: int = 1                 # how many times to play back-to-back (ignored if window is set)
    repeat_gap: tuple = (0.0, 0.0)  # (min, max) seconds of silence between repeats
    days: list = field(default_factory=lambda: list(DAYS))
    enabled: bool = True

    def parsed_time(self) -> tuple[str, Optional[str]]:
        """Return (start "HH:MM", end "HH:MM" or None)."""
        s = self.time.strip()
        if "-" in s:
            a, b = s.split("-", 1)
            return a.strip(), b.strip()
        return s, None

    def source(self) -> str:
        return self.folder or self.file or "?"

    def once(self) -> "Event":
        """Copy that plays a single time: no window, no repeats (a folder
        still picks one random file)."""
        start, _ = self.parsed_time()
        return replace(self, time=start, repeat=1, repeat_gap=(0.0, 0.0))

    def next_run(self, after: datetime) -> Optional[datetime]:
        start_s, end_s = self.parsed_time()
        sh, sm = (int(x) for x in start_s.split(":"))

        # Windowed events: if `after` is currently inside today's window,
        # fire right now instead of waiting for next start. Without this,
        # starting the daemon mid-window (or chaining from a back-to-back
        # event that ended on the same minute) skips the window entirely.
        if end_s is not None:
            eh, em = (int(x) for x in end_s.split(":"))
            start_today = after.replace(hour=sh, minute=sm, second=0, microsecond=0)
            end_today = after.replace(hour=eh, minute=em, second=0, microsecond=0)
            if end_today <= start_today:
                # Window crosses midnight (e.g. 21:00-01:00). Two cases:
                # window that started today, or window that started yesterday.
                if after >= start_today and DAYS[start_today.weekday()] in self.days:
                    return after
                start_yday = start_today - timedelta(days=1)
                if start_yday <= after < end_today and DAYS[start_yday.weekday()] in self.days:
                    return after
            else:
                if start_today <= after < end_today and DAYS[start_today.weekday()] in self.days:
                    return after

        for d in range(0, 8):
            cand = (after + timedelta(days=d)).replace(
                hour=sh, minute=sm, second=0, microsecond=0
            )
            if cand <= after:
                continue
            if DAYS[cand.weekday()] in self.days:
                return cand
        return None


def parse_gap(raw) -> tuple[float, float]:
    """Accept a single number or a [min, max] list. Returns (min, max)."""
    if raw is None:
        return (0.0, 0.0)
    if isinstance(raw, (list, tuple)):
        if len(raw) != 2:
            raise ValueError(f"repeat_gap range must have 2 values, got {raw}")
        lo, hi = float(raw[0]), float(raw[1])
        if lo > hi:
            lo, hi = hi, lo
        return (max(0.0, lo), max(0.0, hi))
    v = float(raw)
    return (v, v)


def sample_gap(rng: tuple[float, float]) -> float:
    lo, hi = rng
    return random.uniform(lo, hi) if hi > lo else lo


def pick_audio(ev: Event) -> Optional[Path]:
    """Return the path to play for this event. Random pick if folder is set."""
    if ev.folder:
        folder = Path(ev.folder) if os.path.isabs(ev.folder) else ROOT / ev.folder
        if not folder.is_dir():
            log(f"folder not found: {folder}")
            return None
        candidates = [p for p in folder.iterdir()
                      if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
        if not candidates:
            log(f"no audio files in {folder}")
            return None
        return random.choice(candidates)
    if ev.file:
        return Path(ev.file) if os.path.isabs(ev.file) else ROOT / ev.file
    return None


SAMPLE_CONFIG = {
    # IANA name — controls every wall-clock comparison and persisted timestamp.
    # "local" (or omit) follows the host's /etc/localtime. Example overrides:
    # "Europe/Chisinau", "Europe/Berlin", "America/New_York".
    "timezone": "local",
    "schedule": [
        {"name": "alarm-5", "time": "05:00", "file": "test.wav", "volume": 80,
         "repeat": 3, "repeat_gap": 1.0},
        {"name": "alarm-6", "time": "06:00", "file": "test.wav", "volume": 90,
         "repeat": 3, "repeat_gap": 1.0},
        {"name": "alarm-7", "time": "07:00", "file": "test.wav", "volume": 100,
         "repeat": 5, "repeat_gap": [0.5, 3.0]},
        {"name": "morning-music", "time": "09:00-10:00", "folder": "music",
         "volume": 60, "repeat_gap": [5, 30],
         "days": ["mon", "tue", "wed", "thu", "fri"]},
    ],
}


def load_config() -> list[Event]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(SAMPLE_CONFIG, indent=2))
        log(f"created sample config at {CONFIG_PATH}")
    cfg = json.loads(CONFIG_PATH.read_text())
    set_timezone(cfg.get("timezone"))
    events = []
    for item in cfg.get("schedule", []):
        if not item.get("file") and not item.get("folder"):
            raise ValueError(f"event '{item.get('name')}': need either 'file' or 'folder'")
        events.append(Event(
            name=item["name"],
            time=parse_time_field(item["time"]),
            file=item.get("file"),
            folder=item.get("folder"),
            volume=int(item.get("volume", 80)),
            repeat=max(1, int(item.get("repeat", 1))),
            repeat_gap=parse_gap(item.get("repeat_gap", 0)),
            days=parse_days(item.get("days", DAYS)),
            enabled=bool(item.get("enabled", True)),
        ))
    return events


def find_next(events: list[Event], after: datetime,
              skip_name: Optional[str] = None,
              skip_until: Optional[datetime] = None) -> Optional[tuple[Event, datetime]]:
    upcoming = []
    for ev in events:
        if not ev.enabled:
            continue
        if (skip_name == ev.name and skip_until is not None
                and after < skip_until):
            continue
        nxt = ev.next_run(after)
        if nxt:
            upcoming.append((ev, nxt))
    return min(upcoming, key=lambda x: x[1]) if upcoming else None


# How late a point event may start when it came due while another event was
# still playing (e.g. a 06:00 alarm right as a 00:00-06:00 window's last track
# ends). Later than this and it's skipped rather than fired out of context.
MISSED_GRACE = timedelta(minutes=5)


def find_missed(events: list[Event], since: datetime,
                now: datetime) -> Optional[tuple[Event, datetime]]:
    """Earliest single-time event (no window) that was due in (since, now].
    Windowed events don't need this: find_next() fires them while their
    window is still open."""
    best = None
    for ev in events:
        if not ev.enabled or ev.parsed_time()[1] is not None:
            continue
        due = ev.next_run(since)
        if due is None or due > now:
            continue
        if now - due > MISSED_GRACE:
            log(f"{ev.name}: missed (due {due:%Y-%m-%d %H:%M}, busy until "
                f"{now:%H:%M:%S}), skipping")
            continue
        if best is None or due < best[1]:
            best = (ev, due)
    return best


def write_state(state: dict) -> None:
    """Atomic write: temp file + rename, so concurrent readers never see a partial JSON."""
    # Per-writer temp name: a preempted play_event and the one replacing it
    # write at the same moment, and a shared .tmp gets renamed out from under one.
    tmp = STATE_PATH.with_suffix(f"{STATE_PATH.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    os.replace(tmp, STATE_PATH)


# ---------- Daemon -----------------------------------------------------------

_reload = False
_stop = False

# Serializes play_event across threads inside this process. Only one event
# (scheduled or manual) can be playing at a time. A new request that wants
# the slot can preempt the current player.
_play_lock = threading.Lock()
_preempt_requested = False


def _on_reload(signum, frame):
    global _reload
    _reload = True


def _terminate_play() -> None:
    """Terminate the current play subprocess if any. Snapshots the global to
    avoid races with the daemon thread clearing _play_proc in _spawn's finally.

    Sets _aborted so play_file() knows not to restart audio on the aplay
    fallback path."""
    global _aborted
    _aborted = True
    p = _play_proc
    if p is None:
        return
    try:
        p.terminate()
    except (ProcessLookupError, OSError):
        pass


def _on_stop(signum, frame):
    global _stop
    _stop = True
    _terminate_play()


def play_event(ev: Event, state: dict) -> None:
    """Play an event with the system (master) volume set to the event's volume,
    then put the volume back to what it was. Files play at full stream level
    so the two don't multiply."""
    before = get_volume()
    set_volume(ev.volume)
    applied = get_volume()
    log(f"{ev.name}: system volume {before}% -> {ev.volume}% (reads back {applied}%)")
    try:
        _play_event(ev, state)
    finally:
        # Leave it alone if someone moved the volume mid-event (UI slider,
        # keyboard): their change wins over the pre-event level.
        now_vol = get_volume()
        if before is None:
            pass
        elif now_vol != applied:
            log(f"{ev.name}: volume changed during event ({applied}% -> {now_vol}%), not restoring")
        else:
            set_volume(before)
            log(f"{ev.name}: system volume restored to {before}%")


def _play_event(ev: Event, state: dict) -> None:
    state.update({
        "mode": "starting",
        "current": {"name": ev.name, "source": ev.source(), "volume": ev.volume},
        "started_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
        # Clear "next" so the UI stops showing the same event in both
        # "Now" and "Next: … firing…" while play_event is running. The
        # daemon's main loop will recompute and rewrite it after play_event.
        "next": None,
    })
    write_state(state)

    _, end_s = ev.parsed_time()
    end_dt = None
    if end_s:
        hh, mm = (int(x) for x in end_s.split(":"))
        now = now_local()
        end_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if end_dt <= now:
            end_dt += timedelta(days=1)
        log(f"{ev.name}: window mode, playing until {end_dt.strftime('%Y-%m-%d %H:%M')}")
    else:
        log(f"{ev.name}: playing from {ev.source()} at {ev.volume}% x{ev.repeat}")

    all_ok = True
    played = 0
    fail_reason: Optional[str] = None
    # A test-clock change mid-event invalidates end_dt and the plan; stop so
    # the daemon re-plans from the new time.
    clock0 = clock_offset()
    i = 0
    while True:
        i += 1
        if end_dt is None and i > ev.repeat:
            break
        if end_dt is not None and now_local() >= end_dt:
            break
        if _preempt_requested:
            all_ok = False
            fail_reason = fail_reason or "preempted"
            log(f"{ev.name}: preempted")
            break
        if clock_offset() != clock0:
            all_ok = False
            fail_reason = fail_reason or "clock-changed"
            log(f"{ev.name}: test clock changed, stopping")
            break

        path = pick_audio(ev)
        if not path:
            all_ok = False
            fail_reason = fail_reason or "no-audio"
            log(f"{ev.name}: nothing to play")
            break

        of_str = "∞" if end_dt else str(ev.repeat)
        state["mode"] = "playing"
        state["current"] = {
            "name": ev.name, "source": ev.source(), "file": str(path.name),
            "volume": ev.volume, "iteration": i, "of": of_str,
            "window_end": end_dt.strftime("%Y-%m-%d %H:%M") if end_dt else None,
        }
        write_state(state)
        log(f"{ev.name}: iteration {i}/{of_str} -> {path.name}")
        ok = play_file(path)
        if ok:
            played += 1
            all_ok = all_ok and ok
            log(f"{ev.name}: iteration {i}/{of_str} ok=True")
        elif _stop or _aborted:
            # We were deliberately interrupted (stop / preempt / clock change)
            # — don't retry, just unwind play_event so the daemon can move on.
            all_ok = False
            fail_reason = "stop" if _stop else "aborted"
            log(f"{ev.name}: iteration {i}/{of_str} aborted ({fail_reason})")
            break
        else:
            all_ok = False
            fail_reason = fail_reason or "play-failed"
            log(f"{ev.name}: iteration {i}/{of_str} ok=False")
            if end_dt is None:
                break
            # Window mode: keep the window going, but don't spin on a
            # player that fails instantly.
            _sleep_interruptible(5)

        if end_dt is None and i >= ev.repeat:
            break
        if end_dt is not None and now_local() >= end_dt:
            break
        gap = sample_gap(ev.repeat_gap)
        if gap > 0:
            log(f"{ev.name}: gap {gap:.2f}s")
            gap_end = time.time() + gap
            while (not _stop and not _preempt_requested
                   and clock_offset() == clock0
                   and not (end_dt is not None and now_local() >= end_dt)):
                remaining = gap_end - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))
        if _stop:
            break
        if end_dt is not None and now_local() >= end_dt:
            break

    last_entry = {
        "name": ev.name,
        "time": now_local().strftime("%Y-%m-%d %H:%M:%S"),
        "ok": all_ok,
        "played": played,
    }
    if not all_ok:
        last_entry["error"] = fail_reason or "unknown"
    state.update({
        "mode": "idle",
        "current": None,
        "started_at": None,
        "last": last_entry,
    })
    write_state(state)


def play_event_exclusive(ev: Event, state: dict, preempt: bool = True) -> bool:
    """Single playback slot. Acquires the global play lock (preempting any
    active player if `preempt=True`) and runs play_event. Returns True if it
    ran, False if rejected (slot busy and preempt=False)."""
    global _preempt_requested
    if preempt:
        if not _play_lock.acquire(blocking=False):
            log(f"{ev.name}: preempting active playback")
            _preempt_requested = True
            _terminate_play()
            _play_lock.acquire()  # waits for the previous play_event to unwind
        _preempt_requested = False
    else:
        if not _play_lock.acquire(timeout=2):
            log(f"{ev.name}: rejected — another play is in progress")
            return False
    try:
        play_event(ev, state)
    finally:
        _play_lock.release()
    return True


def daemon_main(from_thread: bool = False) -> int:
    """Run the scheduler loop. When `from_thread` is True, skip signal setup
    and the 'already running' check — the caller (e.g. web.py) controls the
    `_reload`/`_stop` flags directly."""
    global _reload, _stop

    if not from_thread:
        existing = daemon_pid()
        if existing:
            print(f"daemon already running (pid {existing})", file=sys.stderr)
            return 1
        signal.signal(signal.SIGHUP, _on_reload)
        signal.signal(signal.SIGTERM, _on_stop)
        signal.signal(signal.SIGINT, _on_stop)

    PID_PATH.write_text(str(os.getpid()))
    state: dict = {"mode": "idle", "current": None, "pid": os.getpid()}
    try:
        events = load_config()
        state["tz"] = _TZ_NAME
        log(f"daemon up pid={os.getpid()} events={len(events)} tz={_TZ_NAME}")
        idle_every = 10  # seconds between re-checks with nothing scheduled
        # Track the most recently fired windowed event so we don't re-fire
        # it in a tight loop if play_event returned before window end.
        skip_name: Optional[str] = None
        skip_until: Optional[datetime] = None
        # After an event, plan from when it was due rather than from when it
        # finished, so events that came due while it played aren't skipped.
        plan_from: Optional[datetime] = None
        clock_seen = clock_offset()

        while not _stop:
            if clock_offset() != clock_seen:
                # Test clock moved: the skip window was computed on the old
                # timeline, so drop it and re-plan from the new time.
                clock_seen = clock_offset()
                skip_name, skip_until = None, None
                plan_from = None
                log(f"clock changed, re-planning from {now_local():%Y-%m-%d %H:%M:%S}")
            if _reload:
                try:
                    events = load_config()
                    state["tz"] = _TZ_NAME
                    log(f"reload ok, {len(events)} events tz={_TZ_NAME}")
                except Exception as e:
                    log(f"reload failed: {e}")
                _reload = False

            now_ = now_local()
            nxt = find_missed(events, plan_from, now_) if plan_from else None
            if nxt:
                log(f"{nxt[0].name}: was due at {nxt[1]:%H:%M:%S} while busy, playing now")
            else:
                nxt = find_next(events, now_, skip_name, skip_until)
            plan_from = None
            if not nxt:
                state.update({"mode": "idle", "next": None})
                write_state(state)
                _sleep_interruptible(idle_every)
                continue

            ev, when = nxt
            state["next"] = {
                "name": ev.name, "time": when.strftime("%Y-%m-%d %H:%M"),
                "source": ev.source(), "volume": ev.volume, "repeat": ev.repeat,
            }
            write_state(state)

            while not _stop and not _reload and clock_offset() == clock_seen:
                remaining = (when - now_local()).total_seconds()
                if remaining <= 0:
                    break
                # 1s steps so a test-clock jump is picked up promptly.
                time.sleep(min(1, max(0.2, remaining)))

            if _stop or _reload or clock_offset() != clock_seen:
                continue

            play_event_exclusive(ev, state, preempt=True)
            if clock_offset() == clock_seen:
                plan_from = when

            # If this was a windowed event, suppress re-firing it until
            # its window has closed (covers early exits like missing files).
            # Measured from `when` (inside the window that just played), not
            # from now: a window that ran to its end would otherwise get
            # tomorrow's end and skip tomorrow's window.
            _, end_s = ev.parsed_time()
            if end_s is not None:
                eh, em = (int(x) for x in end_s.split(":"))
                window_end = when.replace(hour=eh, minute=em, second=0, microsecond=0)
                if window_end <= when:
                    window_end += timedelta(days=1)
                skip_name, skip_until = ev.name, window_end
            else:
                skip_name, skip_until = None, None
    finally:
        try:
            PID_PATH.unlink()
        except FileNotFoundError:
            pass
        write_state({"mode": "stopped"})
        log("daemon stopped")
    return 0


def _sleep_interruptible(seconds: int) -> None:
    end = time.time() + seconds
    clock0 = clock_offset()
    while time.time() < end and not _stop and not _reload and clock_offset() == clock0:
        time.sleep(1)


# ---------- CLI --------------------------------------------------------------

def daemon_pid() -> Optional[int]:
    if not PID_PATH.exists():
        return None
    try:
        pid = int(PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def cmd_status(_args) -> int:
    pid = daemon_pid()
    print(f"daemon: {'running pid=' + str(pid) if pid else 'NOT running'}")
    if test_clock_on():
        print(f"clock:  TEST {now_local():%Y-%m-%d %H:%M:%S} "
              f"(real {real_now_local():%H:%M:%S}; `party.py clock off` to stop)")
    if not STATE_PATH.exists():
        return 0
    s = json.loads(STATE_PATH.read_text())
    print(f"mode:   {s.get('mode')}")
    cur = s.get("current")
    if cur:
        iter_info = ""
        of = cur.get("of", 1)
        if of != 1:
            iter_info = f" [{cur.get('iteration')}/{of}]"
        if cur.get("window_end"):
            we = cur["window_end"].replace("T", " ")[:16]
            iter_info += f" until {we}"
        file_info = f" file={cur['file']}" if cur.get("file") else ""
        print(f"now:    {cur['name']}{iter_info}  src={cur.get('source','?')}"
              f"{file_info} vol={cur['volume']}% since {s.get('started_at')}")
    nxt = s.get("next")
    if nxt:
        try:
            dt = datetime.strptime(nxt["time"], "%Y-%m-%d %H:%M")
            delta = dt - now_local()
            ago = str(delta).split(".")[0]
            print(f"next:   {nxt['name']} at {nxt['time']} (in {ago})  "
                  f"src={nxt.get('source','?')} vol {nxt['volume']}% "
                  f"x{nxt.get('repeat',1)}")
        except Exception:
            print(f"next:   {nxt}")
    last = s.get("last")
    if last:
        flag = "OK" if last.get("ok") else f"FAIL ({last.get('error','?')})"
        played = last.get("played")
        played_str = f"  played={played}" if played is not None else ""
        print(f"last:   {last['name']} at {last['time']} {flag}{played_str}")
    return 0


def cmd_list(_args) -> int:
    events = load_config()
    print(f"  {'name':<14} {'time':<12} {'vol':<4} {'rpt':<4} {'days':<22} source")
    for ev in events:
        days = "every day" if len(ev.days) == 7 else ",".join(ev.days)
        mark = " " if ev.enabled else "x"
        rpt = f"x{ev.repeat}" if ev.repeat > 1 else ("∞" if "-" in ev.time else "-")
        src = f"{ev.folder}/ (random)" if ev.folder else ev.file
        print(f"{mark} {ev.name:<14} {ev.time:<12} {ev.volume:<4} {rpt:<4} {days:<22} {src}")
    return 0


def cmd_next(_args) -> int:
    events = load_config()
    nxt = find_next(events, now_local())
    if not nxt:
        print("no scheduled events")
        return 0
    ev, when = nxt
    delta = when - now_local()
    print(f"{ev.name} at {when.strftime('%Y-%m-%d %H:%M')}  "
          f"(in {str(delta).split('.')[0]})  "
          f"src={ev.source()} vol={ev.volume}% repeat={ev.repeat}")
    return 0


def cmd_play(args) -> int:
    events = load_config()
    target = next((e for e in events if e.name == args.name), None)
    if not target:
        print(f"no event named '{args.name}'", file=sys.stderr)
        return 1
    if daemon_pid():
        print("warning: daemon is running; live state will be clobbered "
              "by this manual play", file=sys.stderr)
    play_event_exclusive(target, {"mode": "idle"}, preempt=True)
    return 0


def cmd_play_next(_args) -> int:
    events = load_config()
    nxt = find_next(events, now_local())
    if not nxt:
        print("no scheduled events", file=sys.stderr)
        return 1
    ev, when = nxt
    print(f"playing next event '{ev.name}' (was scheduled for "
          f"{when.strftime('%Y-%m-%d %H:%M')})")
    if daemon_pid():
        print("warning: daemon is running; it may also fire this event "
              "and the live state will be clobbered", file=sys.stderr)
    play_event(ev, {"mode": "idle"})
    return 0


def cmd_clock(args) -> int:
    apply_config_timezone()
    if args.clock_cmd == "set":
        try:
            target = parse_clock_time(" ".join(args.time))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        set_test_clock(target)
    elif args.clock_cmd == "off":
        set_test_clock(None)
    # The daemon notices clock.json changing on its own (within ~1s).
    if test_clock_on():
        print(f"test clock: {now_local():%Y-%m-%d %H:%M:%S}  "
              f"(real {real_now_local():%Y-%m-%d %H:%M:%S}, tz {_TZ_NAME})")
    else:
        print(f"real clock: {now_local():%Y-%m-%d %H:%M:%S}  (tz {_TZ_NAME})")
    return 0


def cmd_reload(_args) -> int:
    pid = daemon_pid()
    if not pid:
        print("daemon not running", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGHUP)
    print("reload signalled")
    return 0


def cmd_stop(_args) -> int:
    pid = daemon_pid()
    if not pid:
        print("daemon not running", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    print("stop signalled")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="party")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("daemon", help="run the scheduler")
    sub.add_parser("status", help="show current state")
    sub.add_parser("list",   help="show schedule")
    sub.add_parser("next",   help="show next event")
    pp = sub.add_parser("play", help="play a named event now")
    pp.add_argument("name")
    sub.add_parser("play-next", help="play the next scheduled event right now (test)")
    sub.add_parser("reload", help="signal daemon to reload schedule.json")
    cp = sub.add_parser("clock", help="show / set / turn off the test clock")
    csub = cp.add_subparsers(dest="clock_cmd")
    csub.add_parser("set", help="run the scheduler as if it were TIME (keeps ticking)").add_argument(
        "time", nargs="+", help="'YYYY-MM-DD HH:MM[:SS]' or 'HH:MM[:SS]' for today")
    csub.add_parser("off", help="back to real time")
    sub.add_parser("stop",   help="stop daemon")

    args = p.parse_args()
    dispatch = {
        "daemon": lambda a: daemon_main(),
        "status": cmd_status,
        "list":   cmd_list,
        "next":   cmd_next,
        "play":   cmd_play,
        "play-next": cmd_play_next,
        "reload": cmd_reload,
        "clock":  cmd_clock,
        "stop":   cmd_stop,
    }
    return dispatch[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
