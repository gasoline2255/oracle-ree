#!/usr/bin/env python3
"""
OracleREE TUI — Trustless oracle grounding for Gensyn Delphi settlement.
Run: python3 ree.py
"""

from __future__ import annotations

import curses
import os
import queue
import shlex
import subprocess
import threading
from pathlib import Path


# ─── Load .env.local ─────────────────────────────────────────────────────────

def load_env(env_file: Path) -> None:
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


ENV_FILE = Path(__file__).parent / ".env.local"
load_env(ENV_FILE)


# ─── First-run setup ─────────────────────────────────────────────────────────

def run_setup(stdscr: curses.window) -> bool:
    """Interactive setup wizard — runs if no .env.local found."""
    curses.echo()
    try:
        curses.curs_set(1)
    except curses.error:
        pass

    stdscr.clear()
    h, w = stdscr.getmaxyx()

    lines = [
        "Welcome to OracleREE",
        "",
        "No configuration found. Let's get you set up.",
        "All keys are free — takes under 2 minutes.",
        "",
        "Step 1/3 — Delphi API Key (required)",
        "Get yours at: https://api-access.delphi.fyi/",
    ]
    for i, line in enumerate(lines):
        try:
            stdscr.addstr(2 + i, 2, line[:w-4])
        except curses.error:
            pass
    stdscr.refresh()

    def prompt_input(y: int, label: str) -> str:
        try:
            stdscr.addstr(y, 2, f"{label}: ")
            stdscr.refresh()
        except curses.error:
            pass
        buf = ""
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue
            if ch in ("\n", "\r"):
                break
            if isinstance(ch, str) and ch in ("\x7f", "\x08"):
                buf = buf[:-1]
                try:
                    y_cur, x_cur = stdscr.getyx()
                    stdscr.addstr(y_cur, 2 + len(label) + 2, " " * (len(buf) + 2))
                    stdscr.addstr(y_cur, 2 + len(label) + 2, buf)
                    stdscr.refresh()
                except curses.error:
                    pass
            elif isinstance(ch, str) and ch.isprintable():
                buf += ch
                try:
                    stdscr.addstr(y, 2 + len(label) + 2, buf)
                    stdscr.refresh()
                except curses.error:
                    pass
        return buf.strip()

    delphi_key = prompt_input(10, "DELPHI_API_ACCESS_KEY")
    if not delphi_key:
        return False

    try:
        stdscr.addstr(12, 2, "Step 2/3 — Groq API Key (optional — better verdicts)"[:w-4])
        stdscr.addstr(13, 2, "Get yours at: https://console.groq.com"[:w-4])
        stdscr.refresh()
    except curses.error:
        pass
    groq_key = prompt_input(15, "GROQ_API_KEY (Enter to skip)")

    try:
        stdscr.addstr(17, 2, "Step 3/3 — Pinata JWT (optional — IPFS pinning)"[:w-4])
        stdscr.addstr(18, 2, "Get yours at: https://app.pinata.cloud"[:w-4])
        stdscr.refresh()
    except curses.error:
        pass
    pinata_key = prompt_input(20, "PINATA_JWT (Enter to skip)")

    lines_out = [
        f"DELPHI_API_ACCESS_KEY={delphi_key}",
        "DELPHI_NETWORK=mainnet",
    ]
    if groq_key:
        lines_out.append(f"GROQ_API_KEY={groq_key}")
    if pinata_key:
        lines_out.append(f"PINATA_JWT={pinata_key}")

    ENV_FILE.write_text("\n".join(lines_out) + "\n")
    load_env(ENV_FILE)

    try:
        curses.noecho()
        curses.curs_set(0)
    except curses.error:
        pass

    try:
        stdscr.addstr(22, 2, "✓ Setup complete! Press any key to continue...")
        stdscr.refresh()
    except curses.error:
        pass
    stdscr.nodelay(False)
    stdscr.getch()
    stdscr.nodelay(True)
    return True


# ─── TUI ─────────────────────────────────────────────────────────────────────

class OracleREETUI:
    def __init__(self) -> None:
        self.market_input = ""
        self.mode = "edit"
        self.logs: list[str] = []
        self.status = "Ready"
        self.phase = "idle"
        self.progress = 0.0
        self.events: queue.Queue = queue.Queue()
        self.proc = None
        self.return_code = None
        self.result_lines: list[str] = []
        self.phases = [
            ("fetch",   0.10, "Fetch market from Delphi"),
            ("oracle",  0.35, "Fetch and verify oracle evidence"),
            ("ipfs",    0.55, "Pin evidence to IPFS"),
            ("inject",  0.70, "Inject oracle block into prompt"),
            ("ree",     0.85, "Run Gensyn REE inference"),
            ("receipt", 0.95, "Generate combined proof"),
            ("done",    1.00, "Done"),
        ]
        self.reached_phases: set[str] = set()

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.reached_phases.add(phase)
        for key, progress, _ in self.phases:
            if key == phase:
                self.progress = max(self.progress, progress)

    def add_log(self, line: str) -> None:
        self.logs.append(line.rstrip("\n"))
        self.logs = self.logs[-1000:]

    def parse_line(self, line: str) -> None:
        low = line.lower()
        if "fetching market" in low:
            self.set_phase("fetch")
        elif "fetching" in low and any(x in low for x in ["price", "eth", "btc", "web source"]):
            self.set_phase("oracle")
        elif "classification" in low or "evidence" in low:
            self.set_phase("oracle")
        elif "ipfs pinned" in low:
            self.set_phase("ipfs")
        elif "oracle prompt length" in low:
            self.set_phase("inject")
        elif "running ree" in low:
            self.set_phase("ree")
        elif "ree completed" in low:
            self.set_phase("receipt")
        elif "combined hash" in low or "verification passed" in low:
            self.set_phase("done")

        important = [
            "market:", "question:", "oracle hash", "evidence hash",
            "ipfs cid", "ree receipt", "combined hash",
            "verification passed", "saved to", "failed", "error",
            "event verdict", "price verdict",
        ]
        if any(w in low for w in important):
            self.result_lines.append(line.strip())
            self.result_lines = self.result_lines[-10:]

    def start_run(self) -> None:
        if self.mode == "running":
            return
        if not self.market_input.strip():
            self.status = "Paste a market URL first"
            return
        script = Path(__file__).parent / "oracle_ree.py"
        if not script.exists():
            self.status = "oracle_ree.py not found"
            return
        self.logs = []
        self.result_lines = []
        self.progress = 0.0
        self.reached_phases = set()
        self.phase = "idle"
        self.return_code = None
        self.mode = "running"
        self.status = "Running"
        cmd = ["python3", str(script), "--market", self.market_input.strip()]
        self.add_log("$ " + " ".join(shlex.quote(c) for c in cmd))

        def worker() -> None:
            rc = 1
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                self.proc = proc
                if proc.stdout:
                    for raw in proc.stdout:
                        self.events.put(("line", raw.rstrip("\n")))
                proc.wait()
                rc = proc.returncode if proc.returncode is not None else 1
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("done", rc))

        threading.Thread(target=worker, daemon=True).start()

    def handle_event(self, kind: str, payload) -> None:
        if kind == "line":
            self.parse_line(payload)
            self.add_log(payload)
        elif kind == "error":
            self.status = "Failed"
            self.add_log(f"Error: {payload}")
        elif kind == "done":
            self.return_code = payload
            self.mode = "done"
            if payload == 0:
                self.status = "Success"
                self.set_phase("done")
                self.progress = 1.0
            else:
                self.status = f"Failed (exit {payload})"

    def reset(self) -> None:
        if self.mode == "running":
            return
        self.logs = []
        self.result_lines = []
        self.progress = 0.0
        self.reached_phases = set()
        self.phase = "idle"
        self.return_code = None
        self.mode = "edit"
        self.status = "Ready"

    def init_colors(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN,   -1)
            curses.init_pair(2, curses.COLOR_RED,     -1)
            curses.init_pair(3, curses.COLOR_MAGENTA, -1)
            curses.init_pair(4, curses.COLOR_WHITE,   -1)
            curses.init_pair(5, curses.COLOR_YELLOW,  -1)
        except curses.error:
            pass

    def draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        # ── Title bar ──
        try:
            stdscr.addstr(0, 1, "OracleREE", curses.A_BOLD)
            status = f"Status: {self.status}"
            attr = curses.A_BOLD
            if "Success" in self.status:
                attr |= curses.color_pair(1)
            elif "Failed" in self.status:
                attr |= curses.color_pair(2)
            stdscr.addstr(0, max(1, w - len(status) - 2), status[:w-2], attr)
        except curses.error:
            pass

        # ── Progress bar ──
        try:
            bar_w = max(10, w - 4)
            inner_w = bar_w - 2
            fill = int(inner_w * self.progress)
            stdscr.addstr(2, 1, "[")
            if fill > 0:
                stdscr.addstr(2, 2, "#" * fill, curses.color_pair(3))
            if fill < inner_w:
                stdscr.addstr(2, 2 + fill, "-" * (inner_w - fill))
            stdscr.addstr(2, bar_w, "]")
            stdscr.addstr(3, 1, f"Current phase: {self.phase}"[:w-2], curses.A_DIM)
        except curses.error:
            pass

        # ── Phase timeline ──
        y = 5
        for key, _, label in self.phases:
            if y >= h - 8:
                break
            done = key in self.reached_phases
            marker = "[x]" if done else "[ ]"
            attr = curses.color_pair(1) if done else curses.A_NORMAL
            try:
                stdscr.addstr(y, 1, f"{marker} {label}"[:w-2], attr)
            except curses.error:
                pass
            y += 1

        # ── Output ──
        y += 1
        if y < h - 8:
            try:
                stdscr.addstr(y, 1, "OracleREE Output", curses.A_UNDERLINE)
            except curses.error:
                pass
            y += 1
            if self.result_lines:
                for line in self.result_lines[-4:]:
                    if y >= h - 6:
                        break
                    attr = curses.A_NORMAL
                    if "passed" in line.lower() or "success" in line.lower():
                        attr = curses.color_pair(1) | curses.A_BOLD
                    elif "failed" in line.lower() or "error" in line.lower():
                        attr = curses.color_pair(2)
                    elif "verdict" in line.lower() or "hash" in line.lower():
                        attr = curses.color_pair(5)
                    try:
                        stdscr.addstr(y, 1, line[:w-2], attr)
                    except curses.error:
                        pass
                    y += 1
            else:
                try:
                    stdscr.addstr(y, 1, "No output yet. Check logs below.")
                except curses.error:
                    pass
                y += 1

        # ── Market URL field ──
        y += 1
        if y < h - 5:
            try:
                stdscr.addstr(y, 1, "Fields (Enter edit, r run)", curses.A_UNDERLINE)
            except curses.error:
                pass
            y += 1
            value = self.market_input if self.market_input else "-"
            attr = curses.A_REVERSE if self.mode != "running" else curses.A_NORMAL
            try:
                stdscr.addstr(y, 1, f"{'Market URL':14} {value}"[:w-2], attr)
            except curses.error:
                pass
            y += 2

        # ── Logs ──
        if y < h - 2:
            try:
                stdscr.addstr(y, 1, "Logs", curses.A_UNDERLINE)
            except curses.error:
                pass
            y += 1
            log_height = max(1, h - y - 1)
            for line in self.logs[-log_height:]:
                if y >= h - 1:
                    break
                try:
                    stdscr.addstr(y, 1, line[:w-2], curses.A_DIM)
                except curses.error:
                    pass
                y += 1

        # ── Help bar ──
        try:
            stdscr.addstr(h - 1, 1, "q quit | r run | e reset"[:w-2], curses.A_DIM)
        except curses.error:
            pass

        stdscr.refresh()

    def edit_market(self, stdscr: curses.window) -> None:
        h, w = stdscr.getmaxyx()
        win_w = min(w - 4, 120)
        win_h = 5
        win_y = max(0, (h - win_h) // 2)
        win_x = max(0, (w - win_w) // 2)
        try:
            win = curses.newwin(win_h, win_w, win_y, win_x)
        except curses.error:
            return
        win.keypad(True)
        win.box()
        win.addstr(1, 2, "Paste Delphi market URL or ID (Enter to save, Esc to cancel)"[:win_w-4])
        value = self.market_input
        cursor = len(value)
        while True:
            view_w = max(1, win_w - 4)
            offset = max(0, cursor - view_w + 1)
            shown = value[offset:offset + view_w]
            try:
                win.addstr(2, 2, " " * view_w, curses.A_UNDERLINE)
                win.addstr(2, 2, shown, curses.A_UNDERLINE)
                win.move(2, 2 + min(view_w - 1, cursor - offset))
                win.refresh()
            except curses.error:
                pass
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            try:
                key = win.get_wch()
            except curses.error:
                continue
            finally:
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
            if key in ("\n", "\r") or key == curses.KEY_ENTER:
                self.market_input = value.strip()
                return
            if key == "\x1b":
                return
            if key == curses.KEY_LEFT:
                cursor = max(0, cursor - 1)
            elif key == curses.KEY_RIGHT:
                cursor = min(len(value), cursor + 1)
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
                if cursor > 0:
                    value = value[:cursor-1] + value[cursor:]
                    cursor -= 1
            elif isinstance(key, str) and key.isprintable():
                value = value[:cursor] + key + value[cursor:]
                cursor += len(key)

    def handle_key(self, stdscr: curses.window, key: int) -> bool:
        if key == -1:
            return True
        if key in (ord("q"), 27):
            return False
        if self.mode == "running":
            return True
        if key in (10, 13, curses.KEY_ENTER):
            self.edit_market(stdscr)
        elif key == ord("r"):
            self.start_run()
        elif key == ord("e"):
            self.reset()
        return True

    def run(self, stdscr: curses.window) -> None:
        self.init_colors()
        stdscr.nodelay(True)
        stdscr.timeout(100)

        # First-run setup if no .env.local
        if not ENV_FILE.exists():
            run_setup(stdscr)
            stdscr.clear()
            stdscr.nodelay(True)
            stdscr.timeout(100)

        while True:
            while True:
                try:
                    kind, payload = self.events.get_nowait()
                except queue.Empty:
                    break
                self.handle_event(kind, payload)
            self.draw(stdscr)
            key = stdscr.getch()
            if not self.handle_key(stdscr, key):
                break


def main() -> int:
    if not Path("./oracle_ree.py").exists():
        print("oracle_ree.py not found in current directory")
        print("Make sure you are in the oracle-ree folder:")
        print("  cd oracle-ree && python3 ree.py")
        return 2
    tui = OracleREETUI()
    curses.wrapper(tui.run)
    return 0 if tui.return_code in (None, 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
