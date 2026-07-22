"""Live terminal dashboard (curses, stdlib only).

Runs the poller locally and redraws every interval. No server required.
Keys: q / Ctrl-C to quit.
"""

from __future__ import annotations

import curses
import time

_HEALTH_COLOR = {"ok": 2, "info": 4, "warning": 3, "critical": 1}


def _bar(value, maximum, width=24):
    if value is None or not maximum or maximum <= 0:
        return "·" * width
    frac = max(0.0, min(1.0, value / maximum))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def _fmt(v, unit=""):
    return f"{v:g}{unit}" if isinstance(v, (int, float)) else "n/a"


def _add(stdscr, y, x, text, attr=0):
    """Write text clipped to the screen, tolerating the bottom-right cell
    (curses raises ERR when the cursor would advance past the last cell)."""
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    avail = w - x - 1          # leave the final cell untouched
    if avail <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, avail, attr)
    except curses.error:
        pass


def _draw(stdscr, monitor, interval):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    snap = monitor.snapshot()

    title = f" gpumon  ·  {len(snap['gpus'])} GPU(s)  ·  backends: " \
            f"{','.join(snap['backends']) or 'none'}  ·  uptime {snap['uptime_s']:.0f}s "
    _add(stdscr, 0, 0, title.ljust(w), curses.A_REVERSE)
    _add(stdscr, 1, 0, time.strftime("  %Y-%m-%d %H:%M:%S")
         + "   (press q to quit)")

    row = 3
    for g in snap["gpus"]:
        if row >= h - 1:
            break
        state = g["health"]["state"]
        color = curses.color_pair(_HEALTH_COLOR.get(state, 0))
        _add(stdscr, row, 0, f"[{g['key']}] {g['name']}", curses.A_BOLD)
        _add(stdscr, row, max(0, w - 12), f"{state.upper():>10}",
             color | curses.A_BOLD)
        row += 1

        temp = g["temp_c"]
        temp_max = max(g.get("hotspot_c") or 0, 100)
        clock, clock_max = g["clock_mhz"], g["max_clock_mhz"]
        power, power_max = g["power_w"], g["power_limit_w"] or g["power_w"]
        load = g["load_pct"]

        lines = [
            ("temp ", _bar(temp, temp_max), _fmt(temp, "C")
                + (f" (hot {_fmt(g['hotspot_c'],'C')})" if g["hotspot_c"] else "")),
            ("load ", _bar(load, 100), _fmt(load, "%")),
            ("clock", _bar(clock, clock_max),
                f"{_fmt(clock,'MHz')} / {_fmt(clock_max,'MHz')}  mem {_fmt(g['mem_clock_mhz'],'MHz')}"),
            ("power", _bar(power, power_max),
                f"{_fmt(power,'W')} / {_fmt(g['power_limit_w'],'W')}"),
            ("fan  ", _bar(g["fan_pct"], 100) if g["fan_pct"] is not None else "·" * 24,
                f"{_fmt(g['fan_rpm'],'rpm')} / {_fmt(g['fan_pct'],'%')}"),
        ]
        for label, bar, text in lines:
            if row >= h - 1:
                break
            _add(stdscr, row, 2, f"{label} {bar} {text}")
            row += 1

        for a in g["health"]["active_alerts"]:
            if row >= h - 1:
                break
            ac = curses.color_pair(_HEALTH_COLOR.get(a["severity"], 1))
            _add(stdscr, row, 4, f"⚠ {a['code']}: {a['message']}", ac)
            row += 1
        row += 1

    _add(stdscr, h - 1, 0, f" refresh {interval:g}s ".ljust(w), curses.A_REVERSE)
    stdscr.refresh()


def _loop(stdscr, monitor, interval):
    curses.curs_set(0)
    curses.use_default_colors()
    for i, fg in ((1, curses.COLOR_RED), (2, curses.COLOR_GREEN),
                  (3, curses.COLOR_YELLOW), (4, curses.COLOR_CYAN)):
        curses.init_pair(i, fg, -1)
    stdscr.nodelay(True)

    while True:
        monitor.poll_once()
        _draw(stdscr, monitor, interval)
        deadline = time.time() + interval
        while time.time() < deadline:
            ch = stdscr.getch()
            if ch in (ord("q"), ord("Q"), 27):
                return
            time.sleep(0.05)


def run(monitor, interval=1.0):
    try:
        curses.wrapper(_loop, monitor, interval)
    except KeyboardInterrupt:
        pass
