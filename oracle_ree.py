#!/usr/bin/env python3
"""
OracleREE — Trustless oracle grounding for Gensyn Delphi settlement verification.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

# ─── Load .env.local ─────────────────────────────────────────────────────────

env_file = Path(__file__).parent / ".env.local"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())

# ─── Config ──────────────────────────────────────────────────────────────────

DELPHI_API_BASE  = "https://api.delphi.fyi"
COINGECKO_BASE   = "https://api.coingecko.com/api/v3"
PINATA_JWT       = os.environ.get("PINATA_JWT", "")
GROQ_API_KEY     = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY   = os.environ.get("TAVILY_API_KEY", "")
ORACLE_SEAL_URL  = os.environ.get("ORACLE_SEAL_URL", "https://oracle-seal.vercel.app")

DELPHI_TO_REE_MODEL: dict[str, str] = {
    "Claude Opus 4.7":                     "Qwen/Qwen3-0.6B",
    "Claude Opus 4.6":                     "Qwen/Qwen3-0.6B",
    "Claude Opus 4":                       "Qwen/Qwen3-0.6B",
    "Claude Sonnet 4.7":                   "Qwen/Qwen3-0.6B",
    "Claude Sonnet 4.6":                   "Qwen/Qwen3-0.6B",
    "Claude Sonnet 4":                     "Qwen/Qwen3-0.6B",
    "Claude Haiku 4.7":                    "Qwen/Qwen3-0.6B",
    "Claude Haiku 4.6":                    "Qwen/Qwen3-0.6B",
    "Claude Haiku 4":                      "Qwen/Qwen3-0.6B",
    "claude-opus":                         "Qwen/Qwen3-0.6B",
    "claude-sonnet":                       "Qwen/Qwen3-0.6B",
    "claude-haiku":                        "Qwen/Qwen3-0.6B",
    "ChatGPT 5.4":                         "Qwen/Qwen3-0.6B",
    "gpt-4":                               "Qwen/Qwen3-0.6B",
    "gpt-4o":                              "Qwen/Qwen3-0.6B",
    "gpt-4o-mini":                         "Qwen/Qwen3-0.6B",
    "gpt-3.5-turbo":                       "Qwen/Qwen3-0.6B",
    "Gemini 3":                            "Qwen/Qwen3-0.6B",
    "gemini-pro":                          "Qwen/Qwen3-0.6B",
    "gemini-flash":                        "Qwen/Qwen3-0.6B",
    "Grok 4.20":                           "Qwen/Qwen3-0.6B",
    "grok":                                "Qwen/Qwen3-0.6B",
    "Qwen/Qwen3-32B":                      "Qwen/Qwen3-0.6B",
    "Qwen/Qwen3-4B":                       "Qwen/Qwen3-4B",
    "Qwen/Qwen2.5-7B-Instruct":            "Qwen/Qwen2.5-7B-Instruct",
    "Qwen/Qwen2.5-7B":                     "Qwen/Qwen2.5-7B",
    "Qwen/Qwen2.5-3B-Instruct":            "Qwen/Qwen2.5-3B-Instruct",
    "Meta-Llama/Meta-Llama-3-8B-Instruct": "Meta-Llama/Meta-Llama-3-8B-Instruct",
    "Meta-Llama/Meta-Llama-3-8B":          "Meta-Llama/Meta-Llama-3-8B",
    "Meta-Llama/Llama-3.1-8B-Instruct":    "Meta-Llama/Llama-3.1-8B-Instruct",
    "Meta-Llama/Llama-3.1-8B":             "Meta-Llama/Llama-3.1-8B",
    "Meta-Llama/Llama-3.2-3B-Instruct":    "Meta-Llama/Llama-3.2-3B-Instruct",
    "Mistralai/Mistral-7B-Instruct-V0.2":  "Mistralai/Mistral-7B-Instruct-V0.2",
    "01-Ai/Yi-1.5-6B-Chat":               "01-Ai/Yi-1.5-6B-Chat",
    "Llm-Jp/Llm-Jp-3-3.7b-Instruct":      "Qwen/Qwen3-0.6B",
}

COINGECKO_IDS = {
    "BTC": "bitcoin",      "ETH": "ethereum",       "SOL": "solana",
    "BNB": "binancecoin",  "XRP": "ripple",          "ADA": "cardano",
    "AVAX": "avalanche-2", "DOGE": "dogecoin",       "HYPE": "hyperliquid",
    "WIF": "dogwifcoin",   "BONK": "bonk",           "ONDO": "ondo-finance",
    "SUI": "sui",          "APT": "aptos",            "ARB": "arbitrum",
    "OP": "optimism",      "NEAR": "near",            "PEPE": "pepe",
    "PENGU": "pudgy-penguins",
}

# ─── Helpers ─────────────────────────────────────────────────────────────────

def sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()

def normalize_prompt_for_hash(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()

def prompt_hash(text: str) -> str:
    return sha256(normalize_prompt_for_hash(text))

def extract_prompt_question(prompt: str) -> str:
    m = re.search(
        r"QUESTION:\s*(.+?)(?:\n\s*\n|DATA SOURCES:|SETTLEMENT RULES:|VALID OUTCOMES|$)",
        str(prompt or ""), re.I | re.S,
    )
    if m:
        return " ".join(m.group(1).split())
    return ""

def normalize_question_for_match(question: str) -> str:
    q = str(question or "").lower()
    q = re.sub(r"\$?\d+(?:,\d{3})*(?:\.\d+)?", "<num>", q)
    q = re.sub(r"[^a-z0-9<>]+", " ", q)
    return " ".join(q.split())

def analyze_prompt_integrity(user_prompt: str, official_prompt: str, official_question: str) -> dict:
    user_prompt = str(user_prompt or "").strip()
    official_prompt = str(official_prompt or "").strip()
    official_question = str(official_question or "").strip()
    is_user_prompt = bool(user_prompt)
    user_question = extract_prompt_question(user_prompt) if is_user_prompt else ""
    official_hash = prompt_hash(official_prompt) if official_prompt else ""
    user_hash = prompt_hash(user_prompt) if is_user_prompt else ""
    exact_prompt_match = bool(is_user_prompt and official_hash and user_hash and official_hash == user_hash)
    question_match = True
    if is_user_prompt and user_question and official_question:
        question_match = normalize_question_for_match(user_question) == normalize_question_for_match(official_question)
    if not is_user_prompt:
        mode = "CANONICAL_DELPHI_MARKET"; source = "Official Delphi Prompt"; warning = ""
    elif exact_prompt_match:
        mode = "CANONICAL_DELPHI_PROMPT"; source = "User Prompt Matches Official Delphi Prompt"; warning = ""
    elif question_match:
        mode = "MODIFIED_PROMPT_SIMULATION"; source = "User Provided Prompt"
        warning = "Pasted prompt differs from the official Delphi settlement prompt. Canonical verification disabled."
    else:
        mode = "CUSTOM_PROMPT_EXECUTION"; source = "User Provided Prompt"
        warning = "Pasted question differs from the official Delphi market question. This is not canonical market verification."
    return {
        "prompt_source": source, "verification_mode": mode,
        "prompt_match": "YES" if exact_prompt_match or not is_user_prompt else "NO",
        "question_match": "YES" if question_match else "NO",
        "official_prompt_hash": official_hash, "user_prompt_hash": user_hash,
        "official_question": official_question, "user_question": user_question, "warning": warning,
    }

def is_raw_settlement_prompt(value: str) -> bool:
    v = str(value or ""); u = v.upper()
    if re.search(r"0x[a-fA-F0-9]{40}", v): return False
    if re.search(r"https?://", v) and "delphi.fyi" in v.lower(): return False
    return len(v) > 80 and ("QUESTION:" in u or "SETTLEMENT RULES" in u or "VALID OUTCOMES" in u)

def extract_market_id(input_str: str) -> str:
    match = re.search(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])", input_str)
    if match:
        return match.group(0)
    uuid_match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", input_str)
    if uuid_match:
        uuid = uuid_match.group(0)
        print(f"[oracle] UUID detected: {uuid} — searching Delphi for 0x ID...")
        ox_id = resolve_uuid_to_market_id(uuid, input_str)
        if ox_id:
            return ox_id
        raise ValueError(f"Could not resolve UUID {uuid} to a Delphi market ID.\nPlease use the 0x market ID directly from the Delphi API.")
    if len(input_str) > 50 and GROQ_API_KEY:
        print("[oracle] Raw settlement prompt detected — searching Delphi markets...")
        ox_id = resolve_prompt_to_market_id(input_str)
        if ox_id:
            return ox_id
    raise ValueError(f"Could not extract market ID from: {input_str[:100]}")

def resolve_uuid_to_market_id(uuid: str, original_url: str) -> Optional[str]:
    api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
    if not api_key:
        return None
    try:
        for status in ["open", "settled", "expired"]:
            r = requests.get(f"https://api.delphi.fyi/markets", headers={"x-api-key": api_key},
                params={"limit": 100, "status": status}, timeout=15)
            if not r.ok: continue
            for m in r.json().get("markets", []):
                metadata_uri = m.get("metadataUri", "")
                if uuid.replace("-", "") in metadata_uri.replace("-", ""):
                    print(f"[oracle] Resolved UUID → {m.get('id')}")
                    return m.get("id")
        if original_url.startswith("http"):
            try:
                resp = requests.get(original_url, headers={"User-Agent": "Mozilla/5.0 OracleREE/1.0"}, timeout=10)
                title_match = re.search(r'<title[^>]*>([^<]+)</title>|"og:title"[^"]*"([^"]+)"', resp.text)
                if title_match:
                    question = (title_match.group(1) or title_match.group(2) or "").strip()
                    question = re.sub(r"\s*[-|]\s*Delphi.*$", "", question).strip()
                    if question and len(question) > 10:
                        print(f"[oracle] Found question from page: {question[:60]}")
                        ox_id = resolve_prompt_to_market_id(question)
                        if ox_id: return ox_id
            except Exception: pass
    except Exception as e:
        print(f"[oracle] UUID resolution failed: {e}")
    return None

def resolve_prompt_to_market_id(prompt: str) -> Optional[str]:
    api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
    if not api_key: return None
    try:
        question = ""
        for line in prompt.splitlines():
            if "QUESTION:" in line.upper():
                question = line.split(":", 1)[1].strip(); break
        if not question:
            for line in prompt.splitlines():
                if len(line.strip()) > 20:
                    question = line.strip()[:100]; break
        print(f"[oracle] Searching for market: {question[:60]}")
        for status in ["open", "settled", "expired"]:
            r = requests.get("https://api.delphi.fyi/markets", headers={"x-api-key": api_key},
                params={"limit": 100, "status": status}, timeout=15)
            if not r.ok: continue
            q_norm = normalize_question_for_match(question)
            for m in r.json().get("markets", []):
                mq_raw = m.get("metadata", {}).get("question", "")
                mq = mq_raw.lower()
                mq_norm = normalize_question_for_match(mq_raw)
                if (question.lower()[:40] in mq or mq[:40] in question.lower() or
                        (q_norm and (q_norm[:60] in mq_norm or mq_norm[:60] in q_norm))):
                    mid = m.get("id", "")
                    print(f"[oracle] Matched market: {mid} — {mq[:60]}")
                    return mid
    except Exception as e:
        print(f"[oracle] Prompt resolution failed: {e}")
    return None

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def resolve_ree_model(delphi_model: str, fallback: str = "Qwen/Qwen3-0.6B") -> str:
    if delphi_model in DELPHI_TO_REE_MODEL: return DELPHI_TO_REE_MODEL[delphi_model]
    if "/" in delphi_model: return delphi_model
    lower = delphi_model.lower()
    for key, val in DELPHI_TO_REE_MODEL.items():
        if key.lower() in lower or lower in key.lower(): return val
    return fallback

# ─── Delphi API ──────────────────────────────────────────────────────────────

def fetch_market(market_id: str) -> dict:
    api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
    if not api_key:
        raise ValueError("DELPHI_API_ACCESS_KEY not set.\nRun: python3 setup.py  to configure your environment.\nOr get a free key at: https://api-access.delphi.fyi/")
    url = f"{DELPHI_API_BASE}/markets/{market_id}"
    print(f"[oracle] Fetching market from Delphi: {url}")
    r = requests.get(url, headers={"x-api-key": api_key}, timeout=15)
    r.raise_for_status()
    return r.json()

# ─── OracleSeal integration ──────────────────────────────────────────────────

def get_oracle_seal_snapshot(market_id: str) -> Optional[dict]:
    """Check OracleSeal for a close-time capture of this market.

    OracleSeal watcher captures data sources at exact market close time
    and locks them to IPFS. Using this snapshot means we use the data
    that was available at close time — not a live fetch done later.
    This solves the Yahoo Finance timing problem where prices update after midnight.
    """
    try:
        r = requests.get(
            f"{ORACLE_SEAL_URL}/api/evidence/{market_id}",
            timeout=10,
        )
        if r.ok:
            data = r.json()
            snapshots = data.get("snapshots") or []
            if snapshots:
                snap = snapshots[0]
                record = snap if isinstance(snap, dict) else {}
                ipfs_cid = record.get("ipfs_cid")
                if ipfs_cid:
                    print(f"[oracle] OracleSeal snapshot found: {ipfs_cid}")
                    print(f"[oracle] Captured at: {record.get('captured_at', 'unknown')}")
                    return record
    except Exception as e:
        print(f"[oracle] OracleSeal lookup failed: {e}")
    return None


def push_to_oracle_seal(proof: dict) -> bool:
    """Auto-push REE proof to OracleSeal after run completes."""
    verification = proof.get("verification", {}) or {}
    receipt_hash = verification.get("ree_receipt_hash")
    if not receipt_hash:
        return False
    try:
        r = requests.post(
            f"{ORACLE_SEAL_URL}/api/receipts",
            json={
                "marketId": proof.get("market_id"),
                "receiptHash": receipt_hash,
                "ipfsCid": verification.get("ipfs_cid"),
                "combinedHash": verification.get("combined_hash"),
                "oracleHash": verification.get("oracle_evidence_hash"),
                "oracleSealIpfs": verification.get("oracle_seal_ipfs"),
            },
            timeout=10,
        )
        if r.ok:
            print("[oracle] Pushed proof to OracleSeal ✓")
            return True
        print(f"[oracle] OracleSeal push failed: {r.status_code}")
        return False
    except Exception as e:
        print(f"[oracle] OracleSeal push failed: {e}")
        return False

# ─── Oracle data fetchers ────────────────────────────────────────────────────

def resolve_coingecko_id(symbol: str) -> Optional[str]:
    hardcoded = COINGECKO_IDS.get(symbol.upper())
    if hardcoded: return hardcoded
    print(f"[oracle] CoinGecko search for unknown token: {symbol}")
    try:
        r = requests.get(f"{COINGECKO_BASE}/search", params={"query": symbol}, timeout=8)
        coins = r.json().get("coins", [])
        if not coins: return None
        matches = [c for c in coins if c["symbol"].upper() == symbol.upper()]
        if matches:
            matches.sort(key=lambda c: c.get("market_cap_rank") or 99999)
            print(f"[oracle] Resolved {symbol} → {matches[0]['id']}")
            return matches[0]["id"]
        return coins[0]["id"]
    except Exception as e:
        print(f"[oracle] CoinGecko search failed: {e}")
        return None

def fetch_crypto_price(symbol: str, date_str: str) -> dict:
    coin_id = resolve_coingecko_id(symbol)
    if not coin_id:
        return {"error": f"Unknown token: {symbol}", "symbol": symbol, "date": date_str}
    y, m, d = date_str.split("-")
    cg_date = f"{d}-{m}-{y}"
    print(f"[oracle] Fetching {symbol} ({coin_id}) price for {cg_date}")
    try:
        r = requests.get(f"{COINGECKO_BASE}/coins/{coin_id}/history",
            params={"date": cg_date, "localization": "false"}, timeout=10)
        r.raise_for_status()
        data = r.json(); md = data.get("market_data", {})
        price = md.get("current_price", {}).get("usd")
        high  = md.get("high_24h", {}).get("usd")
        low   = md.get("low_24h", {}).get("usd")
        print(f"[oracle] {symbol} on {date_str}: close=${price}")
        return {"symbol": symbol, "coin_gecko_id": coin_id, "date": date_str,
                "close_usd": price, "high_usd": high, "low_usd": low,
                "source": "CoinGecko", "fetched_at": now_iso()}
    except Exception as e:
        return {"error": str(e), "symbol": symbol, "date": date_str}

SOURCE_BASE_URLS = {
    "espn": "https://www.espn.com", "espncricinfo": "https://www.espncricinfo.com",
    "cricinfo": "https://www.espncricinfo.com", "x": "https://twitter.com",
    "twitter": "https://twitter.com", "coinmarketcap": "https://coinmarketcap.com",
    "cmc": "https://coinmarketcap.com", "coingecko": "https://www.coingecko.com",
    "uefa": "https://www.uefa.com", "nba": "https://www.nba.com",
    "nfl": "https://www.nfl.com", "ipl": "https://www.iplt20.com",
    "bbc": "https://www.bbc.com/sport", "sky sports": "https://www.skysports.com",
    "bloomberg": "https://www.bloomberg.com", "reuters": "https://www.reuters.com",
    "cnn": "https://www.cnn.com", "wikipedia": "https://www.wikipedia.org",
    "cricket": "https://www.espncricinfo.com", "psa": "https://www.psacard.com",
    "yahoo finance": "https://finance.yahoo.com", "yahoo": "https://finance.yahoo.com",
    "binance": "https://www.binance.com",
}

def resolve_source_url(source: str, question: str, close_time: str) -> str:
    src_lower = source.lower().strip()
    if src_lower.startswith("http"): return source
    base = None
    for key, url in SOURCE_BASE_URLS.items():
        if key in src_lower: base = url; break
    if not base: base = f"https://{source.strip()}"
    if not GROQ_API_KEY: return base
    print(f"[oracle] Resolving URL for '{source}' via Groq...")
    raw = call_groq(
        "You are a sports and news URL resolver. "
        "Return ONLY a single full URL, nothing else. No markdown, no explanation. "
        "The URL must be for the specific match/event/article, not a homepage. "
        "For ESPN soccer use format: https://www.espn.com/soccer/match/_/gameId/XXXXXXX "
        "For ESPN NBA use format: https://www.espn.com/nba/game/_/gameId/XXXXXXX "
        "For ESPN NFL use format: https://www.espn.com/nfl/game/_/gameId/XXXXXXX",
        f"Data source: {source}\nBase URL: {base}\nMarket question: {question}\n"
        f"Event date: {close_time[:10] if close_time else 'unknown'}\n"
        f"Find the exact ESPN/source URL for this specific match or event result."
    )
    if raw:
        resolved = raw.strip().strip('"').strip("'").split()[0]
        if resolved.startswith("http") and len(resolved) > 20:
            print(f"[oracle] Resolved '{source}' → {resolved}")
            return resolved
    return base

def fetch_web_snapshot(url: str, question: str = "", close_time: str = "") -> dict:
    resolved = resolve_source_url(url, question, close_time)
    print(f"[oracle] Fetching web source: {resolved}")
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Googlebot/2.1 (+http://www.google.com/bot.html)",
    ]
    last_error = None
    for ua in user_agents:
        try:
            r = requests.get(resolved, headers={"User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5", "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive", "Upgrade-Insecure-Requests": "1"}, timeout=15)
            if r.status_code == 200 and len(r.text) > 500:
                print(f"[oracle] Fetched {len(r.text)} chars (status {r.status_code})")
                import re as _re
                text = _re.sub(r"<script[^>]*>.*?</script>", " ", r.text, flags=_re.S)
                text = _re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=_re.S)
                text = _re.sub(r"<[^>]+>", " ", text)
                text = _re.sub(r"\s+", " ", text).strip()
                return {"url": resolved, "original_source": url, "status_code": r.status_code,
                        "text_snippet": text[:8000], "sha256": sha256(r.text), "fetched_at": now_iso()}
            else:
                last_error = f"HTTP {r.status_code}"
        except Exception as e:
            last_error = str(e); continue
    print(f"[oracle] All fetch attempts failed: {last_error}")
    return {"url": resolved, "original_source": url, "error": last_error, "fetched_at": now_iso()}

def call_groq(system_prompt: str, user_prompt: str) -> Optional[str]:
    if not GROQ_API_KEY:
        print("[oracle] GROQ_API_KEY not set — skipping AI grounding"); return None
    try:
        r = requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "temperature": 0.1, "max_tokens": 1000,
                "messages": [{"role": "system", "content": system_prompt},
                              {"role": "user", "content": user_prompt}]}, timeout=20)
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[oracle] Groq call failed: {e}"); return None

def search_tavily(question: str, close_time: str, data_sources: list = None) -> str:
    if not TAVILY_API_KEY: return ""
    date_str = close_time[:10] if close_time else ""
    query = f"{question} result {date_str}".strip()
    print(f"[oracle] Searching web via Tavily: {query}")
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY, "query": query,
            "search_depth": "basic", "max_results": 5, "include_answer": True}, timeout=15)
        data = r.json()
        answer = data.get("answer", "")
        parts = [f"SUMMARY: {answer}"] if answer else []
        for res in data.get("results", [])[:3]:
            parts.append(f"[{res.get('url','')}]\n{res.get('content','')[:800]}")
        combined = "\n\n".join(parts)
        if answer: print(f"[oracle] Tavily answer: {answer[:150]}")
        return combined
    except Exception as e:
        print(f"[oracle] Tavily search failed: {e}"); return ""

def classify_market(question: str, prompt_context: str) -> dict:
    system = ("You are an oracle classifier. Return ONLY valid JSON, no markdown.\n"
        "market_type: crypto_price | crypto_price_range | sports | politics | event | unknown\n"
        "is_price_based: true only if asking about a specific asset price vs threshold.\n"
        "asset: use the SHORT ticker symbol only (BTC, ETH, SOL, etc) never full name.")
    user = (f"Question: {question}\nPrompt: {prompt_context}\n"
        "Return JSON: market_type, is_price_based, asset (short ticker like ETH not Ethereum), "
        "threshold (number or null), capture_date (YYYY-MM-DD or null), confidence")
    raw = call_groq(system, user)
    if not raw:
        return {"market_type": "unknown", "is_price_based": False, "asset": None,
                "threshold": None, "capture_date": None, "confidence": "low"}
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(re.search(r"\{[\s\S]*\}", clean).group(0))
    except Exception:
        return {"market_type": "unknown", "is_price_based": False, "asset": None,
                "threshold": None, "capture_date": None, "confidence": "low"}

def pin_to_ipfs(data: dict, name: str) -> Optional[str]:
    if not PINATA_JWT:
        print("[oracle] PINATA_JWT not set — skipping IPFS pin"); return None
    try:
        r = requests.post("https://uploads.pinata.cloud/v3/files",
            headers={"Authorization": f"Bearer {PINATA_JWT}"},
            files={"file": (f"{name}.json", json.dumps(data), "application/json")}, timeout=30)
        cid = r.json()["data"]["cid"]
        print(f"[oracle] IPFS pinned: {cid}"); return cid
    except Exception as e:
        print(f"[oracle] IPFS pin failed: {e}"); return None

# ─── Oracle evidence builder ─────────────────────────────────────────────────

def build_oracle_evidence(market: dict) -> dict:
    meta           = market.get("metadata") or {}
    question       = meta.get("question", "")
    prompt_context = (meta.get("model") or {}).get("prompt_context", "")
    data_sources   = market.get("dataSources") or []
    resolves_at    = market.get("resolvesAt", "")
    close_date     = resolves_at[:10] if resolves_at else None

    print(f"\n[oracle] Market: {question}")
    print(f"[oracle] Close date: {close_date}")

    classification = classify_market(question, prompt_context)
    print(f"[oracle] Classification: {classification.get('market_type')} "
          f"(is_price_based={classification.get('is_price_based')})")

    evidence = {
        "market_id": market.get("id"), "market_question": question,
        "close_time": resolves_at, "captured_at": now_iso(),
        "classification": classification, "data_sources": [],
    }

    capture_date = classification.get("capture_date") or close_date

    if classification.get("is_price_based") and classification.get("asset"):
        asset = classification["asset"]
        price_data = fetch_crypto_price(asset, capture_date)
        evidence["price_data"] = price_data
        threshold = classification.get("threshold")
        if price_data.get("close_usd") is not None:
            close = price_data["close_usd"]
            outcomes = meta.get("outcomes") or []
            range_market = any("-" in str(o) or "Below" in str(o) or "+" in str(o) for o in outcomes)
            if range_market and outcomes:
                matched_outcome = None
                import re as _re
                def parse_value(s: str) -> float:
                    s = s.replace(",", "").replace("$", "").strip()
                    multiplier = 1
                    if s.lower().endswith("k"): multiplier = 1000; s = s[:-1]
                    elif s.lower().endswith("m"): multiplier = 1_000_000; s = s[:-1]
                    return float(s) * multiplier
                for outcome in outcomes:
                    o = str(outcome).strip()
                    o_clean = o.replace(",", "").replace("$", "")
                    below = _re.match(r"[Bb]elow\s*(.+)", o_clean)
                    if below:
                        try:
                            if close < parse_value(below.group(1)): matched_outcome = outcome; break
                        except Exception: pass
                        continue
                    above = _re.match(r"(.+?)\+\s*$", o_clean)
                    if above:
                        try:
                            if close >= parse_value(above.group(1)): matched_outcome = outcome; break
                        except Exception: pass
                        continue
                    rng = _re.match(r"(.+?)[^\d.k]+(.+)", o_clean)
                    if rng:
                        try:
                            lo = parse_value(rng.group(1)); hi = parse_value(rng.group(2))
                            if lo <= close < hi: matched_outcome = outcome; break
                        except Exception: pass
                if matched_outcome:
                    evidence["price_verdict"] = f"{asset} on {capture_date} was ${close:,.2f} → outcome: {matched_outcome}"
                else:
                    evidence["price_verdict"] = f"{asset} on {capture_date} was ${close:,.2f} → outcome: could not match to a bucket"
            elif threshold:
                met = close > threshold
                evidence["price_verdict"] = (
                    f"{asset} on {capture_date} was ${close:,.6f} — "
                    f"{'above' if met else 'below'} ${threshold:,.6f} → outcome: {'Yes' if met else 'No'}")
            print(f"[oracle] Price verdict: {evidence['price_verdict']}")
    else:
        # ── Check OracleSeal for close-time snapshot first ────────────────
        oracle_seal = get_oracle_seal_snapshot(market.get("id", ""))
        if oracle_seal and oracle_seal.get("data_sources"):
            print(f"[oracle] Using OracleSeal close-time snapshot ✓")
            evidence["data_sources"] = oracle_seal["data_sources"]
            evidence["oracle_seal_ipfs"] = oracle_seal.get("ipfs_cid")
            evidence["oracle_seal_captured_at"] = oracle_seal.get("captured_at")
        else:
            # ── Fallback: live fetch ──────────────────────────────────────
            print(f"[oracle] No OracleSeal snapshot — fetching live sources")
            for src_url in data_sources[:3]:
                evidence["data_sources"].append(fetch_web_snapshot(src_url, question, resolves_at))

        web_context = "\n\n---\n\n".join(
            f"[{s['url']}]\n{s.get('text_snippet', '')}"
            for s in evidence["data_sources"] if "text_snippet" in s)

        is_closed = resolves_at and datetime.fromisoformat(
            resolves_at.replace("Z", "+00:00")) < datetime.now(timezone.utc)

        if is_closed:
            outcomes = meta.get("outcomes") or []
            web_ok = bool(web_context.strip())
            if not web_ok and TAVILY_API_KEY:
                tavily_result = search_tavily(question, resolves_at, data_sources)
                if tavily_result:
                    web_context = tavily_result; web_ok = True
                    print("[oracle] Using Tavily live search as web context")
            if web_ok:
                system_msg = ("You are OracleREE's fact engine. Use the web evidence provided. "
                    "Return ONLY valid JSON: verdict, matchedOutcome, confidence, explanation.")
                user_msg = (f'Question: "{question}"\nValid outcomes: {", ".join(outcomes)}\n'
                    f"Close time: {resolves_at}\n\nWEB EVIDENCE:\n{web_context[:4000]}")
            else:
                print("[oracle] Web sources blocked/failed — using Groq knowledge directly")
                source_hints = ""
                for src in evidence["data_sources"]:
                    if src.get("url"):
                        source_hints += f"  Source: {src.get('original_source','')} → resolved to {src.get('url')}\n"
                system_msg = ("You are a prediction market settlement judge. "
                    "Answer based on your knowledge of real world events. "
                    "IMPORTANT: Only output a matched outcome if you are CERTAIN of the result. "
                    "If the event is after your knowledge cutoff or you are not sure, "
                    "set matchedOutcome to null and confidence to 0. "
                    "Never guess. Never use fallback rules like Draw for unknown results. "
                    "Return ONLY valid JSON: verdict, matchedOutcome, confidence, explanation.")
                user_msg = (f'Question: "{question}"\nValid outcomes: {", ".join(outcomes)}\n'
                    f"Event date: {resolves_at[:10]}\nData sources attempted:\n{source_hints}"
                    f"Settlement rules (DO NOT use fallback rules if you don't know the result):\n{prompt_context[:400]}\n"
                    f"Do you know the actual result of this specific event from your training data? "
                    f"If yes, return the correct matchedOutcome with high confidence. "
                    f"If no, return matchedOutcome: null and confidence: 0.")
            raw = call_groq(system_msg, user_msg)
            if raw:
                try:
                    clean = raw.replace("```json", "").replace("```", "").strip()
                    parsed = json.loads(re.search(r"\{[\s\S]*\}", clean).group(0))
                    matched = str(parsed.get("matchedOutcome") or "").strip()
                    if matched and matched.lower() not in {"none", "null", "unknown"}:
                        evidence["event_verdict"] = parsed
                        print(f"[oracle] Event verdict: {parsed.get('matchedOutcome')} (confidence: {parsed.get('confidence')})")
                    else:
                        print(f"[oracle] Groq could not extract from web content — trying Tavily")
                        if TAVILY_API_KEY:
                            tavily_result = search_tavily(question, resolves_at, data_sources)
                            if tavily_result:
                                raw2 = call_groq(
                                    "You are OracleREE's fact engine. Use the web evidence provided. "
                                    "Return ONLY valid JSON: verdict, matchedOutcome, confidence, explanation.",
                                    f'Question: "{question}"\nValid outcomes: {", ".join(outcomes)}\n'
                                    f"\nWEB EVIDENCE:\n{tavily_result[:4000]}")
                                if raw2:
                                    clean2 = raw2.replace("```json", "").replace("```", "").strip()
                                    parsed2 = json.loads(re.search(r"\{[\s\S]*\}", clean2).group(0))
                                    matched2 = str(parsed2.get("matchedOutcome") or "").strip()
                                    if matched2 and matched2.lower() not in {"none", "null", "unknown"}:
                                        evidence["event_verdict"] = parsed2
                                        print(f"[oracle] Event verdict (Tavily): {matched2} (confidence: {parsed2.get('confidence')})")
                                        evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
                                        evidence["ipfs_cid"] = pin_to_ipfs(evidence, f"oracle-ree-{market.get('id', 'unknown')[:10]}")
                                        return evidence
                        evidence["event_verdict"] = {"verdict": None, "matchedOutcome": None,
                            "confidence": 0, "explanation": "Could not determine outcome from available evidence."}
                except Exception: pass

    evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
    evidence["ipfs_cid"] = pin_to_ipfs(evidence, f"oracle-ree-{market.get('id', 'unknown')[:10]}")
    return evidence

# ─── Prompt builder ──────────────────────────────────────────────────────────

def build_oracle_prompt(original_prompt: str, evidence: dict) -> str:
    lines = [
        "═══════════════════════════════════════════════════",
        "ORACLEREE VERIFIED DATA BLOCK",
        "═══════════════════════════════════════════════════",
        f"Market:      {evidence['market_question']}",
        f"Captured at: {evidence['captured_at']}",
        f"Close time:  {evidence['close_time']}",
        f"Market type: {evidence['classification'].get('market_type', 'unknown')}",
        f"Confidence:  {evidence['classification'].get('confidence', 'unknown')}",
    ]
    if evidence.get("oracle_seal_ipfs"):
        lines += ["", "ORACLE SEAL SNAPSHOT (close-time capture):",
            f"  IPFS CID:    {evidence['oracle_seal_ipfs']}",
            f"  Captured at: {evidence.get('oracle_seal_captured_at', 'unknown')}"]
    if evidence.get("price_data"):
        p = evidence["price_data"]
        lines += ["", "PRICE DATA (CoinGecko):",
            f"  Asset:  {p.get('symbol')}", f"  Date:   {p.get('date')}",
            f"  Close:  ${p.get('close_usd')}", f"  High:   ${p.get('high_usd')}",
            f"  Low:    ${p.get('low_usd')}", f"  Source: {p.get('source')}"]
        if evidence.get("price_verdict"):
            lines += ["", f"VERDICT: {evidence['price_verdict']}"]
    if evidence.get("event_verdict"):
        v = evidence["event_verdict"]
        lines += ["", "EVENT VERDICT (Groq Llama-3.3-70B + web evidence):",
            f"  Result:      {v.get('verdict')}", f"  Outcome:     {v.get('matchedOutcome')}",
            f"  Confidence:  {v.get('confidence')}", f"  Explanation: {v.get('explanation')}"]
    if evidence.get("data_sources"):
        lines += ["", "WEB SOURCES CAPTURED:"]
        for src in evidence["data_sources"]:
            lines.append(f"  {src.get('url')} [HTTP {src.get('status_code', '?')}]")
            lines.append(f"  SHA-256: {src.get('sha256', 'N/A')}")
    lines += ["", "INTEGRITY:",
        f"  Evidence hash: {evidence.get('evidence_hash', 'N/A')}",
        f"  IPFS CID:      {evidence.get('ipfs_cid', 'Not pinned')}",
        "═══════════════════════════════════════════════════",
        "END ORACLEREE VERIFIED DATA BLOCK",
        "═══════════════════════════════════════════════════",
        "", "ORIGINAL SETTLEMENT PROMPT:",
        "─────────────────────────────────────────────────",
        original_prompt]
    return "\n".join(lines)

# ─── REE runner ──────────────────────────────────────────────────────────────

def _all_receipt_files() -> list[Path]:
    return [Path(p) for p in glob.glob(str(Path.home() / ".cache/gensyn/**/receipt_*.json"), recursive=True)]

def _safe_receipt_hash(receipt_path: Path) -> str:
    try:
        with open(receipt_path, encoding="utf-8") as f:
            receipt = json.load(f)
        return str(receipt.get("hashes", {}).get("receipt_hash") or "")
    except Exception: return ""

def _safe_receipt_prompt_hash(receipt_path: Path) -> str:
    try:
        with open(receipt_path, encoding="utf-8") as f:
            receipt = json.load(f)
        return str(receipt.get("input", {}).get("prompt_hash") or "")
    except Exception: return ""

def _find_new_receipt(start_ts: float, expected_prompt_hash: str = "") -> Optional[Path]:
    candidates: list[Path] = []
    for rp in _all_receipt_files():
        try:
            if rp.stat().st_mtime >= start_ts - 2: candidates.append(rp)
        except OSError: continue
    candidates.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    if expected_prompt_hash:
        for rp in candidates:
            if _safe_receipt_prompt_hash(rp) == expected_prompt_hash: return rp
    return candidates[0] if candidates else None

def run_ree(prompt: str, model_name: str = "Qwen/Qwen3-0.6B", max_new_tokens: int = 200) -> Optional[Path]:
    ree_dir = Path(__file__).parent
    ree_sh  = ree_dir / "ree.sh"
    if not ree_sh.exists():
        print(f"[ree] ERROR: ree.sh not found at {ree_sh}"); return None
    prompt_file = ree_dir / "oracle_prompt.jsonl"
    with open(prompt_file, "w", encoding="utf-8") as f:
        json.dump({"prompt": prompt}, f, ensure_ascii=False); f.write("\n")
    prompt_file.chmod(0o644)
    expected_prompt_hash = sha256(prompt)
    started = time.time()
    print(f"\n[ree] Running REE with model={model_name}")
    print(f"[ree] Prompt length: {len(prompt)} chars")
    print(f"[ree] Expected prompt hash: {expected_prompt_hash}")
    try:
        result = subprocess.run(
            ["bash", str(ree_sh), "--model-name", model_name,
             "--prompt-file", str(prompt_file), "--max-new-tokens", str(max_new_tokens)],
            cwd=str(ree_dir), capture_output=True, text=True, timeout=900)
        stdout = result.stdout or ""; stderr = result.stderr or ""
        combined_out = stdout + ("\n" + stderr if stderr else "")
        if result.returncode != 0:
            print(f"[ree] ERROR: REE failed (exit {result.returncode})")
            tail = combined_out[-3000:].strip()
            if tail: print(tail)
            return None
        print("[ree] REE process exited successfully")
        receipt_path: Optional[Path] = None
        for line in combined_out.splitlines():
            if "receipt" not in line.lower(): continue
            m = re.search(r"(/[^\s]+receipt_[0-9_]+\.json)", line)
            if m:
                candidate = Path(m.group(1))
                if candidate.exists(): receipt_path = candidate; break
        if not receipt_path:
            receipt_path = _find_new_receipt(started, expected_prompt_hash)
        if not receipt_path:
            print("[ree] ERROR: REE exited successfully but no new receipt_*.json was found")
            print("[ree] Checked: ~/.cache/gensyn/**/receipt_*.json")
            return None
        actual_prompt_hash = _safe_receipt_prompt_hash(receipt_path)
        if actual_prompt_hash and actual_prompt_hash != expected_prompt_hash:
            print(f"[ree] WARNING: receipt prompt hash differs from expected prompt hash")
            print(f"[ree] Expected: {expected_prompt_hash}"); print(f"[ree] Receipt:  {actual_prompt_hash}")
        receipt_hash = _safe_receipt_hash(receipt_path)
        print(f"[ree] Receipt: {receipt_path}")
        if receipt_hash: print(f"[ree] Receipt hash: {receipt_hash}")
        print("[ree] REE completed successfully")
        return receipt_path
    except subprocess.TimeoutExpired:
        print("[ree] ERROR: REE timed out after 900s"); return None
    finally:
        prompt_file.unlink(missing_ok=True)

# ─── Combined proof builder ──────────────────────────────────────────────────

def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                          prompt_integrity: Optional[dict] = None) -> dict:
    proof = {
        "version": "1.0.0", "tool": "OracleREE", "market_id": market_id,
        "created_at": now_iso(), "oracle_evidence": evidence, "ree_receipt": None,
        "prompt_integrity": prompt_integrity or {},
        "verification": {
            "oracle_evidence_hash": evidence.get("evidence_hash"),
            "ipfs_cid": evidence.get("ipfs_cid"),
            "oracle_seal_ipfs": evidence.get("oracle_seal_ipfs"),
            "ree_receipt_hash": None, "ree_receipt_path": None, "combined_hash": None,
        },
    }
    if receipt_path and receipt_path.exists():
        with open(receipt_path) as f:
            receipt = json.load(f)
        proof["ree_receipt"] = receipt
        proof["verification"]["ree_receipt_path"] = str(receipt_path)
        proof["verification"]["ree_receipt_hash"] = receipt.get("hashes", {}).get("receipt_hash")
        proof["verification"]["prompt_hash"] = receipt.get("input", {}).get("prompt_hash")
        combined = sha256(str(evidence.get("evidence_hash")) + str(receipt.get("hashes", {}).get("receipt_hash")))
        proof["verification"]["combined_hash"] = combined
        print(f"\n[proof] Combined hash:        {combined}")
        print(f"[proof] Oracle evidence hash: {evidence.get('evidence_hash')}")
        print(f"[proof] REE receipt path:     {receipt_path}")
        print(f"[proof] REE receipt hash:     {receipt.get('hashes', {}).get('receipt_hash')}")
    return proof

# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="OracleREE — Trustless oracle grounding for Delphi settlement")
    parser.add_argument("--market", "-m", help="Delphi market URL or market ID (0x...)", default=None)
    parser.add_argument("--model", help="Override REE model (HuggingFace ID)", default=None)
    parser.add_argument("--max-tokens", type=int, default=200)
    parser.add_argument("--oracle-only", action="store_true", help="Only fetch oracle evidence, skip REE inference")
    parser.add_argument("--output", "-o", default=None)
    args = parser.parse_args()

    market_input = args.market
    if not market_input:
        print("OracleREE — Trustless oracle grounding for Delphi settlement")
        print("=" * 60)
        market_input = input("Paste Delphi market URL or ID: ").strip()
    if not market_input:
        print("Error: market URL or ID required"); return 1

    try:
        market_id = extract_market_id(market_input)
        print(f"\n[oracle] Market ID: {market_id}")
    except ValueError as e:
        print(f"Error: {e}"); return 1

    try:
        market = fetch_market(market_id)
    except Exception as e:
        print(f"Error fetching market: {e}"); return 1

    meta           = market.get("metadata") or {}
    question       = meta.get("question", "Unknown")
    prompt_context = (meta.get("model") or {}).get("prompt_context", question)
    delphi_model   = (meta.get("model") or {}).get("model_identifier", "")
    ree_model      = args.model or resolve_ree_model(delphi_model)

    raw_prompt_mode = is_raw_settlement_prompt(market_input)
    provided_prompt = market_input.strip() if raw_prompt_mode else ""
    prompt_integrity = analyze_prompt_integrity(provided_prompt, prompt_context, question)

    prompt_for_execution = (
        provided_prompt if raw_prompt_mode and prompt_integrity.get("prompt_match") != "YES"
        else prompt_context)

    print(f"[oracle] Question:     {question}")
    print(f"[oracle] Delphi model: {delphi_model}")
    print(f"[oracle] REE model:    {ree_model}")
    print(f"[oracle] Prompt source: {prompt_integrity.get('prompt_source')}")
    print(f"[oracle] Prompt match:  {prompt_integrity.get('prompt_match')}")
    print(f"[oracle] Question match:{prompt_integrity.get('question_match')}")
    print(f"[oracle] Verification mode: {prompt_integrity.get('verification_mode')}")
    if prompt_integrity.get("official_prompt_hash"):
        print(f"[oracle] Official prompt hash: {prompt_integrity.get('official_prompt_hash')}")
    if prompt_integrity.get("user_prompt_hash"):
        print(f"[oracle] Provided prompt hash: {prompt_integrity.get('user_prompt_hash')}")
    if prompt_integrity.get("warning"):
        print(f"[oracle] Prompt warning: {prompt_integrity.get('warning')}")

    evidence      = build_oracle_evidence(market)
    oracle_prompt = build_oracle_prompt(prompt_for_execution, evidence)

    print(f"\n[oracle] Oracle prompt length: {len(oracle_prompt)} chars")
    print(f"[oracle] Evidence hash: {evidence.get('evidence_hash')}")
    if evidence.get("ipfs_cid"):
        print(f"[oracle] IPFS CID: {evidence['ipfs_cid']}")
    if evidence.get("oracle_seal_ipfs"):
        print(f"[oracle] OracleSeal IPFS: {evidence['oracle_seal_ipfs']} (close-time snapshot)")

    receipt_path = None
    if not args.oracle_only:
        receipt_path = run_ree(prompt=oracle_prompt, model_name=ree_model, max_new_tokens=args.max_tokens)
        if not receipt_path:
            print("\n[ree] ERROR: No REE receipt was generated for this run.")
            print("[ree] Oracle evidence will be saved, but this is NOT a full REE proof.")
    else:
        print("\n[ree] Skipping REE inference (--oracle-only)")

    proof = build_combined_proof(market_id, evidence, receipt_path, prompt_integrity)

    output_path = (args.output or
        f"oracle_proof_{market_id[:10]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(output_path, "w") as f:
        json.dump(proof, f, indent=2)
    print(f"\n[proof] Saved to: {output_path}")

    print("\n" + "=" * 60)
    print("ORACLEREE SUMMARY")
    print("=" * 60)
    print(f"Market:        {question}")
    print(f"Market ID:     {market_id}")
    print(f"Delphi model:  {delphi_model}")
    print(f"REE model:     {ree_model}")
    print(f"Oracle hash:   {evidence.get('evidence_hash')}")
    print(f"IPFS CID:      {evidence.get('ipfs_cid', 'Not pinned')}")
    if evidence.get("oracle_seal_ipfs"):
        print(f"OracleSeal:    {evidence['oracle_seal_ipfs']} (close-time snapshot)")
    print(f"Prompt source: {prompt_integrity.get('prompt_source')}")
    print(f"Prompt match:  {prompt_integrity.get('prompt_match')}")
    print(f"Mode:          {prompt_integrity.get('verification_mode')}")
    if prompt_integrity.get("warning"):
        print(f"Warning:       {prompt_integrity.get('warning')}")
    if proof["verification"].get("ree_receipt_hash"):
        print(f"REE receipt:   {proof['verification']['ree_receipt_hash']}")
        print(f"Receipt path:  {proof['verification'].get('ree_receipt_path')}")
        print(f"Combined hash: {proof['verification']['combined_hash']}")
        print("\n✓ Oracle data + REE execution cryptographically linked")
        print(f"✓ Verify: cd {Path(__file__).parent} && python3 ree.py verify --receipt-path {proof['verification'].get('ree_receipt_path')}")
    else:
        print("\n⚠ Oracle evidence captured, but REE receipt is missing")
        print("  This is NOT a full REE proof yet.")
    print("=" * 60)

    # Clean up old oracle proof files — keep only 5 most recent per market
    try:
        proof_dir = Path(__file__).parent
        all_proofs = sorted(proof_dir.glob("oracle_proof_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        seen: dict = {}
        for p in all_proofs:
            key = p.name[:24]; seen.setdefault(key, []).append(p)
        for key, files in seen.items():
            for old_file in files[5:]: old_file.unlink(missing_ok=True)
    except Exception: pass

    # Auto-push proof to OracleSeal
    if proof["verification"].get("ree_receipt_hash"):
        push_to_oracle_seal(proof)

    if args.oracle_only: return 0
    return 0 if proof["verification"].get("ree_receipt_hash") else 2


if __name__ == "__main__":
    raise SystemExit(main())