#!/usr/bin/env python3
"""
OracleREE TUI — Trustless oracle grounding for Gensyn Delphi settlement.
Run: python3 ree.py
Version: 4-option menu with Settle Market mode
Patch: USER_REQUESTED_STALE_GUARD_FIX_20260524
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
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def extract_question_from_prompt_text(text: str) -> str:
    text = text or ""
    m = re.search(
        r"QUESTION\s*:\s*(.+?)(?:\n\s*\n|DATA SOURCES\s*:|SETTLEMENT RULES\s*:|VALID OUTCOMES|$)",
        text,
        re.I | re.S,
    )
    if m:
        return " ".join(m.group(1).split())

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
    aw = set(re.findall(r"[a-z0-9]+", (a or "").lower()))
    bw = set(re.findall(r"[a-z0-9]+", (b or "").lower()))
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / max(1, len(aw | bw))


def prompt_belongs_to_market(prompt: str, official_question: str, outcomes: list) -> tuple[bool, str]:
    prompt = prompt or ""
    official_question = official_question or ""
    pasted_question = extract_question_from_prompt_text(prompt)

    if not official_question:
        return True, "no official question available"

    if pasted_question:
        sim = text_similarity(official_question, pasted_question)
        has_explicit_question = bool(re.search(r"QUESTION\s*:", prompt, re.I))
        threshold = 0.72 if has_explicit_question else 0.18
        if sim >= threshold:
            return True, f"question similarity {sim:.2f}"

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


def first_nonempty_str(*vals) -> str:
    """Return the first non-empty string-like value."""
    for val in vals:
        if val is None:
            continue
        s = str(val).strip()
        if s and s.lower() not in {"none", "null", "unknown"}:
            return s
    return ""


def canonical_outcome_from_proof(proof: dict) -> str:
    """
    Single TUI-side source of truth for OracleREE result.

    IMPORTANT:
    The live TUI must not trust early stdout lines like "event verdict" or
    "price verdict" after the backend has saved a proof. Those lines can be
    stale candidate values. The saved proof's resolved_outcome/final_outcome
    is canonical.
    """
    if not isinstance(proof, dict):
        return ""

    oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else {}
    verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
    dashboard = proof.get("dashboard") if isinstance(proof.get("dashboard"), dict) else {}

    def nested(obj, *path):
        cur = obj
        for key in path:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(key)
        return cur

    candidates = [
        nested(proof, "resolved_outcome", "outcome"),
        nested(oe, "resolved_outcome", "outcome"),
        proof.get("final_outcome"),
        proof.get("oracle_result"),
        proof.get("oracle_outcome"),
        proof.get("matched_outcome"),
        oe.get("final_outcome"),
        oe.get("oracle_result"),
        oe.get("oracle_outcome"),
        oe.get("matched_outcome"),
        dashboard.get("oracle_result"),
        dashboard.get("final_outcome"),
        verification.get("oracle_result"),
        verification.get("final_outcome"),
        nested(oe, "final_verdict", "matched_outcome"),
        nested(oe, "event_verdict", "verdict"),
        nested(oe, "event_verdict", "matchedOutcome"),
    ]

    # Last resort: source_results, but only after all canonical fields.
    source_results = oe.get("source_results")
    if isinstance(source_results, list):
        for sr in source_results:
            if not isinstance(sr, dict):
                continue
            candidates.extend([
                nested(sr, "resolved_outcome", "outcome"),
                sr.get("matched_outcome"),
                nested(sr, "derived_result", "matched_outcome"),
            ])
            facts = sr.get("facts")
            if isinstance(facts, list):
                for fact in facts:
                    if isinstance(fact, dict) and str(fact.get("label", "")).lower() == "matched_outcome":
                        candidates.append(fact.get("value"))

    val = first_nonempty_str(*candidates)
    if val.lower() == "inconclusive":
        return "INCONCLUSIVE"
    if val.lower() in {"market not settled", "market still live", "awaiting settlement"}:
        return ""

    # If proof explicitly says there were no valid oracle candidates, return INCONCLUSIVE.
    arb_status = str(nested(oe, "arbitration", "status") or nested(proof, "arbitration", "status") or "").lower()
    fv_pipeline = str(nested(oe, "final_verdict", "pipeline") or nested(proof, "final_verdict", "pipeline") or "").lower()
    if "no_valid" in arb_status or "inconclusive" in fv_pipeline:
        return "INCONCLUSIVE"

    return val


def market_0x_id_from_market(market: dict) -> str:
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
    def __init__(self) -> None:
        self.market_input = ""
        self.settlement_prompt_input = ""
        self.market_ref_input = ""
        self.preflight_market_id = ""
        self.active_input_field = "prompt"
        self.screen = "home"
        self.mode = "idle"
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

        # ── NEW: settle mode flag ──────────────────────────────────────
        # True  → user chose "Settle Market" (no creator result, OracleREE IS the settlement)
        # False → user chose "Run OracleREE Proof" (verify against existing creator result)
        self.settle_mode: bool = False

        self.home_selected = 0
        self.receipt_action = "verify"
        self.receipt_path_input = ""
        self.receipt_extra_args = ""
        self.receipt_status = "Ready"
        self.receipt_logs: list[str] = []
        self.receipt_output: list[str] = []
        self.current_action = "home"

    def oracle_run_active(self) -> bool:
        """True only while an OracleREE proof/settlement run is actively executing.

        This is intentionally stricter than just self.mode == "running" because
        the receipt screen also uses running mode. The results dashboard must not
        display stale oracle proof artifacts while a fresh oracle run is active.
        """
        return (
            self.current_action == "oracle"
            and (
                self.mode == "running"
                or (self.return_code is None and self.started_at is not None and self.finished_at is None)
            )
        )

    # ── market fetch ───────────────────────────────────────────────────────

    def trigger_fetch_for_id(self, mid: str) -> None:
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
        mid = (extract_0x_market_id(self.market_ref_input) or
               extract_0x_market_id(self.market_input) or
               extract_0x_market_id(self.resolved_market_id))
        if mid:
            self.trigger_fetch_for_id(mid)

    # ── run ───────────────────────────────────────────────────────────────

    def backend_market_argument(self) -> str:
        prompt = self.settlement_prompt_input.strip()
        market_ref = self.market_ref_input.strip()

        if market_ref:
            canonical_ref = (self.preflight_market_id or
                             extract_0x_market_id(market_ref) or
                             market_ref)
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

        raw = self.market_input.strip()
        if extract_0x_market_id(raw):
            return extract_0x_market_id(raw) or raw
        uuid = extract_uuid(raw)
        if uuid and raw.lower() == uuid.lower():
            return f"https://app.delphi.fyi/market/{uuid}"
        return raw

    def fetch_market_by_ref_for_preflight(self, market_ref: str) -> tuple[Optional[dict], str]:
        market_ref = (market_ref or "").strip()
        if not market_ref:
            return None, "Market URL or 0x Market ID is required"

        mid = extract_0x_market_id(market_ref)
        if mid:
            data = fetch_market_info(mid)
            if data:
                return data, ""
            return None, f"Could not fetch Delphi market for ID: {mid}"

        uuid = extract_uuid(market_ref)
        if not uuid:
            return None, "Market reference must contain a Delphi URL, UUID, or 0x market ID"

        api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
        if not api_key:
            return None, "DELPHI_API_ACCESS_KEY is missing"

        page_question = ""
        url = market_ref if market_ref.startswith("http") else f"https://app.delphi.fyi/market/{uuid}"
        html = ""
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
                    app_market_id = str(m.get("appMarketId", ""))
                    market_id_field = str(m.get("id", ""))
                    uuid_clean = uuid.replace("-", "").lower()
                    if (uuid_clean in metadata_uri.replace("-", "").lower()
                            or uuid.lower() in app_market_id.lower()
                            or uuid_clean in app_market_id.replace("-","").lower()
                            or uuid.lower() in market_id_field.lower()):
                        return m, ""
        except Exception as exc:
            return None, f"Market lookup failed: {exc}"

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
            if self.settle_mode:
                # Pending markets not in API — pass directly to oracle_ree.py
                self.preflight_market_id = ""
                self.resolved_market_id = market_ref
                self.market_data = {}
                # Extract prompt verification will happen in oracle_ree.py
                return True, ""
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

        belongs, reason = prompt_belongs_to_market(prompt, official_question, outcomes_for_match)
        if not belongs:
            self.active_input_field = "prompt"
            return False, "INPUT ERROR: Settlement prompt does not match the Delphi market URL/ID"

        # Immutability guarantee: prompt must match official — applies in BOTH modes.
        # In settle mode this is the core trust property: creator cannot change sources.
        if official_prompt:
            if normalize_prompt_text(prompt) != normalize_prompt_text(official_prompt):
                self.active_input_field = "prompt"
                return False, "INPUT ERROR: Settlement prompt does not match the official Delphi prompt for this market. Cannot run."

        resolved_0x = market_0x_id_from_market(market)
        if not resolved_0x:
            self.active_input_field = "market"
            return False, "INPUT ERROR: Delphi URL resolved, but no valid 0x market ID was found. Paste the 0x market ID."

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

        # ── Settle mode: block if already settled ─────────────────────────
        if self.settle_mode:
            meta_for_settle = market.get("metadata") or {}
            resolved_outcome = (
                market.get("resolvedOutcome") or
                market.get("winningOutcome") or
                market.get("finalOutcome") or
                meta_for_settle.get("resolvedOutcome") or
                meta_for_settle.get("winningOutcome") or
                meta_for_settle.get("finalOutcome")
            )
            winning_idx = (
                market.get("winningOutcomeIdx")
                if market.get("winningOutcomeIdx") is not None
                else meta_for_settle.get("winningOutcomeIdx")
            )
            if resolved_outcome or winning_idx is not None:
                self.active_input_field = "market"
                return False, "Market is already settled — use 'Run OracleREE Proof' to verify the creator result instead."

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

        if self.settle_mode:
            return True, "Settle mode verified. Running OracleREE settlement..."
        return True, "Input verified. Running canonical proof..."

    def start_run(self) -> None:
        if self.mode == "running":
            return

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

        self.screen = "results"
        self.current_action = "oracle"
        # HARD RESET: remove all stale artifacts/results from previous run.
        # Nothing from an old proof should render during a fresh execution.
        try:
            while True:
                self.events.get_nowait()
        except queue.Empty:
            pass
        self.logs = []
        self.proof_lines = []
        self.ree_receipt_path = ""
        self.oracle_result = ""
        self.resolved_ree_model = ""
        self.resolved_classification = ""
        self.prompt_source = ""
        self.prompt_match = ""
        self.question_match = ""
        self.verification_mode = ""
        self.prompt_warning = ""
        self.official_prompt_hash = ""
        self.provided_prompt_hash = ""
        self.receipt_path_input = ""
        self.receipt_extra_args = ""
        self.receipt_status = "Ready"
        self.receipt_logs = []
        self.receipt_output = []
        self.progress = 0.03
        self.reached = set()
        self.phase = "fetch"
        self.return_code = None
        self.mode = "running"
        self.status = "Running..."
        self.started_at = time.time()
        self.finished_at = None
        self.ree_receipt_path = ""
        self.resolved_market_id = self.preflight_market_id or self.resolved_market_id
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
        self.receipt_path_input = ""
        self.receipt_extra_args = ""
        self.receipt_status = "Ready"
        self.receipt_logs = []
        self.receipt_output = []

        self.trigger_fetch()

        backend_arg = self.backend_market_argument()
        cmd = ["python3", str(script), "--market", backend_arg]

        # ── Pass --settle flag to oracle_ree.py in settle mode ─────────
        if self.settle_mode:
            cmd.append("--settle")

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
        self.current_action = "oracle"
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
        self.settle_mode = False  # always reset to verify mode
        self.left_scroll = 0
        self.log_scroll = 0

    def add_log(self, line: str) -> None:
        self.logs.append(line)
        if len(self.logs) > 2000:
            self.logs = self.logs[-2000:]

    def set_phase(self, key: str) -> None:
        self.phase = key
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
            # Do not display interim verdicts during a running proof.
            # Final UI result is loaded only after successful proof JSON completion.
            pass

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
            self.set_phase("ree")
            self.progress = max(self.progress, 0.92)
        elif "combined hash" in low or "saved to" in low:
            self.set_phase("done")

        if "receipt" in low and "/" in line and ("metadata" in low or "receipt_" in low):
            val = line.split(":", 1)[1].strip() if ":" in line else line.strip()
            if val and not val.startswith("sha256:"):
                self.ree_receipt_path = val

        keys = [
            "oracle hash", "evidence hash", "oracle evidence hash", "ipfs cid",
            "ree receipt", "ree receipt hash", "combined hash", "saved to", "saved file",
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
            if self.current_action == "receipt" or self.screen == "receipt":
                line = str(payload)
                self.receipt_logs.append(line)
                if len(self.receipt_logs) > 1000:
                    self.receipt_logs = self.receipt_logs[-1000:]
                low = line.lower()
                if ("verification passed" in low or "verify: passed" in low or
                        "receipt validation: all checks passed" in low or "all checks passed" in low):
                    self.receipt_status = "PASSED"
                    if self.receipt_action == "verify":
                        self.receipt_output.append("Verify: PASSED")
                    else:
                        self.receipt_output.append("Validation: ALL CHECKS PASSED")
                elif "verification failed" in low or "validate failed" in low or "error" in low:
                    self.receipt_status = "FAILED"
                elif "receipt:" in low and "/" in line:
                    self.receipt_output.append(line.strip())
                elif "model output" in low or "printing model outputs" in low:
                    self.receipt_output.append(line.strip())
                return
            self.parse_line(payload)
            self.add_log(payload)
        elif kind == "market":
            self.fetching_market = False
            self.market_data = payload
        elif kind == "done":
            if self.current_action == "receipt" or self.screen == "receipt":
                self.return_code = payload
                self.mode = "done"
                self.finished_at = time.time()
                if payload == 0:
                    if self.receipt_status not in ("PASSED", "FAILED"):
                        self.receipt_status = "Success"
                    self.status = self.receipt_status
                else:
                    self.receipt_status = f"Failed (exit {payload})"
                    self.status = self.receipt_status
                return
            self.return_code = payload
            self.mode = "done"
            self.finished_at = time.time()
            if payload == 0:
                self.status = "Success"
                self.reached = {k for k, _, _ in PHASES}
                self.set_phase("done")
                self.progress = 1.0
                # After oracle_ree.py exits, the proof file exists. Force the live
                # dashboard to refresh from the saved proof canonical result.
                canonical = self.oracle_outcome()
                if canonical:
                    self.oracle_result = canonical
            elif payload == 2:
                self.status = "Oracle OK — REE receipt missing"
                for k, _, _ in PHASES:
                    if k != "done":
                        self.reached.add(k)
                self.phase = "receipt"
                self.progress = 0.95
            else:
                self.status = f"Failed (exit {payload})"

    def tick_progress(self) -> None:
        if self.mode != "running" or self.started_at is None:
            return
        elapsed = max(0.0, time.time() - self.started_at)
        soft = min(0.92, 0.05 + elapsed / 90.0)
        if soft > self.progress:
            self.progress = soft
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
            self.phase = "ree"

    def collect_paste_buffer(self, stdscr: curses.window, first_key):
        if isinstance(first_key, int):
            return first_key
        time.sleep(0.015)
        pieces = [first_key]
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
                    try:
                        curses.ungetch(nxt)
                    except curses.error:
                        pass
                    break
        finally:
            stdscr.timeout(80)
        data = "".join(pieces)
        data = data.replace("\x1b[200~", "").replace("\x1b[201~", "")
        return data

    def handle_input_page_key(self, key) -> None:
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
                self.status = "Both fields are required"
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
            curses.init_pair(1, curses.COLOR_WHITE,   -1)
            curses.init_pair(2, 8,                    -1)
            curses.init_pair(3, curses.COLOR_GREEN,   -1)
            curses.init_pair(4, curses.COLOR_RED,     -1)
            curses.init_pair(5, curses.COLOR_YELLOW,  -1)
            curses.init_pair(6, curses.COLOR_CYAN,    -1)
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

    # ── home / utility screens ─────────────────────────────────────────────

    def go_home(self) -> None:
        if self.mode == "running":
            return
        self.screen = "home"
        self.current_action = "home"
        self.status = "Ready"
        self.receipt_status = "Ready"

    def open_oracle_input(self) -> None:
        self.reset()
        self.screen = "input"
        self.current_action = "oracle"
        self.settle_mode = False
        self.status = "Ready"

    def open_settle_screen(self) -> None:
        """Open input screen in settle mode.

        Settle mode: market is closed but has no creator result yet.
        OracleREE fetches evidence and becomes the canonical settlement.
        The settlement prompt is still locked to the original creator sources —
        creator cannot change data sources after market creation.
        """
        self.reset()
        self.screen = "input"
        self.current_action = "oracle"
        self.settle_mode = True
        self.status = "Settle mode: paste settlement prompt + market URL/ID"

    def open_receipt_screen(self, action: str) -> None:
        if self.mode == "running":
            return
        self.screen = "receipt"
        self.current_action = "receipt"
        self.receipt_action = action
        self.receipt_status = "Ready"
        self.status = "Ready"
        self.mode = "idle"
        self.return_code = None
        self.receipt_path_input = ""
        self.receipt_extra_args = ""
        self.receipt_logs = []
        self.receipt_output = []
        self.started_at = None
        self.finished_at = None

    def handle_home_key(self, key) -> None:
        # ── UPDATED: 4 items ───────────────────────────────────────────
        items_count = 4
        if key in (curses.KEY_UP, "k"):
            self.home_selected = (self.home_selected - 1) % items_count
            return
        if key in (curses.KEY_DOWN, "j"):
            self.home_selected = (self.home_selected + 1) % items_count
            return
        if key in ("1", ord("1")):
            self.open_oracle_input(); return
        if key in ("2", ord("2")):
            self.open_settle_screen(); return          # NEW
        if key in ("3", ord("3")):
            self.open_receipt_screen("verify"); return
        if key in ("4", ord("4")):
            self.open_receipt_screen("validate"); return
        if key in ("\n", "\r", curses.KEY_ENTER, 10, 13):
            if self.home_selected == 0:
                self.open_oracle_input()
            elif self.home_selected == 1:
                self.open_settle_screen()             # NEW
            elif self.home_selected == 2:
                self.open_receipt_screen("verify")
            elif self.home_selected == 3:
                self.open_receipt_screen("validate")
            return

    def start_receipt_action(self) -> None:
        if self.mode == "running":
            return
        receipt_path = self.receipt_path_input.strip().strip('"').strip("'")
        if not receipt_path:
            self.receipt_status = "Paste receipt path first"
            self.status = self.receipt_status
            return
        ree_sh = Path(__file__).parent / "ree.sh"
        if not ree_sh.exists():
            self.receipt_status = "ree.sh not found"
            self.status = self.receipt_status
            return
        action = "verify" if self.receipt_action == "verify" else "validate"
        cmd = ["bash", str(ree_sh), action, "--receipt-path", receipt_path]
        if self.receipt_extra_args.strip():
            cmd.extend(shlex.split(self.receipt_extra_args.strip()))
        self.mode = "running"
        self.current_action = "receipt"
        self.status = "Running..."
        self.receipt_status = f"Running {action}..."
        self.receipt_logs = []
        self.receipt_output = []
        self.return_code = None
        self.started_at = time.time()
        self.finished_at = None

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
                    cwd=str(Path(__file__).parent),
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

    def handle_receipt_key(self, key) -> None:
        if key in ("r", ord("r")):
            self.start_receipt_action()
            return
        if key in ("\n", "\r", curses.KEY_ENTER, 10, 13):
            self.receipt_status = "Ready. Press r to run."
            return
        if key in ("h", ord("h"), "e", ord("e")):
            self.go_home()
            return
        if key in (curses.KEY_BACKSPACE, 127, 8, "\x7f", "\x08"):
            self.receipt_path_input = self.receipt_path_input[:-1]
            return
        if key in (curses.KEY_DC,):
            self.receipt_path_input = ""
            return
        if isinstance(key, int):
            return
        if isinstance(key, str):
            cleaned = key.replace("\x1b[200~", "").replace("\x1b[201~", "")
            cleaned = " ".join(cleaned.replace("\r", "\n").splitlines()) if ("\n" in cleaned or "\r" in cleaned) else cleaned
            cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch in " \t")
            if cleaned:
                self.receipt_path_input += cleaned
                self.receipt_status = "Ready to run"

    def draw_home_page(self, stdscr: curses.window, h: int, w: int) -> None:
        box_w = min(122, max(88, w - 12))
        box_h = min(32, max(28, h - 6))
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

        ix = x + 6
        menu_y = y + 7
        self.put(stdscr, menu_y, ix, "MAIN MENU", curses.color_pair(6) | curses.A_BOLD)
        self.put(stdscr, menu_y + 1, ix, "Choose an action:", curses.color_pair(2))

        # ── UPDATED: 4 menu items ─────────────────────────────────────
        items = [
            ("[1]", "Run OracleREE Proof",
             "Generate oracle evidence, run REE, and create a Delphi settlement proof"),
            ("[2]", "Settle Market",
             "Settle a closed market using locked creator sources + REE proof"),
            ("[3]", "Verify Receipt",
             "Re-run REE verification against an existing receipt"),
            ("[4]", "Validate Receipt",
             "Validate receipt structure and hashes without full settlement flow"),
        ]

        row = menu_y + 3
        for idx, (key_label, name, desc) in enumerate(items):
            selected = idx == self.home_selected
            attr = curses.color_pair(6) | curses.A_BOLD if selected else curses.color_pair(1)
            desc_attr = curses.color_pair(2)
            prefix = ">" if selected else " "
            if selected:
                self.put(stdscr, row, ix, "╭" + "─" * (box_w - 14) + "╮", curses.color_pair(6))
                self.put(stdscr, row + 1, ix, "│", curses.color_pair(6))
                self.put(stdscr, row + 1, ix + box_w - 13, "│", curses.color_pair(6))
                self.put(stdscr, row + 2, ix, "╰" + "─" * (box_w - 14) + "╯", curses.color_pair(6))
                line_y = row + 1
            else:
                line_y = row + 1
            self.put(stdscr, line_y, ix + 2, f"{prefix} {key_label}  {name}", attr)
            self.put(stdscr, line_y, ix + 38, desc[:max(10, box_w - 56)], desc_attr)
            row += 3 if selected else 2

        controls = "↑/↓ navigate   ENTER select   1/2/3/4 quick select   q quit"
        self.put(stdscr, y + box_h - 2, ix, controls[:box_w - 12], curses.color_pair(2))
        if self.status and self.status != "Ready":
            self.put(stdscr, y + box_h - 3, ix, compact_value(f"Status: {self.status}", box_w - 12), curses.color_pair(5))

    def draw_receipt_page(self, stdscr: curses.window, h: int, w: int) -> None:
        action = "verify" if self.receipt_action == "verify" else "validate"
        is_verify = action == "verify"
        title = "VERIFY RECEIPT" if is_verify else "VALIDATE RECEIPT"
        desc = "Re-run REE verification on an existing receipt." if is_verify else "Validate receipt structure and hashes."

        def safe_load_receipt(path: str) -> dict:
            try:
                if path and Path(path).exists():
                    return json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception:
                return {}
            return {}

        def nested_get(obj: dict, path: list[str], default: str = "") -> str:
            cur = obj
            try:
                for key in path:
                    if not isinstance(cur, dict):
                        return default
                    cur = cur.get(key)
                if cur is None:
                    return default
                return str(cur)
            except Exception:
                return default

        def first_nonempty(*vals) -> str:
            for val in vals:
                if val is not None and str(val).strip():
                    return str(val).strip()
            return ""

        def find_hash_by_key(obj, names: list[str]) -> str:
            wanted = {n.lower() for n in names}
            seen_ids = set()
            def walk(value):
                if id(value) in seen_ids:
                    return ""
                seen_ids.add(id(value))
                if isinstance(value, dict):
                    for k, v in value.items():
                        kl = str(k).lower()
                        if kl in wanted and v is not None and str(v).strip():
                            return str(v).strip()
                        normalized = re.sub(r"[^a-z0-9]", "", kl)
                        for name in wanted:
                            if normalized == re.sub(r"[^a-z0-9]", "", name):
                                if v is not None and str(v).strip():
                                    return str(v).strip()
                    for v in value.values():
                        found = walk(v)
                        if found:
                            return found
                elif isinstance(value, list):
                    for v in value:
                        found = walk(v)
                        if found:
                            return found
                return ""
            return walk(obj)

        def final_model_output(text: str) -> str:
            text = str(text or "").strip()
            if not text:
                return "—"
            if "</think>" in text:
                text = text.split("</think>")[-1].strip()
            text = text.replace("<|im_end|>", "").replace("<|endoftext|>", "").strip()
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            return lines[-1] if lines else compact_value(text, 120)

        def latest_generated_receipt() -> str:
            found = ""
            for line in self.receipt_logs:
                if "receipt:" in line.lower() and "/" in line and "receipt_" in line:
                    m = re.search(r'(/[^\s]+receipt_[0-9_]+\.json)', line)
                    if m:
                        found = m.group(1)
            return found

        def log_has(*needles: str) -> bool:
            hay = "\n".join(self.receipt_logs).lower()
            return any(n.lower() in hay for n in needles)

        def clean_log_lines() -> list[str]:
            useful = []
            skip_fragments = [
                "docker run --rm --gpus", "running command:", "digest:",
                "status: image is up to date", "docker.io/gensynai/ree",
                "warning: could not apply recursive acls",
            ]
            for line in self.receipt_logs:
                low = line.lower().strip()
                if not low:
                    continue
                if any(x in low for x in skip_fragments):
                    continue
                useful.append(line.strip())
            return useful[-8:]

        def draw_box(x: int, y: int, bw: int, bh: int, title_text: str = "") -> None:
            if bw < 8 or bh < 3:
                return
            self.put(stdscr, y, x, "┌" + "─" * (bw - 2) + "┐", curses.color_pair(2))
            for yy in range(1, bh - 1):
                self.put(stdscr, y + yy, x, "│", curses.color_pair(2))
                self.put(stdscr, y + yy, x + bw - 1, "│", curses.color_pair(2))
            self.put(stdscr, y + bh - 1, x, "└" + "─" * (bw - 2) + "┘", curses.color_pair(2))
            if title_text:
                self.put(stdscr, y, x + 2, f" {title_text} ", curses.color_pair(6) | curses.A_BOLD)

        def kv(ypos: int, xpos: int, label: str, value: str, width: int, attr=0, label_w: int = 15) -> None:
            self.put(stdscr, ypos, xpos, label, curses.color_pair(2))
            self.put(stdscr, ypos, xpos + label_w, compact_value(value or "—", max(8, width - label_w - 1)), attr or curses.color_pair(1))

        def center_text(ypos: int, text: str, attr=0) -> None:
            self.put(stdscr, ypos, max(0, (w - len(text)) // 2), text, attr)

        receipt_path = self.receipt_path_input.strip().strip('"').strip("'")
        receipt_data = safe_load_receipt(receipt_path)
        hashes = receipt_data.get("hashes", {}) if isinstance(receipt_data.get("hashes"), dict) else {}
        input_obj = receipt_data.get("input", {}) if isinstance(receipt_data.get("input"), dict) else {}
        params = receipt_data.get("parameters", {}) if isinstance(receipt_data.get("parameters"), dict) else {}
        output_obj = receipt_data.get("output", {}) if isinstance(receipt_data.get("output"), dict) else {}
        execution = receipt_data.get("execution", {}) if isinstance(receipt_data.get("execution"), dict) else {}

        receipt_hash = first_nonempty(
            find_hash_by_key(receipt_data, ["receipt_hash", "receiptHash"]),
            hashes.get("receipt_hash"),
            receipt_data.get("receipt_hash"),
        )
        prompt_hash_val = first_nonempty(
            find_hash_by_key(receipt_data, ["prompt_hash", "promptHash"]),
            input_obj.get("prompt_hash"),
            hashes.get("prompt_hash"),
        )
        parameters_hash = first_nonempty(
            find_hash_by_key(receipt_data, [
                "parameters_hash", "parameter_hash", "params_hash",
                "parametersHash", "parameterHash", "paramsHash",
                "config_hash", "configHash",
            ]),
            hashes.get("parameters_hash"),
        )
        tools_hash = first_nonempty(
            find_hash_by_key(receipt_data, [
                "tools_hash", "tool_hash", "toolsHash", "toolHash",
                "tool_calls_hash", "toolCallsHash",
            ]),
            hashes.get("tools_hash"),
        )
        model_name = first_nonempty(
            nested_get(receipt_data, ["model", "name"]),
            nested_get(receipt_data, ["model", "model_name"]),
            input_obj.get("model"),
            params.get("model_name"),
            params.get("model"),
        )
        device_name = first_nonempty(execution.get("device_name"), execution.get("device_type"))
        original_model_output = final_model_output(first_nonempty(
            output_obj.get("text_output"), output_obj.get("output"), receipt_data.get("text_output")))
        generated_receipt = latest_generated_receipt()

        if not (self.mode == "running" or self.receipt_logs or self.return_code is not None):
            box_w = min(128, max(86, w - 12))
            box_h = min(27, max(22, h - 6))
            x = max(0, (w - box_w) // 2)
            y = max(1, (h - box_h) // 2)
            draw_box(x, y, box_w, box_h)
            center_text(y + 2, "ORACLEREE", curses.A_BOLD | curses.color_pair(3))
            center_text(y + 3, title, curses.A_BOLD | curses.color_pair(6))
            center_text(y + 4, desc, curses.color_pair(2))
            ix = x + 6
            field_w = box_w - 12
            blink = int(time.time() * 2) % 2 == 0
            cursor = "█" if blink else " "
            self.put(stdscr, y + 7, ix, "1. RECEIPT PATH", curses.color_pair(6) | curses.A_BOLD)
            self.put(stdscr, y + 8, ix, "Path to receipt_*.json", curses.color_pair(2))
            self.put(stdscr, y + 9, ix, "╭" + "─" * (field_w - 2) + "╮", curses.color_pair(6))
            self.put(stdscr, y + 10, ix, "│", curses.color_pair(6))
            self.put(stdscr, y + 10, ix + field_w - 1, "│", curses.color_pair(6))
            self.put(stdscr, y + 11, ix, "╰" + "─" * (field_w - 2) + "╯", curses.color_pair(6))
            if self.receipt_path_input:
                shown = compact_value(self.receipt_path_input, field_w - 8)
                line = "> " + shown + " " + cursor
                attr = curses.color_pair(1)
            else:
                line = "> " + cursor
                attr = curses.color_pair(6)
            self.put(stdscr, y + 10, ix + 2, line[:field_w - 4], attr)
            self.put(stdscr, y + 13, ix, "WHAT THIS DOES", curses.color_pair(6) | curses.A_BOLD)
            if is_verify:
                help_lines = [
                    "• Re-runs REE verification using the receipt",
                    "• Confirms output + receipt hashes match exactly",
                    "• Strongest receipt check",
                ]
            else:
                help_lines = [
                    "• Checks receipt JSON structure",
                    "• Validates required hashes and fields",
                    "• Fast pre-check before full verify",
                ]
            for i, line in enumerate(help_lines):
                self.put(stdscr, y + 14 + i, ix, line, curses.color_pair(2))
            stat_attr = curses.color_pair(3) if self.receipt_status in ("PASSED", "Success", "Ready to run") else curses.color_pair(5)
            self.put(stdscr, y + 18, ix, f"Status: {self.receipt_status}", stat_attr | curses.A_BOLD)
            controls = "TAB switch field   r run   h home   q quit   DEL clear field"
            self.put(stdscr, y + box_h - 2, ix, controls[:field_w], curses.color_pair(2))
            return

        self.put(stdscr, 0, 0, " " * w, curses.A_REVERSE)
        self.put(stdscr, 0, 1, "ORACLEREE", curses.A_REVERSE | curses.A_BOLD)
        self.put(stdscr, 0, 12, title.title(), curses.A_REVERSE)
        done = self.mode == "done" or self.return_code is not None
        ok = (self.return_code == 0) or self.receipt_status in ("PASSED", "Success")
        failed = done and not ok
        state_text = "SUCCESS" if ok else ("FAILED" if failed else "RUNNING")
        state_attr = curses.color_pair(3) if ok else (curses.color_pair(4) if failed else curses.color_pair(5))
        header_status = f"Status: {state_text if done else 'Running ' + action + '...'}"
        self.put(stdscr, 0, max(14, w - len(header_status) - 2), header_status, curses.A_REVERSE)
        margin_x = 2
        x = margin_x
        y = 2
        content_w = max(96, w - margin_x * 2)
        content_h = max(25, h - 4)
        draw_box(x, y - 1, content_w, content_h)
        inner_x = x + 3
        inner_y = y + 1
        inner_w = content_w - 6
        elapsed = int((self.finished_at or time.time()) - self.started_at) if self.started_at else 0
        pct = 1.0 if done else min(0.94, 0.10 + elapsed / (180 if is_verify else 40))
        mode_text = "FULL RE-EXECUTION VERIFY" if is_verify else "FAST STRUCTURE VALIDATION"
        self.put(stdscr, inner_y, inner_x, "⋆ RECEIPT CHECK", curses.color_pair(6) | curses.A_BOLD)
        self.put(stdscr, inner_y, inner_x + 22, mode_text, curses.color_pair(6))
        self.put(stdscr, inner_y + 1, inner_x, "Status", curses.color_pair(2))
        self.put(stdscr, inner_y + 1, inner_x + 12, state_text, state_attr | curses.A_BOLD)
        self.put(stdscr, inner_y + 1, inner_x + 30, f"Progress {int(pct * 100)}%", curses.color_pair(1))
        self.put(stdscr, inner_y + 1, inner_x + inner_w - 14, f"Elapsed {elapsed}s", curses.color_pair(2))
        bar_w = max(20, inner_w - 14)
        filled = int(bar_w * pct)
        self.put(stdscr, inner_y + 2, inner_x + 12, "█" * filled + "░" * (bar_w - filled), curses.color_pair(3 if ok else 6))
        gap = 2
        left_w = max(50, int(inner_w * 0.47))
        right_w = inner_w - left_w - gap
        left_x = inner_x
        right_x = inner_x + left_w + gap
        top = inner_y + 5
        left_h = 18
        right_h = 18
        draw_box(left_x, top, left_w, left_h, "RECEIPT SUMMARY")
        draw_box(right_x, top, right_w, right_h, "CHECKLIST + HASHES")
        lx = left_x + 2
        ly = top + 2
        kv(ly, lx, "Path", receipt_path, left_w - 4, curses.color_pair(1), 14)
        kv(ly + 1, lx, "Model", model_name or "Unknown", left_w - 4, curses.color_pair(6), 14)
        kv(ly + 2, lx, "Device", device_name or "—", left_w - 4, curses.color_pair(2), 14)
        kv(ly + 3, lx, "Receipt", receipt_hash or "pending", left_w - 4, curses.color_pair(3 if receipt_hash else 2), 14)
        kv(ly + 4, lx, "Prompt", prompt_hash_val or "pending", left_w - 4, curses.color_pair(3 if prompt_hash_val else 2), 14)
        result_y = ly + 7
        self.put(stdscr, result_y, lx, "FINAL RESULT", curses.color_pair(6) | curses.A_BOLD)
        if not done:
            self.put(stdscr, result_y + 1, lx, "→ Verification running..." if is_verify else "→ Validation running...", curses.color_pair(6) | curses.A_BOLD)
            self.put(stdscr, result_y + 2, lx, "Model output will appear after verification completes." if is_verify else "Hash check results will appear after validation completes.", curses.color_pair(2))
        elif ok:
            if is_verify:
                self.put(stdscr, result_y + 1, lx, "✓ VERIFY PASSED", curses.color_pair(3) | curses.A_BOLD)
                self.put(stdscr, result_y + 2, lx, "✓ Receipt hashes match", curses.color_pair(3))
                self.put(stdscr, result_y + 3, lx, "✓ Model output reproduced", curses.color_pair(3))
            else:
                self.put(stdscr, result_y + 1, lx, "✓ ALL CHECKS PASSED", curses.color_pair(3) | curses.A_BOLD)
                self.put(stdscr, result_y + 2, lx, "✓ Receipt structure valid", curses.color_pair(3))
                self.put(stdscr, result_y + 3, lx, "✓ Required hashes verified", curses.color_pair(3))
        else:
            self.put(stdscr, result_y + 1, lx, "✕ CHECK FAILED", curses.color_pair(4) | curses.A_BOLD)
            self.put(stdscr, result_y + 2, lx, "Review clean logs below for the failure reason.", curses.color_pair(4))
        if is_verify:
            self.put(stdscr, result_y + 5, lx, "MODEL OUTPUT", curses.color_pair(6) | curses.A_BOLD)
            if done and ok:
                self.put(stdscr, result_y + 6, lx, compact_value(original_model_output, left_w - 6), curses.color_pair(3) | curses.A_BOLD)
            else:
                self.put(stdscr, result_y + 6, lx, "Model output will appear after verification completes.", curses.color_pair(2))
        rx = right_x + 2
        ry = top + 2
        if is_verify:
            steps = [
                ("Pull REE image", log_has("pulling from", "image is up to date")),
                ("Mount receipt", log_has("/mnt/receipt-path", "receipt-path")),
                ("Load receipt metadata", log_has("validating receipt", "receipt:")),
                ("Re-run inference", log_has("printing model outputs", "decode:", "model output")),
                ("Compare hashes", log_has("verification passed", "receipt hashes match", "verify: passed")),
                ("Verification passed", ok if done else False),
            ]
        else:
            steps = [
                ("JSON structure valid", bool(receipt_data)),
                ("prompt_hash PASS", log_has("prompt_hash: pass") or bool(prompt_hash_val)),
                ("parameters_hash PASS", log_has("parameters_hash: pass") or bool(parameters_hash)),
                ("receipt_hash PASS", log_has("receipt_hash: pass") or bool(receipt_hash)),
                ("tools_hash PASS", log_has("tools_hash: pass") or bool(tools_hash)),
                ("Validation complete", ok if done else False),
            ]
        self.put(stdscr, ry, rx, "PIPELINE" if is_verify else "VALIDATION CHECKS", curses.color_pair(6) | curses.A_BOLD)
        active_idx = 0
        for idx, (_, hit) in enumerate(steps):
            if hit:
                active_idx = idx + 1
        for i, (label, hit) in enumerate(steps):
            if hit:
                mark = "✓"; attr = curses.color_pair(3)
            elif self.mode == "running" and i == active_idx:
                mark = "→"; attr = curses.color_pair(6) | curses.A_BOLD
            else:
                mark = "○"; attr = curses.color_pair(2)
            self.put(stdscr, ry + 1 + i, rx, f"{mark} {label}", attr)
        hash_y = ry + 9
        self.put(stdscr, hash_y, rx, "HASHES", curses.color_pair(6) | curses.A_BOLD)
        receipt_hash_display = receipt_hash or ("PASS (validated)" if log_has("receipt_hash: pass") else "—")
        prompt_hash_display = prompt_hash_val or ("PASS (validated)" if log_has("prompt_hash: pass") else "—")
        parameters_hash_display = parameters_hash or ("PASS (validated)" if log_has("parameters_hash: pass", "params_hash: pass") else "—")
        tools_hash_display = tools_hash or ("PASS (validated)" if log_has("tools_hash: pass", "tool_hash: pass") else "—")
        kv(hash_y + 1, rx, "Receipt", receipt_hash_display, right_w - 4, curses.color_pair(3 if receipt_hash_display != "—" else 2), 13)
        kv(hash_y + 2, rx, "Prompt", prompt_hash_display, right_w - 4, curses.color_pair(3 if prompt_hash_display != "—" else 2), 13)
        kv(hash_y + 3, rx, "Params", parameters_hash_display, right_w - 4, curses.color_pair(3 if parameters_hash_display != "—" else 2), 13)
        kv(hash_y + 4, rx, "Tools", tools_hash_display, right_w - 4, curses.color_pair(3 if tools_hash_display != "—" else 2), 13)
        bottom_y = top + left_h + 1
        receipt_box_h = 5
        logs_h = max(7, content_h - (bottom_y - y) - receipt_box_h - 4)
        draw_box(inner_x, bottom_y, inner_w, receipt_box_h, "RECEIPT FILE")
        file_x = inner_x + 2
        file_y = bottom_y + 2
        display_receipt = generated_receipt if generated_receipt else receipt_path
        self.put(stdscr, file_y, file_x, "Receipt path", curses.color_pair(2))
        self.put(stdscr, file_y, file_x + 15, compact_value(display_receipt, inner_w - 19), curses.color_pair(3 if display_receipt else 2))
        verify_cmd = f"python3 ree.py {action} --receipt-path {display_receipt or receipt_path}"
        self.put(stdscr, file_y + 1, file_x, "Command", curses.color_pair(2))
        self.put(stdscr, file_y + 1, file_x + 15, compact_value(verify_cmd, inner_w - 19), curses.color_pair(2))
        logs_y = bottom_y + receipt_box_h + 1
        draw_box(inner_x, logs_y, inner_w, logs_h, "CLEAN LOGS")
        lines = clean_log_lines()
        if not lines and self.mode == "running":
            lines = ["Waiting for REE output..."]
        max_lines = max(1, logs_h - 3)
        for i, line in enumerate(lines[-max_lines:]):
            low = line.lower()
            attr = curses.color_pair(3) if "passed" in low or "success" in low else (curses.color_pair(4) if "failed" in low or "error" in low else curses.color_pair(2))
            self.put(stdscr, logs_y + 2 + i, inner_x + 2, compact_value(line, inner_w - 4), attr)
        controls = "r rerun   h home   q quit   DEL clear field"
        self.put(stdscr, y + content_h - 2, inner_x, controls[:inner_w], curses.color_pair(2))

    def draw_nav(self, stdscr: curses.window, w: int) -> None:
        if self.screen in ("home", "input", "receipt"):
            return
        self.put(stdscr, 0, 0, " " * w, curses.A_REVERSE)
        self.put(stdscr, 0, 1, "ORACLEREE", curses.A_REVERSE | curses.A_BOLD)
        # ── UPDATED: different title for settle mode ───────────────────
        dashboard_title = "Settlement Dashboard" if self.settle_mode else "Proof Dashboard"
        self.put(stdscr, 0, 12, dashboard_title, curses.A_REVERSE)
        status_str = f"Status: {self.status}"
        self.put(stdscr, 0, max(14, w - len(status_str) - 2), status_str, curses.A_REVERSE)

    def draw_footer(self, stdscr: curses.window, h: int, w: int) -> None:
        if self.screen in ("home", "input", "receipt"):
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

        # ── UPDATED: different subtitle for settle mode ────────────────
        if self.settle_mode:
            subtitle = "Settle a closed market using locked creator sources"
            mode_badge = "[ SETTLE MODE ]"
            mode_badge_attr = curses.color_pair(5) | curses.A_BOLD
        else:
            subtitle = "Gensyn Delphi settlement verification layer"
            mode_badge = ""
            mode_badge_attr = 0

        self.put(stdscr, y + 2, x + (box_w - len(title)) // 2, title, curses.A_BOLD | curses.color_pair(3))
        self.put(stdscr, y + 3, x + (box_w - len(tagline)) // 2, tagline, curses.A_BOLD | curses.color_pair(6))
        self.put(stdscr, y + 4, x + (box_w - len(subtitle)) // 2, subtitle, curses.color_pair(2))
        if mode_badge:
            self.put(stdscr, y + 4, x + box_w - len(mode_badge) - 4, mode_badge, mode_badge_attr)

        inner_x = x + 6
        field_w = box_w - 12
        blink = int(time.time() * 2) % 2 == 0
        cursor = "█" if blink else " "

        def draw_field(row: int, title: str, help_text: str, value: str, active: bool) -> None:
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
            "Paste the FULL settlement prompt from Delphi (sources locked at creation).",
            self.settlement_prompt_input,
            self.active_input_field == "prompt",
        )
        draw_field(
            12,
            "2. MARKET URL / MARKET ID",
            "Market URL or 0x ID — must match the prompt above.",
            self.market_ref_input,
            self.active_input_field == "market",
        )

        prompt_ok = bool(self.settlement_prompt_input.strip())
        market_ok = bool(self.market_ref_input.strip())
        status = "READY TO RUN" if prompt_ok and market_ok else "BOTH FIELDS REQUIRED"
        status_attr = curses.color_pair(3) | curses.A_BOLD if prompt_ok and market_ok else curses.color_pair(5)

        # ── UPDATED: different integrity text for settle mode ──────────
        if self.settle_mode:
            self.put(stdscr, y + 18, inner_x, "TRUSTLESS SETTLEMENT", curses.color_pair(1) | curses.A_BOLD)
            self.put(stdscr, y + 19, inner_x,
                     "Sources locked at market creation — cannot be changed retroactively.",
                     curses.color_pair(2))
        else:
            self.put(stdscr, y + 18, inner_x, "INTEGRITY", curses.color_pair(1) | curses.A_BOLD)
            self.put(stdscr, y + 19, inner_x,
                     "Prompt + market anchor will be compared before canonical proof.",
                     curses.color_pair(2))

        self.put(stdscr, y + 20, inner_x, status, status_attr)
        controls = "TAB switch field   ENTER next/done   r run   q quit   DEL clear field"
        self.put(stdscr, y + 22, inner_x, controls[:field_w], curses.color_pair(2))
        if self.status and self.status not in ("Ready", "Input ready. Press r to run.",
                                                "Settle mode: paste settlement prompt + market URL/ID"):
            self.put(stdscr, y + box_h - 2, inner_x,
                     compact_value(f"Status: {self.status}", field_w), curses.color_pair(5))

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
        # During a live oracle run, proof_lines/logs may include stale or partial
        # artifact text. The UI must show Waiting... until the final proof is complete.
        if self.oracle_run_active():
            return ""
        needles_l = [n.lower() for n in needles]
        for line in reversed(self.proof_lines + self.logs):
            low = line.lower()
            if all(n in low for n in needles_l):
                if ":" in line:
                    return line.split(":", 1)[1].strip()
                return line.strip()
        return ""

    def latest_saved_file(self) -> str:
        """
        Find the proof JSON saved by oracle_ree.py.

        Important TUI rule:
        - While a run is still active, do NOT reveal saved proof files or fall back
          to old/current partial artifacts. Show Waiting... instead.
        - If the backend fails, do NOT show stale or partial proof files. Show FAILED.
        - Only after return_code == 0 can the TUI reveal proof artifacts.
        """
        run_active = self.oracle_run_active()
        if run_active or self.return_code not in (None, 0):
            return ""

        lines = list(self.proof_lines) + list(self.logs)
        for line in reversed(lines):
            s = str(line).strip()
            m = re.search(r'(/[^\s]+oracle_proof_[^\s]+\.json)', s)
            if m:
                return m.group(1)
            m = re.search(r'\b(oracle_proof_[A-Za-z0-9_x\-]+_[0-9_]+\.json)\b', s)
            if m:
                return m.group(1)
            low = s.lower()
            if "saved to" in low or "saved:" in low or "saved file" in low:
                if ":" in s:
                    possible = s.split(":", 1)[1].strip()
                    if possible:
                        return possible
                parts = s.split()
                for part in reversed(parts):
                    if part.startswith("oracle_proof_") and part.endswith(".json"):
                        return part

        # Fallback: newest proof file in current project dir. Prefer matching market id prefix.
        # IMPORTANT: never use filesystem fallback during an active/failed run. It can show
        # stale hashes/receipt paths from a previous run before the new backend has produced
        # a proof. During running/failed states, only explicit lines from THIS run count.
        if self.mode == "running" or (self.return_code not in (None, 0)):
            return ""
        try:
            base = Path(__file__).parent
            files = list(base.glob("oracle_proof_*.json"))
            if self.resolved_market_id:
                short = self.resolved_market_id[:10]
                matching = [f for f in files if f.name.startswith(f"oracle_proof_{short}")]
                if matching:
                    files = matching
            if files:
                newest = max(files, key=lambda f: f.stat().st_mtime)
                return str(newest)
        except Exception:
            pass
        return ""

    def latest_proof_json(self) -> dict:
        """Load the current run proof JSON, always returning a dict.

        Never return a string here. Downstream code relies on isinstance(proof, dict)
        and stale/invalid proof files must behave exactly like "no proof".
        """
        if self.oracle_run_active() or self.return_code not in (None, 0):
            return {}
        saved = self.latest_saved_file()
        if not saved:
            return {}
        path = Path(saved)
        if not path.is_absolute():
            path = Path(__file__).parent / saved
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return {}
                # Ignore a stale proof from a different market if we know the current market id.
                current = (self.resolved_market_id or self.preflight_market_id or "").lower()
                proof_mid = str(data.get("market_id") or (data.get("oracle_evidence") or {}).get("market_id") or "").lower()
                if current and proof_mid and current != proof_mid:
                    return {}

                # Ignore a stale proof created before this run began. This matters for settle mode
                # and reruns on the same market where old proof files share the same filename prefix.
                if self.started_at:
                    captured = str((data.get("oracle_evidence") or {}).get("captured_at") or data.get("created_at") or "")
                    if captured:
                        try:
                            from datetime import datetime, timezone
                            ct = datetime.fromisoformat(captured.replace("Z", "+00:00"))
                            if ct.tzinfo is None:
                                ct = ct.replace(tzinfo=timezone.utc)
                            if ct.timestamp() < self.started_at - 10:
                                return {}
                        except Exception:
                            pass
                return data
        except Exception:
            return {}
        return {}

    def latest_ree_receipt_path(self) -> str:
        run_active = self.oracle_run_active()
        if run_active or self.return_code not in (None, 0):
            return ""
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
        for line in reversed(self.logs):
            low = line.lower()
            if "receipt" in low and "/" in line and ("metadata" in low or "receipt_" in low):
                val = line.split(":", 1)[1].strip() if ":" in line else line.strip()
                if val and not val.startswith("sha256:"):
                    return val
        return ""

    def latest_ree_receipt_hash(self) -> str:
        run_active = self.oracle_run_active()
        if run_active or self.return_code not in (None, 0):
            return ""
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
        run_active = self.oracle_run_active()
        if run_active or self.return_code not in (None, 0):
            return ""
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
            resolves = str(self.market_data.get("resolvesAt", "") or self.market_data.get("closeTime", "") or "")[:16].replace("T", " ")
            delphi_model = str(model_info.get("model_identifier") or model_info.get("modelIdentifier") or self.market_data.get("judgeModel", "") or delphi_model)
            src = self.market_data.get("dataSources") or meta.get("dataSources") or []
            outs = meta.get("outcomes") or self.market_data.get("outcomes") or []
            sources = " · ".join(map(str, src))
            outcomes = " · ".join(map(str, outs))
            prompt_ctx = str(model_info.get("prompt_context") or model_info.get("promptContext") or self.market_data.get("settlementPrompt") or "")
        elif not mid:
            prompt_ctx = self.market_input
            m = re.search(r"QUESTION:\s*(.+?)(?:DATA SOURCES:|SETTLEMENT RULES:|VALID OUTCOMES|$)", self.market_input, re.I | re.S)
            question = " ".join(m.group(1).split()) if m else (self.resolved_question or "Raw settlement prompt")
        else:
            prompt_ctx = self.market_input if len(self.market_input) > 80 else ""
            if not question:
                question = "Resolving market details..."

        return question, mid, status, resolves, delphi_model, sources, outcomes, prompt_ctx

    def creator_outcome(self) -> str:
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
        """
        Return the canonical OracleREE result for the TUI.

        Priority:
        1. Saved proof JSON canonical fields.
        2. Live stdout lines only while proof is not available.

        This fixes the repeated bug where the backend proof was correct but the
        dashboard showed an early/stale `event verdict` or `price verdict`.
        """
        # HARD RULE: while a new run is active, never display previous proof/output.
        # This stops old OracleREE result / old MATCH/MISMATCH from appearing during reruns.
        if self.oracle_run_active():
            return ""

        proof = self.latest_proof_json()
        proof_result = canonical_outcome_from_proof(proof)
        if proof_result:
            # Keep the live state synced so the rest of the screen uses the proof result.
            self.oracle_result = proof_result
            return proof_result

        if self.oracle_result:
            live_val = str(self.oracle_result).strip()
            if live_val.lower() == "inconclusive":
                return "INCONCLUSIVE"
            if live_val.lower() not in {"none", "null", "unknown"}:
                return live_val

        # Last-resort live log parsing before the saved proof exists.
        for line in reversed(self.proof_lines + self.logs):
            s = str(line).strip()
            low = s.lower()

            # Prefer explicit canonical result lines if backend prints them.
            if "final_canonical_result" in low or "canonical_oracle_result" in low:
                val = s.split(":", 1)[1].strip() if ":" in s else s.split()[-1].strip()
                if val:
                    if val.lower() == "inconclusive":
                        self.oracle_result = "INCONCLUSIVE"
                        return "INCONCLUSIVE"
                    if val.lower() not in {"none", "null", "unknown"}:
                        self.oracle_result = val
                        return val

            if "resolved_outcome" in low and "outcome" in low:
                m = re.search(r'"outcome"\s*:\s*"([^"]+)"', s)
                if m:
                    val = m.group(1).strip()
                    if val.lower() == "inconclusive":
                        self.oracle_result = "INCONCLUSIVE"
                        return "INCONCLUSIVE"
                    if val.lower() not in {"none", "null", "unknown"}:
                        self.oracle_result = val
                        return val

            # Last-resort parsing only for explicit result lines. Do not parse random
            # planning/debug lines that merely contain the word "outcome".
            if any(k in low for k in ["final_outcome:", "oracle_result:", "oracle outcome:", "event verdict:", "price verdict:"]):
                m = re.search(r":\s*([^.;\n]+)", s, re.I)
                if m:
                    val = m.group(1).strip().strip('"\'')
                    if val.lower() == "inconclusive":
                        return "INCONCLUSIVE"
                    if val.lower() not in {"none", "null", "unknown"}:
                        return val
        return ""

    def oracle_result_display(self) -> str:
        if self.oracle_run_active():
            return "Fetching evidence..."
        result = self.oracle_outcome()
        if result:
            return result
        if self.market_data:
            status = self.market_data.get("status", "")
            if status == "open":
                return "Market still live"
            if status not in ("settled",):
                return "Awaiting settlement"
        return "INCONCLUSIVE"


    def ree_expected_outcome_from_proof(self) -> str:
        """Return the verified outcome embedded in the REE prompt inside the saved proof."""
        if self.oracle_run_active():
            return ""
        proof = self.latest_proof_json()
        if not isinstance(proof, dict):
            return ""
        receipt = proof.get("ree_receipt")
        if not isinstance(receipt, dict):
            return ""
        inp = receipt.get("input")
        if not isinstance(inp, dict):
            return ""
        prompt = str(inp.get("prompt") or "")
        m = re.search(r"Verified outcome:\s*(.+?)(?:\n|$)", prompt, re.I)
        if m:
            val = m.group(1).strip()
            if val:
                return "INCONCLUSIVE" if val.lower() == "inconclusive" else val
        return ""

    def ree_oracle_link_status(self) -> tuple[str, int]:
        """
        Compare OracleREE's saved final outcome with the REE prompt's verified outcome.
        This is NOT the creator settlement comparison. It only checks whether the proof
        sent to REE matches OracleREE's canonical output.
        """
        if self.oracle_run_active():
            return "PENDING", curses.color_pair(6)
        oracle = self.oracle_outcome().strip()
        expected = self.ree_expected_outcome_from_proof().strip()
        if not oracle or not expected:
            return "—", curses.color_pair(2)
        if oracle.lower() == expected.lower():
            return "MATCH", curses.color_pair(3) | curses.A_BOLD
        # Also accept if oracle result appears anywhere in REE text output
        try:
            proof = self.latest_proof_json() or {}
            text_out = str((proof.get("ree_receipt") or {}).get("output", {}).get("text_output") or "").lower()
            if oracle.lower() in text_out:
                return "MATCH", curses.color_pair(3) | curses.A_BOLD
        except Exception:
            pass
        return "REE MISMATCH", curses.color_pair(4) | curses.A_BOLD

    def settlement_match_status(self) -> tuple[str, int]:
        if self.oracle_run_active():
            return "PENDING", curses.color_pair(6)
        creator = self.creator_outcome().strip().lower()
        oracle_raw = self.oracle_outcome().strip()
        oracle = oracle_raw.lower()
        if self.market_data and self.market_data.get("status") != "settled":
            return "WAITING FOR SETTLEMENT", curses.color_pair(5) | curses.A_BOLD
        if self.mode == "running":
            return "RUNNING", curses.color_pair(6) | curses.A_BOLD
        if self.return_code not in (None, 0):
            return "FAILED", curses.color_pair(4) | curses.A_BOLD
        if creator and oracle:
            if oracle in {"none", "null", "unknown", "market not settled", "market still live", "awaiting settlement"}:
                return "PENDING", curses.color_pair(2)
            if oracle == "inconclusive":
                # Creator result exists, but OracleREE could not independently verify it.
                # This is not waiting for creator; it is an oracle audit result.
                return "ORACLE INCONCLUSIVE", curses.color_pair(5) | curses.A_BOLD
            if creator == oracle or creator in oracle or oracle in creator:
                return "MATCH", curses.color_pair(3) | curses.A_BOLD
            return "CREATOR MISMATCH", curses.color_pair(4) | curses.A_BOLD
        if not creator:
            return "NO CREATOR RESULT", curses.color_pair(5) | curses.A_BOLD
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
            evidence_hash = (self.latest_line_value("oracle evidence hash") or
                             self.latest_line_value("evidence hash") or
                             self.latest_line_value("oracle hash"))
            ipfs = self.latest_line_value("ipfs cid")
            verdict = (self.latest_line_value("price verdict") or
                       self.latest_line_value("event verdict") or
                       self.latest_line_value("verdict"))
            rows = [("Oracle hash", evidence_hash), ("IPFS CID", ipfs), ("Verdict", verdict)]
            printed = False
            for label, val in rows:
                if val and y < h - 2:
                    y = self.kv(stdscr, y, pad_x, inner_w, label, val,
                                curses.color_pair(3) if label != "Verdict" else curses.color_pair(1))
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
        if y < h - 2:
            y = self.section_header(stdscr, y, rx, rw, "PROOF PACKAGE")
        combined = self.latest_combined_hash()
        oracle_hash = (self.latest_line_value("oracle evidence hash") or
                       self.latest_line_value("evidence hash") or
                       self.latest_line_value("oracle hash"))
        ree_hash = self.latest_ree_receipt_hash()
        ipfs = self.latest_line_value("ipfs cid")
        saved = self.latest_saved_file()
        receipt_path = self.latest_ree_receipt_path()
        proof_rows = [
            ("Combined", combined), ("Oracle", oracle_hash), ("REE", ree_hash),
            ("IPFS", ipfs), ("Saved", saved), ("Receipt", receipt_path),
        ]
        printed = False
        for label, val in proof_rows:
            if val and y < h - 8:
                y = self.kv(stdscr, y, inner_x, inner_w, label, val,
                            curses.color_pair(3) if label in ("Combined", "Oracle", "REE", "IPFS") else curses.color_pair(2))
                printed = True
        if not printed and y < h - 8:
            self.put(stdscr, y, inner_x, "Proof hashes appear here as the run completes.", curses.color_pair(2))
            y += 1
        if self.return_code == 0 and y < h - 6:
            y += 1
            y = self.section_header(stdscr, y, rx, rw, "VERIFY")
            verify = "python3 ree.py verify --receipt-path <receipt.json>"
            self.put(stdscr, y, inner_x, "✓ Oracle data + REE execution cryptographically linked", curses.color_pair(3))
            y += 1
            self.put(stdscr, y, inner_x, verify[:inner_w], curses.color_pair(2))
            y += 1
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
            # HARD LIVE-VIEW OVERRIDE: while running, never render stale proof artifacts/results.
            if self.oracle_run_active():
                artifact_labels = {
                    "Oracle Hash", "IPFS CID", "Receipt", "REE Hash",
                    "Prompt Mode", "Prompt Match", "Question Match",
                }
                if label in {"Oracle Hash", "IPFS CID", "REE Hash"}:
                    value = "Waiting..."
                    attr = curses.color_pair(6)
                elif label == "Receipt":
                    value = "Waiting for REE..."
                    attr = curses.color_pair(6)
                elif label in {"Prompt Mode", "Prompt Match", "Question Match"}:
                    value = "Verifying..."
                    attr = curses.color_pair(6)
                elif label == "OracleREE":
                    value = "Fetching evidence..."
                    attr = curses.color_pair(6)
                elif label == "Comparison":
                    value = "Waiting for result..."
                    attr = curses.color_pair(6)
            label_w = 14
            self.put(stdscr, y, x, f"{label:<{label_w}}", curses.color_pair(2))
            self.put(stdscr, y, x + label_w + 1, compact_value(value or "—", bw - label_w - 2), attr or curses.color_pair(1))
            return y + 1

        def section_lines(prompt: str, heading: str) -> list[str]:
            lines = [ln.rstrip() for ln in prompt.splitlines()]
            out: list[str] = []
            capture = False
            for ln in lines:
                up = ln.upper().strip()
                if heading in up:
                    capture = True; continue
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
            return [s.lstrip("-• ").strip() for s in src if s.lstrip("-• ").strip()][:5]

        def extract_outcome_lines(prompt: str) -> list[str]:
            lines = section_lines(prompt, "VALID OUTCOMES")
            return [ln.lstrip("-• ").strip() for ln in lines if ln.lstrip("-• ").strip()][:6]

        def progress_state(key: str, index: int) -> tuple[str, int]:
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
            if self.mode == "running" and self.phase == "ree" and key == "ree":
                current = True; done = False
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
        prompt_outcomes = extract_outcome_lines(full_prompt)

        # A run is active whenever the OracleREE backend is still executing.
        # Do not reveal old/partial proof artifacts before successful completion.
        run_active = self.oracle_run_active()
        proof_ready = (self.return_code == 0 and not run_active)
        if proof_ready:
            artifact_waiting = "—"
        elif self.return_code not in (None, 0):
            artifact_waiting = "FAILED"
        else:
            # Results page before/during proof completion should show Waiting..., never stale dash/hash/file.
            artifact_waiting = "Waiting..."

        if proof_ready:
            oracle_hash = (self.latest_line_value("oracle evidence hash") or
                           self.latest_line_value("evidence hash") or
                           self.latest_line_value("oracle hash"))
            ipfs = self.latest_line_value("ipfs cid")
            combined = self.latest_combined_hash()
            ree_hash = self.latest_ree_receipt_hash()
            saved = self.latest_saved_file()
            receipt_path = self.latest_ree_receipt_path()
        else:
            oracle_hash = ""
            ipfs = ""
            combined = ""
            ree_hash = ""
            saved = ""
            receipt_path = ""

        pct = int(self.progress * 100)
        elapsed = int(((self.finished_at or time.time()) - self.started_at)) if self.started_at else 0
        if self.return_code == 0:
            run_status = "SUCCESS"; status_attr = curses.color_pair(3)
        elif self.return_code == 2:
            run_status = "ORACLE OK · NO RECEIPT"; status_attr = curses.color_pair(5)
        elif self.return_code is None:
            run_status = self.phase.upper(); status_attr = curses.color_pair(5)
        else:
            run_status = "FAILED"; status_attr = curses.color_pair(4)

        market_h = 9
        summary_h = 7
        evidence_h = max(12, usable_h - market_h - summary_h - 2)
        if usable_h < 38:
            market_h = 8; summary_h = 6
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

        # ── LEFT: SETTLEMENT SUMMARY ─────────────────────────────────────
        # Title changes based on mode
        left_summary_title = "⚖️  MARKET SETTLEMENT" if self.settle_mode else "🏆 SETTLEMENT SUMMARY"
        lx, y, bw, bh = box(1, top + market_h + 1, left_w, summary_h, left_summary_title)
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

        # ── LEFT: ORACLE EVIDENCE + RESULT ───────────────────────────────
        # Title changes based on mode
        evidence_title = "🔒 ORACLE SETTLEMENT PROOF" if self.settle_mode else "🔒 ORACLE EVIDENCE + RESULT"
        lx, y, bw, bh = box(1, top + market_h + summary_h + 2, left_w, evidence_h, evidence_title)
        yy = y + 2
        oracle_res = self.oracle_result_display()
        artifact_attr = curses.color_pair(3) if proof_ready else (curses.color_pair(4) if artifact_waiting == "FAILED" else curses.color_pair(6) if run_active else curses.color_pair(2))
        yy = put_kv(lx + 2, yy, bw - 4, "Oracle Hash", oracle_hash if proof_ready and oracle_hash else artifact_waiting, curses.color_pair(3) if proof_ready and oracle_hash else artifact_attr)
        yy = put_kv(lx + 2, yy, bw - 4, "IPFS CID", ipfs if proof_ready and ipfs else artifact_waiting, curses.color_pair(3) if proof_ready and ipfs else artifact_attr)
        if proof_ready and receipt_path and yy < y + bh - 1:
            import re as _re2
            _m = _re2.search(r"/[^\s]+receipt_[0-9_]+\.json", receipt_path)
            clean_r = _m.group(0) if _m else receipt_path
            yy = put_kv(lx + 2, yy, bw - 4, "Receipt", clean_r, curses.color_pair(3))
        else:
            waiting = "Waiting..." if run_active or self.return_code is None else ("FAILED" if self.return_code not in (None, 0) else "—")
            attr = curses.color_pair(6) if waiting == "Waiting..." else (curses.color_pair(4) if waiting == "FAILED" else curses.color_pair(2))
            yy = put_kv(lx + 2, yy, bw - 4, "Receipt", waiting, attr)
        if proof_ready and ree_hash and yy < y + bh - 1:
            yy = put_kv(lx + 2, yy, bw - 4, "REE Hash", ree_hash, curses.color_pair(3))
        if yy < y + bh - 1:
            yy += 1
        _w = "Verifying..." if run_active else ("FAILED" if self.return_code not in (None, 0) else "—")
        yy = put_kv(lx + 2, yy, bw - 4, "Prompt Mode", self.verification_mode or _w,
                    curses.color_pair(3) if self.verification_mode else curses.color_pair(6) if run_active else curses.color_pair(2))
        yy = put_kv(lx + 2, yy, bw - 4, "Prompt Match", self.prompt_match or _w,
                    curses.color_pair(3) if str(self.prompt_match).upper() == "YES" else curses.color_pair(6) if run_active else curses.color_pair(2))
        yy = put_kv(lx + 2, yy, bw - 4, "Question Match", self.question_match or _w,
                    curses.color_pair(3) if str(self.question_match).upper() == "YES" else curses.color_pair(6) if run_active else curses.color_pair(2))
        if yy < y + bh - 1:
            yy += 1

        # ── SETTLE MODE: Show verdict as canonical result; hide creator comparison ──
        if self.settle_mode:
            oracle_display = "Fetching evidence..." if run_active else oracle_res
            oracle_attr = (curses.color_pair(6) if run_active
                           else curses.color_pair(3) | curses.A_BOLD if oracle_res not in ("INCONCLUSIVE", "")
                           else curses.color_pair(5))
            if yy < y + bh - 1:
                yy = put_kv(lx + 2, yy, bw - 4, "Settlement", oracle_display, oracle_attr)
            if self.return_code == 0 and oracle_res not in ("INCONCLUSIVE", "") and yy < y + bh - 1:
                self.put(stdscr, yy, lx + 2, "★ SETTLEMENT VERDICT", curses.color_pair(3) | curses.A_BOLD)
                yy += 1
                if yy < y + bh - 1:
                    self.put(stdscr, yy, lx + 2, f"  {oracle_res}", curses.color_pair(3) | curses.A_BOLD)
                    yy += 1
            elif run_active and yy < y + bh - 1:
                self.put(stdscr, yy, lx + 2, "→ Settling market...", curses.color_pair(6) | curses.A_BOLD)
                yy += 1
        else:
            # ── VERIFY MODE: Show creator result + comparison ──────────
            creator_res = self.creator_outcome()
            match_txt, match_attr = self.settlement_match_status()
            yy = put_kv(lx + 2, yy, bw - 4, "Creator Result", creator_res or "—",
                        curses.color_pair(3) if creator_res else curses.color_pair(2))
            oracle_display = "Fetching evidence..." if run_active else oracle_res
            oracle_attr = (curses.color_pair(6) if run_active
                           else curses.color_pair(3) if oracle_res != "INCONCLUSIVE"
                           else curses.color_pair(5))
            yy = put_kv(lx + 2, yy, bw - 4, "OracleREE", oracle_display, oracle_attr)
            # ree_txt, ree_attr = self.ree_oracle_link_status()
            # ree_display = "Waiting for result..." if run_active else ree_txt
            # yy = put_kv(lx + 2, yy, bw - 4, "REE Proof", ree_display,
            # curses.color_pair(6) if run_active else ree_attr)
            comp_display = "Waiting for result..." if run_active else match_txt
            comp_attr = curses.color_pair(6) if run_active else match_attr
            yy = put_kv(lx + 2, yy, bw - 4, "Creator Compare", comp_display, comp_attr)

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
        pipeline_items = [
            ("fetch", "Fetch"), ("oracle", "Verify"), ("ipfs", "IPFS"),
            ("inject", "Inject"), ("ree", "Run REE"), ("receipt", "Proof"), ("done", "Done"),
        ]
        step_count = len(pipeline_items)
        usable_w = max(28, bw - 8)
        reserved_right = 18 if self.mode == "running" else 2
        steps_w = max(28, usable_w - reserved_right)
        col_w = max(9, steps_w // step_count)
        icon_y = yy; label_y = yy + 2; guide_y = yy + 1
        for i in range(step_count - 1):
            sx = rx + 4 + (i * col_w); ex = rx + 4 + ((i + 1) * col_w)
            if ex > sx + 2:
                self.put(stdscr, guide_y, sx + 2, "─" * max(1, ex - sx - 3), curses.color_pair(2))
        current_label = ""
        for i, (key, label) in enumerate(pipeline_items):
            col_x = rx + 4 + i * col_w
            state, attr = progress_state(key, i)
            marker = "✓" if state == "done" else "➜" if state == "current" else "✕" if state == "failed" else "○"
            if state == "current":
                current_label = {
                    "fetch": "Fetch market from Delphi", "oracle": "Fetch and verify oracle evidence",
                    "ipfs": "Pin evidence to IPFS", "inject": "Inject oracle block into prompt",
                    "ree": "Run Gensyn REE inference", "receipt": "Generate combined proof", "done": "Done",
                }.get(key, label)
            self.put(stdscr, icon_y, col_x, marker, attr | curses.A_BOLD)
            self.put(stdscr, label_y, col_x, label[:col_w - 1], attr)
        if self.mode == "running":
            spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[int(time.time() * 8) % 10]
            running = f"{spinner} RUNNING {pct}%"
            self.put(stdscr, icon_y, rx + bw - len(running) - 3, running, curses.color_pair(6) | curses.A_BOLD)
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
        waiting = artifact_waiting
        if run_active or (self.return_code is None and not proof_ready):
            artifacts = [
                ("Oracle Hash", "Waiting..."),
                ("IPFS CID", "Waiting..."),
                ("REE Receipt Hash", "Waiting..."),
                ("Combined Hash", "Waiting..."),
                ("Receipt Path", "Waiting..."),
                ("Saved File", "Waiting..."),
            ]
        elif self.return_code not in (None, 0):
            artifacts = [
                ("Oracle Hash", "FAILED"),
                ("IPFS CID", "FAILED"),
                ("REE Receipt Hash", "FAILED"),
                ("Combined Hash", "FAILED"),
                ("Receipt Path", "FAILED"),
                ("Saved File", "FAILED"),
            ]
        else:
            artifacts = [
                ("Oracle Hash", oracle_hash if proof_ready and oracle_hash else waiting),
                ("IPFS CID", ipfs if proof_ready and ipfs else waiting),
                ("REE Receipt Hash", ree_hash if proof_ready and ree_hash else waiting),
                ("Combined Hash", combined if proof_ready and combined else waiting),
                ("Receipt Path", receipt_path if proof_ready and receipt_path else waiting),
                ("Saved File", saved if proof_ready and saved else waiting),
            ]
        for name, val in artifacts:
            if yy >= y + bh - 1:
                break
            ok = (self.return_code == 0 and self.mode != "running" and proof_ready and bool(val) and val not in ("—", "Waiting...", "FAILED"))
            value_attr = curses.color_pair(3) if ok else (curses.color_pair(4) if val == "FAILED" else curses.color_pair(6) if val == "Waiting..." else curses.color_pair(2))
            self.put(stdscr, yy, rx + 2, "☑" if ok else "·", value_attr if ok else curses.color_pair(2))
            self.put(stdscr, yy, rx + 5, name[:21], curses.color_pair(1))
            self.put(stdscr, yy, rx + 28, compact_value(val, bw - 31), value_attr)
            yy += 1

        # ── RIGHT: VERIFY / SETTLE CONFIRMATION ──────────────────────────
        rx, y, bw, bh = box(right_x, top + exec_h + pipeline_h + proof_h + 3, right_w, verify_h, "⋆ VERIFY")
        yy = y + 2
        if self.return_code == 0:
            if not receipt_path or not ree_hash:
                self.put(stdscr, yy, rx + 2, "⚠ Oracle proof saved, but REE receipt was not found", curses.color_pair(5) | curses.A_BOLD)
            elif self.settle_mode:
                # Settle mode: confirm the verdict IS the settlement
                self.put(stdscr, yy, rx + 2, "✓ Settlement proof cryptographically anchored to REE", curses.color_pair(3)); yy += 1
                import re as _re
                m = _re.search(r"(/[^\s]+receipt_[0-9_]+\.json)", receipt_path)
                clean_path = m.group(1) if m else receipt_path
                self.put(stdscr, yy, rx + 2, compact_value(clean_path, bw - 4), curses.color_pair(2))
            else:
                self.put(stdscr, yy, rx + 2, "✓ Oracle data + REE execution cryptographically linked", curses.color_pair(3)); yy += 1
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

        important_logs = [
            l for l in self.logs[-12:]
            if any(k in l.lower() for k in [
                "error", "uuid detected", "found question", "matched market",
                "market id:", "fetching market from delphi", "could not", "404",
                "receipt saved", "receipt:", "settle"
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
        if self.screen == "home":
            self.draw_home_page(stdscr, h, w)
        elif self.screen == "input":
            self.draw_input_page(stdscr, h, w)
        elif self.screen == "receipt":
            self.draw_receipt_page(stdscr, h, w)
        else:
            self.draw_results_page(stdscr, h, w)
        self.draw_footer(stdscr, h, w)
        stdscr.noutrefresh()
        curses.doupdate()

    def edit_input(self, stdscr: curses.window) -> bool:
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
                if cx > 0: cx -= 1
                elif cy > 0: cy -= 1; cx = len(lines[cy])
                continue
            if key == curses.KEY_RIGHT:
                if cx < len(lines[cy]): cx += 1
                elif cy + 1 < len(lines): cy += 1; cx = 0
                continue
            if key == curses.KEY_UP:
                cy = max(0, cy - 1); cx = min(cx, len(lines[cy])); continue
            if key == curses.KEY_DOWN:
                cy = min(len(lines) - 1, cy + 1); cx = min(cx, len(lines[cy])); continue
            if key == curses.KEY_HOME:
                cx = 0; continue
            if key == curses.KEY_END:
                cx = len(lines[cy]); continue
            if isinstance(key, str):
                parts = re.split(r'(\r\n|\n|\r)', key)
                for part in parts:
                    if part in ("\r\n", "\n", "\r"):
                        current = lines[cy]
                        remainder = current[cx:]
                        lines[cy] = current[:cx]
                        lines.insert(cy + 1, remainder)
                        cy += 1; cx = 0
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

            if key == "q" or key == ord("q"):
                break

            if self.screen == "home":
                self.handle_home_key(key)
            elif self.screen == "input":
                if isinstance(key, str):
                    key = self.collect_paste_buffer(stdscr, key)
                is_single = isinstance(key, str) and len(key) == 1
                if key == "r" or key == ord("r"):
                    self.start_run()
                elif is_single and key == "h":
                    self.go_home()
                elif is_single and key == "e":
                    self.reset()
                else:
                    self.handle_input_page_key(key)
            elif self.screen == "receipt":
                if isinstance(key, str):
                    key = self.collect_paste_buffer(stdscr, key)
                self.handle_receipt_key(key)
            else:
                if key == "h" or key == ord("h"):
                    self.go_home()
                elif key == "e" or key == ord("e"):
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



# ─── FINAL PATCH: canonical proof result must keep INCONCLUSIVE ──────────────
def canonical_outcome_from_proof(proof: dict) -> str:
    """Final TUI result reader: saved proof canonical result wins, including INCONCLUSIVE."""
    if not isinstance(proof, dict):
        return ""
    oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else {}
    verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
    dashboard = proof.get("dashboard") if isinstance(proof.get("dashboard"), dict) else {}

    def nested(obj, *path):
        cur = obj
        for key in path:
            if not isinstance(cur, dict):
                return ""
            cur = cur.get(key)
        return cur

    candidates = [
        nested(proof, "resolved_outcome", "outcome"),
        nested(oe, "resolved_outcome", "outcome"),
        proof.get("final_outcome"), proof.get("oracle_result"), proof.get("oracle_outcome"), proof.get("matched_outcome"),
        oe.get("final_outcome"), oe.get("oracle_result"), oe.get("oracle_outcome"), oe.get("matched_outcome"),
        dashboard.get("oracle_result"), dashboard.get("final_outcome"),
        verification.get("oracle_result"), verification.get("final_outcome"),
        nested(oe, "final_verdict", "matched_outcome"), nested(oe, "event_verdict", "verdict"),
        nested(oe, "arbitration", "chosen_outcome"),
    ]
    for val in candidates:
        s = str(val or "").strip()
        if not s or s.lower() in {"none", "null", "unknown"}:
            continue
        if s.lower() == "inconclusive":
            return "INCONCLUSIVE"
        if any(m in s.lower() for m in ["missing required", "outcome_not_found", "fetch_failed", "unsupported", "no validated content"]):
            return "INCONCLUSIVE"
        return s

    status = str(nested(oe, "arbitration", "status") or nested(proof, "arbitration", "status") or "").lower()
    pipe = str(nested(oe, "final_verdict", "pipeline") or nested(proof, "final_verdict", "pipeline") or "").lower()
    if "no_valid" in status or "inconclusive" in pipe or "outcome_not_found" in pipe:
        return "INCONCLUSIVE"
    return ""


if __name__ == "__main__":
    raise SystemExit(main())