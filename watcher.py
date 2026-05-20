#!/usr/bin/env python3
"""
OracleSeal Market Watcher
Runs every 5 minutes via GitHub Actions.
Finds markets closing soon, waits for exact close time,
captures data sources and locks to IPFS.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

DELPHI_API_KEY = os.environ.get("DELPHI_API_ACCESS_KEY", "")
PINATA_JWT     = os.environ.get("PINATA_JWT", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
DELPHI_API     = "https://api.delphi.fyi"
CAPTURES_FILE  = "oracle_seal_captures.json"

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_iso() -> str:
    return now_utc().isoformat()

def sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()

def load_captures() -> dict:
    try:
        if Path(CAPTURES_FILE).exists():
            return json.loads(Path(CAPTURES_FILE).read_text())
    except Exception:
        pass
    return {}

def save_captures(captures: dict) -> None:
    Path(CAPTURES_FILE).write_text(json.dumps(captures, indent=2))

def fetch_open_markets() -> list:
    if not DELPHI_API_KEY:
        print("[watcher] No DELPHI_API_ACCESS_KEY set")
        return []
    try:
        r = requests.get(
            f"{DELPHI_API}/markets",
            headers={"x-api-key": DELPHI_API_KEY},
            params={"limit": 100, "status": "open"},
            timeout=15,
        )
        if r.ok:
            return r.json().get("markets", [])
    except Exception as e:
        print(f"[watcher] Failed to fetch markets: {e}")
    return []

def parse_close_time(market: dict) -> Optional[datetime]:
    raw = market.get("resolvesAt") or market.get("closeTime") or ""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None

def fetch_source(url: str) -> dict:
    """Fetch a data source URL and return snapshot."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0"},
            timeout=15,
        )
        import re
        text = re.sub(r"<script[^>]*>.*?</script>", " ", r.text, flags=re.S)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return {
            "url": url,
            "status_code": r.status_code,
            "text_snippet": text[:5000],
            "sha256": sha256(r.text),
            "fetched_at": now_iso(),
        }
    except Exception as e:
        return {"url": url, "error": str(e), "fetched_at": now_iso()}

def search_tavily(question: str, close_time: str) -> str:
    if not TAVILY_API_KEY:
        return ""
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": f"{question} result {close_time[:10]}",
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": True,
            },
            timeout=15,
        )
        data = r.json()
        answer = data.get("answer", "")
        results = data.get("results", [])
        parts = [f"SUMMARY: {answer}"] if answer else []
        for res in results[:3]:
            parts.append(f"[{res.get('url','')}]\n{res.get('content','')[:800]}")
        return "\n\n".join(parts)
    except Exception as e:
        print(f"[watcher] Tavily failed: {e}")
        return ""

def pin_to_ipfs(data: dict, name: str) -> Optional[str]:
    if not PINATA_JWT:
        print("[watcher] No PINATA_JWT — skipping IPFS pin")
        return None
    try:
        r = requests.post(
            "https://uploads.pinata.cloud/v3/files",
            headers={"Authorization": f"Bearer {PINATA_JWT}"},
            files={"file": (f"{name}.json", json.dumps(data), "application/json")},
            timeout=30,
        )
        cid = r.json()["data"]["cid"]
        print(f"[watcher] IPFS pinned: {cid}")
        return cid
    except Exception as e:
        print(f"[watcher] IPFS pin failed: {e}")
        return None

def capture_market(market: dict, close_time: datetime) -> dict:
    """Capture all data sources for a market at close time."""
    meta = market.get("metadata") or {}
    question = meta.get("question", "Unknown")
    market_id = market.get("id", "unknown")
    data_sources = market.get("dataSources") or []

    print(f"[watcher] Capturing market: {question[:60]}")
    print(f"[watcher] Close time: {close_time.isoformat()}")

    snapshots = []
    for url in data_sources[:3]:
        print(f"[watcher] Fetching: {url}")
        snap = fetch_source(url)
        snapshots.append(snap)

    # Try Tavily for additional evidence
    tavily_context = search_tavily(question, close_time.isoformat())

    capture = {
        "market_id": market_id,
        "market_question": question,
        "close_time": close_time.isoformat(),
        "captured_at": now_iso(),
        "data_sources": snapshots,
        "tavily_context": tavily_context,
        "capture_hash": sha256(json.dumps(snapshots, sort_keys=True)),
    }

    # Pin to IPFS
    cid = pin_to_ipfs(capture, f"oracle-seal-{market_id[:10]}")
    capture["ipfs_cid"] = cid

    print(f"[watcher] Captured {market_id[:10]} → IPFS: {cid}")
    return capture

def main():
    print(f"[watcher] Starting at {now_iso()}")
    captures = load_captures()
    markets = fetch_open_markets()
    print(f"[watcher] Found {len(markets)} open markets")

    now = now_utc()
    window_end = now + timedelta(minutes=6)  # capture markets closing in next 6 mins

    markets_to_capture = []
    for market in markets:
        market_id = market.get("id", "")
        close_time = parse_close_time(market)
        if not close_time:
            continue

        # Already captured
        if market_id in captures:
            print(f"[watcher] Already captured: {market_id[:10]}")
            continue

        # Closing within our window
        if now <= close_time <= window_end:
            markets_to_capture.append((market, close_time))
            print(f"[watcher] Market closing soon: {market.get('metadata',{}).get('question','')[:50]}")
            print(f"[watcher] Close time: {close_time.isoformat()}")

    if not markets_to_capture:
        print("[watcher] No markets closing in this window")
        return

    for market, close_time in markets_to_capture:
        # Wait until exact close time
        wait_secs = (close_time - now_utc()).total_seconds()
        if wait_secs > 0:
            print(f"[watcher] Waiting {wait_secs:.1f}s for exact close time...")
            time.sleep(wait_secs)

        # Capture immediately at close time
        capture = capture_market(market, close_time)
        captures[market.get("id")] = capture

    save_captures(captures)
    print(f"[watcher] Done. Total captures: {len(captures)}")

if __name__ == "__main__":
    main()
