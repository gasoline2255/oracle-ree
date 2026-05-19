#!/usr/bin/env python3
"""
OracleREE — Trustless oracle grounding for Gensyn Delphi settlement verification.

Extends Gensyn REE with verified oracle data injection:
1. Fetch market settlement prompt from Delphi API
2. Fetch verified oracle data (CoinGecko price / web scrape / Groq verdict)
3. Inject oracle data into prompt before REE runs
4. REE generates receipt — prompt_hash covers oracle data
5. Pin evidence to IPFS — immutable, timestamped
6. Combined proof: oracle evidence hash + REE receipt hash

Usage:
    python3 oracle_ree.py
    python3 oracle_ree.py --market 0xabc123...
    python3 oracle_ree.py --market https://app.delphi.fyi/markets/0xabc123...
    python3 oracle_ree.py --market 0xabc123... --oracle-only
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

DELPHI_API_BASE = "https://api.delphi.fyi"
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
PINATA_JWT      = os.environ.get("PINATA_JWT", "")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")

# Non-verifiable models (Claude/GPT/Gemini/Grok) → substitute with small open model
# Verifiable HuggingFace models → used as-is
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
    # Verifiable open-source models
    "Qwen/Qwen3-32B":                      "Qwen/Qwen3-32B",
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

def extract_market_id(input_str: str) -> str:
    # 0x hex format — exactly 40 hex chars
    match = re.search(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])", input_str)
    if match:
        return match.group(0)

    # Delphi URL with UUID — search all markets to find the 0x ID
    uuid_match = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        input_str
    )
    if uuid_match:
        uuid = uuid_match.group(0)
        print(f"[oracle] UUID detected: {uuid} — searching Delphi for 0x ID...")
        ox_id = resolve_uuid_to_market_id(uuid, input_str)
        if ox_id:
            return ox_id
        raise ValueError(
            f"Could not resolve UUID {uuid} to a Delphi market ID.\n"
            f"Please use the 0x market ID directly from the Delphi API."
        )

    # Raw settlement prompt — use Groq to identify the market
    if len(input_str) > 50 and GROQ_API_KEY:
        print("[oracle] Raw settlement prompt detected — searching Delphi markets...")
        ox_id = resolve_prompt_to_market_id(input_str)
        if ox_id:
            return ox_id

    raise ValueError(f"Could not extract market ID from: {input_str[:100]}")


def resolve_uuid_to_market_id(uuid: str, original_url: str) -> Optional[str]:
    """Search Delphi API markets to find the 0x ID matching a UUID URL."""
    api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
    if not api_key:
        return None
    try:
        # Search across all statuses
        for status in ["open", "settled", "expired"]:
            r = requests.get(
                f"https://api.delphi.fyi/markets",
                headers={"x-api-key": api_key},
                params={"limit": 100, "status": status},
                timeout=15,
            )
            if not r.ok:
                continue
            markets = r.json().get("markets", [])
            # Try to match by fetching the Delphi page and comparing
            for m in markets:
                mid = m.get("id", "")
                # Check if the market metadata URL contains our UUID
                metadata_uri = m.get("metadataUri", "")
                if uuid.replace("-", "") in metadata_uri.replace("-", ""):
                    print(f"[oracle] Resolved UUID → {mid}")
                    return mid
        # Fallback: fetch the Delphi page and look for market question,
        # then search the API for that question
        if original_url.startswith("http"):
            try:
                resp = requests.get(
                    original_url,
                    headers={"User-Agent": "Mozilla/5.0 OracleREE/1.0"},
                    timeout=10,
                )
                # Extract question from page title or og:title meta tag
                title_match = re.search(
                    r'<title[^>]*>([^<]+)</title>|"og:title"[^"]*"([^"]+)"',
                    resp.text
                )
                if title_match:
                    question = (title_match.group(1) or title_match.group(2) or "").strip()
                    question = re.sub(r"\s*[-|]\s*Delphi.*$", "", question).strip()
                    if question and len(question) > 10:
                        print(f"[oracle] Found question from page: {question[:60]}")
                        ox_id = resolve_prompt_to_market_id(question)
                        if ox_id:
                            return ox_id
            except Exception:
                pass
    except Exception as e:
        print(f"[oracle] UUID resolution failed: {e}")
    return None


def resolve_prompt_to_market_id(prompt: str) -> Optional[str]:
    """Search Delphi markets to find one matching a raw settlement prompt."""
    api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
    if not api_key:
        return None
    try:
        # Extract question from prompt
        question = ""
        for line in prompt.splitlines():
            if "QUESTION:" in line.upper():
                question = line.split(":", 1)[1].strip()
                break
        if not question:
            # Use first meaningful line
            for line in prompt.splitlines():
                if len(line.strip()) > 20:
                    question = line.strip()[:100]
                    break

        print(f"[oracle] Searching for market: {question[:60]}")

        for status in ["open", "settled", "expired"]:
            r = requests.get(
                "https://api.delphi.fyi/markets",
                headers={"x-api-key": api_key},
                params={"limit": 100, "status": status},
                timeout=15,
            )
            if not r.ok:
                continue
            markets = r.json().get("markets", [])
            for m in markets:
                mq = m.get("metadata", {}).get("question", "").lower()
                if question.lower()[:40] in mq or mq[:40] in question.lower():
                    mid = m.get("id", "")
                    print(f"[oracle] Matched market: {mid} — {mq[:60]}")
                    return mid
    except Exception as e:
        print(f"[oracle] Prompt resolution failed: {e}")
    return None

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def resolve_ree_model(delphi_model: str, fallback: str = "Qwen/Qwen3-0.6B") -> str:
    if delphi_model in DELPHI_TO_REE_MODEL:
        return DELPHI_TO_REE_MODEL[delphi_model]
    if "/" in delphi_model:
        return delphi_model
    lower = delphi_model.lower()
    for key, val in DELPHI_TO_REE_MODEL.items():
        if key.lower() in lower or lower in key.lower():
            return val
    return fallback

# ─── Delphi API ──────────────────────────────────────────────────────────────

def fetch_market(market_id: str) -> dict:
    api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
    if not api_key:
        raise ValueError(
            "DELPHI_API_ACCESS_KEY not set.\n"
            "Run: python3 setup.py  to configure your environment.\n"
            "Or get a free key at: https://api-access.delphi.fyi/"
        )
    headers = {"x-api-key": api_key}
    url = f"{DELPHI_API_BASE}/markets/{market_id}"
    print(f"[oracle] Fetching market from Delphi: {url}")
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()

# ─── Oracle data fetchers ────────────────────────────────────────────────────

def resolve_coingecko_id(symbol: str) -> Optional[str]:
    hardcoded = COINGECKO_IDS.get(symbol.upper())
    if hardcoded:
        return hardcoded
    print(f"[oracle] CoinGecko search for unknown token: {symbol}")
    try:
        r = requests.get(
            f"{COINGECKO_BASE}/search",
            params={"query": symbol},
            timeout=8,
        )
        coins = r.json().get("coins", [])
        if not coins:
            return None
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
        r = requests.get(
            f"{COINGECKO_BASE}/coins/{coin_id}/history",
            params={"date": cg_date, "localization": "false"},
            timeout=10,
        )
        r.raise_for_status()
        data  = r.json()
        md    = data.get("market_data", {})
        price = md.get("current_price", {}).get("usd")
        high  = md.get("high_24h", {}).get("usd")
        low   = md.get("low_24h", {}).get("usd")
        print(f"[oracle] {symbol} on {date_str}: close=${price}")
        return {
            "symbol": symbol, "coin_gecko_id": coin_id, "date": date_str,
            "close_usd": price, "high_usd": high, "low_usd": low,
            "source": "CoinGecko", "fetched_at": now_iso(),
        }
    except Exception as e:
        return {"error": str(e), "symbol": symbol, "date": date_str}

# Known source name → base URL
SOURCE_BASE_URLS = {
    "espn":           "https://www.espn.com",
    "espncricinfo":   "https://www.espncricinfo.com",
    "cricinfo":       "https://www.espncricinfo.com",
    "x":              "https://twitter.com",
    "twitter":        "https://twitter.com",
    "coinmarketcap":  "https://coinmarketcap.com",
    "cmc":            "https://coinmarketcap.com",
    "coingecko":      "https://www.coingecko.com",
    "uefa":           "https://www.uefa.com",
    "nba":            "https://www.nba.com",
    "nfl":            "https://www.nfl.com",
    "ipl":            "https://www.iplt20.com",
    "bbc":            "https://www.bbc.com/sport",
    "sky sports":     "https://www.skysports.com",
    "bloomberg":      "https://www.bloomberg.com",
    "reuters":        "https://www.reuters.com",
    "cnn":            "https://www.cnn.com",
    "wikipedia":      "https://www.wikipedia.org",
    "cricket":        "https://www.espncricinfo.com",
    "psa":            "https://www.psacard.com",
    "yahoo finance":  "https://finance.yahoo.com",
    "yahoo":          "https://finance.yahoo.com",
    "binance":        "https://www.binance.com",
}

def resolve_source_url(source: str, question: str, close_time: str) -> str:
    """Resolve a plain text source label to a specific URL for this market."""
    src_lower = source.lower().strip()

    # Already a valid URL — use as-is
    if src_lower.startswith("http"):
        return source

    # Find base URL from known sources
    base = None
    for key, url in SOURCE_BASE_URLS.items():
        if key in src_lower:
            base = url
            break
    if not base:
        base = f"https://{source.strip()}"

    # Use Groq to find the specific page for this exact market question
    if not GROQ_API_KEY:
        return base

    print(f"[oracle] Resolving URL for '{source}' via Groq...")
    raw = call_groq(
        "You are a URL resolver for prediction market settlement. "
        "Given a data source name and a market question, return the single most "
        "relevant URL that would contain the official result. "
        "Return ONLY the full URL, nothing else. No markdown, no explanation.",
        f"Data source: {source}\n"
        f"Base URL: {base}\n"
        f"Market question: {question}\n"
        f"Close time: {close_time}\n"
        f"Return the most specific URL on {base} that contains the official result."
    )
    if raw:
        resolved = raw.strip().strip('"').strip("'").split()[0]
        if resolved.startswith("http"):
            print(f"[oracle] Resolved '{source}' → {resolved}")
            return resolved

    return base


def fetch_web_snapshot(url: str, question: str = "", close_time: str = "") -> dict:
    """Fetch a web source, resolving plain text labels to real URLs first."""
    resolved = resolve_source_url(url, question, close_time)
    print(f"[oracle] Fetching web source: {resolved}")
    try:
        r = requests.get(
            resolved,
            headers={"User-Agent": "Mozilla/5.0 OracleREE/1.0"},
            timeout=15,
        )
        return {
            "url": resolved,
            "original_source": url,
            "status_code": r.status_code,
            "text_snippet": r.text[:5000],
            "sha256": sha256(r.text),
            "fetched_at": now_iso(),
        }
    except Exception as e:
        return {
            "url": resolved,
            "original_source": url,
            "error": str(e),
            "fetched_at": now_iso(),
        }

def call_groq(system_prompt: str, user_prompt: str) -> Optional[str]:
    if not GROQ_API_KEY:
        print("[oracle] GROQ_API_KEY not set — skipping AI grounding")
        return None
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "temperature": 0.1, "max_tokens": 1000,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
            },
            timeout=20,
        )
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[oracle] Groq call failed: {e}")
        return None

def classify_market(question: str, prompt_context: str) -> dict:
    system = (
        "You are an oracle classifier. Return ONLY valid JSON, no markdown.\n"
        "market_type: crypto_price | crypto_price_range | sports | politics | event | unknown\n"
        "is_price_based: true only if asking about a specific asset price vs threshold.\n"
        "asset: use the SHORT ticker symbol only (BTC, ETH, SOL, etc) never full name."
    )
    user = (
        f"Question: {question}\nPrompt: {prompt_context}\n"
        "Return JSON: market_type, is_price_based, asset (short ticker like ETH not Ethereum), "
        "threshold (number or null), capture_date (YYYY-MM-DD or null), confidence"
    )
    raw = call_groq(system, user)
    if not raw:
        return {
            "market_type": "unknown", "is_price_based": False,
            "asset": None, "threshold": None,
            "capture_date": None, "confidence": "low",
        }
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(re.search(r"\{[\s\S]*\}", clean).group(0))
    except Exception:
        return {
            "market_type": "unknown", "is_price_based": False,
            "asset": None, "threshold": None,
            "capture_date": None, "confidence": "low",
        }

def pin_to_ipfs(data: dict, name: str) -> Optional[str]:
    if not PINATA_JWT:
        print("[oracle] PINATA_JWT not set — skipping IPFS pin")
        return None
    try:
        r = requests.post(
            "https://uploads.pinata.cloud/v3/files",
            headers={"Authorization": f"Bearer {PINATA_JWT}"},
            files={"file": (f"{name}.json", json.dumps(data), "application/json")},
            timeout=30,
        )
        cid = r.json()["data"]["cid"]
        print(f"[oracle] IPFS pinned: {cid}")
        return cid
    except Exception as e:
        print(f"[oracle] IPFS pin failed: {e}")
        return None

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
    print(
        f"[oracle] Classification: {classification.get('market_type')} "
        f"(is_price_based={classification.get('is_price_based')})"
    )

    evidence = {
        "market_id":       market.get("id"),
        "market_question": question,
        "close_time":      resolves_at,
        "captured_at":     now_iso(),
        "classification":  classification,
        "data_sources":    [],
    }

    capture_date = classification.get("capture_date") or close_date

    if classification.get("is_price_based") and classification.get("asset"):
        asset      = classification["asset"]
        price_data = fetch_crypto_price(asset, capture_date)
        evidence["price_data"] = price_data
        threshold = classification.get("threshold")
        if threshold and price_data.get("close_usd") is not None:
            close = price_data["close_usd"]
            met   = close > threshold
            evidence["price_verdict"] = (
                f"{asset} on {capture_date} was ${close:,.6f} — "
                f"{'above' if met else 'below'} ${threshold:,.6f} → "
                f"outcome: {'Yes' if met else 'No'}"
            )
            print(f"[oracle] Price verdict: {evidence['price_verdict']}")
    else:
        for src_url in data_sources[:3]:
            evidence["data_sources"].append(
                fetch_web_snapshot(src_url, question, resolves_at)
            )

        web_context = "\n\n---\n\n".join(
            f"[{s['url']}]\n{s.get('text_snippet', '')}"
            for s in evidence["data_sources"]
            if "text_snippet" in s
        )

        is_closed = resolves_at and datetime.fromisoformat(
            resolves_at.replace("Z", "+00:00")
        ) < datetime.now(timezone.utc)

        if is_closed:
            outcomes = meta.get("outcomes") or []
            raw = call_groq(
                "You are OracleREE's fact engine. Use your knowledge and any web evidence. "
                "Return ONLY valid JSON: verdict, matchedOutcome, confidence, explanation.",
                f'Question: "{question}"\n'
                f"Valid outcomes: {', '.join(outcomes)}\n"
                f"Close time: {resolves_at}\n"
                f"\nWEB EVIDENCE:\n{web_context[:4000]}"
            )
            if raw:
                try:
                    clean = raw.replace("```json", "").replace("```", "").strip()
                    evidence["event_verdict"] = json.loads(
                        re.search(r"\{[\s\S]*\}", clean).group(0)
                    )
                    print(
                        f"[oracle] Event verdict: "
                        f"{evidence['event_verdict'].get('verdict')}"
                    )
                except Exception:
                    pass

    evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
    evidence["ipfs_cid"] = pin_to_ipfs(
        evidence, f"oracle-ree-{market.get('id', 'unknown')[:10]}"
    )
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

    if evidence.get("price_data"):
        p = evidence["price_data"]
        lines += [
            "", "PRICE DATA (CoinGecko):",
            f"  Asset:  {p.get('symbol')}",
            f"  Date:   {p.get('date')}",
            f"  Close:  ${p.get('close_usd')}",
            f"  High:   ${p.get('high_usd')}",
            f"  Low:    ${p.get('low_usd')}",
            f"  Source: {p.get('source')}",
        ]
        if evidence.get("price_verdict"):
            lines += ["", f"VERDICT: {evidence['price_verdict']}"]

    if evidence.get("event_verdict"):
        v = evidence["event_verdict"]
        lines += [
            "", "EVENT VERDICT (Groq Llama-3.3-70B + web evidence):",
            f"  Result:      {v.get('verdict')}",
            f"  Outcome:     {v.get('matchedOutcome')}",
            f"  Confidence:  {v.get('confidence')}",
            f"  Explanation: {v.get('explanation')}",
        ]

    if evidence.get("data_sources"):
        lines += ["", "WEB SOURCES CAPTURED:"]
        for src in evidence["data_sources"]:
            lines.append(f"  {src.get('url')} [HTTP {src.get('status_code', '?')}]")
            lines.append(f"  SHA-256: {src.get('sha256', 'N/A')}")

    lines += [
        "", "INTEGRITY:",
        f"  Evidence hash: {evidence.get('evidence_hash', 'N/A')}",
        f"  IPFS CID:      {evidence.get('ipfs_cid', 'Not pinned')}",
        "═══════════════════════════════════════════════════",
        "END ORACLEREE VERIFIED DATA BLOCK",
        "═══════════════════════════════════════════════════",
        "", "ORIGINAL SETTLEMENT PROMPT:",
        "─────────────────────────────────────────────────",
        original_prompt,
    ]
    return "\n".join(lines)

# ─── REE runner ──────────────────────────────────────────────────────────────

def run_ree(
    prompt: str,
    model_name: str = "Qwen/Qwen3-0.6B",
    max_new_tokens: int = 200,
) -> Optional[Path]:
    ree_dir = Path(__file__).parent
    ree_sh  = ree_dir / "ree.sh"
    if not ree_sh.exists():
        print(f"[ree] ree.sh not found at {ree_sh}")
        return None

    prompt_file = ree_dir / "oracle_prompt.jsonl"
    with open(prompt_file, "w", encoding="utf-8") as f:
        json.dump({"prompt": prompt}, f)
    prompt_file.chmod(0o644)

    print(f"\n[ree] Running REE with model={model_name}")
    print(f"[ree] Prompt length: {len(prompt)} chars")

    try:
        result = subprocess.run(
            [
                "bash", str(ree_sh),
                "--model-name", model_name,
                "--prompt-file", str(prompt_file),
                "--max-new-tokens", str(max_new_tokens),
            ],
            cwd=str(ree_dir),
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode != 0:
            print(f"[ree] REE failed (exit {result.returncode})")
            print(result.stdout[-2000:])
            return None

        print(f"[ree] REE completed successfully")

        # Step 1: scan stdout for a recent Receipt line
        receipt_path: Optional[Path] = None
        for line in result.stdout.splitlines():
            if "Receipt:" in line:
                candidate = Path(line.split("Receipt:", 1)[1].strip())
                if candidate.exists():
                    if time.time() - candidate.stat().st_mtime < 600:
                        receipt_path = candidate
                        break

        # Step 2: fallback — newest receipt overall
        if not receipt_path:
            all_receipts = glob.glob(
                str(Path.home() / ".cache/gensyn/**/receipt_*.json"),
                recursive=True,
            )
            if all_receipts:
                receipt_path = Path(
                    max(all_receipts, key=lambda p: Path(p).stat().st_mtime)
                )
                print(f"[ree] Receipt (fallback): {receipt_path}")

        if receipt_path:
            print(f"[ree] Receipt: {receipt_path}")
        return receipt_path

    except subprocess.TimeoutExpired:
        print("[ree] REE timed out after 600s")
        return None
    finally:
        prompt_file.unlink(missing_ok=True)

# ─── Combined proof builder ──────────────────────────────────────────────────

def build_combined_proof(
    market_id: str,
    evidence: dict,
    receipt_path: Optional[Path],
) -> dict:
    proof = {
        "version":         "1.0.0",
        "tool":            "OracleREE",
        "market_id":       market_id,
        "created_at":      now_iso(),
        "oracle_evidence": evidence,
        "ree_receipt":     None,
        "verification": {
            "oracle_evidence_hash": evidence.get("evidence_hash"),
            "ipfs_cid":             evidence.get("ipfs_cid"),
            "ree_receipt_hash":     None,
            "combined_hash":        None,
        },
    }

    if receipt_path and receipt_path.exists():
        with open(receipt_path) as f:
            receipt = json.load(f)
        proof["ree_receipt"] = receipt
        proof["verification"]["ree_receipt_hash"] = (
            receipt.get("hashes", {}).get("receipt_hash")
        )
        proof["verification"]["prompt_hash"] = (
            receipt.get("input", {}).get("prompt_hash")
        )
        combined = sha256(
            str(evidence.get("evidence_hash")) +
            str(receipt.get("hashes", {}).get("receipt_hash"))
        )
        proof["verification"]["combined_hash"] = combined
        print(f"\n[proof] Combined hash:        {combined}")
        print(f"[proof] Oracle evidence hash: {evidence.get('evidence_hash')}")
        print(f"[proof] REE receipt hash:     "
              f"{receipt.get('hashes', {}).get('receipt_hash')}")

    return proof

# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="OracleREE — Trustless oracle grounding for Delphi settlement"
    )
    parser.add_argument(
        "--market", "-m",
        help="Delphi market URL or market ID (0x...)",
        default=None,
    )
    parser.add_argument(
        "--model",
        help="Override REE model (HuggingFace ID)",
        default=None,
    )
    parser.add_argument("--max-tokens", type=int, default=200)
    parser.add_argument(
        "--oracle-only",
        action="store_true",
        help="Only fetch oracle evidence, skip REE inference",
    )
    parser.add_argument("--output", "-o", default=None)
    args = parser.parse_args()

    market_input = args.market
    if not market_input:
        print("OracleREE — Trustless oracle grounding for Delphi settlement")
        print("=" * 60)
        market_input = input("Paste Delphi market URL or ID: ").strip()

    if not market_input:
        print("Error: market URL or ID required")
        return 1

    try:
        market_id = extract_market_id(market_input)
        print(f"\n[oracle] Market ID: {market_id}")
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    try:
        market = fetch_market(market_id)
    except Exception as e:
        print(f"Error fetching market: {e}")
        return 1

    meta           = market.get("metadata") or {}
    question       = meta.get("question", "Unknown")
    prompt_context = (meta.get("model") or {}).get("prompt_context", question)
    delphi_model   = (meta.get("model") or {}).get("model_identifier", "")
    ree_model      = args.model or resolve_ree_model(delphi_model)

    print(f"[oracle] Question:     {question}")
    print(f"[oracle] Delphi model: {delphi_model}")
    print(f"[oracle] REE model:    {ree_model}")

    evidence      = build_oracle_evidence(market)
    oracle_prompt = build_oracle_prompt(prompt_context, evidence)

    print(f"\n[oracle] Oracle prompt length: {len(oracle_prompt)} chars")
    print(f"[oracle] Evidence hash: {evidence.get('evidence_hash')}")
    if evidence.get("ipfs_cid"):
        print(f"[oracle] IPFS CID: {evidence['ipfs_cid']}")

    receipt_path = None
    if not args.oracle_only:
        receipt_path = run_ree(
            prompt=oracle_prompt,
            model_name=ree_model,
            max_new_tokens=args.max_tokens,
        )
    else:
        print("\n[ree] Skipping REE inference (--oracle-only)")

    proof = build_combined_proof(market_id, evidence, receipt_path)

    output_path = (
        args.output or
        f"oracle_proof_{market_id[:10]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
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
    if proof["verification"].get("ree_receipt_hash"):
        print(f"REE receipt:   {proof['verification']['ree_receipt_hash']}")
        print(f"Combined hash: {proof['verification']['combined_hash']}")
        print("\n✓ Oracle data + REE execution cryptographically linked")
        print("✓ Verify: cd /path/to/ree && python3 ree.py verify "
              "--receipt-path /path/to/receipt.json")
    else:
        print("\n✓ Oracle evidence captured and hashed")
        print("  (Run without --oracle-only to also generate REE receipt)")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
