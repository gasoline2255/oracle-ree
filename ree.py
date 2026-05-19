#!/usr/bin/env python3
"""
OracleREE TUI — Trustless oracle grounding for Gensyn Delphi settlement.
Run: python3 ree.py
"""

from __future__ import annotations

import curses
import json
import os
import queue
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

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

def run_setup(stdscr: curses.window) -> None:
    curses.echo()
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    stdscr.clear()
    h, w = stdscr.getmaxyx()

    def put(y: int, x: int, text: str, attr=0) -> None:
        try:
            stdscr.addstr(y, x, text[:max(0, w - x - 1)], attr)
        except curses.error:
            pass

    put(1, 2, "OracleREE — First-run setup", curses.A_BOLD)
    put(2, 2, "─" * min(50, w - 4))
    put(3, 2, "All API keys are free. Takes under 2 minutes.")
    put(5, 2, "Step 1/3  Delphi API Key (required)")
    put(6, 2, "Get yours: https://api-access.delphi.fyi/")
    stdscr.refresh()

    def prompt(y: int, label: str, mask: bool = False) -> str:
        put(y, 2, f"{label}: ")
        stdscr.refresh()
        buf = ""
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue
            if ch in ("\n", "\r"):
                break
            if isinstance(ch, str) and ch in ("\x7f", "\x08"):
                if buf:
                    buf = buf[:-1]
                    try:
                        yx = stdscr.getyx()
                        stdscr.addstr(yx[0], 2 + len(label) + 2, " " * (len(buf) + 2))
                        stdscr.addstr(yx[0], 2 + len(label) + 2, "*" * len(buf) if mask else buf)
                        stdscr.refresh()
                    except curses.error:
                        pass
            elif isinstance(ch, str) and ch.isprintable():
                buf += ch
                try:
                    yx = stdscr.getyx()
                    disp = "*" * len(buf) if mask else buf
                    stdscr.addstr(yx[0], 2 + len(label) + 2, disp[:w - 10])
                    stdscr.refresh()
                except curses.error:
                    pass
        return buf.strip()

    delphi_key = prompt(8, "DELPHI_API_ACCESS_KEY")
    if not delphi_key:
        try:
            curses.noecho()
            curses.curs_set(0)
        except curses.error:
            pass
        return

    put(10, 2, "Step 2/3  Groq API Key (optional — better verdicts)")
    put(11, 2, "Get yours: https://console.groq.com")
    stdscr.refresh()
    groq_key = prompt(13, "GROQ_API_KEY (Enter to skip)")

    put(15, 2, "Step 3/3  Pinata JWT (optional — IPFS evidence pinning)")
    put(16, 2, "Get yours: https://app.pinata.cloud")
    stdscr.refresh()
    pinata_key = prompt(18, "PINATA_JWT (Enter to skip)")

    lines = [f"DELPHI_API_ACCESS_KEY={delphi_key}", "DELPHI_NETWORK=mainnet"]
    if groq_key:
        lines.append(f"GROQ_API_KEY={groq_key}")
    if pinata_key:
        lines.append(f"PINATA_JWT={pinata_key}")
    ENV_FILE.write_text("\n".join(lines) + "\n")
    load_env(ENV_FILE)

    try:
        curses.noecho()
        curses.curs_set(0)
    except curses.error:
        pass

    put(20, 2, "✓ Setup complete. Press any key to continue.", curses.A_BOLD)
    stdscr.refresh()
    stdscr.nodelay(False)
    stdscr.getch()
    stdscr.nodelay(True)

# ─── Helpers ─────────────────────────────────────────────────────────────────

def extract_market_id(s: str) -> Optional[str]:
    m = re.search(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])", s)
    if m:
        return m.group(0)
    u = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s)
    if u:
        return u.group(0)
    return None

def fetch_market_info(market_id: str) -> Optional[dict]:
    try:
        import requests
        api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
        r = requests.get(
            f"https://api.delphi.fyi/markets/{market_id}",
            headers={"x-api-key": api_key},
            timeout=10,
        )
        if r.ok:
            return r.json()
    except Exception:
        pass
    return None

def wrap_text(text: str, width: int) -> list[str]:
    lines = []
    width = max(8, width)
    for para in text.split("\n"):
        if not para.strip():
            lines.append("")
            continue
        while len(para) > width:
            lines.append(para[:width])
            para = para[width:]
        lines.append(para)
    return lines


def compact_value(value: str, max_len: int) -> str:
    value = str(value or "").strip()
    if max_len <= 8 or len(value) <= max_len:
        return value
    keep = max_len - 3
    left = max(4, keep // 2)
    right = max(4, keep - left)
    return value[:left] + "..." + value[-right:]

# ─── TUI ─────────────────────────────────────────────────────────────────────

PHASES = [
    ("fetch",   0.12, "Fetch market from Delphi"),
    ("oracle",  0.35, "Fetch and verify oracle evidence"),
    ("ipfs",    0.55, "Pin evidence to IPFS"),
    ("inject",  0.70, "Inject oracle block into prompt"),
    ("ree",     0.85, "Run Gensyn REE inference"),
    ("receipt", 0.95, "Generate combined proof"),
    ("done",    1.00, "Done"),
]

class TUI:
    """
    Two-page OracleREE TUI.

    Page 1:
      Clean centered input screen only.

    Page 2:
      Settlement/proof dashboard after the user runs a market.
    """

    def __init__(self) -> None:
        self.market_input = ""
        self.screen = "input"       # input | results
        self.mode = "idle"          # idle | editing | running | done
        self.status = "Ready"
        self.phase = "idle"
        self.progress = 0.0
        self.reached: set[str] = set()
        self.logs: list[str] = []
        self.proof_lines: list[str] = []
        self.events: queue.Queue = queue.Queue()
        self.return_code: Optional[int] = None
        self.market_data: Optional[dict] = None
        self.fetching_market = False
        self.last_fetched_id = ""
        self.left_scroll = 0
        self.log_scroll = 0
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.ree_receipt_path: str = ""
        self._input_active = True

    # ── market fetch (background) ──────────────────────────────────────────

    def trigger_fetch(self) -> None:
        mid = extract_market_id(self.market_input)
        if not mid or mid == self.last_fetched_id:
            return
        self.last_fetched_id = mid
        self.market_data = None
        self.fetching_market = True

        def worker():
            data = fetch_market_info(mid)
            self.events.put(("market", data))

        threading.Thread(target=worker, daemon=True).start()

    # ── run ───────────────────────────────────────────────────────────────

    def start_run(self) -> None:
        if self.mode == "running":
            return

        if not self.market_input.strip():
            self.status = "Press Enter and paste a Market URL, Market ID, or Settlement Prompt"
            return

        script = Path(__file__).parent / "oracle_ree.py"
        if not script.exists():
            self.status = "oracle_ree.py not found in this directory"
            return

        # Switch to the dashboard only when the user actually runs.
        self.screen = "results"
        self.logs = []
        self.proof_lines = []
        self.progress = 0.03
        self.reached = set()
        self.phase = "fetch"
        self.return_code = None
        self.mode = "running"
        self.status = "Running..."
        self.started_at = time.time()
        self.finished_at = None
        self.ree_receipt_path = ""
        self.left_scroll = 0
        self.log_scroll = 0

        # Fetch Delphi metadata only after Run, not on the first input page.
        self.trigger_fetch()

        cmd = ["python3", str(script), "--market", self.market_input.strip()]
        self.add_log("$ " + " ".join(shlex.quote(c) for c in cmd))

        def worker():
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
                if proc.stdout:
                    for raw in proc.stdout:
                        self.events.put(("line", raw.rstrip("\n")))
                proc.wait()
                rc = proc.returncode if proc.returncode is not None else 1
            except Exception as exc:
                self.events.put(("line", f"Error: {exc}"))
            finally:
                self.events.put(("done", rc))

        threading.Thread(target=worker, daemon=True).start()

    def reset(self) -> None:
        if self.mode == "running":
            return

        self.screen = "input"
        self.market_input = ""
        self.logs = []
        self.proof_lines = []
        self.progress = 0.0
        self.reached = set()
        self.phase = "idle"
        self.return_code = None
        self.market_data = None
        self.fetching_market = False
        self.last_fetched_id = ""
        self.mode = "idle"
        self.status = "Ready"
        self.started_at = None
        self.finished_at = None
        self.ree_receipt_path = ""
        self.left_scroll = 0
        self.log_scroll = 0

    def add_log(self, line: str) -> None:
        self.logs.append(line)
        if len(self.logs) > 2000:
            self.logs = self.logs[-2000:]

    def set_phase(self, key: str) -> None:
        self.phase = key
        self.reached.add(key)
        for k, p, _ in PHASES:
            if k == key:
                self.progress = max(self.progress, p)

    def parse_line(self, line: str) -> None:
        low = line.lower()

        if "fetching market" in low:
            self.set_phase("fetch")
        elif "classification" in low or ("fetching" in low and any(x in low for x in ["price", "web source", "eth", "btc", "sol"])):
            self.set_phase("oracle")
        elif "ipfs pinned" in low:
            self.set_phase("ipfs")
        elif "oracle prompt length" in low:
            self.set_phase("inject")
        elif "running ree" in low:
            self.set_phase("ree")
        elif "ree completed" in low:
            # Keep the visible phase on REE until proof hashes start printing.
            # This avoids the dashboard looking stuck at "Generate combined proof".
            self.set_phase("ree")
            self.progress = max(self.progress, 0.92)
        elif "combined hash" in low or "saved to" in low:
            self.set_phase("done")

        # Store the real local receipt file path when normal REE prints it.
        # Example: [ree] Receipt: /home/user/.cache/gensyn/.../metadata/receipt_20260519_092137.json
        if "receipt" in low and "/" in line and ("metadata" in low or "receipt_" in low):
            val = line.split(":", 1)[1].strip() if ":" in line else line.strip()
            if val and not val.startswith("sha256:"):
                self.ree_receipt_path = val

        keys = [
            "oracle hash", "evidence hash", "oracle evidence hash", "ipfs cid",
            "ree receipt", "ree receipt hash", "combined hash", "saved to",
            "verification passed", "market:", "event verdict", "price verdict", "error"
        ]

        if any(k in low for k in keys):
            self.proof_lines.append(line.strip())
            if len(self.proof_lines) > 30:
                self.proof_lines = self.proof_lines[-30:]

    def handle_event(self, kind: str, payload) -> None:
        if kind == "line":
            self.parse_line(payload)
            self.add_log(payload)
        elif kind == "market":
            self.fetching_market = False
            self.market_data = payload
        elif kind == "done":
            self.return_code = payload
            self.mode = "done"
            self.finished_at = time.time()
            if payload == 0:
                self.status = "Success"
                self.set_phase("done")
                self.progress = 1.0
            else:
                self.status = f"Failed (exit {payload})"

    def tick_progress(self) -> None:
        """Keep the dashboard visibly moving while oracle_ree.py is running.

        oracle_ree.py sometimes prints output in bursts, so relying only on stdout can
        make the UI look frozen. This method advances a soft progress estimate until
        real log lines confirm a phase.
        """
        if self.mode != "running" or self.started_at is None:
            return

        elapsed = max(0.0, time.time() - self.started_at)
        # Soft floor based on elapsed time. Never go beyond 92% until the process exits.
        soft = min(0.92, 0.05 + elapsed / 90.0)
        if soft > self.progress:
            self.progress = soft

        # Show a moving current phase even if stdout is quiet.
        if self.progress < 0.15:
            self.phase = "fetch"
        elif self.progress < 0.38:
            self.phase = "oracle"
        elif self.progress < 0.58:
            self.phase = "ipfs"
        elif self.progress < 0.72:
            self.phase = "inject"
        elif self.progress < 0.88:
            self.phase = "ree"
        else:
            # Hold soft progress at REE instead of pretending proof generation started.
            # The UI will move to Done only when actual proof/hash logs arrive.
            self.phase = "ree"

    def collect_paste_buffer(self, stdscr: curses.window, first_key):
        """Collect fast pasted text as one buffer.

        Windows Terminal/WSL can deliver paste data in bursts. Reading only one
        key per render frame can drop most of the paste or leave only the last
        few characters visible. This drains the pending terminal input queue
        immediately after the first printable key so paste behaves like a normal
        chat input field.
        """
        if isinstance(first_key, int):
            return first_key

        # Give the terminal a tiny moment to put the rest of the paste into the queue.
        time.sleep(0.015)

        pieces = [first_key]
        old_timeout = 80
        try:
            stdscr.timeout(0)
            empty_reads = 0
            while empty_reads < 3:
                try:
                    nxt = stdscr.get_wch()
                except curses.error:
                    empty_reads += 1
                    time.sleep(0.005)
                    continue
                empty_reads = 0
                if isinstance(nxt, str):
                    pieces.append(nxt)
                else:
                    # Put navigation/control keys back only if curses supports it.
                    try:
                        curses.ungetch(nxt)
                    except curses.error:
                        pass
                    break
        finally:
            stdscr.timeout(old_timeout)

        data = "".join(pieces)

        # Strip bracketed paste wrappers if terminal sends them.
        data = data.replace("\x1b[200~", "").replace("\x1b[201~", "")
        return data

    def handle_input_page_key(self, key) -> None:
        """Direct typing/pasting on the first page.

        Users can paste a market URL, market ID, or full settlement prompt
        directly into the active chat-style field. Multiline paste is normalized
        into a single clean line for display/execution.
        """
        if key in (curses.KEY_BACKSPACE, 127, 8, "\x7f", "\x08"):
            self.market_input = self.market_input[:-1]
            self.status = "Ready" if not self.market_input else "Input ready. Press r to run."
            return

        if key in (curses.KEY_DC,):
            return

        if key in ("\n", "\r", curses.KEY_ENTER, 10, 13):
            if self.market_input.strip():
                self.status = "Input ready. Press r to run."
            else:
                self.status = "Type or paste a Market URL, Market ID, or Settlement Prompt"
            return

        if key == "\x1b":
            if self.market_input.strip():
                self.status = "Input ready. Press r to run."
            return

        if isinstance(key, int):
            return

        if isinstance(key, str):
            # Remove bracketed paste control sequences and normalize multiline prompt text.
            cleaned = key.replace("\x1b[200~", "").replace("\x1b[201~", "")
            cleaned = " ".join(cleaned.replace("\r", "\n").splitlines()) if ("\n" in cleaned or "\r" in cleaned) else cleaned
            # Ignore non-printable escape leftovers.
            cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch.isspace())
            if cleaned:
                self.market_input += cleaned
                self.status = "Input ready. Press r to run."
            return

    # ── drawing helpers ───────────────────────────────────────────────────

    def init_colors(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_WHITE,   -1)  # bright
            curses.init_pair(2, 8,                    -1)  # dim grey
            curses.init_pair(3, curses.COLOR_GREEN,   -1)  # success
            curses.init_pair(4, curses.COLOR_RED,     -1)  # error
            curses.init_pair(5, curses.COLOR_YELLOW,  -1)  # active/warning
            curses.init_pair(6, curses.COLOR_CYAN,    -1)  # accent
        except curses.error:
            pass

    def put(self, win: curses.window, y: int, x: int, text: str, attr=0) -> None:
        h, w = win.getmaxyx()
        if y < 0 or y >= h or x < 0:
            return
        available = max(0, w - x - 1)
        if not available:
            return
        try:
            win.addstr(y, x, text[:available], attr)
        except curses.error:
            pass

    def draw_nav(self, stdscr: curses.window, w: int) -> None:
        # Results page header only. The input page has its own centered brand card.
        if self.screen == "input":
            return
        self.put(stdscr, 0, 0, " " * w, curses.A_REVERSE)
        self.put(stdscr, 0, 1, "ORACLEREE", curses.A_REVERSE | curses.A_BOLD)
        self.put(stdscr, 0, 12, "Proof Dashboard", curses.A_REVERSE)
        status_str = f"Status: {self.status}"
        self.put(stdscr, 0, max(14, w - len(status_str) - 2), status_str, curses.A_REVERSE)

    def draw_footer(self, stdscr: curses.window, h: int, w: int) -> None:
        # No detached footer on the input screen. Controls live inside the brand card.
        if self.screen == "input":
            return
        self.put(stdscr, h - 1, 0, " " * w, curses.A_REVERSE)
        keys = " q quit   e new input   r rerun "
        self.put(stdscr, h - 1, 1, keys, curses.A_REVERSE)
        ver = " OracleREE · Gensyn Delphi settlement proof "
        self.put(stdscr, h - 1, max(0, w - len(ver) - 1), ver, curses.A_REVERSE)

    def draw_input_page(self, stdscr: curses.window, h: int, w: int) -> None:
        box_w = min(118, max(72, w - 12))
        box_h = 18
        x = max(0, (w - box_w) // 2)
        y = max(1, (h - box_h) // 2)

        self.put(stdscr, y, x, "┌" + "─" * (box_w - 2) + "┐", curses.color_pair(2))
        for i in range(1, box_h - 1):
            self.put(stdscr, y + i, x, "│", curses.color_pair(2))
            self.put(stdscr, y + i, x + box_w - 1, "│", curses.color_pair(2))
        self.put(stdscr, y + box_h - 1, x, "└" + "─" * (box_w - 2) + "┘", curses.color_pair(2))

        title = "ORACLEREE"
        tagline = "Trustless Oracle Grounding"
        subtitle = "Gensyn Delphi settlement verification layer"
        self.put(stdscr, y + 2, x + (box_w - len(title)) // 2, title, curses.A_BOLD | curses.color_pair(3))
        self.put(stdscr, y + 3, x + (box_w - len(tagline)) // 2, tagline, curses.A_BOLD | curses.color_pair(6))
        self.put(stdscr, y + 4, x + (box_w - len(subtitle)) // 2, subtitle, curses.color_pair(2))

        inner_x = x + 6
        field_w = box_w - 12

        self.put(stdscr, y + 6, inner_x, "INPUT", curses.color_pair(1) | curses.A_BOLD)
        self.put(stdscr, y + 7, inner_x, "Market URL  ·  Market ID  ·  Full Settlement Prompt", curses.color_pair(6))

        # Chat-style active input field. Users can type or paste directly here.
        self.put(stdscr, y + 9, inner_x, "╭" + "─" * (field_w - 2) + "╮", curses.color_pair(2))
        self.put(stdscr, y + 10, inner_x, "│", curses.color_pair(2))
        self.put(stdscr, y + 10, inner_x + field_w - 1, "│", curses.color_pair(2))
        self.put(stdscr, y + 11, inner_x, "╰" + "─" * (field_w - 2) + "╯", curses.color_pair(2))

        blink = int(time.time() * 2) % 2 == 0
        cursor = "█" if blink else " "
        display = " ".join(self.market_input.split())
        if display:
            prompt_prefix = "> "
            available = max(8, field_w - 6)
            visible = compact_value(display, available)
            input_line = prompt_prefix + visible + " " + cursor
            attr = curses.color_pair(1)
        else:
            input_line = "> " + cursor
            attr = curses.color_pair(2)
        self.put(stdscr, y + 10, inner_x + 2, input_line[:field_w - 4], attr)

        if display:
            meta = f"Input ready · {len(self.market_input)} characters · press r to run"
            self.put(stdscr, y + 13, inner_x, meta[:field_w], curses.color_pair(3))
        else:
            self.put(stdscr, y + 13, inner_x, "Type or paste directly above. Press Enter when done, then r to run.", curses.color_pair(2))

        self.put(stdscr, y + 15, inner_x, "CONTROLS", curses.color_pair(1) | curses.A_BOLD)
        controls = "Type/paste directly   Enter done   r run   q quit"
        self.put(stdscr, y + 16, inner_x, controls[:field_w], curses.color_pair(2))

        if self.status and self.status not in ("Ready", "Input ready. Press r to run."):
            self.put(stdscr, y + box_h - 2, inner_x, f"Status: {self.status}"[:field_w], curses.color_pair(5))

    def section_header(self, stdscr: curses.window, y: int, x: int, width: int, title: str) -> int:
        self.put(stdscr, y, x, "─" * width, curses.color_pair(2))
        self.put(stdscr, y, x + 1, f" {title} ", curses.color_pair(2) | curses.A_BOLD)
        return y + 1

    def kv(self, stdscr: curses.window, y: int, x: int, width: int, label: str, value: str, attr=0) -> int:
        label_w = min(15, max(10, width // 4))
        self.put(stdscr, y, x, f"{label:<{label_w}}", curses.color_pair(2))
        self.put(stdscr, y, x + label_w + 1, compact_value(value, width - label_w - 2), attr or curses.color_pair(1))
        return y + 1

    def latest_line_value(self, *needles: str) -> str:
        needles_l = [n.lower() for n in needles]
        for line in reversed(self.proof_lines + self.logs):
            low = line.lower()
            if all(n in low for n in needles_l):
                if ":" in line:
                    return line.split(":", 1)[1].strip()
                return line.strip()
        return ""

    def latest_saved_file(self) -> str:
        for line in reversed(self.proof_lines + self.logs):
            low = line.lower()
            if "saved to" in low or "saved:" in low:
                return line.split(":", 1)[1].strip() if ":" in line else line.strip()
        return ""

    def latest_ree_receipt_path(self) -> str:
        if self.ree_receipt_path:
            return self.ree_receipt_path
        # Prefer the full local file path, not the later sha256 receipt hash summary.
        for line in reversed(self.logs):
            low = line.lower()
            if "receipt" in low and "/" in line and ("metadata" in low or "receipt_" in low):
                val = line.split(":", 1)[1].strip() if ":" in line else line.strip()
                if val and not val.startswith("sha256:"):
                    return val
        return ""

    def market_summary(self) -> tuple[str, str, str, str, str, str, str, str]:
        question = ""
        mid = extract_market_id(self.market_input) or ""
        status = ""
        resolves = ""
        delphi_model = ""
        sources = ""
        outcomes = ""
        prompt_ctx = ""

        if self.market_data:
            meta = self.market_data.get("metadata") or {}
            model_info = meta.get("model") or {}
            question = meta.get("question", "") or self.market_data.get("question", "") or ""
            mid = self.market_data.get("id", "") or mid
            status = str(self.market_data.get("status", "") or "")
            resolves = str(self.market_data.get("resolvesAt", "") or self.market_data.get("closeTime", "") or self.market_data.get("close_time", ""))[:16].replace("T", " ")
            delphi_model = str(model_info.get("model_identifier") or model_info.get("modelIdentifier") or self.market_data.get("judgeModel", "") or "")
            src = self.market_data.get("dataSources") or meta.get("dataSources") or []
            outs = meta.get("outcomes") or self.market_data.get("outcomes") or []
            sources = " · ".join(map(str, src))
            outcomes = " · ".join(map(str, outs))
            prompt_ctx = str(model_info.get("prompt_context") or model_info.get("promptContext") or self.market_data.get("settlementPrompt") or "")
        elif not mid:
            question = "Raw settlement prompt"
            prompt_ctx = self.market_input

        return question, mid, status, resolves, delphi_model, sources, outcomes, prompt_ctx

    def draw_left_results(self, stdscr: curses.window, h: int, left_w: int) -> None:
        y = 1
        pad_x = 1
        inner_w = max(20, left_w - 3)
        question, mid, status, resolves, delphi_model, sources, outcomes, prompt_ctx = self.market_summary()

        y = self.section_header(stdscr, y, 0, left_w, "MARKET")
        if self.fetching_market:
            self.put(stdscr, y, pad_x, "Fetching Delphi market metadata...", curses.color_pair(5))
            y += 1
        elif not self.market_data and mid:
            y = self.kv(stdscr, y, pad_x, inner_w, "Market ID", f"{mid[:10]}...{mid[-6:]}", curses.color_pair(1))
            self.put(stdscr, y, pad_x, "Metadata unavailable. Running with supplied input.", curses.color_pair(5))
            y += 1
        else:
            y = self.kv(stdscr, y, pad_x, inner_w, "Question", question or "—", curses.color_pair(1) | curses.A_BOLD)
            if mid:
                y = self.kv(stdscr, y, pad_x, inner_w, "Market ID", f"{mid[:10]}...{mid[-6:]}", curses.color_pair(2))
            if status or resolves:
                y = self.kv(stdscr, y, pad_x, inner_w, "Status", f"{status} · closes {resolves} UTC".strip(" ·"), curses.color_pair(2))
            if delphi_model:
                y = self.kv(stdscr, y, pad_x, inner_w, "Judge", delphi_model, curses.color_pair(2))
            if sources:
                y = self.kv(stdscr, y, pad_x, inner_w, "Sources", sources, curses.color_pair(2))
            if outcomes:
                y = self.kv(stdscr, y, pad_x, inner_w, "Outcomes", outcomes, curses.color_pair(2))

        y += 1
        if y < h - 2:
            y = self.section_header(stdscr, y, 0, left_w, "SETTLEMENT PROMPT")
            prompt = prompt_ctx or self.market_input
            wrapped = wrap_text(prompt, inner_w)
            max_lines = max(3, min(9, h // 4))
            for pl in wrapped[:max_lines]:
                if y >= h - 5:
                    break
                self.put(stdscr, y, pad_x, pl[:inner_w], curses.color_pair(2))
                y += 1
            if len(wrapped) > max_lines and y < h - 4:
                self.put(stdscr, y, pad_x, f"... {len(wrapped) - max_lines} more lines hidden", curses.color_pair(2))
                y += 1

        y += 1
        if y < h - 2:
            y = self.section_header(stdscr, y, 0, left_w, "ORACLE EVIDENCE")
            evidence_hash = self.latest_line_value("oracle evidence hash") or self.latest_line_value("evidence hash") or self.latest_line_value("oracle hash")
            ipfs = self.latest_line_value("ipfs cid")
            verdict = self.latest_line_value("price verdict") or self.latest_line_value("event verdict") or self.latest_line_value("verdict")
            rows = [
                ("Oracle hash", evidence_hash),
                ("IPFS CID", ipfs),
                ("Verdict", verdict),
            ]
            printed = False
            for label, val in rows:
                if val and y < h - 2:
                    y = self.kv(stdscr, y, pad_x, inner_w, label, val, curses.color_pair(3) if label != "Verdict" else curses.color_pair(1))
                    printed = True
            if not printed and y < h - 2:
                self.put(stdscr, y, pad_x, "Evidence appears here after oracle fetch completes.", curses.color_pair(2))
                y += 1

    def draw_right_results(self, stdscr: curses.window, h: int, left_w: int, w: int) -> None:
        rx = left_w + 1
        rw = w - rx
        y = 1
        inner_x = rx + 1
        inner_w = max(20, rw - 3)

        # ── execution status ──
        y = self.section_header(stdscr, y, rx, rw, "EXECUTION")
        pct = int(self.progress * 100)
        status = "SUCCESS" if self.return_code == 0 else ("FAILED" if self.return_code not in (None, 0) else self.phase.upper())
        self.put(stdscr, y, inner_x, f"Status     {status}", curses.color_pair(3) if self.return_code == 0 else curses.color_pair(5))
        self.put(stdscr, y, inner_x + 24, f"Progress {pct}%", curses.color_pair(1))
        y += 1

        bar_w = max(20, inner_w)
        fill = int(bar_w * self.progress)
        bar = "█" * fill + "░" * (bar_w - fill)
        self.put(stdscr, y, inner_x, bar[:inner_w], curses.color_pair(3) if self.progress >= 1.0 else curses.color_pair(6))
        y += 2

        # ── compact pipeline ──
        y = self.section_header(stdscr, y, rx, rw, "PIPELINE")
        cols = 2 if inner_w > 70 else 1
        if cols == 2:
            col_w = inner_w // 2
            for i, (key, _, label) in enumerate(PHASES):
                row_y = y + i // 2
                col_x = inner_x + (i % 2) * col_w
                done = key in self.reached
                marker = "✓" if done else "·"
                attr = curses.color_pair(3) if done else curses.color_pair(2)
                self.put(stdscr, row_y, col_x, f"{marker} {label}"[:col_w - 1], attr)
            y += (len(PHASES) + 1) // 2
        else:
            for key, _, label in PHASES:
                if y >= h - 2:
                    break
                done = key in self.reached
                marker = "✓" if done else "·"
                attr = curses.color_pair(3) if done else curses.color_pair(2)
                self.put(stdscr, y, inner_x, f"{marker} {label}"[:inner_w], attr)
                y += 1

        y += 1

        # ── proof package, the important part ──
        if y < h - 2:
            y = self.section_header(stdscr, y, rx, rw, "PROOF PACKAGE")
        combined = self.latest_line_value("combined hash")
        oracle_hash = self.latest_line_value("oracle evidence hash") or self.latest_line_value("evidence hash") or self.latest_line_value("oracle hash")
        ree_hash = self.latest_line_value("ree receipt hash")
        ipfs = self.latest_line_value("ipfs cid")
        saved = self.latest_saved_file()
        receipt_path = self.latest_ree_receipt_path()
        proof_rows = [
            ("Combined", combined),
            ("Oracle", oracle_hash),
            ("REE", ree_hash),
            ("IPFS", ipfs),
            ("Saved", saved),
            ("Receipt", receipt_path),
        ]
        printed = False
        for label, val in proof_rows:
            if val and y < h - 8:
                y = self.kv(stdscr, y, inner_x, inner_w, label, val, curses.color_pair(3) if label in ("Combined", "Oracle", "REE", "IPFS") else curses.color_pair(2))
                printed = True
        if not printed and y < h - 8:
            self.put(stdscr, y, inner_x, "Proof hashes appear here as the run completes.", curses.color_pair(2))
            y += 1

        # ── verification command ──
        if self.return_code == 0 and y < h - 6:
            y += 1
            y = self.section_header(stdscr, y, rx, rw, "VERIFY")
            verify = "python3 ree.py verify --receipt-path <receipt.json>"
            self.put(stdscr, y, inner_x, "✓ Oracle data + REE execution cryptographically linked", curses.color_pair(3))
            y += 1
            self.put(stdscr, y, inner_x, verify[:inner_w], curses.color_pair(2))
            y += 1

        # ── recent logs only, not the full wall of text ──
        if y < h - 2:
            y += 1
            y = self.section_header(stdscr, y, rx, rw, "RECENT LOGS")
        log_h = max(1, h - y - 2)
        visible_logs = self.logs[-min(log_h, 8):]
        for ll in visible_logs:
            if y >= h - 2:
                break
            clean = ll.replace("\t", " ")
            attr = curses.color_pair(4) if "error" in clean.lower() or "failed" in clean.lower() else curses.color_pair(2)
            self.put(stdscr, y, inner_x, clean[:inner_w], attr)
            y += 1

    def draw_divider(self, stdscr: curses.window, h: int, left_w: int) -> None:
        for y in range(1, h - 1):
            self.put(stdscr, y, left_w, "│", curses.color_pair(2))


    def draw_results_page(self, stdscr: curses.window, h: int, w: int) -> None:
        """Mockup-style OracleREE proof dashboard.

        This page intentionally does NOT show the input card. The first screen is
        only for input; this screen is only for execution/proof review.
        """
        top = 1
        bottom = h - 2
        usable_h = max(12, bottom - top + 1)

        left_w = max(56, min(70, int(w * 0.34)))
        gap = 1
        right_x = left_w + gap
        right_w = max(50, w - right_x - 1)

        def box(x: int, y: int, bw: int, bh: int, title: str):
            bh = max(3, bh)
            if y + bh > bottom + 1:
                bh = max(3, bottom + 1 - y)
            if bw <= 10 or bh <= 2:
                return x, y, bw, bh

            self.put(stdscr, y, x, "┌" + "─" * (bw - 2) + "┐", curses.color_pair(2))
            for yy in range(y + 1, y + bh - 1):
                self.put(stdscr, yy, x, "│", curses.color_pair(2))
                self.put(stdscr, yy, x + bw - 1, "│", curses.color_pair(2))
            self.put(stdscr, y + bh - 1, x, "└" + "─" * (bw - 2) + "┘", curses.color_pair(2))
            self.put(stdscr, y, x + 2, f" {title} ", curses.color_pair(6) | curses.A_BOLD)
            return x, y, bw, bh

        def put_kv(x: int, y: int, bw: int, label: str, value: str, attr=0):
            label_w = 14
            self.put(stdscr, y, x, f"{label:<{label_w}}", curses.color_pair(2))
            self.put(
                stdscr,
                y,
                x + label_w + 1,
                compact_value(value or "—", bw - label_w - 2),
                attr or curses.color_pair(1),
            )
            return y + 1

        def section_lines(prompt: str, heading: str) -> list[str]:
            lines = [ln.rstrip() for ln in prompt.splitlines()]
            out: list[str] = []
            capture = False
            for ln in lines:
                up = ln.upper().strip()
                if heading in up:
                    capture = True
                    continue
                if capture and up.endswith(":") and heading not in up:
                    break
                if capture and ln.strip():
                    out.append(ln.strip())
            return out

        def extract_question(prompt: str) -> str:
            m = re.search(r"QUESTION:\s*(.+?)(?:\n\s*\n|DATA SOURCES:|SETTLEMENT RULES:|VALID OUTCOMES|$)", prompt, re.I | re.S)
            if m:
                return " ".join(m.group(1).split())
            return ""

        def extract_sources(prompt: str) -> list[str]:
            src = section_lines(prompt, "DATA SOURCES")
            clean: list[str] = []
            for s in src:
                s = s.lstrip("-• ").strip()
                if s:
                    clean.append(s)
            return clean[:5]

        def extract_rules(prompt: str) -> list[str]:
            rules = section_lines(prompt, "SETTLEMENT RULES")
            clean: list[str] = []
            for r in rules:
                r = r.lstrip("-• ").strip()
                if r:
                    clean.append(r)
            if not clean:
                for ln in prompt.splitlines():
                    stripped = ln.strip()
                    if stripped.startswith("-") or stripped.startswith("•"):
                        clean.append(stripped.lstrip("-• ").strip())
            return clean[:6]

        def extract_outcome_lines(prompt: str) -> list[str]:
            lines = section_lines(prompt, "VALID OUTCOMES")
            clean: list[str] = []
            for ln in lines:
                ln = ln.lstrip("-• ").strip()
                if ln:
                    clean.append(ln)
            return clean[:6]

        def progress_state(key: str, index: int) -> tuple[str, int]:
            """Return visual state and attr.

            done: green, current: cyan/yellow, pending: dim, failed: red.
            Uses both real stdout events and soft progress so the UI visibly moves
            even while oracle_ree.py is quiet.
            """
            if self.return_code not in (None, 0):
                if key == self.phase:
                    return "failed", curses.color_pair(4) | curses.A_BOLD
                if key in self.reached:
                    return "done", curses.color_pair(3)
                return "pending", curses.color_pair(2)

            threshold = PHASES[index][1]
            done = key in self.reached or (self.return_code == 0) or (self.mode == "running" and self.progress >= threshold + 0.02)
            current = self.mode == "running" and key == self.phase and not done

            # Keep receipt/proof from looking stuck. While REE is running at high
            # soft progress, show "Run REE" as the active stage until proof hashes
            # actually arrive.
            if self.mode == "running" and self.phase == "ree" and key == "ree":
                current = True
                done = False

            if done:
                return "done", curses.color_pair(3)
            if current:
                return "current", curses.color_pair(6) | curses.A_BOLD
            return "pending", curses.color_pair(2)

        question, mid, status, resolves, delphi_model, sources, outcomes, prompt_ctx = self.market_summary()
        full_prompt = prompt_ctx or self.market_input or ""
        prompt_question = extract_question(full_prompt)
        if not question and prompt_question:
            question = prompt_question
        prompt_sources = extract_sources(full_prompt)
        prompt_rules = extract_rules(full_prompt)
        prompt_outcomes = extract_outcome_lines(full_prompt)

        oracle_hash = self.latest_line_value("oracle evidence hash") or self.latest_line_value("evidence hash") or self.latest_line_value("oracle hash")
        ipfs = self.latest_line_value("ipfs cid")
        combined = self.latest_line_value("combined hash")
        ree_hash = self.latest_line_value("ree receipt hash")
        saved = self.latest_saved_file()
        receipt_path = self.latest_ree_receipt_path()

        pct = int(self.progress * 100)
        elapsed = int(((self.finished_at or time.time()) - self.started_at)) if self.started_at else 0
        run_status = "SUCCESS" if self.return_code == 0 else ("FAILED" if self.return_code not in (None, 0) else self.phase.upper())
        status_attr = curses.color_pair(3) if self.return_code == 0 else curses.color_pair(4) if self.return_code not in (None, 0) else curses.color_pair(5)

        # Mockup-like dimensions: compact logs, bigger proof/settlement clarity.
        market_h = 12
        summary_h = max(14, min(19, usable_h - 19))
        evidence_h = 6
        if usable_h < 38:
            market_h = 10
            summary_h = max(10, usable_h - 17)
            evidence_h = 5

        exec_h = 5
        pipeline_h = 7
        proof_h = 12
        verify_h = 5
        logs_h = max(7, usable_h - exec_h - pipeline_h - proof_h - verify_h - 4)

        # ── LEFT: MARKET DETAILS ─────────────────────────────────────────
        lx, y, bw, bh = box(1, top, left_w, market_h, "⋈ MARKET DETAILS")
        yy = y + 2
        yy = put_kv(lx + 2, yy, bw - 4, "Question", question or "—", curses.color_pair(1))
        if mid:
            yy = put_kv(lx + 2, yy, bw - 4, "Market ID", f"{mid[:10]}...{mid[-6:]}" if len(mid) > 18 else mid, curses.color_pair(3))
        if status or resolves:
            yy = put_kv(lx + 2, yy, bw - 4, "Status", f"{status} · closed {resolves} UTC".strip(" ·"), curses.color_pair(3) if status == "settled" else curses.color_pair(2))
        if delphi_model:
            yy = put_kv(lx + 2, yy, bw - 4, "Judge", delphi_model, curses.color_pair(3))
        if sources:
            yy = put_kv(lx + 2, yy, bw - 4, "Sources", sources, curses.color_pair(3))
        elif prompt_sources:
            yy = put_kv(lx + 2, yy, bw - 4, "Sources", " · ".join(prompt_sources), curses.color_pair(3))
        if outcomes:
            yy = put_kv(lx + 2, yy, bw - 4, "Outcomes", outcomes, curses.color_pair(3))
        elif prompt_outcomes:
            yy = put_kv(lx + 2, yy, bw - 4, "Outcomes", " · ".join(prompt_outcomes), curses.color_pair(3))

        # ── LEFT: SETTLEMENT SUMMARY ─────────────────────────────────────
        lx, y, bw, bh = box(1, top + market_h + 1, left_w, summary_h, "🏆 SETTLEMENT SUMMARY")
        yy = y + 2
        summary_lines: list[tuple[str, int]] = []
        if full_prompt:
            summary_lines.append(("You are a prediction market settlement judge.", curses.color_pair(2)))
            summary_lines.append(("Your task is to determine the correct outcome.", curses.color_pair(2)))
            summary_lines.append(("", curses.color_pair(2)))
            if question or prompt_question:
                summary_lines.append(("Question:", curses.color_pair(3) | curses.A_BOLD))
                summary_lines.append((question or prompt_question, curses.color_pair(1)))
                summary_lines.append(("", curses.color_pair(2)))
            if prompt_sources or sources:
                summary_lines.append(("Data Sources:", curses.color_pair(3) | curses.A_BOLD))
                for s in (prompt_sources or sources.split(" · "))[:4]:
                    summary_lines.append((f"• {s}", curses.color_pair(1)))
                summary_lines.append(("", curses.color_pair(2)))
            if prompt_rules:
                summary_lines.append(("Settlement Rules (short):", curses.color_pair(3) | curses.A_BOLD))
                for r in prompt_rules[:4]:
                    summary_lines.append((f"• {r}", curses.color_pair(1)))
            elif outcomes or prompt_outcomes:
                summary_lines.append(("Valid outcomes:", curses.color_pair(3) | curses.A_BOLD))
                for o in (prompt_outcomes or outcomes.split(" · "))[:5]:
                    summary_lines.append((f"• {o}", curses.color_pair(1)))
        else:
            summary_lines.append(("Settlement details appear here after market metadata loads.", curses.color_pair(2)))

        for text, attr in summary_lines:
            if yy >= y + bh - 1:
                break
            if not text:
                yy += 1
                continue
            wrapped = wrap_text(text, bw - 6)
            for j, part in enumerate(wrapped[:3]):
                if yy >= y + bh - 1:
                    break
                self.put(stdscr, yy, lx + 2, part[:bw - 4], attr)
                yy += 1

        # ── LEFT: ORACLE EVIDENCE ────────────────────────────────────────
        lx, y, bw, bh = box(1, top + market_h + summary_h + 2, left_w, evidence_h, "🔒 ORACLE EVIDENCE")
        yy = y + 2
        yy = put_kv(lx + 2, yy, bw - 4, "Oracle Hash", oracle_hash or "—", curses.color_pair(3) if oracle_hash else curses.color_pair(2))
        yy = put_kv(lx + 2, yy, bw - 4, "IPFS CID", ipfs or "—", curses.color_pair(3) if ipfs else curses.color_pair(2))

        # ── RIGHT: EXECUTION OVERVIEW ────────────────────────────────────
        rx, y, bw, bh = box(right_x, top, right_w, exec_h, "⋆ EXECUTION OVERVIEW")
        yy = y + 2
        self.put(stdscr, yy, rx + 2, "Status", curses.color_pair(2))
        self.put(stdscr, yy, rx + 14, run_status, status_attr | curses.A_BOLD)
        self.put(stdscr, yy, rx + 32, f"Progress {pct}%", curses.color_pair(1))
        if elapsed:
            self.put(stdscr, yy, rx + bw - 16, f"Elapsed {elapsed}s", curses.color_pair(2))
        yy += 1
        bar_w = max(10, bw - 26)
        fill = int(bar_w * self.progress)
        bar_attr = curses.color_pair(3) if self.return_code == 0 else curses.color_pair(4) if self.return_code not in (None, 0) else curses.color_pair(6)
        self.put(stdscr, yy, rx + 14, "█" * fill + "░" * (bar_w - fill), bar_attr)

        # ── RIGHT: PIPELINE ──────────────────────────────────────────────
        rx, y, bw, bh = box(right_x, top + exec_h + 1, right_w, pipeline_h, "⋆ PIPELINE")
        yy = y + 2

        # Short labels keep the pipeline clean and prevent broken words like
        # "fro m Delphi" or "bl ock into prompt" on smaller terminals.
        pipeline_items = [
            ("fetch", "Fetch"),
            ("oracle", "Verify"),
            ("ipfs", "IPFS"),
            ("inject", "Inject"),
            ("ree", "Run REE"),
            ("receipt", "Proof"),
            ("done", "Done"),
        ]

        step_count = len(pipeline_items)
        usable_w = max(28, bw - 8)

        # Reserve right side for the RUNNING indicator so it never collides
        # with the Done stage.
        reserved_right = 18 if self.mode == "running" else 2
        steps_w = max(28, usable_w - reserved_right)
        col_w = max(9, steps_w // step_count)

        icon_y = yy
        label_y = yy + 2
        guide_y = yy + 1

        # Light connector line between steps.
        for i in range(step_count - 1):
            sx = rx + 4 + (i * col_w)
            ex = rx + 4 + ((i + 1) * col_w)
            if ex > sx + 2:
                self.put(stdscr, guide_y, sx + 2, "─" * max(1, ex - sx - 3), curses.color_pair(2))

        current_label = ""
        for i, (key, label) in enumerate(pipeline_items):
            col_x = rx + 4 + i * col_w
            state, attr = progress_state(key, i)
            marker = "✓" if state == "done" else "➜" if state == "current" else "✕" if state == "failed" else "○"

            if state == "current":
                current_label = {
                    "fetch": "Fetch market from Delphi",
                    "oracle": "Fetch and verify oracle evidence",
                    "ipfs": "Pin evidence to IPFS",
                    "inject": "Inject oracle block into prompt",
                    "ree": "Run Gensyn REE inference",
                    "receipt": "Generate combined proof",
                    "done": "Done",
                }.get(key, label)

            self.put(stdscr, icon_y, col_x, marker, attr | curses.A_BOLD)
            self.put(stdscr, label_y, col_x, label[:col_w - 1], attr)

        if self.mode == "running":
            spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 8) % 10]
            running = f"{spinner} RUNNING {pct}%"
            self.put(stdscr, icon_y, rx + bw - len(running) - 3, running, curses.color_pair(6) | curses.A_BOLD)

            # Full current step appears on its own line, so labels stay clean.
            if current_label and y + bh - 2 > label_y:
                self.put(stdscr, y + bh - 2, rx + 4, f"Current step: {current_label}"[:bw - 28], curses.color_pair(6))
        elif self.return_code == 0:
            self.put(stdscr, y + bh - 2, rx + bw - 14, "COMPLETE", curses.color_pair(3) | curses.A_BOLD)
        elif self.return_code not in (None, 0):
            self.put(stdscr, y + bh - 2, rx + bw - 12, "FAILED", curses.color_pair(4) | curses.A_BOLD)

        # ── RIGHT: PROOF PACKAGE ─────────────────────────────────────────
        rx, y, bw, bh = box(right_x, top + exec_h + pipeline_h + 2, right_w, proof_h, "⋆ PROOF PACKAGE")
        yy = y + 2
        self.put(stdscr, yy, rx + 2, "Artifact", curses.color_pair(1) | curses.A_BOLD)
        self.put(stdscr, yy, rx + 28, "Value / Status", curses.color_pair(1) | curses.A_BOLD)
        yy += 1
        self.put(stdscr, yy, rx + 2, "─" * (bw - 4), curses.color_pair(2)); yy += 1
        artifacts = [
            ("Combined Hash", combined or "—"),
            ("Oracle Hash", oracle_hash or "—"),
            ("REE Receipt Hash", ree_hash or "—"),
            ("IPFS CID", ipfs or "—"),
            ("Receipt Path", receipt_path or "—"),
            ("Saved File", saved or "—"),
            ("Saved At", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.finished_at)) if self.finished_at else "—"),
        ]
        for name, val in artifacts:
            if yy >= y + bh - 1:
                break
            ok = val != "—"
            self.put(stdscr, yy, rx + 2, "☑" if ok else "·", curses.color_pair(3) if ok else curses.color_pair(2))
            self.put(stdscr, yy, rx + 5, name[:21], curses.color_pair(1))
            self.put(stdscr, yy, rx + 28, compact_value(val, bw - 31), curses.color_pair(3) if ok else curses.color_pair(2))
            yy += 1

        # ── RIGHT: VERIFY ────────────────────────────────────────────────
        rx, y, bw, bh = box(right_x, top + exec_h + pipeline_h + proof_h + 3, right_w, verify_h, "⋆ VERIFY")
        yy = y + 2
        if self.return_code == 0:
            self.put(stdscr, yy, rx + 2, "✓ Oracle data + REE execution cryptographically linked", curses.color_pair(3)); yy += 1
            target = receipt_path or "<receipt.json>"
            self.put(stdscr, yy, rx + 2, compact_value(f"python3 ree.py verify --receipt-path {target}", bw - 4), curses.color_pair(2))
        elif receipt_path:
            self.put(stdscr, yy, rx + 2, "REE receipt path detected:", curses.color_pair(3)); yy += 1
            self.put(stdscr, yy, rx + 2, compact_value(receipt_path, bw - 4), curses.color_pair(3))
        else:
            self.put(stdscr, yy, rx + 2, "Verification command appears after receipt is generated.", curses.color_pair(2))

        # ── RIGHT: RECENT LOGS ───────────────────────────────────────────
        logs_y = top + exec_h + pipeline_h + proof_h + verify_h + 4
        rx, y, bw, bh = box(right_x, logs_y, right_w, logs_h, "⋆ RECENT LOGS")
        yy = y + 2
        self.put(stdscr, y, rx + bw - 10, " Live ● ", curses.color_pair(3) if self.mode == "running" else curses.color_pair(2))

        def phase_log_status(key: str, i: int) -> tuple[str, int]:
            state, attr = progress_state(key, i)
            if self.return_code == 0 or state == "done":
                return "SUCCESS", curses.color_pair(3)
            if state == "current":
                return f"RUNNING ({pct}%)", curses.color_pair(6) | curses.A_BOLD
            if state == "failed":
                return "FAILED", curses.color_pair(4) | curses.A_BOLD
            return "PENDING", curses.color_pair(2)

        now_label = time.strftime("%H:%M:%S", time.localtime())
        for i, (key, _, label) in enumerate(PHASES):
            if yy >= y + bh - 1:
                break
            status_txt, sattr = phase_log_status(key, i)
            if status_txt == "PENDING" and i > 5 and bh < 10:
                continue
            prefix = now_label if self.mode == "running" and key == self.phase else "        "
            left = f"[{prefix}] {label}"
            dots = "." * max(2, bw - len(left) - len(status_txt) - 8)
            self.put(stdscr, yy, rx + 2, left[:bw - 18], curses.color_pair(3) if status_txt == "SUCCESS" else curses.color_pair(2))
            self.put(stdscr, yy, rx + 2 + min(len(left), bw - 18), f" {dots} ", curses.color_pair(2))
            self.put(stdscr, yy, rx + bw - len(status_txt) - 3, status_txt, sattr)
            yy += 1

        if receipt_path and yy < y + bh - 1:
            yy += 1
            self.put(stdscr, yy, rx + 2, "REE receipt saved on this machine:", curses.color_pair(3) | curses.A_BOLD); yy += 1
            if yy < y + bh - 1:
                self.put(stdscr, yy, rx + 2, compact_value(receipt_path, bw - 4), curses.color_pair(3))

    def draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        self.draw_nav(stdscr, w)

        if self.screen == "input":
            self.draw_input_page(stdscr, h, w)
        else:
            self.draw_results_page(stdscr, h, w)

        self.draw_footer(stdscr, h, w)
        stdscr.noutrefresh()
        curses.doupdate()

    # ── edit popup ────────────────────────────────────────────────────────

    def edit_input(self, stdscr: curses.window) -> bool:
        """Input editor.

        Enter or Esc accepts the input and returns to the home screen.
        Multiline paste is supported; pasted newlines are preserved.
        Users then press r to run, exactly like the old TUI.
        """
        h, w = stdscr.getmaxyx()
        pw = min(w - 4, 130)
        ph = min(h - 4, 24)
        py = max(1, (h - ph) // 2)
        px = max(0, (w - pw) // 2)

        try:
            win = curses.newwin(ph, pw, py, px)
        except curses.error:
            return False

        win.keypad(True)
        win.nodelay(False)

        try:
            curses.curs_set(1)
        except curses.error:
            pass

        lines = self.market_input.splitlines() or [""]
        cy = len(lines) - 1
        cx = len(lines[cy])
        top = 0

        while True:
            win.erase()
            try:
                win.border()
            except curses.error:
                pass

            iw = max(10, pw - 4)
            editor_h = max(3, ph - 8)

            try:
                win.addstr(1, 2, "ORACLEREE INPUT", curses.A_BOLD)
                win.addstr(2, 2, "Paste Market URL, Market ID, or full settlement prompt.", curses.A_DIM)
                win.addstr(3, 2, "Enter done  ·  Esc done  ·  r run after returning", curses.A_DIM)
                win.addstr(4, 2, "─" * iw, curses.A_DIM)
            except curses.error:
                pass

            if cy < top:
                top = cy
            elif cy >= top + editor_h:
                top = cy - editor_h + 1

            for row in range(editor_h):
                li = top + row
                try:
                    win.addstr(5 + row, 2, " " * iw)
                    if li < len(lines):
                        text = lines[li]
                        win.addstr(5 + row, 2, text[:iw])
                except curses.error:
                    pass

            footer_y = ph - 2
            info = f"Lines: {len(lines)}  Chars: {len(chr(10).join(lines))}"
            try:
                win.addstr(footer_y, 2, info[:iw], curses.A_DIM)
            except curses.error:
                pass

            screen_y = 5 + (cy - top)
            screen_x = 2 + min(cx, iw - 1)
            try:
                win.move(screen_y, screen_x)
            except curses.error:
                pass

            win.refresh()

            try:
                key = win.get_wch()
            except curses.error:
                continue

            if key in ("\x1b", "\n", "\r", curses.KEY_ENTER):
                self.market_input = "\n".join(lines).strip()
                self.status = "Input ready. Press r to run."
                break

            if key in (curses.KEY_BACKSPACE, "\x7f", "\x08"):
                if cx > 0:
                    current = lines[cy]
                    lines[cy] = current[:cx - 1] + current[cx:]
                    cx -= 1
                elif cy > 0:
                    prev_len = len(lines[cy - 1])
                    lines[cy - 1] += lines[cy]
                    del lines[cy]
                    cy -= 1
                    cx = prev_len
                continue

            if key == curses.KEY_DC:
                current = lines[cy]
                if cx < len(current):
                    lines[cy] = current[:cx] + current[cx + 1:]
                elif cy + 1 < len(lines):
                    lines[cy] += lines[cy + 1]
                    del lines[cy + 1]
                continue

            if key == curses.KEY_LEFT:
                if cx > 0:
                    cx -= 1
                elif cy > 0:
                    cy -= 1
                    cx = len(lines[cy])
                continue
            if key == curses.KEY_RIGHT:
                if cx < len(lines[cy]):
                    cx += 1
                elif cy + 1 < len(lines):
                    cy += 1
                    cx = 0
                continue
            if key == curses.KEY_UP:
                cy = max(0, cy - 1)
                cx = min(cx, len(lines[cy]))
                continue
            if key == curses.KEY_DOWN:
                cy = min(len(lines) - 1, cy + 1)
                cx = min(cx, len(lines[cy]))
                continue
            if key == curses.KEY_HOME:
                cx = 0
                continue
            if key == curses.KEY_END:
                cx = len(lines[cy])
                continue

            if isinstance(key, str):
                parts = re.split(r'(\r\n|\n|\r)', key)
                for part in parts:
                    if part in ("\r\n", "\n", "\r"):
                        current = lines[cy]
                        remainder = current[cx:]
                        lines[cy] = current[:cx]
                        lines.insert(cy + 1, remainder)
                        cy += 1
                        cx = 0
                    elif part:
                        current = lines[cy]
                        lines[cy] = current[:cx] + part + current[cx:]
                        cx += len(part)
                continue

        try:
            curses.curs_set(0)
        except curses.error:
            pass

        del win
        stdscr.touchwin()
        stdscr.refresh()
        return False

    # ── main loop ─────────────────────────────────────────────────────────

    def run(self, stdscr: curses.window) -> None:
        self.init_colors()
        stdscr.nodelay(True)
        stdscr.timeout(80)
        stdscr.keypad(True)

        try:
            curses.curs_set(0)
        except curses.error:
            pass

        # first-run setup
        if not ENV_FILE.exists():
            run_setup(stdscr)
            stdscr.clear()
            stdscr.nodelay(True)
            stdscr.timeout(80)

        while True:
            while True:
                try:
                    kind, payload = self.events.get_nowait()
                    self.handle_event(kind, payload)
                except queue.Empty:
                    break

            self.tick_progress()
            self.draw(stdscr)

            try:
                key = stdscr.get_wch()
            except curses.error:
                continue

            if self.mode == "running":
                continue

            # q is the only quit key. Esc is used as "done"/neutral on the input screen.
            if key == "q" or key == ord("q"):
                break

            if self.screen == "input":
                # If this is typed/pasted text, drain the rest of the paste before redraw.
                if isinstance(key, str) and key not in ("r", "q", "e"):
                    key = self.collect_paste_buffer(stdscr, key)

                if key == "r" or key == ord("r"):
                    self.start_run()
                elif key == "e" or key == ord("e"):
                    self.reset()
                else:
                    self.handle_input_page_key(key)
            else:
                if key == "e" or key == ord("e"):
                    self.reset()
                elif key == "r" or key == ord("r"):
                    self.start_run()


def main() -> int:
    if not Path(__file__).parent.joinpath("oracle_ree.py").exists():
        print("oracle_ree.py not found.")
        print("Make sure you are in the oracle-ree directory:")
        print("  cd oracle-ree && python3 ree.py")
        return 2
    tui = TUI()
    curses.wrapper(tui.run)
    return 0 if tui.return_code in (None, 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
