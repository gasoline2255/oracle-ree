#!/usr/bin/env python3
"""
OracleREE TUI — Trustless oracle grounding for Gensyn Delphi settlement.
Run: python3 ree.py
Version: strict preflight guard — no dashboard until URL + prompt verify
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
    # Prefer the real Delphi API market address. UUIDs from app.delphi.fyi URLs
    # are not valid for /markets/{id}; they must be resolved first.
    m = re.search(r"0x[a-fA-F0-9]{38,42}(?![a-fA-F0-9])", s)
    if m:
        return m.group(0)
    u = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s)
    if u:
        return u.group(0)
    return None

def extract_0x_market_id(s: str) -> Optional[str]:
    m = re.search(r"0x[a-fA-F0-9]{38,42}(?![a-fA-F0-9])", s or "")
    return m.group(0) if m else None

def extract_uuid(s: str) -> Optional[str]:
    u = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", s or "", re.I)
    return u.group(0) if u else None

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


def normalize_prompt_text(text: str) -> str:
    """Normalize prompt text for integrity comparison.

    This is intentionally strict enough to catch threshold/rule edits, while
    ignoring harmless whitespace and label/casing differences.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def extract_question_from_prompt_text(text: str) -> str:
    """Best-effort question extraction from a pasted settlement prompt.

    Some Delphi prompts do not contain an explicit QUESTION: block. In that case
    avoid returning generic instruction lines like "You are a prediction market
    judge" because that creates false mismatches during preflight.
    """
    text = text or ""
    m = re.search(
        r"QUESTION\s*:\s*(.+?)(?:\n\s*\n|DATA SOURCES\s*:|SETTLEMENT RULES\s*:|VALID OUTCOMES|$)",
        text,
        re.I | re.S,
    )
    if m:
        return " ".join(m.group(1).split())

    # Fallback for simpler sports/event prompts. Prefer the line that actually
    # describes settlement, not the generic judge instruction.
    preferred: list[str] = []
    generic_prefixes = (
        "settlement prompt", "market url", "valid outcomes", "you are",
        "your task", "output exactly", "web page data", "source:",
    )
    for line in text.splitlines():
        line = line.strip()
        low = line.lower()
        if len(line) <= 25 or low.startswith(generic_prefixes):
            continue
        if any(k in low for k in ["settle based", "official result", "match between", "will ", "who "]):
            preferred.append(line)

    if preferred:
        return " ".join(preferred[0].split())
    return ""


def text_similarity(a: str, b: str) -> float:
    """Small dependency-free token similarity for preflight guardrails."""
    aw = set(re.findall(r"[a-z0-9]+", (a or "").lower()))
    bw = set(re.findall(r"[a-z0-9]+", (b or "").lower()))
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(1, len(aw | bw))


def prompt_belongs_to_market(prompt: str, official_question: str, outcomes: list) -> tuple[bool, str]:
    """Return whether a pasted prompt appears to belong to the selected market.

    Strict exact prompt comparison is still handled separately. This check only
    prevents obviously wrong URL + prompt pairs from opening the dashboard.
    It avoids false negatives for prompts that do not contain QUESTION: and only
    describe a sports/event settlement in natural language.
    """
    prompt = prompt or ""
    official_question = official_question or ""
    pasted_question = extract_question_from_prompt_text(prompt)

    if not official_question:
        return True, "no official question available"

    if pasted_question:
        sim = text_similarity(official_question, pasted_question)
        # Explicit QUESTION: blocks should be reasonably close. Natural-language
        # event prompts can be shorter/different, so allow a lower threshold when
        # the prompt lacks an explicit QUESTION: label.
        has_explicit_question = bool(re.search(r"QUESTION\s*:", prompt, re.I))
        threshold = 0.72 if has_explicit_question else 0.18
        if sim >= threshold:
            return True, f"question similarity {sim:.2f}"

    # Secondary guard: if most non-generic outcomes are present in the prompt,
    # it is very likely the prompt belongs to the same market even if the wording
    # differs. This handles sports markets like Team A / Draw / Team B.
    prompt_l = prompt.lower()
    meaningful = []
    for o in outcomes or []:
        ov = str(o or "").strip()
        if not ov or ov.lower() in {"yes", "no", "draw"}:
            continue
        meaningful.append(ov)
    if meaningful:
        hits = sum(1 for o in meaningful if o.lower() in prompt_l)
        if hits >= max(1, min(2, len(meaningful))):
            return True, f"outcome name match {hits}/{len(meaningful)}"

    return False, "question/outcomes do not match selected market"


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


def market_0x_id_from_market(market: dict) -> str:
    """Extract the real Delphi 0x market id from any market payload shape."""
    if not isinstance(market, dict):
        return ""
    candidates = [
        market.get("id"),
        market.get("marketId"),
        market.get("market_id"),
        market.get("address"),
        market.get("marketAddress"),
        market.get("contractAddress"),
    ]
    for val in candidates:
        mid = extract_0x_market_id(str(val or ""))
        if mid:
            return mid
    # Last-resort scan of full JSON. This prevents accidentally passing app UUIDs
    # to oracle_ree.py when Delphi returns a slightly different object shape.
    try:
        mid = extract_0x_market_id(json.dumps(market))
        return mid or ""
    except Exception:
        return ""

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
        self.settlement_prompt_input = ""
        self.market_ref_input = ""
        self.preflight_market_id = ""
        self.active_input_field = "prompt"  # prompt | market
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
        # Values parsed from oracle_ree.py stdout. This keeps the dashboard useful
        # even when the user pasted a Delphi URL/UUID or full settlement prompt
        # and the TUI itself cannot resolve metadata before oracle_ree.py does.
        self.resolved_market_id: str = ""
        self.resolved_question: str = ""
        self.resolved_delphi_model: str = ""
        self.resolved_ree_model: str = ""
        self.resolved_classification: str = ""
        self.oracle_result: str = ""
        self.prompt_source: str = ""
        self.prompt_match: str = ""
        self.question_match: str = ""
        self.verification_mode: str = ""
        self.prompt_warning: str = ""
        self.official_prompt_hash: str = ""
        self.provided_prompt_hash: str = ""

    # ── market fetch (background) ──────────────────────────────────────────

    def trigger_fetch_for_id(self, mid: str) -> None:
        """Fetch Delphi metadata for an already-resolved 0x market ID."""
        if not mid or mid == self.last_fetched_id:
            return
        self.last_fetched_id = mid
        self.market_data = None
        self.fetching_market = True

        def worker():
            data = fetch_market_info(mid)
            self.events.put(("market", data))

        threading.Thread(target=worker, daemon=True).start()

    def trigger_fetch(self) -> None:
        # Only the 0x address works with Delphi API. Do not fetch UUID directly.
        mid = extract_0x_market_id(self.market_ref_input) or extract_0x_market_id(self.market_input) or extract_0x_market_id(self.resolved_market_id)
        if mid:
            self.trigger_fetch_for_id(mid)

    # ── run ───────────────────────────────────────────────────────────────

    def backend_market_argument(self) -> str:
        """Return the safest argument to pass to oracle_ree.py.

        app.delphi.fyi URLs contain a UUID. The Delphi API usually needs the
        0x market address. If the user pasted only the UUID, pass the canonical
        Delphi URL so oracle_ree.py can resolve the page/question instead of
        failing against /markets/{uuid}.
        """
        # Canonical mode now requires both fields:
        # 1) user-provided settlement prompt
        # 2) Delphi Market URL / 0x Market ID
        # We pass both together so oracle_ree.py can anchor to the real market
        # while still comparing the pasted prompt against Delphi's canonical prompt.
        prompt = self.settlement_prompt_input.strip()
        market_ref = self.market_ref_input.strip()

        # IMPORTANT: after preflight, always pass the resolved 0x market ID
        # to oracle_ree.py. Passing the app UUID again can fail inside the
        # backend resolver and incorrectly open the failed dashboard.
        if market_ref:
            canonical_ref = self.preflight_market_id or self.resolved_market_id or extract_0x_market_id(market_ref) or market_ref
            if not extract_0x_market_id(canonical_ref):
                uuid = extract_uuid(canonical_ref)
                if uuid and canonical_ref.lower() == uuid.lower():
                    canonical_ref = f"https://app.delphi.fyi/market/{uuid}"
        else:
            canonical_ref = ""

        if prompt and canonical_ref:
            return (
                "MARKET URL / MARKET ID:\n"
                f"{canonical_ref}\n\n"
                "SETTLEMENT PROMPT:\n"
                f"{prompt}"
            )

        # Fallback for old saved state / backwards compatibility.
        raw = self.market_input.strip()
        if extract_0x_market_id(raw):
            return extract_0x_market_id(raw) or raw
        uuid = extract_uuid(raw)
        if uuid and raw.lower() == uuid.lower():
            return f"https://app.delphi.fyi/market/{uuid}"
        return raw

    def fetch_market_by_ref_for_preflight(self, market_ref: str) -> tuple[Optional[dict], str]:
        """Resolve Market URL / UUID / 0x ID before switching to dashboard.

        Returns (market, error). This prevents the UI from showing the proof
        dashboard for obviously wrong URLs or mismatched prompts.
        """
        market_ref = (market_ref or "").strip()
        if not market_ref:
            return None, "Market URL or 0x Market ID is required"

        # Direct 0x market ID path.
        mid = extract_0x_market_id(market_ref)
        if mid:
            data = fetch_market_info(mid)
            if data:
                return data, ""
            return None, f"Could not fetch Delphi market for ID: {mid}"

        # Delphi app URL / UUID path.
        uuid = extract_uuid(market_ref)
        if not uuid:
            return None, "Market reference must contain a Delphi URL, UUID, or 0x market ID"

        api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
        if not api_key:
            return None, "DELPHI_API_ACCESS_KEY is missing"

        # Try to fetch the Delphi page and get the visible market question.
        page_question = ""
        url = market_ref if market_ref.startswith("http") else f"https://app.delphi.fyi/market/{uuid}"
        try:
            import requests
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 OracleREE/1.0"}, timeout=10)
            html = resp.text or ""
            title_match = re.search(r'<title[^>]*>([^<]+)</title>|"og:title"[^>]*content="([^"]+)"', html, re.I)
            if title_match:
                page_question = (title_match.group(1) or title_match.group(2) or "").strip()
                page_question = re.sub(r"\s*[-|]\s*Delphi.*$", "", page_question).strip()
        except Exception:
            pass

        # Step 1: try to extract 0x market ID directly from the page HTML
        try:
            import requests
            ox_match = re.search(r'0x[a-fA-F0-9]{40}(?![a-fA-F0-9])', html)
            if ox_match:
                mid = ox_match.group(0)
                data = fetch_market_info(mid)
                if data:
                    return data, ""
        except Exception:
            pass

        # Step 2: search by metadataUri UUID match
        try:
            import requests
            for status in ["open", "settled", "expired"]:
                r = requests.get(
                    "https://api.delphi.fyi/markets",
                    headers={"x-api-key": api_key},
                    params={"limit": 100, "status": status},
                    timeout=15,
                )
                if not r.ok:
                    continue
                for m in r.json().get("markets", []):
                    metadata_uri = str(m.get("metadataUri", ""))
                    if uuid.replace("-", "") in metadata_uri.replace("-", ""):
                        return m, ""
        except Exception as exc:
            return None, f"Market lookup failed: {exc}"

        # Step 3: match by page title — pick highest similarity, no blocking
        if page_question:
            try:
                import requests
                best_match = None
                best_score = 0.0
                for status in ["open", "settled", "expired"]:
                    r = requests.get(
                        "https://api.delphi.fyi/markets",
                        headers={"x-api-key": api_key},
                        params={"limit": 100, "status": status},
                        timeout=15,
                    )
                    if not r.ok:
                        continue
                    for m in r.json().get("markets", []):
                        q = (m.get("metadata") or {}).get("question", "")
                        score = text_similarity(page_question, q)
                        if score > best_score:
                            best_score = score
                            best_match = m
                if best_match and best_score >= 0.7:
                    return best_match, ""
            except Exception as exc:
                return None, f"Market lookup failed: {exc}"

        return None, f"Could not resolve market from URL. Try pasting the 0x market ID instead."

    def validate_inputs_before_run(self) -> tuple[bool, str]:
        """Validate both input boxes before showing the proof dashboard."""
        prompt = self.settlement_prompt_input.strip()
        market_ref = self.market_ref_input.strip()

        if not prompt:
            self.active_input_field = "prompt"
            return False, "Paste the settlement prompt first"
        if not market_ref:
            self.active_input_field = "market"
            return False, "Paste the Market URL or 0x Market ID"

        market, err = self.fetch_market_by_ref_for_preflight(market_ref)
        if err or not market:
            self.active_input_field = "market"
            return False, "INPUT ERROR: " + (err or "Could not resolve market")

        meta = market.get("metadata") or {}
        model_info = meta.get("model") or {}
        official_prompt = (
            model_info.get("prompt_context")
            or model_info.get("promptContext")
            or market.get("settlementPrompt")
            or ""
        )
        official_question = meta.get("question", "") or market.get("question", "") or ""
        outcomes_for_match = meta.get("outcomes") or market.get("outcomes") or []

        # Always ensure the pasted prompt belongs to the selected visible market.
        # This is intentionally smarter than a single QUESTION: comparison because
        # many sports prompts do not contain a QUESTION: field and instead say
        # "Settle based on the official result...".
        belongs, reason = prompt_belongs_to_market(prompt, official_question, outcomes_for_match)
        if not belongs:
            self.active_input_field = "prompt"
            return False, "INPUT ERROR: Settlement prompt does not match the Delphi market URL/ID"

        # Hard block if prompt does not match the official Delphi prompt
        if official_prompt:
            if normalize_prompt_text(prompt) != normalize_prompt_text(official_prompt):
                self.active_input_field = "prompt"
                return False, "INPUT ERROR: Settlement prompt does not match the official Delphi prompt for this market. Cannot run."

        # Preload metadata so the dashboard is populated immediately after run starts.
        # IMPORTANT: the backend only accepts the real 0x market id. If preflight
        # cannot produce a 0x id, do NOT open the execution dashboard.
        resolved_0x = market_0x_id_from_market(market)
        if not resolved_0x:
            self.active_input_field = "market"
            return False, "INPUT ERROR: Delphi URL resolved, but no valid 0x market ID was found. Paste the 0x market ID."

        # Block running on markets that haven't closed or were cancelled
        market_status = market.get("status", "")
        resolves_at = market.get("resolvesAt", "")
        if market_status == "expired":
            self.active_input_field = "market"
            return False, "Market was cancelled/expired — no settlement data available."
        if market_status == "open" and resolves_at:
            from datetime import datetime, timezone
            try:
                close_time = datetime.fromisoformat(resolves_at.replace("Z", "+00:00"))
                if close_time > datetime.now(timezone.utc):
                    self.active_input_field = "market"
                    return False, f"Market is still LIVE — closes {resolves_at[:16].replace('T',' ')} UTC. Run OracleREE after market closes."
            except Exception:
                pass

        # Block running on markets that haven't closed or were cancelled
        market_status = market.get("status", "")
        resolves_at = market.get("resolvesAt", "")
        if market_status == "expired":
            self.active_input_field = "market"
            return False, "Market was cancelled/expired — no settlement data available."
        if market_status == "open" and resolves_at:
            from datetime import datetime, timezone
            try:
                close_time = datetime.fromisoformat(resolves_at.replace("Z", "+00:00"))
                if close_time > datetime.now(timezone.utc):
                    self.active_input_field = "market"
                    return False, f"Market is still LIVE — closes {resolves_at[:16].replace('T',' ')} UTC. Run OracleREE after market closes."
            except Exception:
                pass

        self.market_data = market
        self.preflight_market_id = resolved_0x
        self.resolved_market_id = resolved_0x
        self.resolved_question = official_question
        self.resolved_delphi_model = str(
            model_info.get("model_identifier")
            or model_info.get("modelIdentifier")
            or market.get("judgeModel", "")
            or ""
        )
        return True, "Input verified. Running canonical proof..."

    def start_run(self) -> None:
        if self.mode == "running":
            return

        # Official verification requires both fields.
        if not self.settlement_prompt_input.strip():
            self.status = "Paste the settlement prompt first"
            self.active_input_field = "prompt"
            return
        if not self.market_ref_input.strip():
            self.status = "Paste the Market URL or 0x Market ID"
            self.active_input_field = "market"
            return

        ok, message = self.validate_inputs_before_run()
        self.status = message
        if not ok:
            # Stay on the input screen. Do NOT show execution dashboard for
            # wrong URLs, unresolved markets, or prompt/market mismatches.
            return

        self.market_input = (
            "MARKET URL / MARKET ID:\n"
            f"{self.market_ref_input.strip()}\n\n"
            "SETTLEMENT PROMPT:\n"
            f"{self.settlement_prompt_input.strip()}"
        )

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
        # Keep metadata resolved during preflight so dashboard starts populated
        # and backend receives the 0x market ID instead of the app UUID.
        self.resolved_market_id = self.preflight_market_id or self.resolved_market_id
        self.resolved_question = self.resolved_question
        self.resolved_delphi_model = self.resolved_delphi_model
        self.resolved_ree_model = ""
        self.resolved_classification = ""
        self.oracle_result = ""
        self.prompt_source = ""
        self.prompt_match = ""
        self.question_match = ""
        self.verification_mode = ""
        self.prompt_warning = ""
        self.official_prompt_hash = ""
        self.provided_prompt_hash = ""
        self.left_scroll = 0
        self.log_scroll = 0

        # Fetch Delphi metadata only after Run, not on the first input page.
        self.trigger_fetch()

        backend_arg = self.backend_market_argument()
        cmd = ["python3", str(script), "--market", backend_arg]
        # Keep logs clean; backend output is enough for the proof dashboard.

        self._proc = None

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
                self._proc = proc
                if proc.stdout:
                    for raw in proc.stdout:
                        self.events.put(("line", raw.rstrip("\n")))
                proc.wait()
                rc = proc.returncode if proc.returncode is not None else 1
            except Exception as exc:
                self.events.put(("line", f"Error: {exc}"))
            finally:
                self._proc = None
                self.events.put(("done", rc))

        threading.Thread(target=worker, daemon=True).start()

    def reset(self) -> None:
        if self.mode == "running":
            return

        self.screen = "input"
        self.market_input = ""
        self.settlement_prompt_input = ""
        self.market_ref_input = ""
        self.preflight_market_id = ""
        self.active_input_field = "prompt"
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
        self.resolved_market_id = ""
        self.resolved_question = ""
        self.resolved_delphi_model = ""
        self.resolved_ree_model = ""
        self.resolved_classification = ""
        self.oracle_result = ""
        self.prompt_source = ""
        self.prompt_match = ""
        self.question_match = ""
        self.verification_mode = ""
        self.prompt_warning = ""
        self.official_prompt_hash = ""
        self.provided_prompt_hash = ""
        self.left_scroll = 0
        self.log_scroll = 0

    def add_log(self, line: str) -> None:
        self.logs.append(line)
        if len(self.logs) > 2000:
            self.logs = self.logs[-2000:]

    def set_phase(self, key: str) -> None:
        self.phase = key
        # Mark the selected phase and all previous phases as reached so the
        # pipeline turns green progressively instead of only at the end.
        seen = False
        for k, p, _ in PHASES:
            self.reached.add(k)
            if k == key:
                self.progress = max(self.progress, p)
                seen = True
                break
        if not seen:
            self.reached.add(key)

    def parse_line(self, line: str) -> None:
        low = line.lower()

        # Extract useful structured values from oracle_ree.py output.
        # This is important for URL / prompt input: oracle_ree.py resolves the
        # final 0x market ID, then the TUI can fetch/display full market details.
        def after_colon(txt: str) -> str:
            return txt.split(":", 1)[1].strip() if ":" in txt else ""

        if "[oracle] market id:" in low:
            mid = after_colon(line)
            if mid:
                self.resolved_market_id = mid
                self.trigger_fetch_for_id(mid)

        elif "matched market:" in low:
            m = re.search(r"0x[a-fA-F0-9]{40}", line)
            if m:
                self.resolved_market_id = m.group(0)
                self.trigger_fetch_for_id(self.resolved_market_id)

        elif "[oracle] question:" in low:
            q = after_colon(line)
            if q:
                self.resolved_question = q

        elif "[oracle] market:" in low:
            q = after_colon(line)
            if q and not self.resolved_question:
                self.resolved_question = q

        elif "[oracle] delphi model:" in low:
            self.resolved_delphi_model = after_colon(line)

        elif "[oracle] ree model:" in low:
            self.resolved_ree_model = after_colon(line)

        elif "[oracle] classification:" in low:
            self.resolved_classification = after_colon(line)

        elif "[oracle] prompt source:" in low:
            self.prompt_source = after_colon(line)

        elif "[oracle] prompt match:" in low:
            self.prompt_match = after_colon(line)

        elif "[oracle] question match:" in low:
            self.question_match = after_colon(line)

        elif "[oracle] verification mode:" in low:
            self.verification_mode = after_colon(line)

        elif "[oracle] official prompt hash:" in low:
            self.official_prompt_hash = after_colon(line)

        elif "[oracle] provided prompt hash:" in low:
            self.provided_prompt_hash = after_colon(line)

        elif "[oracle] prompt warning:" in low:
            self.prompt_warning = after_colon(line)

        elif "prompt source:" in low and not self.prompt_source:
            self.prompt_source = after_colon(line)

        elif "prompt match:" in low and not self.prompt_match:
            self.prompt_match = after_colon(line)

        elif "mode:" in low and not self.verification_mode:
            self.verification_mode = after_colon(line)

        elif "warning:" in low and not self.prompt_warning:
            self.prompt_warning = after_colon(line)

        elif "price verdict:" in low or "event verdict:" in low:
            verdict = after_colon(line)
            self.oracle_result = verdict
            m = re.search(r"outcome:\s*([^.;]+)", verdict, re.I)
            if m:
                self.oracle_result = m.group(1).strip().strip('"\'')

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
            "verification passed", "market:", "question:", "delphi model:", "event verdict", "price verdict",
            "prompt source:", "prompt match:", "question match:", "verification mode:",
            "official prompt hash:", "provided prompt hash:", "prompt warning:", "error"
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
                self.reached = {k for k, _, _ in PHASES}
                self.set_phase("done")
                self.progress = 1.0
            elif payload == 2:
                # Exit 2 = oracle evidence captured but no REE receipt
                self.status = "Oracle OK — REE receipt missing"
                # Mark all phases except done as reached
                for k, _, _ in PHASES:
                    if k != "done":
                        self.reached.add(k)
                self.phase = "receipt"
                self.progress = 0.95
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
        """Two-field input screen.

        Field 1: settlement prompt
        Field 2: Market URL / Market ID

        Tab/Enter switches fields. r runs after both fields are filled.
        """
        if key in ("\t", curses.KEY_BTAB):
            self.active_input_field = "market" if self.active_input_field == "prompt" else "prompt"
            self.status = "Ready"
            return

        if key in ("\n", "\r", curses.KEY_ENTER, 10, 13):
            if self.active_input_field == "prompt":
                self.active_input_field = "market"
                self.status = "Paste the Market URL or 0x Market ID"
            else:
                if self.settlement_prompt_input.strip() and self.market_ref_input.strip():
                    self.status = "Input ready. Press r to run."
                else:
                    self.status = "Both fields are required"
            return

        if key == "\x1b":
            if self.settlement_prompt_input.strip() and self.market_ref_input.strip():
                self.status = "Input ready. Press r to run."
            else:
                self.status = "Both fields are required for canonical verification"
            return

        target = "settlement_prompt_input" if self.active_input_field == "prompt" else "market_ref_input"
        current = getattr(self, target)

        if key in (curses.KEY_BACKSPACE, 127, 8, "\x7f", "\x08"):
            setattr(self, target, current[:-1])
            self.status = "Ready"
            return

        if key in (curses.KEY_DC,):
            setattr(self, target, "")
            self.status = "Ready"
            return

        if isinstance(key, int):
            return

        if isinstance(key, str):
            cleaned = key.replace("\x1b[200~", "").replace("\x1b[201~", "")
            # Keep multiline prompt content, but keep market URL/ID single-line.
            if self.active_input_field == "market":
                cleaned = " ".join(cleaned.replace("\r", "\n").splitlines()) if ("\n" in cleaned or "\r" in cleaned) else cleaned
            cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch in "\n\r\t ")
            if cleaned:
                setattr(self, target, current + cleaned)
                if self.settlement_prompt_input.strip() and self.market_ref_input.strip():
                    self.status = "Input ready. Press r to run."
                else:
                    self.status = "Ready"
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
        if self.mode == "running":
            keys = " c stop run   q quit "
        else:
            keys = " q quit   e new input   r rerun "
        self.put(stdscr, h - 1, 1, keys, curses.A_REVERSE)
        ver = " OracleREE · Gensyn Delphi settlement proof "
        self.put(stdscr, h - 1, max(0, w - len(ver) - 1), ver, curses.A_REVERSE)

    def draw_input_page(self, stdscr: curses.window, h: int, w: int) -> None:
        box_w = min(124, max(82, w - 12))
        box_h = 24
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
        blink = int(time.time() * 2) % 2 == 0
        cursor = "█" if blink else " "

        def draw_field(row: int, title: str, help_text: str, value: str, active: bool, multiline: bool = False) -> None:
            title_attr = curses.color_pair(6) | curses.A_BOLD if active else curses.color_pair(1) | curses.A_BOLD
            border_attr = curses.color_pair(6) if active else curses.color_pair(2)
            self.put(stdscr, y + row, inner_x, title, title_attr)
            if value.strip():
                self.put(stdscr, y + row, inner_x + field_w - 11, "[DEL=clear]", curses.color_pair(4))
            self.put(stdscr, y + row + 1, inner_x, help_text[:field_w], curses.color_pair(2))
            self.put(stdscr, y + row + 2, inner_x, "╭" + "─" * (field_w - 2) + "╮", border_attr)
            self.put(stdscr, y + row + 3, inner_x, "│", border_attr)
            self.put(stdscr, y + row + 3, inner_x + field_w - 1, "│", border_attr)
            self.put(stdscr, y + row + 4, inner_x, "╰" + "─" * (field_w - 2) + "╯", border_attr)
            if value.strip():
                display = " ".join(value.split())
                shown = compact_value(display, field_w - 8)
                line = "> " + shown + (" " + cursor if active else "")
                attr = curses.color_pair(6) if active else curses.color_pair(1)
            else:
                placeholder = "Paste here..."
                line = "> " + (cursor if active else placeholder)
                attr = curses.color_pair(6) if active else curses.color_pair(2)
            self.put(stdscr, y + row + 3, inner_x + 2, line[:field_w - 4], attr)

        draw_field(
            6,
            "1. SETTLEMENT PROMPT",
            "Paste the FULL settlement prompt from Delphi.",
            self.settlement_prompt_input,
            self.active_input_field == "prompt",
            multiline=True,
        )

        draw_field(
            12,
            "2. MARKET URL / MARKET ID",
            "Required anchor for canonical verification.",
            self.market_ref_input,
            self.active_input_field == "market",
        )

        prompt_ok = bool(self.settlement_prompt_input.strip())
        market_ok = bool(self.market_ref_input.strip())
        status = "READY TO RUN" if prompt_ok and market_ok else "BOTH FIELDS REQUIRED"
        status_attr = curses.color_pair(3) | curses.A_BOLD if prompt_ok and market_ok else curses.color_pair(5)
        self.put(stdscr, y + 18, inner_x, "INTEGRITY", curses.color_pair(1) | curses.A_BOLD)
        self.put(stdscr, y + 19, inner_x, "Prompt + market anchor will be compared before canonical proof.", curses.color_pair(2))
        self.put(stdscr, y + 20, inner_x, status, status_attr)

        controls = "TAB switch field   ENTER next/done   r run   q quit   DEL clear field"
        self.put(stdscr, y + 22, inner_x, controls[:field_w], curses.color_pair(2))

        if self.status and self.status not in ("Ready", "Input ready. Press r to run."):
            self.put(stdscr, y + box_h - 2, inner_x, compact_value(f"Status: {self.status}", field_w), curses.color_pair(5))

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

    def latest_proof_json(self) -> dict:
        """Read the latest OracleREE proof JSON when available.

        The backend can finish oracle evidence without producing a REE receipt.
        Reading the proof file lets the TUI show that honestly instead of
        showing a false mismatch or fake verify command.
        """
        saved = self.latest_saved_file()
        if not saved:
            return {}
        path = Path(saved)
        if not path.is_absolute():
            path = Path(__file__).parent / saved
        try:
            if path.exists():
                return json.loads(path.read_text())
        except Exception:
            return {}
        return {}

    def latest_ree_receipt_path(self) -> str:
        if self.ree_receipt_path:
            p = self.ree_receipt_path
            import re as _re
            m = _re.search(r"(/[^\s]+receipt_[0-9_]+\.json)", p)
            if m:
                return m.group(1)
            return p

        proof = self.latest_proof_json()
        receipt = proof.get("ree_receipt") if isinstance(proof, dict) else None
        if isinstance(receipt, dict):
            for key in ("path", "receipt_path", "file", "file_path"):
                val = receipt.get(key)
                if val:
                    return str(val)
        elif isinstance(receipt, str) and "/" in receipt:
            return receipt

        # Prefer the full local file path, not the later sha256 receipt hash summary.
        for line in reversed(self.logs):
            low = line.lower()
            if "receipt" in low and "/" in line and ("metadata" in low or "receipt_" in low):
                val = line.split(":", 1)[1].strip() if ":" in line else line.strip()
                if val and not val.startswith("sha256:"):
                    return val
        return ""

    def latest_ree_receipt_hash(self) -> str:
        proof = self.latest_proof_json()
        if isinstance(proof, dict):
            receipt = proof.get("ree_receipt")
            if isinstance(receipt, dict):
                for key in ("receipt_hash", "hash", "ree_receipt_hash"):
                    if receipt.get(key):
                        return str(receipt.get(key))
            verification = proof.get("verification") or {}
            if verification.get("ree_receipt_hash"):
                return str(verification.get("ree_receipt_hash"))
        return self.latest_line_value("ree receipt hash")

    def latest_combined_hash(self) -> str:
        proof = self.latest_proof_json()
        if isinstance(proof, dict):
            verification = proof.get("verification") or {}
            if verification.get("combined_hash"):
                return str(verification.get("combined_hash"))
        return self.latest_line_value("combined hash")

    def market_summary(self) -> tuple[str, str, str, str, str, str, str, str]:
        question = self.resolved_question or ""
        mid = self.resolved_market_id or extract_0x_market_id(self.market_input) or extract_uuid(self.market_input) or ""
        status = ""
        resolves = ""
        delphi_model = self.resolved_delphi_model or ""
        sources = ""
        outcomes = ""
        prompt_ctx = ""

        if self.market_data:
            meta = self.market_data.get("metadata") or {}
            model_info = meta.get("model") or {}
            question = meta.get("question", "") or self.market_data.get("question", "") or question
            mid = self.market_data.get("id", "") or mid
            status = str(self.market_data.get("status", "") or "")
            resolves = str(self.market_data.get("resolvesAt", "") or self.market_data.get("closeTime", "") or self.market_data.get("close_time", ""))[:16].replace("T", " ")
            delphi_model = str(model_info.get("model_identifier") or model_info.get("modelIdentifier") or self.market_data.get("judgeModel", "") or delphi_model)
            src = self.market_data.get("dataSources") or meta.get("dataSources") or []
            outs = meta.get("outcomes") or self.market_data.get("outcomes") or []
            sources = " · ".join(map(str, src))
            outcomes = " · ".join(map(str, outs))
            prompt_ctx = str(model_info.get("prompt_context") or model_info.get("promptContext") or self.market_data.get("settlementPrompt") or "")
        elif not mid:
            # Raw prompt that has not been matched to a Delphi market yet.
            prompt_ctx = self.market_input
            m = re.search(r"QUESTION:\s*(.+?)(?:DATA SOURCES:|SETTLEMENT RULES:|VALID OUTCOMES|$)", self.market_input, re.I | re.S)
            question = " ".join(m.group(1).split()) if m else (self.resolved_question or "Raw settlement prompt")
        else:
            # URL/UUID/prompt already resolved by oracle_ree.py, but metadata may still be loading.
            prompt_ctx = self.market_input if len(self.market_input) > 80 else ""
            if not question:
                question = "Resolving market details..."

        return question, mid, status, resolves, delphi_model, sources, outcomes, prompt_ctx

    def creator_outcome(self) -> str:
        """Return Delphi creator/market settled outcome when available."""
        if not self.market_data:
            return ""
        meta = self.market_data.get("metadata") or {}
        outcomes = meta.get("outcomes") or self.market_data.get("outcomes") or []
        idx = self.market_data.get("winningOutcomeIdx")
        if idx is None:
            idx = self.market_data.get("winningOutcomeIndex")
        try:
            if idx is not None and outcomes:
                return str(outcomes[int(idx)])
        except Exception:
            pass
        for key in ("winningOutcome", "finalOutcome", "outcome"):
            if self.market_data.get(key):
                return str(self.market_data.get(key))
        return ""

    def oracle_outcome(self) -> str:
        """Return OracleREE's extracted outcome/verdict when detectable.

        If the backend evidence result is null/None, do not return None as a
        real result. The UI should show INCONCLUSIVE and comparison should stay
        PENDING, not MISMATCH.
        """
        if self.oracle_result and str(self.oracle_result).strip().lower() not in {"none", "null", "inconclusive"}:
            return self.oracle_result

        proof = self.latest_proof_json()
        evidence = proof.get("oracle_evidence", {}) if isinstance(proof, dict) else {}
        event_verdict = evidence.get("event_verdict") if isinstance(evidence, dict) else None
        if isinstance(event_verdict, dict):
            for key in ("matchedOutcome", "matched_outcome", "verdict"):
                val = event_verdict.get(key)
                if val and str(val).strip().lower() not in {"none", "null", "unknown"}:
                    return str(val).strip()

        price_verdict = evidence.get("price_verdict") if isinstance(evidence, dict) else ""
        if price_verdict:
            m = re.search(r"outcome:\s*([^.;]+)", str(price_verdict), re.I)
            if m:
                return m.group(1).strip().strip('"\'')

        for line in reversed(self.proof_lines + self.logs):
            m = re.search(r"outcome:\s*([^.;]+)", line, re.I)
            if m:
                val = m.group(1).strip().strip('"\'')
                if val.lower() not in {"none", "null"}:
                    return val
        return ""

    def oracle_result_display(self) -> str:
        result = self.oracle_outcome()
        if result:
            return result
        # If market is not settled, show appropriate message
        if self.market_data:
            status = self.market_data.get("status", "")
            if status == "open":
                return "Market still live"
            if status not in ("settled",):
                return "Awaiting settlement"
        return "INCONCLUSIVE"

    def settlement_match_status(self) -> tuple[str, int]:
        creator = self.creator_outcome().strip().lower()
        oracle_raw = self.oracle_outcome().strip()
        oracle = oracle_raw.lower()
        # Market not settled yet
        if self.market_data and self.market_data.get("status") != "settled":
            return "WAITING FOR SETTLEMENT", curses.color_pair(5) | curses.A_BOLD
        if creator and oracle:
            if oracle in {"none", "null", "inconclusive", "unknown", "market not settled", "market still live", "awaiting settlement"}:
                return "WAITING FOR CREATOR", curses.color_pair(5) | curses.A_BOLD
            if creator == oracle or creator in oracle or oracle in creator:
                return "MATCH", curses.color_pair(3) | curses.A_BOLD
            return "MISMATCH", curses.color_pair(4) | curses.A_BOLD
        if not creator:
            return "WAITING FOR CREATOR", curses.color_pair(5) | curses.A_BOLD
        return "PENDING", curses.color_pair(2)

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
        combined = self.latest_combined_hash()
        oracle_hash = self.latest_line_value("oracle evidence hash") or self.latest_line_value("evidence hash") or self.latest_line_value("oracle hash")
        ree_hash = self.latest_ree_receipt_hash()
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
            if self.return_code == 2:
                if key in ("receipt", "done"):
                    return "failed", curses.color_pair(4) | curses.A_BOLD
                if key in self.reached:
                    return "done", curses.color_pair(3)
                return "pending", curses.color_pair(2)
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
        combined = self.latest_combined_hash()
        ree_hash = self.latest_ree_receipt_hash()
        saved = self.latest_saved_file()
        receipt_path = self.latest_ree_receipt_path()

        pct = int(self.progress * 100)
        elapsed = int(((self.finished_at or time.time()) - self.started_at)) if self.started_at else 0
        if self.return_code == 0:
            run_status = "SUCCESS"
            status_attr = curses.color_pair(3)
        elif self.return_code == 2:
            run_status = "ORACLE OK · NO RECEIPT"
            status_attr = curses.color_pair(5)
        elif self.return_code is None:
            run_status = self.phase.upper()
            status_attr = curses.color_pair(5)
        else:
            run_status = "FAILED"
            status_attr = curses.color_pair(4)

        # Mockup-like dimensions: compact logs, bigger proof/settlement clarity.
        # Left column: keep market details short, use summary mainly for the settlement prompt,
        # and put verdict comparison inside Oracle Evidence.
        market_h = 9
        summary_h = 7
        evidence_h = max(12, usable_h - market_h - summary_h - 2)
        if usable_h < 38:
            market_h = 8
            summary_h = 6
            evidence_h = max(10, usable_h - market_h - summary_h - 2)

        exec_h = 5
        pipeline_h = 7
        proof_h = 14
        verify_h = 4
        logs_h = max(6, usable_h - exec_h - pipeline_h - proof_h - verify_h - 4)

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
        # Final outcome and verification mode are shown in Oracle Evidence + Result.

        # ── LEFT: SETTLEMENT SUMMARY ─────────────────────────
        lx, y, bw, bh = box(1, top + market_h + 1, left_w, summary_h, "🏆 SETTLEMENT SUMMARY")
        yy = y + 2
        yy = put_kv(lx + 2, yy, bw - 4, "Question", question or "—", curses.color_pair(1))
        if delphi_model and yy < y + bh - 1:
            yy = put_kv(lx + 2, yy, bw - 4, "Judge", delphi_model, curses.color_pair(3))
        if sources and yy < y + bh - 1:
            yy = put_kv(lx + 2, yy, bw - 4, "Sources", sources, curses.color_pair(3))
        elif prompt_sources and yy < y + bh - 1:
            yy = put_kv(lx + 2, yy, bw - 4, "Sources", " · ".join(prompt_sources), curses.color_pair(3))
        if outcomes and yy < y + bh - 1:
            yy = put_kv(lx + 2, yy, bw - 4, "Outcomes", outcomes, curses.color_pair(3))
        elif prompt_outcomes and yy < y + bh - 1:
            yy = put_kv(lx + 2, yy, bw - 4, "Outcomes", " · ".join(prompt_outcomes), curses.color_pair(3))

        # ── LEFT: ORACLE EVIDENCE + SETTLEMENT COMPARISON ────────────────
        lx, y, bw, bh = box(1, top + market_h + summary_h + 2, left_w, evidence_h, "🔒 ORACLE EVIDENCE + RESULT")
        yy = y + 2
        creator_res = self.creator_outcome()
        oracle_res = self.oracle_result_display()
        match_txt, match_attr = self.settlement_match_status()
        yy = put_kv(lx + 2, yy, bw - 4, "Oracle Hash", oracle_hash or "—", curses.color_pair(3) if oracle_hash else curses.color_pair(2))
        yy = put_kv(lx + 2, yy, bw - 4, "IPFS CID", ipfs or "—", curses.color_pair(3) if ipfs else curses.color_pair(2))
        if receipt_path and yy < y + bh - 1:
            import re as _re2
            _m = _re2.search(r"/[^\s]+receipt_[0-9_]+\.json", receipt_path)
            clean_r = _m.group(0) if _m else receipt_path
            yy = put_kv(lx + 2, yy, bw - 4, "Receipt", clean_r, curses.color_pair(3))
        elif yy < y + bh - 1:
            waiting = "Waiting for REE..." if self.mode == "running" else "NOT FOUND"
            attr = curses.color_pair(6) if self.mode == "running" else curses.color_pair(5)
            yy = put_kv(lx + 2, yy, bw - 4, "Receipt", waiting, attr)
        if ree_hash and yy < y + bh - 1:
            yy = put_kv(lx + 2, yy, bw - 4, "REE Hash", ree_hash, curses.color_pair(3))
        if yy < y + bh - 1:
            yy += 1
        _w = "Verifying..." if self.mode == "running" else "—"
        yy = put_kv(lx + 2, yy, bw - 4, "Prompt Mode", self.verification_mode or _w, curses.color_pair(3) if self.verification_mode else curses.color_pair(6) if self.mode == "running" else curses.color_pair(2))
        yy = put_kv(lx + 2, yy, bw - 4, "Prompt Match", self.prompt_match or _w, curses.color_pair(3) if str(self.prompt_match).upper() == "YES" else curses.color_pair(6) if self.mode == "running" else curses.color_pair(2))
        yy = put_kv(lx + 2, yy, bw - 4, "Question Match", self.question_match or _w, curses.color_pair(3) if str(self.question_match).upper() == "YES" else curses.color_pair(6) if self.mode == "running" else curses.color_pair(2))
        if yy < y + bh - 1:
            yy += 1
        yy = put_kv(lx + 2, yy, bw - 4, "Creator Result", creator_res or "—", curses.color_pair(3) if creator_res else curses.color_pair(2))
        oracle_display = "Fetching evidence..." if self.mode == "running" and oracle_res == "INCONCLUSIVE" else oracle_res
        oracle_attr = curses.color_pair(6) if self.mode == "running" and oracle_res == "INCONCLUSIVE" else curses.color_pair(3) if oracle_res != "INCONCLUSIVE" else curses.color_pair(5)
        yy = put_kv(lx + 2, yy, bw - 4, "OracleREE", oracle_display, oracle_attr)
        comp_display = "Waiting for result..." if self.mode == "running" and match_txt == "PENDING" else match_txt
        comp_attr = curses.color_pair(6) if self.mode == "running" and match_txt == "PENDING" else match_attr
        yy = put_kv(lx + 2, yy, bw - 4, "Comparison", comp_display, comp_attr)

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
        waiting = "Waiting..." if self.mode == "running" else "—"
        artifacts = [
            ("Oracle Hash", oracle_hash or waiting),
            ("IPFS CID", ipfs or waiting),
            ("REE Receipt Hash", ree_hash or waiting),
            ("Combined Hash", combined or waiting),
            ("Receipt Path", receipt_path or waiting),
            ("Saved File", saved or waiting),
        ]
        for name, val in artifacts:
            if yy >= y + bh - 1:
                break
            ok = val != "—"
            value_attr = curses.color_pair(3) if ok else curses.color_pair(2)
            if name == "Settlement Match" and val == "MISMATCH":
                value_attr = curses.color_pair(4) | curses.A_BOLD
            elif name == "Settlement Match" and val == "MATCH":
                value_attr = curses.color_pair(3) | curses.A_BOLD
            self.put(stdscr, yy, rx + 2, "☑" if ok else "·", value_attr if ok else curses.color_pair(2))
            self.put(stdscr, yy, rx + 5, name[:21], curses.color_pair(1))
            self.put(stdscr, yy, rx + 28, compact_value(val, bw - 31), value_attr)
            yy += 1

        # ── RIGHT: VERIFY ────────────────────────────────────────────────
        rx, y, bw, bh = box(right_x, top + exec_h + pipeline_h + proof_h + 3, right_w, verify_h, "⋆ VERIFY")
        yy = y + 2
        if self.return_code == 0:
            if not receipt_path or not ree_hash:
                self.put(stdscr, yy, rx + 2, "⚠ Oracle proof saved, but REE receipt was not found", curses.color_pair(5) | curses.A_BOLD); yy += 1
                self.put(stdscr, yy, rx + 2, "Check oracle_ree.py receipt generation before claiming full REE proof", curses.color_pair(2))
            elif self.prompt_warning:
                self.put(stdscr, yy, rx + 2, "⚠ Non-canonical prompt: custom simulation only", curses.color_pair(5) | curses.A_BOLD); yy += 1
                target = receipt_path
                self.put(stdscr, yy, rx + 2, compact_value(f"python3 ree.py verify --receipt-path {target}", bw - 4), curses.color_pair(2))
            else:
                self.put(stdscr, yy, rx + 2, "✓ Oracle data + REE execution cryptographically linked", curses.color_pair(3)); yy += 1
                # Extract just the path from any command string
                import re as _re
                m = _re.search(r"(/[^\s]+receipt_[0-9_]+\.json)", receipt_path)
                clean_path = m.group(1) if m else receipt_path
                self.put(stdscr, yy, rx + 2, compact_value(clean_path, bw - 4), curses.color_pair(2))
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
            # Exit 2: oracle steps succeed, receipt/done fail
            if self.return_code == 2:
                if key in ("receipt", "done"):
                    return "FAILED", curses.color_pair(4) | curses.A_BOLD
                if key in self.reached:
                    return "SUCCESS", curses.color_pair(3)
                return "PENDING", curses.color_pair(2)
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
            prefix = "        "
            left = f"[{prefix}] {label}"
            dots = "." * max(2, bw - len(left) - len(status_txt) - 8)
            self.put(stdscr, yy, rx + 2, left[:bw - 18], curses.color_pair(3) if status_txt == "SUCCESS" else curses.color_pair(2))
            self.put(stdscr, yy, rx + 2 + min(len(left), bw - 18), f" {dots} ", curses.color_pair(2))
            self.put(stdscr, yy, rx + bw - len(status_txt) - 3, status_txt, sattr)
            yy += 1

        # Show raw backend errors/resolution lines so failures are actionable.
        important_logs = [
            l for l in self.logs[-12:]
            if any(k in l.lower() for k in [
                "error", "uuid detected", "found question", "matched market",
                "error", "uuid detected", "found question", "matched market",
                "market id:", "fetching market from delphi", "could not", "404",
                "receipt saved", "receipt:"
            ]) and not l.lower().startswith("[tui]") and not l.startswith("$")
        ]
        if important_logs and yy < y + bh - 1:
            yy += 1
            for raw in important_logs[-3:]:
                if yy >= y + bh - 1:
                    break
                attr = curses.color_pair(4) if "error" in raw.lower() or "could not" in raw.lower() or "404" in raw.lower() else curses.color_pair(2)
                self.put(stdscr, yy, rx + 2, compact_value(raw, bw - 4), attr)
                yy += 1

        if self.prompt_warning and yy < y + bh - 1:
            yy += 1
            self.put(stdscr, yy, rx + 2, "Prompt integrity warning:", curses.color_pair(5) | curses.A_BOLD); yy += 1
            if yy < y + bh - 1:
                self.put(stdscr, yy, rx + 2, compact_value(self.prompt_warning, bw - 4), curses.color_pair(5))

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
                if key == "c" or key == ord("c"):
                    if hasattr(self, "_proc") and self._proc:
                        try:
                            self._proc.terminate()
                        except Exception:
                            pass
                    self.status = "Cancelled"
                    self.mode = "done"
                    self.return_code = 1
                    self.finished_at = time.time()
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
