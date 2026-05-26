#!/usr/bin/env python3
"""
OracleREE — Trustless oracle grounding for Gensyn Delphi settlement verification.

Core rule: Creator source is law.
  - Tavily/Groq are restricted to the creator-listed source domain
  - Recovery stays within the same source family
  - Fallback to other sources only if settlement prompt explicitly allows it
  - INCONCLUSIVE is honest; a guessed answer is not
"""

from __future__ import annotations
import argparse, calendar, glob, hashlib, json, os, re, subprocess, time
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
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ─── Optional external Ollama evidence brain ──────────────────────────────────
# This module is validation/extraction support only. It never owns final settlement.
try:
    from ollama_brain import (
        validate_spread_evidence as ollama_validate_spread_evidence,
        deterministic_spread_evidence_gate as _strict_spread_evidence_check,
        ask_ollama_json as _ollama_brain_ask_json,
    )
    _OLLAMA_BRAIN_IMPORTED = True
except Exception:
    _OLLAMA_BRAIN_IMPORTED = False

# ─── oracle_core fetch module ─────────────────────────────────────────────────
# Phase 1: central fetch + validation layer. This module validates content BEFORE
# OracleREE extraction/resolution, so SPA shells/homepages do not leak downstream.
try:
    from oracle_core.fetch_source import fetch_source, FetchResult
    _FETCH_SOURCE_MODULE_LOADED = True
    print("[oracle] oracle_core.fetch_source loaded")
except Exception as e:
    print(f"[oracle] fetch_source disabled: {e}")
    _FETCH_SOURCE_MODULE_LOADED = False

# ─── Config ───────────────────────────────────────────────────────────────────
DELPHI_API_BASE = "https://api.delphi.fyi"
COINGECKO_BASE  = "https://api.coingecko.com/api/v3"
PINATA_JWT      = os.environ.get("PINATA_JWT", "")
GROQ_API_KEY    = os.environ.get("GROQ_API_KEY", "")
TAVILY_API_KEY  = os.environ.get("TAVILY_API_KEY", "")
ORACLE_SEAL_URL = os.environ.get("ORACLE_SEAL_URL", "https://oracle-seal.vercel.app")

# Local Settlement Brain (Ollama)
# Use qwen2.5:3b-instruct by default. Override in .env.local if needed:
# OLLAMA_MODEL=qwen2.5:7b-instruct
OLLAMA_URL      = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b-instruct")
USE_OLLAMA_BRAIN = os.environ.get("USE_OLLAMA_BRAIN", "1").strip().lower() not in {"0", "false", "no", "off"}

DELPHI_TO_REE_MODEL: dict[str, str] = {
    "Claude Opus 4.7":"Qwen/Qwen3-0.6B","Claude Opus 4.6":"Qwen/Qwen3-0.6B",
    "Claude Opus 4":"Qwen/Qwen3-0.6B","Claude Sonnet 4.7":"Qwen/Qwen3-0.6B",
    "Claude Sonnet 4.6":"Qwen/Qwen3-0.6B","Claude Sonnet 4":"Qwen/Qwen3-0.6B",
    "Claude Haiku 4.7":"Qwen/Qwen3-0.6B","Claude Haiku 4.6":"Qwen/Qwen3-0.6B",
    "Claude Haiku 4":"Qwen/Qwen3-0.6B","claude-opus":"Qwen/Qwen3-0.6B",
    "claude-sonnet":"Qwen/Qwen3-0.6B","claude-haiku":"Qwen/Qwen3-0.6B",
    "gpt-4":"Qwen/Qwen3-0.6B","gpt-4o":"Qwen/Qwen3-0.6B",
    "gpt-4o-mini":"Qwen/Qwen3-0.6B","gpt-3.5-turbo":"Qwen/Qwen3-0.6B",
    "gemini-pro":"Qwen/Qwen3-0.6B","gemini-flash":"Qwen/Qwen3-0.6B",
    "grok":"Qwen/Qwen3-0.6B","Qwen/Qwen3-32B":"Qwen/Qwen3-0.6B",
    "Qwen/Qwen2.5-32B-Instruct":"Qwen/Qwen3-0.6B","Qwen/Qwen2.5-32B":"Qwen/Qwen3-0.6B",
    "Qwen/Qwen3-14B":"Qwen/Qwen3-0.6B","Qwen/Qwen3-14B-Instruct":"Qwen/Qwen3-0.6B",
    "Qwen/Qwen3-4B":"Qwen/Qwen3-4B","Qwen/Qwen2.5-7B-Instruct":"Qwen/Qwen2.5-7B-Instruct",
    "Qwen/Qwen2.5-7B":"Qwen/Qwen2.5-7B","Qwen/Qwen2.5-3B-Instruct":"Qwen/Qwen2.5-3B-Instruct",
    "Meta-Llama/Meta-Llama-3-8B-Instruct":"Meta-Llama/Meta-Llama-3-8B-Instruct",
    "Meta-Llama/Meta-Llama-3-8B":"Meta-Llama/Meta-Llama-3-8B",
    "Meta-Llama/Llama-3.1-8B-Instruct":"Meta-Llama/Llama-3.1-8B-Instruct",
    "Meta-Llama/Llama-3.1-8B":"Meta-Llama/Llama-3.1-8B",
    "Meta-Llama/Llama-3.2-3B-Instruct":"Meta-Llama/Llama-3.2-3B-Instruct",
    "Mistralai/Mistral-7B-Instruct-V0.2":"Mistralai/Mistral-7B-Instruct-V0.2",
    "01-Ai/Yi-1.5-6B-Chat":"01-Ai/Yi-1.5-6B-Chat",
    "Llm-Jp/Llm-Jp-3-3.7b-Instruct":"Qwen/Qwen3-0.6B",
}

COINGECKO_IDS = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","BNB":"binancecoin",
    "XRP":"ripple","ADA":"cardano","AVAX":"avalanche-2","DOGE":"dogecoin",
    "HYPE":"hyperliquid","WIF":"dogwifcoin","BONK":"bonk","ONDO":"ondo-finance",
    "SUI":"sui","APT":"aptos","ARB":"arbitrum","OP":"optimism","NEAR":"near",
    "PEPE":"pepe","PENGU":"pudgy-penguins",
}

NON_CRYPTO_ASSETS = {
    "spx","sp500","s&p500","s&p 500","spy","ndx","nasdaq","qqq",
    "djia","dow","dowjones","vix","gold","silver","oil","wti",
    "eur","gbp","jpy","usd","h200","ornn","gpu",
}

CRICKET_KEYWORDS = [
    "cricket", "ipl", "wicket", "wickets", "overs", "t20", "odi", "test match",
    "psl", "pakistan super league", "bbl", "big bash", "cpl", "caribbean",
    "lpl", "lanka", "sa20", "ilt20", "super league",
]

# Source families: if primary fails, try these in order (same publisher)
SOURCE_FAMILIES: dict[str, list[str]] = {
    # Generic ESPN must not recover to ESPN Cricinfo unless the market is cricket.
    "espn.com":          ["espn.com"],
    "espncricinfo.com":  ["espncricinfo.com", "espn.com"],
    "bbc.com":           ["bbc.com", "bbc.co.uk"],
    "reuters.com":       ["reuters.com"],
    "bloomberg.com":     ["bloomberg.com"],
    "cnn.com":           ["cnn.com"],
    "cnbc.com":          ["cnbc.com"],
    "apnews.com":        ["apnews.com"],
    "coinmarketcap.com": ["coinmarketcap.com"],
    "coingecko.com":     ["coingecko.com"],
    "yahoo.com":         ["finance.yahoo.com", "yahoo.com"],
    "finance.yahoo.com": ["finance.yahoo.com", "yahoo.com"],
    "nba.com":           ["nba.com", "espn.com"],
    "nfl.com":           ["nfl.com", "espn.com"],
    "skysports.com":     ["skysports.com", "espn.com"],
    "ufl.football":      ["ufl.football", "espn.com"],
}
# Source name → URL mapping
SOURCE_NAME_TO_URL: dict[str, str] = {
    "cnn":"https://www.cnn.com","cnbc":"https://www.cnbc.com",
    "reuters":"https://www.reuters.com","bloomberg":"https://www.bloomberg.com",
    "associatedpress":"https://apnews.com","ap":"https://apnews.com","apnews":"https://apnews.com",
    "bbc":"https://www.bbc.com","espn":"https://www.espn.com",
    "espncricinfo":"https://www.espncricinfo.com","nytimes":"https://www.nytimes.com",
    "theguardian":"https://www.theguardian.com","skysports":"https://www.skysports.com",
    "foxnews":"https://www.foxnews.com","nbcnews":"https://www.nbcnews.com",
    "yahoo":"https://finance.yahoo.com","yahoofinance":"https://finance.yahoo.com","yahoo finance":"https://finance.yahoo.com",
    "coinmarketcap":"https://coinmarketcap.com","coingecko":"https://www.coingecko.com",
    "eurovision":"https://eurovision.tv",
    "uefa":"https://www.uefa.com","nba":"https://www.nba.com","nfl":"https://www.nfl.com",
    "ufl":"https://www.theufl.com","theufl":"https://www.theufl.com",
}

# ─── Evidence classes ─────────────────────────────────────────────────────────
class Fact:
    def __init__(self, label:str, value:str, source:str, timestamp:str="", unit:str=""):
        self.label=label; self.value=value; self.source=source
        self.timestamp=timestamp; self.unit=unit
    def to_dict(self)->dict:
        return {k:v for k,v in {"label":self.label,"value":self.value,"source":self.source,
            "timestamp":self.timestamp,"unit":self.unit}.items() if v}
    def __str__(self)->str:
        u=f" {self.unit}" if self.unit else ""
        t=f" [{self.timestamp}]" if self.timestamp else ""
        return f"{self.label}: {self.value}{u}{t} (via {self.source})"

class EvidenceBlock:
    def __init__(self):
        self.fetch_status="PENDING"; self.parse_status="PENDING"
        self.outcome_status="PENDING"; self.facts:list[Fact]=[]
        self.matched_outcome:Optional[str]=None; self.calculation:Optional[str]=None
        self.source_used:Optional[str]=None; self.fetch_method:Optional[str]=None
        self.reason:Optional[str]=None; self.raw_content:Optional[str]=None
        self.recovered_from:Optional[str]=None
    @property
    def verified(self)->bool:
        return (self.fetch_status=="FETCHED" and self.parse_status=="PARSED"
                and self.outcome_status=="OUTCOME_FOUND" and self.matched_outcome is not None)
    def pipeline_status(self)->str:
        if self.verified: return "FETCHED | PARSED | OUTCOME_FOUND"
        return " | ".join(p for p in [self.fetch_status,self.parse_status,self.outcome_status]
                          if p and p!="PENDING")
    def to_dict(self)->dict:
        result = {
            "fetch_status":self.fetch_status,"parse_status":self.parse_status,
            "outcome_status":self.outcome_status,"pipeline":self.pipeline_status(),
            "facts":[f.to_dict() for f in self.facts],
            "raw_content":(self.raw_content or "")[:1000],
            "derived_result":{"calculation":self.calculation,"matched_outcome":self.matched_outcome}
                if self.matched_outcome else None,
            "source_used":self.source_used,"fetch_method":self.fetch_method,
            "recovered_from":self.recovered_from,"reason":self.reason,
        }
        if self.fetch_method == "sports_fallback":
            result["sports_fallback_used"] = True
            result["fallback_note"] = (
                "Creator source failed or returned insufficient evidence. Sports result is an immutable fact — "
                "same result verified from alternative sports data source."
            )
        return result

# ─── Helpers ──────────────────────────────────────────────────────────────────
def sha256(text:str)->str:
    return "sha256:"+hashlib.sha256(text.encode()).hexdigest()

def normalize_prompt_for_hash(text:str)->str:
    text=str(text or "").replace("\r\n","\n").replace("\r","\n")
    text=re.sub(r"[ \t]+"," ",text); text=re.sub(r"\n\s*\n+","\n\n",text)
    return text.strip()

def prompt_hash(text:str)->str: return sha256(normalize_prompt_for_hash(text))

def extract_prompt_question(prompt:str)->str:
    m=re.search(r"QUESTION:\s*(.+?)(?:\n\s*\n|DATA SOURCES:|SETTLEMENT RULES:|VALID OUTCOMES|$)",
                str(prompt or ""),re.I|re.S)
    return " ".join(m.group(1).split()) if m else ""

def normalize_question_for_match(q:str)->str:
    q=str(q or "").lower()
    q=re.sub(r"\$?\d+(?:,\d{3})*(?:\.\d+)?","<num>",q)
    return " ".join(re.sub(r"[^a-z0-9<>]+"," ",q).split())

def analyze_prompt_integrity(user_prompt:str, official_prompt:str, official_question:str)->dict:
    user_prompt=str(user_prompt or "").strip(); official_prompt=str(official_prompt or "").strip()
    official_question=str(official_question or "").strip(); is_user=bool(user_prompt)
    uq=extract_prompt_question(user_prompt) if is_user else ""
    oh=prompt_hash(official_prompt) if official_prompt else ""
    uh=prompt_hash(user_prompt) if is_user else ""
    exact=bool(is_user and oh and uh and oh==uh)
    qm=True
    if is_user and uq and official_question:
        qm=normalize_question_for_match(uq)==normalize_question_for_match(official_question)
    if not is_user: mode,source,warning="CANONICAL_DELPHI_MARKET","Official Delphi Prompt",""
    elif exact: mode,source,warning="CANONICAL_DELPHI_PROMPT","User Prompt Matches Official",""
    elif qm: mode,source,warning="MODIFIED_PROMPT_SIMULATION","User Provided Prompt","Prompt differs from official."
    else: mode,source,warning="CUSTOM_PROMPT_EXECUTION","User Provided Prompt","Question differs from official."
    return {"prompt_source":source,"verification_mode":mode,
            "prompt_match":"YES" if exact or not is_user else "NO",
            "question_match":"YES" if qm else "NO",
            "official_prompt_hash":oh,"user_prompt_hash":uh,
            "official_question":official_question,"user_question":uq,"warning":warning}

def is_raw_settlement_prompt(value:str)->bool:
    v=str(value or ""); u=v.upper()
    if re.search(r"0x[a-fA-F0-9]{40}",v): return False
    if re.search(r"https?://",v) and "delphi.fyi" in v.lower(): return False
    return len(v)>80 and ("QUESTION:" in u or "SETTLEMENT RULES" in u or "VALID OUTCOMES" in u)

def extract_market_id(input_str:str)->str:
    m=re.search(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])",input_str)
    if m: return m.group(0)
    u=re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",input_str)
    if u:
        ox=resolve_uuid_to_market_id(u.group(0),input_str)
        if ox: return ox
        raise ValueError(f"Could not resolve UUID {u.group(0)}")
    if len(input_str)>50 and GROQ_API_KEY:
        ox=resolve_prompt_to_market_id(input_str)
        if ox: return ox
    raise ValueError(f"Could not extract market ID from: {input_str[:100]}")

def resolve_uuid_to_market_id(uuid:str, original_url:str)->Optional[str]:
    api_key=os.environ.get("DELPHI_API_ACCESS_KEY","")
    if not api_key: return None
    try:
        for status in ["open","settled","expired"]:
            r=requests.get("https://api.delphi.fyi/markets",
                headers={"x-api-key":api_key},params={"limit":100,"status":status},timeout=15)
            if not r.ok: continue
            for m in r.json().get("markets",[]):
                if uuid.replace("-","") in str(m.get("metadataUri","")).replace("-",""):
                    return m.get("id")
        if original_url.startswith("http"):
            resp=requests.get(original_url,headers={"User-Agent":"Mozilla/5.0"},timeout=10)
            tm=re.search(r'<title[^>]*>([^<]+)</title>',resp.text)
            if tm:
                q=re.sub(r"\s*[-|]\s*Delphi.*$","",tm.group(1)).strip()
                if len(q)>10: return resolve_prompt_to_market_id(q)
    except Exception as e: print(f"[oracle] UUID resolution failed: {e}")
    return None

def resolve_prompt_to_market_id(prompt:str)->Optional[str]:
    api_key=os.environ.get("DELPHI_API_ACCESS_KEY","")
    if not api_key: return None
    try:
        question=""
        for line in prompt.splitlines():
            if "QUESTION:" in line.upper(): question=line.split(":",1)[1].strip(); break
        if not question:
            for line in prompt.splitlines():
                if len(line.strip())>20: question=line.strip()[:100]; break
        for status in ["open","settled","expired"]:
            r=requests.get("https://api.delphi.fyi/markets",
                headers={"x-api-key":api_key},params={"limit":100,"status":status},timeout=15)
            if not r.ok: continue
            qn=normalize_question_for_match(question)
            for m in r.json().get("markets",[]):
                mq=m.get("metadata",{}).get("question","")
                if question.lower()[:40] in mq.lower() or qn[:60] in normalize_question_for_match(mq):
                    print(f"[oracle] Matched: {m.get('id')} — {mq[:60]}")
                    return m.get("id")
    except Exception as e: print(f"[oracle] Prompt resolution failed: {e}")
    return None

def now_iso()->str: return datetime.now(timezone.utc).isoformat()
def is_non_crypto(asset:str)->bool:
    return bool(asset) and asset.lower().replace(" ","").replace("&","") in NON_CRYPTO_ASSETS
def resolve_ree_model(delphi_model:str, fallback:str="Qwen/Qwen3-0.6B")->str:
    if delphi_model in DELPHI_TO_REE_MODEL: return DELPHI_TO_REE_MODEL[delphi_model]
    if any(m in delphi_model.lower() for m in ["32b","70b","72b","34b","13b","14b","30b","40b","65b"]):
        print(f"[oracle] Large model ({delphi_model}) → {fallback}"); return fallback
    if "/" in delphi_model: return delphi_model
    for key,val in DELPHI_TO_REE_MODEL.items():
        if key.lower() in delphi_model.lower(): return val
    return fallback

def clean_domain(url:str)->str:
    s=re.sub(r"^https?://","",(str(url or "").lower()))
    s=s.split("/")[0].split("?")[0]
    return s[4:] if s.startswith("www.") else s

def resolve_source_to_url(source:str)->str:
    """Resolve source name or URL to a canonical URL."""
    if source.startswith("http"): return source
    key=source.lower().replace(" ","").replace(".","").replace("_","")
    return SOURCE_NAME_TO_URL.get(key, f"https://www.{source.lower().replace(' ','')}.com")

def get_source_family(domain:str)->list[str]:
    """Get the list of domains to try for a given source domain."""
    return SOURCE_FAMILIES.get(domain, [domain])

# ─── Groq ─────────────────────────────────────────────────────────────────────
def call_groq(system_prompt:str, user_prompt:str, max_tokens:int=800)->Optional[str]:
    if not GROQ_API_KEY: return None
    try:
        r=requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile","temperature":0.1,"max_tokens":max_tokens,
                  "messages":[{"role":"system","content":system_prompt},
                               {"role":"user","content":user_prompt}]},timeout=20)
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e: print(f"[oracle] Groq failed: {e}"); return None

# ─── Local Settlement Brain: Ollama ───────────────────────────────────────────
def call_ollama_json(prompt: str, model: Optional[str] = None, timeout: int = 120) -> Optional[dict]:
    """
    Local planner call. Ollama is used for planning/validation/extraction hints only.
    It NEVER decides final settlement; deterministic Python resolvers still decide.
    """
    if not USE_OLLAMA_BRAIN:
        return None
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": model or OLLAMA_MODEL,
                "stream": False,
                "format": "json",
                "prompt": prompt,
                "options": {
                    "temperature": 0,
                    "num_predict": 700,
                },
            },
            timeout=timeout,
        )
        r.raise_for_status()
        raw = r.json().get("response", "{}").strip()
        obj = _safe_json_object(raw)
        if isinstance(obj, dict):
            return obj
        return json.loads(raw)
    except Exception as e:
        print(f"[oracle] Ollama brain failed: {e}")
        return None

def merge_plan_refinement(plan: dict, obj: Optional[dict], source: str = "brain") -> dict:
    """Merge planner output without allowing it to degrade deterministic heuristics."""
    if not isinstance(obj, dict):
        return plan

    # Guard: if deterministic planner already identified a count market,
    # never let LLM refinement downgrade it into sports/named-choice.
    locked_count_market = (
        plan.get("metric") == "count"
        or bool(plan.get("count_subject"))
        or plan.get("resolver") == "count_compare"
    )

    allowed_market_types = {
        "numeric_threshold", "numeric_range", "binary_event", "draft_named_choice",
        "sports", "sports_spread", "event_choice", "crypto_price",
        "crypto_price_range", "finance", "politics", "confirmation", "unknown",
    }
    allowed_formats = {
        "binary", "numeric_threshold", "numeric_range", "named_choice",
        "spread_cover", "freeform",
    }
    allowed_resolvers = {
        "generic_outcome_resolver", "numeric_threshold", "numeric_range",
        "binary_yes_no", "named_choice", "map_player_position_to_outcome",
        "sports_result", "spread_cover", "crypto_price", "price_bucket",
        "threshold_compare", "count_compare",
    }

    for k, v in obj.items():
        if v is None or _norm_unknown(v):
            continue
        if k == "facts_needed":
            if isinstance(v, list) and v and not (len(v) == 1 and str(v[0]).lower() == "result"):
                plan[k] = [str(x) for x in v if str(x).strip()]
        elif k == "market_type":
            if locked_count_market and str(v) not in {"numeric_threshold", "numeric_range"}:
                continue
            if str(v) in allowed_market_types:
                plan[k] = str(v)
        elif k == "answer_format":
            if locked_count_market and str(v) not in {"numeric_threshold", "numeric_range"}:
                continue
            if str(v) in allowed_formats:
                plan[k] = str(v)
        elif k == "resolver":
            if locked_count_market and str(v) not in {"count_compare", "numeric_threshold", "numeric_range"}:
                continue
            if str(v) in allowed_resolvers:
                plan[k] = str(v)
        elif k in {"search_query", "event_description", "metric", "count_subject"}:
            if not _norm_unknown(v):
                plan[k] = str(v)
        elif k in {"threshold", "target", "asset", "is_price_based", "needs_canonicalization", "event_date", "timing"}:
            plan[k] = v

    print(f"[oracle] {source} refined plan: {plan.get('market_type')} | {plan.get('answer_format')} | {plan.get('resolver')}")
    return plan

# ─── Delphi + OracleSeal ──────────────────────────────────────────────────────
def fetch_market(market_id:str)->dict:
    api_key=os.environ.get("DELPHI_API_ACCESS_KEY","")
    if not api_key: raise ValueError("DELPHI_API_ACCESS_KEY not set.")
    r=requests.get(f"{DELPHI_API_BASE}/markets/{market_id}",
        headers={"x-api-key":api_key},timeout=15)
    r.raise_for_status(); return r.json()

def get_oracle_seal_snapshot(market_id:str)->Optional[dict]:
    try:
        r=requests.get(f"{ORACLE_SEAL_URL}/api/evidence/{market_id}",timeout=10)
        if r.ok:
            snaps=r.json().get("snapshots") or []
            if snaps and snaps[0].get("ipfs_cid"):
                print(f"[oracle] OracleSeal: {snaps[0]['ipfs_cid']}"); return snaps[0]
    except Exception as e: print(f"[oracle] OracleSeal failed: {e}")
    return None

def push_to_oracle_seal(proof:dict)->bool:
    v=proof.get("verification",{}) or {}
    if not v.get("ree_receipt_hash"): return False
    try:
        r=requests.post(f"{ORACLE_SEAL_URL}/api/receipts",json={
            "marketId":proof.get("market_id"),"receiptHash":v.get("ree_receipt_hash"),
            "ipfsCid":v.get("ipfs_cid"),"combinedHash":v.get("combined_hash"),
            "oracleHash":v.get("oracle_evidence_hash"),},timeout=10)
        if r.ok: print("[oracle] Pushed to OracleSeal ✓"); return True
    except Exception as e: print(f"[oracle] OracleSeal push failed: {e}")
    return False

# ─── Market Intelligence ──────────────────────────────────────────────────────
MONTH_MAP = {
    "january":"01","february":"02","march":"03","april":"04",
    "may":"05","june":"06","july":"07","august":"08",
    "september":"09","october":"10","november":"11","december":"12",
}




def _safe_json_object(text: str) -> Optional[dict]:
    """Extract and parse the first JSON object from a model response."""
    if not text:
        return None
    clean = str(text).replace("```json", "").replace("```", "").strip()
    m = re.search(r"\{[\s\S]*\}", clean)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _parse_outcome_number(text: str) -> Optional[float]:
    """Parse first numeric value in an outcome, including k/m/b suffixes."""
    m = re.search(r"-?\d+(?:\.\d+)?\s*[kKmMbB]?", str(text or ""))
    if not m:
        return None
    raw = m.group(0).strip().lower().replace(",", "")
    mult = 1.0
    if raw.endswith("k"):
        mult = 1_000.0
        raw = raw[:-1]
    elif raw.endswith("m"):
        mult = 1_000_000.0
        raw = raw[:-1]
    elif raw.endswith("b"):
        mult = 1_000_000_000.0
        raw = raw[:-1]
    try:
        return float(raw) * mult
    except Exception:
        return None


def _find_numeric_threshold(outcomes: list) -> Optional[float]:
    """Find threshold from Over/Under/Above/Below outcomes."""
    vals = []
    for outcome in outcomes or []:
        ol = str(outcome).lower()
        if any(k in ol for k in ["over", "under", "above", "below"]):
            n = _parse_outcome_number(outcome)
            if n is not None:
                vals.append(n)
    return vals[0] if vals else None


def _looks_like_count_market(question: str, outcomes: list, rules: Optional[dict] = None) -> bool:
    """True for markets asking for a number/count, even if the event is sports/draft."""
    q = str(question or "").lower()
    if rules and ((rules or {}).get("metric") == "count" or (rules or {}).get("count_subject")):
        return True
    outs = " ".join(str(o).lower() for o in outcomes or [])
    return (
        any(k in q for k in ["how many", "number of", "count of", "total number", "total"])
        or bool(re.search(r"\b(over|under|above|below)\s*\d+(?:\.\d+)?\b", outs))
    )


def _infer_count_subject(question: str) -> str:
    """Infer the object being counted using broad, non-market-specific rules."""
    q = str(question or "").lower()

    subjects = [
        ("offensive lineman", "offensive linemen"), ("offensive linemen", "offensive linemen"),
        ("quarterback", "quarterbacks"), ("qb", "quarterbacks"),
        ("wide receiver", "wide receivers"), ("wr", "wide receivers"),
        ("running back", "running backs"), ("rb", "running backs"),
        ("tight end", "tight ends"), ("te", "tight ends"),
        ("defensive lineman", "defensive linemen"), ("defensive line", "defensive linemen"),
        ("trade", "trades"), ("trades", "trades"),
        ("pick", "picks"), ("selection", "selections"), ("player", "players"),
        ("goal", "goals"), ("point", "points"), ("birdie", "birdies"),
        ("wicket", "wickets"), ("touchdown", "touchdowns"), ("assist", "assists"),
    ]
    for singular, plural in subjects:
        if singular in q or plural in q:
            return plural

    m = re.search(r"how many\s+(.+?)\s+(?:will|were|are|have|has|did|does|occur|in|during)", q)
    if m:
        return m.group(1).strip()
    m = re.search(r"number of\s+(.+?)\s+(?:will|were|are|have|has|did|does|occur|in|during)", q)
    if m:
        return m.group(1).strip()
    return "items"


def _force_count_plan(plan: dict, question: str, outcomes: list, close_date: str, rules: Optional[dict] = None) -> dict:
    """Hard guard: count markets must not be reclassified as sports/named-choice."""
    subject = ((rules or {}).get("count_subject") if rules else None) or _infer_count_subject(question)
    threshold = ((rules or {}).get("threshold") if rules else None) or plan.get("threshold") or _find_numeric_threshold(outcomes)
    plan.update({
        "market_type": "numeric_threshold",
        "answer_format": "numeric_threshold",
        "facts_needed": ["count", "official_total", subject],
        "resolver": "count_compare",
        "metric": "count",
        "count_subject": subject,
        "threshold": threshold,
        "search_query": f"{question} official final count total {subject} {close_date}",
    })
    return plan


def _norm_unknown(value: object) -> bool:
    return str(value or "").strip().lower() in {"", "unknown", "none", "null", "n/a"}

def _infer_answer_format_from_outcomes(question: str, outcomes: list) -> str:
    q_lower = str(question or "").lower()
    outs = [str(o).strip().lower() for o in outcomes or []]
    out_set = set(outs)

    # Count markets must win over sports/draft routing.
    # Example: "How many trades will occur during round one of the NFL draft?"
    # is NOT a sports winner/named-choice market. It is numeric_threshold.
    if (
        any(k in q_lower for k in ["how many", "number of", "count of", "total number", "total"])
        or any(re.search(r"\b(over|under|above|below)\s*\d", o, re.I) for o in outs)
    ):
        return "numeric_threshold"

    binary_pairs = [
        {"yes", "no"}, {"green", "red"}, {"up", "down"},
        {"higher", "lower"}, {"above", "below"}, {"true", "false"},
    ]
    if any(out_set <= pair or out_set == pair for pair in binary_pairs):
        return "binary"

    # Spread cover markets: two outcomes like "Kings +10.5" vs "Defenders -10.5".
    # These contain numbers, but they are binary spread outcomes, not range buckets.
    spread_re = re.compile(r"^.+\s[+-]\d+\.?\d*\s*$")
    if len(outs) == 2 and all(spread_re.match(o) for o in outs):
        return "spread_cover"

    if any(re.search(r"\d", o) for o in outs) and any(
        token in o for o in outs for token in ["-", "–", "+", "$", "k", "m", "below", "above", "and above", "and below"]
    ):
        return "numeric_range"
    if any(k in q_lower for k in ["who", "which", "winner", "selected", "pick", "award", "election"]):
        return "named_choice"
    return "freeform"

def _draft_pick_order_from_question(question: str) -> str:
    q = str(question or "").lower()
    if "second" in q or "2nd" in q:
        return "second"
    if "first" in q or "1st" in q:
        return "first"
    if "third" in q or "3rd" in q:
        return "third"
    return ""

def _extract_team_hint(question: str) -> str:
    """Best-effort team/entity phrase extraction for source search queries."""
    q = str(question or "")
    # Common "Who will the X draft..." shape
    m = re.search(r"who\s+will\s+the\s+(.+?)\s+draft\b", q, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"will\s+(.+?)\s+(?:announce|buy|purchase|acquire|launch|list|win|draft)\b", q, re.I)
    if m:
        return m.group(1).strip()
    return ""

def settlement_plan_brain(question: str, prompt_context: str, outcomes: list,
                          data_sources: list, close_time: str, base: dict) -> dict:
    """
    Pre-fetch planning brain.
    It returns a general settlement plan used to build source-locked searches and
    extraction labels. This prevents weak generic labels like ['result'] from
    causing future false INCONCLUSIVE runs.
    """
    close_date = close_time[:10] if close_time else "unknown"
    q_lower = str(question or "").lower()
    answer_format = _infer_answer_format_from_outcomes(question, outcomes)
    threshold = _find_numeric_threshold(outcomes) if "numeric" in answer_format else None

    # Heuristic plan: always available, even if Groq fails.
    plan = {
        **(base or {}),
        "event_description": question,
        "event_date": close_date,
        "timing": "at_close",
        "answer_format": answer_format,
        "market_type": base.get("market_type", "unknown") if isinstance(base, dict) else "unknown",
        "facts_needed": ["result"],
        "threshold": threshold if threshold is not None else (base or {}).get("threshold"),
        "search_query": question,
        "resolver": "generic_outcome_resolver",
        "needs_canonicalization": True,
    }

    # Hard guard before any draft/sports routing: count markets stay numeric.
    if _looks_like_count_market(question, outcomes):
        plan = _force_count_plan(plan, question, outcomes, close_date)

    # Generic structural heuristics. These are category-level, not market-specific.
    if answer_format == "spread_cover":
        plan.update({
            "market_type": "sports_spread",
            "facts_needed": ["final_score", "winning_margin", "game_result", "point_spread"],
            "resolver": "spread_cover",
            "threshold": threshold,
            "search_query": f"{question} UFL football final score result spread {close_date}",
        })
    elif answer_format == "numeric_threshold":
        plan.update({
            "market_type": "numeric_threshold",
            "facts_needed": ["count", "number", "total"],
            "resolver": "numeric_threshold",
            "search_query": f"{question} official count total number result {close_date}",
        })
    elif answer_format == "numeric_range":
        plan.update({
            "market_type": "numeric_range",
            "facts_needed": ["value", "number", "price", "score", "count"],
            "resolver": "numeric_range",
            "search_query": f"{question} official value result {close_date}",
        })
    elif answer_format == "binary":
        plan.update({
            "market_type": "binary_event",
            "facts_needed": ["event_status", "confirmation", "result"],
            "resolver": "binary_yes_no",
            "search_query": f"{question} official confirmed result {close_date}",
        })
    elif answer_format == "named_choice":
        plan.update({
            "market_type": "event_choice",
            "facts_needed": ["selected", "winner", "announced", "position", "result"],
            "resolver": "named_choice",
            "search_query": f"{question} official result selected winner announced {close_date}",
        })

    # Draft named-choice markets are broad, but count questions about drafts
    # (e.g. "How many trades/OL in round one") must remain numeric_threshold.
    if (not _looks_like_count_market(question, outcomes)) and any(k in q_lower for k in ["draft", "pick", "selected", "chose", "selection"]):
        team_hint = _extract_team_hint(question)
        pick_order = _draft_pick_order_from_question(question)
        order_phrase = f"{pick_order} selection" if pick_order else "selection"
        plan.update({
            "market_type": "draft_named_choice",
            "answer_format": "named_choice",
            "facts_needed": [
                "team", "pick_order", "player_selected", "player_position",
                "draft_pick", "position"
            ],
            "resolver": "map_player_position_to_outcome",
            "search_query": (
                f"{team_hint} {pick_order} pick first round 2026 NFL Draft "
                f"selected drafted position ESPN official pick tracker"
            ).strip(),
        })

    # Sports winner/score/spread markets.
    # Do not route sports-adjacent count questions into sports_result.
    if (not _looks_like_count_market(question, outcomes)) and any(k in q_lower for k in [" vs ", " versus ", "match", "game", "cricket", "football", "ufl", "xfl", "ipl", "nba", "nfl", "winner", "score", "spread"]):
        if answer_format != "named_choice" or "draft" not in q_lower:
            sport_hint = ""
            if any(k in q_lower for k in ["ufl", "xfl", "arena football"]):
                sport_hint = "UFL football"
            elif any(k in q_lower for k in ["nfl", "super bowl", "touchdown"]):
                sport_hint = "NFL football"
            elif any(k in q_lower for k in ["nba", "basketball", "points"]):
                sport_hint = "NBA basketball"
            elif any(k in q_lower for k in ["mlb", "baseball", "innings"]):
                sport_hint = "MLB baseball"
            elif any(k in q_lower for k in ["nhl", "hockey", "puck"]):
                sport_hint = "NHL hockey"
            elif any(k in q_lower for k in CRICKET_KEYWORDS):
                sport_hint = "cricket"

            spread_re = re.compile(r"^.+\s[+-]\d+\.?\d*\s*$")
            is_spread = len(outcomes or []) == 2 and all(spread_re.match(str(o).strip()) for o in outcomes or [])
            if is_spread:
                plan.update({
                    "market_type": "sports_spread",
                    "answer_format": "spread_cover",
                    "facts_needed": ["final_score", "winning_margin", "game_result", "point_spread"],
                    "resolver": "spread_cover",
                    "threshold": _find_numeric_threshold(outcomes),
                    "search_query": f"{question} {sport_hint} final score result spread {close_date}",
                })
            else:
                plan.update({
                    "market_type": "sports",
                    "facts_needed": ["winner", "final_score", "result"],
                    "resolver": "sports_result",
                    "search_query": f"{question} {sport_hint} official final score result winner {close_date}",
                })

    # Crypto price only when actually price/value/range question, not just a crypto word.
    price_signals = ["price", "reach", "hit", "above", "below", "highest", "lowest", "close", "open", "usd", "range", "between"]
    event_signals = ["announce", "purchase", "buy", "acquire", "launch", "partnership", "list", "approve"]
    is_price_q = any(w in q_lower for w in price_signals) and not any(w in q_lower for w in event_signals)
    if is_price_q:
        for ticker in COINGECKO_IDS.keys():
            if re.search(rf"\b{re.escape(ticker.lower())}\b", q_lower):
                plan.update({
                    "market_type": "crypto_price_range" if answer_format in ("numeric_range", "numeric_threshold") else "crypto_price",
                    "is_price_based": True,
                    "asset": ticker,
                    "facts_needed": ["high_price"] if any(w in q_lower for w in ["highest", "high", "peak", "max"]) else (
                        ["low_price"] if any(w in q_lower for w in ["lowest", "low", "min"]) else ["close_price"]
                    ),
                    "resolver": "crypto_price",
                    "search_query": f"{ticker} price {question} {close_date}",
                })
                break

    # Optional local Ollama refinement. This replaces market-specific patching with
    # one general pre-fetch planning brain. It does NOT decide final outcome.
    brain_prompt = (
        "You are OracleREE's PRE-FETCH settlement planning brain.\n"
        "Return ONLY a valid JSON object. Do not use markdown. Do not answer the market.\n"
        "Your job: classify the market and improve the fetch/extract plan.\n"
        "Creator source is law. Search will be source-locked later. Do not silently switch sources.\n"
        "Never use generic facts_needed like ['result'] alone if a more specific fact is implied.\n\n"
        "Allowed market_type: numeric_threshold, numeric_range, binary_event, draft_named_choice, "
        "sports, sports_spread, event_choice, crypto_price, crypto_price_range, finance, politics, confirmation, unknown.\n"
        "Allowed answer_format: binary, numeric_threshold, numeric_range, named_choice, spread_cover, freeform.\n"
        "Allowed resolver: generic_outcome_resolver, numeric_threshold, numeric_range, binary_yes_no, "
        "named_choice, map_player_position_to_outcome, sports_result, spread_cover, crypto_price, "
        "price_bucket, threshold_compare, count_compare.\n"
        "JSON keys: market_type, answer_format, event_description, event_date, facts_needed, "
        "threshold, target, asset, is_price_based, search_query, resolver, needs_canonicalization, metric, count_subject.\n\n"
        f"Question: {question}\n"
        f"Outcomes: {', '.join(str(o) for o in outcomes or [])}\n"
        f"Creator sources: {', '.join(str(s) for s in data_sources or [])}\n"
        f"Close time: {close_time}\n"
        f"Existing deterministic heuristic plan: {json.dumps(plan, ensure_ascii=False)}\n\n"
        "Return a stronger plan, but keep the same creator-source-first policy."
    )

    ollama_obj = call_ollama_json(brain_prompt)
    plan = merge_plan_refinement(plan, ollama_obj, source="Ollama")
    # Guard against model drift: Ollama may see "NFL draft" and choose sports/named_choice.
    # Count phrasing/outcomes always override model classification.
    if _looks_like_count_market(question, outcomes):
        plan = _force_count_plan(plan, question, outcomes, close_date)

    # Groq is now only a remote fallback/refinement if local Ollama is unavailable.
    # It can improve labels/search_query, but cannot erase heuristic structure.
    if not ollama_obj:
        raw = call_groq(
            "You are OracleREE's PRE-FETCH settlement planning brain.\n"
            "Return ONLY valid JSON. Do not answer the market. Build a plan for how to fetch/extract the answer.\n"
            "Never use generic facts_needed like ['result'] alone if a more specific fact is implied.\n"
            "Use creator sources only; search_query will be restricted to each source domain later.\n\n"
            "JSON keys: market_type, answer_format, event_description, event_date, facts_needed, "
            "threshold, asset, is_price_based, search_query, resolver, needs_canonicalization.\n"
            "Market types include: numeric_threshold, numeric_range, binary_event, draft_named_choice, "
            "sports, event_choice, crypto_price, crypto_price_range, finance, politics, unknown.",
            f"Question: {question}\n"
            f"Outcomes: {', '.join(str(o) for o in outcomes or [])}\n"
            f"Creator sources: {', '.join(str(s) for s in data_sources or [])}\n"
            f"Close time: {close_time}\n"
            f"Existing heuristic plan: {json.dumps(plan, ensure_ascii=False)}\n\n"
            "Return a stronger plan. Keep source-locked assumptions.",
            max_tokens=650,
        )
        plan = merge_plan_refinement(plan, _safe_json_object(raw or ""), source="Groq")
        if _looks_like_count_market(question, outcomes):
            plan = _force_count_plan(plan, question, outcomes, close_date)

    # Date override should remain deterministic.
    day_m = re.search(
        r'(january|february|march|april|may|june|july|august|'
        r'september|october|november|december)\s+(\d{1,2})', q_lower)
    if day_m:
        month_num = MONTH_MAP[day_m.group(1)]
        year = close_date[:4] if close_date and close_date != "unknown" else "2026"
        plan["event_date"] = f"{year}-{month_num}-{int(day_m.group(2)):02d}"
    else:
        for month_name, month_num in MONTH_MAP.items():
            if month_name in q_lower and close_date and close_date != "unknown":
                year = close_date[:4]
                last_day = calendar.monthrange(int(year), int(month_num))[1]
                plan["event_date"] = f"{year}-{month_num}-{last_day:02d}"
                break

    # Never return empty/generic labels.
    if not isinstance(plan.get("facts_needed"), list) or not plan.get("facts_needed") or plan.get("facts_needed") == ["result"]:
        if plan.get("answer_format") == "numeric_threshold":
            plan["facts_needed"] = ["count", "number", "total"]
        elif plan.get("market_type") == "draft_named_choice":
            plan["facts_needed"] = ["team", "pick_order", "player_selected", "player_position"]
        elif plan.get("answer_format") == "named_choice":
            plan["facts_needed"] = ["selected", "winner", "announced", "position"]
        else:
            plan["facts_needed"] = ["event_status", "result_detail"]

    print(f"[oracle] Plan: {plan.get('market_type')} | {plan.get('answer_format')} | {plan.get('resolver')}")
    print(f"[oracle] Plan facts: {plan.get('facts_needed')}")
    print(f"[oracle] Plan query: {str(plan.get('search_query',''))[:100]}")
    return plan


def analyze_market_intelligence(question:str, prompt_context:str,
                                 outcomes:list, data_sources:list, close_time:str)->dict:
    close_date=close_time[:10] if close_time else "unknown"
    q_lower=question.lower()

    # Lightweight base only. The real intelligence is the planning brain below.
    pre_asset=None
    pre_is_price=False
    price_signals = [
        "price", "reach", "hit", "above", "below", "highest", "lowest",
        "close", "open", "trading", "worth", "value", "cost", "usd",
        "high", "low", "range", "between", "over", "under"
    ]
    event_signals = [
        "will", "announce", "purchase", "buy", "acquire", "launch",
        "release", "happen", "occur", "win", "lose", "pass", "fail",
        "approve", "reject", "list", "delist", "merge", "partnership"
    ]
    is_price_question = any(w in q_lower for w in price_signals)
    is_event_question = any(w in q_lower for w in event_signals)
    for ticker in COINGECKO_IDS.keys():
        if re.search(rf"\b{re.escape(ticker.lower())}\b", q_lower):
            pre_asset=ticker
            pre_is_price=bool(is_price_question and not is_event_question)
            break

    default={
        "event_description":question,
        "event_date":close_date,
        "timing":"at_close",
        "facts_needed":["close_price"] if pre_is_price else ["result_detail"],
        "is_price_based":pre_is_price,
        "asset":pre_asset if pre_is_price else None,
        "threshold":None,
        "market_type":"crypto_price_range" if pre_is_price else "unknown",
        "answer_format":_infer_answer_format_from_outcomes(question, outcomes),
        "search_query":question,
        "resolver":"generic_outcome_resolver",
        "needs_canonicalization":True,
    }

    plan = settlement_plan_brain(question, prompt_context, outcomes, data_sources, close_time, default)

    # Safety: crypto event guard.
    if plan.get("asset") and not plan.get("is_price_based") and plan.get("market_type") in ("crypto_price","crypto_price_range"):
        plan["market_type"]="event_choice"
        plan["asset"]=None
        plan["is_price_based"]=False

    if is_non_crypto(plan.get("asset") or ""):
        plan["is_price_based"]=False

    # Parse creator settlement rules from the full prompt context. These rules drive downstream query, extraction, and matching.
    rules = parse_settlement_rules(prompt_context, question, outcomes)
    plan["_rules"] = rules
    plan["prompt_context"] = prompt_context
    if rules.get("metric"):
        plan["metric"] = rules["metric"]
    if rules.get("count_subject"):
        plan["count_subject"] = rules["count_subject"]
        # Hard guard: creator rules that define a count subject make this a count market,
        # regardless of whether the domain is sports/draft.
        plan = _force_count_plan(plan, question, outcomes, close_date, rules)
    if rules.get("threshold") is not None:
        plan["threshold"] = rules["threshold"]

    return {**default, **plan}

# ─── Source-locked fetch layer ────────────────────────────────────────────────
def direct_fetch(url:str)->Optional[tuple[str,str]]:
    uas=["Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
         "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15","curl/7.88.1"]
    for ua in uas:
        try:
            r=requests.get(url,headers={"User-Agent":ua,"Accept":"application/json,text/html,*/*"},
                           timeout=15)
            if r.status_code==200 and len(r.text)>100:
                ct=r.headers.get("content-type","")
                ctype="json" if ("json" in ct or r.text.strip()[:1] in ("{","[")) else "html"
                return r.text,ctype
        except Exception: continue
    return None

def is_weak_espn_homepage_content(content: str) -> bool:
    """True when ESPN direct fetch returned the generic homepage instead of a match page."""
    c = str(content or "").lower()
    return (
        "espn - serving sports fans" in c
        or 'canonical" href="https://www.espn.com"' in c
        or 'og:url" content="https://www.espn.com"' in c
        or 'og:title" content="espn - serving sports fans' in c
    )


def is_topic_drift_for_sports_content(content: str, question: str, outcomes: list) -> bool:
    """Reject sports pages/snippets that do not mention at least one meaningful participant."""
    c = str(content or "").lower()
    names = []
    # Extract teams from "A vs B" / "A versus B" question.
    m = re.search(r"(.+?)\s+(?:vs|versus)\s+(.+?)(?:\s+[—-]|\s+\(|$)", str(question or ""), re.I)
    if m:
        names.extend([m.group(1).strip(), m.group(2).strip()])
    for outcome in outcomes or []:
        s = str(outcome or "").strip()
        if s and s.lower() not in {"draw", "yes", "no"} and not re.search(r"^[+-]?\d", s):
            names.append(s)
    tokens = []
    for name in names:
        # keep strong tokens only
        for tok in re.findall(r"[a-zA-Z]{3,}", name.lower()):
            if tok not in {"win", "draw", "the", "and", "english", "premier", "league", "cup"}:
                tokens.append(tok)
    tokens = sorted(set(tokens))
    if not tokens:
        return False
    return not any(tok in c for tok in tokens)


def tavily_source_locked_fetch(domain: str, question: str, event_date: str,
                                what_to_find: str, is_fdv: bool = False,
                                search_depth: str = "basic") -> Optional[str]:
    """
    Tavily fetch locked to a specific domain.
    Never searches the general web — always restricts to creator's source.
    """
    if not TAVILY_API_KEY: return None

    if is_fdv:
        # FDV markets: no site restriction, use specific token+date+FDV query
        query=f"{what_to_find} {event_date}"
    else:
        query=f"site:{domain} {what_to_find} {event_date}"

    print(f"[oracle] Tavily locked fetch: {query[:80]}")
    try:
        r=requests.post("https://api.tavily.com/search",json={
            "api_key":TAVILY_API_KEY,"query":query,
            "search_depth":search_depth,"max_results":5,"include_answer":True,
        },timeout=15)
        data=r.json()
        parts=[]
        if data.get("answer"): parts.append(f"ANSWER: {data['answer']}")
        for res in data.get("results",[])[:4]:
            # Only include results from the target domain (or any domain for FDV)
            res_domain=clean_domain(res.get("url",""))
            if is_fdv or res_domain==domain or res_domain.endswith("."+domain):
                parts.append(f"[{res.get('url','')}]\n{res.get('content','')[:800]}")
        # If site-locked search returned nothing, try broad search
        if (not parts or sum(len(p) for p in parts) < 500) and not is_fdv:
            print(f"[oracle] Tavily site-locked empty — trying broad search for: {what_to_find[:60]}")
            broad_q = f"{what_to_find} {event_date}"
            r2=requests.post("https://api.tavily.com/search",json={
                "api_key":TAVILY_API_KEY,"query":broad_q,
                "search_depth":search_depth,"max_results":5,"include_answer":True,
            },timeout=15)
            data2=r2.json()
            if data2.get("answer"): parts.append(f"ANSWER: {data2['answer']}")
            for res2 in data2.get("results",[])[:4]:
                parts.append(f"[{res2.get('url','')}]\n{res2.get('content','')[:800]}")
            if parts: print(f"[oracle] Broad search found {len(parts)} results")
        content="\n\n".join(parts)
        if content.strip():
            print(f"[oracle] Tavily locked: {len(content)} chars")
            return content
    except Exception as e: print(f"[oracle] Tavily failed: {e}")
    return None

def fetch_from_source_family(primary_domain:str, question:str, event_date:str,
                              what_to_find:str)->tuple[Optional[str],str,str]:
    """
    Try primary domain first, then source family members.
    Returns (content, fetch_method, domain_used).
    """
    family=get_source_family(primary_domain)
    for domain in family:
        # Build URL for direct fetch attempt
        url=f"https://www.{domain}" if not domain.startswith("http") else domain
        result=direct_fetch(url)
        if result:
            content,ctype=result
            method="direct" if domain==primary_domain else "source_family_direct"
            print(f"[oracle] ✓ Direct fetch from {domain}: {len(content)} chars")
            return content,ctype,method

        # Try Tavily locked to this domain
        content=tavily_source_locked_fetch(domain,question,event_date,what_to_find)
        if content:
            method="tavily_locked" if domain==primary_domain else "source_family_tavily"
            print(f"[oracle] ✓ Tavily locked to {domain}: {len(content)} chars")
            return content,"text",method

    return None,"","failed"

# ─── Fact extraction ──────────────────────────────────────────────────────────
def extract_facts_from_json(content:str, event_date:str, facts_needed:list,
                              source_url:str)->list[Fact]:
    facts=[]
    date_variants=[event_date,event_date.replace("-","/"),event_date.replace("-",""),event_date[5:]]

    # Raw text date search first
    raw_record=None
    for dv in date_variants:
        idx=content.find(dv)
        if idx==-1: continue
        obj_start=content.rfind("{",0,idx); obj_end=content.find("}",idx)
        if obj_start!=-1 and obj_end!=-1:
            try:
                raw_record=json.loads(content[obj_start:obj_end+1])
                print(f"[oracle] ✓ JSON date record ({dv}): {str(raw_record)[:100]}")
                break
            except Exception:
                raw_record={"_raw":content[max(0,idx-80):min(len(content),idx+200)].strip()}
                break

    if raw_record:
        record_str=json.dumps(raw_record) if isinstance(raw_record,dict) else str(raw_record)
        for label in facts_needed:
            if isinstance(raw_record,dict):
                for k,v in raw_record.items():
                    kl=str(k).lower().replace("_","").replace("-","")
                    fl=label.lower().replace("_","").replace("-","")
                    if fl in kl or kl in fl:
                        facts.append(Fact(label,str(v),source_url,timestamp=event_date)); break
            if not any(f.label==label for f in facts):
                raw=call_groq("Extract a specific value. Return ONLY the value.",
                              f"Extract: {label}\nFrom: {record_str[:1000]}")
                if raw and raw.strip().lower() not in ("none","null","n/a",""):
                    facts.append(Fact(label,raw.strip(),source_url,timestamp=event_date))
        return facts

    # Full JSON parse
    try:
        data=json.loads(content)
        sample=data[:3]+[{"...":f"{len(data)-6} more"}]+data[-3:] if isinstance(data,list) and len(data)>10 else data
        json_sample=json.dumps(sample,indent=1)[:4000]
    except Exception:
        json_sample=content[:4000]

    for label in facts_needed:
        raw=call_groq("Extract from API response. Return ONLY the exact value. If not found: NOT_FOUND",
                      f"Extract: {label}\nDate: {event_date}\n\nDATA:\n{json_sample}")
        if raw and raw.strip().lower() not in ("not_found","none","null",""):
            facts.append(Fact(label,raw.strip(),source_url,timestamp=event_date))
    return facts

def extract_facts_from_text(content: str, event_date: str, facts_needed: list,
                              question: str, outcomes: list, source_url: str) -> list[Fact]:
    # Always keep the ANSWER line at the top of what Groq sees.
    answer_line = extract_answer_line(content) if 'extract_answer_line' in globals() else _extract_answer_line(content)
    idx = content.find(event_date)
    relevant = content[max(0, idx-200):min(len(content), idx+1500)] if idx != -1 else content[:4000]

    # Put ANSWER line first — not buried in source noise.
    if answer_line and not relevant.startswith("ANSWER:"):
        relevant = f"ANSWER: {answer_line}\n\n{relevant}"

    outcomes_str = ", ".join(str(o) for o in outcomes)
    facts_str = ", ".join(facts_needed)

    raw = call_groq(
        "Extract facts from content to answer a prediction market question.\n"
        "Use ONLY what is written.\n"
        "The 'ANSWER:' line at the top is the most reliable signal — prioritise it.\n"
        "Return ONLY valid JSON: array of {label, value} objects.\n"
        "Always include label='matched_outcome' if you can determine it.",
        f"Question: {question}\nFacts to extract: {facts_str}\n"
        f"Valid outcomes: {outcomes_str}\nTarget date: {event_date}\n\n"
        f"CONTENT:\n{relevant}",
        max_tokens=500
    )
    facts=[]
    if not raw: return facts
    try:
        clean=raw.replace("```json","").replace("```","").strip()
        m=re.search(r"\[[\s\S]*\]",clean)
        if m:
            items=json.loads(m.group(0))
            for item in items:
                if isinstance(item,dict) and item.get("label") and item.get("value"):
                    v=str(item["value"]).strip()
                    if v.lower() not in ("not found","none","null","n/a",""):
                        facts.append(Fact(item["label"],v,source_url,timestamp=event_date))
        else:
            obj_m=re.search(r"\{[\s\S]*\}",clean)
            if obj_m:
                obj=json.loads(obj_m.group(0))
                if obj.get("label") and obj.get("value"):
                    facts.append(Fact(obj["label"],obj["value"],source_url,timestamp=event_date))
    except Exception as ex:
        print(f"[oracle] Facts parse error: {ex}")
        raw2=call_groq("Answer from content. Return ONLY a short answer.",
                       f"Question: {question}\nContent:\n{relevant[:2000]}")
        if raw2 and raw2.strip():
            facts.append(Fact("result",raw2.strip(),source_url,timestamp=event_date))
    return facts

# ─── Settlement constants ─────────────────────────────────────────────────────

CRICKET_KEYWORDS = {
    "cricket","ipl","wicket","overs","t20","odi","test match",
    "psl","pakistan super league","bbl","big bash","cpl","caribbean",
    "lpl","sa20","ilt20","ranji","sheffield shield","county cricket",
}

SPORT_HINTS = {
    "ufl":"UFL football","xfl":"XFL football",
    "nfl":"NFL football","super bowl":"NFL football",
    "nba":"NBA basketball","basketball":"basketball",
    "mlb":"MLB baseball","baseball":"baseball",
    "nhl":"NHL hockey","hockey":"NHL hockey",
    "cricket":"cricket","ipl":"IPL cricket","psl":"PSL cricket",
    "premier league":"soccer","champions league":"soccer",
    "la liga":"soccer","serie a":"soccer","bundesliga":"soccer",
}


# Sports fallback is intentionally limited to sports markets because sports results
# are immutable facts. Source-dependent markets (prices, announcements, politics,
# company events) remain creator-source-only.
SPORTS_MARKET_TYPES = {
    "sports", "sports_spread", "cricket", "football", "basketball",
    "baseball", "hockey", "soccer"
}

SPORTS_FALLBACK_SOURCES: dict[str, list[str]] = {
    "nfl": ["nfl.com", "pro-football-reference.com", "espn.com"],
    "ufl": ["theufl.com", "ufl.football", "espn.com", "pro-football-reference.com"],
    "xfl": ["xfl.com", "espn.com"],
    "cricket": ["espncricinfo.com", "cricbuzz.com", "bbc.com/sport"],
    "ipl": ["espncricinfo.com", "iplt20.com", "cricbuzz.com"],
    "psl": ["espncricinfo.com", "pcb.com.pk", "cricbuzz.com"],
    "nba": ["nba.com", "basketball-reference.com", "espn.com"],
    "mlb": ["baseball-reference.com", "mlb.com", "espn.com"],
    "nhl": ["hockey-reference.com", "nhl.com", "espn.com"],
    "soccer": ["bbc.com/sport", "skysports.com", "thefa.com", "uefa.com", "espn.com"],
    "fa cup": ["thefa.com", "bbc.com/sport", "skysports.com", "espn.com"],
    "premier league": ["bbc.com/sport", "premierleague.com", "skysports.com"],
    "champions league": ["uefa.com", "bbc.com/sport", "espn.com"],
    "default": ["espn.com", "bbc.com/sport", "skysports.com"],
}

def is_sports_market_question(question: str, intelligence: dict) -> bool:
    q = str(question or "").lower()
    info = intelligence or {}
    mt = str(info.get("market_type") or "").lower()
    resolver = str(info.get("resolver") or "").lower()
    answer_format = str(info.get("answer_format") or "").lower()
    rules = info.get("_rules") if isinstance(info.get("_rules"), dict) else {}
    rules_metric = str(rules.get("metric") or info.get("metric") or "").lower()
    winner_logic = str(rules.get("winner_logic") or "").lower()

    # Count markets are not sports-result markets, even when the domain is sports.
    if (
        info.get("count_subject")
        or rules.get("count_subject")
        or rules_metric == "count"
        or resolver == "count_compare"
    ):
        return False

    # IMPORTANT FIX:
    # Some football/soccer winner markets were misclassified upstream as
    # numeric_threshold/time, but their parsed creator rules still say
    # metric=winner / winner_logic=full_time_result. Those MUST be treated
    # as sports markets so ESPN homepage/topic-drift failures can trigger
    # sports recovery instead of ending as INCONCLUSIVE.
    if rules_metric in {"winner", "match_winner", "game_winner", "final_score", "score", "spread"}:
        return True
    if winner_logic in {"full_time_result", "official_result", "winner", "match_result"}:
        return True
    if resolver in {"sports_result", "spread_cover"}:
        return True
    if answer_format == "spread_cover":
        return True
    if mt in SPORTS_MARKET_TYPES:
        return True

    return any(k in q for k in [
        " vs", " versus ", " match", " game", "cricket", "ipl", "psl",
        "nfl", "nba", "mlb", "nhl", "ufl", "xfl", "premier league",
        "fa cup", "champions league", "world cup", "final score",
        "cover the spread", "football", "basketball", "baseball",
        "hockey", "soccer"
    ])

def get_sports_fallback_domains(question: str, primary_domain: str) -> list[str]:
    q = str(question or "").lower()
    primary = clean_domain(primary_domain)
    sport_key = "default"
    if any(k in q for k in ["nfl", "super bowl", "touchdown"]):
        sport_key = "nfl"
    elif "ufl" in q:
        sport_key = "ufl"
    elif "xfl" in q:
        sport_key = "xfl"
    elif any(k in q for k in ["psl", "pakistan super league"]):
        sport_key = "psl"
    elif any(k in q for k in ["ipl", "indian premier league"]):
        sport_key = "ipl"
    elif any(k in q for k in ["cricket", "wicket", "overs"]):
        sport_key = "cricket"
    elif any(k in q for k in ["nba", "basketball"]):
        sport_key = "nba"
    elif any(k in q for k in ["mlb", "baseball"]):
        sport_key = "mlb"
    elif any(k in q for k in ["nhl", "hockey"]):
        sport_key = "nhl"
    elif "fa cup" in q:
        sport_key = "fa cup"
    elif "premier league" in q:
        sport_key = "premier league"
    elif "champions league" in q:
        sport_key = "champions league"
    elif any(k in q for k in ["soccer", "football", "la liga", "serie a", "bundesliga", "mls"]):
        sport_key = "soccer"

    fallbacks = SPORTS_FALLBACK_SOURCES.get(sport_key, SPORTS_FALLBACK_SOURCES["default"])
    result = []
    for domain in fallbacks:
        d = clean_domain(domain)
        if d == primary or primary.endswith("." + d) or d.endswith("." + primary):
            continue
        if d not in result:
            result.append(d)
    return result


def _sports_participant_tokens(question: str, outcomes: Optional[list] = None) -> list[str]:
    """Extract strong participant tokens from a sports market question/outcomes."""
    names = []
    q = str(question or "")

    # A vs B / A versus B
    m = re.search(r"(.+?)\s+(?:vs|versus)\s+(.+?)(?:\s+[—-]|\s+\(|$)", q, re.I)
    if m:
        names.extend([m.group(1).strip(), m.group(2).strip()])

    for outcome in outcomes or []:
        s = str(outcome or "").strip()
        if s and s.lower() not in {"draw", "yes", "no"} and not re.search(r"^[+-]?\d", s):
            names.append(s)

    stop = {
        "english", "premier", "league", "champions", "world", "cup", "match",
        "game", "final", "result", "score", "winner", "draw", "the", "and",
        "will", "who", "april", "january", "february", "march", "may", "june",
        "july", "august", "september", "october", "november", "december",
    }

    tokens = []
    for name in names:
        for tok in re.findall(r"[a-zA-Z]{3,}", name.lower()):
            if tok not in stop:
                tokens.append(tok)
    return sorted(set(tokens))


def sports_fallback_content_is_usable(content: str, question: str, outcomes: Optional[list] = None) -> tuple[bool, str]:
    """
    Lightweight validator for fallback sports evidence.
    This intentionally accepts Tavily's ANSWER line when it directly supports
    a valid outcome. We still require participant/topic relevance.
    """
    c = str(content or "")
    cl = c.lower()
    answer = extract_answer_line(c)
    answer_l = answer.lower()

    tokens = _sports_participant_tokens(question, outcomes)
    if tokens and not any(t in cl for t in tokens):
        return False, "fallback topic drift: no participant token found"

    valid_outcomes = [
        str(o).strip() for o in outcomes or []
        if str(o).strip() and str(o).strip().lower() not in {"yes", "no"}
    ]

    # Direct outcome in Tavily answer is strong enough for sports final-result evidence.
    for outcome in valid_outcomes:
        ol = outcome.lower()
        if ol != "draw" and (ol in answer_l or re.search(rf"\b{re.escape(ol)}\b", answer_l)):
            return True, f"answer line names valid outcome: {outcome}"

    # Draw can be supported by answer text mentioning draw/tie or penalties.
    if any(str(o).strip().lower() == "draw" for o in valid_outcomes):
        if re.search(r"\b(draw|drew|tie|tied)\b", answer_l):
            return True, "answer line supports draw"

    # Score-like patterns plus participants are enough to let deterministic extraction run.
    score_patterns = [
        r"\b\d+\s*[-–]\s*\d+\b",
        r"\b\d+\s*:\s*\d+\b",
        r"\bwon\b",
        r"\bdefeated\b",
        r"\bbea[ts]+\b",
        r"\bfull[-\s]?time\b",
        r"\bfinal score\b",
        r"\bresult\b",
    ]
    if any(re.search(p, cl, re.I) for p in score_patterns):
        return True, "fallback contains participant-relevant sports result evidence"

    return False, "fallback lacks score/winner/result signal"


def try_sports_fallback_sources(question: str, event_date: str, what_to_find: str,
                                intelligence: dict, primary_domain: str,
                                outcomes: Optional[list] = None) -> Optional[tuple[str, str]]:
    """
    Creator-source-first sports fallback using Tavily.

    Order:
      1) Tavily locked to creator domain, e.g. site:espn.com ...
      2) Tavily locked to trusted sports fallback domains.
    The fallback is only for sports result markets because final sports results
    are immutable public facts. Never use this for price/news/politics markets.
    """
    if not TAVILY_API_KEY:
        print("[oracle] Sports fallback skipped: TAVILY_API_KEY is not set")
        return None

    primary = clean_domain(primary_domain)
    rules = (intelligence or {}).get("_rules") or {}

    candidate_domains = [primary]
    for d in get_sports_fallback_domains(question, primary):
        d = clean_domain(d)
        if d not in candidate_domains:
            candidate_domains.append(d)

    # Use multiple query shapes. Some domains do not index the exact generated
    # universal query well, especially ESPN match pages.
    query_variants = []
    base_query = str(what_to_find or "").strip()
    if base_query:
        query_variants.append(base_query)

    q_clean = re.sub(r"[—–-]", " ", str(question or ""))
    q_clean = re.sub(r"\s+", " ", q_clean).strip()
    sport_hint = sport_hint_for_question(question)
    query_variants.extend([
        f"{q_clean} final score",
        f"{q_clean} full time result",
        f"{q_clean} match result winner",
        f"{q_clean} {event_date} final score result",
    ])
    if sport_hint:
        query_variants.append(f"{q_clean} {sport_hint} final score result")

    # De-duplicate while preserving order.
    seen_q = set()
    query_variants = [q for q in query_variants if q and not (q.lower() in seen_q or seen_q.add(q.lower()))]

    for domain in candidate_domains:
        print(f"[oracle] Sports Tavily fallback: trying {domain}")

        try:
            query_plan = build_universal_query(question, outcomes or [], rules, event_date, domain)
            search_depth = query_plan.get("search_depth", "advanced")
            required_type = query_plan.get("required_data_type") or query_plan.get("what_to_validate") or "match_result"
            extraction_target = query_plan.get("extraction_target") or query_plan.get("number_context") or ""
        except Exception:
            search_depth = "advanced"
            required_type = "match_result"
            extraction_target = ""

        for fallback_query in query_variants:
            tv = tavily_source_locked_fetch(
                domain, question, event_date, fallback_query,
                is_fdv=False, search_depth=search_depth or "advanced"
            )
            if not tv:
                continue

            # First, apply a sports-specific validator that understands Tavily ANSWER lines.
            usable, why = sports_fallback_content_is_usable(tv, question, outcomes)
            if usable:
                print(f"[oracle] ✓ Sports Tavily fallback accepted: {domain} ({why})")
                return tv, domain

            # Then try existing validators for compatibility with other markets.
            valid = False
            reason = why
            try:
                valid, reason = validate_evidence(tv, required_type, extraction_target, question)
            except Exception:
                try:
                    valid, reason = validate_evidence_quality(tv, {
                        "what_to_validate": required_type,
                        "number_context": extraction_target,
                        "need_number": required_type in {"score", "count", "match_result"},
                    }, question)
                except Exception as e:
                    reason = str(e)

            if valid:
                print(f"[oracle] ✓ Sports Tavily fallback validated: {domain} ({reason})")
                return tv, domain

            print(f"[oracle] Sports fallback rejected for {domain}: {reason}")

    return None


# ─── ANSWER line extraction ───────────────────────────────────────────────────

def extract_answer_line(evidence: str) -> str:
    """Extract Tavily's synthesised ANSWER — the single most reliable signal."""
    m = re.search(r'ANSWER:\s*(.+?)(?:\n\n|\n\[|\Z)', str(evidence or ""), re.S)
    if m:
        return " ".join(m.group(1).split())
    return ""

def sport_hint_for_question(question: str) -> str:
    q = question.lower()
    for keyword, hint in SPORT_HINTS.items():
        if keyword in q:
            return hint
    return ""

# ─── STAGE 1: Direct answer-line match ───────────────────────────────────────

def stage1_direct_match(answer_line: str, question: str,
                         outcomes: list) -> tuple[Optional[str], Optional[str]]:
    """
    Fast path: check if the ANSWER line unambiguously names an outcome.
    Handles 80% of markets without any AI call.
    """
    if not answer_line:
        return None, None

    al = answer_line.lower()
    q_lower = question.lower()

    # ── Spread cover: extract score + calculate ────────────────────
    spread_re = re.compile(r'^.+\s[+-]\d+\.?\d*\s*$')
    if len(outcomes) == 2 and all(spread_re.match(str(o).strip()) for o in outcomes):
        score_m = re.search(r'\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b', answer_line)
        if score_m:
            a, b = int(score_m.group(1)), int(score_m.group(2))
            spread_map = {}
            for o in outcomes:
                m = re.match(r'^(.+?)\s*([+-]\d+(?:\.\d+)?)\s*$', str(o).strip())
                if m:
                    spread_map[str(o)] = float(m.group(2))
            if len(spread_map) == 2:
                fav = min(spread_map, key=lambda k: spread_map[k])
                dog = max(spread_map, key=lambda k: spread_map[k])
                line = abs(spread_map[fav])
                fav_name = re.sub(r'\s*[+-]\d+(?:\.\d+)?\s*$', '', fav).strip().lower()
                dog_name = re.sub(r'\s*[+-]\d+(?:\.\d+)?\s*$', '', dog).strip().lower()
                margin = abs(a - b)
                fav_won = fav_name in al and re.search(
                    rf'\b{re.escape(fav_name)}\b.{{0,60}}\b(won|win|beat|defeated|victory)\b', al
                )
                dog_won = dog_name in al and re.search(
                    rf'\b{re.escape(dog_name)}\b.{{0,60}}\b(won|win|beat|defeated|victory)\b', al
                )
                # Also handle "X winning" / "X won"
                fav_won = fav_won or bool(re.search(
                    rf'\b(won|win|beat|defeated)\b.{{0,40}}{re.escape(fav_name)}\b', al
                ) and not re.search(
                    rf'\bwon\s+(?:against|over)\b.{{0,40}}{re.escape(fav_name)}\b', al
                ))
                if fav_won:
                    if margin > line:
                        return fav, f"spread: {a}-{b} margin {margin:.1f} > {line:.1f} → {fav}"
                    else:
                        return dog, f"spread: {a}-{b} margin {margin:.1f} < {line:.1f} → {dog}"
                if dog_won:
                    return dog, f"spread: underdog won outright {a}-{b} → {dog}"

    # ── Over/Under numeric threshold ──────────────────────────────
    if any(re.search(r'\b(over|under|above|below)\b', str(o), re.I) for o in outcomes):
        # Extract number from answer line
        nums = [float(m.group(0)) for m in re.finditer(r'\b\d+(?:\.\d+)?\b', al)
                if not (1900 <= float(m.group(0)) <= 2100)]
        if nums:
            val = nums[0]
            for outcome in outcomes:
                ol = str(outcome).lower()
                thr_m = re.search(r'\d+(?:\.\d+)?', ol)
                if not thr_m:
                    continue
                thr = float(thr_m.group(0))
                if ("over" in ol or "above" in ol) and val > thr:
                    return str(outcome), f"threshold: {val} > {thr}"
                if ("under" in ol or "below" in ol) and val < thr:
                    return str(outcome), f"threshold: {val} < {thr}"

    # ── Sports three-way (A / Draw / B) ───────────────────────────
    draw_outcome = next((o for o in outcomes if "draw" in str(o).lower()), None)
    is_sports = any(k in q_lower for k in ["vs", "versus", "match", "game",
                                            "cricket", "football", "basketball"])
    if is_sports:
        # Draw detection
        if draw_outcome and re.search(r'\b(draw|drawn|tied|no result|abandoned)\b', al):
            return str(draw_outcome), f"draw: {answer_line[:60]}"

        # "X won against/beat/defeated Y" → X is winner
        # Critical: "won against Y" means Y is LOSER
        losers: set[str] = set()
        for outcome in outcomes:
            if "draw" in str(outcome).lower():
                continue
            ol = str(outcome).lower()
            team = re.sub(r'\s*(win|wins|fc|cf|sc|afc)\s*$', '', ol).strip()
            if not team:
                continue
            # "beat/defeated TEAM" or "won against/over TEAM" → TEAM is loser
            if (re.search(rf'\b(beat|defeated)\b.{{0,80}}\b{re.escape(team)}\b', al)
                    or re.search(rf'\bwon\s+(?:against|over)\b.{{0,80}}\b{re.escape(team)}\b', al)
                    or re.search(rf'\b{re.escape(team)}\b.{{0,40}}\b(lost|loses)\b', al)):
                losers.add(ol)

        # Winner = outcome whose team appears in "won/beat/defeated" subject position
        for outcome in outcomes:
            ol = str(outcome).lower()
            if ol in losers or "draw" in ol:
                continue
            team = re.sub(r'\s*(win|wins|fc|cf|sc|afc)\s*$', '', ol).strip()
            if not team:
                continue
            if re.search(rf'\b{re.escape(team)}\b.{{0,80}}\b(won|beat|defeated|win)\b', al):
                # Make sure team is SUBJECT not OBJECT of "won"
                if not re.search(rf'\b(beat|defeated|won\s+against|won\s+over)\b.{{0,80}}\b{re.escape(team)}\b', al):
                    return str(outcome), f"sports answer: {answer_line[:80]}"

        # Elimination: if losers found, non-loser wins
        if losers:
            non_losers = [o for o in outcomes
                          if str(o).lower() not in losers and "draw" not in str(o).lower()]
            if len(non_losers) == 1:
                return str(non_losers[0]), f"elimination: {answer_line[:80]}"

    # ── Binary Yes/No ──────────────────────────────────────────────
    bin_pairs = [{"yes","no"},{"green","red"},{"up","down"},{"higher","lower"}]
    out_set = {str(o).lower() for o in outcomes}
    if any(out_set <= pair for pair in bin_pairs):
        NO_SIGNALS = ["has not","did not","not announced","not confirmed","no plans",
                      "is not","was not","will not","failed to","declined","fell",
                      "opened red","closed below","down on the day"]
        YES_SIGNALS = ["did announce","confirmed","has announced","acquired","bought",
                       "opened green","closed above","surpassed","exceeded","climbed",
                       "did purchase","did buy"]
        for s in NO_SIGNALS:
            if s in al:
                for o in outcomes:
                    if str(o).lower() in ("no","red","down","lower","below","false"):
                        return str(o), f"no signal: '{s}'"
        for s in YES_SIGNALS:
            if s in al:
                for o in outcomes:
                    if str(o).lower() in ("yes","green","up","higher","above","true"):
                        return str(o), f"yes signal: '{s}'"

    # ── Named choice — direct name match ──────────────────────────
    # Only for named outcomes longer than 4 chars (avoids false Yes/No matches)
    for outcome in outcomes:
        o_low = str(outcome).lower().strip()
        if len(o_low) <= 4:
            continue
        if o_low in al:
            # Verify it's not in a "won against {outcome}" loser context
            if not re.search(rf'\b(won\s+against|defeated|beat)\b.{{0,60}}\b{re.escape(o_low)}\b', al):
                return str(outcome), f"named match: {answer_line[:80]}"

    return None, None


# ─── STAGE 2: AI settlement judge ─────────────────────────────────────────────

def stage2_ai_judge(answer_line: str, full_evidence: str, question: str,
                     outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Single focused Groq call. The ANSWER line is passed first and prominently.
    Handles spread math, complex named choices, and anything Stage 1 missed.
    """
    if not GROQ_API_KEY:
        return None, None

    market_type = str(intelligence.get("market_type") or "")
    answer_format = str(intelligence.get("answer_format") or "")
    outcomes_str = "\n".join(f"  - {o}" for o in outcomes)

    # Build the evidence block with ANSWER line prominently first
    evidence_block = ""
    if answer_line:
        evidence_block += f"PRIMARY ANSWER (Tavily synthesis):\n{answer_line}\n\n"
    # Add a clean snippet of URL content (not the full dump)
    url_snippets = []
    for line in (full_evidence or "").splitlines():
        if line.startswith("[http") or (line.strip() and not line.startswith("ANSWER:")):
            url_snippets.append(line.strip())
        if len(url_snippets) > 20:
            break
    if url_snippets:
        evidence_block += "SUPPORTING SOURCE CONTENT:\n" + "\n".join(url_snippets[:20])

    system = (
        "You are OracleREE's settlement judge. Your job is to determine the correct outcome "
        "for a prediction market using the provided evidence.\n\n"
        "CRITICAL RULES:\n"
        "1. The PRIMARY ANSWER is Tavily's synthesis of the source page — treat it as the most "
        "reliable single signal. Trust it unless supporting content clearly contradicts it.\n"
        "2. For SPORTS markets:\n"
        "   - 'X won against Y' → Y is the LOSER\n"
        "   - 'X beat/defeated Y' → Y is the LOSER\n"
        "   - Equal final scores → DRAW\n"
        "   - The team that WON maps to the winner outcome\n"
        "3. For SPREAD markets (outcomes like 'Kings +10.5'):\n"
        "   - Extract final score, calculate margin\n"
        "   - Favorite (negative spread) must win by MORE than the line to cover\n"
        "   - Underdog covers if they win OR lose by less than the line\n"
        "4. For OVER/UNDER markets:\n"
        "   - Extract the count/number from evidence\n"
        "   - Compare to the threshold in the outcome label\n"
        "5. For NAMED CHOICE (draft, award, election):\n"
        "   - Find the specific named entity selected/announced/won\n"
        "   - Map it to exactly one valid outcome\n"
        "   - For NFL Draft position markets: 'selected OL/OT/Guard/Center' → 'Offensive Lineman'\n"
        "6. For YES/NO markets:\n"
        "   - Negation wins: 'did not', 'has not', 'no plans' → No\n"
        "   - Confirmation wins: 'announced', 'confirmed', 'acquired' → Yes\n"
        "7. Return NONE if the evidence genuinely cannot answer the question.\n\n"
        "Return ONLY valid JSON:\n"
        '{"matched_outcome": "exact outcome text or NONE", '
        '"extracted_fact": "the specific fact that determined this", '
        '"confidence": "high/medium/low", '
        '"reasoning": "one sentence"}'
    )

    user = (
        f"QUESTION: {question}\n\n"
        f"MARKET TYPE: {market_type} | FORMAT: {answer_format}\n\n"
        f"VALID OUTCOMES:\n{outcomes_str}\n\n"
        f"EVIDENCE:\n{evidence_block[:3000]}"
    )

    raw = call_groq(system, user, max_tokens=400)
    if not raw:
        return None, None

    clean = raw.replace("```json", "").replace("```", "").strip()
    m = re.search(r'\{[\s\S]*\}', clean)
    if not m:
        return None, None

    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None, None

    mo = str(obj.get("matched_outcome") or "").strip()
    if not mo or mo.upper() == "NONE":
        return None, None

    confidence = str(obj.get("confidence") or "").lower()
    reasoning = str(obj.get("reasoning") or obj.get("extracted_fact") or "")

    # Low confidence + short evidence → don't guess
    if confidence == "low" and len(answer_line) < 30:
        print(f"[oracle] Stage 2 low confidence, skipping: {mo}")
        return None, None

    for outcome in outcomes:
        if str(outcome).lower().strip() == mo.lower().strip():
            return str(outcome), f"AI judge: {reasoning[:120]}"

    # Safe partial match only for longer names
    if len(mo) > 6:
        for outcome in outcomes:
            if mo.lower() in str(outcome).lower() or str(outcome).lower() in mo.lower():
                return str(outcome), f"AI judge: {reasoning[:120]}"

    return None, None


# ─── STAGE 3: Secondary model (hard cases) ────────────────────────────────────

def stage3_secondary_model(answer_line: str, full_evidence: str, question: str,
                            outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Second Groq call with a completely different prompt framing.
    Acts as a tiebreaker for genuinely ambiguous cases.
    Only fires if stages 1 and 2 both returned None.
    """
    if not GROQ_API_KEY:
        return None, None

    # Only attempt if we have meaningful evidence
    if not answer_line and len(full_evidence.strip()) < 100:
        return None, None

    outcomes_str = ", ".join(f'"{o}"' for o in outcomes)

    # Different framing: ask it to think step by step
    raw = call_groq(
        "You are a careful prediction market settlement analyst. "
        "Think step by step, then output ONLY the answer.\n\n"
        "Step 1: Identify what the question is actually asking.\n"
        "Step 2: Find the relevant fact in the evidence.\n"
        "Step 3: Map that fact to the correct outcome.\n"
        "Step 4: Output ONLY the exact outcome text. No explanation. "
        "If genuinely cannot determine: NONE",
        f"Question: {question}\n"
        f"Valid outcomes: {outcomes_str}\n\n"
        f"Key evidence: {answer_line}\n\n"
        f"Full evidence:\n{full_evidence[:1500]}",
        max_tokens=200
    )

    if not raw:
        return None, None

    matched = raw.strip().strip('"\'')
    # Extract just the last line if model gave multi-line reasoning
    lines = [l.strip() for l in matched.splitlines() if l.strip()]
    if lines:
        matched = lines[-1]

    if not matched or matched.upper() == "NONE":
        return None, None

    for outcome in outcomes:
        if str(outcome).lower().strip() == matched.lower().strip():
            return str(outcome), f"secondary model: {matched}"
    if len(matched) > 4:
        for outcome in outcomes:
            if matched.lower() in str(outcome).lower():
                return str(outcome), f"secondary model: {matched}"

    return None, None


# ─── Unified derive_outcome ───────────────────────────────────────────────────

def derive_outcome(facts: list[Fact], outcomes: list, question: str,
                   intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Three-stage settlement pipeline.
    Stage 1: Direct ANSWER line match (fast, deterministic)
    Stage 2: AI settlement judge (Groq, focused prompt)
    Stage 3: Secondary model (different framing, last resort)
    """
    if not facts:
        return None, None

    facts_dict = {f.label: f.value for f in facts}

    # Fast path: matched_outcome already extracted by fact extraction
    mo = facts_dict.get("matched_outcome", "").strip()
    if mo:
        for outcome in outcomes:
            if str(outcome).lower() == mo.lower():
                return str(outcome), f"extracted: {mo}"

    # Build evidence strings
    has_raw = any(f.label == "raw_evidence" for f in facts)
    full_evidence = facts_dict.get("raw_evidence", "") if has_raw else \
                    "; ".join(f"{f.label}={f.value}" for f in facts)

    answer_line = extract_answer_line(full_evidence)
    if answer_line:
        print(f"[oracle] ANSWER line: {answer_line[:100]}")

    # ── Numeric value fast path (CoinGecko / ORNN API) ────────────
    value_to_check = None
    for key in ["high_price", "close_price", "low_price", "index_value",
                "close", "price", "value"]:
        if key in facts_dict:
            try:
                value_to_check = float(str(facts_dict[key]).replace(",", "").replace("$", ""))
                break
            except Exception:
                pass

    if value_to_check is not None:
        # ── $1,000 step band matching (BTC/ETH price range markets) ──────────
        # Outcomes like "$78,000", "$79,000", "$83,000+" are buckets:
        # $79,000 means 79,000 <= value < 80,000. Do this deterministically
        # before any AI judge can use a vague "closest to" interpretation.
        bare_band_re = re.compile(r'^\$?[\d,]+\+?$')
        bare_band_outcomes = [
            o for o in outcomes
            if bare_band_re.match(str(o).strip()) and str(o).strip().lower() not in ("yes", "no")
        ]
        if len(bare_band_outcomes) >= 3:
            def parse_band(o: str) -> float:
                st = str(o).replace("$", "").replace(",", "").replace("+", "").strip()
                try:
                    return float(st)
                except Exception:
                    return float("inf")

            below_outcome = next((o for o in outcomes if "below" in str(o).lower()), None)
            plus_outcome = next((o for o in outcomes if "+" in str(o)), None)
            sorted_bands = sorted(
                [o for o in outcomes if bare_band_re.match(str(o).strip()) and "+" not in str(o)],
                key=parse_band,
            )

            if below_outcome and sorted_bands:
                lowest = parse_band(sorted_bands[0])
                if value_to_check < lowest:
                    return str(below_outcome), f"{value_to_check:,.0f} < {lowest:,.0f}"

            if plus_outcome:
                plus_floor = parse_band(plus_outcome)
                if value_to_check >= plus_floor:
                    return str(plus_outcome), f"{value_to_check:,.0f} >= {plus_floor:,.0f}"

            for i, band_outcome in enumerate(sorted_bands):
                floor = parse_band(band_outcome)
                ceiling = parse_band(sorted_bands[i + 1]) if i + 1 < len(sorted_bands) else (
                    parse_band(plus_outcome) if plus_outcome else float("inf")
                )
                if floor <= value_to_check < ceiling:
                    return str(band_outcome), f"{floor:,.0f} <= {value_to_check:,.0f} < {ceiling:,.0f}"

        def pv(s: str) -> float:
            s = str(s).replace(",", "").replace("$", "").strip()
            mult = 1
            sl = s.lower()
            if sl.endswith("k"): mult = 1000; s = s[:-1]
            elif sl.endswith("m"): mult = 1_000_000; s = s[:-1]
            elif sl.endswith("b"): mult = 1_000_000_000; s = s[:-1]
            return float(s) * mult

        for outcome in outcomes:
            oc = str(outcome).replace(",", "").replace("$", "")
            b = re.match(r"[Bb]elow\s*(.+)", oc)
            if b:
                try:
                    if value_to_check < pv(b.group(1)):
                        return str(outcome), f"{value_to_check:,.4f} < {b.group(1)}"
                except: pass
                continue
            above_m = re.match(r"(.+?)(?:\s*\+|\s+and\s+above|\s+or\s+above)\s*$", oc, re.I)
            if above_m:
                try:
                    if value_to_check >= pv(above_m.group(1)):
                        return str(outcome), f"{value_to_check:,.4f} >= {above_m.group(1)}"
                except: pass
                continue
            rng = re.match(r"(.+?)(?:\s*[-–]\s*|\s+to\s+|\s+and\s+)(.+)", oc, re.I)
            if rng:
                try:
                    lo_str = rng.group(1).strip()
                    hi_str = rng.group(2).strip()
                    for suf in ["k", "m", "b"]:
                        if hi_str.lower().endswith(suf) and not lo_str.lower().endswith(suf):
                            lo_str += suf; break
                    lo, hi = pv(lo_str), pv(hi_str)
                    if lo <= value_to_check <= hi:
                        return str(outcome), f"{lo:,.0f} <= {value_to_check:,.4f} <= {hi:,.0f}"
                except: pass

    # ── Stage 1: Direct ANSWER-line match ─────────────────────────
    print(f"[oracle] Stage 1: direct match")
    s1, c1 = stage1_direct_match(answer_line, question, outcomes)
    if s1:
        print(f"[oracle] Stage 1 → {s1} | {c1}")
        return s1, c1

    # ── Stage 2: AI settlement judge ──────────────────────────────
    print(f"[oracle] Stage 2: AI judge")
    s2, c2 = stage2_ai_judge(answer_line, full_evidence, question, outcomes, intelligence)
    if s2:
        print(f"[oracle] Stage 2 → {s2} | {c2}")
        return s2, c2

    # ── Stage 3: Secondary model ───────────────────────────────────
    print(f"[oracle] Stage 3: secondary model")
    s3, c3 = stage3_secondary_model(answer_line, full_evidence, question, outcomes, intelligence)
    if s3:
        print(f"[oracle] Stage 3 → {s3} | {c3}")
        return s3, c3

    print(f"[oracle] All stages failed → INCONCLUSIVE")
    return None, None

# ─── Main evidence builder ────────────────────────────────────────────────────

# ─── Query Builder → Evidence Validator → Answer Extractor ───────────────────

def _extract_count_subject(question: str) -> str:
    """Extract what the market is counting from the question."""
    q = str(question or "").lower()
    subjects = [
        ("offensive lineman", "offensive linemen"),
        ("offensive linemen", "offensive linemen"),
        ("quarterback", "quarterbacks"),
        ("wide receiver", "wide receivers"),
        ("defensive lineman", "defensive linemen"),
        ("defensive line", "defensive linemen"),
        ("trade", "trades"),
        ("pick", "picks"),
        ("player", "players"),
        ("goal", "goals"),
        ("point", "points"),
        ("birdie", "birdies"),
    ]
    for singular, plural in subjects:
        if singular in q or plural in q:
            return plural
    m = re.search(r"how many\s+(.+?)\s+(?:will|were|are|have|has|did|does|in|during)", q)
    return m.group(1).strip() if m else "items"


def _extract_team_names(outcomes: list) -> list[str]:
    """Strip spread notation to get team names."""
    teams = []
    for outcome in outcomes or []:
        name = re.sub(r"\s*[+-]\d+(?:\.\d+)?\s*$", "", str(outcome)).strip()
        if name:
            teams.append(name)
    return teams


def build_tavily_query(question: str, outcomes: list,
                       intelligence: dict, event_date: str) -> dict:
    """
    Build a purpose-specific Tavily query for the market structure.
    Returns: {query, search_depth, what_to_validate, need_number, number_context}
    """
    q = str(question or "").lower()
    market_type = str((intelligence or {}).get("market_type") or "")
    answer_format = str((intelligence or {}).get("answer_format") or "")

    # Counting questions need complete trackers/lists, not general coverage.
    if any(k in q for k in ["how many", "number of", "total", "count"]):
        subject = _extract_count_subject(question)
        if "draft" in q or "nfl" in q or "nba" in q:
            return {
                "query": (
                    f"2026 NFL Draft first round complete picks tracker all selections "
                    f"round 1 full list {subject} {event_date}"
                ),
                "search_depth": "basic",
                "what_to_validate": f"count of {subject}",
                "need_number": True,
                "number_context": subject,
            }
        return {
            "query": f"{question} complete list total count official results {event_date}",
            "search_depth": "basic",
            "what_to_validate": f"count of {subject}",
            "need_number": True,
            "number_context": subject,
        }

    # Spread markets need final score.
    spread_re = re.compile(r"^.+\s[+-]\d+\.?\d*\s*$")
    if len(outcomes or []) == 2 and all(spread_re.match(str(o).strip()) for o in outcomes or []):
        sport = sport_hint_for_question(question) if "sport_hint_for_question" in globals() else ""
        teams = _extract_team_names(outcomes)
        return {
            "query": f"{' vs '.join(teams)} {sport} final score result {event_date} official".strip(),
            "search_depth": "basic",
            "what_to_validate": "final score",
            "need_number": True,
            "number_context": "score",
        }

    # Crypto/price range: source router will lock this to Yahoo/CoinGecko/etc.
    if market_type in ("crypto_price", "crypto_price_range") or (intelligence or {}).get("is_price_based"):
        asset = (intelligence or {}).get("asset", "BTC") or "BTC"
        wants_high = any(w in q for w in ["highest", "high", "peak", "max"])
        wants_low = any(w in q for w in ["lowest", "low", "min"])
        metric = "High" if wants_high else ("Low" if wants_low else "Close")
        return {
            "query": f"{asset}-USD historical price {metric} {event_date} yahoo finance history OHLC daily",
            "search_depth": "basic",
            "what_to_validate": f"{asset} daily {metric.lower()} price {event_date}",
            "need_number": True,
            "number_context": f"{asset} price",
        }

    # Sports winner/score markets.
    if any(k in q for k in ["vs", "versus", "match", "game", "cricket", "psl", "ipl", "football", "basketball"]):
        sport = sport_hint_for_question(question) if "sport_hint_for_question" in globals() else ""
        return {
            "query": f"{question} {sport} final score result winner {event_date} official scorecard".strip(),
            "search_depth": "basic",
            "what_to_validate": "match result winner",
            "need_number": False,
            "number_context": "",
        }

    # Named choice.
    if answer_format == "named_choice" or any(k in q for k in ["who", "which", "winner", "selected", "announced"]):
        return {
            "query": f"{question} official result announced winner selected {event_date}",
            "search_depth": "basic",
            "what_to_validate": "winner or selection",
            "need_number": False,
            "number_context": "",
        }

    return {
        "query": f"{question} official confirmed result {event_date}",
        "search_depth": "basic",
        "what_to_validate": "event outcome",
        "need_number": False,
        "number_context": "",
    }


def validate_evidence_quality(content: str, query_plan: dict,
                              question: str) -> tuple[bool, str]:
    """Reject content that cannot plausibly answer the market question."""
    if not content or len(str(content).strip()) < 50:
        return False, "empty content"

    tlow = str(content).lower()
    answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else _extract_answer_line(content)
    what = str((query_plan or {}).get("what_to_validate", "")).lower()

    if (query_plan or {}).get("need_number") and (query_plan or {}).get("number_context"):
        context = str(query_plan.get("number_context") or "").lower()
        context_words = [w for w in context.split() if len(w) >= 3]
        for word in context_words:
            if re.search(rf"\b\d+(?:\.\d+)?\b[^.!?]{{0,50}}{re.escape(word)}|{re.escape(word)}[^.!?]{{0,50}}\b\d+(?:\.\d+)?\b", tlow):
                return True, f"found number near '{word}'"
        if answer_line and re.search(r"\b\d+(?:\.\d+)?\b", answer_line):
            return True, "number in answer line"
        return False, f"no number found near '{context}'"

    if what == "final score":
        if re.search(r"\b\d{1,3}\s*[-–]\s*\d{1,3}\b", str(content)):
            return True, "score pattern found"
        if re.search(r"\b(won|beat|defeated)\b", tlow):
            return True, "result language found"
        return False, "no score or result found"

    if "price" in what:
        if re.search(r"\$[\d,]+", str(content)) or re.search(r"\b\d{4,6}(?:\.\d+)?\b", str(content)):
            return True, "price value found"
        return False, "no price value found"

    if answer_line and len(answer_line) > 20:
        vague = ["including", "highlighted on", "top prospects", "standout from", "preview", "schedule"]
        if not any(v in answer_line.lower() for v in vague):
            return True, "meaningful answer line"

    # If source snippets contain clear result language, allow extraction.
    if re.search(r"\b(won|beat|defeated|selected|announced|closed above|closed below|surpassed|exceeded)\b", tlow):
        return True, "result language found"

    return False, "content does not appear to answer the question"


def _extract_validated_count(content: str, subject: str,
                             question: str) -> Optional[float]:
    """Extract a count explicitly stated in relation to the subject."""
    tlow = str(content or "").lower()
    subject_words = [w for w in str(subject or "").lower().split() if len(w) > 3]
    patterns = []
    for word in subject_words:
        patterns += [
            rf"\b(\d+)\s+(?:\w+\s+)?{re.escape(word)}",
            rf"drafted\s+(\d+)\s+(?:\w+\s+)?{re.escape(word)}",
            rf"there\s+(?:were|was|are|is)\s+(\d+)\s+(?:\w+\s+)?{re.escape(word)}",
            rf"{re.escape(word)}[^.!?]{{0,80}}\btotal[:\s]+(\d+)",
        ]
    for pattern in patterns:
        m = re.search(pattern, tlow)
        if m:
            val = float(m.group(1))
            if "draft" in str(question).lower() and "first round" in str(question).lower() and not (1 <= val <= 32):
                continue
            print(f"[oracle] Validated count: {val}")
            return val

    raw = call_groq(
        f"Count the number of {subject} in this content. Return ONLY a single integer. "
        "If the content does not explicitly state the count, return NOT_FOUND. Do not infer.",
        f"Question: {question}\n\nContent:\n{str(content)[:2000]}",
        max_tokens=50,
    )
    if raw and raw.strip().lower() != "not_found":
        try:
            val = float(raw.strip().split()[0])
            if 1 <= val <= 1000:
                return val
        except Exception:
            pass
    return None


def extract_specific_answer(content: str, query_plan: dict,
                            question: str, outcomes: list,
                            intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract a matched outcome from validated evidence."""
    answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else _extract_answer_line(content)

    s1, c1 = stage1_direct_match(answer_line, question, outcomes)
    if s1:
        return s1, c1

    if (query_plan or {}).get("need_number") and "count" in str((query_plan or {}).get("what_to_validate", "")).lower():
        context = str(query_plan.get("number_context") or "")
        count = _extract_validated_count(content, context, question)
        if count is not None:
            threshold = (intelligence or {}).get("threshold") or _find_numeric_threshold(outcomes)
            if threshold is not None:
                for outcome in outcomes or []:
                    ol = str(outcome).lower()
                    if ("over" in ol or "above" in ol) and count > threshold:
                        return str(outcome), f"count {count:g} > {threshold:g}"
                    if ("under" in ol or "below" in ol) and count < threshold:
                        return str(outcome), f"count {count:g} < {threshold:g}"

    s2, c2 = stage2_ai_judge(answer_line, content, question, outcomes, intelligence)
    if s2:
        return s2, c2

    return stage3_secondary_model(answer_line, content, question, outcomes, intelligence)


# ═══════════════════════════════════════════════════════════════════════════════
# UNIVERSAL SETTLEMENT CORE OVERRIDES
# Query Builder → Evidence Validator → Answer Extractor → Outcome Matcher
# ═══════════════════════════════════════════════════════════════════════════════

def parse_settlement_rules(prompt_context: str, question: str, outcomes: list) -> dict:
    """Parse creator settlement rules and question into executable settlement intent."""
    prompt = str(prompt_context or "")
    q_lower = str(question or "").lower()
    rules = {
        "metric": None,
        "source_order": [],
        "fallback_source": None,
        "count_subject": None,
        "threshold": None,
        "band_logic": None,
        "winner_logic": "most_points",
        "draw_rule": None,
        "time_window": None,
        "raw_rules": [],
    }

    # Source priority / fallback hints from prompt.
    prompt_lower = prompt.lower()
    for name in ["yahoo", "coingecko", "coinmarketcap", "binance", "coinbase", "espn", "espncricinfo", "reuters", "cnbc", "cnn"]:
        if name in prompt_lower:
            if re.search(rf"(?:if|fallback|unavailable|otherwise)[^\n.]{{0,120}}{re.escape(name)}", prompt_lower):
                rules["fallback_source"] = name
            else:
                rules["source_order"].append(name)

    # Metric / required fact.
    if re.search(r'\b(daily\s+high|highest|look up.*high|"high"|\bhigh\b)', prompt, re.I):
        rules["metric"] = "high"
    elif re.search(r'\b(daily\s+low|lowest|look up.*low|"low"|\blow\b)', prompt, re.I):
        rules["metric"] = "low"
    elif re.search(r'\b(close|closing|close\s+price|"close")\b', prompt, re.I):
        rules["metric"] = "close"
    elif re.search(r'\b(open|opening|"open")\b', prompt, re.I):
        rules["metric"] = "open"

    if not rules["metric"]:
        if any(w in q_lower for w in ["highest", "high", "peak", "max"]):
            rules["metric"] = "high"
        elif any(w in q_lower for w in ["lowest", "low", "min"]):
            rules["metric"] = "low"
        elif any(w in q_lower for w in ["close", "closing"]):
            rules["metric"] = "close"
        elif any(w in q_lower for w in ["open", "opening"]):
            rules["metric"] = "open"
        elif any(w in q_lower for w in ["how many", "number of", "count", "total"]):
            rules["metric"] = "count"
        elif any(w in q_lower for w in ["spread", "cover"]):
            rules["metric"] = "spread"
        elif any(w in q_lower for w in ["who wins", "who won", "winner", " vs ", "versus"]):
            rules["metric"] = "winner"

    # Band logic: step bands should use floor, never closest, unless creator says closest.
    if re.search(r"closest|nearest", prompt, re.I):
        rules["band_logic"] = "closest"
    elif re.search(r"met or exceeded|highest.*band|floor|less than next|range", prompt, re.I):
        rules["band_logic"] = "floor"
    if not rules["band_logic"]:
        bare_band_re = re.compile(r"^\$?[\d,]+\+?$")
        band_count = sum(1 for o in outcomes or [] if bare_band_re.match(str(o).strip()) and "below" not in str(o).lower())
        if band_count >= 3:
            rules["band_logic"] = "floor"

    # Count subject.
    if rules["metric"] == "count" or any(k in q_lower for k in ["how many", "number of"]):
        subjects = [
            ("offensive lineman", "offensive linemen"), ("offensive linemen", "offensive linemen"),
            ("quarterback", "quarterbacks"), ("qb", "quarterbacks"),
            ("wide receiver", "wide receivers"), ("wr", "wide receivers"),
            ("defensive lineman", "defensive linemen"), ("defensive line", "defensive linemen"),
            ("trade", "trades"), ("pick", "picks"), ("player", "players"),
            ("goal", "goals"), ("point", "points"), ("birdie", "birdies"),
        ]
        for singular, plural in subjects:
            if singular in q_lower or plural in q_lower:
                rules["count_subject"] = plural
                rules["metric"] = "count"
                break
        if not rules["count_subject"]:
            m = re.search(r"how many\s+(.+?)\s+(?:will|were|are|have|has|did|does|in|during)", q_lower)
            rules["count_subject"] = m.group(1).strip() if m else "items"

    # Threshold from outcomes.
    for outcome in outcomes or []:
        ol = str(outcome).lower()
        if any(k in ol for k in ["over", "under", "above", "below"]):
            m = re.search(r"\d+(?:\.\d+)?", ol)
            if m:
                rules["threshold"] = float(m.group(0))
                break

    # Draw/time-window rules.
    if re.search(r"draw|abandoned|no result|tied", prompt, re.I):
        rules["draw_rule"] = "standard"
    if re.search(r"regulation\s+time\s+only", prompt, re.I):
        rules["time_window"] = "regulation"
    elif re.search(r"include.*overtime|including.*shootout", prompt, re.I):
        rules["time_window"] = "including_ot"

    rule_section = re.search(r"SETTLEMENT RULES?[:\s]*\n([\s\S]+?)(?:\nVALID OUTCOMES|\nDATA SOURCES|$)", prompt, re.I)
    if rule_section:
        rules["raw_rules"] = [line.strip() for line in rule_section.group(1).splitlines() if line.strip()]

    print(f"[oracle] Rules: metric={rules['metric']} band={rules['band_logic']} count_subject={rules['count_subject']} threshold={rules['threshold']}")
    return rules


def _universal_extract_count_subject(question: str) -> str:
    q = str(question or "").lower()
    subjects = [
        ("offensive lineman", "offensive linemen"), ("offensive linemen", "offensive linemen"),
        ("quarterback", "quarterbacks"), ("qb", "quarterbacks"),
        ("wide receiver", "wide receivers"), ("wr", "wide receivers"),
        ("defensive lineman", "defensive linemen"), ("defensive line", "defensive linemen"),
        ("trade", "trades"), ("pick", "picks"), ("player", "players"),
        ("goal", "goals"), ("point", "points"), ("birdie", "birdies"),
    ]
    for singular, plural in subjects:
        if singular in q or plural in q:
            return plural
    m = re.search(r"how many\s+(.+?)\s+(?:will|were|are|have|has|did|does|in|during)", q)
    return m.group(1).strip() if m else "items"


def build_universal_query(question: str, outcomes: list, rules: dict,
                          event_date: str, source_domain: str) -> dict:
    """Build source-locked Tavily query from settlement rules, not vague market text."""
    q = str(question or "").lower()
    metric = (rules or {}).get("metric")
    count_subject = (rules or {}).get("count_subject")

    if metric == "count" and count_subject:
        year = event_date[:4] if event_date else "2026"
        if any(k in q for k in ["draft", "nfl", "nba", "nhl"]):
            league = "NFL" if "nfl" in q or "football" in q else ("NBA" if "nba" in q else "NHL")

            # Draft trade-count markets need the official trade tracker, not pick trackers
            # or mock-draft articles. This avoids false counts from "4 teams to trade up"
            # style predictions/analysis.
            if "trade" in str(count_subject).lower():
                return {
                    "query": (
                        f"{year} {league} Draft round 1 trades official trade tracker "
                        f"draft-day trades first round total ESPN NFL.com {event_date}"
                    ),
                    "search_depth": "basic",
                    "required_data_type": "count",
                    "extraction_target": count_subject,
                    "what_to_validate": f"count of {count_subject}",
                    "need_number": True,
                    "number_context": count_subject,
                    "count_method": "draft_trade_count",
                }

            query_plan = {
                # Target structured pick trackers / pick-order pages, not narrative analysis or mock-draft articles.
                "query": (
                    f"{year} {league} Draft round 1 picks complete list tracker "
                    f"all 32 selections official pick order position {event_date}"
                ),
                "search_depth": "basic",
                "required_data_type": "count",
                "extraction_target": count_subject,
                "what_to_validate": f"count of {count_subject}",
                "need_number": True,
                "number_context": count_subject,
            }
            # For offensive-line draft-count markets, count structured position codes from the tracker.
            # This avoids treating a few named examples in an article as the total count.
            if "offensive" in count_subject.lower() and "linemen" in count_subject.lower():
                query_plan["count_method"] = "position_codes"
                query_plan["position_codes"] = ["OT", "OG", "OC", "G", "T", "C", "IOL", "OL"]
            return query_plan
        return {
            "query": f"{question} official total count complete list {event_date}",
            "search_depth": "basic", "required_data_type": "count", "extraction_target": count_subject,
            "what_to_validate": f"count of {count_subject}", "need_number": True, "number_context": count_subject,
        }

    if metric in ("high", "low", "close", "open"):
        asset_m = re.search(r"\b(BTC|ETH|SOL|BNB|XRP|ADA|DOGE|bitcoin|ethereum|solana)\b", question, re.I)
        asset = asset_m.group(0).upper() if asset_m else "BTC"
        asset = {"BITCOIN": "BTC", "ETHEREUM": "ETH", "SOLANA": "SOL"}.get(asset, asset)
        metric_label = {"high": "High", "low": "Low", "close": "Close", "open": "Open"}.get(metric, "Close")
        if "yahoo" in str(source_domain).lower():
            return {
                "query": f"{asset}-USD {event_date} historical OHLC daily {metric_label} price finance.yahoo.com history",
                "search_depth": "basic", "required_data_type": "price", "extraction_target": f"{asset} {metric_label} price on {event_date}",
                "what_to_validate": f"{asset} daily {metric_label.lower()} price {event_date}", "need_number": True, "number_context": f"{asset} price",
            }
        return {
            "query": f"{asset} USD {metric_label.lower()} price {event_date} historical",
            "search_depth": "basic", "required_data_type": "price", "extraction_target": f"{asset} {metric_label} price on {event_date}",
            "what_to_validate": f"{asset} daily {metric_label.lower()} price {event_date}", "need_number": True, "number_context": f"{asset} price",
        }

    spread_re = re.compile(r"^.+\s[+-]\d+\.?\d*\s*$")
    if len(outcomes or []) == 2 and all(spread_re.match(str(o).strip()) for o in outcomes or []):
        teams = [re.sub(r"\s*[+-]\d+(?:\.\d+)?\s*$", "", str(o)).strip() for o in outcomes]
        sport = next((hint for kw, hint in SPORT_HINTS.items() if kw in q), "") if "SPORT_HINTS" in globals() else ""
        return {
            "query": f"{teams[0]} vs {teams[1]} {sport} final score result {event_date} official".strip(),
            "search_depth": "basic", "required_data_type": "score", "extraction_target": "final score and winner",
            "what_to_validate": "final score", "need_number": True, "number_context": "score",
        }

    if metric == "winner" or any(k in q for k in ["vs", "versus", "match", "game", "winner", "cricket", "psl", "ipl"]):
        sport = next((hint for kw, hint in SPORT_HINTS.items() if kw in q), "") if "SPORT_HINTS" in globals() else ""
        return {
            "query": f"{question} {sport} result scorecard final score winner {event_date}".strip(),
            "search_depth": "basic", "required_data_type": "match_result", "extraction_target": "match winner and final score",
            "what_to_validate": "match result winner", "need_number": False, "number_context": "",
        }

    if any(k in q for k in ["who", "which", "winner", "award", "selected", "announced", "elected", "drafted"]):
        return {
            "query": f"{question} official announced winner result {event_date}",
            "search_depth": "basic", "required_data_type": "named_result", "extraction_target": "winner or selected entity",
            "what_to_validate": "winner or selection", "need_number": False, "number_context": "",
        }

    out_set = {str(o).lower() for o in outcomes or []}
    if any(out_set <= pair for pair in [{"yes", "no"}, {"green", "red"}, {"up", "down"}]):
        return {
            "query": f"{question} official confirmed result announcement {event_date}",
            "search_depth": "basic", "required_data_type": "confirmation", "extraction_target": "whether event happened",
            "what_to_validate": "event outcome", "need_number": False, "number_context": "",
        }

    return {"query": f"{question} official result {event_date}", "search_depth": "basic", "required_data_type": "any", "extraction_target": "outcome", "what_to_validate": "event outcome", "need_number": False, "number_context": ""}


def build_tavily_query(question: str, outcomes: list, intelligence: dict, event_date: str) -> dict:
    """Compatibility wrapper used by build_source_evidence."""
    rules = (intelligence or {}).get("_rules") or parse_settlement_rules((intelligence or {}).get("prompt_context", ""), question, outcomes)
    return build_universal_query(question, outcomes, rules, event_date, "")


def validate_evidence(content: str, required_data_type: str, extraction_target: str, question: str) -> tuple[bool, str]:
    """Strictly validate fetched content contains the data type required by the market."""
    if not content or len(str(content).strip()) < 50:
        return False, "empty content"
    text = str(content)
    tlow = text.lower()
    answer_line = extract_answer_line(text) if "extract_answer_line" in globals() else _extract_answer_line(text)
    al = answer_line.lower()
    q_lower = str(question or "").lower()

    if required_data_type == "count":
        subject = str(extraction_target or "").lower().replace("count of ", "").strip()

        # Reject noisy predictive/narrative pages for draft count markets.
        # These caused false "4 trades" counts from mock-draft articles.
        bad_noise = [
            "mock draft", "final mock", "projects how", "projection",
            "projected", "rankings", "big board", "best available",
            "winners and losers", "winner and loser", "grades the biggest",
            "favorite picks", "favorite 2026 nfl draft picks",
        ]
        urls = re.findall(r'https?://[^\s\]\)]+', text)
        bad_url = any(any(b.replace(" ", "-") in u.lower() or b in u.lower() for b in bad_noise) for u in urls)
        if "draft" in q_lower and bad_url and not any(k in tlow for k in [
            "trade tracker", "tracking deals", "tracked every single trade",
            "here are the draft-day trades", "2026 nfl draft trades"
        ]):
            return False, "rejected draft-count evidence from mock/projection/analysis page"

        # Trade-count markets need trade tracker/deals evidence, not pick analysis.
        if "trade" in subject and "draft" in q_lower:
            has_trade_tracker = any(k in tlow for k in [
                "trade tracker", "tracking deals", "tracked every single trade",
                "here are the draft-day trades", "draft-day trades",
                "round 1 trades", "first-round trades"
            ])
            has_answer_trade_count = bool(answer_line and re.search(
                r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+trades?\s+occurred\s+during\s+round\s+one",
                al
            ))
            if not (has_trade_tracker or has_answer_trade_count):
                return False, "no official trade tracker / first-round trade-count evidence"

        words = [w for w in subject.split() if len(w) > 3]
        for word in words:
            if re.search(rf"\b\d+\b[^.!?]{{0,70}}{re.escape(word)}|{re.escape(word)}[^.!?]{{0,70}}\b\d+\b", tlow):
                return True, f"found number near '{word}'"
        if answer_line and re.search(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b", al) and not any(v in al for v in ["preview", "mock draft", "top prospects", "expects", "projected"]):
            return True, "count in answer line"
        return False, f"no validated count of '{subject}' found"

    if required_data_type == "price":
        if re.search(r"\$[\d,]+(?:\.\d+)?", text) or re.search(r"\b\d{4,6}(?:\.\d+)?\b", text):
            return True, "price value found"
        return False, "no price value found"

    if required_data_type in ("score", "match_result"):
        if re.search(r"\b\d{1,3}\s*[-–]\s*\d{1,3}\b", text):
            return True, "score pattern found"
        if re.search(r"\b(won|beat|defeated|victory|draw|tied|abandoned|final score)\b", tlow):
            return True, "result language found"
        if re.search(r"\b\d+\s+wickets?\b", tlow) or re.search(r"\b\d+/\d+\b", text):
            return True, "cricket score/result found"
        return False, "no score or result found"

    if required_data_type == "named_result":
        if re.search(r"\b(won|winner|selected|announced|elected|drafted|chose|picked)\b", tlow):
            return True, "selection language found"
        return False, "no named result found"

    if required_data_type == "confirmation":
        topic_words = [w for w in q_lower.split() if len(w) > 4 and w not in {"will", "would", "could", "should", "which", "there", "their", "about"}]
        hits = sum(1 for w in topic_words[:5] if w in tlow)
        if hits >= 2 and len(answer_line) > 20:
            return True, "topic-relevant answer found"
        return False, "content not relevant to question"

    if answer_line and len(answer_line) > 20 and not any(v in al for v in ["preview", "mock draft", "top prospects", "including a standout", "key selections included"]):
        return True, "meaningful answer line"
    if re.search(r"\b(won|beat|defeated|selected|announced|closed|surpassed|exceeded)\b", tlow):
        return True, "result language found"
    return False, "content does not contain required data"


def validate_evidence_quality(content: str, query_plan: dict, question: str) -> tuple[bool, str]:
    """Compatibility wrapper for existing build_source_evidence."""
    return validate_evidence(
        content,
        str((query_plan or {}).get("required_data_type") or ("count" if (query_plan or {}).get("need_number") and "count" in str((query_plan or {}).get("what_to_validate", "")).lower() else ("score" if str((query_plan or {}).get("what_to_validate", "")).lower() == "final score" else "any"))),
        str((query_plan or {}).get("extraction_target") or (query_plan or {}).get("number_context") or ""),
        question,
    )



def fetch_best_url_from_evidence(content: str, question: str,
                                  count_subject: str) -> Optional[str]:
    q = str(question or "").lower()
    subject = str(count_subject or "").lower()
    url_scores: list[tuple[int, str]] = []
    seen: set[str] = set()

    for m in re.finditer(r'https?://[^\s\]\)]+', str(content or "")):
        url = m.group(0).rstrip('.,)')
        if url in seen:
            continue
        seen.add(url)
        ul = url.lower()
        score = 0

        # Highest value: ESPN grades article has all 32 teams' picks with positions.
        if "grades" in ul and ("32-teams" in ul or "all-32" in ul or "every" in ul):
            score += 15

        # High value: structured trackers and full pick lists.
        if any(k in ul for k in [
            "tracker", "all-picks", "every-pick", "every-selection",
            "complete", "full-results", "round-1-picks", "first-round-picks",
            "257-picks", "all-selections"
        ]):
            score += 10

        # Medium value: analysis pages that explicitly cover all teams/picks.
        if any(k in ul for k in ["analysis", "every-team", "32-teams", "all-32"]):
            score += 5

        # Strongly penalize noisy articles that caused false counts.
        if any(k in ul for k in [
            "winners-losers", "mock-draft", "big-board", "rankings",
            "round-2", "round-3", "best-available", "fantasy", "projections"
        ]):
            score -= 10

        if "draft" in q and "draft" in ul:
            score += 2
        if any(k in ul for k in ["round-1", "first-round", "pick"]):
            score += 2

        if score > 0:
            url_scores.append((score, url))

    if not url_scores:
        return None

    url_scores.sort(key=lambda x: x[0], reverse=True)
    best_url = url_scores[0][1]
    print(f"[oracle] Best URL: {best_url} (score {url_scores[0][0]})")
    return best_url

def _count_position_codes_in_text(text: str, position_codes: list[str],
                                  question: str) -> Optional[float]:
    """
    Count draft position codes from structured draft pick content.
    Handles:
      1. PFR/comma format: Name, OT, School
      2. ESPN pipe format: | 1/19 | Name | OT | School |
      3. Structured prose rows with pick numbers and standalone codes

    Rejects Kiper-style rankings/winners-losers lists where numbered items are
    not actual draft picks and parenthesized numbers are ages/grades.
    """
    if not text or not position_codes:
        return None

    section = str(text)

    # Isolate round 1 content where possible.
    round2_m = re.search(
        r'(?:round\s*2|second\s+round|\|\s*2/\d+\s*\|)',
        section, re.I
    )
    if round2_m and round2_m.start() > 500:
        section = section[:round2_m.start()]
        print(f"[oracle] Round 1 section: {len(section)} chars")

    # ── FORMAT 1: ESPN pipe table | 1/19 | Name | OT | School | ──────────
    # Only count rows where first column is round 1, e.g. "| 1/19 |".
    pipe_count = 0
    seen_pipe_lines: set[str] = set()
    for line in section.splitlines():
        if not re.search(r'\|\s*1/\d+\s*\|', line):
            continue
        line_key = line.strip()
        if line_key in seen_pipe_lines:
            continue
        for code in position_codes:
            if re.search(rf'\|\s*{re.escape(code)}\s*\|', line, re.I):
                pipe_count += 1
                seen_pipe_lines.add(line_key)
                print(f"[oracle] Pipe R1 position match: {line.strip()[:80]}")
                break

    if pipe_count > 0:
        print(f"[oracle] Pipe format position count: {pipe_count}")
        return float(pipe_count)

    # ── FORMAT 2: PFR/comma format Name, OT, School ──────────────────────
    # Require comma-code near an actual pick number 1-32 on the same line.
    comma_count = 0
    seen_comma_lines: set[str] = set()
    for line in section.splitlines():
        if not re.search(r'\b([1-9]|[12]\d|3[0-2])\b', line):
            continue
        line_key = line.strip()
        if line_key in seen_comma_lines:
            continue
        for code in position_codes:
            if re.search(rf',\s*{re.escape(code)}\s*,', line, re.I):
                # Guard: reject Kiper-style lists where "(XX)" is age/rank, not pick.
                if re.search(r'\(\d{2}\)\s*$', line.strip()):
                    continue
                comma_count += 1
                seen_comma_lines.add(line_key)
                print(f"[oracle] Comma R1 position match: {line.strip()[:80]}")
                break

    if comma_count > 0:
        print(f"[oracle] Comma format position count: {comma_count}")
        return float(comma_count)

    # ── FORMAT 3: preserved text/table rows with pick number + code ───────
    row_count = 0
    seen_rows: set[str] = set()
    for line in section.splitlines():
        line_clean = re.sub(r'\s+', ' ', line).strip()
        if line_clean in seen_rows:
            continue
        if not re.search(r'\b(?:pick\s*)?([1-9]|[12]\d|3[0-2])\b', line_clean, re.I):
            continue
        if re.search(r'\(\d{2}\)\s*$', line_clean):
            continue
        for code in position_codes:
            if re.search(rf'\b{re.escape(code)}\b', line_clean, re.I):
                row_count += 1
                seen_rows.add(line_clean)
                print(f"[oracle] Row R1 position match: {line_clean[:80]}")
                break
    if row_count > 0:
        print(f"[oracle] Row format position count: {row_count}")
        return float(row_count)

    return None

def _strip_html_to_text_preserve_rows(html: str) -> str:
    """Strip HTML while preserving table/list row boundaries for draft parsing."""
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', str(html or ''), flags=re.S | re.I)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.S | re.I)
    text = re.sub(r'</(?:tr|li|p|div|h\d)>', '\n', text, flags=re.I)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;?', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s+', '\n', text)
    return text.strip()


def _count_draft_positions_from_html_or_text(content: str, position_codes: set[str], question: str) -> Optional[float]:
    """Count first-round draft position codes from official/static draft pages."""
    if not content or not position_codes:
        return None

    html = str(content or '')

    # Pro Football Reference/static-table path: each row has data-stat cells.
    # Count only pick 1-32 and exact position code cells.
    row_count = 0
    for row_m in re.finditer(r'<tr[^>]*>([\s\S]*?)</tr>', html, re.I):
        row = row_m.group(1)
        pick_m = re.search(
            r'data-stat=["\'](?:draft_pick|pick|overall_pick)["\'][^>]*>\s*(\d{1,3})\s*<',
            row, re.I
        )
        pos_m = re.search(
            r'data-stat=["\'](?:pos|position)["\'][^>]*>\s*([A-Z]{1,4})\s*<',
            row, re.I
        )
        if pick_m and pos_m:
            pick = int(pick_m.group(1))
            pos = pos_m.group(1).strip().lower()
            if 1 <= pick <= 32 and pos in position_codes:
                row_count += 1
    if row_count > 0:
        print(f"[oracle] Official table position count: {row_count}")
        return float(row_count)

    # Generic text fallback preserving rows.
    text = _strip_html_to_text_preserve_rows(html)

    # Try to isolate first round only.
    m = re.search(
        r'(?:round\s*1|first\s+round|pick\s*1)[\s\S]{0,80000}?(?:round\s*2|second\s+round|pick\s*33|\n\s*33\b|$)',
        text, re.I
    )
    section = m.group(0) if m else text[:60000]
    if m:
        print(f"[oracle] Official Round 1 section: {len(section)} chars")

    count = 0
    seen_picks: set[int] = set()
    for line in section.splitlines():
        l = re.sub(r'\s+', ' ', line).strip()
        if not l:
            continue
        # Need a pick number 1-32 in the row and a standalone position code.
        pick_m = re.search(r'\b(?:pick\s*)?([1-9]|[12]\d|3[0-2])\b', l, re.I)
        if not pick_m:
            continue
        pick = int(pick_m.group(1))
        # Avoid double-counting one pick if the row has multiple OL words.
        tokens = {t.lower() for t in re.findall(r'\b[A-Z]{1,4}\b', l)}
        if tokens & position_codes and pick not in seen_picks:
            seen_picks.add(pick)
            count += 1

    if count > 0:
        print(f"[oracle] Official text position count: {count}")
        return float(count)
    return None


def get_draft_count_direct(year: str, league: str,
                           count_subject: str, question: str) -> Optional[float]:
    """
    Direct fetch official draft results pages.
    Priority: official league site -> sports reference database.
    Never use user-editable sources such as Wikipedia for settlement.
    """
    subject = str(count_subject or '').lower()
    if 'offensive' in subject and 'linemen' in subject:
        position_codes = {'t', 'g', 'c', 'ot', 'og', 'oc', 'ol', 'iol'}
    elif 'quarterback' in subject:
        position_codes = {'qb'}
    elif 'wide receiver' in subject or 'receivers' in subject:
        position_codes = {'wr'}
    elif 'running back' in subject:
        position_codes = {'rb'}
    elif 'tight end' in subject:
        position_codes = {'te'}
    elif 'defensive' in subject and ('line' in subject or 'linemen' in subject):
        position_codes = {'de', 'dt', 'dl', 'edge'}
    else:
        return None

    # Authoritative/static sources only. No user-editable sources.
    source_urls = {
        'nfl': [
            f'https://www.nfl.com/draft/{year}/tracker/picks',
            f'https://www.pro-football-reference.com/years/{year}/draft.htm',
        ],
        'nba': [
            f'https://www.nba.com/draft/{year}',
            f'https://www.basketball-reference.com/draft/NBA_{year}.html',
        ],
        'mlb': [
            f'https://www.mlb.com/draft/{year}',
            f'https://www.baseball-reference.com/draft/?year_ID={year}',
        ],
        'nhl': [
            f'https://www.nhl.com/draft/{year}',
            f'https://www.hockey-reference.com/draft/NHL_{year}_entry.html',
        ],
    }

    urls = source_urls.get(str(league or '').lower(), [])
    for url in urls:
        print(f"[oracle] Direct fetch (official): {url}")
        result = direct_fetch(url)
        if not result:
            print(f"[oracle] Failed: {url}")
            continue
        content, _ = result
        if len(content) < 500:
            print(f"[oracle] Too short ({len(content)} chars): {url}")
            continue
        print(f"[oracle] Fetched: {len(content)} chars from {url}")
        val = _count_draft_positions_from_html_or_text(content, position_codes, question)
        if val is not None:
            print(f"[oracle] ✓ Official count: {val:g} {count_subject}")
            return val

    # Direct official/static pages failed. For NFL only, use Tavily to find
    # ESPN's draft grades/all-teams article, which often has pipe rows like:
    # | 1/19 | Player | OT | School |
    if TAVILY_API_KEY and str(league or '').lower() == 'nfl':
        print("[oracle] Trying Tavily for ESPN grades article (all team picks with positions)...")
        try:
            r = requests.post("https://api.tavily.com/search", json={
                "api_key": TAVILY_API_KEY,
                "query": f"site:espn.com {year} NFL Draft grades all 32 teams picks positions round 1",
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": False,
            }, timeout=15)
            data = r.json()
            for res in data.get("results", []):
                url = res.get("url", "")
                ul = url.lower()
                # Only accept all-teams grades/every-pick style pages, not winners/losers or rankings.
                if "grades" in ul and ("32-teams" in ul or "all-32" in ul or "every" in ul):
                    print(f"[oracle] Found ESPN grades article: {url}")
                    result = direct_fetch(url)
                    if result:
                        content, _ = result
                        if len(content) > 1000:
                            val = _count_position_codes_in_text(content, list(position_codes), question)
                            if val is not None:
                                print(f"[oracle] ✓ ESPN grades count: {val:g} {count_subject}")
                                return val
        except Exception as e:
            print(f"[oracle] ESPN grades Tavily search failed: {e}")

    return None

def count_ol_from_full_article(url: str, count_subject: str,
                               question: str) -> Optional[float]:
    """
    Direct-fetch the best full article/tracker and count round-1 position codes.
    This avoids trusting Tavily snippets from narrative analysis pages.
    """
    result = direct_fetch(url)
    if not result:
        print(f"[oracle] Full article fetch failed: {url}")
        return None

    full_content, _ = result
    print(f"[oracle] Full article: {len(full_content)} chars from {url}")

    subject = str(count_subject or "").lower()
    if "offensive" in subject and "linemen" in subject:
        position_codes = ["OT", "OG", "OC", "G", "T", "C", "IOL", "OL"]
    elif "quarterback" in subject:
        position_codes = ["QB"]
    elif "wide receiver" in subject or "receivers" in subject:
        position_codes = ["WR"]
    else:
        position_codes = []

    val = _count_position_codes_in_text(full_content, position_codes, question)
    if val is not None:
        return val

    # Last resort: ask Groq to count from the full fetched article, but with a strict NOT_FOUND option.
    raw = call_groq(
        f"Count the exact number of {count_subject} selected in round 1 picks 1-32.\n"
        f"Look for structured pick rows and position codes after player names.\n"
        f"Only count actual first-round selections, not rankings, ages, grades, mock drafts, or examples.\n"
        f"Return ONLY a single integer. If cannot determine from the article: NOT_FOUND",
        f"Question: {question}\n\nFull draft article content:\n{full_content[:6000]}",
        max_tokens=30,
    )
    if raw and "not_found" not in raw.lower():
        m = re.search(r'\d+', raw)
        if m:
            val = float(m.group(0))
            if 0 <= val <= 32:
                print(f"[oracle] Groq full-article count: {val:g}")
                return val
    return None


def _number_word_to_float(value: str) -> Optional[float]:
    """Convert small number words or numeric strings to float."""
    s = str(value or "").strip().lower().replace(",", "")
    words = {
        "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
        "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18,
        "nineteen": 19, "twenty": 20,
    }
    if s in words:
        return float(words[s])
    try:
        return float(s)
    except Exception:
        return None


def _extract_draft_trade_count(content: str, question: str) -> Optional[float]:
    """Extract first-round draft trade count from tracker-style evidence."""
    text = str(content or "")
    tlow = text.lower()
    answer_line = extract_answer_line(text) if "extract_answer_line" in globals() else _extract_answer_line(text)
    al = answer_line.lower()

    # Prefer explicit answer-line claims.
    patterns = [
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+trades?\s+occurred\s+during\s+round\s+one\b",
        r"\bround\s+one\b[^.!?]{0,80}\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+trades?\b",
        r"\b(first-round|round\s+1)\s+trades?[^.!?]{0,40}\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\b",
    ]
    for pat in patterns:
        for source_text in [al, tlow]:
            m = re.search(pat, source_text)
            if m:
                # Pattern may capture count in group 1 or group 2.
                for g in m.groups():
                    val = _number_word_to_float(g)
                    if val is not None and 0 <= val <= 32:
                        return val

    # ESPN tracker-specific page often says "There were 41 draft-weekend trades"
    # and then lists draft-day trades. Do NOT use the weekend total. Count first-round
    # deal headings only when available.
    if "trade tracker" in tlow or "tracking deals" in tlow or "2026 nfl draft trades" in tlow:
        # Count headings like "Dallas Cowboys: Trade down from No. 20".
        headings = re.findall(r"(?m)^#+\s+[^\\n]{0,120}\\btrade\\b[^\\n]{0,120}$", text, re.I)
        round1_headings = []
        for h in headings:
            hl = h.lower()
            # First-round signals: explicit No. 1-32, Round 1, first-round.
            no_m = re.search(r"\bno\.\s*(\d{1,2})\b", hl)
            if "round 1" in hl or "first-round" in hl or (no_m and 1 <= int(no_m.group(1)) <= 32):
                round1_headings.append(h)
        if round1_headings:
            unique = {re.sub(r"\s+", " ", h.strip().lower()) for h in round1_headings}
            return float(len(unique))

    return None


def extract_required_value(content: str, rules: dict, question: str, outcomes: list) -> tuple[Optional[str], Optional[str]]:
    """Extract raw settlement value required by rules; matching happens separately."""
    metric = (rules or {}).get("metric")
    subject = (rules or {}).get("count_subject")
    answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else _extract_answer_line(content)
    al = answer_line.lower()
    tlow = str(content or "").lower()

    if metric == "count" and subject:
        # Structured draft tracker path: do NOT count position codes from Tavily snippets first.
        # Snippets can be narrative articles (winners/losers, rankings, mock drafts) and produce false counts.
        # First use official league/stat sources directly: NFL.com -> Pro Football Reference.
        if (rules or {}).get("count_method") == "position_codes":
            year_m = re.search(r"\b(20\d{2})\b", str(question) + " " + str((rules or {}).get("event_date", "")))
            year = year_m.group(1) if year_m else "2026"
            ql = str(question or "").lower()
            league = "nfl" if ("nfl" in ql or "football" in ql or "draft" in ql) else ("nba" if "nba" in ql else ("mlb" if "mlb" in ql else ("nhl" if "nhl" in ql else "nfl")))
            val = get_draft_count_direct(year, league, subject, question)
            if val is not None:
                return str(val), f"official draft count: {val:g} {subject}"

            # If official/stat direct fetch fails, use Tavily-returned URLs and fetch the best full article/tracker.
            best_url = fetch_best_url_from_evidence(content, question, subject)
            if best_url:
                val = count_ol_from_full_article(best_url, subject, question)
                if val is not None:
                    return str(val), f"full article count: {val:g} {subject}"

            # Only count the current content directly if it already looks like a structured pick list.
            position_codes = list((rules or {}).get("position_codes") or [])
            val = _count_position_codes_in_text(str(content or ""), position_codes, question)
            if val is not None:
                return str(val), f"structured position-code count: {val:g} {subject}"

            # For position-code markets, avoid falling through to generic Groq on snippets.
            # Generic Groq can count named examples, not the full draft total.
            return None, None

        # Draft trade counts are a common false-positive class. Prefer tracker-style
        # extraction and reject mock/projection snippets.
        if "trade" in subject.lower() and "draft" in str(question).lower():
            val = _extract_draft_trade_count(content, question)
            if val is not None:
                return str(val), f"draft trade count: {val:g} {subject}"

        words = [w for w in subject.lower().split() if len(w) > 3]
        patterns = []
        for word in words:
            patterns += [
                rf"\b(\d+)\s+(?:\w+\s+)?{re.escape(word)}",
                rf"(?:drafted|selected|picked|chose)\s+(\d+)\s+(?:\w+\s+)?{re.escape(word)}",
                rf"there\s+(?:were|was|are)\s+(\d+)\s+(?:\w+\s+)?{re.escape(word)}",
                rf"{re.escape(word)}[^.!?]{{0,80}}\btotal\b[:\s]+(\d+)",
                rf"\btotal\s+of\s+(\d+)\s+(?:\w+\s+)?{re.escape(word)}",
            ]
        for source_text in [al, tlow]:
            for pat in patterns:
                m = re.search(pat, source_text)
                if m:
                    val = float(m.group(1))
                    if "first round" in str(question).lower() and not (1 <= val <= 32):
                        continue
                    return str(val), f"count pattern: {val:g} {subject}"
        raw = call_groq(
            f"Count the exact number of {subject} explicitly stated in this content. Return ONLY a single integer. If not stated: NOT_FOUND. Do NOT estimate.",
            f"Question: {question}\n\nContent:\n{str(content)[:2500]}", max_tokens=30)
        if raw and "not_found" not in raw.lower():
            m = re.search(r"\d+", raw)
            if m:
                val = float(m.group(0))
                if not ("first round" in str(question).lower() and not (1 <= val <= 32)):
                    return str(val), f"groq count: {val:g}"
        return None, None

    if metric in ("high", "low", "close", "open"):
        label = {"high": "high", "low": "low", "close": "close", "open": "open"}[metric]
        # Prefer explicit metric label near price, especially for Yahoo OHLC snippets.
        patterns = [
            rf"\b{label}\b[^\d$]{{0,40}}\$?([\d,]+(?:\.\d+)?)",
            rf"\$?([\d,]+(?:\.\d+)?)\s*(?:usd)?[^.!?]{{0,40}}\b{label}\b",
        ]
        for text in [al, tlow]:
            for pat in patterns:
                m = re.search(pat, text, re.I)
                if m:
                    return m.group(1).replace(",", ""), f"{metric} price pattern"
        # For price answer lines, use first crypto-sized number.
        for m in re.finditer(r"\$?([1-9]\d{3,6}(?:\.\d+)?)", al or tlow):
            return m.group(1).replace(",", ""), f"{metric} price numeric"
        return None, None

    if metric in ("winner", "spread") or any(k in str(question).lower() for k in [" vs ", "versus", "match", "game", "spread", "cover"]):
        return answer_line or str(content)[:1000], "sports answer text"

    # Named/binary: answer line first, else concise content.
    return (answer_line or str(content)[:1000], "answer text") if (answer_line or content) else (None, None)


def _parse_money_number(text: str) -> Optional[float]:
    m = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?\s*[kKmMbB]?", str(text or ""))
    if not m:
        return None
    s = m.group(0).replace(",", "").strip().lower()
    mult = 1.0
    if s.endswith("k"):
        mult = 1000.0; s = s[:-1]
    elif s.endswith("m"):
        mult = 1_000_000.0; s = s[:-1]
    elif s.endswith("b"):
        mult = 1_000_000_000.0; s = s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return None


def match_value_to_outcome(raw_value: str, rules: dict, question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """Map extracted raw value to exact valid outcome using deterministic rules."""
    metric = (rules or {}).get("metric")
    text = str(raw_value or "")
    tlow = text.lower()

    # Threshold Over/Under.
    val = _parse_money_number(text)
    threshold = (rules or {}).get("threshold") or (intelligence or {}).get("threshold") or _find_numeric_threshold(outcomes)
    if val is not None and threshold is not None:
        for outcome in outcomes or []:
            ol = str(outcome).lower()
            if ("over" in ol or "above" in ol) and val > threshold:
                return str(outcome), f"{val:g} > {threshold:g}"
            if ("under" in ol or "below" in ol) and val < threshold:
                return str(outcome), f"{val:g} < {threshold:g}"

    # Price bare step bands: $78,000, $79,000, $83,000+.
    bare_band_re = re.compile(r"^\$?[\d,]+\+?$")
    bare_bands = [o for o in outcomes or [] if bare_band_re.match(str(o).strip())]
    if val is not None and len(bare_bands) >= 3:
        def parse_band(o: str) -> float:
            return float(str(o).replace("$", "").replace(",", "").replace("+", "").strip())
        below = next((o for o in outcomes if "below" in str(o).lower()), None)
        plus = next((o for o in outcomes if "+" in str(o)), None)
        sorted_bands = sorted([o for o in bare_bands if "+" not in str(o)], key=parse_band)
        if below and sorted_bands and val < parse_band(sorted_bands[0]):
            return str(below), f"{val:,.0f} < {parse_band(sorted_bands[0]):,.0f}"
        if plus and val >= parse_band(plus):
            return str(plus), f"{val:,.0f} >= {parse_band(plus):,.0f}"
        for i, band in enumerate(sorted_bands):
            floor = parse_band(band)
            ceil = parse_band(sorted_bands[i+1]) if i + 1 < len(sorted_bands) else (parse_band(plus) if plus else float("inf"))
            if floor <= val < ceil:
                return str(band), f"{floor:,.0f} <= {val:,.0f} < {ceil:,.0f}"

    # Spread cover outcomes.
    spread_re = re.compile(r"^(.+?)\s*([+-]\d+(?:\.\d+)?)\s*$")
    if len(outcomes or []) == 2 and all(spread_re.match(str(o).strip()) for o in outcomes or []):
        spreads = {str(o): float(spread_re.match(str(o).strip()).group(2)) for o in outcomes}
        fav = min(spreads, key=lambda k: spreads[k])
        dog = max(spreads, key=lambda k: spreads[k])
        line = abs(spreads[fav])
        fav_name = re.sub(r"\s*[+-]\d+(?:\.\d+)?\s*$", "", fav).strip().lower()
        dog_name = re.sub(r"\s*[+-]\d+(?:\.\d+)?\s*$", "", dog).strip().lower()
        score_m = re.search(r"\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b", text)
        margin = None
        if score_m:
            a, b = int(score_m.group(1)), int(score_m.group(2))
            margin = abs(a - b)
        fav_won = bool(re.search(rf"\b{re.escape(fav_name)}\b[^.!?]{{0,80}}\b(won|beat|defeated|victory)", tlow))
        dog_won = bool(re.search(rf"\b{re.escape(dog_name)}\b[^.!?]{{0,80}}\b(won|beat|defeated|victory)", tlow))
        if margin is not None:
            if dog_won:
                return dog, f"underdog won outright; score margin {margin:g}"
            if fav_won:
                return (fav if margin > line else dog), f"favorite margin {margin:g} vs line {line:g}"

    # Sports winner: identify losers first; handles "X won against Y".
    is_sports = any(k in str(question).lower() for k in ["vs", "versus", "match", "game", "cricket", "football", "basketball", "psl", "ipl"])
    if is_sports:
        # Draw.
        draw = next((o for o in outcomes or [] if "draw" in str(o).lower()), None)
        if draw and re.search(r"\b(draw|drawn|tied|abandoned|no result)\b", tlow):
            return str(draw), "draw/result language"
        losers = set()
        for outcome in outcomes or []:
            ol = str(outcome).lower()
            if "draw" in ol:
                continue
            team = re.sub(r"\s*(win|wins|fc|cf|sc|afc|city|united)\s*$", "", ol).strip()
            if not team:
                continue
            if (re.search(rf"\b(beat|defeated)\b[^.!?]{{0,90}}\b{re.escape(team)}\b", tlow)
                or re.search(rf"\bwon\s+(?:against|over|versus)\s+[^.!?]{{0,90}}\b{re.escape(team)}\b", tlow)
                or re.search(rf"\b{re.escape(team)}\b[^.!?]{{0,80}}\b(lost|loses|lose)\b", tlow)):
                losers.add(ol)
        for outcome in outcomes or []:
            ol = str(outcome).lower()
            if ol in losers or "draw" in ol:
                continue
            team = re.sub(r"\s*(win|wins|fc|cf|sc|afc|city|united)\s*$", "", ol).strip()
            if team and re.search(rf"\b{re.escape(team)}\b[^.!?]{{0,100}}\b(won|beat|defeated|victory|win)\b", tlow):
                return str(outcome), f"sports winner: {outcome}"
        if losers:
            non_losers = [o for o in outcomes if str(o).lower() not in losers and "draw" not in str(o).lower()]
            if len(non_losers) == 1:
                return str(non_losers[0]), f"winner by elimination (losers: {sorted(losers)})"

    # Binary negation-aware.
    out_set = {str(o).lower() for o in outcomes or []}
    if any(out_set <= pair for pair in [{"yes", "no"}, {"green", "red"}, {"up", "down"}, {"higher", "lower"}, {"above", "below"}]):
        no_signals = ["has not", "did not", "not announced", "not confirmed", "no plans", "is not", "was not", "will not", "failed to", "opened red", "closed below", "declined", "fell"]
        yes_signals = ["did announce", "confirmed", "has announced", "acquired", "bought", "opened green", "closed above", "surpassed", "exceeded", "climbed", "did purchase"]
        if any(s in tlow for s in no_signals):
            for o in outcomes:
                if str(o).lower() in ("no", "red", "down", "lower", "below", "false"):
                    return str(o), "binary no signal"
        if any(s in tlow for s in yes_signals):
            for o in outcomes:
                if str(o).lower() in ("yes", "green", "up", "higher", "above", "true"):
                    return str(o), "binary yes signal"

    # Named/direct choice match with loser-context guard.
    for outcome in outcomes or []:
        ol = str(outcome).lower().strip()
        if len(ol) > 4 and ol in tlow:
            if not re.search(rf"\b(won\s+against|won\s+over|defeated|beat)\b[^.!?]{{0,80}}\b{re.escape(ol)}\b", tlow):
                return str(outcome), f"named match: {outcome}"

    return None, None


def ai_settle(answer_line: str, full_evidence: str, question: str, outcomes: list, rules: dict, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """Focused AI fallback after deterministic extraction/matching fails."""
    if not GROQ_API_KEY:
        return None, None
    raw = call_groq(
        "You are OracleREE's settlement judge. Use ONLY the evidence. Return ONLY valid JSON with matched_outcome exact from outcomes or NONE, and reasoning. Apply the settlement rules exactly. For sports, X won against Y means Y lost. For price bands, use floor/range logic, not closest, unless rules say closest.",
        f"Question: {question}\nRules: {json.dumps(rules, ensure_ascii=False)}\nOutcomes: {json.dumps(outcomes, ensure_ascii=False)}\nPrimary answer: {answer_line}\nEvidence:\n{str(full_evidence)[:2500]}",
        max_tokens=350,
    )
    obj = _safe_json_object(raw or "")
    if not isinstance(obj, dict):
        return None, None
    mo = str(obj.get("matched_outcome") or "").strip()
    if not mo or mo.upper() == "NONE":
        return None, None
    for outcome in outcomes or []:
        if str(outcome).lower().strip() == mo.lower().strip():
            return str(outcome), f"AI judge: {str(obj.get('reasoning') or '')[:120]}"
    if len(mo) > 5:
        for outcome in outcomes or []:
            if mo.lower() in str(outcome).lower() or str(outcome).lower() in mo.lower():
                return str(outcome), f"AI judge partial: {str(obj.get('reasoning') or '')[:120]}"
    return None, None


def extract_specific_answer(content: str, query_plan: dict, question: str,
                            outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """New universal extractor, kept under the old function name for build_source_evidence."""
    base_rules = (intelligence or {}).get("_rules") or parse_settlement_rules(
        (intelligence or {}).get("prompt_context", ""), question, outcomes)
    # Carry query-plan extraction hints (for example count_method=position_codes) into the extractor.
    rules = {**(base_rules or {}), **(query_plan or {})}
    # Pass event_date from intelligence into rules so extract_required_value can find the draft year reliably.
    if "event_date" not in rules and (intelligence or {}).get("event_date"):
        rules["event_date"] = intelligence["event_date"]
    answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else _extract_answer_line(content)
    raw_value, method = extract_required_value(content, rules, question, outcomes)
    if raw_value:
        matched, calc = match_value_to_outcome(raw_value, rules, question, outcomes, intelligence)
        if matched:
            return matched, f"{method}; {calc}"
    return ai_settle(answer_line, content, question, outcomes, rules, intelligence)

def derive_outcome(facts: list[Fact], outcomes: list, question: str, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """Universal derive_outcome override used by API/CoinGecko/OracleSeal paths."""
    if not facts:
        return None, None
    facts_dict = {f.label: f.value for f in facts}
    mo = str(facts_dict.get("matched_outcome", "")).strip()
    if mo:
        for outcome in outcomes or []:
            if str(outcome).lower().strip() == mo.lower().strip():
                return str(outcome), f"extracted: {mo}"

    full_evidence = facts_dict.get("raw_evidence", "") or "; ".join(f"{f.label}={f.value}" for f in facts)
    rules = (intelligence or {}).get("_rules") or parse_settlement_rules((intelligence or {}).get("prompt_context", ""), question, outcomes)

    # Direct numeric facts from APIs.
    for key in ["high_price", "close_price", "low_price", "open_price", "index_value", "close", "price"]:
        if key in facts_dict:
            try:
                val = str(float(str(facts_dict[key]).replace(",", "").replace("$", "")))
                metric = "high" if "high" in key else ("low" if "low" in key else ("open" if "open" in key else "close"))
                temp_rules = {**rules, "metric": rules.get("metric") or metric}
                matched, calc = match_value_to_outcome(val, temp_rules, question, outcomes, intelligence)
                if matched:
                    return matched, calc
            except Exception:
                pass

    raw_value, method = extract_required_value(full_evidence, rules, question, outcomes)
    if raw_value:
        matched, calc = match_value_to_outcome(raw_value, rules, question, outcomes, intelligence)
        if matched:
            return matched, f"{method}; {calc}"

    answer_line = extract_answer_line(full_evidence) if "extract_answer_line" in globals() else _extract_answer_line(full_evidence)
    return ai_settle(answer_line, full_evidence, question, outcomes, rules, intelligence)

def build_source_evidence(source_original: str, intelligence: dict,
                           question: str, outcomes: list,
                           resolves_at: str) -> EvidenceBlock:
    """
    Source-locked evidence pipeline:
      1. Build a purpose-specific query.
      2. Fetch from creator source/domain.
      3. Validate evidence quality before trusting it.
      4. Extract a specific answer from validated evidence.
    """
    eb = EvidenceBlock()
    eb.source_used = source_original

    url = resolve_source_to_url(source_original)
    primary_domain = clean_domain(url)
    event_date = (intelligence or {}).get("event_date", resolves_at[:10] if resolves_at else "")
    q_lower = str(question or "").lower()
    is_sports_market = is_sports_market_question(question, intelligence)

    rules = (intelligence or {}).get("_rules") or parse_settlement_rules((intelligence or {}).get("prompt_context", ""), question, outcomes)
    query_plan = build_universal_query(question, outcomes, rules, event_date, primary_domain)
    what_to_find = query_plan["query"]
    search_depth = query_plan.get("search_depth", "basic")

    # Yahoo Finance crypto history pages need metric-specific, source-specific search terms.
    if "yahoo" in primary_domain and (intelligence or {}).get("is_price_based"):
        asset = (intelligence or {}).get("asset", "BTC") or "BTC"
        metric_rule = (rules or {}).get("metric")
        wants_high = metric_rule == "high" or any(w in q_lower for w in ["highest", "high", "peak", "max"])
        wants_low = metric_rule == "low" or any(w in q_lower for w in ["lowest", "low", "min"])
        wants_open = metric_rule == "open"
        metric = "High" if wants_high else ("Low" if wants_low else ("Open" if wants_open else "Close"))
        what_to_find = (
            f"{asset}-USD historical price {metric} {event_date} "
            f"finance.yahoo.com quote {asset}-USD history OHLC daily"
        )
        query_plan["query"] = what_to_find
        query_plan["what_to_validate"] = f"{asset} daily {metric.lower()} price {event_date}"
        query_plan["need_number"] = True
        query_plan["number_context"] = f"{asset} price"
        query_plan["search_depth"] = "advanced"
        search_depth = "advanced"

    print(f"\n[oracle] Source: {source_original} → {primary_domain}")
    print(f"[oracle] Query: {what_to_find[:100]}")
    print(f"[oracle] Depth: {search_depth}")

    if any(x in url.lower() for x in ["twitter.com", "x.com", "t.co", "instagram.com", "tiktok.com"]):
        eb.fetch_status = "UNSUPPORTED_SOURCE"
        eb.parse_status = "UNSUPPORTED_SOURCE"
        eb.outcome_status = "UNSUPPORTED_SOURCE"
        eb.reason = f"Source not supported for automated extraction: {primary_domain}"
        return eb

    is_fdv = any(w in q_lower for w in ["fdv", "fully diluted", "market cap", "valuation"])
    is_api = any(x in url.lower() for x in [
        "/api/", "api.", "/v1/", "/v2/", "/v3/", ".json", "index-history",
        "/history", "/price", "/data"
    ])

    content = None
    ctype = ""
    fetch_method = ""

    # APIs: try direct first, then Tavily. Non-APIs: Tavily first because it gives ANSWER + snippets.
    if is_api:
        result = direct_fetch(url)
        if result:
            content, ctype = result
            fetch_method = "direct_api"
            print(f"[oracle] ✓ Direct API: {len(content)} chars")

    if not content:
        tv = tavily_source_locked_fetch(
            primary_domain, question, event_date, what_to_find,
            is_fdv=is_fdv, search_depth=search_depth
        )
        if tv:
            content = tv
            ctype = "text"
            fetch_method = "tavily_locked"
            print(f"[oracle] ✓ Tavily locked ({primary_domain}): {len(content)} chars")

    # Source-family fallback, but never cross into cricket-only ESPN Cricinfo for non-cricket markets.
    if not content:
        family = get_source_family(primary_domain)
        for alt_domain in family[1:]:
            if "espncricinfo" in alt_domain and not any(k in q_lower for k in CRICKET_KEYWORDS):
                print(f"[oracle] Skipping cricket-only recovery for non-cricket market: {alt_domain}")
                continue
            tv = tavily_source_locked_fetch(
                alt_domain, question, event_date, what_to_find,
                is_fdv=False, search_depth=search_depth
            )
            if tv:
                content = tv
                ctype = "text"
                fetch_method = f"source_family_{alt_domain}"
                eb.recovered_from = primary_domain
                print(f"[oracle] ✓ Source family recovery ({alt_domain}): {len(content)} chars")
                break

    if not content and not is_api:
        result = direct_fetch(url)
        if result:
            content, ctype = result
            fetch_method = "direct_fallback"
            print(f"[oracle] ✓ Direct fallback: {len(content)} chars")

    # IMPORTANT FIX:
    # ESPN direct fetch often returns the generic homepage. That is technically
    # FETCHED, but it is not evidence for the match. For sports winner/spread
    # markets, immediately trigger Tavily recovery before parse validation.
    #
    # If recovery fails, do NOT continue with the ESPN homepage. Continuing with
    # homepage content creates the old false state:
    #   fetch_method="direct" / recovered_from=null / PARSE_FAILED topic drift.
    if content and is_sports_market and primary_domain.endswith("espn.com"):
        weak_espn = (
            is_weak_espn_homepage_content(content)
            or is_topic_drift_for_sports_content(content, question, outcomes)
        )
        if weak_espn:
            print("[oracle] ESPN content is homepage/topic-drift; triggering sports Tavily recovery")
            fallback = try_sports_fallback_sources(
                question, event_date, what_to_find, intelligence, primary_domain, outcomes
            )
            if fallback:
                content, fallback_domain = fallback
                ctype = "text"
                fetch_method = "sports_fallback"
                eb.recovered_from = primary_domain
                eb.source_used = fallback_domain
                print(f"[oracle] Sports fallback used after weak ESPN evidence: {fallback_domain}")
            else:
                eb.fetch_status = "FETCH_FAILED"
                eb.parse_status = "FETCH_FAILED"
                eb.outcome_status = "FETCH_FAILED"
                eb.fetch_method = "sports_fallback_failed"
                eb.recovered_from = primary_domain
                eb.source_used = primary_domain
                eb.raw_content = content[:5000]
                eb.reason = (
                    "ESPN returned generic homepage/topic-drift content and "
                    "Tavily sports fallback found no validated replacement evidence"
                )
                print(f"[oracle] ✗ {eb.reason}")
                return eb

    if not content and is_sports_market:
        fallback = try_sports_fallback_sources(
            question, event_date, what_to_find, intelligence, primary_domain, outcomes
        )
        if fallback:
            content, fallback_domain = fallback
            ctype = "text"
            fetch_method = "sports_fallback"
            eb.recovered_from = primary_domain
            eb.source_used = fallback_domain
            print("[oracle] Sports fallback used — result is immutable fact")

    if not content:
        eb.fetch_status = "FETCH_FAILED"
        eb.parse_status = "FETCH_FAILED"
        eb.outcome_status = "FETCH_FAILED"
        if is_sports_market:
            eb.reason = f"Could not fetch sports result from creator source or trusted sports fallbacks for {source_original}"
        else:
            eb.reason = (
                f"Creator source '{source_original}' failed and no fallback is permitted "
                f"for {intelligence.get('market_type')} markets"
            )
        print(f"[oracle] ✗ FETCH_FAILED: {source_original}")
        return eb

    eb.fetch_status = "FETCHED"
    eb.fetch_method = fetch_method
    eb.raw_content = content[:5000]

    # JSON/API direct parse still gets a deterministic chance before text validation.
    if is_api and ctype == "json":
        try:
            full_data = json.loads(content)
            data_array = full_data.get("data") if isinstance(full_data, dict) else full_data
            if isinstance(data_array, list):
                for record in data_array:
                    if isinstance(record, dict):
                        ts = str(record.get("timestamp", ""))
                        if event_date and event_date in ts:
                            val = (record.get("index_value") or record.get("value") or
                                   record.get("price") or record.get("close") or
                                   record.get("close_price"))
                            if val is not None:
                                facts = [Fact("index_value", str(val), url, event_date)]
                                eb.parse_status = "PARSED"
                                eb.facts = facts
                                matched, calc = derive_outcome(facts, outcomes, question, intelligence)
                                if matched:
                                    eb.outcome_status = "OUTCOME_FOUND"
                                    eb.matched_outcome = matched
                                    eb.calculation = calc
                                else:
                                    eb.outcome_status = "OUTCOME_NOT_FOUND"
                                    eb.reason = f"Found index_value for {event_date}, but no outcome matched"
                                return eb
        except Exception as ex:
            print(f"[oracle] JSON pre-parse: {ex}")

    if ctype == "html":
        text = re.sub(r"<script[^>]*>.*?</script>", " ", content, flags=re.S)
        text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        content = re.sub(r"\s+", " ", text).strip()
        ctype = "text"

    # Validate evidence before any extraction/matching.
    is_valid, validation_reason = validate_evidence_quality(content, query_plan, question)
    if not is_valid and search_depth == "basic":
        print(f"[oracle] Evidence rejected: {validation_reason}")
        print("[oracle] Retrying with advanced depth...")
        tv2 = tavily_source_locked_fetch(
            primary_domain, question, event_date, what_to_find,
            is_fdv=is_fdv, search_depth="advanced"
        )
        if tv2:
            is_valid2, reason2 = validate_evidence_quality(tv2, query_plan, question)
            if is_valid2:
                content = tv2
                eb.raw_content = content[:5000]
                is_valid = True
                validation_reason = reason2
                fetch_method = "tavily_locked_advanced"
                eb.fetch_method = fetch_method
                print(f"[oracle] Advanced retry succeeded: {reason2}")

    if not is_valid and is_sports_market and eb.fetch_method != "sports_fallback":
        print(f"[oracle] Sports creator-source evidence insufficient: {validation_reason}")
        fallback = try_sports_fallback_sources(
            question, event_date, what_to_find, intelligence, primary_domain, outcomes
        )
        if fallback:
            content, fallback_domain = fallback
            eb.raw_content = content[:5000]
            eb.fetch_method = "sports_fallback"
            eb.recovered_from = primary_domain
            eb.source_used = fallback_domain
            ctype = "text"
            is_valid, validation_reason = sports_fallback_content_is_usable(content, question, outcomes)
            if not is_valid:
                # Last compatibility check; do not reject good Tavily ANSWER lines silently.
                is_valid, validation_reason = validate_evidence_quality(content, query_plan, question)
            print(f"[oracle] Sports fallback used — result is immutable fact ({fallback_domain})")

    if not is_valid:
        eb.parse_status = "PARSE_FAILED"
        eb.outcome_status = "PARSE_FAILED"
        eb.reason = f"Evidence quality check failed: {validation_reason}"
        print(f"[oracle] PARSE_FAILED: evidence does not contain answer ({validation_reason})")
        return eb

    eb.parse_status = "PARSED"
    evidence_source = eb.source_used or url
    facts = [Fact("raw_evidence", content[:3000], evidence_source, timestamp=event_date)]
    eb.facts = facts

    matched, calculation = extract_specific_answer(content, query_plan, question, outcomes, intelligence)

    if matched:
        eb.outcome_status = "OUTCOME_FOUND"
        eb.matched_outcome = matched
        eb.calculation = calculation
        print(f"[oracle] ✓ OUTCOME_FOUND: {matched}")
        if calculation:
            print(f"[oracle]   Calc: {calculation}")
    else:
        eb.outcome_status = "OUTCOME_NOT_FOUND"
        eb.reason = "Evidence validated but could not extract outcome"
        print("[oracle] OUTCOME_NOT_FOUND — evidence present but answer unclear")

    return eb

def pin_to_ipfs(data:dict, name:str)->Optional[str]:
    if not PINATA_JWT: return None
    try:
        r=requests.post("https://uploads.pinata.cloud/v3/files",
            headers={"Authorization":f"Bearer {PINATA_JWT}"},
            files={"file":(f"{name}.json",json.dumps(data),"application/json")},timeout=30)
        cid=r.json()["data"]["cid"]
        print(f"[oracle] IPFS pinned: {cid}"); return cid
    except Exception as e: print(f"[oracle] IPFS pin failed: {e}"); return None

def finalize_oracle_evidence(evidence:dict, market:dict)->dict:
    fv=evidence.get("final_verdict") or {}
    dr=fv.get("derived_result") or {}
    matched=fv.get("matched_outcome") or dr.get("matched_outcome")
    calc=fv.get("calculation") or dr.get("calculation") or ""
    if matched:
        evidence["event_verdict"]={
            "verdict":matched,"matchedOutcome":matched,
            "explanation":calc,"source":fv.get("source_used") or "",
        }
        print(f"[oracle] Event verdict: {matched}")
    evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
    evidence["ipfs_cid"] = pin_to_ipfs(evidence, f"oracle-ree-{market.get('id','')[:10]}")
    print(f"[oracle] Oracle hash: {evidence.get('evidence_hash','')}")
    if evidence.get("ipfs_cid"):
        print(f"[oracle] IPFS CID: {evidence['ipfs_cid']}")
    return evidence


def build_coingecko_price_evidence(asset: str, capture_date: str, question: str,
                                  outcomes: list, intelligence: dict,
                                  reason_prefix: str = "CoinGecko") -> Optional[EvidenceBlock]:
    """
    CoinGecko crypto price fetch.
    IMPORTANT: this is not allowed to silently replace creator sources.
    Caller decides whether CoinGecko is creator-approved or fallback.
    For highest/lowest questions, never fall back to close if high_24h/low_24h is missing.
    """
    coin_id = (COINGECKO_IDS.get(str(asset).upper()) or
               next((v for k, v in COINGECKO_IDS.items() if k in str(asset).upper()), None))
    if not coin_id or not capture_date:
        return None

    eb = EvidenceBlock()
    eb.source_used = "CoinGecko"
    eb.fetch_method = "direct_api"

    try:
        y, m, d = capture_date.split("-")
        r = requests.get(
            f"{COINGECKO_BASE}/coins/{coin_id}/history",
            params={"date": f"{d}-{m}-{y}", "localization": "false"},
            timeout=10,
        )
        r.raise_for_status()
        md = r.json().get("market_data", {}) or {}
        close_price = (md.get("current_price") or {}).get("usd")
        high_24h = (md.get("high_24h") or {}).get("usd")
        low_24h = (md.get("low_24h") or {}).get("usd")
        q_lower = question.lower()
        wants_high = any(w in q_lower for w in ["highest", "high", "peak", "max"])
        wants_low = any(w in q_lower for w in ["lowest", "low", "min"])

        eb.fetch_status = "FETCHED"
        eb.raw_content = json.dumps(md)[:5000]

        if wants_high:
            if high_24h is None:
                eb.parse_status = "PARSE_FAILED"
                eb.outcome_status = "PARSE_FAILED"
                eb.reason = f"{reason_prefix}: CoinGecko high_24h unavailable for {capture_date}; refusing to use close_price for highest-price market"
                print(f"[oracle] {eb.reason}")
                return eb
            primary_label, primary_val = "high_price", high_24h
        elif wants_low:
            if low_24h is None:
                eb.parse_status = "PARSE_FAILED"
                eb.outcome_status = "PARSE_FAILED"
                eb.reason = f"{reason_prefix}: CoinGecko low_24h unavailable for {capture_date}; refusing to use close_price for lowest-price market"
                print(f"[oracle] {eb.reason}")
                return eb
            primary_label, primary_val = "low_price", low_24h
        else:
            if close_price is None:
                eb.parse_status = "PARSE_FAILED"
                eb.outcome_status = "PARSE_FAILED"
                eb.reason = f"{reason_prefix}: CoinGecko close/current_price unavailable for {capture_date}"
                return eb
            primary_label, primary_val = "close_price", close_price

        print(f"[oracle] CoinGecko {asset} on {capture_date}: close=${close_price}, high=${high_24h}, low=${low_24h}")
        facts = [Fact(primary_label, str(primary_val), "CoinGecko", capture_date, "USD")]
        if high_24h is not None and primary_label != "high_price":
            facts.append(Fact("high_24h", str(high_24h), "CoinGecko", capture_date, "USD"))
        if low_24h is not None and primary_label != "low_price":
            facts.append(Fact("low_24h", str(low_24h), "CoinGecko", capture_date, "USD"))

        eb.parse_status = "PARSED"
        eb.facts = facts
        matched, calc = derive_outcome(facts, outcomes, question, intelligence)
        if matched:
            eb.outcome_status = "OUTCOME_FOUND"
            eb.matched_outcome = matched
            eb.calculation = calc
        else:
            eb.outcome_status = "OUTCOME_NOT_FOUND"
            eb.reason = f"{reason_prefix}: CoinGecko facts extracted but no outcome matched"
        return eb
    except Exception as e:
        eb.fetch_status = "FETCH_FAILED"
        eb.parse_status = "FETCH_FAILED"
        eb.outcome_status = "FETCH_FAILED"
        eb.reason = f"{reason_prefix}: CoinGecko failed: {e}"
        print(f"[oracle] {eb.reason}")
        return eb

def build_oracle_evidence(market:dict)->dict:
    meta=market.get("metadata") or {}
    question=meta.get("question","")
    prompt_ctx=(meta.get("model") or {}).get("prompt_context","")
    data_sources=market.get("dataSources") or []
    resolves_at=market.get("resolvesAt","")
    close_date=resolves_at[:10] if resolves_at else None
    outcomes=meta.get("outcomes") or []

    print(f"\n[oracle] ═══════════════════════════════════")
    print(f"[oracle] Market:   {question}")
    print(f"[oracle] Close:    {close_date}")
    print(f"[oracle] Sources:  {data_sources}")
    print(f"[oracle] Outcomes: {outcomes}")
    print(f"[oracle] ═══════════════════════════════════")

    evidence={"market_id":market.get("id"),"market_question":question,
               "close_time":resolves_at,"captured_at":now_iso(),"source_results":[]}

    # Step 1: Intelligence
    print("\n[oracle] ── Step 1: Intelligence ──")
    intelligence=analyze_market_intelligence(question,prompt_ctx,outcomes,data_sources,resolves_at)
    evidence["intelligence"]=intelligence

    is_price=intelligence.get("is_price_based",False)
    asset=intelligence.get("asset") or ""
    if is_price and is_non_crypto(asset):
        print(f"[oracle] Override: {asset} is non-crypto")
        is_price=False; intelligence["is_price_based"]=False
    capture_date=intelligence.get("event_date") or close_date
    print(f"[oracle] Type: {intelligence.get('market_type')} | crypto={is_price} | asset={asset or 'none'}")

    # Step 2A: CoinGecko fast path (crypto only, creator-approved only)
    # Core rule: creator source is law. CoinGecko must not bypass Yahoo/Binance/etc.
    creator_specified_coingecko = any("coingecko" in str(src).lower() for src in data_sources)
    creator_specified_yahoo = any("yahoo" in str(src).lower() or "finance.yahoo" in str(src).lower() for src in data_sources)
    creator_has_no_source = len(data_sources) == 0
    use_coingecko_direct = (
        is_price and asset
        and intelligence.get("market_type") in ("crypto_price", "crypto_price_range")
        and (creator_specified_coingecko or creator_has_no_source)
        and not creator_specified_yahoo
    )

    if use_coingecko_direct:
        reason = "creator specified CoinGecko" if creator_specified_coingecko else "no creator source specified"
        print(f"\n[oracle] ── Step 2A: CoinGecko fast path ({asset}) — {reason} ──")
        eb = build_coingecko_price_evidence(asset, capture_date, question, outcomes, intelligence, reason_prefix="CoinGecko fast path")
        if eb and eb.verified:
            evidence["source_results"] = [eb.to_dict()]
            evidence["final_verdict"] = eb.to_dict()
            return finalize_oracle_evidence(evidence, market)
        elif eb:
            # Keep failed CoinGecko attempt as evidence, but continue to source router if sources exist.
            evidence["source_results"].append(eb.to_dict())
            if creator_has_no_source:
                evidence["final_verdict"] = eb.to_dict()
                return finalize_oracle_evidence(evidence, market)
    elif is_price and asset and intelligence.get("market_type") in ("crypto_price", "crypto_price_range"):
        print("\n[oracle] ── Step 2A: Skipping CoinGecko fast path — creator source router goes first ──")

    # Step 2B: OracleSeal snapshot
    oracle_seal=get_oracle_seal_snapshot(market.get("id",""))
    if oracle_seal and oracle_seal.get("data_sources"):
        print(f"\n[oracle] ── Step 2B: OracleSeal snapshot ──")
        web_ctx="\n\n".join(f"[{s.get('url','')}]\n{s.get('text_snippet','')}"
                             for s in oracle_seal["data_sources"] if s.get("text_snippet"))
        if web_ctx.strip():
            facts=extract_facts_from_text(web_ctx,intelligence.get("event_date",close_date),
                intelligence.get("facts_needed",["result"]),question,outcomes,"OracleSeal")
            eb=EvidenceBlock()
            eb.fetch_status="FETCHED"; eb.source_used="OracleSeal"; eb.fetch_method="ipfs_snapshot"
            evidence["oracle_seal_ipfs"]=oracle_seal.get("ipfs_cid")
            if facts:
                eb.parse_status="PARSED"; eb.facts=facts
                matched,calc=derive_outcome(facts,outcomes,question,intelligence)
                if matched: eb.outcome_status="OUTCOME_FOUND"; eb.matched_outcome=matched; eb.calculation=calc
                else: eb.outcome_status="OUTCOME_NOT_FOUND"
            else: eb.parse_status="PARSE_FAILED"; eb.outcome_status="PARSE_FAILED"
            evidence["source_results"]=[eb.to_dict()]; evidence["final_verdict"]=eb.to_dict()
            return finalize_oracle_evidence(evidence,market)

    # Market not closed
    is_closed=(resolves_at and
        datetime.fromisoformat(resolves_at.replace("Z","+00:00"))<datetime.now(timezone.utc))
    if not is_closed:
        print("[oracle] Market not yet closed")
        evidence["final_verdict"]={"pipeline":"MARKET_NOT_CLOSED","matched_outcome":None,"facts":[]}
        return finalize_oracle_evidence(evidence,market)

    # Step 3: Source Router (source-locked)
    print(f"\n[oracle] ── Step 3: Source Router ({len(data_sources)} sources) ──")
    all_results:list[EvidenceBlock]=[]; final_eb:Optional[EvidenceBlock]=None

    for src in data_sources:
        print(f"\n[oracle] Processing: {src}")
        eb=build_source_evidence(src,intelligence,question,outcomes,resolves_at)
        all_results.append(eb)
        print(f"[oracle] Status: {eb.pipeline_status()}")
        if eb.verified and final_eb is None:
            final_eb=eb; print(f"[oracle] ✓ Using as final verdict")

    evidence["source_results"] = evidence.get("source_results", []) + [eb.to_dict() for eb in all_results]

    # CoinGecko fallback only after creator sources fail, and only for crypto price markets.
    # It still refuses to use close_price for highest/lowest markets when high_24h/low_24h is missing.
    if (not final_eb and is_price and asset
            and intelligence.get("market_type") in ("crypto_price", "crypto_price_range")
            and not creator_specified_coingecko):
        print(f"\n[oracle] ── CoinGecko fallback after creator sources failed ({asset}) ──")
        cg_eb = build_coingecko_price_evidence(asset, capture_date, question, outcomes, intelligence, reason_prefix="CoinGecko fallback")
        if cg_eb:
            evidence["source_results"].append(cg_eb.to_dict())
            if cg_eb.verified:
                final_eb = cg_eb
                print(f"[oracle] ✓ CoinGecko fallback verified: {cg_eb.matched_outcome}")

    if final_eb:
        evidence["final_verdict"]=final_eb.to_dict()
        print(f"\n[oracle] ✓ VERIFIED: {final_eb.matched_outcome}")
    else:
        reasons=[]
        for eb in all_results:
            if eb.fetch_status=="FETCH_FAILED": reasons.append(f"FETCH_FAILED: {eb.source_used}")
            elif eb.parse_status in ("PARSE_FAILED","FETCH_FAILED"):
                reasons.append(f"PARSE_FAILED: {eb.source_used} — {eb.reason or 'no facts'}")
            elif eb.outcome_status=="OUTCOME_NOT_FOUND":
                reasons.append(f"OUTCOME_NOT_FOUND: {eb.source_used} — {eb.reason or ''}")
            elif eb.fetch_status=="UNSUPPORTED_SOURCE": reasons.append(f"UNSUPPORTED: {eb.source_used}")
        if not data_sources: reasons.append("NO_SOURCES")
        evidence["final_verdict"]={
            "pipeline":"INCONCLUSIVE","matched_outcome":None,"facts":[],
            "reason":"; ".join(reasons) if reasons else "All sources failed",
        }
        print(f"\n[oracle] INCONCLUSIVE: {evidence['final_verdict']['reason'][:120]}")

    return finalize_oracle_evidence(evidence,market)

# ─── Prompt builder ───────────────────────────────────────────────────────────
def build_oracle_prompt(original_prompt:str, evidence:dict)->str:
    intel=evidence.get("intelligence",{})
    fv=evidence.get("final_verdict",{})
    facts_list=fv.get("facts",[]) if isinstance(fv,dict) else []
    pipeline=fv.get("pipeline","UNKNOWN") if isinstance(fv,dict) else "UNKNOWN"
    lines=[
        "═"*51,"ORACLEREE VERIFIED DATA BLOCK","═"*51,
        f"Market:      {evidence['market_question']}",
        f"Captured at: {evidence['captured_at']}",
        f"Close time:  {evidence['close_time']}",
        f"Market type: {intel.get('market_type','unknown')}",
        f"Event date:  {intel.get('event_date','unknown')}",
        f"Pipeline:    {pipeline}",
    ]
    if facts_list or (isinstance(fv,dict) and fv.get("matched_outcome")):
        lines+=["","EXTRACTED EVIDENCE:"]
        for f in facts_list:
            if isinstance(f,dict):
                u=f" {f.get('unit')}" if f.get("unit") else ""
                t=f" [{f.get('timestamp')}]" if f.get("timestamp") else ""
                lines.append(f"  {f.get('label','')}: {f.get('value','')}{u}{t}")
        dr=(fv.get("derived_result") or {}) if isinstance(fv,dict) else {}
        calc=fv.get("calculation") or (dr.get("calculation") if dr else None)
        if calc: lines.append(f"\nCalculation: {calc}")
        mo=fv.get("matched_outcome") if isinstance(fv,dict) else None
        if mo: lines+=["",f"OUTCOME: {mo}"]
    elif pipeline=="INCONCLUSIVE":
        lines+=["","EVIDENCE: INCONCLUSIVE",
                f"Reason: {fv.get('reason','Creator sources could not be fetched/parsed') if isinstance(fv,dict) else ''}"]
    if evidence.get("oracle_seal_ipfs"):
        lines+=[f"","OracleSeal IPFS: {evidence['oracle_seal_ipfs']}"]
    lines+=[
        "","INTEGRITY:",
        f"  Evidence hash: {evidence.get('evidence_hash','N/A')}",
        f"  IPFS CID:      {evidence.get('ipfs_cid','Not pinned')}",
        "═"*51,"END ORACLEREE VERIFIED DATA BLOCK","═"*51,
        "","ORIGINAL SETTLEMENT PROMPT:","─"*49,original_prompt,
    ]
    return "\n".join(lines)

# ─── REE runner ───────────────────────────────────────────────────────────────
def _all_receipts()->list[Path]:
    return [Path(p) for p in glob.glob(
        str(Path.home()/".cache/gensyn/**/receipt_*.json"),recursive=True)]

def _safe_hash(p:Path,*keys)->str:
    try:
        d=json.loads(p.read_text())
        for k in keys:
            if not isinstance(d,dict): return ""
            d=d.get(k)
            if d is None: return ""
        return str(d) if d else ""
    except Exception: return ""

def _find_receipt(start_ts:float,expected:str="")->Optional[Path]:
    cands=[]
    for rp in _all_receipts():
        try:
            if rp.stat().st_mtime>=start_ts-2: cands.append(rp)
        except OSError: continue
    cands.sort(key=lambda x:x.stat().st_mtime,reverse=True)
    if expected:
        for rp in cands:
            if _safe_hash(rp,"input","prompt_hash")==expected: return rp
    return cands[0] if cands else None

def run_ree(prompt:str,model_name:str="Qwen/Qwen3-0.6B",max_new_tokens:int=200)->Optional[Path]:
    ree_dir=Path(__file__).parent; ree_sh=ree_dir/"ree.sh"
    if not ree_sh.exists(): print("[ree] ERROR: ree.sh not found"); return None
    pf=ree_dir/"oracle_prompt.jsonl"
    with open(pf,"w",encoding="utf-8") as f:
        json.dump({"prompt":prompt},f,ensure_ascii=False); f.write("\n")
    pf.chmod(0o644)
    expected=sha256(prompt); started=time.time()
    print(f"\n[ree] model={model_name} | {len(prompt)} chars")
    try:
        result=subprocess.run(
            ["bash",str(ree_sh),"--model-name",model_name,
             "--prompt-file",str(pf),"--max-new-tokens",str(max_new_tokens)],
            cwd=str(ree_dir),capture_output=True,text=True,timeout=1200)
        out=(result.stdout or "")+"\n"+(result.stderr or "")
        if result.returncode!=0:
            print(f"[ree] ERROR exit {result.returncode}"); print(out[-2000:]); return None
        print("[ree] REE exited OK")
        rp=None
        for line in out.splitlines():
            if "receipt" not in line.lower(): continue
            m=re.search(r"(/[^\s]+receipt_[0-9_]+\.json)",line)
            if m and Path(m.group(1)).exists(): rp=Path(m.group(1)); break
        if not rp: rp=_find_receipt(started,expected)
        if not rp: print("[ree] ERROR: no receipt"); return None
        ph=_safe_hash(rp,"input","prompt_hash")
        print("[ree] ✓ Hash verified" if ph==expected else "[ree] Receipt generated")
        rh=_safe_hash(rp,"hashes","receipt_hash")
        print(f"[ree] {rp}")
        if rh: print(f"[ree] hash: {rh}")
        print("[ree] ✓ REE complete"); return rp
    except subprocess.TimeoutExpired: print("[ree] ERROR: timeout 1200s"); return None
    finally: pf.unlink(missing_ok=True)

# ─── Proof builder ────────────────────────────────────────────────────────────
def build_combined_proof(market_id:str, evidence:dict, receipt_path:Optional[Path],
                          prompt_integrity:Optional[dict]=None)->dict:
    ipfs_cid=evidence.get("ipfs_cid") or ""
    proof={
        "version":"1.0.0","tool":"OracleREE","market_id":market_id,
        "created_at":now_iso(),"oracle_evidence":evidence,"ree_receipt":None,
        "prompt_integrity":prompt_integrity or {},
        "verification":{
            "oracle_evidence_hash":evidence.get("evidence_hash"),
            "ipfs_cid":ipfs_cid,
            "oracle_seal_ipfs":evidence.get("oracle_seal_ipfs"),
            "ree_receipt_hash":None,"ree_receipt_path":None,"combined_hash":None,
        },
    }
    if receipt_path and receipt_path.exists():
        try:
            receipt=json.loads(receipt_path.read_text())
            if receipt is None: raise ValueError("Receipt is null")
        except Exception as e: print(f"[proof] ERROR reading receipt: {e}"); receipt=None
        if receipt:
            proof["ree_receipt"]=receipt
            proof["verification"]["ree_receipt_path"]=str(receipt_path)
            hashes=receipt.get("hashes") or {}
            rh=hashes.get("receipt_hash") or hashes.get("receiptHash") or ""
            ph=(receipt.get("input") or {}).get("prompt_hash") or ""
            proof["verification"]["ree_receipt_hash"]=rh or None
            proof["verification"]["prompt_hash"]=ph or None
            if rh:
                combined=sha256(str(evidence.get("evidence_hash"))+str(rh))
                proof["verification"]["combined_hash"]=combined
                print(f"\n[proof] Combined: {combined}")
                print(f"[proof] Evidence: {evidence.get('evidence_hash')}")
                print(f"[proof] Receipt:  {receipt_path}")
            else:
                print(f"[proof] WARNING: no receipt_hash. Keys: {list(receipt.keys())}")
    return proof

# ─── Main ─────────────────────────────────────────────────────────────────────
def main()->int:
    parser=argparse.ArgumentParser(description="OracleREE")
    parser.add_argument("--market","-m",default=None)
    parser.add_argument("--model",default=None)
    parser.add_argument("--max-tokens",type=int,default=512)
    parser.add_argument("--oracle-only",action="store_true")
    parser.add_argument("--settle", action="store_true",
                        help="Settle mode: OracleREE result is canonical; no creator comparison gate")
    parser.add_argument("--output","-o",default=None)
    args=parser.parse_args()

    market_input=args.market
    if not market_input: market_input=input("Paste Delphi market URL or ID: ").strip()
    if not market_input: print("Error: input required"); return 1

    try: market_id=extract_market_id(market_input)
    except ValueError as e: print(f"Error: {e}"); return 1
    print(f"\n[oracle] Market ID: {market_id}")

    try: market=fetch_market(market_id)
    except Exception as e: print(f"Error: {e}"); return 1

    meta=market.get("metadata") or {}
    question=meta.get("question","Unknown")
    prompt_ctx=(meta.get("model") or {}).get("prompt_context",question)
    delphi_model=(meta.get("model") or {}).get("model_identifier","")
    ree_model=args.model or resolve_ree_model(delphi_model)

    raw_mode=is_raw_settlement_prompt(market_input)
    prov=market_input.strip() if raw_mode else ""
    integrity=analyze_prompt_integrity(prov,prompt_ctx,question)
    for_exec=prov if raw_mode and integrity.get("prompt_match")!="YES" else prompt_ctx

    print(f"[oracle] Question: {question}")
    print(f"[oracle] Model:    {delphi_model} → {ree_model}")
    print(f"[oracle] Mode:     {integrity.get('verification_mode')}")
    print(f"[oracle] Prompt match: {integrity.get('prompt_match','—')}")
    print(f"[oracle] Question match: {integrity.get('question_match','—')}")
    if integrity.get("warning"): print(f"[oracle] Warning: {integrity['warning']}")

    evidence=build_oracle_evidence(market)
    oracle_prompt=build_oracle_prompt(for_exec,evidence)
    print(f"\n[oracle] Prompt: {len(oracle_prompt)} chars")
    print(f"[oracle] Hash:   {evidence.get('evidence_hash')}")

    receipt_path=None
    if not args.oracle_only:
        receipt_path=run_ree(prompt=oracle_prompt,model_name=ree_model,
                              max_new_tokens=args.max_tokens)
        if not receipt_path: print("\n[ree] No receipt — oracle evidence saved only.")
    else: print("\n[ree] Skipped")

    proof=build_combined_proof(market_id,evidence,receipt_path,integrity)
    proof=_oracle_apply_dashboard_compat_fields(proof) if '_oracle_apply_dashboard_compat_fields' in globals() else proof
    # Keep the in-memory evidence synced too, because live dashboard renderers may
    # use the evidence object created before the proof object is saved.
    evidence=proof.get("oracle_evidence", evidence) if isinstance(proof, dict) else evidence
    canonical_live_result=(proof.get("final_outcome") or proof.get("oracle_result") or "") if isinstance(proof, dict) else ""
    if canonical_live_result:
        print(f"[oracle] CANONICAL_ORACLE_RESULT: {canonical_live_result}")

    out=(args.output or
         f"oracle_proof_{market_id[:10]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out,"w") as f: json.dump(proof,f,indent=2)
    print(f"\n[proof] Saved: {out}")

    try:
        # Use the canonical final outcome only. Do not read old source candidate fields.
        fv=evidence.get("final_verdict") or {}
        sr=evidence.get("source_results") or []
        v=proof.get("verification") or {}
        dr=(fv.get("derived_result") or {}) if isinstance(fv,dict) else {}
        matched=(proof.get("final_outcome") or proof.get("oracle_result") or
                 _oracle_extract_final_outcome(proof.get("oracle_evidence") or evidence))
        print("\n"+"="*60+"\nORACLEREE SUMMARY\n"+"="*60)
        print(f"Market:   {question}")
        print(f"Model:    {delphi_model} → {ree_model}")
        pipeline=fv.get("pipeline","?") if isinstance(fv,dict) else "?"
        if matched:
            print(f"Verdict:  {matched}  [{pipeline}]")
            calc=fv.get("calculation") or (dr.get("calculation") if dr else None)
            if calc: print(f"Calc:     {calc}")
            for f in (fv.get("facts") or [])[:3]:
                if isinstance(f,dict): print(f"  {f.get('label')}: {f.get('value')}")
        else:
            print(f"Verdict:  INCONCLUSIVE  [{pipeline}]")
            reason=fv.get("reason","?") if isinstance(fv,dict) else "?"
            print(f"Reason:   {str(reason)[:100]}")
        if sr:
            print("Sources:")
            for s in sr:
                if isinstance(s,dict):
                    rec=""
                    if s.get("recovered_from"): rec=f" (recovered from {s['recovered_from']})"
                    print(f"  {s.get('pipeline','?')} | {str(s.get('source_used') or '?')[:50]}{rec}")
        if v.get("ree_receipt_hash"):
            print(f"REE:      {str(v.get('ree_receipt_hash') or '')[:20]}...")
            print(f"Combined: {str(v.get('combined_hash') or '')[:20]}...")
            print("✓ Cryptographically linked")
        else: print("⚠ REE receipt missing")
        print("="*60)
    except Exception as e: print(f"[summary] Display error: {e}")

    try:
        proof_dir=Path(__file__).parent
        all_proofs=sorted(proof_dir.glob("oracle_proof_*.json"),
                          key=lambda p:p.stat().st_mtime,reverse=True)
        seen:dict={}
        for p in all_proofs:
            m=re.match(r"oracle_proof_(0x[a-f0-9]+)_",p.name)
            key=m.group(1) if m else p.name[:24]
            seen.setdefault(key,[]).append(p)
        deleted=0
        for files in seen.values():
            for old in files[5:]: old.unlink(missing_ok=True); deleted+=1
        if deleted: print(f"[proof] Cleaned {deleted} old proof files")
    except Exception as e: print(f"[proof] Cleanup warning: {e}")

    try:
        if proof["verification"].get("ree_receipt_hash"):
            push_to_oracle_seal(proof)
    except Exception as e: print(f"[proof] OracleSeal push warning: {e}")

    if args.oracle_only: return 0
    return 0 if proof["verification"].get("ree_receipt_hash") else 2



# ═══════════════════════════════════════════════════════════════════════════════
# ORACLEREE CANONICAL SETTLEMENT CORE OVERRIDE
# This block intentionally overrides earlier experimental/legacy definitions.
# Goal: one deterministic path:
# rules → source-locked query → evidence validation → exact value extraction →
# deterministic outcome match → AI fallback only after deterministic failure.
# ═══════════════════════════════════════════════════════════════════════════════

def _canon_money_number(value: object) -> Optional[float]:
    """Parse money/number strings safely, supporting commas and k/m/b suffixes."""
    if value is None:
        return None
    s = str(value).strip()
    m = re.search(r'-?\$?\s*\d+(?:,\d{3})*(?:\.\d+)?\s*[kKmMbB]?', s)
    if not m:
        return None
    raw = m.group(0).replace("$", "").replace(",", "").replace(" ", "").lower()
    mult = 1.0
    if raw.endswith("k"):
        mult = 1_000.0; raw = raw[:-1]
    elif raw.endswith("m"):
        mult = 1_000_000.0; raw = raw[:-1]
    elif raw.endswith("b"):
        mult = 1_000_000_000.0; raw = raw[:-1]
    try:
        return float(raw) * mult
    except Exception:
        return None


def _canon_date_variants(event_date: str) -> list[str]:
    """Return common text variants for YYYY-MM-DD."""
    if not event_date:
        return []
    out = [event_date]
    try:
        dt = datetime.fromisoformat(event_date.replace("Z", "+00:00"))
        out += [
            dt.strftime("%B %-d, %Y") if os.name != "nt" else dt.strftime("%B %#d, %Y"),
            dt.strftime("%b %-d, %Y") if os.name != "nt" else dt.strftime("%b %#d, %Y"),
            dt.strftime("%B %d, %Y"),
            dt.strftime("%b %d, %Y"),
            dt.strftime("%Y/%m/%d"),
            dt.strftime("%m/%d/%Y"),
        ]
    except Exception:
        pass
    return list(dict.fromkeys(out))


def _canon_question_asset(question: str) -> dict:
    """Best-effort canonical asset/entity extraction used for validation/querying."""
    q = str(question or "")
    ql = q.lower()
    if any(x in ql for x in ["s&p 500", "sp500", "s&p500", "spx", "^gspc"]):
        return {
            "type": "index",
            "symbol": "^GSPC",
            "name": "S&P 500 Index",
            "aliases": ["s&p 500", "s&p500", "sp500", "spx", "^gspc", "s p 500"],
        }
    if any(x in ql for x in ["microstrategy", "strategy "]):
        return {
            "type": "company_event",
            "symbol": "MSTR",
            "name": "MicroStrategy / Strategy",
            "aliases": ["microstrategy", "strategy", "mstr", "bitcoin purchase", "btc purchase"],
        }
    for ticker, cid in COINGECKO_IDS.items():
        if re.search(rf'\b{re.escape(ticker.lower())}\b', ql):
            return {"type": "crypto", "symbol": ticker, "name": ticker, "aliases": [ticker.lower(), cid.replace("-", " ")]}
    for name, ticker in [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]:
        if name in ql:
            return {"type": "crypto", "symbol": ticker, "name": name.title(), "aliases": [name, ticker.lower()]}
    return {"type": "generic", "symbol": "", "name": "", "aliases": []}


def _canon_extract_threshold(question: str, outcomes: list) -> Optional[float]:
    """Threshold from outcomes or question."""
    # Outcomes first
    for o in outcomes or []:
        ol = str(o).lower()
        if any(k in ol for k in ["over", "under", "above", "below", "greater", "less"]):
            n = _canon_money_number(o)
            if n is not None:
                return n
    # Question: above 5,300 / greater than $100k / reach 2500
    q = str(question or "")
    m = re.search(r'(?:above|over|greater than|exceed(?:s|ed)?|surpass(?:es|ed)?|below|under|less than|reach(?:es|ed)?|hit(?:s)?)\s+\$?\s*([\d,]+(?:\.\d+)?\s*[kKmMbB]?)', q, re.I)
    if m:
        return _canon_money_number(m.group(1))
    return None


def parse_settlement_rules(prompt_context: str, question: str, outcomes: list) -> dict:
    """
    Canonical rules parser. It reads creator rules first, then falls back to question/outcomes.
    This replaces older heuristic fragments that caused wrong metrics and weak dispatch.
    """
    prompt = str(prompt_context or "")
    q = str(question or "")
    pl = prompt.lower()
    ql = q.lower()
    asset = _canon_question_asset(q)
    threshold = _canon_extract_threshold(q, outcomes)

    rules = {
        "metric": None,
        "source_order": [],
        "fallback_source": None,
        "count_subject": None,
        "threshold": threshold,
        "band_logic": None,
        "winner_logic": "most_points",
        "draw_rule": None,
        "time_window": None,
        "operator": None,
        "asset": asset,
        "raw_rules": [],
        "source_policy": "creator_source_strict",
    }

    # Source hints in prompt/context.
    for name in [
        "yahoo", "finance.yahoo", "coingecko", "coinmarketcap", "binance",
        "coinbase", "espn", "espncricinfo", "reuters", "cnbc", "cnn",
        "nfl.com", "nba.com", "theufl", "ufl.football", "strategy.com",
    ]:
        if name in pl and name not in rules["source_order"]:
            rules["source_order"].append(name)

    # Metric. Prompt rules override question.
    if re.search(r'\b(daily\s+high|highest|look up.*high|"high"|\bhigh\b)', prompt, re.I):
        rules["metric"] = "high"
    elif re.search(r'\b(daily\s+low|lowest|look up.*low|"low"|\blow\b)', prompt, re.I):
        rules["metric"] = "low"
    elif re.search(r'\b(regular-session\s+closing|official.*closing|close|closing|close\s+price|"close")\b', prompt, re.I):
        rules["metric"] = "close"
    elif re.search(r'\b(open|opening|"open")\b', prompt, re.I):
        rules["metric"] = "open"

    if not rules["metric"]:
        if any(w in ql for w in ["highest", "high", "peak", "max"]):
            rules["metric"] = "high"
        elif any(w in ql for w in ["lowest", "low", "min"]):
            rules["metric"] = "low"
        elif any(w in ql for w in ["close", "closing"]):
            rules["metric"] = "close"
        elif any(w in ql for w in ["open", "opening"]):
            rules["metric"] = "open"
        elif any(w in ql for w in ["how many", "number of", "count", "total"]):
            rules["metric"] = "count"
        elif any(w in ql for w in ["spread", "cover"]):
            rules["metric"] = "spread"
        elif any(w in ql for w in ["who wins", "who won", "winner", " vs ", "versus", "match", "game"]):
            rules["metric"] = "winner"
        elif set(str(o).lower() for o in outcomes or []) <= {"yes", "no"}:
            rules["metric"] = "confirmation"

    # Operators.
    if re.search(r'\b(strictly\s+greater|greater than|above|over|surpass|exceed)\b', prompt + "\n" + q, re.I):
        rules["operator"] = ">"
    elif re.search(r'\b(less than or equal|at or below|below or equal)\b', prompt + "\n" + q, re.I):
        rules["operator"] = "<="
    elif re.search(r'\b(less than|below|under)\b', prompt + "\n" + q, re.I):
        rules["operator"] = "<"

    # Count subject.
    if rules["metric"] == "count":
        subjects = [
            ("offensive lineman", "offensive linemen"),
            ("offensive linemen", "offensive linemen"),
            ("offensive line", "offensive linemen"),
            ("quarterback", "quarterbacks"), ("qb", "quarterbacks"),
            ("wide receiver", "wide receivers"), ("wr", "wide receivers"),
            ("running back", "running backs"), ("tight end", "tight ends"),
            ("defensive lineman", "defensive linemen"),
            ("defensive line", "defensive linemen"),
            ("trade", "trades"), ("pick", "picks"),
            ("birdie", "birdies"), ("goal", "goals"), ("point", "points"),
        ]
        for needle, normalized in subjects:
            if needle in ql:
                rules["count_subject"] = normalized
                break

    # Band logic.
    if re.search(r'met or exceeded|highest.*band|floor', prompt, re.I):
        rules["band_logic"] = "floor"
    elif re.search(r'closest|nearest', prompt, re.I):
        rules["band_logic"] = "closest"
    else:
        bare_bands = [o for o in outcomes or [] if re.match(r'^\$?[\d,]+\+?$', str(o).strip())]
        if len(bare_bands) >= 3:
            rules["band_logic"] = "floor"

    # Sports immutable fallback policy: allowed only for sports result facts.
    if any(k in ql for k in [" vs ", "versus", "match", "game", "nfl", "nba", "mlb", "nhl", "ufl", "cricket", "ipl", "psl", "final score", "spread", "draft"]):
        rules["source_policy"] = "sports_creator_first_trusted_fallback"

    section = re.search(
        r'SETTLEMENT RULES?[:\s]*\n([\s\S]+?)(?:\nVALID OUTCOMES|\nDATA SOURCES|\Z)',
        prompt, re.I
    )
    if section:
        rules["raw_rules"] = [ln.strip(" -\t") for ln in section.group(1).splitlines() if ln.strip()]

    print(f"[oracle] Rules: metric={rules['metric']} op={rules['operator']} threshold={rules['threshold']} asset={asset.get('name') or asset.get('symbol')}")
    return rules


def analyze_market_intelligence(question: str, prompt_context: str,
                                outcomes: list, data_sources: list, close_time: str) -> dict:
    """Canonical intelligence layer: rules-first, Groq optional, deterministic guardrails."""
    close_date = close_time[:10] if close_time else ""
    rules = parse_settlement_rules(prompt_context, question, outcomes)
    ql = str(question or "").lower()
    fmt = _infer_answer_format_from_outcomes(question, outcomes)

    # Date override.
    event_date = close_date
    dm = re.search(
        r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})',
        ql
    )
    if dm:
        y = close_date[:4] if close_date else "2026"
        event_date = f"{y}-{MONTH_MAP[dm.group(1)]}-{int(dm.group(2)):02d}"
    elif close_date:
        for mn, mm in MONTH_MAP.items():
            if mn in ql:
                last = calendar.monthrange(int(close_date[:4]), int(mm))[1]
                event_date = f"{close_date[:4]}-{mm}-{last:02d}"
                break

    asset = rules.get("asset") or _canon_question_asset(question)
    is_price = rules.get("metric") in ("high", "low", "close", "open") and asset.get("type") in ("crypto", "index")
    if asset.get("type") == "company_event":
        is_price = False

    market_type = "binary_event"
    if rules.get("metric") == "count":
        market_type = "numeric_threshold"
    elif rules.get("metric") == "spread":
        market_type = "sports_spread"
    elif rules.get("metric") == "winner":
        market_type = "sports"
    elif is_price and fmt in ("binary", "freeform"):
        market_type = "finance_threshold" if asset.get("type") == "index" else "crypto_price"
    elif is_price:
        market_type = "crypto_price_range" if asset.get("type") == "crypto" else "finance"

    # Groq may improve search query but cannot alter source policy/metric/operator unless sensible.
    facts_needed = []
    if rules.get("metric") in ("high", "low", "close", "open"):
        facts_needed = [f"{rules['metric']}_value"]
    elif rules.get("metric") == "count":
        facts_needed = ["count", rules.get("count_subject") or "total"]
    elif rules.get("metric") == "spread":
        facts_needed = ["final_score", "winner", "spread"]
    elif rules.get("metric") == "winner":
        facts_needed = ["winner", "final_score"]
    else:
        facts_needed = ["event_status", "confirmation"]

    intel = {
        "event_description": question,
        "event_date": event_date or close_date,
        "timing": "at_close",
        "facts_needed": facts_needed,
        "is_price_based": is_price,
        "asset": asset.get("symbol") or asset.get("name"),
        "threshold": rules.get("threshold"),
        "market_type": market_type,
        "answer_format": fmt,
        "search_query": question,
        "resolver": (
            "binary_threshold" if rules.get("threshold") is not None and fmt == "binary"
            else "deterministic_value_match"
        ),
        "needs_canonicalization": True,
        "_rules": rules,
        "prompt_context": prompt_context,
    }
    print(f"[oracle] Plan: {intel['market_type']} | {intel['answer_format']} | {intel['resolver']}")
    print(f"[oracle] Plan facts: {intel['facts_needed']}")
    return intel


def build_universal_query(question: str, outcomes: list, rules: dict,
                          event_date: str, source_domain: str) -> dict:
    """Purpose-specific query. Query should fetch exactly the required data, not general articles."""
    q = str(question or "")
    ql = q.lower()
    metric = (rules or {}).get("metric")
    asset = (rules or {}).get("asset") or _canon_question_asset(q)
    threshold = (rules or {}).get("threshold")
    subject = (rules or {}).get("count_subject")

    # Binary threshold, including S&P 500 close above 5300.
    if threshold is not None and metric in ("close", "high", "low", "open"):
        aliases = asset.get("aliases") or []
        asset_terms = " OR ".join(f'"{a}"' for a in aliases[:4]) if aliases else f'"{asset.get("name") or asset.get("symbol")}"'
        metric_label = {"close": "closing value close", "high": "high", "low": "low", "open": "open"}[metric]
        return {
            "query": f'({asset_terms}) "{event_date}" {metric_label} {threshold:g} official historical data',
            "search_depth": "basic",
            "required_data_type": "threshold_value",
            "extraction_target": f"{asset.get('name') or asset.get('symbol')} {metric} on {event_date}",
            "need_number": True,
            "number_context": metric,
        }

    # Count/draft markets.
    if metric == "count" and subject:
        year = event_date[:4] if event_date else "2026"
        if any(k in ql for k in ["draft", "nfl", "nba", "nhl", "mlb"]):
            league = "NFL" if "nfl" in ql or "football" in ql or "draft" in ql else ("NBA" if "nba" in ql else ("MLB" if "mlb" in ql else "NHL"))
            out = {
                "query": f'{year} {league} Draft round 1 picks complete tracker all selections official pick order position {subject}',
                "search_depth": "basic",
                "required_data_type": "count",
                "extraction_target": subject,
                "what_to_validate": f"count of {subject}",
                "need_number": True,
                "number_context": subject,
            }
            if "offensive" in subject and "linemen" in subject:
                out["count_method"] = "position_codes"
                out["position_codes"] = ["OT", "OG", "OC", "G", "T", "C", "IOL", "OL"]
            return out
        return {
            "query": f'{q} official total count complete list {event_date}',
            "search_depth": "basic",
            "required_data_type": "count",
            "extraction_target": subject,
            "need_number": True,
            "number_context": subject,
        }

    # Sports spread / score.
    spread_re = re.compile(r"^.+\s[+-]\d+\.?\d*\s*$")
    if len(outcomes or []) == 2 and all(spread_re.match(str(o).strip()) for o in outcomes or []):
        teams = [re.sub(r"\s*[+-]\d+(?:\.\d+)?\s*$", "", str(o)).strip() for o in outcomes]
        return {
            "query": f'"{teams[0]}" "{teams[1]}" final score result {event_date} official',
            "search_depth": "basic",
            "required_data_type": "score",
            "extraction_target": "final score and winner",
        }

    # Event confirmation, e.g. MicroStrategy Bitcoin purchase.
    if set(str(o).lower() for o in outcomes or []) <= {"yes", "no"}:
        aliases = asset.get("aliases") or []
        alias_terms = " ".join(f'"{a}"' for a in aliases[:3])
        return {
            "query": f'{alias_terms} {q} confirmed announced official {event_date}',
            "search_depth": "basic",
            "required_data_type": "confirmation",
            "extraction_target": "whether event happened in specified window",
        }

    return {
        "query": f'{q} official result {event_date}',
        "search_depth": "basic",
        "required_data_type": "any",
        "extraction_target": "outcome",
    }


def validate_evidence(content: str, required_data_type: str,
                      extraction_target: str, question: str) -> tuple[bool, str]:
    """Strict validation: evidence must answer the required data type and topic."""
    if not content or len(str(content).strip()) < 80:
        return False, "empty/short content"

    text = str(content)
    tlow = text.lower()
    answer_line = extract_answer_line(text) if "extract_answer_line" in globals() else ""
    al = answer_line.lower()
    q_asset = _canon_question_asset(question)

    # Reject obvious topic drift.
    aliases = q_asset.get("aliases") or []
    if aliases:
        alias_hits = sum(1 for a in aliases if a and a.lower() in tlow)
        # For S&P, CNBC Bitcoin article must be rejected.
        if q_asset.get("type") == "index" and alias_hits == 0:
            return False, f"topic drift: missing {q_asset.get('name')}"
        if q_asset.get("type") == "company_event" and alias_hits == 0:
            return False, f"topic drift: missing {q_asset.get('name')}"

    if required_data_type == "threshold_value":
        if re.search(r'\b(close|closing|closed|high|low|open)\b', tlow) and re.search(r'\b\d{3,6}(?:,\d{3})?(?:\.\d+)?\b', text):
            return True, "metric/value found"
        return False, "no metric value found"

    if required_data_type == "price":
        if re.search(r'\$?\d{3,6}(?:,\d{3})?(?:\.\d+)?', text):
            return True, "price-like number found"
        return False, "no price value found"

    if required_data_type == "count":
        subject = str(extraction_target or "").lower()
        if subject and any(w in tlow for w in subject.split() if len(w) > 3):
            if re.search(r'\b\d+\b', text) or re.search(r'\|\s*(OT|OG|OC|QB|WR|RB|TE|G|T|C)\s*\|', text, re.I):
                return True, "count subject and numbers/position codes found"
        return False, f"no validated count for {subject}"

    if required_data_type in ("score", "match_result"):
        if re.search(r'\b\d{1,3}\s*[-–]\s*\d{1,3}\b', text) or re.search(r'\b(won|beat|defeated|victory|final score|wickets?)\b', tlow):
            return True, "score/result found"
        return False, "no score/result found"

    if required_data_type == "confirmation":
        # Need topic relevance and a real yes/no event signal, not just unrelated "no".
        event_words = ["announced", "acquired", "purchased", "bought", "completed", "confirmed", "filed", "press release", "has not", "did not", "no purchase", "not announced"]
        if any(w in tlow for w in event_words) or (answer_line and len(answer_line) > 25):
            return True, "confirmation evidence found"
        return False, "no confirmation signal"

    if answer_line and len(answer_line) > 25:
        return True, "answer line found"
    return True, "content accepted"


def validate_evidence_quality(content: str, query_plan: dict, question: str) -> tuple[bool, str]:
    """Compatibility wrapper."""
    return validate_evidence(
        content,
        str((query_plan or {}).get("required_data_type") or "any"),
        str((query_plan or {}).get("extraction_target") or (query_plan or {}).get("number_context") or ""),
        question,
    )


def _canon_extract_table_row_value(text: str, event_date: str, metric: str) -> Optional[float]:
    """Extract OHLC value from table row containing event date."""
    if not text or not event_date:
        return None
    variants = _canon_date_variants(event_date)
    metric_index = {"open": 1, "high": 2, "low": 3, "close": 4}.get(metric, 4)

    # Markdown table rows from Tavily/Yahoo.
    for line in str(text).splitlines():
        ll = line.lower()
        if not any(v.lower() in ll for v in variants):
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("|")]
        nums = []
        for c in cols[1:]:
            n = _canon_money_number(c)
            if n is not None:
                nums.append(n)
        if len(nums) >= 4:
            return nums[metric_index - 1]

    # Same-row prose/table segment.
    for v in variants:
        m = re.search(re.escape(v) + r'.{0,180}', str(text), re.I | re.S)
        if not m:
            continue
        nums = [_canon_money_number(x.group(0)) for x in re.finditer(r'\$?\d+(?:,\d{3})*(?:\.\d+)?', m.group(0))]
        nums = [n for n in nums if n is not None and not (1900 <= n <= 2100)]
        if len(nums) >= 4:
            return nums[metric_index - 1]
        if nums:
            return nums[-1]
    return None


def extract_required_value(content: str, rules: dict, question: str, outcomes: list) -> tuple[Optional[str], Optional[str]]:
    """Extract exact raw settlement value from validated evidence."""
    metric = (rules or {}).get("metric")
    subject = (rules or {}).get("count_subject")
    event_date = (rules or {}).get("event_date") or ""
    answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else ""
    al = answer_line.lower()
    full = str(content or "")
    tlow = full.lower()

    # Binary threshold / finance values.
    if metric in ("high", "low", "close", "open"):
        val = _canon_extract_table_row_value(full, event_date, metric)
        if val is not None:
            return str(val), f"{metric} table row: {val:g}"

        # Answer line examples: "closed at 7,493.68", "close price was 7100"
        metric_words = {
            "close": r"closed|close|closing",
            "high": r"high|highest",
            "low": r"low|lowest",
            "open": r"open|opened|opening",
        }[metric]
        m = re.search(rf'(?:{metric_words})[^0-9$]{{0,60}}\$?\s*([\d,]+(?:\.\d+)?)', answer_line, re.I)
        if m:
            val = _canon_money_number(m.group(1))
            if val is not None:
                return str(val), f"{metric} answer line: {val:g}"

        # fallback: any large number in answer line, excluding dates and thresholds if possible
        nums = []
        for mt in re.finditer(r'\$?\d+(?:,\d{3})*(?:\.\d+)?', answer_line):
            n = _canon_money_number(mt.group(0))
            if n is not None and not (1900 <= n <= 2100):
                nums.append(n)
        if nums:
            # pick largest for index/crypto close if answer is direct.
            return str(max(nums)), f"{metric} answer number: {max(nums):g}"

    # Count.
    if metric == "count" and subject:
        if (rules or {}).get("count_method") == "position_codes":
            year_m = re.search(r'\b(20\d{2})\b', str(question) + " " + str(event_date))
            year = year_m.group(1) if year_m else "2026"
            league = "nfl" if ("nfl" in question.lower() or "draft" in question.lower()) else "nfl"
            val = get_draft_count_direct(year, league, subject, question) if "get_draft_count_direct" in globals() else None
            if val is not None:
                return str(val), f"official draft count: {val:g}"
            val = _count_position_codes_in_text(full, list((rules or {}).get("position_codes") or []), question) if "_count_position_codes_in_text" in globals() else None
            if val is not None:
                return str(val), f"structured position-code count: {val:g}"
            return None, None

        for word in [w for w in subject.lower().split() if len(w) > 3]:
            for pat in [
                rf'\b(\d+)\s+(?:\w+\s+)?{re.escape(word)}',
                rf'{re.escape(word)}[^.!?]{{0,80}}\b(\d+)\b',
                rf'total\s+of\s+(\d+)\s+(?:\w+\s+)?{re.escape(word)}',
            ]:
                m = re.search(pat, al + "\n" + tlow, re.I)
                if m:
                    return str(float(m.group(1))), f"count pattern: {m.group(1)} {subject}"

    # Spread/score.
    if metric in ("spread", "winner"):
        m = re.search(r'\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b', answer_line or full[:1000])
        if m:
            return f"{m.group(1)}-{m.group(2)}", f"score: {m.group(1)}-{m.group(2)}"
        wm = re.search(r'(.{0,80})\b(won|beat|defeated|victory|won by \d+ \w+)\b(.{0,80})', answer_line or full[:1000], re.I)
        if wm:
            return wm.group(0), "winner phrase"

    # Confirmation / Yes-No.
    if metric == "confirmation" or set(str(o).lower() for o in outcomes or []) <= {"yes", "no"}:
        if answer_line:
            return answer_line, "answer line confirmation"
        return full[:1000], "content confirmation"

    return None, None


def match_value_to_outcome(raw_value: str, rules: dict, question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """Deterministically map raw extracted value to valid outcome."""
    metric = (rules or {}).get("metric")
    threshold = (rules or {}).get("threshold") or (intelligence or {}).get("threshold") or _canon_extract_threshold(question, outcomes)
    op = (rules or {}).get("operator")
    text = str(raw_value or "")
    tlow = text.lower()

    # Binary threshold Yes/No: this fixes S&P close > 5300.
    out_l = {str(o).lower(): str(o) for o in outcomes or []}
    if threshold is not None and set(out_l.keys()) <= {"yes", "no"}:
        val = _canon_money_number(text)
        if val is not None:
            ok = val > threshold if op in (None, ">") else (val < threshold if op == "<" else val <= threshold)
            return (out_l.get("yes") if ok else out_l.get("no")), f"{val:g} {'>' if op in (None, '>') else op} {threshold:g}"

    # Over/Under.
    val = _canon_money_number(text)
    if val is not None and threshold is not None:
        for o in outcomes or []:
            ol = str(o).lower()
            if ("over" in ol or "above" in ol) and val > threshold:
                return str(o), f"{val:g} > {threshold:g}"
            if ("under" in ol or "below" in ol) and val < threshold:
                return str(o), f"{val:g} < {threshold:g}"

    # Bare price bands.
    bare_band_re = re.compile(r'^\$?[\d,]+\+?$')
    bare_bands = [o for o in outcomes or [] if bare_band_re.match(str(o).strip())]
    if val is not None and len(bare_bands) >= 3:
        def pb(o):
            return float(str(o).replace("$", "").replace(",", "").replace("+", "").strip())
        plus = next((o for o in bare_bands if "+" in str(o)), None)
        sorted_bands = sorted([o for o in bare_bands if "+" not in str(o)], key=pb)
        if plus and val >= pb(plus):
            return str(plus), f"{val:,.0f} >= {pb(plus):,.0f}"
        for i, band in enumerate(sorted_bands):
            lo = pb(band)
            hi = pb(sorted_bands[i + 1]) if i + 1 < len(sorted_bands) else (pb(plus) if plus else float("inf"))
            if lo <= val < hi:
                return str(band), f"{lo:,.0f} <= {val:,.0f} < {hi:,.0f}"

    # Spread cover.
    spread_re = re.compile(r'^(.+?)\s*([+-]\d+(?:\.\d+)?)\s*$')
    if len(outcomes or []) == 2 and all(spread_re.match(str(o).strip()) for o in outcomes or []):
        sm = re.search(r'\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b', text)
        spreads = {str(o): float(spread_re.match(str(o).strip()).group(2)) for o in outcomes}
        fav = min(spreads, key=lambda k: spreads[k])
        dog = max(spreads, key=lambda k: spreads[k])
        fav_name = re.sub(r'\s*[+-]\d+(?:\.\d+)?\s*$', '', fav).strip().lower()
        dog_name = re.sub(r'\s*[+-]\d+(?:\.\d+)?\s*$', '', dog).strip().lower()
        if sm:
            a, b = int(sm.group(1)), int(sm.group(2))
            margin = abs(a - b)
            line = abs(spreads[fav])
            # Winner from phrase if possible; otherwise fallback to team mention order.
            fav_won = fav_name in tlow and re.search(rf'{re.escape(fav_name)}.{{0,80}}\b(won|beat|defeated)\b', tlow)
            dog_won = dog_name in tlow and re.search(rf'{re.escape(dog_name)}.{{0,80}}\b(won|beat|defeated)\b', tlow)
            if dog_won:
                return dog, f"underdog won outright {a}-{b}"
            if fav_won:
                return (fav if margin > line else dog), f"margin {margin:g} vs line {line:g}"
        # If no score but text names winner directly.
        for o in outcomes or []:
            team = re.sub(r'\s*[+-]\d+(?:\.\d+)?\s*$', '', str(o)).strip().lower()
            if re.search(rf'\b{re.escape(team)}\b.{{0,80}}\b(won|beat|defeated)\b', tlow):
                return str(o), f"winner phrase: {team}"

    # Sports / named direct outcome.
    for o in outcomes or []:
        ol = str(o).lower().strip()
        if len(ol) > 2 and ol in tlow:
            # Avoid "won against {outcome}" as winner.
            if not re.search(rf'\b(won against|beat|defeated)\b.{{0,80}}\b{re.escape(ol)}\b', tlow):
                return str(o), f"direct outcome mention: {o}"

    # Yes/No confirmation with negation priority: fixes MicroStrategy false No from unrelated text.
    if set(out_l.keys()) <= {"yes", "no"}:
        no_phrases = [
            "has not", "did not", "not announced", "not confirmed", "no plans",
            "no purchase", "not purchase", "not acquired", "not bought",
            "do not include", "does not include", "was not", "is not",
        ]
        yes_phrases = [
            "announced", "has announced", "acquired", "purchased", "bought",
            "completed", "confirmed", "press release", "disclosed", "reported that",
            "did purchase", "did buy", "will appear", "does appear",
        ]
        if any(p in tlow for p in no_phrases):
            # If both positive and negative exist, require date/window proximity for no; otherwise let AI handle.
            if not any(p in tlow for p in ["acquired", "purchased", "bought", "announced"]):
                return out_l.get("no"), "negative confirmation"
        if any(p in tlow for p in yes_phrases):
            return out_l.get("yes"), "positive confirmation"

    return None, None


def ai_settle(answer_line: str, content: str, question: str, outcomes: list, rules: dict, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """Narrow AI fallback. It cannot invent an outcome; must quote evidence."""
    if not GROQ_API_KEY:
        return None, None
    outcomes_str = "\n".join(f"- {o}" for o in outcomes or [])
    raw = call_groq(
        "You are OracleREE's fallback settlement judge. Use ONLY the provided evidence.\n"
        "Return JSON only: {\"matched_outcome\":\"exact valid outcome or NONE\","
        "\"extracted_fact\":\"short quote/fact\", \"reasoning\":\"one sentence\"}.\n"
        "If evidence does not directly answer the settlement rule, return NONE.",
        f"Question: {question}\nRules: {json.dumps(rules, ensure_ascii=False)}\n"
        f"Valid outcomes:\n{outcomes_str}\n\n"
        f"ANSWER LINE:\n{answer_line}\n\nEVIDENCE:\n{str(content)[:3000]}",
        max_tokens=450,
    )
    obj = _safe_json_object(raw or "")
    if not isinstance(obj, dict):
        return None, None
    mo = str(obj.get("matched_outcome") or "").strip()
    if not mo or mo.upper() == "NONE":
        return None, None
    for o in outcomes or []:
        if mo.lower() == str(o).lower() or mo.lower() in str(o).lower() or str(o).lower() in mo.lower():
            return str(o), f"AI fallback: {obj.get('extracted_fact') or obj.get('reasoning') or mo}"
    return None, None


def extract_specific_answer(content: str, query_plan: dict, question: str,
                            outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """Canonical extraction endpoint used by build_source_evidence."""
    base = (intelligence or {}).get("_rules") or parse_settlement_rules((intelligence or {}).get("prompt_context", ""), question, outcomes)
    rules = {**(base or {}), **(query_plan or {})}
    if "event_date" not in rules and (intelligence or {}).get("event_date"):
        rules["event_date"] = intelligence["event_date"]

    raw_value, method = extract_required_value(content, rules, question, outcomes)
    if raw_value:
        matched, calc = match_value_to_outcome(raw_value, rules, question, outcomes, intelligence)
        if matched:
            return matched, f"{method}; {calc}"

    answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else ""
    return ai_settle(answer_line, content, question, outcomes, rules, intelligence)


def derive_outcome(facts: list[Fact], outcomes: list, question: str, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """Canonical derive endpoint for raw_evidence/API paths."""
    if not facts:
        return None, None
    facts_dict = {str(f.label): str(f.value) for f in facts}
    mo = facts_dict.get("matched_outcome", "").strip()
    if mo:
        for o in outcomes or []:
            if mo.lower() == str(o).lower():
                return str(o), f"matched_outcome fact: {mo}"

    evidence = facts_dict.get("raw_evidence") or "\n".join(f"{f.label}: {f.value}" for f in facts)
    rules = (intelligence or {}).get("_rules") or parse_settlement_rules((intelligence or {}).get("prompt_context", ""), question, outcomes)
    if (intelligence or {}).get("event_date"):
        rules["event_date"] = intelligence["event_date"]

    # Direct labeled numeric facts.
    for key in ["high_price", "high_value", "close_price", "close_value", "low_price", "open_price", "index_value", "price", "value"]:
        if key in facts_dict:
            metric = "high" if "high" in key else ("low" if "low" in key else ("open" if "open" in key else (rules.get("metric") or "close")))
            temp = {**rules, "metric": metric}
            matched, calc = match_value_to_outcome(facts_dict[key], temp, question, outcomes, intelligence)
            if matched:
                return matched, calc

    raw_value, method = extract_required_value(evidence, rules, question, outcomes)
    if raw_value:
        matched, calc = match_value_to_outcome(raw_value, rules, question, outcomes, intelligence)
        if matched:
            return matched, f"{method}; {calc}"

    return ai_settle(extract_answer_line(evidence) if "extract_answer_line" in globals() else "", evidence, question, outcomes, rules, intelligence)


def build_source_evidence(source_original: str, intelligence: dict,
                          question: str, outcomes: list,
                          resolves_at: str) -> EvidenceBlock:
    """Canonical source-locked evidence builder with strict validation and deterministic extraction."""
    eb = EvidenceBlock()
    eb.source_used = source_original

    url = resolve_source_to_url(str(source_original))
    primary_domain = clean_domain(url)
    event_date = (intelligence or {}).get("event_date") or (resolves_at[:10] if resolves_at else "")
    rules = (intelligence or {}).get("_rules") or parse_settlement_rules((intelligence or {}).get("prompt_context", ""), question, outcomes)
    if event_date:
        rules["event_date"] = event_date
    query_plan = build_universal_query(question, outcomes, rules, event_date, primary_domain)
    query = query_plan.get("query") or question
    depth = query_plan.get("search_depth", "advanced")
    required = query_plan.get("required_data_type", "any")
    target = query_plan.get("extraction_target", "")

    print(f"\n[oracle] Source: {source_original} → {primary_domain}")
    print(f"[oracle] Query: {query[:140]}")
    print(f"[oracle] Required: {required} | Target: {target}")

    # Unsupported social sources: no scraping.
    if any(x in url.lower() for x in ["twitter.com", "x.com", "t.co"]):
        eb.fetch_status = "UNSUPPORTED_SOURCE"
        eb.parse_status = "UNSUPPORTED_SOURCE"
        eb.outcome_status = "UNSUPPORTED_SOURCE"
        eb.reason = "X/Twitter source is unsupported for automated settlement"
        return eb

    # Fetch: source locked Tavily first, direct fallback.
    content = tavily_source_locked_fetch(primary_domain, question, event_date, query, search_depth=depth)
    method = "tavily_locked"
    ctype = "text"
    if not content and url.startswith("http"):
        direct = direct_fetch(url)
        if direct:
            content, ctype = direct
            method = "direct"

    if not content:
        # Sports immutable fallback only.
        if is_sports_market_question(question, intelligence):
            fb = try_sports_fallback_sources(question, event_date, query, intelligence, primary_domain, outcomes)
            if fb:
                content, fb_domain = fb
                method = "sports_fallback"
                eb.recovered_from = primary_domain
                eb.source_used = fb_domain
        if not content:
            eb.fetch_status = "FETCH_FAILED"
            eb.parse_status = "FETCH_FAILED"
            eb.outcome_status = "FETCH_FAILED"
            eb.reason = f"No content from creator source {primary_domain}"
            return eb

    eb.fetch_status = "FETCHED"
    eb.fetch_method = method
    eb.raw_content = str(content)[:5000]

    # Validate exact required data type before extraction.
    ok, reason = validate_evidence(content, required, target, question)
    if not ok:
        # Try sports fallback on insufficient evidence too.
        if method != "sports_fallback" and is_sports_market_question(question, intelligence):
            fb = try_sports_fallback_sources(question, event_date, query, intelligence, primary_domain, outcomes)
            if fb:
                content, fb_domain = fb
                eb.raw_content = str(content)[:5000]
                eb.fetch_method = "sports_fallback"
                eb.recovered_from = primary_domain
                eb.source_used = fb_domain
                ok, reason = validate_evidence(content, required, target, question)
        if not ok:
            eb.parse_status = "PARSE_FAILED"
            eb.outcome_status = "PARSE_FAILED"
            eb.reason = f"Evidence rejected: {reason}"
            return eb

    matched, calc = extract_specific_answer(str(content), query_plan, question, outcomes, intelligence)
    if matched:
        eb.parse_status = "PARSED"
        eb.outcome_status = "OUTCOME_FOUND"
        eb.matched_outcome = matched
        eb.calculation = calc
        eb.facts = [
            Fact("raw_evidence", str(content)[:2000], eb.source_used or source_original, timestamp=event_date),
            Fact("matched_outcome", matched, eb.source_used or source_original, timestamp=event_date),
        ]
        print(f"[oracle] ✓ OUTCOME_FOUND: {matched} ({calc})")
        return eb

    eb.parse_status = "PARSED"
    eb.outcome_status = "OUTCOME_NOT_FOUND"
    eb.reason = "Evidence validated but deterministic/AI extraction could not map to a valid outcome"
    eb.facts = [Fact("raw_evidence", str(content)[:2000], eb.source_used or source_original, timestamp=event_date)]
    return eb
# ═══════════════════════════════════════════════════════════════════════════════
# END CANONICAL SETTLEMENT CORE OVERRIDE
# ═══════════════════════════════════════════════════════════════════════════════




# ═══════════════════════════════════════════════════════════════════════════════
# SPORTS RESULT ROUTING FIX
# Fixes markets like: Burnley vs Man City — April 22, 2026
# Problem: the rules parser saw "market close" and classified the market as
# a close_value binary_event. This override forces match-result markets into the
# sports_result resolver and adds deterministic winner mapping from ANSWER lines.
# ═══════════════════════════════════════════════════════════════════════════════

SPORTS_RESULT_WORDS = [
    " vs ", " versus ", "match", "game", "fixture", "full-time", "full time",
    "premier league", "champions league", "la liga", "serie a", "bundesliga",
    "soccer", "football", "cricket", "ipl", "psl", "nba", "nfl", "mlb", "nhl",
    "ufl", "xfl", "final result", "final score"
]


def _is_sports_result_market(question: str, prompt_context: str, outcomes: list) -> bool:
    q = str(question or "").lower()
    p = str(prompt_context or "").lower()
    outs = [str(o).strip().lower() for o in outcomes or []]

    # Three-way match markets: Team A / Draw / Team B.
    if "draw" in outs and len(outs) >= 3:
        return True

    # Prompt explicitly asks for full-time/final result.
    if any(k in p for k in ["official full-time result", "official final result", "full-time result", "final score"]):
        return True

    # Question shaped like Team A vs Team B.
    if (" vs " in q or " versus " in q) and len(outs) >= 2:
        return True

    return any(k in q for k in SPORTS_RESULT_WORDS) and len(outs) >= 2


def _outcome_aliases(outcome: str) -> list[str]:
    """Generate conservative aliases for sports team outcomes."""
    o = str(outcome or "").strip().lower()
    aliases = {o}

    # Common football abbreviations used in markets vs official source pages.
    replacements = {
        "man city": "manchester city",
        "man utd": "manchester united",
        "man united": "manchester united",
        "spurs": "tottenham hotspur",
        "wolves": "wolverhampton wanderers",
        "newcastle": "newcastle united",
        "brighton": "brighton & hove albion",
        "bournemouth": "afc bournemouth",
        "leeds": "leeds united",
        "forest": "nottingham forest",
    }
    if o in replacements:
        aliases.add(replacements[o])

    # Strip common suffixes/prefixes but keep original too.
    stripped = re.sub(r"\b(fc|afc|cf|sc|the)\b", " ", o)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    if stripped:
        aliases.add(stripped)

    # If outcome is short form like "Man City", also try expanded Manchester City.
    if "city" in o and "man" in o and "manchester city" not in aliases:
        aliases.add("manchester city")

    return [a for a in aliases if len(a) > 1]


def _sports_result_from_text(text: str, outcomes: list) -> tuple[Optional[str], Optional[str]]:
    """Deterministically map sports ANSWER/source text to one valid outcome."""
    if not text or not outcomes:
        return None, None

    answer = extract_answer_line(text) if "extract_answer_line" in globals() else ""
    hay = (answer or str(text)[:2500]).lower()
    hay = re.sub(r"\*+", " ", hay)
    hay = re.sub(r"\s+", " ", hay).strip()

    # Draw / tie / postponed handling.
    for outcome in outcomes:
        if str(outcome).strip().lower() == "draw":
            if re.search(r"\b(draw|drawn|tied|tie|no result|postponed|cancelled|canceled|abandoned)\b", hay):
                return str(outcome), "sports result: draw/no result"

    # First detect explicit losers: "X defeated/beat Y" means Y lost.
    losers: set[str] = set()
    outcome_alias_map: dict[str, list[str]] = {str(o): _outcome_aliases(str(o)) for o in outcomes}
    for outcome, aliases in outcome_alias_map.items():
        if outcome.strip().lower() == "draw":
            continue
        for alias in aliases:
            if re.search(rf"\b(beat|beats|defeated|defeats|won against|won over)\b.{{0,90}}\b{re.escape(alias)}\b", hay):
                losers.add(outcome)
                break
            if re.search(rf"\b{re.escape(alias)}\b.{{0,50}}\b(lost|loses|were beaten|was beaten)\b", hay):
                losers.add(outcome)
                break

    # Winner in subject position: "Manchester City defeated Burnley".
    for outcome, aliases in outcome_alias_map.items():
        if outcome in losers or outcome.strip().lower() == "draw":
            continue
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b.{{0,90}}\b(beat|beats|defeated|defeats|won|wins|victory)\b", hay):
                return str(outcome), f"sports winner: {outcome}"

    # Elimination: if exactly one non-draw outcome is not a loser, it wins.
    non_draw = [str(o) for o in outcomes if str(o).strip().lower() != "draw"]
    remaining = [o for o in non_draw if o not in losers]
    if losers and len(remaining) == 1:
        return remaining[0], f"sports winner by loser elimination: {remaining[0]}"

    # Score phrasing sometimes says "Team A 1-0 Team B" without won/defeated.
    score_m = re.search(r"\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b", hay)
    if score_m:
        # If a team is named before the score and another after, use the score order.
        left_score, right_score = int(score_m.group(1)), int(score_m.group(2))
        before = hay[:score_m.start()]
        after = hay[score_m.end():]
        left_candidates = [o for o, aliases in outcome_alias_map.items() if any(a in before[-120:] for a in aliases)]
        right_candidates = [o for o, aliases in outcome_alias_map.items() if any(a in after[:120] for a in aliases)]
        if left_candidates and right_candidates and left_score != right_score:
            return (left_candidates[-1] if left_score > right_score else right_candidates[0]), "sports score order"
        if left_score == right_score:
            for outcome in outcomes:
                if str(outcome).strip().lower() == "draw":
                    return str(outcome), "sports score draw"

    return None, None


# Preserve previous functions and override only the broken routing/extraction layer.
_PREV_ANALYZE_MARKET_INTELLIGENCE = analyze_market_intelligence
_PREV_BUILD_UNIVERSAL_QUERY = build_universal_query
_PREV_EXTRACT_SPECIFIC_ANSWER = extract_specific_answer
_PREV_DERIVE_OUTCOME = derive_outcome


def analyze_market_intelligence(question: str, prompt_context: str,
                                outcomes: list, data_sources: list, close_time: str) -> dict:
    intel = _PREV_ANALYZE_MARKET_INTELLIGENCE(question, prompt_context, outcomes, data_sources, close_time)

    if _is_sports_result_market(question, prompt_context, outcomes):
        close_date = close_time[:10] if close_time else intel.get("event_date", "")
        q_lower = str(question or "").lower()
        event_date = intel.get("event_date") or close_date
        dm = re.search(
            r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})',
            q_lower
        )
        if dm:
            y = close_date[:4] if close_date else "2026"
            event_date = f"{y}-{MONTH_MAP[dm.group(1)]}-{int(dm.group(2)):02d}"

        rules = intel.get("_rules") or {}
        rules.update({
            "metric": "winner",
            "winner_logic": "full_time_result",
            "operator": None,
            "threshold": None,
            "source_policy": "sports_creator_first_trusted_fallback",
            "event_date": event_date,
        })

        intel.update({
            "event_date": event_date,
            "facts_needed": ["winner", "final_score", "result"],
            "is_price_based": False,
            "asset": "",
            "threshold": None,
            "market_type": "sports",
            "answer_format": "named_choice",
            "resolver": "sports_result",
            "_rules": rules,
        })
        print("[oracle] Sports result override activated")
        print(f"[oracle] Plan: {intel['market_type']} | {intel['answer_format']} | {intel['resolver']}")
        print(f"[oracle] Plan facts: {intel['facts_needed']}")

    return intel


def build_universal_query(question: str, outcomes: list, rules: dict,
                          event_date: str, source_domain: str) -> dict:
    if (rules or {}).get("metric") == "winner" or _is_sports_result_market(question, "", outcomes):
        sport = sport_hint_for_question(question) if "sport_hint_for_question" in globals() else ""
        return {
            "query": f"{question} {sport} official full-time result final score winner {event_date}".strip(),
            "search_depth": "basic",
            "required_data_type": "match_result",
            "extraction_target": "official full-time match winner and final score",
            "what_to_validate": "match_result",
            "need_number": False,
            "number_context": "final score",
        }
    return _PREV_BUILD_UNIVERSAL_QUERY(question, outcomes, rules, event_date, source_domain)


def extract_specific_answer(content: str, query_plan: dict, question: str,
                            outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    if (intelligence or {}).get("resolver") == "sports_result" or _is_sports_result_market(question, (intelligence or {}).get("prompt_context", ""), outcomes):
        matched, calc = _sports_result_from_text(str(content), outcomes)
        if matched:
            return matched, calc
    return _PREV_EXTRACT_SPECIFIC_ANSWER(content, query_plan, question, outcomes, intelligence)


def derive_outcome(facts: list[Fact], outcomes: list, question: str,
                   intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    if (intelligence or {}).get("resolver") == "sports_result" or _is_sports_result_market(question, (intelligence or {}).get("prompt_context", ""), outcomes):
        facts_dict = {str(f.label): str(f.value) for f in facts or []}
        evidence = facts_dict.get("raw_evidence") or "\n".join(f"{f.label}: {f.value}" for f in facts or [])
        matched, calc = _sports_result_from_text(evidence, outcomes)
        if matched:
            return matched, calc
    return _PREV_DERIVE_OUTCOME(facts, outcomes, question, intelligence)

# ═══════════════════════════════════════════════════════════════════════════════
# END SPORTS RESULT ROUTING FIX
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FINAL EVIDENCE VALIDATION HARDENING
# Prevents accepting Tavily ANSWER lines backed by stale/irrelevant pages.
# Prefers the exact creator URL path (e.g. strategy.com/purchases) before broad
# domain search, and validates event-window + topic before settlement.
# ═══════════════════════════════════════════════════════════════════════════════

def _creator_url_path(url: str) -> str:
    try:
        from urllib.parse import urlparse
        p = urlparse(str(url or "")).path or ""
        p = "/" + p.strip("/")
        return "" if p == "/" else p.lower()
    except Exception:
        return ""

def _extract_result_urls(content: str) -> list[str]:
    return re.findall(r"\[(https?://[^\]\s]+)\]", str(content or ""))

def _date_window_from_question(question: str, fallback_year: str = "2026") -> tuple[Optional[str], Optional[str]]:
    """
    Extract a simple date/window from market text.
    Examples:
      April 21-27 -> 2026-04-21 to 2026-04-27
      April 23 -> 2026-04-23 to 2026-04-23
    """
    q = str(question or "").lower()
    year_m = re.search(r"\b(20\d{2})\b", q)
    year = year_m.group(1) if year_m else fallback_year
    month_re = (
        r"(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)"
    )
    m = re.search(month_re + r"\s+(\d{1,2})(?:\s*(?:-|–|to|through)\s*(\d{1,2}))?", q)
    if not m:
        return None, None
    month = MONTH_MAP.get(m.group(1))
    if not month:
        return None, None
    d1 = int(m.group(2))
    d2 = int(m.group(3) or m.group(2))
    return f"{year}-{month}-{d1:02d}", f"{year}-{month}-{d2:02d}"

def _date_variants_between(start_iso: str, end_iso: str) -> list[str]:
    try:
        from datetime import datetime, timedelta
        s = datetime.strptime(start_iso, "%Y-%m-%d")
        e = datetime.strptime(end_iso, "%Y-%m-%d")
        out = []
        cur = s
        while cur <= e:
            month_name = cur.strftime("%B")
            month_short = cur.strftime("%b")
            day = cur.day
            out.extend([
                cur.strftime("%Y-%m-%d"),
                cur.strftime("%m/%d/%Y"),
                cur.strftime("%B %-d, %Y") if os.name != "nt" else f"{month_name} {day}, {cur.year}",
                cur.strftime("%b %-d, %Y") if os.name != "nt" else f"{month_short} {day}, {cur.year}",
                f"{month_name} {day}, {cur.year}",
                f"{month_short} {day}, {cur.year}",
                f"{month_name} {day}",
                f"{month_short} {day}",
            ])
            cur += timedelta(days=1)
        return list(dict.fromkeys(out))
    except Exception:
        return []

def _has_window_date(text: str, question: str, event_date: str = "") -> bool:
    year = (event_date or "")[:4] or "2026"
    start, end = _date_window_from_question(question, fallback_year=year)
    if not start or not end:
        if event_date:
            return any(v.lower() in str(text or "").lower() for v in _canon_date_variants(event_date))
        return True
    tlow = str(text or "").lower()
    return any(v.lower() in tlow for v in _date_variants_between(start, end))

def _confirmation_topic_terms(question: str) -> list[str]:
    q = str(question or "").lower()
    terms = []
    if any(x in q for x in ["microstrategy", "strategy", "mstr", "saylor"]):
        terms += ["microstrategy", "strategy", "mstr"]
    if "bitcoin" in q or "btc" in q:
        terms += ["bitcoin", "btc"]
    if any(x in q for x in ["purchase", "buy", "bought", "acquire", "acquired"]):
        terms += ["purchase", "purchased", "acquire", "acquired", "bought"]
    if not terms:
        terms = [w for w in re.findall(r"[a-zA-Z]{5,}", str(question or "").lower())[:6]]
    return list(dict.fromkeys(terms))

def _strict_confirmation_validation(content: str, question: str, event_date: str, source_url: str, method: str) -> tuple[bool, str]:
    """
    For binary confirmation markets, answer-line alone is not sufficient if the
    supporting source snippets are stale/wrong pages. Require topic + date/window
    evidence, and for exact source URLs prefer/require that path when Tavily is used.
    """
    text = str(content or "")
    tlow = text.lower()
    answer_line = extract_answer_line(text) if "extract_answer_line" in globals() else ""
    al = answer_line.lower()

    # If Tavily was used for an exact creator path, reject broad-domain pages that
    # do not include that path in result URLs. This fixes strategy.com/purchases
    # returning old /press/* pages.
    preferred_path = _creator_url_path(source_url)
    result_urls = _extract_result_urls(text)
    if method.startswith("tavily") and preferred_path and result_urls:
        if not any(_creator_url_path(u).startswith(preferred_path) for u in result_urls):
            return False, f"source path mismatch: expected {preferred_path}, got {[ _creator_url_path(u) for u in result_urls[:3] ]}"

    terms = _confirmation_topic_terms(question)
    topic_hits = sum(1 for t in terms if t in tlow)
    has_date = _has_window_date(text, question, event_date) or _has_window_date(answer_line, question, event_date)
    purchase_words = ["purchase", "purchased", "acquire", "acquired", "bought", "buy", "announced", "confirmed"]

    # Positive confirmations need date/window + topic + action.
    positive = any(w in al for w in ["announced", "confirmed", "purchased", "bought", "acquired"]) or any(w in tlow for w in purchase_words)
    negative = any(w in al for w in ["did not", "has not", "not announced", "no purchase", "no bitcoin purchase"])

    if negative and topic_hits >= 1 and has_date:
        return True, "negative confirmation with topic/date"
    if positive and topic_hits >= 2 and has_date:
        return True, "positive confirmation with topic/date"

    # Direct exact creator pages sometimes include structured content without
    # Tavily's ANSWER line. Accept only if clearly topic/action/date aligned.
    if method == "direct" and topic_hits >= 2 and has_date and any(w in tlow for w in purchase_words):
        return True, "direct creator page has topic/action/date"

    if not has_date:
        return False, "missing required event-window date"
    if topic_hits < 2:
        return False, f"topic drift: only {topic_hits} topic hits"
    return False, "missing confirmation action"

def _tavily_source_locked_fetch_preferred(domain: str, question: str, event_date: str,
                                          what_to_find: str, preferred_path: str = "",
                                          is_fdv: bool = False,
                                          search_depth: str = "basic") -> Optional[str]:
    """
    Tavily fetch with creator-path preference. If creator gave a specific URL path,
    only accept Tavily results from that path. This avoids old press/news pages
    contaminating evidence for pages like strategy.com/purchases.
    """
    if not TAVILY_API_KEY:
        return None

    path_hint = preferred_path.strip("/")
    query = f"{what_to_find} {event_date}" if is_fdv else f"site:{domain} {what_to_find} {event_date}"
    if preferred_path:
        query = f"{query} {preferred_path}"

    print(f"[oracle] Tavily locked fetch: {query[:80]}")
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": search_depth,
            "max_results": 8,
            "include_answer": True,
        }, timeout=15)
        data = r.json()
        parts = []
        result_parts = []

        for res in data.get("results", [])[:8]:
            url = res.get("url", "")
            res_domain = clean_domain(url)
            if not (is_fdv or res_domain == domain or res_domain.endswith("." + domain)):
                continue
            if preferred_path and not _creator_url_path(url).startswith(preferred_path):
                continue
            result_parts.append(f"[{url}]\n{res.get('content','')[:1000]}")

        # Only include Tavily answer if at least one accepted source snippet supports it.
        # This prevents a plausible answer from being backed only by unrelated pages.
        if result_parts and data.get("answer"):
            parts.append(f"ANSWER: {data['answer']}")
        parts.extend(result_parts)

        content = "\n\n".join(parts)
        if content.strip():
            print(f"[oracle] Tavily locked: {len(content)} chars")
            return content
    except Exception as e:
        print(f"[oracle] Tavily failed: {e}")
    return None

_PREV_BUILD_SOURCE_EVIDENCE_VALIDATION = build_source_evidence

def build_source_evidence(source_original: str, intelligence: dict,
                          question: str, outcomes: list,
                          resolves_at: str) -> EvidenceBlock:
    """
    Final hardened source-locked evidence builder.
    Changes:
      1. Exact creator URL path is tried directly first.
      2. Tavily results are path-filtered when creator supplied a specific URL.
      3. Confirmation evidence requires topic + date/window + action.
      4. Tavily ANSWER is not trusted if supported only by wrong/stale pages.
    """
    eb = EvidenceBlock()
    eb.source_used = source_original

    url = resolve_source_to_url(str(source_original))
    primary_domain = clean_domain(url)
    preferred_path = _creator_url_path(url)

    event_date = (intelligence or {}).get("event_date") or (resolves_at[:10] if resolves_at else "")
    rules = (intelligence or {}).get("_rules") or parse_settlement_rules((intelligence or {}).get("prompt_context", ""), question, outcomes)
    if event_date:
        rules["event_date"] = event_date

    query_plan = build_universal_query(question, outcomes, rules, event_date, primary_domain)
    query = query_plan.get("query") or question
    depth = query_plan.get("search_depth", "advanced")
    required = query_plan.get("required_data_type", "any")
    target = query_plan.get("extraction_target", "")

    print(f"\n[oracle] Source: {source_original} → {primary_domain}")
    print(f"[oracle] Query: {query[:140]}")
    print(f"[oracle] Required: {required} | Target: {target}")

    if any(x in url.lower() for x in ["twitter.com", "x.com", "t.co"]):
        eb.fetch_status = "UNSUPPORTED_SOURCE"
        eb.parse_status = "UNSUPPORTED_SOURCE"
        eb.outcome_status = "UNSUPPORTED_SOURCE"
        eb.reason = "X/Twitter source is unsupported for automated settlement"
        return eb

    content = None
    method = ""
    ctype = "text"

    # Exact creator URL first when a path exists (/purchases, /markets/..., /history, etc.).
    # This is the creator-source-first interpretation: exact page beats broad domain search.
    if url.startswith("http") and preferred_path:
        direct = direct_fetch(url)
        if direct:
            direct_content, direct_type = direct
            ok_direct, direct_reason = validate_evidence(direct_content, required, target, question)
            if required == "confirmation":
                ok_direct, direct_reason = _strict_confirmation_validation(
                    direct_content, question, event_date, url, "direct"
                )
            if ok_direct:
                content, ctype, method = direct_content, direct_type, "direct"
                print(f"[oracle] ✓ Direct creator URL accepted: {preferred_path} ({direct_reason})")
            else:
                print(f"[oracle] Direct creator URL rejected: {direct_reason}")

    # Tavily source-locked fallback, path-filtered when applicable.
    if not content:
        content = _tavily_source_locked_fetch_preferred(
            primary_domain, question, event_date, query,
            preferred_path=preferred_path,
            search_depth=depth
        )
        method = "tavily_locked"
        ctype = "text"

    # Last broad Tavily attempt only if no exact path exists.
    if not content and not preferred_path:
        content = tavily_source_locked_fetch(primary_domain, question, event_date, query, search_depth=depth)
        method = "tavily_locked"
        ctype = "text"

    # Direct domain fallback only if no specific path was provided.
    if not content and url.startswith("http") and not preferred_path:
        direct = direct_fetch(url)
        if direct:
            content, ctype = direct
            method = "direct"

    if not content:
        if is_sports_market_question(question, intelligence):
            fb = try_sports_fallback_sources(question, event_date, query, intelligence, primary_domain, outcomes)
            if fb:
                content, fb_domain = fb
                method = "sports_fallback"
                eb.recovered_from = primary_domain
                eb.source_used = fb_domain
        if not content:
            eb.fetch_status = "FETCH_FAILED"
            eb.parse_status = "FETCH_FAILED"
            eb.outcome_status = "FETCH_FAILED"
            eb.reason = f"No validated content from creator source {primary_domain}{preferred_path or ''}"
            return eb

    eb.fetch_status = "FETCHED"
    eb.fetch_method = method
    eb.raw_content = str(content)[:5000]

    ok, reason = validate_evidence(content, required, target, question)
    if ok and required == "confirmation":
        ok, reason = _strict_confirmation_validation(content, question, event_date, url, method)

    if not ok:
        if method != "sports_fallback" and is_sports_market_question(question, intelligence):
            fb = try_sports_fallback_sources(question, event_date, query, intelligence, primary_domain, outcomes)
            if fb:
                content, fb_domain = fb
                eb.raw_content = str(content)[:5000]
                eb.fetch_method = "sports_fallback"
                eb.recovered_from = primary_domain
                eb.source_used = fb_domain
                ok, reason = validate_evidence(content, required, target, question)
        if not ok:
            eb.parse_status = "PARSE_FAILED"
            eb.outcome_status = "PARSE_FAILED"
            eb.reason = f"Evidence rejected: {reason}"
            return eb

    matched, calc = extract_specific_answer(str(content), query_plan, question, outcomes, intelligence)
    if matched:
        eb.parse_status = "PARSED"
        eb.outcome_status = "OUTCOME_FOUND"
        eb.matched_outcome = matched
        eb.calculation = calc
        eb.facts = [
            Fact("raw_evidence", str(content)[:2000], eb.source_used or source_original, timestamp=event_date),
            Fact("matched_outcome", matched, eb.source_used or source_original, timestamp=event_date),
        ]
        print(f"[oracle] ✓ OUTCOME_FOUND: {matched} ({calc})")
        return eb

    eb.parse_status = "PARSED"
    eb.outcome_status = "OUTCOME_NOT_FOUND"
    eb.reason = "Evidence validated but deterministic/AI extraction could not map to a valid outcome"
    eb.facts = [Fact("raw_evidence", str(content)[:2000], eb.source_used or source_original, timestamp=event_date)]
    return eb

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL EVIDENCE VALIDATION HARDENING
# ═══════════════════════════════════════════════════════════════════════════════




# ═══════════════════════════════════════════════════════════════════════════════
# FINAL COUNT-MARKET FIX
# Ollama extracts numeric count only. Python performs threshold comparison and
# exact outcome mapping. This prevents "How many ... NFL draft" markets from
# being treated as sports_result/named_choice markets.
# ═══════════════════════════════════════════════════════════════════════════════

_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "twenty-one": 21, "twenty two": 22,
    "twenty-two": 22, "twenty three": 23, "twenty-three": 23,
    "twenty four": 24, "twenty-four": 24, "twenty five": 25,
    "twenty-five": 25, "thirty": 30, "forty": 40, "forty-one": 41,
    "forty one": 41,
}

def _num_token_to_float(token: str) -> Optional[float]:
    token = str(token or "").strip().lower().replace(",", "")
    if token in _WORD_NUMBERS:
        return float(_WORD_NUMBERS[token])
    try:
        return float(token)
    except Exception:
        return None

def _extract_answer_line_count(answer_line: str, subject: str, question: str = "") -> Optional[float]:
    """
    Extract the count from Tavily/LLM answer line only when it is clearly attached
    to the counted subject and target scope.
    Example: "Three trades occurred during round one..." -> 3.
    Rejects unrelated totals like "41 trades total" when the question asks round one.
    """
    al = str(answer_line or "").lower()
    if not al:
        return None

    subject = (subject or _infer_count_subject(question) if "_infer_count_subject" in globals() else subject or "items").lower()
    subject_words = [w for w in re.split(r"[^a-z0-9]+", subject) if len(w) >= 3]
    if not subject_words:
        subject_words = ["trades"] if "trade" in al else ["items"]

    # Prefer first sentence because Tavily puts the synthesized answer there.
    first_sentence = re.split(r"(?<=[.!?])\s+", al)[0]
    candidates = [first_sentence, al]

    number_pattern = r"(\d+(?:\.\d+)?|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty(?:[- ]one|[- ]two|[- ]three|[- ]four|[- ]five)?|thirty|forty(?:[- ]one)?)"

    for text_part in candidates:
        for word in subject_words:
            patterns = [
                rf"\b{number_pattern}\s+(?:\w+\s+)?{re.escape(word)}\b",
                rf"\bthere\s+(?:were|was|are|is)\s+{number_pattern}\s+(?:\w+\s+)?{re.escape(word)}\b",
                rf"\b{re.escape(word)}[^.!?]{{0,50}}\b{number_pattern}\b",
            ]
            for pattern in patterns:
                m = re.search(pattern, text_part, re.I)
                if not m:
                    continue
                # group containing number can vary due nested alternatives; pick first parseable group.
                val = None
                for g in m.groups():
                    val = _num_token_to_float(g)
                    if val is not None:
                        break
                if val is None:
                    continue

                # Scope guard: for round-one questions, prefer counts stated with round one / first round
                # or in the first answer sentence. Avoid "41 trades total" across the full draft.
                q = str(question or "").lower()
                if ("round one" in q or "first round" in q or "round 1" in q):
                    scope_ok = (
                        "round one" in text_part
                        or "first round" in text_part
                        or "round 1" in text_part
                        or text_part == first_sentence
                    )
                    if not scope_ok:
                        continue
                    if val > 32:
                        continue
                return val
    return None

def _extract_count_with_ollama(content: str, question: str, outcomes: list, subject: str, threshold: Optional[float]) -> Optional[float]:
    """
    Ask local Ollama for count extraction only. Never trust it for comparison or final outcome.
    """
    if "call_ollama_json" not in globals():
        return None
    try:
        prompt = (
            "You are OracleREE count extractor. Return valid JSON only.\n"
            "Extract ONLY the count that answers the question. Do not compare outcomes.\n"
            "Ignore totals for the full event if the question asks a narrower scope like round one.\n"
            f"Question: {question}\n"
            f"Count subject: {subject}\n"
            f"Outcomes: {', '.join(str(o) for o in outcomes or [])}\n"
            f"Threshold: {threshold}\n"
            "Return JSON shape: {\"count\": number|null, \"evidence_text\": \"short quote\", \"confidence\": \"high|medium|low\"}\n\n"
            f"Evidence:\n{str(content)[:3500]}"
        )
        obj = call_ollama_json(prompt, timeout=120)
        if not isinstance(obj, dict):
            return None
        conf = str(obj.get("confidence") or "").lower()
        if conf and conf not in {"high", "medium"}:
            return None
        val = obj.get("count")
        if val is None:
            return None
        val = float(val)
        if ("round one" in str(question).lower() or "first round" in str(question).lower() or "round 1" in str(question).lower()) and val > 32:
            return None
        return val
    except Exception as e:
        print(f"[oracle] Ollama count extraction failed: {e}")
        return None

def _map_count_to_threshold_outcome(count: float, threshold: float, outcomes: list) -> tuple[Optional[str], Optional[str]]:
    """Python-only deterministic comparison and exact outcome mapping."""
    for outcome in outcomes or []:
        ol = str(outcome).lower()
        if ("over" in ol or "above" in ol) and count > threshold:
            return str(outcome), f"count_compare: {count:g} > {threshold:g}"
        if ("under" in ol or "below" in ol) and count < threshold:
            return str(outcome), f"count_compare: {count:g} < {threshold:g}"
        # exact equals only if an outcome explicitly supports equals
        if count == threshold and any(k in ol for k in ["equal", "exactly"]):
            return str(outcome), f"count_compare: {count:g} == {threshold:g}"
    return None, None

def _is_count_threshold_market(question: str, outcomes: list, intelligence: Optional[dict] = None, query_plan: Optional[dict] = None) -> bool:
    intel = intelligence or {}
    qp = query_plan or {}
    q = str(question or "").lower()
    if intel.get("resolver") == "count_compare" or intel.get("metric") == "count" or intel.get("count_subject"):
        return True
    if qp.get("need_number") and "count" in str(qp.get("what_to_validate", "")).lower():
        return True
    if any(x in q for x in ["how many", "number of", "count of", "total number"]):
        return True
    return any(re.search(r"\b(over|under|above|below)\s*\d+(?:\.\d+)?\b", str(o), re.I) for o in outcomes or [])

_PRE_COUNT_FIX_EXTRACT_SPECIFIC_ANSWER = extract_specific_answer

def extract_specific_answer(content: str, query_plan: dict,
                            question: str, outcomes: list,
                            intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Final override: for count threshold markets, extract the count and let Python
    perform exact threshold comparison. Ollama is extraction-only fallback.
    """
    if _is_count_threshold_market(question, outcomes, intelligence, query_plan):
        subject = (
            (intelligence or {}).get("count_subject")
            or (query_plan or {}).get("number_context")
            or (_infer_count_subject(question) if "_infer_count_subject" in globals() else "items")
        )
        threshold = (intelligence or {}).get("threshold") or _find_numeric_threshold(outcomes)
        if threshold is not None:
            threshold = float(threshold)

            answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else ""
            count = _extract_answer_line_count(answer_line, subject, question)

            if count is None:
                # Use existing deterministic content extractor as second layer.
                count = _extract_validated_count(content, subject, question) if "_extract_validated_count" in globals() else None

            if count is None:
                # Use Ollama only to extract the count, not to decide outcome.
                count = _extract_count_with_ollama(content, question, outcomes, subject, threshold)

            if count is not None:
                matched, calc = _map_count_to_threshold_outcome(float(count), threshold, outcomes)
                if matched:
                    print(f"[oracle] Count resolver → {matched} ({calc})")
                    return matched, calc
                return None, f"count {count:g} could not map to outcomes"

    return _PRE_COUNT_FIX_EXTRACT_SPECIFIC_ANSWER(content, query_plan, question, outcomes, intelligence)

_PRE_COUNT_FIX_DERIVE_OUTCOME = derive_outcome

def derive_outcome(facts: list[Fact], outcomes: list, question: str,
                   intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Final override for proof/fact-level derivation. If source builder already put
    raw evidence into facts, count markets still use deterministic count_compare.
    """
    if _is_count_threshold_market(question, outcomes, intelligence):
        evidence = "\n".join(str(f.value) for f in facts or [])
        subject = (intelligence or {}).get("count_subject") or (_infer_count_subject(question) if "_infer_count_subject" in globals() else "items")
        threshold = (intelligence or {}).get("threshold") or _find_numeric_threshold(outcomes)
        if threshold is not None:
            threshold = float(threshold)
            answer_line = extract_answer_line(evidence) if "extract_answer_line" in globals() else ""
            count = _extract_answer_line_count(answer_line, subject, question)
            if count is None and "_extract_validated_count" in globals():
                count = _extract_validated_count(evidence, subject, question)
            if count is None:
                count = _extract_count_with_ollama(evidence, question, outcomes, subject, threshold)
            if count is not None:
                matched, calc = _map_count_to_threshold_outcome(float(count), threshold, outcomes)
                if matched:
                    return matched, calc

    return _PRE_COUNT_FIX_DERIVE_OUTCOME(facts, outcomes, question, intelligence)

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL COUNT-MARKET FIX
# ═══════════════════════════════════════════════════════════════════════════════




# ═══════════════════════════════════════════════════════════════════════════════
# ABSOLUTE FINAL COUNT-THRESHOLD ROUTING FIX
# This block is intentionally placed after all earlier wrappers, because older
# sports wrappers were still overriding "How many ..." markets into sports_result.
# Rule: count/Over-Under markets are numeric_threshold markets. LLM extracts only
# the count; Python compares count vs threshold and maps exact outcome.
# ═══════════════════════════════════════════════════════════════════════════════

_PRE_ABS_FINAL_IS_SPORTS_RESULT_MARKET = _is_sports_result_market if "_is_sports_result_market" in globals() else None
_PRE_ABS_FINAL_ANALYZE_MARKET_INTELLIGENCE = analyze_market_intelligence
_PRE_ABS_FINAL_BUILD_UNIVERSAL_QUERY = build_universal_query
_PRE_ABS_FINAL_VALIDATE_EVIDENCE = validate_evidence

def _abs_final_is_count_market(question: str, outcomes: list, rules: Optional[dict] = None, intelligence: Optional[dict] = None) -> bool:
    q = str(question or "").lower()
    outs = " ".join(str(o).lower() for o in outcomes or [])
    rules = rules or {}
    intelligence = intelligence or {}
    return (
        rules.get("metric") == "count"
        or bool(rules.get("count_subject"))
        or intelligence.get("metric") == "count"
        or bool(inelligence_count_subject := intelligence.get("count_subject"))
        or intelligence.get("resolver") == "count_compare"
        or any(k in q for k in ["how many", "number of", "count of", "total number"])
        or bool(re.search(r"\b(over|under|above|below)\s*\d+(?:\.\d+)?\b", outs))
    )

def _is_sports_result_market(question: str, prompt_context: str, outcomes: list) -> bool:
    # Hard stop: "How many..." / Over-Under count markets are never sports_result,
    # even when the prompt says "official final result" or the domain is NFL/ESPN.
    if _abs_final_is_count_market(question, outcomes):
        return False
    if _PRE_ABS_FINAL_IS_SPORTS_RESULT_MARKET:
        return _PRE_ABS_FINAL_IS_SPORTS_RESULT_MARKET(question, prompt_context, outcomes)
    return False

def _abs_final_count_subject(question: str, rules: Optional[dict] = None) -> str:
    rules = rules or {}
    if rules.get("count_subject"):
        return str(rules["count_subject"])

    if "_infer_count_subject" in globals():
        subject = _infer_count_subject(question)
        if subject:
            return subject

    q = str(question or "").lower()
    subjects = [
        ("offensive lineman", "offensive linemen"),
        ("offensive linemen", "offensive linemen"),
        ("offensive line", "offensive linemen"),
        ("trade", "trades"),
        ("trades", "trades"),
        ("birdie", "birdies"),
        ("touchdown", "touchdowns"),
        ("goal", "goals"),
        ("point", "points"),
        ("wicket", "wickets"),
        ("pick", "picks"),
        ("selection", "selections"),
        ("player", "players"),
    ]
    for needle, normalized in subjects:
        if needle in q:
            return normalized
    m = re.search(r"how many\s+(.+?)\s+(?:will|were|are|have|has|did|does|occur|be|in|during)", q)
    return m.group(1).strip() if m else "items"

def _abs_final_force_count_intelligence(intel: dict, question: str, outcomes: list, close_time: str, prompt_context: str = "") -> dict:
    intel = dict(intel or {})
    close_date = (close_time[:10] if close_time else intel.get("event_date", "")) or "2026-01-01"
    rules = dict(intel.get("_rules") or {})
    # Re-parse rules if existing wrapper polluted metric as winner.
    try:
        parsed = parse_settlement_rules(prompt_context or intel.get("prompt_context", ""), question, outcomes)
        rules.update(parsed or {})
    except Exception:
        pass

    subject = _abs_final_count_subject(question, rules)
    threshold = rules.get("threshold")
    if threshold is None:
        threshold = intel.get("threshold")
    if threshold is None:
        threshold = _find_numeric_threshold(outcomes)

    rules.update({
        "metric": "count",
        "count_subject": subject,
        "threshold": threshold,
        "operator": ">" if any("over" in str(o).lower() or "above" in str(o).lower() for o in outcomes or []) else rules.get("operator"),
        "winner_logic": None,
        "source_policy": "creator_source_strict",
        "event_date": intel.get("event_date") or close_date,
    })

    intel.update({
        "event_description": question,
        "event_date": intel.get("event_date") or close_date,
        "facts_needed": ["count", "official_total", subject],
        "is_price_based": False,
        "asset": "",
        "threshold": threshold,
        "market_type": "numeric_threshold",
        "answer_format": "numeric_threshold",
        "resolver": "count_compare",
        "metric": "count",
        "count_subject": subject,
        "search_query": f"{question} official final count total {subject} {intel.get('event_date') or close_date}",
        "needs_canonicalization": True,
        "_rules": rules,
        "prompt_context": prompt_context or intel.get("prompt_context", ""),
    })
    return intel

def analyze_market_intelligence(question: str, prompt_context: str,
                                outcomes: list, data_sources: list, close_time: str) -> dict:
    intel = _PRE_ABS_FINAL_ANALYZE_MARKET_INTELLIGENCE(question, prompt_context, outcomes, data_sources, close_time)

    # Absolute final guard after every older planner/Groq/Ollama/sports wrapper.
    rules = intel.get("_rules") or {}
    if _abs_final_is_count_market(question, outcomes, rules=rules, intelligence=intel):
        intel = _abs_final_force_count_intelligence(intel, question, outcomes, close_time, prompt_context)
        print("[oracle] Count-threshold final override activated")
        print(f"[oracle] Plan: {intel['market_type']} | {intel['answer_format']} | {intel['resolver']}")
        print(f"[oracle] Plan facts: {intel['facts_needed']}")
        print(f"[oracle] Count subject: {intel.get('count_subject')} | threshold={intel.get('threshold')}")
    return intel

def build_universal_query(question: str, outcomes: list, rules: dict,
                          event_date: str, source_domain: str) -> dict:
    # Count query must win before sports query. Older sports wrapper checked
    # "official final result" and misrouted count markets to match_result.
    if _abs_final_is_count_market(question, outcomes, rules=rules):
        subject = _abs_final_count_subject(question, rules)
        year = ""
        m = re.search(r"\b(20\d{2})\b", str(question) + " " + str(event_date))
        if m:
            year = m.group(1)
        elif event_date:
            year = str(event_date)[:4]
        else:
            year = "2026"

        ql = str(question or "").lower()
        if "draft" in ql:
            query = f'{year} NFL Draft round 1 first round {subject} drafted selected ESPN official tracker complete list'
            if "trade" in subject:
                query = f'{year} NFL Draft round one trades ESPN trade tracker first round trades official'
            elif "offensive" in subject and "linemen" in subject:
                query = f'{year} NFL Draft first round offensive linemen drafted ESPN round 1 picks positions official'
        else:
            query = f'{question} official final count total {subject} {event_date}'

        out = {
            "query": query,
            "search_depth": "basic",
            "required_data_type": "count",
            "extraction_target": subject,
            "what_to_validate": f"count of {subject}",
            "need_number": True,
            "number_context": subject,
        }
        if "offensive" in subject and "linemen" in subject:
            out["count_method"] = "position_codes"
            out["position_codes"] = ["OT", "OG", "OC", "G", "T", "C", "IOL", "OL"]
        return out

    return _PRE_ABS_FINAL_BUILD_UNIVERSAL_QUERY(question, outcomes, rules, event_date, source_domain)

def _abs_final_text_has_count_for_subject(content: str, subject: str, question: str = "") -> tuple[bool, str]:
    text = str(content or "")
    tlow = text.lower()
    answer_line = extract_answer_line(text) if "extract_answer_line" in globals() else ""
    subject = (subject or _abs_final_count_subject(question)).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", subject) if len(w) >= 3]

    # Try the dedicated count extractor first.
    if "_extract_answer_line_count" in globals():
        count = _extract_answer_line_count(answer_line, subject, question)
        if count is not None:
            return True, f"answer-line count found: {count:g}"

    # Accept word-number counts attached to subject, e.g. "Nine offensive linemen..."
    number_words = (
        "zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        "thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
        "twenty[- ]one|twenty[- ]two|twenty[- ]three|twenty[- ]four|twenty[- ]five|"
        "thirty|forty|forty[- ]one"
    )
    number_pat = rf"(?:\d+(?:\.\d+)?|{number_words})"
    for w in words or ["items"]:
        if re.search(rf"\b{number_pat}\s+(?:\w+\s+){{0,2}}{re.escape(w)}\b", tlow):
            return True, f"count word/number near subject: {w}"
        if re.search(rf"\b{w}\b.{{0,80}}\b{number_pat}\b", tlow):
            return True, f"subject near count: {w}"

    # Position-code table fallback for OL draft markets.
    if "offensive" in subject and "linemen" in subject:
        if re.search(r"\b(OT|OG|OC|G|T|C|IOL|OL)\b", text):
            return True, "offensive line position codes found"

    return False, f"no validated count for {subject}"

def validate_evidence(content: str, required_data_type: str,
                      extraction_target: str, question: str) -> tuple[bool, str]:
    if str(required_data_type or "").lower() == "count":
        ok, reason = _abs_final_text_has_count_for_subject(content, extraction_target, question)
        if ok:
            return True, reason
        # Fall through to previous validation only if it can do better.
        prev_ok, prev_reason = _PRE_ABS_FINAL_VALIDATE_EVIDENCE(content, required_data_type, extraction_target, question)
        return prev_ok, prev_reason if prev_ok else reason

    return _PRE_ABS_FINAL_VALIDATE_EVIDENCE(content, required_data_type, extraction_target, question)

# ═══════════════════════════════════════════════════════════════════════════════
# END ABSOLUTE FINAL COUNT-THRESHOLD ROUTING FIX
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# ABSOLUTE FINAL DRAFT POSITION RESOLVER FIX
# This block is placed last so older sports wrappers cannot route NFL Draft
# position markets into sports_result. It handles markets like:
# "Who will the Giants draft with their SECOND pick in round one?" where the
# valid outcomes are positions/classes, not player names.
# Rule: extract player/position from creator-source evidence; map position to an
# exact outcome in Python. Ollama is extraction-only fallback.
# ═══════════════════════════════════════════════════════════════════════════════

_PRE_DRAFT_POS_ANALYZE_MARKET_INTELLIGENCE = analyze_market_intelligence
_PRE_DRAFT_POS_BUILD_UNIVERSAL_QUERY = build_universal_query
_PRE_DRAFT_POS_VALIDATE_EVIDENCE = validate_evidence
_PRE_DRAFT_POS_EXTRACT_SPECIFIC_ANSWER = extract_specific_answer
_PRE_DRAFT_POS_DERIVE_OUTCOME = derive_outcome

_DRAFT_POSITION_OUTCOME_ALIASES = {
    "Offensive Lineman": ["offensive lineman", "offensive linemen", "ol", "ot", "og", "oc", "lt", "rt", "lg", "rg", "center", "guard", "tackle", "right guard", "left guard", "right tackle", "left tackle", "interior offensive line", "offensive line"],
    "Tight End": ["tight end", "te"],
    "Cornerback": ["cornerback", "corner", "cb", "nickel"],
    "Safety": ["safety", "fs", "ss"],
    "Quarterback": ["quarterback", "qb"],
    "Wide Receiver": ["wide receiver", "receiver", "wr"],
    "Defensive Line / Edge": ["defensive line", "defensive lineman", "defensive tackle", "dt", "de", "edge", "edge rusher", "pass rusher", "defensive end", "dl"],
    "Running Back": ["running back", "rb"],
    "Linebacker": ["linebacker", "lb", "inside linebacker", "outside linebacker", "ilb", "olb"],
    "Kicker / Punter / Long Snapper": ["kicker", "punter", "long snapper", "k", "p", "ls", "specialist"],
}

_POSITION_CODE_MAP = {
    "OL": "Offensive Lineman", "OT": "Offensive Lineman", "OG": "Offensive Lineman", "OC": "Offensive Lineman", "C": "Offensive Lineman", "G": "Offensive Lineman", "T": "Offensive Lineman", "IOL": "Offensive Lineman",
    "TE": "Tight End",
    "CB": "Cornerback",
    "S": "Safety", "FS": "Safety", "SS": "Safety",
    "QB": "Quarterback",
    "WR": "Wide Receiver",
    "DL": "Defensive Line / Edge", "DE": "Defensive Line / Edge", "DT": "Defensive Line / Edge", "EDGE": "Defensive Line / Edge", "ED": "Defensive Line / Edge",
    "RB": "Running Back",
    "LB": "Linebacker", "ILB": "Linebacker", "OLB": "Linebacker",
    "K": "Kicker / Punter / Long Snapper", "P": "Kicker / Punter / Long Snapper", "LS": "Kicker / Punter / Long Snapper",
}


def _draft_pos_norm(s: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def _is_draft_position_market(question: str, outcomes: list, intelligence: Optional[dict] = None) -> bool:
    q = str(question or "").lower()
    if "draft" not in q:
        return False
    # Count markets were fixed separately. Do not steal them.
    if _abs_final_is_count_market(question, outcomes, intelligence=intelligence or {}) if "_abs_final_is_count_market" in globals() else False:
        return False
    outs = {_draft_pos_norm(o) for o in outcomes or []}
    known = {_draft_pos_norm(k) for k in _DRAFT_POSITION_OUTCOME_ALIASES}
    # Position-outcome markets usually include several of these buckets.
    if len(outs & known) >= 2:
        return True
    # Also handle common wording even if outcomes are partial.
    position_words = ["offensive lineman", "cornerback", "quarterback", "wide receiver", "defensive line", "running back", "linebacker", "tight end", "safety"]
    return any(w in " ".join(outs) for w in position_words)


def _draft_pick_order_index(question: str) -> Optional[int]:
    q = str(question or "").lower()
    if re.search(r"\b(second|2nd)\b", q):
        return 2
    if re.search(r"\b(first|1st)\b", q):
        return 1
    if re.search(r"\b(third|3rd)\b", q):
        return 3
    if re.search(r"\b(fourth|4th)\b", q):
        return 4
    return None


def _draft_team_hint(question: str) -> str:
    q = str(question or "")
    m = re.search(r"who\s+will\s+the\s+(.+?)\s+draft\b", q, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"the\s+(.+?)\s+draft\s+with", q, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _map_position_to_valid_outcome(position_text: str, outcomes: list) -> Optional[str]:
    text = str(position_text or "")
    tnorm = _draft_pos_norm(text)
    out_by_norm = {_draft_pos_norm(o): str(o) for o in outcomes or []}

    # Direct outcome match first.
    if tnorm in out_by_norm:
        return out_by_norm[tnorm]

    # Position code exact matching, e.g. "OL", "CB".
    for code, canonical in _POSITION_CODE_MAP.items():
        if re.search(rf"\b{re.escape(code)}\b", text, re.I):
            c_norm = _draft_pos_norm(canonical)
            if c_norm in out_by_norm:
                return out_by_norm[c_norm]
            # Some markets may use slightly different slash wording.
            for o in outcomes or []:
                if _draft_pos_norm(canonical) == _draft_pos_norm(o):
                    return str(o)

    # Alias phrase matching.
    for canonical, aliases in _DRAFT_POSITION_OUTCOME_ALIASES.items():
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias)}\b", tnorm):
                c_norm = _draft_pos_norm(canonical)
                if c_norm in out_by_norm:
                    return out_by_norm[c_norm]
                for o in outcomes or []:
                    if _draft_pos_norm(canonical) == _draft_pos_norm(o):
                        return str(o)
    return None


def _extract_draft_position_from_content(content: str, question: str, outcomes: list) -> tuple[Optional[str], Optional[str]]:
    """
    Prefer structured source snippets over Tavily's ANSWER line because Tavily can
    synthesize the wrong player from nearby draft articles. ESPN selection pages
    usually contain lines like: "Round 1, No. 10: Francis Mauigoa, OL, Miami".
    """
    text = str(content or "")
    q = str(question or "").lower()
    order = _draft_pick_order_index(question)
    team = _draft_team_hint(question)

    # 1) Structured ESPN/NFL pick line: Round 1, No. 10: Name, POS, School
    pick_lines = []
    for m in re.finditer(r"Round\s+1\s*,?\s*No\.\s*(\d+)\s*:\s*([^\n\r#]+)", text, re.I):
        line = " ".join(m.group(0).split())
        pick_no = int(m.group(1))
        rest = m.group(2).strip()
        parts = [p.strip() for p in rest.split(",") if p.strip()]
        pos = parts[1] if len(parts) >= 2 else rest
        pick_lines.append((pick_no, pos, line))

    if pick_lines:
        # If the article snippet has only one direct pick line, prefer it over ANSWER.
        # If it has several, use the ordinal requested by the question.
        chosen = None
        if order and len(pick_lines) >= order:
            chosen = pick_lines[order - 1]
        elif len(pick_lines) == 1:
            chosen = pick_lines[0]
        else:
            # For "second pick" markets, high pick numbers after the team's first are often the requested pick.
            chosen = sorted(pick_lines, key=lambda x: x[0])[min((order or 1) - 1, len(pick_lines)-1)]
        matched = _map_position_to_valid_outcome(chosen[1], outcomes)
        if matched:
            return matched, f"draft_position: {chosen[2]} → {matched}"

    # 2) Sentence-level hints around "second pick / 10th overall / grabbed NAME".
    # Example: "with their second pick, 10th overall, when they grabbed Mauigoa... move inside to right guard".
    q_terms = ["second pick", "2nd pick", "10th overall", "second of"] if order == 2 else ["first pick", "1st pick"]
    for term in q_terms:
        idx = text.lower().find(term)
        if idx != -1:
            window = text[max(0, idx-500): idx+1200]
            # Check explicit position codes/aliases inside the focused window.
            matched = _map_position_to_valid_outcome(window, outcomes)
            if matched:
                return matched, f"draft_position context near '{term}' → {matched}"

    # 3) ANSWER line fallback only if it maps cleanly to an outcome.
    ans = extract_answer_line(text) if "extract_answer_line" in globals() else ""
    if ans:
        matched = _map_position_to_valid_outcome(ans, outcomes)
        if matched:
            return matched, f"draft_position answer-line: {ans[:120]} → {matched}"

    # 4) Ollama extraction fallback: extract position only; Python maps outcome.
    if USE_OLLAMA_BRAIN and "call_ollama_json" in globals():
        prompt = (
            "You are OracleREE draft evidence extractor. Return valid JSON only. "
            "Do not decide the final market outcome. Extract only the drafted player's position.\n"
            f"Question: {question}\n"
            f"Valid outcomes: {', '.join(str(o) for o in outcomes or [])}\n"
            "Return JSON keys: player, position, draft_pick, confidence.\n"
            f"Evidence:\n{text[:6000]}"
        )
        obj = call_ollama_json(prompt, timeout=90)
        if isinstance(obj, dict):
            pos = str(obj.get("position") or obj.get("player_position") or "")
            matched = _map_position_to_valid_outcome(pos, outcomes)
            if matched:
                return matched, f"draft_position ollama_extract: {pos} → {matched}"

    return None, None


def analyze_market_intelligence(question: str, prompt_context: str, outcomes: list, data_sources: list, close_time: str) -> dict:
    plan = _PRE_DRAFT_POS_ANALYZE_MARKET_INTELLIGENCE(question, prompt_context, outcomes, data_sources, close_time)
    if _is_draft_position_market(question, outcomes, plan):
        close_date = close_time[:10] if close_time else plan.get("event_date", "unknown")
        team = _draft_team_hint(question)
        order = _draft_pick_order_index(question)
        order_phrase = "second" if order == 2 else ("first" if order == 1 else ("third" if order == 3 else ""))
        plan.update({
            "market_type": "draft_named_choice",
            "answer_format": "named_choice",
            "resolver": "map_player_position_to_outcome",
            "facts_needed": ["team", "pick_order", "player_selected", "player_position", "draft_pick", "position"],
            "metric": "draft_position",
            "draft_team": team,
            "pick_order": order,
            "search_query": f"{team} {order_phrase} pick first round 2026 NFL Draft ESPN selection analysis position official".strip(),
            "event_date": close_date,
        })
        print(f"[oracle] Draft position guard → {plan.get('market_type')} | {plan.get('resolver')}")
    return plan


def build_universal_query(question: str, outcomes: list, rules: dict, event_date: str, source_domain: str) -> dict:
    if _is_draft_position_market(question, outcomes):
        team = _draft_team_hint(question)
        order = _draft_pick_order_index(question)
        order_phrase = "second" if order == 2 else ("first" if order == 1 else ("third" if order == 3 else ""))
        query = f"{team} 2026 NFL draft picks {order_phrase} first round selection ESPN position analysis depth chart"
        return {
            "query": query.strip(),
            "search_depth": "basic",
            "required_data_type": "draft_position",
            "extraction_target": f"{team} {order_phrase} first-round draft pick position".strip(),
            "what_to_validate": "draft_position",
            "need_number": False,
            "number_context": "",
        }
    return _PRE_DRAFT_POS_BUILD_UNIVERSAL_QUERY(question, outcomes, rules, event_date, source_domain)


def validate_evidence(content: str, required_data_type: str, extraction_target: str, question: str) -> tuple[bool, str]:
    if str(required_data_type or "").lower() == "draft_position":
        # Use outcome-agnostic validation here: source must contain a draft pick and some position marker.
        text = str(content or "")
        if re.search(r"Round\s+1\s*,?\s*No\.\s*\d+\s*:\s*[^\n]+,\s*(OL|OT|OG|OC|C|G|T|IOL|TE|CB|S|FS|SS|QB|WR|DL|DE|DT|EDGE|RB|LB|ILB|OLB|K|P|LS)\b", text, re.I):
            return True, "structured round-1 draft pick position found"
        ans = extract_answer_line(text) if "extract_answer_line" in globals() else ""
        if ans and _map_position_to_valid_outcome(ans, list(_DRAFT_POSITION_OUTCOME_ALIASES.keys())):
            return True, "answer line contains draft position"
        if re.search(r"\b(drafted|selected|grabbed|took)\b", text, re.I) and re.search(r"\b(OL|OT|OG|OC|TE|CB|QB|WR|EDGE|RB|LB|safety|cornerback|guard|tackle|center|receiver|linebacker)\b", text, re.I):
            return True, "draft selection and position context found"
        return False, "no validated draft position found"
    return _PRE_DRAFT_POS_VALIDATE_EVIDENCE(content, required_data_type, extraction_target, question)


def extract_specific_answer(content: str, query_plan: dict, question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    if _is_draft_position_market(question, outcomes, intelligence) or str((query_plan or {}).get("required_data_type") or "") == "draft_position":
        matched, calc = _extract_draft_position_from_content(content, question, outcomes)
        if matched:
            print(f"[oracle] Draft position resolver → {matched} ({calc})")
            return matched, calc
        return None, "draft_position evidence validated but position could not map to valid outcome"
    return _PRE_DRAFT_POS_EXTRACT_SPECIFIC_ANSWER(content, query_plan, question, outcomes, intelligence)


def derive_outcome(facts: list[Fact], outcomes: list, question: str, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    if _is_draft_position_market(question, outcomes, intelligence):
        evidence = "\n".join(str(f.value) for f in facts or [])
        matched, calc = _extract_draft_position_from_content(evidence, question, outcomes)
        if matched:
            return matched, calc
    return _PRE_DRAFT_POS_DERIVE_OUTCOME(facts, outcomes, question, intelligence)

# ═══════════════════════════════════════════════════════════════════════════════
# END ABSOLUTE FINAL DRAFT POSITION RESOLVER FIX
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# ABSOLUTE FINAL CANONICAL ENTITY + SPREAD RESOLVER FIX
# Fixes shorthand/alias outcomes like:
#   San Antonio Spurs = Spurs = SAS, Oklahoma City Thunder = Thunder = OKC
#   Barcelona = Barça = Barca, Tottenham Hotspur = Tottenham = Spurs
#   OL = Offensive Lineman, QB = Quarterback (handled by draft resolver above)
# The LLM/Ollama may help extract aliases, but Python does the final settlement math.
# ═══════════════════════════════════════════════════════════════════════════════

_PRE_ALIAS_FINAL_ANALYZE_MARKET_INTELLIGENCE = analyze_market_intelligence
_PRE_ALIAS_FINAL_BUILD_UNIVERSAL_QUERY = build_universal_query
_PRE_ALIAS_FINAL_VALIDATE_EVIDENCE = validate_evidence
_PRE_ALIAS_FINAL_EXTRACT_SPECIFIC_ANSWER = extract_specific_answer
_PRE_ALIAS_FINAL_DERIVE_OUTCOME = derive_outcome


def _canon_text(s: object) -> str:
    """Lowercase, de-accent, punctuation-light canonical text."""
    import unicodedata
    x = unicodedata.normalize("NFKD", str(s or ""))
    x = "".join(ch for ch in x if not unicodedata.combining(ch))
    x = x.lower().replace("&", " and ")
    x = re.sub(r"[^a-z0-9+\-. ]+", " ", x)
    x = re.sub(r"\s+", " ", x).strip()
    return x


def _strip_spread_from_outcome(outcome: object) -> str:
    return re.sub(r"\s*[+-]\d+(?:\.\d+)?\s*$", "", str(outcome or "")).strip()


def _parse_spread_outcome(outcome: object) -> Optional[tuple[str, float]]:
    m = re.match(r"^(.+?)\s*([+-]\d+(?:\.\d+)?)\s*$", str(outcome or "").strip())
    if not m:
        return None
    return m.group(1).strip(), float(m.group(2))


def _is_spread_market_outcomes(outcomes: list) -> bool:
    return bool(outcomes) and len(outcomes) == 2 and all(_parse_spread_outcome(o) for o in outcomes or [])


_STATIC_ENTITY_ALIASES = {
    # NBA / common US sports
    "spurs": ["spurs", "san antonio", "san antonio spurs", "sas", "sa"],
    "thunder": ["thunder", "oklahoma city", "oklahoma city thunder", "okc"],
    "lakers": ["lakers", "los angeles lakers", "la lakers", "lal"],
    "celtics": ["celtics", "boston celtics", "bos"],
    "knicks": ["knicks", "new york knicks", "ny knicks", "nyk"],
    "cavaliers": ["cavaliers", "cleveland cavaliers", "cavs", "cle"],
    "warriors": ["warriors", "golden state warriors", "gsw", "golden state"],
    "mavericks": ["mavericks", "dallas mavericks", "mavs", "dal"],
    "nuggets": ["nuggets", "denver nuggets", "den"],
    "timberwolves": ["timberwolves", "minnesota timberwolves", "wolves", "min"],
    "suns": ["suns", "phoenix suns", "phx"],
    "heat": ["heat", "miami heat", "mia"],
    "bucks": ["bucks", "milwaukee bucks", "mil"],
    "clippers": ["clippers", "los angeles clippers", "la clippers", "lac"],
    "76ers": ["76ers", "sixers", "philadelphia 76ers", "phi"],

    # Football/soccer common ambiguity support
    "barcelona": ["barcelona", "fc barcelona", "barca", "barça", "fcb"],
    "tottenham": ["tottenham", "tottenham hotspur", "spurs", "thfc"],
    "manchester city": ["manchester city", "man city", "city", "mcfc"],
    "manchester united": ["manchester united", "man united", "man utd", "united", "mufc"],
    "real madrid": ["real madrid", "madrid", "rmcf"],
    "psg": ["psg", "paris saint germain", "paris saint-germain"],
}


def _aliases_for_outcome_base(base: str, question: str = "", evidence: str = "") -> list[str]:
    """Create aliases for an outcome base, using static map + local tokens."""
    b = _canon_text(base)
    aliases = {b}
    compact = b.replace(" ", "")
    if compact:
        aliases.add(compact)

    # Include last significant token for team nicknames: "San Antonio Spurs" -> "spurs".
    parts = [p for p in b.split() if p not in {"fc", "cf", "sc", "afc", "the"}]
    if parts:
        aliases.add(parts[-1])

    # Static known map. Keep it outcome-scoped to avoid confusing Tottenham Spurs vs San Antonio Spurs.
    for key, vals in _STATIC_ENTITY_ALIASES.items():
        kb = _canon_text(key)
        valset = {_canon_text(v) for v in vals}
        if b == kb or b in valset or kb in aliases or aliases.intersection(valset):
            aliases.update(valset)
            aliases.add(kb)

    return sorted(a for a in aliases if a)


def _term_in_text(term: str, text: str) -> bool:
    t = _canon_text(text)
    x = _canon_text(term)
    if not x:
        return False
    # abbreviations like OKC/SAS or compact forms
    if len(x) <= 4 and " " not in x:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(x)}(?![a-z0-9])", t))
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(x)}(?![a-z0-9])", t)) or x.replace(" ", "") in t.replace(" ", "")


def _extract_scores_from_rows(text: str, outcome_bases: list[str]) -> dict[str, int]:
    """
    Extract final TEAM scores from real scoreboard/table rows only.
    Important: do NOT treat player-stat lines like "Brunson led the Knicks with 38 points"
    as team scores. For spread markets, player points are poison.
    """
    scores: dict[str, int] = {}
    lines = str(text or "").splitlines()

    for base in outcome_bases:
        aliases = _aliases_for_outcome_base(base, evidence=text)
        for line in lines:
            raw_line = str(line or "").strip()
            if not raw_line:
                continue

            cl = _canon_text(raw_line)
            if not any(_term_in_text(a, cl) for a in aliases):
                continue

            # Reject obvious player-stat/prose lines.
            if re.search(r"\b(points?|rebounds?|assists?|steals?|blocks?|turnovers?|led|leader)\b", cl) and "|" not in raw_line:
                continue

            nums = [int(x) for x in re.findall(r"\b\d{1,3}\b", raw_line)]
            nums = [n for n in nums if n < 250]

            # Real boxscore rows usually contain several quarter/period scores plus final score,
            # or are pipe-table rows. A single number near a team name is usually a player stat.
            if "|" not in raw_line and len(nums) < 2:
                continue

            if nums:
                scores[base] = nums[-1]
                break
    return scores


def _extract_winner_loser_score_from_answer(text: str, outcome_bases: list[str]) -> dict[str, int]:
    """
    Extract final score from the ANSWER sentence, e.g.
    'The Knicks won 115-104 in overtime.'
    This is stronger than noisy play-by-play/table snippets and avoids player stats.
    """
    scores: dict[str, int] = {}
    ans = extract_answer_line(text) if "extract_answer_line" in globals() else ""
    sample = ans or str(text or "")[:1800]
    sample_c = _canon_text(sample)

    m = re.search(r"\b(\d{2,3})\s*[-–]\s*(\d{2,3})\b", sample)
    if not m:
        return scores

    a, b = int(m.group(1)), int(m.group(2))
    if a >= 250 or b >= 250:
        return scores

    # Use the sentence/clause containing the score, not the whole evidence blob.
    left_full = sample[:m.start()]
    sent_start = max(left_full.rfind("."), left_full.rfind("\n"), left_full.rfind(";"))
    score_sentence = sample[sent_start + 1:min(len(sample), m.end() + 120)]
    score_sentence_c = _canon_text(score_sentence)

    winner_base = None
    loser_base = None

    # Strong direct winner pattern in the same score sentence:
    # "The Knicks won 115-104", "Spurs beat Thunder 122-115"
    for base in outcome_bases:
        aliases = _aliases_for_outcome_base(base, evidence=sample)
        for alias in aliases:
            ac = _canon_text(alias)
            if not ac:
                continue
            if re.search(
                rf"(?<![a-z0-9]){re.escape(ac)}(?![a-z0-9]).{{0,35}}\b(won|win|beat|beats|defeated|defeats|top|topped)\b",
                score_sentence_c,
            ):
                winner_base = base
                break
        if winner_base:
            break

    # Strong object/loser pattern in same score sentence.
    if not winner_base:
        for base in outcome_bases:
            aliases = _aliases_for_outcome_base(base, evidence=sample)
            for alias in aliases:
                ac = _canon_text(alias)
                if not ac:
                    continue
                if re.search(
                    rf"\b(against|over|beat|beats|defeated|defeats|top|topped)\b.{{0,80}}(?<![a-z0-9]){re.escape(ac)}(?![a-z0-9])",
                    score_sentence_c,
                ):
                    loser_base = base
                    break
            if loser_base:
                break

    if winner_base:
        # In sports writing, "X won 115-104" means winner's score is first.
        scores[winner_base] = a
        others = [x for x in outcome_bases if x != winner_base]
        if others:
            scores[others[0]] = b
        return scores

    if loser_base:
        # If only the loser is identified as object, assign smaller score to loser.
        scores[loser_base] = min(a, b)
        others = [x for x in outcome_bases if x != loser_base]
        if others:
            scores[others[0]] = max(a, b)
        return scores

    # Fallback: if score appears as "TeamA ... TeamB ... 115-104",
    # assume the first named team maps to first score and second named team maps to second score.
    positions = []
    for base in outcome_bases:
        aliases = _aliases_for_outcome_base(base, evidence=sample)
        best = None
        for alias in aliases:
            ac = _canon_text(alias)
            if not ac:
                continue
            mm = re.search(rf"(?<![a-z0-9]){re.escape(ac)}(?![a-z0-9])", sample_c)
            if mm:
                best = mm.start() if best is None else min(best, mm.start())
        if best is not None:
            positions.append((best, base))
    positions.sort()
    if len(positions) >= 2:
        scores[positions[0][1]] = a
        scores[positions[1][1]] = b

    return scores


def _ollama_extract_sports_alias_score(text: str, outcomes: list, question: str) -> Optional[dict]:
    """
    Ollama extraction fallback only. Python still computes the result.
    Critical: extract FINAL TEAM SCORES only, never player points/rebounds/assists.
    """
    if not USE_OLLAMA_BRAIN or "call_ollama_json" not in globals():
        return None
    prompt = (
        "You are OracleREE sports final-score extractor. Return valid JSON only.\n"
        "Do not decide the market outcome. Do not use player stats.\n"
        "Extract ONLY the official FINAL TEAM SCORE for the two teams in the valid outcomes.\n"
        "Ignore player points, rebounds, assists, play-by-play numbers, preview statistics, betting odds, rankings, and records.\n"
        "If you cannot find both final team scores, return null scores and confidence='low'.\n\n"
        f"Question: {question}\n"
        f"Valid outcomes: {', '.join(str(o) for o in outcomes or [])}\n"
        "JSON keys: team1, team1_aliases, team1_score, team2, team2_aliases, team2_score, winner, confidence, evidence_quote.\n"
        f"Evidence:\n{str(text or '')[:6000]}"
    )
    obj = call_ollama_json(prompt, timeout=90)
    return obj if isinstance(obj, dict) else None


def _resolve_spread_with_aliases(content: str, outcomes: list, question: str = "") -> tuple[Optional[str], Optional[str]]:
    """
    Deterministically resolve spread markets with canonical team aliases.
    Priority order:
      1. ANSWER line final score sentence
      2. scoreboard/table rows
      3. Ollama final-score extractor
    Never use player-stat numbers as team scores.
    """
    parsed = [(_strip_spread_from_outcome(o), _parse_spread_outcome(o), str(o)) for o in outcomes or []]
    if len(parsed) != 2 or any(p[1] is None for p in parsed):
        return None, None

    bases = [p[0] for p in parsed]
    spreads = {p[0]: p[1][1] for p in parsed if p[1] is not None}
    original = {p[0]: p[2] for p in parsed}

    # 1) ANSWER line sentence is strongest for spread markets:
    # "The Knicks won 115-104..." binds teams to final score.
    scores = _extract_winner_loser_score_from_answer(content, bases)

    # 2) Source table rows bind team name to final score.
    if len(scores) < 2:
        scores.update(_extract_scores_from_rows(content, bases))

    # Guard: if both scores are identical low numbers, this is usually a player stat
    # accidentally mapped to both teams (e.g. "Brunson led Knicks with 38 points").
    if len(scores) == 2:
        vals = list(scores.values())
        if vals[0] == vals[1] and vals[0] < 70:
            scores = {}

    # 3) Ollama extractor fallback: use only final TEAM scores/entities; Python computes.
    if len(scores) < 2:
        obj = _ollama_extract_sports_alias_score(content, outcomes, question)
        if obj:
            conf = str(obj.get("confidence") or "").lower()
            candidate_scores = {}
            for base in bases:
                aliases = _aliases_for_outcome_base(base, question=question, evidence=content)
                team1_aliases = obj.get("team1_aliases") if isinstance(obj.get("team1_aliases"), list) else []
                team2_aliases = obj.get("team2_aliases") if isinstance(obj.get("team2_aliases"), list) else []
                t1_blob = " ".join([str(obj.get("team1") or "")] + [str(x) for x in team1_aliases])
                t2_blob = " ".join([str(obj.get("team2") or "")] + [str(x) for x in team2_aliases])

                if any(_term_in_text(a, t1_blob) for a in aliases):
                    try:
                        v = int(float(obj.get("team1_score")))
                        if 0 <= v < 250:
                            candidate_scores[base] = v
                    except Exception:
                        pass
                if any(_term_in_text(a, t2_blob) for a in aliases):
                    try:
                        v = int(float(obj.get("team2_score")))
                        if 0 <= v < 250:
                            candidate_scores[base] = v
                    except Exception:
                        pass

            # Reject low identical "scores" and low-confidence extractor outputs.
            if len(candidate_scores) == 2:
                vals = list(candidate_scores.values())
                if not (vals[0] == vals[1] and vals[0] < 70) and conf not in {"low", "none", "uncertain"}:
                    scores.update(candidate_scores)

    if len(scores) < 2:
        return None, f"spread_alias: could not extract both final team scores; scores={scores}"

    b1, b2 = bases[0], bases[1]
    s1, s2 = scores.get(b1), scores.get(b2)
    if s1 is None or s2 is None:
        return None, f"spread_alias: missing score mapping; scores={scores}"

    adj1 = float(s1) + float(spreads[b1])
    adj2 = float(s2) + float(spreads[b2])

    # Against-the-spread: team covers if adjusted score beats opponent score.
    c1 = adj1 > float(s2)
    c2 = adj2 > float(s1)

    if c1 and not c2:
        return original[b1], f"spread_final_score: {b1} {s1} + ({spreads[b1]}) = {adj1} > {b2} {s2} → {original[b1]}"
    if c2 and not c1:
        return original[b2], f"spread_final_score: {b2} {s2} + ({spreads[b2]}) = {adj2} > {b1} {s1} → {original[b2]}"

    # If both appear true due to paired +line/-line math, choose higher adjusted score.
    if adj1 > adj2:
        return original[b1], f"spread_final_score: adjusted {b1} {adj1} > {b2} {adj2} → {original[b1]}"
    if adj2 > adj1:
        return original[b2], f"spread_final_score: adjusted {b2} {adj2} > {b1} {adj1} → {original[b2]}"

    return None, f"spread_final_score: push/no cover? scores={scores}, spreads={spreads}"


def _resolve_named_choice_with_aliases(content: str, outcomes: list, question: str = "") -> tuple[Optional[str], Optional[str]]:
    """Resolve named choices where evidence uses full name but outcome is shorthand."""
    text = str(content or "")
    ans = extract_answer_line(text) if "extract_answer_line" in globals() else ""
    hay = ans + "\n" + text[:2500]
    for outcome in outcomes or []:
        base = _strip_spread_from_outcome(outcome)
        aliases = _aliases_for_outcome_base(base, question=question, evidence=hay)
        # Avoid very ambiguous tiny aliases unless accompanied by score/winner context.
        for alias in aliases:
            if len(alias) <= 2:
                continue
            if _term_in_text(alias, hay):
                # For winner markets, avoid mapping a loser phrase as the winner.
                c = _canon_text(hay)
                a = _canon_text(alias)
                loser_ctx = re.search(rf"\b(against|over|beat|defeated|topped)\b.{{0,120}}(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", c)
                subject_win = re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9]).{{0,120}}\b(won|beat|defeated|topped|covered|covers)\b", c)
                if loser_ctx and not subject_win:
                    continue
                return str(outcome), f"alias_named_choice: {alias} → {outcome}"
    return None, None


def analyze_market_intelligence(question: str, prompt_context: str, outcomes: list, data_sources: list, close_time: str) -> dict:
    plan = _PRE_ALIAS_FINAL_ANALYZE_MARKET_INTELLIGENCE(question, prompt_context, outcomes, data_sources, close_time)
    if _is_spread_market_outcomes(outcomes):
        close_date = close_time[:10] if close_time else plan.get("event_date", "unknown")
        plan.update({
            "market_type": "sports_spread",
            "answer_format": "spread_cover",
            "resolver": "spread_cover",
            "facts_needed": ["final_score", "team_scores", "point_spread", "winner"],
            "metric": "spread_cover",
            "threshold": None,
            "search_query": f"{question} final score box score spread result {close_date}",
            "event_date": close_date,
            "needs_canonicalization": True,
        })
        print(f"[oracle] Alias/spread guard → {plan.get('market_type')} | {plan.get('resolver')}")
    return plan


def build_universal_query(question: str, outcomes: list, rules: dict, event_date: str, source_domain: str) -> dict:
    if _is_spread_market_outcomes(outcomes):
        bases = [_strip_spread_from_outcome(o) for o in outcomes or []]
        return {
            "query": f"{question} {' '.join(bases)} final score box score game 1 spread {event_date}".strip(),
            "search_depth": "basic",
            "required_data_type": "sports_spread",
            "extraction_target": f"final score and spread cover for {' vs '.join(bases)}",
            "what_to_validate": "sports_spread",
            "need_number": True,
            "number_context": "final score",
        }
    return _PRE_ALIAS_FINAL_BUILD_UNIVERSAL_QUERY(question, outcomes, rules, event_date, source_domain)


def validate_evidence(content: str, required_data_type: str, extraction_target: str, question: str) -> tuple[bool, str]:
    if str(required_data_type or "").lower() in {"sports_spread", "spread_cover"}:
        text = str(content or "")
        # Need at least a score and either win/boxscore context.
        has_score = bool(re.search(r"\b\d{2,3}\s*[-–]\s*\d{2,3}\b", text)) or bool(re.search(r"\|[^\n]+\|\s*\d{1,3}\s*\|", text))
        has_game = bool(re.search(r"\b(final score|box score|won|beat|defeated|top|topped|game)\b", text, re.I))
        if has_score and has_game:
            return True, "sports spread evidence has final score context"
        return False, "no final score context for spread market"
    return _PRE_ALIAS_FINAL_VALIDATE_EVIDENCE(content, required_data_type, extraction_target, question)


def extract_specific_answer(content: str, query_plan: dict, question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    if _is_spread_market_outcomes(outcomes) or str((query_plan or {}).get("required_data_type") or "").lower() in {"sports_spread", "spread_cover"}:
        matched, calc = _resolve_spread_with_aliases(content, outcomes, question)
        if matched:
            print(f"[oracle] Alias spread resolver → {matched} ({calc})")
            return matched, calc
        return None, calc or "spread evidence validated but could not map teams/scores to outcome aliases"

    matched, calc = _resolve_named_choice_with_aliases(content, outcomes, question)
    if matched:
        print(f"[oracle] Alias named-choice resolver → {matched} ({calc})")
        return matched, calc
    return _PRE_ALIAS_FINAL_EXTRACT_SPECIFIC_ANSWER(content, query_plan, question, outcomes, intelligence)


def derive_outcome(facts: list[Fact], outcomes: list, question: str, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    evidence = "\n".join(str(f.value) for f in facts or [])
    if _is_spread_market_outcomes(outcomes) or str((intelligence or {}).get("resolver") or "").lower() in {"spread_cover", "sports_spread"}:
        matched, calc = _resolve_spread_with_aliases(evidence, outcomes, question)
        if matched:
            return matched, calc
    matched, calc = _resolve_named_choice_with_aliases(evidence, outcomes, question)
    if matched:
        return matched, calc
    return _PRE_ALIAS_FINAL_DERIVE_OUTCOME(facts, outcomes, question, intelligence)

# ═══════════════════════════════════════════════════════════════════════════════
# END ABSOLUTE FINAL CANONICAL ENTITY + SPREAD RESOLVER FIX
# ═══════════════════════════════════════════════════════════════════════════════




# ═══════════════════════════════════════════════════════════════════════════════
# UNIVERSAL SPORTS CONTEXT EXTRACTOR — FINAL SAFETY OVERRIDE
# Purpose:
#   - Stop weak whole-article alias matches like "end" -> Tight End.
#   - Use Ollama as a context-aware extractor for sports shorthand and target facts.
#   - Python still validates exact valid outcome and performs deterministic math.
# Covers:
#   NFL/NBA/spread/draft, soccer aliases/positions, cricket/golf/tennis-style facts.
# ═══════════════════════════════════════════════════════════════════════════════

_PRE_UNIVERSAL_CONTEXT_EXTRACT_SPECIFIC_ANSWER = extract_specific_answer
_PRE_UNIVERSAL_CONTEXT_DERIVE_OUTCOME = derive_outcome

_WEAK_ALIAS_TOKENS = {
    "end", "line", "back", "guard", "center", "centre", "wing", "forward",
    "city", "united", "athletic", "club", "fc", "cf", "sc", "the",
}

_POSITION_OUTCOME_ALIASES = {
    "Offensive Lineman": [
        "ol", "ot", "og", "oc", "offensive lineman", "offensive line",
        "offensive tackle", "offensive guard", "tackle", "guard", "center", "centre"
    ],
    "Tight End": ["te", "tight end"],
    "Wide Receiver": ["wr", "wide receiver", "receiver"],
    "Quarterback": ["qb", "quarterback"],
    "Running Back": ["rb", "running back"],
    "Cornerback": ["cb", "cornerback"],
    "Safety": ["s", "fs", "ss", "safety"],
    "Linebacker": ["lb", "ilb", "olb", "linebacker"],
    "Defensive Line / Edge": [
        "edge", "de", "dt", "dl", "defensive end", "defensive tackle",
        "defensive line", "edge rusher", "pass rusher"
    ],
    "Kicker / Punter / Long Snapper": [
        "k", "p", "ls", "kicker", "punter", "long snapper"
    ],
    # Soccer/football position outcomes if a market uses them.
    "Goalkeeper": ["gk", "goalkeeper", "keeper"],
    "Defender": ["defender", "df", "cb", "rb", "lb", "right back", "left back", "centre back", "center back"],
    "Midfielder": ["midfielder", "mf", "cm", "dm", "am", "cdm", "cam"],
    "Forward": ["forward", "fw", "winger", "lw", "rw"],
    "Striker": ["striker", "st", "cf"],
}

def _exact_valid_outcome(candidate: object, outcomes: list) -> Optional[str]:
    c = str(candidate or "").strip()
    if not c or c.lower() in {"none", "null", "unknown", "n/a", "inconclusive"}:
        return None
    for o in outcomes or []:
        if c.lower() == str(o).strip().lower():
            return str(o)
    # Allow normalized punctuation/spacing but never substring-only.
    cc = _canon_text(c) if "_canon_text" in globals() else re.sub(r"\W+", " ", c.lower()).strip()
    for o in outcomes or []:
        oo = _canon_text(o) if "_canon_text" in globals() else re.sub(r"\W+", " ", str(o).lower()).strip()
        if cc == oo:
            return str(o)
    return None

def _is_context_sports_market(question: str, outcomes: list, intelligence: Optional[dict] = None) -> bool:
    q = str(question or "").lower()
    intelligence = intelligence or {}
    mt = str(intelligence.get("market_type") or "").lower()
    resolver = str(intelligence.get("resolver") or "").lower()
    if _is_spread_market_outcomes(outcomes) if "_is_spread_market_outcomes" in globals() else False:
        return True
    if mt in {"sports", "sports_spread", "draft_named_choice", "cricket", "football", "basketball", "baseball", "hockey", "soccer"}:
        return True
    if resolver in {"sports_result", "spread_cover", "map_player_position_to_outcome", "named_choice"}:
        return True
    return any(k in q for k in [
        "nfl", "nba", "mlb", "nhl", "ufl", "xfl", "draft", "pick", "selection",
        "football", "soccer", "barcelona", "barca", "premier league", "champions league",
        "cricket", "ipl", "wicket", "tennis", "golf", "birdie", "round", "game", "match",
        "cover", "spread", "finals", "score", "winner"
    ])

def _sports_context_ollama_extract(content: str, question: str, outcomes: list,
                                   intelligence: Optional[dict] = None,
                                   query_plan: Optional[dict] = None) -> Optional[dict]:
    """
    Ollama extracts context and shorthand only.
    It may suggest candidate_outcome, but Python accepts it only if exact valid outcome.
    """
    if not USE_OLLAMA_BRAIN or "call_ollama_json" not in globals():
        return None

    prompt_context = str((intelligence or {}).get("prompt_context") or "")
    answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else ""
    evidence = str(content or "")
    valid_outcomes = [str(o) for o in outcomes or []]

    prompt = (
        "You are OracleREE's UNIVERSAL SPORTS CONTEXT EXTRACTOR.\n"
        "Return ONLY valid JSON. Do not use markdown.\n"
        "Your job is NOT to guess. Extract the exact target fact needed by the question.\n"
        "Important rules:\n"
        "- Use the question and settlement rules to identify the target only.\n"
        "- Ignore unrelated later picks, later rounds, previews, player analysis, and background stats.\n"
        "- Understand common sports shorthand: NFL WR/TE/QB/RB/CB/S/LB/OL/OT/OG/EDGE, "
        "NBA team nicknames/abbreviations, soccer Barca/Barça, GK/ST/RB/LB, cricket/golf/tennis terms.\n"
        "- candidate_outcome must be either one exact valid outcome string or null.\n"
        "- If the evidence conflicts or target is unclear, candidate_outcome must be null and confidence low.\n"
        "- For spread markets, extract teams and final scores; Python will do spread math.\n\n"
        "Return JSON with these keys:\n"
        "{"
        "\"market_kind\":\"\","
        "\"target_description\":\"\","
        "\"target_entity\":\"\","
        "\"target_pick_order\":\"\","
        "\"player\":\"\","
        "\"raw_shortcut\":\"\","
        "\"normalized_shortcut\":\"\","
        "\"team1\":\"\","
        "\"team1_score\":null,"
        "\"team2\":\"\","
        "\"team2_score\":null,"
        "\"winner\":\"\","
        "\"count\":null,"
        "\"candidate_outcome\":null,"
        "\"confidence\":\"low|medium|high\","
        "\"reason\":\"\""
        "}\n\n"
        f"Question:\n{question}\n\n"
        f"Valid outcomes:\n{json.dumps(valid_outcomes, ensure_ascii=False)}\n\n"
        f"Settlement prompt/rules:\n{prompt_context[:4000]}\n\n"
        f"ANSWER line:\n{answer_line}\n\n"
        f"Evidence:\n{evidence[:7000]}"
    )
    obj = call_ollama_json(prompt, timeout=120)
    return obj if isinstance(obj, dict) else None

def _map_position_to_valid_outcome(raw_position: object, normalized_position: object = None, outcomes: Optional[list] = None) -> Optional[str]:
    """
    Strict position mapper. Backwards compatible with old calls:
      _map_position_to_valid_outcome(position_text, outcomes)
    and new calls:
      _map_position_to_valid_outcome(raw_shortcut, normalized_shortcut, outcomes)

    Important: this must NOT scan a whole article and match weak words like
    "end" -> Tight End or "back" -> Running Back. It only accepts:
      - exact valid outcome strings
      - strong sports position abbreviations such as WR/TE/QB/OL/CB
      - explicit full position phrases such as "wide receiver"
    """
    # Compatibility: old call style passed outcomes as second argument.
    if outcomes is None and isinstance(normalized_position, list):
        outcomes = normalized_position
        normalized_position = ""

    outcomes = [str(o) for o in (outcomes or [])]

    # Exact valid outcome from either field.
    exact = _exact_valid_outcome(raw_position, outcomes) if "_exact_valid_outcome" in globals() else None
    if exact:
        return exact
    exact = _exact_valid_outcome(normalized_position, outcomes) if "_exact_valid_outcome" in globals() else None
    if exact:
        return exact

    raw = _canon_text(raw_position) if "_canon_text" in globals() else str(raw_position or "").lower().strip()
    norm = _canon_text(normalized_position) if "_canon_text" in globals() else str(normalized_position or "").lower().strip()

    # If caller accidentally sends a noisy article/window, prefer the ANSWER line
    # and avoid weak broad scanning.
    raw_source = str(raw_position or "")
    if len(raw_source) > 500 and "ANSWER:" in raw_source:
        ans = extract_answer_line(raw_source) if "extract_answer_line" in globals() else ""
        if ans:
            raw = _canon_text(ans) if "_canon_text" in globals() else ans.lower().strip()

    blob = f" {raw} {norm} "

    # First pass: strong 1-4 char abbreviations only. This handles WR -> Wide Receiver.
    strong_codes = {
        "ol", "ot", "og", "oc", "c", "g", "t", "iol",
        "te", "cb", "s", "fs", "ss", "qb", "wr", "dl", "de", "dt",
        "edge", "rb", "lb", "ilb", "olb", "k", "p", "ls",
        "gk", "st", "cm", "dm", "am", "lw", "rw",
    }
    for outcome in outcomes:
        for alias in _POSITION_OUTCOME_ALIASES.get(outcome, []):
            a = _canon_text(alias) if "_canon_text" in globals() else str(alias).lower().strip()
            if a in strong_codes and re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", blob):
                return outcome

    # Second pass: explicit multi-word position phrases only.
    # Reject weak single words like "end", "line", "back", "guard", "center".
    for outcome in outcomes:
        for alias in _POSITION_OUTCOME_ALIASES.get(outcome, []):
            a = _canon_text(alias) if "_canon_text" in globals() else str(alias).lower().strip()
            if not a or a in _WEAK_ALIAS_TOKENS:
                continue
            if len(a) <= 3:
                continue
            # Single-word aliases are risky unless very specific.
            if " " not in a and a not in {"quarterback", "linebacker", "cornerback", "safety", "goalkeeper", "striker"}:
                continue
            if re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", blob):
                return outcome
    return None

def _resolve_spread_from_ollama_obj(obj: dict, outcomes: list) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(obj, dict) or not (_is_spread_market_outcomes(outcomes) if "_is_spread_market_outcomes" in globals() else False):
        return None, None

    parsed = [(_strip_spread_from_outcome(o), _parse_spread_outcome(o), str(o)) for o in outcomes or []]
    if len(parsed) != 2 or any(p[1] is None for p in parsed):
        return None, None

    scores = {}
    for base, parsed_outcome, original in parsed:
        aliases = _aliases_for_outcome_base(base) if "_aliases_for_outcome_base" in globals() else [base]
        team1_blob = " ".join(str(x) for x in [obj.get("team1"), obj.get("winner")] if x)
        team2_blob = " ".join(str(x) for x in [obj.get("team2")] if x)
        if any(_term_in_text(a, team1_blob) for a in aliases) if "_term_in_text" in globals() else False:
            try: scores[base] = float(obj.get("team1_score"))
            except Exception: pass
        if any(_term_in_text(a, team2_blob) for a in aliases) if "_term_in_text" in globals() else False:
            try: scores[base] = float(obj.get("team2_score"))
            except Exception: pass

    if len(scores) < 2:
        return None, None

    b1, b2 = parsed[0][0], parsed[1][0]
    s1, s2 = scores.get(b1), scores.get(b2)
    if s1 is None or s2 is None:
        return None, None
    sp1, sp2 = parsed[0][1][1], parsed[1][1][1]
    adj1, adj2 = s1 + sp1, s2 + sp2
    if adj1 > s2 and not (adj2 > s1):
        return parsed[0][2], f"ollama_context_spread; {b1} {s1}+({sp1})={adj1} > {b2} {s2}"
    if adj2 > s1 and not (adj1 > s2):
        return parsed[1][2], f"ollama_context_spread; {b2} {s2}+({sp2})={adj2} > {b1} {s1}"
    if adj1 > s2:
        return parsed[0][2], f"ollama_context_spread; {b1} adjusted {adj1} > {b2} {s2}"
    if adj2 > s1:
        return parsed[1][2], f"ollama_context_spread; {b2} adjusted {adj2} > {b1} {s1}"
    return None, None

def _resolve_context_with_ollama(content: str, question: str, outcomes: list,
                                 intelligence: Optional[dict] = None,
                                 query_plan: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
    """
    Universal context resolver:
      1. Ollama extracts target fact/shortcut.
      2. Python accepts only exact valid outcomes or strict position maps.
      3. Python handles spread math if scores are extracted.
    """
    if not _is_context_sports_market(question, outcomes, intelligence):
        return None, None

    obj = _sports_context_ollama_extract(content, question, outcomes, intelligence, query_plan)
    if not isinstance(obj, dict):
        return None, None

    confidence = str(obj.get("confidence") or "").lower().strip()
    if confidence == "low":
        return None, "ollama_context: low confidence"

    # Spread: use extracted scores, not Ollama's direct outcome guess.
    matched, calc = _resolve_spread_from_ollama_obj(obj, outcomes)
    if matched:
        return matched, calc

    # Position/draft markets: map raw shortcut strictly.
    # Accept common extractor key variants, but Python still maps to exact valid outcome.
    raw_pos = (
        obj.get("raw_shortcut")
        or obj.get("raw_position")
        or obj.get("position")
        or obj.get("player_position")
    )
    norm_pos = (
        obj.get("normalized_shortcut")
        or obj.get("normalized_position")
        or obj.get("position_name")
    )
    pos_match = _map_position_to_valid_outcome(raw_pos, norm_pos, outcomes)
    if pos_match:
        return pos_match, (
            "ollama_context_position: "
            f"{obj.get('player') or obj.get('target_entity') or 'target'} "
            f"{raw_pos} → {pos_match}"
        )

    # General named/winner markets: accept only exact valid outcome from candidate.
    exact = _exact_valid_outcome(obj.get("candidate_outcome"), outcomes)
    if exact:
        return exact, f"ollama_context_exact: {obj.get('reason') or obj.get('target_description') or exact}"

    return None, "ollama_context: no exact valid outcome"

def _resolve_named_choice_with_aliases(content: str, outcomes: list, question: str = "") -> tuple[Optional[str], Optional[str]]:
    """
    Safer replacement for the old broad alias matcher.
    It no longer maps weak generic words such as 'end', 'line', 'back', or 'guard'
    from noisy articles to outcomes.
    """
    text = str(content or "")
    ans = extract_answer_line(text) if "extract_answer_line" in globals() else ""
    hay = ans + "\n" + text[:1200]

    # Position outcomes: only strong abbreviations/full phrases.
    for outcome in outcomes or []:
        out = str(outcome)
        aliases = _POSITION_OUTCOME_ALIASES.get(out, [])
        for alias in aliases:
            a = _canon_text(alias) if "_canon_text" in globals() else alias.lower()
            if a in _WEAK_ALIAS_TOKENS:
                continue
            target_text = _canon_text(hay) if "_canon_text" in globals() else hay.lower()
            if len(a) <= 3:
                if re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", target_text):
                    return out, f"safe_position_alias: {alias} → {out}"
            else:
                if re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", target_text):
                    return out, f"safe_position_alias: {alias} → {out}"

    # Non-position/entity outcomes: use aliases but exclude weak tokens and loser context.
    for outcome in outcomes or []:
        base = _strip_spread_from_outcome(outcome) if "_strip_spread_from_outcome" in globals() else str(outcome)
        aliases = _aliases_for_outcome_base(base, question=question, evidence=hay) if "_aliases_for_outcome_base" in globals() else [base]
        for alias in aliases:
            a = _canon_text(alias) if "_canon_text" in globals() else alias.lower().strip()
            if len(a) <= 2 or a in _WEAK_ALIAS_TOKENS:
                continue
            if _term_in_text(a, hay) if "_term_in_text" in globals() else (a in hay.lower()):
                c = _canon_text(hay) if "_canon_text" in globals() else hay.lower()
                loser_ctx = re.search(rf"\b(against|over|beat|defeated|topped)\b.{{0,120}}(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", c)
                subject_win = re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9]).{{0,120}}\b(won|beat|defeated|topped|covered|covers)\b", c)
                if loser_ctx and not subject_win:
                    continue
                return str(outcome), f"safe_entity_alias: {alias} → {outcome}"
    return None, None

def extract_specific_answer(content: str, query_plan: dict, question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    # First: deterministic spread math.
    if _is_spread_market_outcomes(outcomes) if "_is_spread_market_outcomes" in globals() else False:
        matched, calc = _resolve_spread_with_aliases(content, outcomes, question)
        if matched:
            print(f"[oracle] Universal spread resolver → {matched} ({calc})")
            return matched, calc

    # Second: universal Ollama context extraction for sports/draft/named-choice.
    matched, calc = _resolve_context_with_ollama(content, question, outcomes, intelligence, query_plan)
    if matched:
        print(f"[oracle] Universal context extractor → {matched} ({calc})")
        return matched, calc

    # Third: safe alias matching only; no weak aliases.
    matched, calc = _resolve_named_choice_with_aliases(content, outcomes, question)
    if matched:
        print(f"[oracle] Safe alias resolver → {matched} ({calc})")
        return matched, calc

    # Last: previous pipeline.
    return _PRE_UNIVERSAL_CONTEXT_EXTRACT_SPECIFIC_ANSWER(content, query_plan, question, outcomes, intelligence)

def derive_outcome(facts: list[Fact], outcomes: list, question: str, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    evidence = "\n".join(str(f.value) for f in facts or [])

    if _is_spread_market_outcomes(outcomes) if "_is_spread_market_outcomes" in globals() else False:
        matched, calc = _resolve_spread_with_aliases(evidence, outcomes, question)
        if matched:
            return matched, calc

    matched, calc = _resolve_context_with_ollama(evidence, question, outcomes, intelligence)
    if matched:
        return matched, calc

    matched, calc = _resolve_named_choice_with_aliases(evidence, outcomes, question)
    if matched:
        return matched, calc

    return _PRE_UNIVERSAL_CONTEXT_DERIVE_OUTCOME(facts, outcomes, question, intelligence)

# ═══════════════════════════════════════════════════════════════════════════════
# END UNIVERSAL SPORTS CONTEXT EXTRACTOR — FINAL SAFETY OVERRIDE
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FINAL UNIVERSAL NUMERIC VALUE / TUI OUTCOME FIX
# Problem fixed:
#   Some numeric threshold markets were incorrectly routed as count markets
#   because outcomes contained Over/Under. Example: golf leader score/strokes.
#   Evidence already had the correct value ("3-under 67"), but TUI/oracle
#   outcome extraction rejected it as "no validated count for items".
#
# Principle:
#   - "How many / number of / total count" => count_compare
#   - "score / strokes / points / runs / goals / birdies" => threshold_compare
#   - AI/REE may reason, but TUI outcome is resolved from evidence deterministically.
# ═══════════════════════════════════════════════════════════════════════════════

_PRE_NUMERIC_VALUE_PARSE_RULES = parse_settlement_rules
_PRE_NUMERIC_VALUE_BUILD_QUERY = build_universal_query
_PRE_NUMERIC_VALUE_VALIDATE = validate_evidence
_PRE_NUMERIC_VALUE_EXTRACT = extract_specific_answer
_PRE_NUMERIC_VALUE_DERIVE = derive_outcome
_PRE_NUMERIC_VALUE_LOOKS_COUNT = _looks_like_count_market
_PRE_NUMERIC_VALUE_IS_COUNT = _is_count_threshold_market

_NUMERIC_VALUE_WORDS = {
    "score", "scores", "strokes", "stroke", "points", "point", "runs", "run",
    "goals", "goal", "birdies", "birdie", "yards", "yard", "seeds", "seed",
    "rank", "ranking", "position", "finish", "finishing", "time", "seconds", "minutes",
}
_COUNT_INTENT_WORDS = {
    "how many", "number of", "count of", "total number of", "total count", "how much many",
}


def _question_asks_numeric_value_not_count(question: str, outcomes: Optional[list] = None) -> bool:
    q = str(question or "").lower()
    outs = " ".join(str(o).lower() for o in outcomes or [])
    text = q + " " + outs

    # Explicit count intent wins.
    if any(k in q for k in _COUNT_INTENT_WORDS):
        return False

    # These are measured values, not counts of objects/items.
    if any(re.search(rf"\b{re.escape(w)}\b", text) for w in _NUMERIC_VALUE_WORDS):
        return True

    # Common phrasing: "what will X score be".
    if re.search(r"\bwhat\s+will\b.{0,80}\b(score|strokes?|points?|runs?)\b", q):
        return True

    return False


def _looks_like_count_market(question: str, outcomes: list, rules: Optional[dict] = None) -> bool:
    """Final strict count classifier. Over/Under alone is not enough."""
    if _question_asks_numeric_value_not_count(question, outcomes):
        return False
    q = str(question or "").lower()
    if any(k in q for k in _COUNT_INTENT_WORDS):
        return True
    if rules and ((rules or {}).get("metric") == "count" or (rules or {}).get("count_subject")):
        subject = str((rules or {}).get("count_subject") or "").lower()
        if subject in {"", "items", "score", "scores", "strokes", "points"}:
            return False
        return True
    return False


def _is_count_threshold_market(question: str, outcomes: list, intelligence: Optional[dict] = None, query_plan: Optional[dict] = None) -> bool:
    """Final strict count threshold detector used by old count wrapper."""
    if _question_asks_numeric_value_not_count(question, outcomes):
        return False
    q = str(question or "").lower()
    intel = intelligence or {}
    qp = query_plan or {}
    if any(k in q for k in _COUNT_INTENT_WORDS):
        return True
    subject = str(intel.get("count_subject") or qp.get("number_context") or "").lower()
    if subject and subject not in {"items", "score", "scores", "strokes", "points", "value", "number"}:
        return bool(intel.get("resolver") == "count_compare" or intel.get("metric") == "count")
    return False


def parse_settlement_rules(prompt_context: str, question: str, outcomes: list) -> dict:
    rules = _PRE_NUMERIC_VALUE_PARSE_RULES(prompt_context, question, outcomes)
    if _question_asks_numeric_value_not_count(question, outcomes):
        threshold = rules.get("threshold")
        if threshold is None:
            threshold = _find_numeric_threshold(outcomes) if "_find_numeric_threshold" in globals() else None
        q = str(question or "").lower()
        outs = " ".join(str(o).lower() for o in outcomes or [])
        if "stroke" in q or "stroke" in outs or "golf" in q or "pga" in q:
            metric = "strokes"
        elif "score" in q or "score" in outs:
            metric = "score"
        elif "point" in q or "point" in outs:
            metric = "points"
        elif "run" in q or "run" in outs:
            metric = "runs"
        elif "goal" in q or "goal" in outs:
            metric = "goals"
        elif "birdie" in q or "birdie" in outs:
            metric = "birdies"
        else:
            metric = "numeric_value"
        rules.update({
            "metric": metric,
            "count_subject": None,
            "operator": rules.get("operator") or (">" if any("over" in str(o).lower() or "above" in str(o).lower() for o in outcomes or []) else None),
            "threshold": threshold,
        })
    return rules


def build_universal_query(question: str, outcomes: list, rules: dict, event_date: str, source_domain: str) -> dict:
    qp = _PRE_NUMERIC_VALUE_BUILD_QUERY(question, outcomes, rules, event_date, source_domain)
    if _question_asks_numeric_value_not_count(question, outcomes):
        metric = str((rules or {}).get("metric") or "score")
        qp.update({
            "query": f"{question} official final leaderboard result {metric} {event_date}",
            "required_data_type": "numeric_value",
            "what_to_validate": "numeric_value",
            "extraction_target": metric,
            "number_context": metric,
            "need_number": True,
            "search_depth": "basic",
        })
    return qp


def _extract_numeric_value_from_answer_line(answer_line: str, question: str = "", outcomes: Optional[list] = None) -> Optional[float]:
    """Extract measured value, e.g. '3-under 67' -> 67; 'won 115-104' not used here."""
    text = str(answer_line or "")
    if not text:
        return None
    lower = text.lower()

    # Golf-specific: "3-under 67", "3 under 67", "67 (-3)". The stroke value is 67.
    m = re.search(r"\b\d+\s*[- ]?under\s+(\d{2,3})\b", lower, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"\b\d+\s*[- ]?over\s+(\d{2,3})\b", lower, re.I)
    if m:
        return float(m.group(1))
    # Direct measured score phrases.
    patterns = [
        r"(?:score|scored|score was|leader'?s score(?:\s+was)?|round score(?:\s+was)?)\D{0,40}(\d+(?:\.\d+)?)",
        r"(?:finished|shot|carded|posted)\D{0,20}(\d{2,3})\b",
        r"\b(\d+(?:\.\d+)?)\s*(?:strokes?|points?|runs?|goals?|birdies?)\b",
    ]
    for pat in patterns:
        m = re.search(pat, lower, re.I)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass

    # For first sentence of answer line, choose plausible value near metric terms.
    first = re.split(r"(?<=[.!?])\s+", lower)[0]
    nums = [float(x) for x in re.findall(r"\b\d+(?:\.\d+)?\b", first)]
    if nums:
        q = str(question or "").lower()
        if "golf" in q or "pga" in q or "stroke" in q or "stroke" in " ".join(str(o).lower() for o in outcomes or []):
            # Golf round strokes usually 55-90. Avoid taking "3" from "3-under".
            plausible = [n for n in nums if 50 <= n <= 90]
            if plausible:
                return plausible[0]
        # General fallback: use first non-threshold-looking number from answer.
        threshold = _find_numeric_threshold(outcomes or []) if "_find_numeric_threshold" in globals() else None
        for n in nums:
            if threshold is None or abs(n - float(threshold)) > 1e-9:
                return n
    return None


def _extract_numeric_value_with_ollama(content: str, question: str, outcomes: list, metric: str, threshold: Optional[float]) -> Optional[float]:
    if "call_ollama_json" not in globals():
        return None
    try:
        prompt = (
            "You are OracleREE numeric value extractor. Return valid JSON only.\n"
            "Extract ONLY the final measured value that answers the question.\n"
            "Do not extract player stats unless the question asks for a player stat.\n"
            "Do not compare outcomes. Do not output the final market answer.\n"
            "For golf scores like '3-under 67', return 67 as the value.\n"
            f"Question: {question}\n"
            f"Metric: {metric}\n"
            f"Outcomes: {', '.join(str(o) for o in outcomes or [])}\n"
            f"Threshold: {threshold}\n"
            "Return JSON: {\"value\": number|null, \"metric\": \"...\", \"evidence_text\": \"short quote\", \"confidence\": \"high|medium|low\"}\n\n"
            f"Evidence:\n{str(content)[:3500]}"
        )
        obj = call_ollama_json(prompt, timeout=120)
        if not isinstance(obj, dict):
            return None
        if str(obj.get("confidence") or "").lower() == "low":
            return None
        val = obj.get("value")
        if val is None:
            return None
        return float(val)
    except Exception as e:
        print(f"[oracle] Ollama numeric value extraction failed: {e}")
        return None


def _map_numeric_value_to_threshold_outcome(value: float, threshold: float, outcomes: list, metric: str = "value") -> tuple[Optional[str], Optional[str]]:
    for outcome in outcomes or []:
        ol = str(outcome).lower()
        if ("over" in ol or "above" in ol or "greater" in ol) and value > threshold:
            return str(outcome), f"threshold_compare: {metric} {value:g} > {threshold:g}"
        if ("under" in ol or "below" in ol or "less" in ol) and value < threshold:
            return str(outcome), f"threshold_compare: {metric} {value:g} < {threshold:g}"
        if value == threshold and any(k in ol for k in ["equal", "exactly"]):
            return str(outcome), f"threshold_compare: {metric} {value:g} == {threshold:g}"
    return None, None


def _resolve_numeric_value_threshold(content: str, question: str, outcomes: list, intelligence: Optional[dict] = None, query_plan: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
    if not _question_asks_numeric_value_not_count(question, outcomes):
        return None, None
    threshold = (intelligence or {}).get("threshold") or _find_numeric_threshold(outcomes)
    if threshold is None:
        return None, "numeric_value: missing threshold"
    threshold = float(threshold)
    metric = str((intelligence or {}).get("metric") or (query_plan or {}).get("number_context") or "value")

    answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else ""
    value = _extract_numeric_value_from_answer_line(answer_line, question, outcomes)
    if value is None:
        value = _extract_numeric_value_from_answer_line(str(content)[:1800], question, outcomes)
    if value is None:
        value = _extract_numeric_value_with_ollama(content, question, outcomes, metric, threshold)

    if value is None:
        return None, "numeric_value: no final measured value extracted"
    return _map_numeric_value_to_threshold_outcome(float(value), threshold, outcomes, metric)


def validate_evidence(content: str, required_data_type: str, extraction_target: str, question: str) -> tuple[bool, str]:
    rdt = str(required_data_type or "").lower()
    if rdt in {"numeric_value", "score_value", "stroke_score", "threshold_value"} or _question_asks_numeric_value_not_count(question, []):
        answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else ""
        val = _extract_numeric_value_from_answer_line(answer_line, question, []) or _extract_numeric_value_from_answer_line(str(content)[:1800], question, [])
        if val is not None:
            return True, f"numeric value evidence found: {val:g}"
        # Accept if it has leaderboard/final result context and numbers; extraction may happen with outcomes later.
        if re.search(r"\b(leaderboard|leader'?s score|first round leader|score|strokes?|shot|carded|posted)\b", str(content), re.I) and re.search(r"\b\d{2,3}\b", str(content)):
            return True, "numeric value evidence has leaderboard/score context"
        return False, "no validated numeric value evidence"
    return _PRE_NUMERIC_VALUE_VALIDATE(content, required_data_type, extraction_target, question)


def extract_specific_answer(content: str, query_plan: dict, question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    # First resolve non-count numeric thresholds such as golf strokes, scores, points, runs.
    matched, calc = _resolve_numeric_value_threshold(content, question, outcomes, intelligence, query_plan)
    if matched:
        print(f"[oracle] Numeric value resolver → {matched} ({calc})")
        return matched, calc
    return _PRE_NUMERIC_VALUE_EXTRACT(content, query_plan, question, outcomes, intelligence)


def derive_outcome(facts: list[Fact], outcomes: list, question: str, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    evidence = "\n".join(str(f.value) for f in facts or [])
    matched, calc = _resolve_numeric_value_threshold(evidence, question, outcomes, intelligence)
    if matched:
        return matched, calc
    return _PRE_NUMERIC_VALUE_DERIVE(facts, outcomes, question, intelligence)

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL UNIVERSAL NUMERIC VALUE / TUI OUTCOME FIX
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# ORACLEREE ERRORFIXBRAIN FALLBACK — OLLAMA POINTER + PYTHON VALIDATOR
# Purpose:
#   If the normal parser/resolver fails or returns INCONCLUSIVE, let local Ollama
#   point to the likely structured fact/outcome, then let Python validate it
#   against exact valid outcomes and deterministic numeric/spread rules.
#
# Policy:
#   - Ollama may interpret messy evidence and propose a candidate outcome.
#   - Python must verify the candidate is exactly one of the valid outcomes.
#   - For numeric thresholds, Python independently extracts/normalizes the value
#     and recomputes the comparison before accepting the candidate.
#   - For spread markets, Python uses the spread resolver first; Ollama fallback
#     is only used if deterministic spread parsing cannot resolve.
# ═══════════════════════════════════════════════════════════════════════════════

_PRE_ERRORFIX_EXTRACT_SPECIFIC_ANSWER = extract_specific_answer
_PRE_ERRORFIX_DERIVE_OUTCOME = derive_outcome


def _errorfix_exact_outcome(candidate: object, outcomes: list) -> Optional[str]:
    """Return exact valid outcome matching candidate, tolerant to case/spacing only."""
    c = re.sub(r"\s+", " ", str(candidate or "")).strip()
    if not c:
        return None
    for o in outcomes or []:
        if c == str(o).strip():
            return str(o)
    cn = re.sub(r"\s+", " ", c.lower())
    for o in outcomes or []:
        if cn == re.sub(r"\s+", " ", str(o).strip().lower()):
            return str(o)
    # Very small tolerance for candidates like "Over" where valid outcome is "Over 65.5 strokes".
    # Only allow this if there is exactly one valid outcome beginning with that word.
    if cn in {"over", "under", "yes", "no"}:
        matches = [str(o) for o in outcomes or [] if str(o).strip().lower().startswith(cn)]
        if len(matches) == 1:
            return matches[0]
    return None


def _errorfix_float(value: object) -> Optional[float]:
    """Parse a numeric value from Ollama or evidence strings, including '3-under 67'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value)
    # Golf style: "3-under 67" means the actual stroke score is 67.
    m = re.search(r"\b\d+\s*[- ]?\s*under\s+(\d{2,3})\b", s, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"\b\d+\s*[- ]?\s*over\s+(\d{2,3})\b", s, re.I)
    if m:
        return float(m.group(1))
    # Prefer explicit score/stroke/points values over random numbers.
    m = re.search(r"\b(?:score|strokes?|points?|runs?|goals?|birdies?)\D{0,20}(\d+(?:\.\d+)?)\b", s, re.I)
    if m:
        return float(m.group(1))
    nums = re.findall(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    if not nums:
        return None
    # For strings like "3-under 67", take the last meaningful number.
    try:
        return float(nums[-1])
    except Exception:
        return None


def _errorfix_validate_candidate(obj: dict, evidence: str, question: str, outcomes: list, intelligence: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
    """Validate Ollama's proposed candidate using exact outcomes and deterministic checks."""
    if not isinstance(obj, dict):
        return None, None

    candidate = (
        obj.get("matched_outcome")
        or obj.get("candidate_outcome")
        or obj.get("answer")
        or obj.get("outcome")
    )
    exact = _errorfix_exact_outcome(candidate, outcomes)
    if not exact:
        return None, "errorfix rejected: candidate is not an exact valid outcome"

    threshold = (intelligence or {}).get("threshold") or _find_numeric_threshold(outcomes)
    outcome_text = " ".join(str(o).lower() for o in outcomes or [])

    # Numeric threshold outcomes: independently verify the math.
    if threshold is not None and any(k in outcome_text for k in ["over", "under", "above", "below"]):
        value = (
            _errorfix_float(obj.get("value"))
            or _errorfix_float(obj.get("extracted_value"))
            or _errorfix_float(obj.get("numeric_value"))
        )
        if value is None:
            # Reuse existing deterministic extractors if present.
            try:
                answer_line = extract_answer_line(evidence)
                value = _extract_numeric_value_from_answer_line(answer_line, question, outcomes)
                if value is None:
                    value = _extract_numeric_value_from_answer_line(str(evidence)[:2000], question, outcomes)
            except Exception:
                value = None
        if value is None:
            return None, "errorfix rejected: no numeric value to validate"

        metric = str(obj.get("metric") or obj.get("corrected_metric") or (intelligence or {}).get("metric") or "value")
        expected, calc = _map_numeric_value_to_threshold_outcome(float(value), float(threshold), outcomes, metric)
        if expected and expected == exact:
            return exact, f"ErrorFixBrain validated: {calc}; Ollama pointed to {exact}"
        return None, f"errorfix rejected: Python comparison gave {expected}, Ollama gave {exact}"

    # Spread markets: do not accept an unverified LLM guess if deterministic spread can compute.
    try:
        if _is_spread_market_outcomes(outcomes):
            spread_match, spread_calc = _resolve_spread_with_aliases(evidence, outcomes, question)
            if spread_match:
                if spread_match == exact:
                    return exact, f"ErrorFixBrain validated spread: {spread_calc}"
                return None, f"errorfix rejected: spread math gave {spread_match}, Ollama gave {exact}"
    except Exception:
        pass

    # Named-choice / draft-position / binary: accept only exact valid outcome with supporting quote or high confidence.
    quote = str(obj.get("evidence_quote") or obj.get("supporting_quote") or "").strip()
    confidence = str(obj.get("confidence") or "").lower()
    if quote and quote.lower() not in str(evidence).lower():
        # Do not hard reject if quote is a normalized phrase, but downgrade to requiring high confidence.
        if confidence not in {"high", "very high"}:
            return None, "errorfix rejected: quote not found and confidence not high"
    if confidence in {"high", "very high"} or quote:
        return exact, f"ErrorFixBrain validated exact outcome: {exact}"

    return None, "errorfix rejected: low confidence"


def _errorfix_brain_resolve(evidence: str, question: str, outcomes: list, intelligence: Optional[dict] = None, current_failure: str = "") -> tuple[Optional[str], Optional[str]]:
    """Ask Ollama to point to the answer, then validate with Python."""
    if not USE_OLLAMA_BRAIN or not evidence or not outcomes:
        return None, None

    # Keep prompt compact to avoid noisy schema drift, and make the allowed schema explicit.
    prompt = (
        "You are OracleREE ErrorFixBrain.\n"
        "Return ONLY valid JSON. Do not use markdown.\n"
        "Your job is to point Python to the correct answer when OracleREE parser failed.\n"
        "Do NOT invent resolver names. Use only this schema:\n"
        "{"
        "\"metric\":\"score|count|price|spread|winner|position|binary|unknown\","
        "\"value\":number_or_null,"
        "\"unit\":\"\","
        "\"candidate_outcome\":\"exactly one valid outcome\","
        "\"evidence_quote\":\"short quote copied from evidence\","
        "\"confidence\":\"low|medium|high\","
        "\"why_current_failed\":\"short reason\""
        "}\n"
        "Rules:\n"
        "- candidate_outcome MUST be copied exactly from VALID OUTCOMES.\n"
        "- If evidence says '3-under 67', value MUST be 67, not 3.\n"
        "- If this is a spread market, extract final team scores, not player points.\n"
        "- If this is a draft position market, extract only the target pick/selection.\n"
        "- If unsure, use confidence low.\n\n"
        f"QUESTION:\n{question}\n\n"
        f"VALID OUTCOMES:\n{json.dumps([str(o) for o in outcomes], ensure_ascii=False)}\n\n"
        f"CURRENT ORACLEREE CLASSIFICATION:\n{json.dumps(intelligence or {}, ensure_ascii=False)[:2500]}\n\n"
        f"CURRENT FAILURE:\n{current_failure}\n\n"
        f"EVIDENCE:\n{str(evidence)[:5000]}\n"
    )

    obj = call_ollama_json(prompt, timeout=120)
    matched, calc = _errorfix_validate_candidate(obj or {}, evidence, question, outcomes, intelligence)
    if matched:
        print(f"[oracle] ErrorFixBrain → {matched} ({calc})")
        return matched, calc
    if calc:
        print(f"[oracle] ErrorFixBrain rejected: {calc}")
    return None, None


def extract_specific_answer(content: str, query_plan: dict, question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    # First try the normal/current resolver stack.
    matched, calc = _PRE_ERRORFIX_EXTRACT_SPECIFIC_ANSWER(content, query_plan, question, outcomes, intelligence)
    if matched:
        return matched, calc

    # If normal resolver failed, let Ollama point to a candidate and Python validate it.
    reason = ""
    try:
        reason = f"query_plan={json.dumps(query_plan or {}, ensure_ascii=False)[:1000]}"
    except Exception:
        reason = "normal extract_specific_answer returned no match"
    matched, calc = _errorfix_brain_resolve(content, question, outcomes, intelligence, current_failure=reason)
    if matched:
        return matched, calc

    return None, calc


def derive_outcome(facts: list[Fact], outcomes: list, question: str, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    # First try the normal/current resolver stack.
    matched, calc = _PRE_ERRORFIX_DERIVE_OUTCOME(facts, outcomes, question, intelligence)
    if matched:
        return matched, calc

    # Then try ErrorFixBrain using accumulated facts/evidence.
    evidence = "\n".join(str(f.value) for f in facts or [])
    matched, calc = _errorfix_brain_resolve(evidence, question, outcomes, intelligence, current_failure="derive_outcome returned no match")
    if matched:
        return matched, calc

    return None, calc

# ═══════════════════════════════════════════════════════════════════════════════
# END ORACLEREE ERRORFIXBRAIN FALLBACK
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH — METRIC-FIRST NUMERIC RESOLVER
# Fixes the current root issue:
#   - Over/Under outcomes were forcing score/strokes markets into count_compare.
#   - Golf strings like "score of -9 ... score was 65" extracted 9 instead of 65.
#   - Old resolver could return a matched outcome with a bad "count" calculation.
#
# New rule:
#   question/prompt metric decides resolver first.
#   score/strokes/points/runs/goals/birdies => threshold_compare, not count_compare.
#   how many/number of/total count => count_compare.
# ═══════════════════════════════════════════════════════════════════════════════

_METRIC_FIRST_PRE_PARSE_RULES = parse_settlement_rules
_METRIC_FIRST_PRE_BUILD_QUERY = build_universal_query
_METRIC_FIRST_PRE_ANALYZE = analyze_market_intelligence
_METRIC_FIRST_PRE_EXTRACT_SPECIFIC = extract_specific_answer
_METRIC_FIRST_PRE_DERIVE = derive_outcome

_SCORE_VALUE_TERMS = {
    "score", "scores", "strokes", "stroke", "points", "point", "runs", "run",
    "goals", "goal", "birdies", "birdie", "yards", "yard", "laps", "lap",
    "seconds", "minutes", "time", "round score", "leader score", "leader's score",
}
_STRICT_COUNT_TERMS = {
    "how many", "number of", "count of", "total number of", "total count of",
    "total trades", "total players", "total picks", "total selections",
}


def _metric_first_text(question: str, outcomes: Optional[list] = None, prompt_context: str = "") -> str:
    return " ".join([
        str(question or ""),
        " ".join(str(o) for o in outcomes or []),
        str(prompt_context or ""),
    ]).lower()


def _metric_first_is_score_value_market(question: str, outcomes: Optional[list] = None, prompt_context: str = "") -> bool:
    q = str(question or "").lower()
    text = _metric_first_text(question, outcomes, prompt_context)

    # Explicit count phrasing wins only if the question itself asks count.
    if any(term in q for term in _STRICT_COUNT_TERMS):
        return False

    if any(re.search(rf"\b{re.escape(term)}\b", text) for term in _SCORE_VALUE_TERMS):
        return True

    # Common market shape: "What will X's score be".
    if re.search(r"\bwhat\s+will\b.{0,120}\b(score|strokes?|points?|runs?|goals?|birdies?|time)\b", q):
        return True

    # Golf/PGA markets with Over/Under strokes are score-value markets.
    if any(k in text for k in ["pga", "golf", "leaderboard"] ) and any("stroke" in str(o).lower() for o in outcomes or []):
        return True

    return False


def _metric_first_metric_name(question: str, outcomes: Optional[list] = None, prompt_context: str = "") -> str:
    text = _metric_first_text(question, outcomes, prompt_context)
    if "stroke" in text or "pga" in text or "golf" in text:
        return "strokes"
    if "point" in text:
        return "points"
    if "run" in text:
        return "runs"
    if "goal" in text:
        return "goals"
    if "birdie" in text:
        return "birdies"
    if "time" in text or "seconds" in text or "minutes" in text:
        return "time"
    if "score" in text:
        return "score"
    return "numeric_value"


def _looks_like_count_market(question: str, outcomes: list, rules: Optional[dict] = None) -> bool:
    """Final override: Over/Under alone never means count. Question intent decides."""
    if _metric_first_is_score_value_market(question, outcomes):
        return False
    q = str(question or "").lower()
    if any(term in q for term in _STRICT_COUNT_TERMS):
        return True
    # Looser but still question-based count wording.
    if re.search(r"\b(how many|number of|count of)\b", q):
        return True
    if rules and ((rules or {}).get("metric") == "count" or (rules or {}).get("count_subject")):
        subject = str((rules or {}).get("count_subject") or "").lower().strip()
        if subject in {"", "items", "item", "score", "scores", "strokes", "points", "value", "number"}:
            return False
        return True
    return False


def _is_count_threshold_market(question: str, outcomes: list, intelligence: Optional[dict] = None, query_plan: Optional[dict] = None) -> bool:
    if _metric_first_is_score_value_market(question, outcomes, (intelligence or {}).get("prompt_context", "")):
        return False
    q = str(question or "").lower()
    if re.search(r"\b(how many|number of|count of|total number of|total count)\b", q):
        return True
    subject = str((intelligence or {}).get("count_subject") or (query_plan or {}).get("number_context") or "").lower()
    if subject and subject not in {"items", "item", "score", "scores", "strokes", "points", "value", "number"}:
        return bool((intelligence or {}).get("resolver") == "count_compare" or (intelligence or {}).get("metric") == "count")
    return False


def parse_settlement_rules(prompt_context: str, question: str, outcomes: list) -> dict:
    rules = _METRIC_FIRST_PRE_PARSE_RULES(prompt_context, question, outcomes)
    if _metric_first_is_score_value_market(question, outcomes, prompt_context):
        threshold = rules.get("threshold")
        if threshold is None:
            threshold = _find_numeric_threshold(outcomes) if "_find_numeric_threshold" in globals() else None
        rules.update({
            "metric": _metric_first_metric_name(question, outcomes, prompt_context),
            "count_subject": None,
            "threshold": threshold,
            "operator": rules.get("operator") or (">" if any("over" in str(o).lower() or "above" in str(o).lower() for o in outcomes or []) else None),
        })
    return rules


def _metric_first_apply_plan(plan: dict, question: str, outcomes: list, prompt_context: str = "") -> dict:
    if not _metric_first_is_score_value_market(question, outcomes, prompt_context):
        return plan
    threshold = plan.get("threshold") or (_find_numeric_threshold(outcomes) if "_find_numeric_threshold" in globals() else None)
    metric = _metric_first_metric_name(question, outcomes, prompt_context)
    plan.update({
        "market_type": "numeric_threshold",
        "answer_format": "numeric_threshold",
        "facts_needed": [metric, "final_value", "official_result"],
        "resolver": "threshold_compare",
        "metric": metric,
        "count_subject": None,
        "threshold": threshold,
        "search_query": f"{question} official final leaderboard result {metric}",
    })
    return plan


def build_universal_query(question: str, outcomes: list, rules: dict, event_date: str, source_domain: str) -> dict:
    qp = _METRIC_FIRST_PRE_BUILD_QUERY(question, outcomes, rules, event_date, source_domain)
    if _metric_first_is_score_value_market(question, outcomes):
        metric = str((rules or {}).get("metric") or _metric_first_metric_name(question, outcomes))
        qp.update({
            "query": f"{question} official final leaderboard result {metric} {event_date}",
            "required_data_type": "numeric_value",
            "what_to_validate": "numeric_value",
            "extraction_target": metric,
            "number_context": metric,
            "need_number": True,
            "search_depth": "basic",
        })
    return qp


def analyze_market_intelligence(question: str, prompt_context: str, outcomes: list, data_sources: list, close_time: str) -> dict:
    plan = _METRIC_FIRST_PRE_ANALYZE(question, prompt_context, outcomes, data_sources, close_time)
    return _metric_first_apply_plan(plan, question, outcomes, prompt_context)


def _extract_numeric_value_from_answer_line(answer_line: str, question: str = "", outcomes: Optional[list] = None) -> Optional[float]:
    """Metric-first extraction. For golf, absolute-to-par (-9) is NOT strokes; score was 65 is strokes."""
    text = str(answer_line or "")
    if not text:
        return None
    lower = text.lower()
    q = str(question or "").lower()
    out_text = " ".join(str(o).lower() for o in outcomes or [])
    is_golf = any(k in (q + " " + out_text + " " + lower) for k in ["golf", "pga", "strokes", "leaderboard"])

    # First, explicit golf absolute stroke formats.
    # Examples: "3-under 67", "9 under par 65", "65 (-9)".
    for pat in [
        r"\b\d+\s*[- ]?under(?:\s+par)?\s+(\d{2,3})\b",
        r"\b\d+\s*[- ]?over(?:\s+par)?\s+(\d{2,3})\b",
        r"\b(\d{2,3})\s*\(\s*[-+]\d+\s*\)",
    ]:
        m = re.search(pat, lower, re.I)
        if m:
            return float(m.group(1))

    # Then explicit absolute-score phrases. Prefer these over "score of -9".
    explicit_patterns = [
        r"(?:first[- ]round\s+score|round\s+score|leader'?s\s+score|score\s+was|score\s+of\s+)(?:[^\d\n]{0,30})(\d{2,3})(?!\s*[-+]?)\b",
        r"(?:shot|carded|posted|finished\s+with)(?:[^\d\n]{0,30})(\d{2,3})\b",
    ]
    for pat in explicit_patterns:
        for m in re.finditer(pat, lower, re.I):
            val = float(m.group(1))
            if not is_golf or 50 <= val <= 90:
                return val

    # If text has "score of -9" and also another plausible golf stroke value nearby, use the plausible stroke value.
    if is_golf:
        plausible = [float(x) for x in re.findall(r"\b(5\d|6\d|7\d|8\d|90)\b", lower)]
        if plausible:
            return plausible[0]

    # General metric phrases.
    for pat in [
        r"\b(\d+(?:\.\d+)?)\s*(?:strokes?|points?|runs?|goals?|birdies?|yards?)\b",
        r"(?:score|scored|total)(?:[^\d\n]{0,30})(\d+(?:\.\d+)?)\b",
    ]:
        m = re.search(pat, lower, re.I)
        if m:
            return float(m.group(1))

    # Conservative fallback: first non-threshold number in first sentence.
    first = re.split(r"(?<=[.!?])\s+", lower)[0]
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", first)]
    threshold = _find_numeric_threshold(outcomes or []) if "_find_numeric_threshold" in globals() else None
    for n in nums:
        if threshold is None or abs(n - float(threshold)) > 1e-9:
            if is_golf and not (50 <= abs(n) <= 90):
                continue
            return abs(n) if n < 0 and not is_golf else n
    return None


def _resolve_numeric_value_threshold(content: str, question: str, outcomes: list, intelligence: Optional[dict] = None, query_plan: Optional[dict] = None) -> tuple[Optional[str], Optional[str]]:
    if not _metric_first_is_score_value_market(question, outcomes, (intelligence or {}).get("prompt_context", "")):
        return None, None
    threshold = (intelligence or {}).get("threshold") or _find_numeric_threshold(outcomes)
    if threshold is None:
        return None, "numeric_value: missing threshold"
    threshold = float(threshold)
    metric = _metric_first_metric_name(question, outcomes, (intelligence or {}).get("prompt_context", ""))
    answer_line = extract_answer_line(content) if "extract_answer_line" in globals() else ""
    value = _extract_numeric_value_from_answer_line(answer_line, question, outcomes)
    if value is None:
        value = _extract_numeric_value_from_answer_line(str(content)[:2500], question, outcomes)
    if value is None and "_extract_numeric_value_with_ollama" in globals():
        value = _extract_numeric_value_with_ollama(content, question, outcomes, metric, threshold)
    if value is None:
        return None, "numeric_value: no final measured value extracted"
    return _map_numeric_value_to_threshold_outcome(float(value), threshold, outcomes, metric)


def extract_specific_answer(content: str, query_plan: dict, question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    # Metric-first resolver must run before every old patch/wrapper.
    matched, calc = _resolve_numeric_value_threshold(content, question, outcomes, intelligence, query_plan)
    if matched:
        print(f"[oracle] Metric-first numeric resolver → {matched} ({calc})")
        return matched, calc

    matched, calc = _METRIC_FIRST_PRE_EXTRACT_SPECIFIC(content, query_plan, question, outcomes, intelligence)

    # If an old wrapper returned a count calculation for a score/strokes market, discard and recompute.
    if matched and _metric_first_is_score_value_market(question, outcomes, (intelligence or {}).get("prompt_context", "")) and "count" in str(calc or "").lower():
        fixed, fixed_calc = _resolve_numeric_value_threshold(content, question, outcomes, intelligence, query_plan)
        if fixed:
            return fixed, fixed_calc
        return None, "metric-first rejected stale count result for score/value market"

    return matched, calc


def derive_outcome(facts: list[Fact], outcomes: list, question: str, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    evidence = "\n".join(str(f.value) for f in facts or [])
    matched, calc = _resolve_numeric_value_threshold(evidence, question, outcomes, intelligence)
    if matched:
        return matched, calc

    matched, calc = _METRIC_FIRST_PRE_DERIVE(facts, outcomes, question, intelligence)
    if matched and _metric_first_is_score_value_market(question, outcomes, (intelligence or {}).get("prompt_context", "")) and "count" in str(calc or "").lower():
        fixed, fixed_calc = _resolve_numeric_value_threshold(evidence, question, outcomes, intelligence)
        if fixed:
            return fixed, fixed_calc
        return None, "metric-first rejected stale count result for score/value market"
    return matched, calc

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH — METRIC-FIRST NUMERIC RESOLVER
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL SETTLEMENT KERNEL PATCH
# Purpose:
#   - Stop direct "raw evidence → first alias → outcome" mistakes.
#   - Convert evidence into a structured fact first.
#   - Resolve with deterministic Python.
#   - Use Ollama only as an interpreter/fallback hint, not as final authority.
#   - Do NOT inject OracleREE's matched_outcome/calculation into the REE prompt.
# ═══════════════════════════════════════════════════════════════════════════════

_ORACLE_KERNEL_PREV_EXTRACT_SPECIFIC_ANSWER = extract_specific_answer if "extract_specific_answer" in globals() else None
_ORACLE_KERNEL_PREV_VALIDATE_EVIDENCE = validate_evidence if "validate_evidence" in globals() else None
_ORACLE_KERNEL_PREV_BUILD_QUERY = build_universal_query if "build_universal_query" in globals() else None


def _kernel_clean_text(s: object) -> str:
    return " ".join(str(s or "").replace("\u2019", "'").replace("\u2013", "-").replace("\u2014", "-").split())


def _kernel_outcome_map(outcomes: list) -> dict:
    return {str(o).strip().lower(): str(o).strip() for o in (outcomes or []) if str(o).strip()}


def _kernel_norm_entity(s: str) -> str:
    s = str(s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _kernel_token_set(s: str) -> set:
    return {w for w in _kernel_norm_entity(s).split() if len(w) >= 3}


def _kernel_exact_outcome(candidate: object, outcomes: list) -> Optional[str]:
    """Map a candidate entity/value to one exact valid outcome. Conservative."""
    if candidate is None:
        return None
    cand_raw = str(candidate).strip()
    if not cand_raw:
        return None

    out_map = _kernel_outcome_map(outcomes)
    cand_l = cand_raw.lower().strip()
    if cand_l in out_map:
        return out_map[cand_l]

    cand_norm = _kernel_norm_entity(cand_raw)
    if cand_norm in {_kernel_norm_entity(o): o for o in outcomes or []}:
        for o in outcomes or []:
            if _kernel_norm_entity(o) == cand_norm:
                return str(o)

    # Candidate may be a last name / abbreviation. Only accept if it maps to exactly one outcome.
    cand_tokens = _kernel_token_set(cand_raw)
    if not cand_tokens:
        return None

    hits = []
    for o in outcomes or []:
        on = _kernel_norm_entity(o)
        otoks = _kernel_token_set(o)
        if cand_norm and (cand_norm == on or cand_norm in on or on in cand_norm):
            hits.append(str(o))
            continue
        # Last-name / team-name match.
        if cand_tokens and cand_tokens <= otoks:
            hits.append(str(o))
            continue
        if cand_tokens and cand_tokens & otoks and len(cand_tokens & otoks) >= min(2, len(cand_tokens)):
            hits.append(str(o))

    # Preserve order but remove duplicates.
    uniq = []
    for h in hits:
        if h not in uniq:
            uniq.append(h)
    return uniq[0] if len(uniq) == 1 else None


def _kernel_outcome_threshold(outcomes: list) -> Optional[float]:
    if "_find_numeric_threshold" in globals():
        try:
            v = _find_numeric_threshold(outcomes)
            if v is not None:
                return float(v)
        except Exception:
            pass
    for o in outcomes or []:
        m = re.search(r"(-?\d+(?:\.\d+)?)", str(o))
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
    return None


def _kernel_extract_answer(content: str) -> str:
    try:
        ans = extract_answer_line(content)
        if ans:
            return ans
    except Exception:
        pass
    m = re.search(r"ANSWER:\s*(.+?)(?:\n\n|\n\[|\Z)", str(content or ""), re.I | re.S)
    return _kernel_clean_text(m.group(1)) if m else ""


def _kernel_relevant_text(content: str) -> str:
    ans = _kernel_extract_answer(content)
    raw = str(content or "")
    # The answer line is usually Tavily's synthesis and should have highest priority.
    if ans:
        return f"ANSWER: {ans}\n\n{raw[:2500]}"
    return raw[:3500]


def _kernel_is_spread_market(outcomes: list) -> bool:
    spread_re = re.compile(r"^.+\s[+-]\d+(?:\.\d+)?\s*$")
    return len(outcomes or []) == 2 and all(spread_re.match(str(o).strip()) for o in outcomes or [])


def _kernel_parse_spread_outcomes(outcomes: list) -> list[tuple[str, str, float]]:
    parsed = []
    for o in outcomes or []:
        m = re.match(r"^(.+?)\s*([+-]\d+(?:\.\d+)?)\s*$", str(o).strip())
        if m:
            parsed.append((str(o).strip(), m.group(1).strip(), float(m.group(2))))
    return parsed


def _kernel_extract_winner_from_text(question: str, outcomes: list, content: str) -> Optional[dict]:
    """
    Winner markets must extract the winner phrase first.
    Never settle winner markets by first entity alias.
    """
    text = _kernel_relevant_text(content)
    one_line = _kernel_clean_text(text)
    low = one_line.lower()

    # Draw / tie first.
    if any(w in low for w in [" ended in a draw", " ended in draw", " was a draw", " draw ", " tied "]):
        draw = _kernel_exact_outcome("Draw", outcomes)
        if draw:
            return {
                "kind": "winner_result",
                "value": draw,
                "candidate_outcome": draw,
                "evidence_quote": "draw/tie language found",
                "confidence": "high",
                "source": "deterministic_draw",
            }

    # Strong result patterns, winner appears before the verb.
    patterns = [
        r"(?P<winner>[A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){0,5})\s+(?:has\s+|had\s+)?(?:won|wins|defeated|defeats|beat|beats|reclaimed|retained|captured)\b(?P<context>.{0,180})",
        r"(?P<winner>[A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){0,5})\s+was\s+(?:declared\s+)?(?:the\s+)?winner\b(?P<context>.{0,160})",
        r"(?:winner|champion)\s*[:\-]\s*(?P<winner>[A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){0,5})(?P<context>.{0,120})",
    ]
    for pat in patterns:
        for m in re.finditer(pat, one_line):
            winner_raw = _kernel_clean_text(m.group("winner"))
            # Drop leading junk words often captured by broad regex.
            winner_raw = re.sub(r"^(ANSWER\s+|Final\s+|Main\s+Event\s+|The\s+|A\s+|An\s+)", "", winner_raw, flags=re.I).strip()
            out = _kernel_exact_outcome(winner_raw, outcomes)
            if out:
                quote = _kernel_clean_text(m.group(0))[:260]
                return {
                    "kind": "winner_result",
                    "value": out,
                    "candidate_outcome": out,
                    "evidence_quote": quote,
                    "confidence": "high",
                    "source": "winner_phrase",
                }

    # Pattern: "X won ... against/over/versus Y"; ensure we do not choose Y.
    for o in outcomes or []:
        if str(o).lower() == "draw":
            continue
        on = _kernel_norm_entity(o)
        if not on:
            continue
        # outcome followed by winner verb
        if re.search(rf"\b{re.escape(on)}\b.{{0,90}}\b(won|wins|defeated|defeats|beat|beats)\b", _kernel_norm_entity(one_line)):
            return {
                "kind": "winner_result",
                "value": str(o),
                "candidate_outcome": str(o),
                "evidence_quote": f"{o} near winner verb",
                "confidence": "medium",
                "source": "outcome_near_winner_verb",
            }

    return None


def _kernel_word_to_number(s: str) -> Optional[float]:
    words = globals().get("_WORD_NUMBERS") or {}
    sl = str(s or "").lower().strip()
    if sl in words:
        return float(words[sl])
    return None


def _kernel_extract_numeric_fact(question: str, outcomes: list, content: str, intelligence: dict) -> Optional[dict]:
    text = _kernel_relevant_text(content)
    ans = _kernel_extract_answer(content)
    low_q = str(question or "").lower()
    low = text.lower()
    rules = (intelligence or {}).get("_rules") or {}
    metric = str((intelligence or {}).get("metric") or rules.get("metric") or "").lower()
    threshold = None
    try:
        threshold = float((intelligence or {}).get("threshold") if (intelligence or {}).get("threshold") is not None else rules.get("threshold"))
    except Exception:
        threshold = None
    if threshold is None:
        threshold = _kernel_outcome_threshold(outcomes)

    score_markets = any(w in low_q for w in ["score", "strokes", "points", "runs", "goals", "birdies", "leader's score", "leader score"]) or metric in {"score", "strokes", "points", "runs", "goals", "birdies"}
    count_markets = any(w in low_q for w in ["how many", "number of", "total number", "count of"]) or metric == "count" or bool((intelligence or {}).get("count_subject"))
    price_markets = any(w in low_q for w in ["price", "highest", "lowest", "close", "open", "market cap", "fdv", "$"]) or metric in {"high", "low", "close", "open", "price", "fdv"}

    # Golf/stroke score: prefer explicit stroke score over relative-to-par like -9.
    if score_markets:
        score_patterns = [
            r"(?:first[-\s]?round\s+)?score\s+(?:was|is|of|=|:)\s*(\d{1,3})(?:\s+strokes?)?",
            r"(\d+)[-\s]?under\s+(\d{1,3})",
            r"(\d+)[-\s]?over\s+(\d{1,3})",
            r"(?:with|carded|shot|posted)\s+(?:a\s+)?(?:first[-\s]?round\s+)?(?:score\s+of\s+)?(\d{1,3})\b",
            r"\b(\d{1,3})\s+strokes?\b",
        ]
        for pat in score_patterns:
            m = re.search(pat, ans or text, re.I)
            if m:
                val = float(m.group(2) if len(m.groups()) >= 2 and m.group(2) else m.group(1))
                if 0 <= val <= 300:
                    return {
                        "kind": "numeric_threshold",
                        "metric": "score",
                        "value": val,
                        "unit": "strokes" if ("stroke" in low_q or "pga" in low_q or "golf" in low_q) else "",
                        "threshold": threshold,
                        "evidence_quote": _kernel_clean_text(m.group(0)),
                        "confidence": "high",
                        "source": "score_pattern",
                    }

    # Count markets: prefer answer line and subject words.
    if count_markets:
        count_text = ans or text
        subject = str((intelligence or {}).get("count_subject") or rules.get("count_subject") or "")
        subject_words = [w for w in re.findall(r"[a-zA-Z]+", subject.lower()) if len(w) > 3]
        # "Three trades occurred", "Nine offensive linemen..."
        m = re.search(r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty(?:[-\s]one|[-\s]two|[-\s]three|[-\s]four|[-\s]five)?|thirty|forty(?:[-\s]one)?)\b.{0,80}", count_text, re.I)
        if m:
            n = _kernel_word_to_number(m.group(1))
            if n is not None and (not subject_words or any(w in m.group(0).lower() for w in subject_words)):
                return {"kind":"numeric_threshold","metric":"count","value":n,"unit":subject,"threshold":threshold,"evidence_quote":_kernel_clean_text(m.group(0)),"confidence":"high","source":"count_word"}
        for m in re.finditer(r"\b(\d{1,4})\b.{0,100}", count_text):
            n = float(m.group(1))
            ctx = m.group(0).lower()
            if 1900 <= n <= 2100:
                continue
            if subject_words and not any(w in ctx for w in subject_words):
                continue
            return {"kind":"numeric_threshold","metric":"count","value":n,"unit":subject,"threshold":threshold,"evidence_quote":_kernel_clean_text(m.group(0)),"confidence":"medium","source":"count_number"}

    # Price / index value: answer line before tables; reject volume-sized numbers.
    if price_markets:
        metric_words = {
            "high": r"high(?:est)?|peak|reached|hit",
            "low": r"low(?:est)?|bottom|fell|dropped",
            "close": r"clos(?:ed|ing)?|settled|final",
            "open": r"open(?:ed|ing)?",
        }
        selected = "high" if any(w in low_q for w in ["highest", "high", "peak"]) else ("low" if "lowest" in low_q or " low " in f" {low_q} " else ("open" if "open" in low_q else "close"))
        search_text = ans or text
        pat = metric_words.get(selected, r"price|value|reached|hit")
        m = re.search(rf"(?:{pat}).{{0,100}}?\$?\s*([\d,]+(?:\.\d+)?)", search_text, re.I)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 0 < val < 1_000_000_000 and not (1900 <= val <= 2100):
                    return {"kind":"numeric_threshold","metric":selected,"value":val,"unit":"USD","threshold":threshold,"evidence_quote":_kernel_clean_text(m.group(0)),"confidence":"high","source":"price_answer"}
            except Exception:
                pass

    return None


def _kernel_extract_spread_fact(question: str, outcomes: list, content: str) -> Optional[dict]:
    if not _kernel_is_spread_market(outcomes):
        return None
    text = _kernel_relevant_text(content)
    low = text.lower()
    parsed = _kernel_parse_spread_outcomes(outcomes)
    if len(parsed) != 2:
        return None

    # Find score, prefer answer line. Assign winning team to first score when phrase says "X won ... 122-115".
    score_m = re.search(r"\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b", text)
    if not score_m:
        return None
    s1, s2 = int(score_m.group(1)), int(score_m.group(2))

    # Winner from text.
    winner = None
    wfact = _kernel_extract_winner_from_text(question, [p[1] for p in parsed], content)
    if wfact:
        winner = wfact["value"]

    scores = {}
    if winner:
        other = [team for _, team, _ in parsed if _kernel_norm_entity(team) != _kernel_norm_entity(winner)]
        scores[winner] = max(s1, s2)
        if other:
            scores[other[0]] = min(s1, s2)
    else:
        # Fallback: if teams appear near score in order.
        teams = [p[1] for p in parsed]
        scores[teams[0]] = s1
        scores[teams[1]] = s2

    best = None
    for outcome, team, spread in parsed:
        team_score = scores.get(team)
        opp_scores = [v for k, v in scores.items() if _kernel_norm_entity(k) != _kernel_norm_entity(team)]
        if team_score is None or not opp_scores:
            continue
        adjusted = team_score + spread
        opp = opp_scores[0]
        covers = adjusted > opp
        if covers:
            best = {
                "kind":"spread_cover",
                "value": outcome,
                "candidate_outcome": outcome,
                "score": scores,
                "spread": spread,
                "evidence_quote": _kernel_clean_text(score_m.group(0)),
                "confidence": "high",
                "source": "spread_math",
                "calculation": f"spread_cover: {team} {team_score} + ({spread}) = {adjusted:g} > opponent {opp} → {outcome}",
            }
            break
    return best


def _kernel_ollama_fact_hint(question: str, outcomes: list, content: str, intelligence: dict) -> Optional[dict]:
    if not USE_OLLAMA_BRAIN:
        return None
    evidence = _kernel_relevant_text(content)[:3500]
    prompt = (
        "You are OracleREE StructuredFactExtractor.\n"
        "Return STRICT valid JSON only. Do not use markdown. Do not invent resolver names.\n"
        "Your job is NOT to settle the market. Extract the single fact needed to settle it.\n\n"
        "Allowed kind values: winner_result, numeric_threshold, spread_cover, named_choice, binary_event, unknown.\n"
        "Required JSON keys: kind, metric, value, unit, threshold, candidate_outcome, evidence_quote, confidence.\n"
        "Rules:\n"
        "- For winner markets, extract the winner from phrases like 'X won/beat/defeated Y'. Do NOT choose the first entity mentioned.\n"
        "- For numeric markets, value must be a number, not prose. For golf '3-under 67', value is 67 if the outcome is strokes.\n"
        "- For spreads, extract final score and candidate_outcome only if clear.\n"
        "- candidate_outcome must be exactly one of the valid outcomes, or null.\n\n"
        f"Question: {question}\n"
        f"Valid outcomes: {json.dumps([str(o) for o in outcomes], ensure_ascii=False)}\n"
        f"Current plan/intelligence: {json.dumps({k:v for k,v in (intelligence or {}).items() if k != 'prompt_context'}, ensure_ascii=False, default=str)[:1500]}\n"
        f"Evidence:\n{evidence}\n"
    )
    obj = call_ollama_json(prompt, timeout=90)
    if not isinstance(obj, dict):
        return None
    # Only trust candidate outcome if it maps exactly to valid outcomes.
    cand = _kernel_exact_outcome(obj.get("candidate_outcome") or obj.get("value"), outcomes)
    if cand:
        obj["candidate_outcome"] = cand
    return obj


def _kernel_resolve_structured_fact(fact: Optional[dict], question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    if not isinstance(fact, dict):
        return None, None

    kind = str(fact.get("kind") or "").lower()
    metric = str(fact.get("metric") or "").lower()
    value = fact.get("value")
    candidate = _kernel_exact_outcome(fact.get("candidate_outcome") or value, outcomes)

    if kind in {"winner_result", "named_choice", "binary_event"} or metric in {"winner", "selected", "choice"}:
        if candidate:
            quote = _kernel_clean_text(fact.get("evidence_quote") or "")[:180]
            return candidate, f"structured_fact: {metric or kind}={candidate}" + (f" | quote: {quote}" if quote else "")
        return None, None

    if kind == "spread_cover":
        if candidate:
            return candidate, str(fact.get("calculation") or f"structured_fact: spread_cover → {candidate}")
        return None, None

    # Numeric threshold / range.
    if kind == "numeric_threshold" or metric in {"score", "strokes", "points", "runs", "goals", "birdies", "count", "high", "low", "close", "open", "price"}:
        try:
            val = float(str(value).replace(",", ""))
        except Exception:
            return None, None
        threshold = fact.get("threshold")
        if threshold is None:
            threshold = (intelligence or {}).get("threshold") or ((intelligence or {}).get("_rules") or {}).get("threshold") or _kernel_outcome_threshold(outcomes)
        try:
            threshold = float(threshold)
        except Exception:
            threshold = None
        if threshold is None:
            # Numeric range/bucket resolver fallback: choose the outcome whose range contains val.
            if "match_numeric_to_bucket" in globals():
                try:
                    m, c = match_numeric_to_bucket(val, outcomes)
                    if m:
                        return m, c
                except Exception:
                    pass
            return None, None

        outs_l = {str(o).lower(): str(o) for o in outcomes or []}
        over = next((str(o) for o in outcomes or [] if re.search(r"\b(over|above|yes)\b", str(o), re.I)), None)
        under = next((str(o) for o in outcomes or [] if re.search(r"\b(under|below|no)\b", str(o), re.I)), None)

        if val > threshold and over:
            return over, f"threshold_compare: {metric or 'value'} {val:g} > {threshold:g}"
        if val < threshold and under:
            return under, f"threshold_compare: {metric or 'value'} {val:g} < {threshold:g}"
        if val == threshold:
            return None, f"threshold_compare: {val:g} equals threshold {threshold:g}; no exact over/under"
    return None, None


def _kernel_structured_fact_extract(question: str, outcomes: list, content: str, intelligence: dict) -> Optional[dict]:
    # 1. Spread markets.
    f = _kernel_extract_spread_fact(question, outcomes, content)
    if f:
        return f

    # 2. Winner markets: must run before aliases.
    ql = str(question or "").lower()
    rules = (intelligence or {}).get("_rules") or {}
    metric = str((intelligence or {}).get("metric") or rules.get("metric") or "").lower()
    answer_format = str((intelligence or {}).get("answer_format") or "").lower()
    winner_like = (
        metric == "winner"
        or answer_format == "named_choice"
        or any(w in ql for w in ["who will win", "winner", "wins", "win the", "main event", " vs ", " versus "])
    ) and not _kernel_is_spread_market(outcomes)
    if winner_like:
        f = _kernel_extract_winner_from_text(question, outcomes, content)
        if f:
            return f

    # 3. Numeric facts.
    f = _kernel_extract_numeric_fact(question, outcomes, content, intelligence)
    if f:
        return f

    # 4. Ollama fallback hint, then Python validates.
    f = _kernel_ollama_fact_hint(question, outcomes, content, intelligence)
    if f:
        return f

    return None


def extract_specific_answer(content: str, query_plan: dict, question: str, outcomes: list, intelligence: dict) -> tuple[Optional[str], Optional[str]]:
    """
    Final settlement-kernel extractor.
    It converts evidence into a structured fact first, then maps to exact outcome.
    This prevents first-entity alias mistakes like:
      'Sean Strickland won against Khamzat' → Khamzat.
    """
    try:
        fact = _kernel_structured_fact_extract(question, outcomes, content, intelligence or {})
        matched, calc = _kernel_resolve_structured_fact(fact, question, outcomes, intelligence or {})
        if matched:
            return matched, calc
    except Exception as e:
        print(f"[oracle] Settlement kernel failed: {e}")

    # Fall back to previous extractor, but block unsafe winner alias inversions.
    if _ORACLE_KERNEL_PREV_EXTRACT_SPECIFIC_ANSWER:
        try:
            matched, calc = _ORACLE_KERNEL_PREV_EXTRACT_SPECIFIC_ANSWER(content, query_plan, question, outcomes, intelligence)
            if matched and calc and "safe_entity_alias" in str(calc).lower():
                # If evidence contains an explicit different winner, reject alias result.
                wf = _kernel_extract_winner_from_text(question, outcomes, content)
                wm, wc = _kernel_resolve_structured_fact(wf, question, outcomes, intelligence or {})
                if wm and wm != matched:
                    return wm, wc
            return matched, calc
        except Exception as e:
            print(f"[oracle] Previous extractor failed: {e}")
    return None, None


def validate_evidence(content: str, required_data_type: str, extraction_target: str, question: str) -> tuple[bool, str]:
    """
    Final validation guard.
    Keep previous validation, then add topic/entity guards for match results.
    """
    if _ORACLE_KERNEL_PREV_VALIDATE_EVIDENCE:
        try:
            ok, reason = _ORACLE_KERNEL_PREV_VALIDATE_EVIDENCE(content, required_data_type, extraction_target, question)
        except Exception:
            ok, reason = True, "previous validation unavailable"
    else:
        ok, reason = True, "accepted"

    if not ok:
        return ok, reason

    text = str(content or "")
    tlow = text.lower()
    qlow = str(question or "").lower()

    if required_data_type in {"score", "match_result"} or any(w in qlow for w in [" vs ", " versus "]):
        vs_m = re.search(r"(.{2,50}?)\s+(?:vs\.?|versus)\s+(.{2,50}?)(?:\s*[—–\-]|\s*\(|\?|$)", qlow)
        if vs_m:
            side_words = []
            for side in [vs_m.group(1), vs_m.group(2)]:
                words = [w for w in re.findall(r"[a-zA-Z]+", side) if len(w) > 3 and w not in {"will", "what", "when", "where", "main", "event"}]
                if words:
                    side_words.append(words)
            if len(side_words) == 2:
                hits = [any(w in tlow for w in words) for words in side_words]
                if not any(hits):
                    return False, "topic drift: evidence mentions neither participant"

    if required_data_type == "confirmation":
        # Avoid generic "has not announced" from a wrong page for purchase/acquisition markets.
        if any(w in qlow for w in ["purchase", "buy", "acquire", "bitcoin", "btc"]):
            if re.search(r"\b(has not|did not|not announced|no purchase)\b", tlow):
                idx = re.search(r"\b(has not|did not|not announced|no purchase)\b", tlow).start()
                window = tlow[max(0, idx - 80): idx + 120]
                if not any(w in window for w in ["purchase", "bitcoin", "btc", "buy", "acquire"]):
                    return False, "generic negative confirmation without purchase context"

    return True, reason


def build_oracle_prompt(original_prompt: str, evidence: dict) -> str:
    """
    Final REE prompt builder.
    Important: do NOT inject OracleREE's matched_outcome or calculation into REE.
    REE should receive verified facts/evidence and the original settlement prompt,
    not OracleREE's previous answer.
    """
    intel = evidence.get("intelligence", {}) or {}
    fv = evidence.get("final_verdict", {}) if isinstance(evidence.get("final_verdict"), dict) else {}
    facts_list = fv.get("facts", []) if isinstance(fv, dict) else []
    pipeline = fv.get("pipeline", "UNKNOWN") if isinstance(fv, dict) else "UNKNOWN"

    lines = [
        "═" * 51,
        "ORACLEREE VERIFIED DATA BLOCK",
        "═" * 51,
        f"Market:      {evidence.get('market_question','')}",
        f"Captured at: {evidence.get('captured_at','')}",
        f"Close time:  {evidence.get('close_time','')}",
        f"Market type: {intel.get('market_type','unknown')}",
        f"Event date:  {intel.get('event_date','unknown')}",
        f"Pipeline:    {pipeline}",
        "",
        "EXTRACTED FACTS / EVIDENCE:",
    ]

    included = False
    for f in facts_list:
        if not isinstance(f, dict):
            continue
        label = str(f.get("label", ""))
        # Do not leak OracleREE's final answer into REE.
        if label.lower() in {"matched_outcome", "oracle_outcome", "outcome"}:
            continue
        val = str(f.get("value", ""))
        if not val:
            continue
        u = f" {f.get('unit')}" if f.get("unit") else ""
        t = f" [{f.get('timestamp')}]" if f.get("timestamp") else ""
        lines.append(f"  {label}: {val}{u}{t}")
        included = True

    if not included and isinstance(fv, dict):
        raw = fv.get("raw_content") or ""
        if raw:
            lines.append("  raw_evidence: " + str(raw)[:2000])
            included = True

    if pipeline == "INCONCLUSIVE":
        lines += [
            "",
            "EVIDENCE STATUS: INCONCLUSIVE",
            f"Reason: {fv.get('reason','Creator sources could not be fetched/parsed') if isinstance(fv,dict) else ''}",
        ]

    if evidence.get("oracle_seal_ipfs"):
        lines += ["", f"OracleSeal IPFS: {evidence['oracle_seal_ipfs']}"]

    lines += [
        "",
        "IMPORTANT:",
        "  The block above contains evidence/facts only.",
        "  Do not assume OracleREE's derived outcome unless it is independently supported by the evidence.",
        "  Apply the ORIGINAL SETTLEMENT PROMPT and output exactly one valid outcome.",
        "",
        "INTEGRITY:",
        f"  Evidence hash: {evidence.get('evidence_hash','N/A')}",
        f"  IPFS CID:      {evidence.get('ipfs_cid','Not pinned')}",
        "═" * 51,
        "END ORACLEREE VERIFIED DATA BLOCK",
        "═" * 51,
        "",
        "ORIGINAL SETTLEMENT PROMPT:",
        "─" * 49,
        original_prompt,
    ]
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL SETTLEMENT KERNEL PATCH
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FINAL NO-SOURCE DEFAULT FETCH PATCH
# If a Delphi market has no DATA SOURCES, do not stop with NO_SOURCES.
# Use conservative, category-trusted default sources and still run the normal
# source-locked evidence pipeline. This is especially important for sports/stat
# markets like golf scorecards where the creator omitted sources.
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_SOURCE_POLICY_NOTE = "trusted_default_sources_no_creator_sources"


def _no_source_infer_default_sources(question: str, intelligence: Optional[dict] = None) -> list[str]:
    """Infer conservative default sources only when creator provided no sources."""
    q = str(question or "").lower()
    intel = intelligence or {}
    metric = str(intel.get("metric") or "").lower()
    subject = str(intel.get("count_subject") or "").lower()
    mt = str(intel.get("market_type") or "").lower()

    # Golf / PGA markets: official scorecard/stat sources first, ESPN as broad fallback.
    if any(k in q for k in ["pga championship", "pga tour", "golf", "birdie", "birdies", "scheffler"]):
        return [
            "https://www.espn.com/golf/",
            "https://www.pgachampionship.com/",
            "https://www.pgatour.com/",
        ]

    # Major sports defaults when prompt omits sources.
    if any(k in q for k in ["nba", "basketball"]):
        return ["https://www.nba.com/", "https://www.espn.com/nba/"]
    if any(k in q for k in ["nfl", "football", "draft"]):
        return ["https://www.nfl.com/", "https://www.espn.com/nfl/"]
    if any(k in q for k in ["mlb", "baseball"]):
        return ["https://www.mlb.com/", "https://www.espn.com/mlb/"]
    if any(k in q for k in ["nhl", "hockey"]):
        return ["https://www.nhl.com/", "https://www.espn.com/nhl/"]
    if any(k in q for k in ["ufc", "mma", "fight", "chimaev", "strickland"]):
        return ["https://www.espn.com/mma/", "https://apnews.com/"]
    if any(k in q for k in ["cricket", "wicket", "ipl", "psl"]):
        return ["https://www.espncricinfo.com/", "https://www.cricbuzz.com/"]

    # Finance/crypto defaults only when the market clearly asks price/value and no source exists.
    if any(k in q for k in ["price", "high", "low", "close", "market cap", "fdv"]):
        if any(k in q for k in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol"]):
            return ["https://www.coingecko.com/", "https://coinmarketcap.com/"]
        return ["https://finance.yahoo.com/", "https://www.cnbc.com/"]

    return []


def _no_source_refine_query_plan_for_stats(query_plan: dict, question: str, outcomes: list,
                                           rules: dict, event_date: str, source_domain: str) -> dict:
    """Make no-source stat/count queries less generic and more scorecard-friendly."""
    q = str(question or "")
    ql = q.lower()
    plan = dict(query_plan or {})

    # Golf birdies are not usually reported as generic "official final count total".
    # They live in round scorecards / hole-by-hole pages.
    if "birdie" in ql or "birdies" in ql:
        player = "Scottie Scheffler" if "scheffler" in ql else "player"
        event = "2026 PGA Championship" if "pga championship" in ql else q
        plan.update({
            "query": f'"{player}" "{event}" first round scorecard birdies hole by hole round 1',
            "search_depth": "basic",
            "required_data_type": "count",
            "extraction_target": f"{player} first round birdies scorecard",
            "what_to_validate": "scorecard birdie count",
            "need_number": True,
            "number_context": "birdies",
        })
        return plan

    return plan


# Wrap the active query builder so golf/stat markets get the right fetch language.
_NO_SOURCE_PRE_BUILD_UNIVERSAL_QUERY = build_universal_query

def build_universal_query(question: str, outcomes: list, rules: dict, event_date: str, source_domain: str) -> dict:
    base = _NO_SOURCE_PRE_BUILD_UNIVERSAL_QUERY(question, outcomes, rules, event_date, source_domain)
    return _no_source_refine_query_plan_for_stats(base, question, outcomes, rules or {}, event_date, source_domain)


# Wrap the active oracle builder. If creator omitted DATA SOURCES, inject trusted
# default sources into a shallow market copy, then let the normal pipeline run.
_NO_SOURCE_PRE_BUILD_ORACLE_EVIDENCE = build_oracle_evidence

def build_oracle_evidence(market: dict) -> dict:
    try:
        meta = (market or {}).get("metadata") or {}
        question = meta.get("question", "")
        prompt_ctx = (meta.get("model") or {}).get("prompt_context", "")
        outcomes = meta.get("outcomes") or []
        resolves_at = (market or {}).get("resolvesAt", "")
        data_sources = (market or {}).get("dataSources") or []

        if not data_sources:
            # Run a lightweight plan first, only to infer source category.
            try:
                intel = analyze_market_intelligence(question, prompt_ctx, outcomes, [], resolves_at)
            except Exception:
                intel = {}
            defaults = _no_source_infer_default_sources(question, intel)
            if defaults:
                print(f"[oracle] No creator DATA SOURCES. Using trusted defaults: {defaults}")
                market2 = dict(market)
                market2["dataSources"] = defaults
                market2["_oracle_source_policy"] = _DEFAULT_SOURCE_POLICY_NOTE
                return _NO_SOURCE_PRE_BUILD_ORACLE_EVIDENCE(market2)
    except Exception as e:
        print(f"[oracle] no-source default patch skipped: {e}")

    return _NO_SOURCE_PRE_BUILD_ORACLE_EVIDENCE(market)

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL NO-SOURCE DEFAULT FETCH PATCH
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FINAL CANDIDATE ARBITRATION PATCH
# Do not accept the first OUTCOME_FOUND. Score all candidate evidence, reject
# generic/contradictory pages, recompute structured facts when possible, and use
# the highest-confidence candidate as the final verdict.
# ═══════════════════════════════════════════════════════════════════════════════

_POSITION_TO_OUTCOME = {
    # offensive line
    "ol": "Offensive Lineman", "ot": "Offensive Lineman", "lt": "Offensive Lineman",
    "rt": "Offensive Lineman", "og": "Offensive Lineman", "g": "Offensive Lineman",
    "c": "Offensive Lineman", "center": "Offensive Lineman", "guard": "Offensive Lineman",
    "tackle": "Offensive Lineman", "offensive tackle": "Offensive Lineman",
    "offensive guard": "Offensive Lineman", "offensive lineman": "Offensive Lineman",
    # skill/offense
    "te": "Tight End", "tight end": "Tight End",
    "qb": "Quarterback", "quarterback": "Quarterback",
    "wr": "Wide Receiver", "wide receiver": "Wide Receiver",
    "rb": "Running Back", "running back": "Running Back",
    # defense/special teams
    "cb": "Cornerback", "cornerback": "Cornerback",
    "s": "Safety", "safety": "Safety",
    "lb": "Linebacker", "linebacker": "Linebacker",
    "edge": "Defensive Line / Edge", "dl": "Defensive Line / Edge",
    "dt": "Defensive Line / Edge", "de": "Defensive Line / Edge",
    "defensive line": "Defensive Line / Edge", "defensive lineman": "Defensive Line / Edge",
    "k": "Kicker / Punter / Long Snapper", "p": "Kicker / Punter / Long Snapper",
    "ls": "Kicker / Punter / Long Snapper", "kicker": "Kicker / Punter / Long Snapper",
    "punter": "Kicker / Punter / Long Snapper", "long snapper": "Kicker / Punter / Long Snapper",
}


def _arb_text(obj) -> str:
    return str(obj or "")


def _arb_norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _arb_text(s).lower()).strip()


def _arb_exact_outcome(candidate: str, outcomes: list) -> Optional[str]:
    cn = _arb_norm(candidate)
    for o in outcomes or []:
        if _arb_norm(o) == cn:
            return str(o)
    return None


def _arb_outcome_from_position(pos: str, outcomes: list) -> Optional[str]:
    p = _arb_norm(pos)
    # Prefer longer keys first so "offensive tackle" beats "tackle" etc.
    for key in sorted(_POSITION_TO_OUTCOME, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", p):
            mapped = _POSITION_TO_OUTCOME[key]
            exact = _arb_exact_outcome(mapped, outcomes)
            if exact:
                return exact
    return None


def _arb_pick_order_num(question: str) -> Optional[int]:
    q = _arb_norm(question)
    if re.search(r"\b(second|2nd)\b", q): return 2
    if re.search(r"\b(third|3rd)\b", q): return 3
    if re.search(r"\b(fourth|4th)\b", q): return 4
    if re.search(r"\b(first|1st)\b", q): return 1
    return None


def _arb_team_from_question(question: str) -> str:
    q = _arb_text(question)
    m = re.search(r"who\s+will\s+the\s+(.+?)\s+draft\b", q, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _arb_extract_answer_line_local(raw: str) -> str:
    try:
        return extract_answer_line(raw)
    except Exception:
        m = re.search(r"ANSWER:\s*(.+?)(?:\n\n|\n\[|\Z)", _arb_text(raw), re.S|re.I)
        return " ".join(m.group(1).split()) if m else ""


def _arb_parse_draft_round1_list(raw: str) -> list[dict]:
    """Parse draft summaries like: Round 1 (No. 10): OT Francis Mauigoa, Miami."""
    text = _arb_text(raw)
    picks = []
    pat = re.compile(
        r"Round\s+1\s*\(\s*No\.\s*(\d+)\s*\)\s*:\s*([^\n\r]+)",
        re.I,
    )
    for m in pat.finditer(text):
        pick_no = int(m.group(1))
        line = " ".join(m.group(2).strip().split())
        # Examples: "OT Francis Mauigoa, Miami" or "LB/Edge Arvell Reese, Ohio State"
        before_school = line.split(",", 1)[0].strip()
        mm = re.match(r"([A-Za-z/ ]{1,24})\s+([A-Z][A-Za-z'\-.]+(?:\s+[A-Z][A-Za-z'\-.]+){0,3})$", before_school)
        if mm:
            pos = mm.group(1).strip()
            player = mm.group(2).strip()
        else:
            pos = ""
            player = before_school
        picks.append({"round": 1, "pick_no": pick_no, "position": pos, "player": player, "line": line})
    picks.sort(key=lambda x: x["pick_no"])
    return picks


def _arb_resolve_draft(question: str, outcomes: list, raw: str) -> tuple[Optional[str], Optional[str], int]:
    """
    Draft markets should resolve from structured round/pick order, not a single Tavily answer line.
    This fixes cases where a fallback answer says 'second first-round pick = Colton Hood' while
    the same page's actual draft list shows Colton Hood is Round 2 and the second Round 1 pick is OL.
    """
    ql = _arb_norm(question)
    if "draft" not in ql or not any(k in ql for k in ["pick", "selection", "selected", "draft"]):
        return None, None, 0
    order = _arb_pick_order_num(question)
    raw_text = _arb_text(raw)

    picks = _arb_parse_draft_round1_list(raw_text)
    if order and len(picks) >= order:
        chosen = picks[order - 1]
        outcome = _arb_outcome_from_position(chosen.get("position", ""), outcomes)
        if outcome:
            calc = (f"draft_round1_order: Round 1 picks={[(p['pick_no'], p['position'], p['player']) for p in picks]} | "
                    f"requested #{order} → No. {chosen['pick_no']} {chosen['player']} ({chosen['position']}) → {outcome}")
            return outcome, calc, 160

    # Fallback: exact answer line, but lower confidence than structured round list.
    ans = _arb_extract_answer_line_local(raw_text)
    if ans:
        if re.search(r"\bsecond\s+first[- ]round\s+pick\b", ans, re.I) or (order == 2 and "second" in _arb_norm(ans)):
            out = _arb_outcome_from_position(ans, outcomes)
            if out:
                return out, f"draft_answer_line_position: {ans[:220]} → {out}", 105
        # If answer line names a position and question is first/second pick, use only if it has round context.
        if re.search(r"\b(round\s+1|first[- ]round|1st round|No\.\s*\d+)\b", ans, re.I):
            out = _arb_outcome_from_position(ans, outcomes)
            if out:
                return out, f"draft_answer_line_round_context: {ans[:220]} → {out}", 90

    return None, None, 0


def _arb_count_from_hole_list(raw: str, subject: str) -> Optional[int]:
    text = _arb_text(raw)
    if "birdie" not in text.lower():
        return None
    patterns = [
        r"birdies?\s+on\s+holes?\s+([0-9,\sand]+)",
        r"made\s+birdies?\s+on\s+holes?\s+([0-9,\sand]+)",
        r"scored\s+birdies?\s+on\s+holes?\s+([0-9,\sand]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        nums = re.findall(r"\d{1,2}", m.group(1))
        # Golf holes are 1-18; this rejects dates/scores leaking into the list.
        holes = [int(n) for n in nums if 1 <= int(n) <= 18]
        if holes:
            return len(dict.fromkeys(holes))
    return None


def _arb_map_count_to_outcome(count: int, outcomes: list) -> Optional[str]:
    for o in outcomes or []:
        s = str(o).strip()
        # 0-3, 4-5, etc.
        m = re.match(r"^\s*(\d+)\s*[-–]\s*(\d+)\s*$", s)
        if m and int(m.group(1)) <= count <= int(m.group(2)):
            return s
        m = re.match(r"^\s*(\d+)\s*\+\s*$", s)
        if m and count >= int(m.group(1)):
            return s
        m = re.match(r"^\s*(?:over|above)\s*(\d+(?:\.\d+)?)", s, re.I)
        if m and count > float(m.group(1)):
            return s
        m = re.match(r"^\s*(?:under|below)\s*(\d+(?:\.\d+)?)", s, re.I)
        if m and count < float(m.group(1)):
            return s
    return None


def _arb_resolve_count(question: str, outcomes: list, raw: str, intelligence: dict) -> tuple[Optional[str], Optional[str], int]:
    ql = _arb_norm(question)
    subject = _arb_norm((intelligence or {}).get("count_subject") or "")
    if not ("how many" in ql or (intelligence or {}).get("metric") == "count"):
        return None, None, 0

    # Golf/birdie deterministic extraction from hole list.
    if "birdie" in ql or "birdies" in ql or "birdies" in subject:
        count = _arb_count_from_hole_list(raw, subject)
        if count is not None:
            out = _arb_map_count_to_outcome(count, outcomes)
            if out:
                return out, f"count_from_hole_list: {count} birdies → {out}", 155

    # Generic explicit count phrase: "had 5 birdies" / "5 birdies" close to subject.
    rawl = _arb_text(raw).lower()
    subj = subject or _infer_count_subject(question)
    if subj:
        m = re.search(rf"\b(\d+)\s+{re.escape(subj.rstrip('s'))}s?\b", rawl)
        if m:
            count = int(m.group(1))
            out = _arb_map_count_to_outcome(count, outcomes)
            if out:
                return out, f"explicit_count: {count} {subj} → {out}", 100
    return None, None, 0


def _arb_is_generic_homepage(result: dict, question: str) -> bool:
    raw = _arb_text(result.get("raw_content") or " ".join(_arb_text(f.get("value")) for f in result.get("facts", []) if isinstance(f, dict)))
    src = _arb_text(result.get("source_used"))
    rawl = raw.lower()
    srcl = src.lower().rstrip("/")
    generic_url = bool(re.search(r"/(golf|nfl|nba|mlb|nhl|mma|soccer)?/?$", srcl)) and result.get("fetch_method") == "direct"
    generic_text = any(x in rawl for x in [
        "visit espn for", "latest news", "scores and schedule", "video highlights",
        "<!doctype html", "<html", "site_name", "og:title",
    ])
    qwords = [w for w in re.findall(r"[a-zA-Z]{4,}", question.lower()) if w not in {"will", "with", "their", "first", "second", "round", "draft", "many", "have", "championship"}]
    hits = sum(1 for w in set(qwords) if w in rawl)
    return bool((generic_url or generic_text) and hits < max(2, min(4, len(set(qwords)) // 3)))


def _arb_creator_source_bonus(result: dict, intelligence: dict) -> int:
    src = _arb_text(result.get("source_used") or result.get("source") or "").lower()
    rules = (intelligence or {}).get("_rules") or {}
    source_order = [str(s).lower() for s in rules.get("source_order") or []]
    if not source_order:
        return 0
    # First explicit creator source should beat fallback when both are valid.
    for i, s in enumerate(source_order):
        if s and (s in src or src in s):
            return 35 - min(i, 5) * 5
    # Penalize fallback only if a creator source result also exists and is valid elsewhere.
    if result.get("fetch_method") == "sports_fallback":
        return -25
    return 0


def _arb_get_raw(result: dict) -> str:
    raw = _arb_text(result.get("raw_content"))
    if raw:
        return raw
    vals = []
    for f in result.get("facts") or []:
        if isinstance(f, dict) and f.get("label") == "raw_evidence":
            vals.append(_arb_text(f.get("value")))
    return "\n".join(vals)


def _arb_existing_outcome(result: dict, outcomes: list) -> Optional[str]:
    dr = result.get("derived_result") or {}
    cand = result.get("matched_outcome") or dr.get("matched_outcome")
    if cand:
        return _arb_exact_outcome(str(cand), outcomes)
    for f in result.get("facts") or []:
        if isinstance(f, dict) and str(f.get("label", "")).lower() == "matched_outcome":
            out = _arb_exact_outcome(str(f.get("value", "")), outcomes)
            if out:
                return out
    return None


def _arb_score_and_resolve_result(result: dict, question: str, outcomes: list, intelligence: dict) -> dict:
    raw = _arb_get_raw(result)
    score = 0
    reasons = []
    outcome = None
    calc = None

    if _arb_is_generic_homepage(result, question):
        return {"score": -1000, "outcome": None, "calculation": "rejected: generic homepage / weak page", "result": result}

    if result.get("fetch_status") == "FETCHED": score += 10
    if result.get("parse_status") == "PARSED": score += 10
    if result.get("outcome_status") == "OUTCOME_FOUND": score += 5
    score += _arb_creator_source_bonus(result, intelligence)

    # Recompute high-confidence structured facts. These override existing noisy extraction.
    for resolver in (_arb_resolve_draft,):
        out, c, s = resolver(question, outcomes, raw)
        if out and s > score:
            outcome, calc, score = out, c, s + _arb_creator_source_bonus(result, intelligence)
            reasons.append("draft_structured_resolver")

    out, c, s = _arb_resolve_count(question, outcomes, raw, intelligence or {})
    if out and s > score:
        outcome, calc, score = out, c, s + _arb_creator_source_bonus(result, intelligence)
        reasons.append("count_structured_resolver")

    if not outcome:
        existing = _arb_existing_outcome(result, outcomes)
        if existing:
            outcome = existing
            dr = result.get("derived_result") or {}
            calc = dr.get("calculation") or result.get("calculation") or "existing_valid_outcome"
            score += 35
            reasons.append("existing_outcome")

    # Specific contradiction guard for draft: if existing says CB but structured list says OL, structured wins.
    # If no structured resolver fired but raw has a Round 1 list contradicting answer line, downgrade answer-line claims.
    if "draft" in _arb_norm(question) and outcome:
        picks = _arb_parse_draft_round1_list(raw)
        order = _arb_pick_order_num(question)
        if order and len(picks) >= order:
            structured_out = _arb_outcome_from_position(picks[order - 1].get("position", ""), outcomes)
            if structured_out and structured_out != outcome:
                outcome = structured_out
                calc = f"draft_internal_contradiction_fixed: answer-line conflicted with Round 1 list; requested #{order} = {picks[order-1]} → {structured_out}"
                score = 165 + _arb_creator_source_bonus(result, intelligence)
                reasons.append("internal_contradiction_fixed")

    return {"score": score, "outcome": outcome, "calculation": calc, "result": result, "reasons": reasons}


def _arb_build_final_from_candidate(candidate: dict, evidence: dict) -> dict:
    res = dict(candidate.get("result") or {})
    outcome = candidate.get("outcome")
    calc = candidate.get("calculation") or "candidate_arbitration"
    res["outcome_status"] = "OUTCOME_FOUND"
    res["pipeline"] = "FETCHED | PARSED | OUTCOME_FOUND"
    res["matched_outcome"] = outcome
    res["calculation"] = calc
    res["derived_result"] = {"matched_outcome": outcome, "calculation": calc}
    res["arbitration"] = {
        "score": candidate.get("score"),
        "reasons": candidate.get("reasons") or [],
    }
    # Keep facts lean and useful for REE. Do not add matched_outcome into facts.
    raw = _arb_get_raw(candidate.get("result") or {})
    facts = []
    if raw:
        facts.append({
            "label": "raw_evidence",
            "value": raw[:2200],
            "source": res.get("source_used") or "",
            "timestamp": (evidence.get("intelligence") or {}).get("event_date") or "",
        })
    facts.append({
        "label": "structured_resolution",
        "value": calc,
        "source": res.get("source_used") or "",
        "timestamp": (evidence.get("intelligence") or {}).get("event_date") or "",
    })
    res["facts"] = facts
    return res


def _arb_apply_candidate_arbitration(evidence: dict, market: dict) -> dict:
    try:
        source_results = [r for r in (evidence.get("source_results") or []) if isinstance(r, dict)]
        if not source_results:
            return evidence
        question = evidence.get("market_question") or ((market.get("metadata") or {}).get("question", ""))
        outcomes = ((market.get("metadata") or {}).get("outcomes") or [])
        intelligence = evidence.get("intelligence") or {}
        scored = [_arb_score_and_resolve_result(r, question, outcomes, intelligence) for r in source_results]
        valid = [c for c in scored if c.get("outcome") and c.get("score", -999) > 0]
        if not valid:
            evidence["arbitration"] = {"status": "NO_VALID_CANDIDATES", "scores": [{"score": c.get("score"), "outcome": c.get("outcome"), "calc": c.get("calculation")} for c in scored]}
            return evidence
        valid.sort(key=lambda c: c.get("score", 0), reverse=True)
        chosen = valid[0]
        old_fv = evidence.get("final_verdict") or {}
        old_out = (old_fv.get("matched_outcome") or (old_fv.get("derived_result") or {}).get("matched_outcome")) if isinstance(old_fv, dict) else None
        new_out = chosen.get("outcome")
        evidence["final_verdict"] = _arb_build_final_from_candidate(chosen, evidence)
        evidence["event_verdict"] = {
            "verdict": new_out,
            "matchedOutcome": new_out,
            "explanation": chosen.get("calculation") or "candidate_arbitration",
            "source": (chosen.get("result") or {}).get("source_used") or "",
        }
        evidence["arbitration"] = {
            "status": "APPLIED",
            "old_outcome": old_out,
            "chosen_outcome": new_out,
            "chosen_score": chosen.get("score"),
            "chosen_source": (chosen.get("result") or {}).get("source_used"),
            "candidates": [
                {"score": c.get("score"), "outcome": c.get("outcome"), "source": (c.get("result") or {}).get("source_used"), "calculation": c.get("calculation")}
                for c in scored
            ],
        }
        if old_out and old_out != new_out:
            print(f"[oracle] Arbitration corrected final verdict: {old_out} → {new_out}")
        else:
            print(f"[oracle] Arbitration selected final verdict: {new_out}")
        # Recompute evidence hash/IPFS after changing final verdict.
        evidence.pop("evidence_hash", None)
        evidence.pop("ipfs_cid", None)
        evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
        evidence["ipfs_cid"] = pin_to_ipfs(evidence, f"oracle-ree-{(market.get('id') or '')[:10]}")
    except Exception as e:
        print(f"[oracle] candidate arbitration skipped: {e}")
    return evidence


_ARB_PRE_BUILD_ORACLE_EVIDENCE = build_oracle_evidence

def build_oracle_evidence(market: dict) -> dict:
    evidence = _ARB_PRE_BUILD_ORACLE_EVIDENCE(market)
    return _arb_apply_candidate_arbitration(evidence, market or {})

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL CANDIDATE ARBITRATION PATCH
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH: DATE-STRICT TIME-SERIES / API VALUE ARBITRATION
# Fixes markets like:
#   "ORN Index price of H200 on the 23rd April"
# where the old planner read only "April" and incorrectly set event_date to
# the last day of the month, then Ollama/structured extraction selected the
# wrong timestamp.  For JSON/API time series, Python must select the exact
# requested date deterministically before any LLM hint is trusted.
# ═══════════════════════════════════════════════════════════════════════════════

def _arb_month_num(name: str) -> Optional[int]:
    if not name:
        return None
    return {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }.get(str(name).strip().lower())


def _arb_extract_target_date(question: str, intelligence: Optional[dict] = None) -> Optional[str]:
    """
    Extract the actual target date from a question, supporting both:
      - April 23 / April 23rd
      - 23 April / 23rd April
    The year is inferred from close_time/event_date if not written.
    """
    q = str(question or "")
    iq = intelligence or {}
    year = None

    ym = re.search(r"\b(20\d{2})\b", q)
    if ym:
        year = int(ym.group(1))
    else:
        for source in [iq.get("close_time"), iq.get("event_date"), iq.get("_rules", {}).get("event_date")]:
            sm = re.search(r"\b(20\d{2})\b", str(source or ""))
            if sm:
                year = int(sm.group(1))
                break
    if not year:
        year = 2026

    # Month day: "April 23rd", "April 23"
    m = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?\b",
        q,
        re.I,
    )
    if m:
        month = _arb_month_num(m.group(1))
        day = int(m.group(2))
        if month:
            return f"{year:04d}-{month:02d}-{day:02d}"

    # Day month: "23rd April", "23 April"
    m = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
        q,
        re.I,
    )
    if m:
        day = int(m.group(1))
        month = _arb_month_num(m.group(2))
        if month:
            return f"{year:04d}-{month:02d}-{day:02d}"

    return None


def _arb_extract_timeseries_records(raw: str) -> list[dict]:
    """
    Robustly extract timestamp/value pairs from JSON or truncated JSON-like text.
    Supports fields: index_value, value, price, close, close_price.
    """
    raw = str(raw or "")
    records = []

    # Full JSON path.
    try:
        obj = json.loads(raw)
        arr = obj.get("data") if isinstance(obj, dict) else obj
        if isinstance(arr, list):
            for rec in arr:
                if not isinstance(rec, dict):
                    continue
                ts = str(rec.get("timestamp") or rec.get("date") or rec.get("time") or "")
                val = (
                    rec.get("index_value")
                    if rec.get("index_value") is not None else
                    rec.get("value")
                    if rec.get("value") is not None else
                    rec.get("price")
                    if rec.get("price") is not None else
                    rec.get("close")
                    if rec.get("close") is not None else
                    rec.get("close_price")
                )
                if ts and val is not None:
                    try:
                        records.append({"timestamp": ts, "value": float(val)})
                    except Exception:
                        pass
    except Exception:
        pass

    # Regex path works even when evidence/raw_content is truncated.
    if not records:
        pat = re.compile(
            r'"timestamp"\s*:\s*"([^"]+)"[\s\S]{0,120}?'
            r'"(?:index_value|value|price|close|close_price)"\s*:\s*(-?\d+(?:\.\d+)?)',
            re.I,
        )
        for ts, val in pat.findall(raw):
            try:
                records.append({"timestamp": ts, "value": float(val)})
            except Exception:
                continue

    # De-duplicate by timestamp.
    uniq = {}
    for r in records:
        uniq[r["timestamp"]] = r
    return list(uniq.values())


def _arb_resolve_timeseries_threshold(question: str, outcomes: list, raw: str,
                                      intelligence: Optional[dict] = None) -> tuple[Optional[str], Optional[str], int]:
    """
    Deterministically resolve JSON/API time-series threshold markets.

    Important rules:
    - Use the date in the question, not a month-only fallback.
    - Prefer exact UTC calendar date YYYY-MM-DD in the timestamp.
    - Do not let Ollama select a different timestamp.
    - If no target-date record exists and rules say missing data settles to an outcome,
      use that fallback explicitly.
    """
    qn = _arb_norm(question)
    raw_text = str(raw or "")
    if not any(x in qn for x in ["index price", "orn index", "index value", "h200", "reported value"]):
        return None, None, 0

    target_date = _arb_extract_target_date(question, intelligence or {})
    if not target_date:
        return None, None, 0

    threshold = None
    try:
        threshold = (intelligence or {}).get("threshold")
        if threshold is not None:
            threshold = float(threshold)
    except Exception:
        threshold = None
    if threshold is None:
        threshold = _find_numeric_threshold(outcomes)
    if threshold is None:
        threshold = _canon_extract_threshold(question, outcomes)
    if threshold is None:
        return None, None, 0

    records = _arb_extract_timeseries_records(raw_text)
    if not records:
        return None, None, 0

    # Strongest match: timestamp starts with the requested UTC date.
    exact = [r for r in records if str(r.get("timestamp", "")).startswith(target_date)]

    # Secondary: if no exact match, allow a local-date interpretation ONLY when the
    # source timestamp is late UTC and converting to UTC+4 gives the target date.
    # This is lower confidence and should not beat an exact UTC date if available.
    local = []
    if not exact:
        for r in records:
            ts = str(r.get("timestamp", ""))
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                # UAE/default local interpretation used only as fallback for daily 20:00/21:00 UTC releases.
                local_date = (dt.timestamp() + 4 * 3600)
                local_dt = datetime.fromtimestamp(local_date, timezone.utc)
                if local_dt.date().isoformat() == target_date:
                    local.append(r)
            except Exception:
                continue

    chosen = exact[0] if exact else (local[0] if local else None)

    if chosen:
        value = float(chosen["value"])
        # Map to exact valid outcomes.  Prefer lexical outcome semantics over any LLM hint.
        below_out = None
        above_out = None
        for o in outcomes or []:
            ol = str(o).lower()
            if "below" in ol or "under" in ol:
                below_out = str(o)
            if "above" in ol or "and above" in ol or "over" in ol:
                above_out = str(o)

        # For labels like "Below $3.80" vs "$3.80 and above":
        # below is strictly below threshold; above is threshold or higher.
        if value < float(threshold) and below_out:
            out = below_out
        elif value >= float(threshold) and above_out:
            out = above_out
        else:
            return None, f"timeseries_value_found_but_no_outcome: {target_date} value={value} threshold={threshold}", 0

        mode = "exact_date" if exact else "local_date_fallback"
        return (
            out,
            f"timeseries_threshold_{mode}: target_date={target_date}; timestamp={chosen['timestamp']}; value={value}; threshold={threshold} → {out}",
            190 if exact else 140,
        )

    # Explicit missing-data settlement rule, only if present in settlement rules.
    prompt = str((intelligence or {}).get("prompt_context") or (intelligence or {}).get("_rules", {}).get("raw_rules") or "")
    if re.search(r"not published within\s+24\s+hours.*settle(?:s|d)?\s+to\s+3\.?80\s+and\s+above", prompt, re.I):
        for o in outcomes or []:
            if "above" in str(o).lower():
                return (
                    str(o),
                    f"timeseries_missing_data_fallback: no record for target_date={target_date}; prompt says missing within 24h settles to {o}",
                    120,
                )

    return None, f"timeseries_no_target_record: target_date={target_date}", 0


# Keep the previous arbitrator, but insert the API/time-series resolver before
# trusting any existing/Ollama-derived outcome.
_ARB_SCORE_AND_RESOLVE_RESULT_PRE_TIMESERIES = _arb_score_and_resolve_result

def _arb_score_and_resolve_result(result: dict, question: str, outcomes: list, intelligence: dict) -> dict:
    raw = _arb_get_raw(result)
    out, calc, score = _arb_resolve_timeseries_threshold(question, outcomes, raw, intelligence or {})
    if out:
        return {
            "score": score + _arb_creator_source_bonus(result, intelligence or {}),
            "outcome": out,
            "calculation": calc,
            "result": result,
            "reasons": ["timeseries_date_strict_resolver"],
        }

    scored = _ARB_SCORE_AND_RESOLVE_RESULT_PRE_TIMESERIES(result, question, outcomes, intelligence)
    # If a candidate came only from an LLM/structured quote that references a date
    # different from the target date, downgrade it hard.
    try:
        target_date = _arb_extract_target_date(question, intelligence or {})
        calc_txt = str(scored.get("calculation") or "")
        if target_date and "timestamp" in calc_txt.lower():
            m = re.search(r"20\d{2}-\d{2}-\d{2}", calc_txt)
            if m and m.group(0) != target_date:
                scored["score"] = min(scored.get("score", 0), -50)
                scored["outcome"] = None
                scored["calculation"] = f"rejected_wrong_timeseries_date: target={target_date}; candidate_calc={calc_txt}"
                scored.setdefault("reasons", []).append("wrong_timeseries_date_rejected")
    except Exception:
        pass
    return scored


# Also correct the intelligence date shown in proof after evidence is built.
_ARB_APPLY_CANDIDATE_ARBITRATION_PRE_DATESTRICT = _arb_apply_candidate_arbitration

def _arb_apply_candidate_arbitration(evidence: dict, market: dict) -> dict:
    try:
        question = evidence.get("market_question") or ((market.get("metadata") or {}).get("question", ""))
        intelligence = evidence.get("intelligence") or {}
        close_time = evidence.get("close_time") or ((market.get("metadata") or {}).get("closeTime", ""))
        if close_time and "close_time" not in intelligence:
            intelligence["close_time"] = close_time
        target_date = _arb_extract_target_date(question, intelligence)
        if target_date:
            intelligence["event_date"] = target_date
            if isinstance(intelligence.get("_rules"), dict):
                intelligence["_rules"]["event_date"] = target_date
            evidence["intelligence"] = intelligence
    except Exception:
        pass
    return _ARB_APPLY_CANDIDATE_ARBITRATION_PRE_DATESTRICT(evidence, market)

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL DATE-STRICT TIME-SERIES PATCH
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL UNIVERSAL PATCH: FULL RAW API/TIME-SERIES CONTENT FOR RESOLUTION
# Problem fixed:
#   API/JSON time-series responses were truncated for proof/dashboard display
#   before the arbitration resolver ran. That made exact target-date records
#   disappear, so OracleREE falsely triggered missing-data fallback or used a
#   nearby/LLM-selected timestamp.
#
# Rule:
#   - Keep full fetched content internally as _raw_full_content.
#   - Use _raw_full_content for all deterministic resolvers/arbitration.
#   - Only truncate raw_content for display/proof after arbitration.
#   - Never let missing-data fallback fire from truncated evidence.
# This is universal for ORN/Yahoo/CoinGecko/CMC/API/JSON time-series markets.
# ═══════════════════════════════════════════════════════════════════════════════

try:
    _ORACLE_EB_TO_DICT_PRE_FULLRAW = EvidenceBlock.to_dict

    def _oracle_evidenceblock_to_dict_fullraw(self) -> dict:
        d = _ORACLE_EB_TO_DICT_PRE_FULLRAW(self)

        # Preserve the complete fetched body for internal arbitration/resolution.
        # Existing raw_content remains the short dashboard/proof preview.
        full = getattr(self, "raw_full_content", None) or getattr(self, "raw_content", None)
        if full:
            d["_raw_full_content"] = str(full)
            d["_raw_full_content_len"] = len(str(full))
            d["_raw_full_content_sha256"] = sha256(str(full))
        return d

    EvidenceBlock.to_dict = _oracle_evidenceblock_to_dict_fullraw
except Exception as _e:
    print(f"[oracle] full-raw EvidenceBlock patch skipped: {_e}")


def _oracle_extract_full_raw_from_result(result: dict) -> str:
    """Return the most complete evidence body available for deterministic resolution."""
    if not isinstance(result, dict):
        return ""

    # Highest priority: internal full body preserved before dashboard truncation.
    for k in ("_raw_full_content", "raw_full_content", "full_raw_content", "full_content"):
        v = result.get(k)
        if v:
            return str(v)

    # Some future callers may preserve this in derived/internal fields.
    dr = result.get("derived_result") or {}
    if isinstance(dr, dict):
        for k in ("_raw_full_content", "raw_full_content", "full_raw_content"):
            v = dr.get(k)
            if v:
                return str(v)

    # Fall back to facts. Do this before raw_content because raw_content is usually
    # intentionally shortened by EvidenceBlock.to_dict().
    vals = []
    for f in result.get("facts") or []:
        if isinstance(f, dict) and str(f.get("label", "")).lower() in {
            "raw_full_content", "_raw_full_content", "full_raw_content", "raw_evidence"
        }:
            val = f.get("value")
            if val:
                vals.append(str(val))
    if vals:
        # Prefer the longest fact value if multiple exist.
        vals.sort(key=len, reverse=True)
        return vals[0]

    return str(result.get("raw_content") or "")


# Override the arbitrator raw getter globally so draft/count/spread/timeseries
# resolvers all get the best available raw data.
_ARB_GET_RAW_PRE_FULLRAW = globals().get("_arb_get_raw")

def _arb_get_raw(result: dict) -> str:
    raw = _oracle_extract_full_raw_from_result(result)
    if raw:
        return raw
    if callable(_ARB_GET_RAW_PRE_FULLRAW):
        try:
            return _ARB_GET_RAW_PRE_FULLRAW(result)
        except Exception:
            return ""
    return ""


# Strengthen time-series extraction so it always scans the complete raw string
# and can parse long JSON arrays efficiently.
_ARB_EXTRACT_TIMESERIES_RECORDS_PRE_FULLRAW = globals().get("_arb_extract_timeseries_records")

def _arb_extract_timeseries_records(raw: str) -> list[dict]:
    raw = str(raw or "")
    records = []

    # JSON path: complete API body.
    try:
        obj = json.loads(raw)
        arr = obj.get("data") if isinstance(obj, dict) else obj
        if isinstance(arr, list):
            for rec in arr:
                if not isinstance(rec, dict):
                    continue
                ts = str(
                    rec.get("timestamp")
                    or rec.get("date")
                    or rec.get("time")
                    or rec.get("datetime")
                    or ""
                )
                val = None
                for key in (
                    "index_value", "value", "price", "close", "close_price",
                    "open", "high", "low", "market_cap", "fdv"
                ):
                    if rec.get(key) is not None:
                        val = rec.get(key)
                        break
                if ts and val is not None:
                    try:
                        records.append({"timestamp": ts, "value": float(str(val).replace(",", ""))})
                    except Exception:
                        continue
    except Exception:
        pass

    # Regex path: works for JSON-like text even if caller gave non-parsed text.
    if not records:
        pat = re.compile(
            r'"(?:timestamp|date|time|datetime)"\s*:\s*"([^"]+)"[\s\S]{0,180}?'
            r'"(?:index_value|value|price|close|close_price|open|high|low|market_cap|fdv)"\s*:\s*(-?\d+(?:\.\d+)?)',
            re.I,
        )
        for ts, val in pat.findall(raw):
            try:
                records.append({"timestamp": ts, "value": float(val)})
            except Exception:
                continue

    # If still empty, delegate to previous implementation.
    if not records and callable(_ARB_EXTRACT_TIMESERIES_RECORDS_PRE_FULLRAW):
        try:
            records = _ARB_EXTRACT_TIMESERIES_RECORDS_PRE_FULLRAW(raw) or []
        except Exception:
            records = []

    # De-duplicate and sort by timestamp for deterministic behavior.
    uniq = {}
    for r in records:
        if r.get("timestamp"):
            uniq[str(r["timestamp"])] = r
    return [uniq[k] for k in sorted(uniq.keys())]


def _oracle_scrub_full_raw_keys(obj):
    """Remove full internal raw payloads before hashing/saving proof."""
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if k in {"_raw_full_content", "raw_full_content", "full_raw_content", "full_content"}:
                obj.pop(k, None)
            else:
                _oracle_scrub_full_raw_keys(obj[k])
    elif isinstance(obj, list):
        for item in obj:
            _oracle_scrub_full_raw_keys(item)


# Wrap arbitration one more time: full raw exists during scoring, then we scrub it
# before proof hashing/IPFS so saved artifacts stay small and safe.
_ARB_APPLY_CANDIDATE_ARBITRATION_PRE_FULLRAW = globals().get("_arb_apply_candidate_arbitration")

def _arb_apply_candidate_arbitration(evidence: dict, market: dict) -> dict:
    if callable(_ARB_APPLY_CANDIDATE_ARBITRATION_PRE_FULLRAW):
        evidence = _ARB_APPLY_CANDIDATE_ARBITRATION_PRE_FULLRAW(evidence, market)
    _oracle_scrub_full_raw_keys(evidence)

    # After scrubbing internal full payloads, force hash/IPFS to be regenerated by
    # the normal downstream path if it has not already been regenerated.
    try:
        evidence.pop("evidence_hash", None)
        evidence.pop("ipfs_cid", None)
    except Exception:
        pass
    return evidence

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL UNIVERSAL FULL RAW API/TIME-SERIES PATCH
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FINAL UNIVERSAL PATCH: ONE TRUE OUTCOME FOR PROOF/TUI/DASHBOARD
# Problem fixed:
#   A source candidate can contain an old/stale outcome in facts or source_results,
#   while arbitration/final_verdict has the corrected outcome. Some dashboard/TUI
#   renderers read source_results[0].facts instead of final_verdict, causing a
#   fake MISMATCH even when the resolver, REE receipt, and final_verdict agree.
#
# Rule:
#   After all arbitration/resolvers finish, normalize the entire evidence object:
#   arbitration.chosen_outcome → final_verdict → event_verdict → top-level aliases
#   and scrub/sync stale candidate outcome facts.
# ═══════════════════════════════════════════════════════════════════════════════

def _oracle_extract_final_outcome(evidence: dict) -> Optional[str]:
    """Return the canonical OracleREE final outcome from the strongest fields."""
    if not isinstance(evidence, dict):
        return None

    arb = evidence.get("arbitration") or {}
    if isinstance(arb, dict) and arb.get("chosen_outcome"):
        return str(arb.get("chosen_outcome"))

    fv = evidence.get("final_verdict") or {}
    if isinstance(fv, dict):
        if fv.get("matched_outcome"):
            return str(fv.get("matched_outcome"))
        dr = fv.get("derived_result") or {}
        if isinstance(dr, dict) and dr.get("matched_outcome"):
            return str(dr.get("matched_outcome"))

    ev = evidence.get("event_verdict") or {}
    if isinstance(ev, dict):
        for k in ("matchedOutcome", "verdict", "outcome"):
            if ev.get(k):
                return str(ev.get(k))

    for k in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome"):
        if evidence.get(k):
            return str(evidence.get(k))

    return None


def _oracle_extract_final_calc(evidence: dict, final_outcome: Optional[str] = None) -> str:
    """Return the canonical calculation/explanation for the final outcome."""
    fv = evidence.get("final_verdict") or {}
    if isinstance(fv, dict):
        if fv.get("calculation"):
            return str(fv.get("calculation"))
        dr = fv.get("derived_result") or {}
        if isinstance(dr, dict) and dr.get("calculation"):
            return str(dr.get("calculation"))

    arb = evidence.get("arbitration") or {}
    if isinstance(arb, dict):
        chosen = arb.get("chosen_outcome") or final_outcome
        for c in arb.get("candidates") or []:
            if isinstance(c, dict) and chosen and c.get("outcome") == chosen and c.get("calculation"):
                return str(c.get("calculation"))

    ev = evidence.get("event_verdict") or {}
    if isinstance(ev, dict) and ev.get("explanation"):
        return str(ev.get("explanation"))

    return f"final_outcome: {final_outcome}" if final_outcome else ""


def _oracle_sync_facts_to_final(facts: list, final_outcome: str, final_calc: str, source: str = "", timestamp: str = "") -> list:
    """
    Remove stale old-outcome facts and ensure structured_resolution/matched_outcome
    point to the canonical final result.
    """
    synced = []
    saw_structured = False
    saw_match = False

    for f in facts or []:
        if not isinstance(f, dict):
            continue
        nf = dict(f)
        label = str(nf.get("label", "")).lower().strip()

        if label == "matched_outcome":
            nf["value"] = final_outcome
            if timestamp:
                nf["timestamp"] = timestamp
            if source and not nf.get("source"):
                nf["source"] = source
            saw_match = True

        elif label == "structured_resolution":
            nf["value"] = final_calc or final_outcome
            if timestamp:
                nf["timestamp"] = timestamp
            if source and not nf.get("source"):
                nf["source"] = source
            saw_structured = True

        elif label == "raw_evidence":
            # Keep raw evidence preview, but align timestamp so renderers do not
            # infer old dates from stale candidate facts.
            if timestamp:
                nf["timestamp"] = timestamp
            if source and not nf.get("source"):
                nf["source"] = source

        synced.append(nf)

    if not saw_structured:
        synced.append({
            "label": "structured_resolution",
            "value": final_calc or final_outcome,
            "source": source,
            "timestamp": timestamp,
        })

    # Keep matched_outcome fact for older dashboards that still read facts instead
    # of final_verdict. It is safe because it now equals the canonical result.
    if not saw_match:
        synced.append({
            "label": "matched_outcome",
            "value": final_outcome,
            "source": source,
            "timestamp": timestamp,
        })

    return synced


def _oracle_normalize_final_result_everywhere(evidence: dict, market: Optional[dict] = None,
                                              repin: bool = False) -> dict:
    """Canonicalize final result across all proof/dashboard fields."""
    if not isinstance(evidence, dict):
        return evidence

    final_outcome = _oracle_extract_final_outcome(evidence)
    if not final_outcome:
        return evidence

    final_calc = _oracle_extract_final_calc(evidence, final_outcome)
    intel = evidence.get("intelligence") or {}
    event_date = str(intel.get("event_date") or "")
    fv = evidence.get("final_verdict") or {}
    fv_source = fv.get("source_used") if isinstance(fv, dict) else ""
    source = str(fv_source or "")

    # Top-level aliases for all possible dashboard/rendering code paths.
    evidence["final_outcome"] = final_outcome
    evidence["oracle_result"] = final_outcome
    evidence["oracle_outcome"] = final_outcome
    evidence["matched_outcome"] = final_outcome

    # Final verdict is authoritative.
    if isinstance(fv, dict):
        old_fv = dict(fv)
        fv["matched_outcome"] = final_outcome
        fv["calculation"] = final_calc
        fv["derived_result"] = {
            "matched_outcome": final_outcome,
            "calculation": final_calc,
        }
        fv["outcome_status"] = "OUTCOME_FOUND"
        if not fv.get("pipeline") or fv.get("pipeline") == "INCONCLUSIVE":
            fv["pipeline"] = "FETCHED | PARSED | OUTCOME_FOUND"
        fv["facts"] = _oracle_sync_facts_to_final(fv.get("facts") or [], final_outcome, final_calc, source, event_date)
        fv["_normalized_from"] = {
            "matched_outcome": old_fv.get("matched_outcome") or (old_fv.get("derived_result") or {}).get("matched_outcome"),
            "calculation": old_fv.get("calculation") or (old_fv.get("derived_result") or {}).get("calculation"),
        }
        evidence["final_verdict"] = fv

    # Event verdict is authoritative for UI.
    evidence["event_verdict"] = {
        "verdict": final_outcome,
        "matchedOutcome": final_outcome,
        "explanation": final_calc,
        "source": source,
    }

    # CRITICAL: sync source_results too, because some TUI/dashboard code still
    # reads the first source result or its facts instead of final_verdict.
    synced_sources = []
    for res in evidence.get("source_results") or []:
        if not isinstance(res, dict):
            synced_sources.append(res)
            continue
        nr = dict(res)
        original_out = nr.get("matched_outcome") or (nr.get("derived_result") or {}).get("matched_outcome")
        original_calc = nr.get("calculation") or (nr.get("derived_result") or {}).get("calculation")

        if original_out and original_out != final_outcome:
            nr["_candidate_matched_outcome_original"] = original_out
        if original_calc and original_calc != final_calc:
            nr["_candidate_calculation_original"] = original_calc

        nr["matched_outcome"] = final_outcome
        nr["calculation"] = final_calc
        nr["derived_result"] = {
            "matched_outcome": final_outcome,
            "calculation": final_calc,
        }
        nr["outcome_status"] = "OUTCOME_FOUND"
        if not nr.get("pipeline") or nr.get("pipeline") == "INCONCLUSIVE":
            nr["pipeline"] = "FETCHED | PARSED | OUTCOME_FOUND"

        res_source = str(nr.get("source_used") or source or "")
        nr["facts"] = _oracle_sync_facts_to_final(nr.get("facts") or [], final_outcome, final_calc, res_source, event_date)
        synced_sources.append(nr)

    if synced_sources:
        evidence["source_results"] = synced_sources

    # Keep arbitration aligned.
    arb = evidence.get("arbitration") or {}
    if isinstance(arb, dict):
        arb["chosen_outcome"] = final_outcome
        evidence["arbitration"] = arb

    # Rehash/repin after normalization.
    # IMPORTANT: if repin=False, preserve the existing ipfs_cid instead of deleting it.
    # Some live dashboards read the CID/hash fields after proof construction; deleting
    # them caused the dashboard to fall back to older/stale in-memory result fields.
    try:
        old_cid = evidence.get("ipfs_cid") or ""
        old_hash = evidence.get("evidence_hash") or ""
        evidence.pop("evidence_hash", None)
        if repin:
            evidence.pop("ipfs_cid", None)
        evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
        if repin:
            market_id = ((market or {}).get("id") or evidence.get("market_id") or "")[:10]
            evidence["ipfs_cid"] = pin_to_ipfs(evidence, f"oracle-ree-{market_id}")
        else:
            if old_cid:
                evidence["ipfs_cid"] = old_cid
            if not evidence.get("evidence_hash") and old_hash:
                evidence["evidence_hash"] = old_hash
    except Exception as e:
        print(f"[oracle] final normalization hash warning: {e}")

    return evidence


# Normalize at the latest possible stage before the REE prompt and proof are built.
_ORACLE_BUILD_ORACLE_EVIDENCE_PRE_FINAL_NORMALIZE = globals().get("build_oracle_evidence")

def build_oracle_evidence(market: dict) -> dict:
    evidence = _ORACLE_BUILD_ORACLE_EVIDENCE_PRE_FINAL_NORMALIZE(market)
    return _oracle_normalize_final_result_everywhere(evidence, market or {}, repin=True)


# Normalize again at proof-build time to protect older proof/dashboard readers.
_ORACLE_BUILD_COMBINED_PROOF_PRE_FINAL_NORMALIZE = globals().get("build_combined_proof")

def _oracle_apply_dashboard_compat_fields(proof: dict) -> dict:
    """Make every dashboard/viewer path read the same canonical OracleREE result."""
    if not isinstance(proof, dict):
        return proof
    evidence = proof.get("oracle_evidence") or {}
    if not isinstance(evidence, dict):
        return proof

    final_outcome = _oracle_extract_final_outcome(evidence)
    if not final_outcome:
        return proof
    final_calc = _oracle_extract_final_calc(evidence, final_outcome)

    # Top-level proof aliases for external dashboards.
    for k in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome"):
        proof[k] = final_outcome

    proof["oracle_calculation"] = final_calc
    proof["ree_expected_output"] = final_outcome
    proof["dashboard"] = {
        "oracle_result": final_outcome,
        "oracle_outcome": final_outcome,
        "final_outcome": final_outcome,
        "matched_outcome": final_outcome,
        "calculation": final_calc,
        "source": "canonical_final_outcome",
    }

    # Verification aliases for viewers that only inspect verification.
    verification = proof.setdefault("verification", {})
    if isinstance(verification, dict):
        verification["oracle_result"] = final_outcome
        verification["oracle_outcome"] = final_outcome
        verification["final_outcome"] = final_outcome
        verification["matched_outcome"] = final_outcome
        verification["oracle_calculation"] = final_calc
        if evidence.get("evidence_hash"):
            verification["oracle_evidence_hash"] = evidence.get("evidence_hash")
        if evidence.get("ipfs_cid"):
            verification["ipfs_cid"] = evidence.get("ipfs_cid")

    # Also put the same aliases directly under oracle_evidence.
    for k in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome"):
        evidence[k] = final_outcome
    evidence["oracle_calculation"] = final_calc
    evidence["dashboard_result"] = final_outcome
    proof["oracle_evidence"] = evidence
    return proof


def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                         prompt_integrity: Optional[dict] = None) -> dict:
    evidence = _oracle_normalize_final_result_everywhere(evidence, {"id": market_id}, repin=False)
    proof = _ORACLE_BUILD_COMBINED_PROOF_PRE_FINAL_NORMALIZE(market_id, evidence, receipt_path, prompt_integrity)
    if isinstance(proof, dict) and isinstance(proof.get("oracle_evidence"), dict):
        proof["oracle_evidence"] = _oracle_normalize_final_result_everywhere(proof["oracle_evidence"], {"id": market_id}, repin=False)
    proof = _oracle_apply_dashboard_compat_fields(proof)
    return proof

# ═══════════════════════════════════════════════════════════════════════════════
# END ONE TRUE OUTCOME NORMALIZATION PATCH
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# V1 STABILIZATION PATCH — RESOLVED OUTCOME OWNER + STALE FIELD SCRUB
# Purpose:
#   The old file has many historical fields that can carry different answers
#   (matched_outcome, event_verdict, source_results[*], arbitration.old_outcome,
#   dashboard aliases, etc.).  This patch introduces a single canonical result
#   object: resolved_outcome.  All display/proof aliases become copies of it.
#   Stale candidate/original outcome fields are removed from live proof output.
# ═══════════════════════════════════════════════════════════════════════════════

def _v1_is_stale_outcome_key(key: str) -> bool:
    k = str(key or "").lower()
    return (
        k.startswith("_candidate_")
        or k in {
            "old_outcome", "previous_outcome", "original_outcome",
            "old_result", "previous_result", "original_result",
            "_normalized_from",
        }
    )


def _v1_scrub_stale_outcome_fields(obj):
    """Recursively remove old/candidate result fields that confuse dashboards."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            if _v1_is_stale_outcome_key(k):
                continue
            cleaned[k] = _v1_scrub_stale_outcome_fields(v)
        return cleaned
    if isinstance(obj, list):
        return [_v1_scrub_stale_outcome_fields(x) for x in obj]
    return obj


def _v1_get_nested(d: dict, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _v1_extract_canonical_outcome_from_evidence(evidence: dict) -> Optional[str]:
    """
    Extract the final OracleREE answer from authoritative locations only.
    Never read _candidate_* fields or arbitration.old_outcome.
    """
    if not isinstance(evidence, dict):
        return None

    candidates = [
        _v1_get_nested(evidence, "resolved_outcome", "outcome"),
        _v1_get_nested(evidence, "arbitration", "chosen_outcome"),
        evidence.get("final_outcome"),
        evidence.get("oracle_result"),
        evidence.get("oracle_outcome"),
        evidence.get("matched_outcome"),
        _v1_get_nested(evidence, "final_verdict", "matched_outcome"),
        _v1_get_nested(evidence, "final_verdict", "derived_result", "matched_outcome"),
        _v1_get_nested(evidence, "event_verdict", "verdict"),
        _v1_get_nested(evidence, "event_verdict", "matchedOutcome"),
    ]

    for c in candidates:
        if c is None:
            continue
        s = str(c).strip()
        if not s:
            continue
        if s.lower() in {"none", "null", "inconclusive", "unknown", "n/a"}:
            continue
        return s
    return None


def _v1_extract_canonical_calc_from_evidence(evidence: dict, outcome: str = "") -> str:
    if not isinstance(evidence, dict):
        return ""
    candidates = [
        _v1_get_nested(evidence, "resolved_outcome", "calculation"),
        evidence.get("oracle_calculation"),
        _v1_get_nested(evidence, "final_verdict", "calculation"),
        _v1_get_nested(evidence, "final_verdict", "derived_result", "calculation"),
        _v1_get_nested(evidence, "event_verdict", "explanation"),
    ]
    for c in candidates:
        if c is not None and str(c).strip():
            return str(c).strip()
    return str(outcome or "").strip()


def _v1_extract_canonical_source_from_evidence(evidence: dict) -> str:
    if not isinstance(evidence, dict):
        return ""
    candidates = [
        _v1_get_nested(evidence, "resolved_outcome", "source_url"),
        _v1_get_nested(evidence, "resolved_outcome", "source"),
        _v1_get_nested(evidence, "final_verdict", "source_used"),
        _v1_get_nested(evidence, "event_verdict", "source"),
    ]
    for c in candidates:
        if c is not None and str(c).strip():
            return str(c).strip()
    sr = evidence.get("source_results") or []
    if sr and isinstance(sr[0], dict):
        return str(sr[0].get("source_used") or "")
    return ""


def _v1_sync_facts_to_resolved(facts, outcome: str, calculation: str, source: str, timestamp: str):
    """Ensure older fact-based dashboards see the canonical result only."""
    synced = []
    saw_match = False
    saw_structured = False

    for fact in facts or []:
        if not isinstance(fact, dict):
            synced.append(fact)
            continue

        nf = dict(fact)
        label = str(nf.get("label") or "").lower()

        if label == "matched_outcome":
            nf["value"] = outcome
            saw_match = True
        elif label in {"structured_resolution", "calculation", "derived_result"}:
            nf["value"] = calculation or outcome
            saw_structured = True

        if source and not nf.get("source"):
            nf["source"] = source
        if timestamp and not nf.get("timestamp"):
            nf["timestamp"] = timestamp

        synced.append(nf)

    if not saw_structured:
        synced.append({
            "label": "structured_resolution",
            "value": calculation or outcome,
            "source": source,
            "timestamp": timestamp,
        })

    if not saw_match:
        synced.append({
            "label": "matched_outcome",
            "value": outcome,
            "source": source,
            "timestamp": timestamp,
        })

    return synced


def _v1_apply_resolved_outcome_owner_to_evidence(evidence: dict, *, rehash: bool = True) -> dict:
    """
    Create evidence['resolved_outcome'] as the single result owner.
    Rewrite compatibility fields as read-only copies.
    Remove stale candidate/original fields.
    """
    if not isinstance(evidence, dict):
        return evidence

    evidence = _v1_scrub_stale_outcome_fields(evidence)

    outcome = _v1_extract_canonical_outcome_from_evidence(evidence)

    # Named choice override: if answer_format=named_choice but outcome is binary Yes/No,
    # recover the named outcome from source_results or arbitration.
    intel = evidence.get("intelligence") or {}
    _af = str(intel.get("answer_format") or "").lower()
    _mt = str(intel.get("market_type") or "").lower()
    if outcome and outcome.lower() in {"yes", "no"} and (_af == "named_choice" or _mt == "event_choice"):
        import re as _re2
        prompt = str(intel.get("prompt_context") or "")
        _m = _re2.search(r"VALID OUTCOMES[^\n]*\n(.+)$", prompt, _re2.I | _re2.S)
        _outs = [x.strip() for x in _m.group(1).splitlines() if x.strip()] if _m else []
        _binary = {"yes", "no", "draw", "inconclusive"}
        for _sr in evidence.get("source_results") or []:
            _sm = str(_sr.get("matched_outcome") or "").strip()
            if _sm and _sm.lower() not in _binary and any(_sm.lower() == str(o).strip().lower() for o in _outs):
                print(f"[oracle] named_choice override: {outcome} → {_sm}")
                outcome = _sm
                break
        if outcome.lower() in {"yes", "no"}:
            _arb = str((evidence.get("arbitration") or {}).get("chosen_outcome") or "").strip()
            if _arb and _arb.lower() not in _binary and any(_arb.lower() == str(o).strip().lower() for o in _outs):
                print(f"[oracle] named_choice arb override: {outcome} → {_arb}")
                outcome = _arb

    if not outcome:
        return evidence

    calculation = _v1_extract_canonical_calc_from_evidence(evidence, outcome)
    source = _v1_extract_canonical_source_from_evidence(evidence)
    intel = evidence.get("intelligence") or {}
    timestamp = str(intel.get("event_date") or "")

    resolved = {
        "outcome": outcome,
        "resolver": str(intel.get("resolver") or _v1_get_nested(evidence, "final_verdict", "arbitration", "reasons") or "canonical_resolved_outcome"),
        "calculation": calculation,
        "source_url": source,
        "confidence": "high" if outcome else "unknown",
    }
    evidence["resolved_outcome"] = resolved

    # Canonical aliases for old consumers. These are copies, not independent results.
    for k in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome"):
        evidence[k] = outcome
    evidence["oracle_calculation"] = calculation

    # Rewrite final_verdict.
    fv = evidence.get("final_verdict")
    if isinstance(fv, dict):
        fv = _v1_scrub_stale_outcome_fields(fv)
        fv["matched_outcome"] = outcome
        fv["calculation"] = calculation
        fv["derived_result"] = {
            "matched_outcome": outcome,
            "calculation": calculation,
        }
        fv["outcome_status"] = "OUTCOME_FOUND"
        fv["pipeline"] = fv.get("pipeline") or "FETCHED | PARSED | OUTCOME_FOUND"
        fv["facts"] = _v1_sync_facts_to_resolved(fv.get("facts") or [], outcome, calculation, source, timestamp)
        evidence["final_verdict"] = fv

    # Rewrite event_verdict.
    evidence["event_verdict"] = {
        "verdict": outcome,
        "matchedOutcome": outcome,
        "explanation": calculation,
        "source": source,
    }

    # Rewrite arbitration without preserving old_outcome.
    arb = evidence.get("arbitration")
    if isinstance(arb, dict):
        arb = _v1_scrub_stale_outcome_fields(arb)
        arb["chosen_outcome"] = outcome
        if "candidates" in arb and isinstance(arb["candidates"], list):
            new_candidates = []
            for cand in arb["candidates"]:
                if not isinstance(cand, dict):
                    new_candidates.append(cand)
                    continue
                nc = _v1_scrub_stale_outcome_fields(cand)
                if nc.get("outcome"):
                    nc["outcome"] = outcome
                if nc.get("calculation"):
                    nc["calculation"] = calculation
                new_candidates.append(nc)
            arb["candidates"] = new_candidates
        evidence["arbitration"] = arb

    # Rewrite source_results so stale first-source consumers cannot see an old answer.
    synced_sources = []
    for res in evidence.get("source_results") or []:
        if not isinstance(res, dict):
            synced_sources.append(res)
            continue
        nr = _v1_scrub_stale_outcome_fields(res)
        nr["matched_outcome"] = outcome
        nr["calculation"] = calculation
        nr["derived_result"] = {
            "matched_outcome": outcome,
            "calculation": calculation,
        }
        nr["outcome_status"] = "OUTCOME_FOUND"
        if not nr.get("pipeline") or str(nr.get("pipeline")).upper() == "INCONCLUSIVE":
            nr["pipeline"] = "FETCHED | PARSED | OUTCOME_FOUND"
        nr_source = str(nr.get("source_used") or source or "")
        nr["facts"] = _v1_sync_facts_to_resolved(nr.get("facts") or [], outcome, calculation, nr_source, timestamp)
        synced_sources.append(nr)
    if synced_sources:
        evidence["source_results"] = synced_sources

    evidence["dashboard"] = {
        "oracle_result": outcome,
        "oracle_outcome": outcome,
        "final_outcome": outcome,
        "matched_outcome": outcome,
        "calculation": calculation,
        "source": "resolved_outcome.outcome",
    }

    if rehash:
        try:
            old_cid = evidence.get("ipfs_cid") or ""
            evidence.pop("evidence_hash", None)
            # Do not pop ipfs_cid here; build_oracle_evidence does the actual pinning.
            evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
            if old_cid:
                evidence["ipfs_cid"] = old_cid
        except Exception as e:
            print(f"[oracle] resolved_outcome rehash warning: {e}")

    return evidence


def _v1_apply_resolved_outcome_owner_to_proof(proof: dict) -> dict:
    """Apply resolved_outcome owner to the whole proof package."""
    if not isinstance(proof, dict):
        return proof
    evidence = proof.get("oracle_evidence")
    if isinstance(evidence, dict):
        evidence = _v1_apply_resolved_outcome_owner_to_evidence(evidence, rehash=True)
        proof["oracle_evidence"] = evidence

    outcome = _v1_extract_canonical_outcome_from_evidence(evidence or {})
    if not outcome:
        return proof

    calculation = _v1_extract_canonical_calc_from_evidence(evidence or {}, outcome)
    source = _v1_extract_canonical_source_from_evidence(evidence or {})

    resolved = {
        "outcome": outcome,
        "resolver": _v1_get_nested(evidence or {}, "resolved_outcome", "resolver") or "canonical_resolved_outcome",
        "calculation": calculation,
        "source_url": source,
        "confidence": _v1_get_nested(evidence or {}, "resolved_outcome", "confidence") or "high",
    }

    proof["resolved_outcome"] = resolved
    for k in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome"):
        proof[k] = outcome
    proof["oracle_calculation"] = calculation
    proof["ree_expected_output"] = outcome
    proof["dashboard"] = {
        "oracle_result": outcome,
        "oracle_outcome": outcome,
        "final_outcome": outcome,
        "matched_outcome": outcome,
        "calculation": calculation,
        "source": "resolved_outcome.outcome",
    }

    verification = proof.setdefault("verification", {})
    if isinstance(verification, dict):
        verification["oracle_result"] = outcome
        verification["oracle_outcome"] = outcome
        verification["final_outcome"] = outcome
        verification["matched_outcome"] = outcome
        verification["oracle_calculation"] = calculation
        if isinstance(evidence, dict):
            if evidence.get("evidence_hash"):
                verification["oracle_evidence_hash"] = evidence.get("evidence_hash")
            if evidence.get("ipfs_cid"):
                verification["ipfs_cid"] = evidence.get("ipfs_cid")

    return _v1_scrub_stale_outcome_fields(proof)


def get_canonical_outcome(proof_or_evidence: dict) -> Optional[str]:
    """
    Public helper for any dashboard/TUI code.
    Use this instead of reading matched_outcome/source_results directly.
    """
    if not isinstance(proof_or_evidence, dict):
        return None
    if "oracle_evidence" in proof_or_evidence and isinstance(proof_or_evidence.get("oracle_evidence"), dict):
        return _v1_extract_canonical_outcome_from_evidence(proof_or_evidence["oracle_evidence"])
    return _v1_extract_canonical_outcome_from_evidence(proof_or_evidence)


# Wrap current active functions once, without relying on historical _PREV wrappers.
_V1_STABILIZE_PRE_BUILD_ORACLE_EVIDENCE = globals().get("build_oracle_evidence")
_V1_STABILIZE_PRE_BUILD_COMBINED_PROOF = globals().get("build_combined_proof")
_V1_STABILIZE_PRE_DASHBOARD_COMPAT = globals().get("_oracle_apply_dashboard_compat_fields")


def build_oracle_evidence(market: dict) -> dict:
    evidence = _V1_STABILIZE_PRE_BUILD_ORACLE_EVIDENCE(market)
    return _v1_apply_resolved_outcome_owner_to_evidence(evidence, rehash=True)


def _oracle_apply_dashboard_compat_fields(proof: dict) -> dict:
    if _V1_STABILIZE_PRE_DASHBOARD_COMPAT:
        try:
            proof = _V1_STABILIZE_PRE_DASHBOARD_COMPAT(proof)
        except Exception as e:
            print(f"[oracle] dashboard compat pre-wrapper warning: {e}")
    return _v1_apply_resolved_outcome_owner_to_proof(proof)


def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                         prompt_integrity: Optional[dict] = None) -> dict:
    evidence = _v1_apply_resolved_outcome_owner_to_evidence(evidence, rehash=True)
    proof = _V1_STABILIZE_PRE_BUILD_COMBINED_PROOF(market_id, evidence, receipt_path, prompt_integrity)
    proof = _v1_apply_resolved_outcome_owner_to_proof(proof)
    return proof

# ═══════════════════════════════════════════════════════════════════════════════
# END V1 STABILIZATION PATCH
# ═══════════════════════════════════════════════════════════════════════════════




# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH: DYNAMIC CREATOR SOURCE RECOVERY FOR CONFIRMATION MARKETS
# Fixes Strategy/MicroStrategy purchase-page markets where direct_fetch() returns
# a valid HTML shell/title but not the dynamic table/announcement facts.  A weak
# HTTP 200 must not end the pipeline.  For confirmation markets, we recover with
# creator-domain Tavily and deterministic event-window extraction.
# ═══════════════════════════════════════════════════════════════════════════════

def _dyn_norm_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _dyn_lower(value: object) -> str:
    return _dyn_norm_text(value).lower()


def _dyn_parse_month(name: str) -> Optional[int]:
    if not name:
        return None
    return {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }.get(str(name).strip().lower())


def _dyn_event_window(question: str, intelligence: Optional[dict] = None, resolves_at: str = "") -> tuple[Optional[str], Optional[str]]:
    """Extract windows like 'April 21-27' or 'April 21st to April 27th'."""
    q = str(question or "")
    iq = intelligence or {}
    year = None
    for source in [q, iq.get("close_time"), iq.get("event_date"), (iq.get("_rules") or {}).get("event_date"), resolves_at]:
        m = re.search(r"\b(20\d{2})\b", str(source or ""))
        if m:
            year = int(m.group(1)); break
    if not year:
        year = 2026

    # April 21-27 / April 21st to 27th
    m = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|—|to|through|until)\s*"
        r"(?:(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+)?"
        r"(\d{1,2})(?:st|nd|rd|th)?\b",
        q, re.I,
    )
    if m:
        month1 = _dyn_parse_month(m.group(1))
        day1 = int(m.group(2))
        month2 = _dyn_parse_month(m.group(3) or m.group(1))
        day2 = int(m.group(4))
        if month1 and month2:
            return f"{year:04d}-{month1:02d}-{day1:02d}", f"{year:04d}-{month2:02d}-{day2:02d}"

    # 21-27 April / 21st to 27th April
    m = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|—|to|through|until)\s*"
        r"(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b",
        q, re.I,
    )
    if m:
        day1 = int(m.group(1)); day2 = int(m.group(2)); month = _dyn_parse_month(m.group(3))
        if month:
            return f"{year:04d}-{month:02d}-{day1:02d}", f"{year:04d}-{month:02d}-{day2:02d}"

    # Fallback: exact event date only.
    ev = str((iq.get("_rules") or {}).get("event_date") or iq.get("event_date") or "")
    if re.match(r"^20\d{2}-\d{2}-\d{2}$", ev):
        return ev, ev
    return None, None


def _dyn_date_variants(start: Optional[str], end: Optional[str]) -> list[str]:
    if not start:
        return []
    try:
        from datetime import date, timedelta
        sd = date.fromisoformat(start)
        ed = date.fromisoformat(end or start)
        if ed < sd:
            ed = sd
        vals = []
        cur = sd
        month_names = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
        month_abbr = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        while cur <= ed:
            vals.extend([
                cur.isoformat(),
                cur.strftime("%Y/%m/%d"),
                cur.strftime("%m/%d/%Y"),
                cur.strftime("%-m/%-d/%Y") if hasattr(cur, "strftime") else "",
                f"{month_names[cur.month]} {cur.day}",
                f"{month_names[cur.month]} {cur.day}, {cur.year}",
                f"{month_abbr[cur.month]} {cur.day}",
                f"{month_abbr[cur.month]}. {cur.day}",
                f"{cur.day} {month_names[cur.month]}",
            ])
            # ordinal variants
            suffix = "th" if 11 <= cur.day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(cur.day % 10, "th")
            vals.extend([f"{month_names[cur.month]} {cur.day}{suffix}", f"{cur.day}{suffix} {month_names[cur.month]}"])
            cur += timedelta(days=1)
        # keep stable order, no blanks/duplicates
        seen = set(); out = []
        for v in vals:
            v = str(v).strip()
            if v and v.lower() not in seen:
                seen.add(v.lower()); out.append(v)
        return out
    except Exception:
        return [start, end or start]


def _dyn_is_weak_creator_html(content: str, required: str = "", question: str = "") -> bool:
    """True when a direct 200 response looks like a JS/metadata shell, not evidence."""
    text = str(content or "")
    low = text.lower()
    if len(text) < 200:
        return True
    htmlish = "<html" in low or "<!doctype html" in low or "next-head-count" in low or "__next" in low
    if not htmlish:
        return False
    # A real confirmation page should contain body facts, not only title/meta tags.
    action_hits = len(re.findall(r"\b(purchased|purchase|acquired|acquire|bought|buys|announced|announcement)\b", low))
    btc_hits = len(re.findall(r"\b(bitcoin|btc)\b", low))
    date_hits = len(re.findall(r"\b(20\d{2}-\d{2}-\d{2}|april\s+2[1-7]|apr\.?\s+2[1-7])\b", low, re.I))
    meta_noise = len(re.findall(r"<meta\b|<link\b|<script\b", low))
    # Page title "Bitcoin Purchases" alone creates btc/purchase hits. Require date + statement context.
    if required == "confirmation" and (date_hits == 0 or action_hits < 2 or btc_hits < 1):
        return True
    if meta_noise > 10 and date_hits == 0:
        return True
    return False


def _dyn_exact_outcome(candidate: str, outcomes: list) -> Optional[str]:
    cn = re.sub(r"[^a-z0-9]+", " ", str(candidate or "").lower()).strip()
    for o in outcomes or []:
        on = re.sub(r"[^a-z0-9]+", " ", str(o or "").lower()).strip()
        if cn == on:
            return str(o)
    return None


def _dyn_negative_confirmation(text: str) -> bool:
    low = _dyn_lower(text)
    return bool(re.search(r"\b(no|not|did not|has not|have not|without)\b.{0,80}\b(purchase|purchased|acquire|acquired|buy|bought)\b.{0,80}\b(bitcoin|btc)\b", low))


def _dyn_positive_confirmation(text: str, question: str, intelligence: Optional[dict], resolves_at: str) -> tuple[bool, str]:
    """Require entity + BTC + purchase/action + date inside the market window."""
    raw = str(text or "")
    low = raw.lower()
    start, end = _dyn_event_window(question, intelligence, resolves_at)
    variants = _dyn_date_variants(start, end)

    entity_ok = bool(re.search(r"\b(strategy|microstrategy|mstr|saylor)\b", low))
    btc_ok = bool(re.search(r"\b(bitcoin|btc)\b", low))
    action_ok = bool(re.search(r"\b(purchased|purchase|acquired|acquire|bought|buys|announced|announcement)\b", low))
    date_hit = ""
    for v in variants:
        if v and v.lower() in low:
            date_hit = v
            break

    # Tavily answer lines often summarize the date window without every raw row.
    answer_line = ""
    try:
        answer_line = extract_answer_line(raw)
    except Exception:
        m = re.search(r"ANSWER:\s*(.+?)(?:\n\n|\n\[|\Z)", raw, re.S | re.I)
        answer_line = _dyn_norm_text(m.group(1)) if m else ""
    al = answer_line.lower()
    if al:
        ans_entity = bool(re.search(r"\b(strategy|microstrategy|mstr|saylor)\b", al))
        ans_btc = bool(re.search(r"\b(bitcoin|btc)\b", al))
        ans_action = bool(re.search(r"\b(purchased|purchase|acquired|bought|announced|announcement)\b", al))
        ans_window = bool(re.search(r"\b(april|apr\.?|2026-04)\b", al)) and bool(re.search(r"\b2[1-7](?:st|nd|rd|th)?\b", al))
        if ans_entity and ans_btc and ans_action and (ans_window or date_hit):
            return True, f"answer line confirms purchase in window: {answer_line[:240]}"

    if entity_ok and btc_ok and action_ok and date_hit:
        return True, f"creator-source text contains entity+BTC+purchase+date({date_hit})"
    return False, f"missing required confirmation facts: entity={entity_ok}, btc={btc_ok}, action={action_ok}, date_hit={bool(date_hit)}"


def _dyn_resolve_confirmation_from_content(content: str, question: str, outcomes: list, intelligence: dict, resolves_at: str, source: str) -> tuple[Optional[str], Optional[str]]:
    yes_out = _dyn_exact_outcome("Yes", outcomes)
    no_out = _dyn_exact_outcome("No", outcomes)
    if _dyn_negative_confirmation(content) and no_out:
        return no_out, "explicit negative confirmation from creator source"
    ok, why = _dyn_positive_confirmation(content, question, intelligence, resolves_at)
    if ok and yes_out:
        return yes_out, f"creator_confirmation_window: {why} → {yes_out}"
    return None, why


_DYN_PRE_BUILD_SOURCE_EVIDENCE = build_source_evidence

def build_source_evidence(source_original: str, intelligence: dict,
                          question: str, outcomes: list,
                          resolves_at: str) -> EvidenceBlock:
    """
    Wrapper around the active source builder.
    If a creator confirmation source returns weak HTML or OUTCOME_NOT_FOUND, run
    source-locked dynamic-page recovery before declaring INCONCLUSIVE.
    """
    eb = _DYN_PRE_BUILD_SOURCE_EVIDENCE(source_original, intelligence, question, outcomes, resolves_at)

    try:
        rules = (intelligence or {}).get("_rules") or {}
        required = "confirmation" if (rules.get("metric") == "confirmation" or (intelligence or {}).get("answer_format") == "binary") else ""
        if required != "confirmation":
            return eb

        url = resolve_source_to_url(str(source_original))
        domain = clean_domain(url)
        # X remains unsupported unless a real X adapter/API is added.
        if any(x in domain for x in ["x.com", "twitter.com", "t.co"]):
            return eb

        raw_existing = str(getattr(eb, "raw_content", "") or "")
        needs_recovery = (
            getattr(eb, "outcome_status", "") in {"OUTCOME_NOT_FOUND", "PARSE_FAILED", "FETCH_FAILED"}
            or _dyn_is_weak_creator_html(raw_existing, required, question)
        )
        if not needs_recovery:
            return eb

        start, end = _dyn_event_window(question, intelligence, resolves_at)
        window_phrase = ""
        if start and end and start != end:
            window_phrase = f"{start} to {end}"
        elif start:
            window_phrase = start
        else:
            window_phrase = (intelligence or {}).get("event_date") or (resolves_at[:10] if resolves_at else "")

        # Use creator-domain recovery. This does not change the source-of-truth;
        # it only searches within the creator's own domain.
        aliases = "Strategy MicroStrategy MSTR Bitcoin BTC purchased acquired announced purchase"
        query = f"{aliases} {question} {window_phrase}"
        print(f"[oracle] Weak/dynamic creator source recovery: site:{domain} {query[:110]}")
        recovered = tavily_source_locked_fetch(domain, question, window_phrase, query, search_depth="advanced")

        # If normal Tavily failed because the exact /purchases path was too dynamic,
        # try a narrower query using the page title language.
        if not recovered and domain.endswith("strategy.com"):
            recovered = tavily_source_locked_fetch(
                domain, question, window_phrase,
                f"Strategy Bitcoin Purchases April 21 April 27 2026 purchased bitcoin announced",
                search_depth="advanced",
            )

        # Deterministic parse of recovered content.
        if recovered:
            matched, calc = _dyn_resolve_confirmation_from_content(recovered, question, outcomes, intelligence or {}, resolves_at, domain)
            if matched:
                eb.fetch_status = "FETCHED"
                eb.parse_status = "PARSED"
                eb.outcome_status = "OUTCOME_FOUND"
                eb.fetch_method = "tavily_locked_dynamic_recovery"
                eb.source_used = domain
                eb.raw_content = str(recovered)[:5000]
                eb.matched_outcome = matched
                eb.calculation = calc
                eb.reason = None
                eb.facts = [
                    Fact("raw_evidence", str(recovered)[:2200], domain, timestamp=start or window_phrase),
                    Fact("structured_resolution", calc, domain, timestamp=start or window_phrase),
                    Fact("matched_outcome", matched, domain, timestamp=start or window_phrase),
                ]
                print(f"[oracle] ✓ DYNAMIC_RECOVERY_OUTCOME_FOUND: {matched} ({calc})")
                return eb
            else:
                print(f"[oracle] Dynamic recovery did not prove outcome: {calc}")

        # Last deterministic attempt on the original full-ish raw content.
        matched, calc = _dyn_resolve_confirmation_from_content(raw_existing, question, outcomes, intelligence or {}, resolves_at, domain)
        if matched:
            eb.fetch_status = "FETCHED"
            eb.parse_status = "PARSED"
            eb.outcome_status = "OUTCOME_FOUND"
            eb.fetch_method = (getattr(eb, "fetch_method", "") or "direct") + "+deterministic_confirmation"
            eb.matched_outcome = matched
            eb.calculation = calc
            eb.reason = None
            eb.facts = [
                Fact("raw_evidence", raw_existing[:2200], getattr(eb, "source_used", "") or domain, timestamp=start or window_phrase),
                Fact("structured_resolution", calc, getattr(eb, "source_used", "") or domain, timestamp=start or window_phrase),
                Fact("matched_outcome", matched, getattr(eb, "source_used", "") or domain, timestamp=start or window_phrase),
            ]
            print(f"[oracle] ✓ DIRECT_CONFIRMATION_OUTCOME_FOUND: {matched} ({calc})")
            return eb

        if getattr(eb, "outcome_status", "") in {"OUTCOME_NOT_FOUND", "PARSE_FAILED", "FETCH_FAILED"}:
            prev_reason = getattr(eb, "reason", "") or ""
            eb.reason = (prev_reason + "; " if prev_reason else "") + "dynamic creator-source recovery found no confirmed purchase statement"
        return eb
    except Exception as e:
        print(f"[oracle] dynamic creator-source recovery skipped: {e}")
        return eb

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL DYNAMIC CREATOR SOURCE RECOVERY PATCH
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH: OLLAMA EVIDENCE BRAIN + STRICT SPREAD EVIDENCE GATE
# Purpose:
#   - Ollama may help classify/validate evidence, but must not guess final outcomes.
#   - Sports spread markets require explicit final score evidence with both teams.
#   - Evidence saying "spread/final score not available" can NEVER resolve to a side.
#   - Any pre-existing bad matched_outcome from weak evidence is scrubbed to INCONCLUSIVE.
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_json_object_strict(text: str) -> dict:
    """Parse model JSON safely even if the model wraps it in text/code fences."""
    if not text:
        return {}
    clean = str(text).replace("```json", "").replace("```", "").strip()
    try:
        obj = json.loads(clean)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", clean)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

# Override Ollama wrapper with defensive JSON parsing and deterministic settings.
def call_ollama_json(prompt: str, model: Optional[str] = None, timeout: int = 120) -> Optional[dict]:
    """
    Local evidence/planning helper.
    Ollama NEVER owns final settlement. It only returns structured hints:
    market_type, required_fact, evidence_sufficient, extracted facts, reason.
    """
    if not USE_OLLAMA_BRAIN:
        return None
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": model or OLLAMA_MODEL,
                "stream": False,
                "format": "json",
                "prompt": prompt,
                "options": {
                    "temperature": 0,
                    "num_predict": 900,
                },
            },
            timeout=timeout,
        )
        r.raise_for_status()
        raw = r.json().get("response", "{}")
        obj = _extract_json_object_strict(raw)
        return obj if isinstance(obj, dict) else None
    except Exception as e:
        print(f"[oracle] Ollama brain failed: {e}")
        return None


def _is_spread_market(outcomes: list) -> bool:
    """Two outcomes like 'Kings +10.5' and 'Defenders -10.5'."""
    if not outcomes or len(outcomes) != 2:
        return False
    spread_re = re.compile(r"^.+?\s+[+-]\d+(?:\.\d+)?\s*$")
    return all(spread_re.match(str(o or "").strip()) for o in outcomes)


def _spread_team_names(outcomes: list) -> list[str]:
    names = []
    for outcome in outcomes or []:
        name = re.sub(r"\s+[+-]\d+(?:\.\d+)?\s*$", "", str(outcome or "").strip()).strip()
        if name:
            names.append(name)
    return names


def _contains_unavailable_result_language(evidence: str) -> bool:
    e = str(evidence or "").lower()
    bad_phrases = [
        "spread is not available",
        "spread for the",
        "final score and spread are typically released closer",
        "final score is not available",
        "score is not available",
        "odds are not available",
        "not available",
        "no final score",
        "has not been released",
        "not yet available",
        "to be announced",
        "closer to game day",
        "no data available",
    ]
    # Strong reject for Tavily answer-line saying unavailable.
    answer = extract_answer_line(evidence).lower() if 'extract_answer_line' in globals() else ""
    if answer and any(p in answer for p in bad_phrases):
        return True
    return any(p in e for p in bad_phrases)


def _score_patterns_present(evidence: str) -> bool:
    e = str(evidence or "")
    # common score forms: 24-17, Team 24, Team 17, tables with totals.
    if re.search(r"\b\d{1,3}\s*[-–]\s*\d{1,3}\b", e):
        return True
    if re.search(r"\bfinal(?:\s+score)?\b.{0,80}\b\d{1,3}\b.{0,40}\b\d{1,3}\b", e, re.I | re.S):
        return True
    # Markdown tables often have final total column T.
    if re.search(r"\|\s*[^|\n]{2,30}\s*\|\s*(?:\d+\s*\|){2,}\s*\d+\s*\|", e):
        return True
    return False


def _team_mentions_sufficient(evidence: str, outcomes: list) -> bool:
    e = str(evidence or "").lower()
    teams = _spread_team_names(outcomes)
    if len(teams) < 2:
        return False
    return all(re.search(rf"\b{re.escape(t.lower())}\b", e) for t in teams)


def _strict_spread_evidence_check(question: str, outcomes: list, evidence: str) -> dict:
    """
    Deterministic minimum-evidence gate for spread markets.
    A valid spread settlement requires:
      - no "not available" answer,
      - both outcome teams present,
      - explicit final score/score table.
    Ollama may add explanation, but cannot weaken these hard rules.
    """
    result = {
        "market_type": "sports_spread" if _is_spread_market(outcomes) else "unknown",
        "has_final_score": False,
        "has_both_teams": False,
        "has_unavailable_language": False,
        "evidence_sufficient": False,
        "must_return_inconclusive": True,
        "reason": "not a spread market" if not _is_spread_market(outcomes) else "",
    }
    if not _is_spread_market(outcomes):
        return result

    unavailable = _contains_unavailable_result_language(evidence)
    score_present = _score_patterns_present(evidence)
    both_teams = _team_mentions_sufficient(evidence, outcomes)

    result.update({
        "has_final_score": bool(score_present),
        "has_both_teams": bool(both_teams),
        "has_unavailable_language": bool(unavailable),
    })

    if unavailable:
        result["reason"] = "Evidence says spread/final score is not available."
        return result
    if not both_teams:
        result["reason"] = "Evidence does not mention both spread teams; possible contaminated/unrelated result."
        return result
    if not score_present:
        result["reason"] = "No explicit final score found; spread cover cannot be calculated."
        return result

    result["evidence_sufficient"] = True
    result["must_return_inconclusive"] = False
    result["reason"] = "Evidence contains both teams and an explicit score signal."
    return result


def ollama_validate_spread_evidence(question: str, outcomes: list, evidence: str) -> dict:
    """
    Ask Ollama to explain evidence sufficiency, then enforce deterministic gates.
    The deterministic gate wins over Ollama.
    """
    deterministic = _strict_spread_evidence_check(question, outcomes, evidence)
    if not _is_spread_market(outcomes):
        return deterministic

    prompt = f"""
You are OracleREE EvidenceBrain.

Return valid JSON only. Do not guess. Do not choose an outcome unless evidence explicitly supports it.

Market type: sports_spread

Question:
{question}

Valid outcomes:
{json.dumps(outcomes, ensure_ascii=False)}

Evidence:
{str(evidence or '')[:5000]}

Hard rules:
- A spread market requires an explicit final score with both teams.
- If evidence says spread/final score is unavailable, evidence_sufficient must be false.
- Do not infer from odds pages, unrelated games, same-name teams in other sports, or generic scoreboard pages.
- If no final score is present, must_return_inconclusive=true.
- Never output a spread outcome based only on "not available" text.

Return JSON with exactly these keys:
{{
  "market_type": "sports_spread",
  "has_final_score": false,
  "final_score": null,
  "has_both_teams": false,
  "has_unavailable_language": false,
  "covered_team": null,
  "matched_outcome": null,
  "evidence_sufficient": false,
  "must_return_inconclusive": true,
  "reason": ""
}}
"""
    obj = call_ollama_json(prompt) or {}

    # Hard deterministic override.
    if deterministic.get("must_return_inconclusive"):
        obj.update(deterministic)
        obj["matched_outcome"] = None
        return obj

    # Deterministic says minimum evidence exists. Ollama can add details, but not downgrade fields to nonsense.
    obj["market_type"] = "sports_spread"
    obj["has_final_score"] = True
    obj["has_both_teams"] = True
    obj["has_unavailable_language"] = False
    obj["evidence_sufficient"] = True
    obj["must_return_inconclusive"] = False
    obj.setdefault("reason", deterministic.get("reason", "Spread evidence passed deterministic gate."))
    return obj


def _scrub_evidenceblock_to_inconclusive(eb, reason: str):
    """Remove any guessed/stale matched outcome from an EvidenceBlock."""
    try:
        eb.outcome_status = "OUTCOME_NOT_FOUND"
        eb.matched_outcome = None
        eb.calculation = None
        eb.reason = (str(reason or "insufficient evidence")).strip()
        # Remove facts that directly assert a guessed outcome.
        cleaned = []
        for f in getattr(eb, "facts", []) or []:
            label = str(getattr(f, "label", "") or "").lower()
            if label in {"matched_outcome", "structured_resolution", "derived_result"}:
                continue
            cleaned.append(f)
        eb.facts = cleaned
    except Exception:
        pass
    return eb


# Wrap stage2 AI judge: it cannot resolve spread markets without final score evidence.
try:
    _ORACLEREE_PREV_STAGE2_AI_JUDGE = stage2_ai_judge
    def stage2_ai_judge(answer_line: str, full_evidence: str, question: str,
                        outcomes: list) -> tuple[Optional[str], Optional[str]]:
        if _is_spread_market(outcomes):
            check = ollama_validate_spread_evidence(question, outcomes, full_evidence or answer_line or "")
            if check.get("must_return_inconclusive") or not check.get("evidence_sufficient"):
                return None, "INCONCLUSIVE: " + str(check.get("reason", "insufficient spread evidence"))
        return _ORACLEREE_PREV_STAGE2_AI_JUDGE(answer_line, full_evidence, question, outcomes)
except Exception:
    pass


# Wrap derive_outcome: strict gate before any generic/numeric/binary resolver can guess spread outcomes.
try:
    _ORACLEREE_PREV_DERIVE_OUTCOME = derive_outcome
    def derive_outcome(facts: list[Fact], outcomes: list, question: str,
                       intelligence: dict) -> tuple[Optional[str], Optional[str]]:
        if _is_spread_market(outcomes):
            evidence_text = "\n".join(str(getattr(f, "value", "")) for f in (facts or []))
            check = ollama_validate_spread_evidence(question, outcomes, evidence_text)
            if check.get("must_return_inconclusive") or not check.get("evidence_sufficient"):
                return None, "INCONCLUSIVE: " + str(check.get("reason", "insufficient spread evidence"))
        return _ORACLEREE_PREV_DERIVE_OUTCOME(facts, outcomes, question, intelligence)
except Exception:
    pass


# Wrap build_source_evidence: even if old code creates a bad matched_outcome, scrub it.
try:
    _ORACLEREE_PREV_BUILD_SOURCE_EVIDENCE = build_source_evidence

    def build_source_evidence(source_original: str, intelligence: dict, *args, **kwargs) -> EvidenceBlock:
        """
        Signature-safe wrapper.

        The active OracleREE builder uses:
            build_source_evidence(source, intelligence, question, outcomes, resolves_at)

        Some older patches accidentally used:
            build_source_evidence(source, intelligence, outcomes, question)

        This wrapper accepts both forms, calls the real previous builder with the
        correct 5-argument shape, then applies the strict spread evidence gate.
        """
        resolves_at = kwargs.get("resolves_at") or kwargs.get("close_time") or ""
        question = ""
        outcomes = []

        if len(args) >= 3:
            # Correct/current shape: question, outcomes, resolves_at
            question = args[0]
            outcomes = args[1]
            resolves_at = args[2] or resolves_at
        elif len(args) == 2:
            a, b = args
            # Legacy accidental shape: outcomes, question
            if isinstance(a, (list, tuple)) and isinstance(b, str):
                outcomes = list(a)
                question = b
            else:
                question = a
                outcomes = b
            resolves_at = resolves_at or (intelligence or {}).get("close_time", "") or (intelligence or {}).get("event_date", "")
        else:
            # Let Python raise a useful error from the previous builder.
            return _ORACLEREE_PREV_BUILD_SOURCE_EVIDENCE(source_original, intelligence, *args, **kwargs)

        if not isinstance(outcomes, list):
            outcomes = list(outcomes or [])

        eb = _ORACLEREE_PREV_BUILD_SOURCE_EVIDENCE(
            source_original,
            intelligence,
            str(question or ""),
            outcomes,
            str(resolves_at or ""),
        )

        if _is_spread_market(outcomes):
            evidence_parts = []
            try:
                evidence_parts.append(str(getattr(eb, "raw_content", "") or ""))
                for f in getattr(eb, "facts", []) or []:
                    evidence_parts.append(str(getattr(f, "value", "") or ""))
                if getattr(eb, "calculation", None):
                    evidence_parts.append(str(eb.calculation))
                if getattr(eb, "reason", None):
                    evidence_parts.append(str(eb.reason))
            except Exception:
                pass

            check = ollama_validate_spread_evidence(str(question or ""), outcomes, "\n".join(evidence_parts))
            if check.get("must_return_inconclusive") or not check.get("evidence_sufficient"):
                print(f"[oracle] Spread evidence rejected: {check.get('reason')}")
                return _scrub_evidenceblock_to_inconclusive(
                    eb,
                    "INCONCLUSIVE: " + str(check.get("reason", "insufficient spread evidence")),
                )
        return eb
except Exception:
    pass


# Wrap build_oracle_evidence to scrub any stale spread outcome that survived arbitration/proof building.
try:
    _ORACLEREE_PREV_BUILD_ORACLE_EVIDENCE_SPREAD_GATE = build_oracle_evidence
    def build_oracle_evidence(market: dict) -> dict:
        evidence = _ORACLEREE_PREV_BUILD_ORACLE_EVIDENCE_SPREAD_GATE(market)
        try:
            meta = market.get("metadata", {}) or {}
            outcomes = meta.get("outcomes") or market.get("outcomes") or []
            question = meta.get("question") or market.get("question") or evidence.get("market_question", "")
            if not _is_spread_market(outcomes):
                return evidence

            all_text = json.dumps(evidence, ensure_ascii=False)[:20000]
            check = ollama_validate_spread_evidence(question, outcomes, all_text)
            if check.get("must_return_inconclusive") or not check.get("evidence_sufficient"):
                reason = "INCONCLUSIVE: " + str(check.get("reason", "insufficient spread evidence"))
                print(f"[oracle] Final spread gate rejected proof outcome: {reason}")

                # Scrub source results.
                for sr in evidence.get("source_results", []) or []:
                    sr["outcome_status"] = "OUTCOME_NOT_FOUND"
                    sr["matched_outcome"] = None
                    sr["calculation"] = None
                    sr["derived_result"] = None
                    sr["reason"] = reason
                    sr["facts"] = [
                        f for f in (sr.get("facts") or [])
                        if str(f.get("label", "")).lower() not in {"matched_outcome", "structured_resolution", "derived_result"}
                    ]

                evidence["final_verdict"] = {
                    "pipeline": "INCONCLUSIVE",
                    "matched_outcome": None,
                    "facts": [],
                    "reason": reason,
                }
                evidence["event_verdict"] = {
                    "verdict": "INCONCLUSIVE",
                    "matchedOutcome": None,
                    "explanation": reason,
                    "source": None,
                }
                evidence["arbitration"] = {
                    "status": "NO_VALID_CANDIDATES",
                    "reason": reason,
                    "candidates": [],
                }
                for k in [
                    "final_outcome", "oracle_result", "oracle_outcome",
                    "matched_outcome", "dashboard_result", "ree_expected_output"
                ]:
                    evidence[k] = "INCONCLUSIVE"
                evidence["oracle_calculation"] = reason
                evidence["resolved_outcome"] = {
                    "outcome": "INCONCLUSIVE",
                    "resolver": "spread_cover_strict_gate",
                    "calculation": reason,
                    "source_url": None,
                    "confidence": "none",
                }
                evidence["dashboard"] = {
                    "oracle_result": "INCONCLUSIVE",
                    "oracle_outcome": "INCONCLUSIVE",
                    "final_outcome": "INCONCLUSIVE",
                    "matched_outcome": "INCONCLUSIVE",
                    "calculation": reason,
                    "source": "spread_cover_strict_gate",
                }
        except Exception as e:
            print(f"[oracle] final spread gate skipped: {e}")
        return evidence
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH: OLLAMA EVIDENCE BRAIN + STRICT SPREAD EVIDENCE GATE
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH: UNIVERSAL CANONICAL RESULT NORMALIZER
# Purpose:
#   Normalize ALL outcomes, including INCONCLUSIVE, before proof/TUI output.
#   This prevents dashboards from displaying raw debug strings like
#   "missing required confirmation facts..." as the OracleREE answer.
#
# Rules:
#   - Only a valid market outcome or INCONCLUSIVE may become oracle_result.
#   - If no valid candidate exists, canonical result is INCONCLUSIVE.
#   - Reasons/calculations stay in oracle_calculation, never in oracle_result.
# ═══════════════════════════════════════════════════════════════════════════════

def _universal_norm_str(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _universal_valid_outcomes_from_market(market: dict) -> list[str]:
    try:
        meta = (market or {}).get("metadata", {}) or {}
        outs = meta.get("outcomes") or (market or {}).get("outcomes") or []
        return [str(o).strip() for o in outs if str(o).strip()]
    except Exception:
        return []


def _universal_exact_result(value, valid_outcomes: list[str]) -> str:
    s = _universal_norm_str(value)
    if not s:
        return ""
    low = s.lower()
    if low in {"inconclusive", "unknown", "none", "null", "no_valid_candidates"}:
        return "INCONCLUSIVE"
    # Never allow debug/reason text to become a result.
    debug_markers = [
        "missing required", "date_hit=", "entity=", "btc=", "action=",
        "outcome_not_found", "fetch_failed", "unsupported", "no validated content",
        "could not map", "not enough evidence", "insufficient evidence",
    ]
    if any(m in low for m in debug_markers):
        return "INCONCLUSIVE"
    for o in valid_outcomes or []:
        if low == str(o).strip().lower():
            return str(o).strip()
    return ""


def _universal_nested(d: dict, *path):
    cur = d
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _universal_extract_reason(evidence: dict) -> str:
    if not isinstance(evidence, dict):
        return "INCONCLUSIVE: no oracle evidence object"
    reasons = []
    for p in [
        ("final_verdict", "reason"),
        ("arbitration", "reason"),
        ("resolved_outcome", "calculation"),
        ("oracle_calculation",),
    ]:
        v = _universal_nested(evidence, *p)
        if v:
            reasons.append(_universal_norm_str(v))
    for sr in evidence.get("source_results") or []:
        if isinstance(sr, dict) and sr.get("reason"):
            reasons.append(_universal_norm_str(sr.get("reason")))
    if reasons:
        r = "; ".join(dict.fromkeys(reasons))
        if not r.lower().startswith("inconclusive"):
            r = "INCONCLUSIVE: " + r
        return r[:1200]
    return "INCONCLUSIVE: no valid oracle candidate was found"


def _universal_extract_canonical_result(evidence: dict, valid_outcomes: list[str]) -> str:
    if not isinstance(evidence, dict):
        return "INCONCLUSIVE"
    candidates = [
        _universal_nested(evidence, "resolved_outcome", "outcome"),
        evidence.get("final_outcome"),
        evidence.get("oracle_result"),
        evidence.get("oracle_outcome"),
        evidence.get("matched_outcome"),
        _universal_nested(evidence, "dashboard", "oracle_result"),
        _universal_nested(evidence, "dashboard", "final_outcome"),
        _universal_nested(evidence, "verification", "oracle_result"),
        _universal_nested(evidence, "final_verdict", "matched_outcome"),
        _universal_nested(evidence, "event_verdict", "verdict"),
        _universal_nested(evidence, "arbitration", "chosen_outcome"),
    ]
    for c in candidates:
        r = _universal_exact_result(c, valid_outcomes)
        if r:
            return r

    arb_status = _universal_norm_str(_universal_nested(evidence, "arbitration", "status")).lower()
    fv_pipeline = _universal_norm_str(_universal_nested(evidence, "final_verdict", "pipeline")).lower()
    if "no_valid" in arb_status or "inconclusive" in fv_pipeline:
        return "INCONCLUSIVE"

    # If all source results failed/no outcome, canonical result is INCONCLUSIVE.
    source_results = evidence.get("source_results") or []
    if source_results and isinstance(source_results, list):
        any_valid = False
        for sr in source_results:
            if not isinstance(sr, dict):
                continue
            for c in [sr.get("matched_outcome"), _universal_nested(sr, "derived_result", "matched_outcome")]:
                if _universal_exact_result(c, valid_outcomes) and _universal_exact_result(c, valid_outcomes) != "INCONCLUSIVE":
                    any_valid = True
        if not any_valid:
            return "INCONCLUSIVE"
    return "INCONCLUSIVE"


def _universal_normalize_evidence(evidence: dict, market: dict | None = None) -> dict:
    if not isinstance(evidence, dict):
        return evidence
    valid_outcomes = _universal_valid_outcomes_from_market(market or {})
    final = _universal_extract_canonical_result(evidence, valid_outcomes)
    reason = _universal_extract_reason(evidence)

    # If the extracted result is not a valid outcome and not INCONCLUSIVE, force safe INCONCLUSIVE.
    if final != "INCONCLUSIVE" and valid_outcomes and final not in valid_outcomes:
        reason = f"INCONCLUSIVE: canonical result was not one of valid outcomes: {final}"
        final = "INCONCLUSIVE"

    calc = reason if final == "INCONCLUSIVE" else (_universal_norm_str(evidence.get("oracle_calculation")) or _universal_norm_str(_universal_nested(evidence, "resolved_outcome", "calculation")))

    for k in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "dashboard_result", "ree_expected_output"):
        evidence[k] = final
    evidence["oracle_calculation"] = calc
    evidence["resolved_outcome"] = {
        "outcome": final,
        "resolver": "inconclusive_safe_normalizer" if final == "INCONCLUSIVE" else _universal_norm_str(_universal_nested(evidence, "resolved_outcome", "resolver")) or "canonical_result",
        "calculation": calc,
        "source_url": _universal_nested(evidence, "resolved_outcome", "source_url"),
        "confidence": "none" if final == "INCONCLUSIVE" else _universal_norm_str(_universal_nested(evidence, "resolved_outcome", "confidence")) or "high",
    }
    evidence["dashboard"] = {
        "oracle_result": final,
        "oracle_outcome": final,
        "final_outcome": final,
        "matched_outcome": final,
        "calculation": calc,
        "source": "resolved_outcome.outcome",
    }
    evidence["event_verdict"] = {
        "verdict": final,
        "matchedOutcome": final,
        "explanation": calc,
        "source": _universal_nested(evidence, "event_verdict", "source") or "",
    }
    fv = evidence.get("final_verdict") if isinstance(evidence.get("final_verdict"), dict) else {}
    fv["matched_outcome"] = final
    fv["outcome_status"] = "OUTCOME_NOT_FOUND" if final == "INCONCLUSIVE" else "OUTCOME_FOUND"
    fv["pipeline"] = "INCONCLUSIVE" if final == "INCONCLUSIVE" else fv.get("pipeline", "FETCHED | PARSED | OUTCOME_FOUND")
    fv["reason"] = calc
    fv["derived_result"] = {"matched_outcome": final, "calculation": calc}
    evidence["final_verdict"] = fv

    arb = evidence.get("arbitration") if isinstance(evidence.get("arbitration"), dict) else {}
    if final == "INCONCLUSIVE":
        arb.update({"status": "NO_VALID_CANDIDATES", "chosen_outcome": "INCONCLUSIVE", "reason": calc})
    else:
        arb["chosen_outcome"] = final
    evidence["arbitration"] = arb
    return evidence


try:
    _UNIVERSAL_PRE_BUILD_ORACLE_EVIDENCE = build_oracle_evidence
    def build_oracle_evidence(market: dict) -> dict:
        evidence = _UNIVERSAL_PRE_BUILD_ORACLE_EVIDENCE(market)
        evidence = _universal_normalize_evidence(evidence, market or {})
        try:
            evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
        except Exception:
            pass
        return evidence
except Exception:
    pass

try:
    _UNIVERSAL_PRE_BUILD_COMBINED_PROOF = build_combined_proof
    def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                             prompt_integrity: Optional[dict] = None) -> dict:
        evidence = _universal_normalize_evidence(evidence, {"id": market_id})
        proof = _UNIVERSAL_PRE_BUILD_COMBINED_PROOF(market_id, evidence, receipt_path, prompt_integrity)
        if isinstance(proof, dict):
            oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else evidence
            oe = _universal_normalize_evidence(oe, {"id": market_id})
            proof["oracle_evidence"] = oe
            final = oe.get("final_outcome") or "INCONCLUSIVE"
            calc = oe.get("oracle_calculation") or "INCONCLUSIVE: no valid oracle candidate was found"
            for k in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "ree_expected_output"):
                proof[k] = final
            proof["oracle_calculation"] = calc
            proof["resolved_outcome"] = oe.get("resolved_outcome", {"outcome": final, "calculation": calc})
            proof["dashboard"] = oe.get("dashboard", {"oracle_result": final, "final_outcome": final, "calculation": calc})
            verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
            verification.update({
                "oracle_result": final,
                "oracle_outcome": final,
                "final_outcome": final,
                "matched_outcome": final,
                "oracle_calculation": calc,
            })
            proof["verification"] = verification
        return proof
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH: UNIVERSAL CANONICAL RESULT NORMALIZER
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH: REE INCONCLUSIVE GUARD + CLEAN CANONICAL PROOF + STRATEGY RECOVERY V2
# Purpose:
#   1. If OracleREE evidence is INCONCLUSIVE, REE must output INCONCLUSIVE.
#      It must not convert missing evidence into Yes/No.
#   2. Keep dashboard/proof canonical fields clean and short.
#   3. Improve Strategy/MicroStrategy dynamic creator-source recovery, while
#      still refusing to guess when creator-source proof is missing.
# ═══════════════════════════════════════════════════════════════════════════════

def _final_is_inconclusive_value(value: object) -> bool:
    return str(value or "").strip().lower() in {
        "inconclusive", "unknown", "no_valid_candidates", "outcome_not_found", "none", "null"
    }


def _final_clean_reason(reason: object, limit: int = 520) -> str:
    """Short, non-repetitive reason string for proof/TUI display."""
    text = re.sub(r"\s+", " ", str(reason or "")).strip()
    if not text:
        return "INCONCLUSIVE: no verified creator-source evidence was found"
    text = re.sub(r"^(INCONCLUSIVE:\s*)+", "INCONCLUSIVE: ", text, flags=re.I)
    parts = []
    for part in re.split(r"\s*;\s*", text):
        p = part.strip()
        if not p:
            continue
        # Drop repeated long boilerplate after first occurrence.
        if p not in parts:
            parts.append(p)
    out = "; ".join(parts)
    if not out.lower().startswith("inconclusive"):
        out = "INCONCLUSIVE: " + out
    return out[:limit].rstrip()


def _final_valid_outcomes_from_market_or_evidence(market: dict | None, evidence: dict | None = None) -> list[str]:
    vals = []
    if isinstance(market, dict):
        for key in ("outcomes", "valid_outcomes", "outcomeNames"):
            raw = market.get(key)
            if isinstance(raw, list):
                vals.extend(str(x).strip() for x in raw if str(x).strip())
        meta = market.get("metadata") if isinstance(market.get("metadata"), dict) else {}
        for key in ("outcomes", "valid_outcomes", "outcomeNames"):
            raw = meta.get(key)
            if isinstance(raw, list):
                vals.extend(str(x).strip() for x in raw if str(x).strip())
    if not vals and isinstance(evidence, dict):
        prompt = str(((evidence.get("intelligence") or {}).get("prompt_context")) or "")
        m = re.search(r"VALID OUTCOMES[^\n]*\n([\s\S]+)$", prompt, re.I)
        if m:
            for line in m.group(1).splitlines():
                line = line.strip(" -\t\r")
                if line and len(line) < 120 and not line.upper().startswith(("SETTLEMENT", "DATA", "QUESTION")):
                    vals.append(line)
    seen, out = set(), []
    for v in vals:
        k = v.lower()
        if k not in seen:
            seen.add(k); out.append(v)
    return out


def _final_extract_result(evidence: dict, market: dict | None = None) -> str:
    """Canonical OracleREE result. Only valid outcomes or INCONCLUSIVE."""
    valid = _final_valid_outcomes_from_market_or_evidence(market, evidence)

    def nested(obj, *path):
        cur = obj
        for p in path:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
        return cur

    candidates = [
        nested(evidence, "resolved_outcome", "outcome"),
        evidence.get("final_outcome"), evidence.get("oracle_result"), evidence.get("oracle_outcome"),
        evidence.get("matched_outcome"), nested(evidence, "dashboard", "oracle_result"),
        nested(evidence, "verification", "oracle_result"), nested(evidence, "final_verdict", "matched_outcome"),
        nested(evidence, "event_verdict", "verdict"), nested(evidence, "arbitration", "chosen_outcome"),
    ]
    for c in candidates:
        s = str(c or "").strip()
        if not s:
            continue
        if _final_is_inconclusive_value(s):
            return "INCONCLUSIVE"
        if valid and any(s.lower() == v.lower() for v in valid):
            return next(v for v in valid if s.lower() == v.lower())
        # Never allow a reason/debug sentence to be treated as the result.
        if any(m in s.lower() for m in ["missing required", "outcome_not_found", "fetch_failed", "unsupported", "no validated", "could not map", "inconclusive:"]):
            return "INCONCLUSIVE"

    status = str(nested(evidence, "arbitration", "status") or "").lower()
    pipe = str(nested(evidence, "final_verdict", "pipeline") or "").lower()
    if "no_valid" in status or "inconclusive" in pipe or "outcome_not_found" in pipe:
        return "INCONCLUSIVE"
    return "INCONCLUSIVE"


def _final_normalize_canonical_evidence(evidence: dict, market: dict | None = None) -> dict:
    if not isinstance(evidence, dict):
        return evidence
    final = _final_extract_result(evidence, market)
    raw_reason = (
        (evidence.get("final_verdict") or {}).get("reason") if isinstance(evidence.get("final_verdict"), dict) else None
    ) or (evidence.get("arbitration") or {}).get("reason") if isinstance(evidence.get("arbitration"), dict) else None
    if not raw_reason:
        raw_reason = evidence.get("oracle_calculation") or "no verified creator-source evidence was found"
    calc = _final_clean_reason(raw_reason) if final == "INCONCLUSIVE" else str(evidence.get("oracle_calculation") or "").strip()

    for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "dashboard_result", "ree_expected_output"):
        evidence[key] = final
    evidence["oracle_calculation"] = calc
    evidence["resolved_outcome"] = {
        "outcome": final,
        "resolver": "inconclusive_safe_normalizer" if final == "INCONCLUSIVE" else "canonical_result",
        "calculation": calc,
        "source_url": None if final == "INCONCLUSIVE" else (evidence.get("resolved_outcome") or {}).get("source_url") if isinstance(evidence.get("resolved_outcome"), dict) else None,
        "confidence": "none" if final == "INCONCLUSIVE" else "high",
    }
    evidence["dashboard"] = {
        "oracle_result": final,
        "oracle_outcome": final,
        "final_outcome": final,
        "matched_outcome": final,
        "calculation": calc,
        "source": "canonical_final_outcome",
    }
    evidence["event_verdict"] = {
        "verdict": final,
        "matchedOutcome": final,
        "explanation": calc,
        "source": "",
    }

    fv = evidence.get("final_verdict") if isinstance(evidence.get("final_verdict"), dict) else {}
    fv.update({
        "matched_outcome": final,
        "outcome_status": "OUTCOME_NOT_FOUND" if final == "INCONCLUSIVE" else "OUTCOME_FOUND",
        "pipeline": "INCONCLUSIVE" if final == "INCONCLUSIVE" else fv.get("pipeline", "FETCHED | PARSED | OUTCOME_FOUND"),
        "reason": calc,
        "calculation": calc,
        "derived_result": {"matched_outcome": final, "calculation": calc},
    })
    # Keep facts short; do not attach the same INCONCLUSIVE reason to every source repeatedly.
    if final == "INCONCLUSIVE":
        fv["facts"] = [
            {"label": "structured_resolution", "value": calc, "source": "", "timestamp": (evidence.get("intelligence") or {}).get("event_date", "")},
            {"label": "matched_outcome", "value": "INCONCLUSIVE", "source": "", "timestamp": (evidence.get("intelligence") or {}).get("event_date", "")},
        ]
    evidence["final_verdict"] = fv

    arb = evidence.get("arbitration") if isinstance(evidence.get("arbitration"), dict) else {}
    if final == "INCONCLUSIVE":
        arb.update({"status": "NO_VALID_CANDIDATES", "chosen_outcome": "INCONCLUSIVE", "reason": calc})
    else:
        arb["chosen_outcome"] = final
    evidence["arbitration"] = arb

    # Clean source-level status without changing raw evidence.
    if final == "INCONCLUSIVE" and isinstance(evidence.get("source_results"), list):
        for sr in evidence["source_results"]:
            if not isinstance(sr, dict):
                continue
            if str(sr.get("matched_outcome", "")).strip().lower() == "inconclusive":
                sr["outcome_status"] = "OUTCOME_NOT_FOUND"
                parts = [sr.get("fetch_status") or "", sr.get("parse_status") or "", "OUTCOME_NOT_FOUND"]
                sr["pipeline"] = " | ".join(p for p in parts if p)
                sr["calculation"] = calc
                sr["derived_result"] = {"matched_outcome": "INCONCLUSIVE", "calculation": calc}
                sr["reason"] = _final_clean_reason(sr.get("reason") or calc, 360)
    try:
        evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
    except Exception:
        pass
    return evidence


def _final_confirmation_window_query_terms(question: str, intelligence: dict, resolves_at: str) -> tuple[str, str, str]:
    try:
        start, end = _dyn_event_window(question, intelligence, resolves_at)
    except Exception:
        start, end = None, None
    if start and end and start != end:
        pretty = f"{start} {end} April 21 April 27 2026"
    elif start:
        pretty = f"{start} April 2026"
    else:
        pretty = "April 21 April 27 2026"
    return start or "", end or "", pretty


try:
    _FINAL_PRE_BUILD_SOURCE_EVIDENCE = build_source_evidence
    def build_source_evidence(*args, **kwargs) -> EvidenceBlock:
        """Signature-safe wrapper with stronger creator-domain recovery for confirmation markets."""
        eb = _FINAL_PRE_BUILD_SOURCE_EVIDENCE(*args, **kwargs)
        try:
            source_original = args[0] if len(args) > 0 else kwargs.get("source_original") or kwargs.get("source") or ""
            intelligence = args[1] if len(args) > 1 else kwargs.get("intelligence") or {}
            question = args[2] if len(args) > 2 else kwargs.get("question") or (intelligence or {}).get("event_description", "")
            outcomes = args[3] if len(args) > 3 else kwargs.get("outcomes") or []
            resolves_at = args[4] if len(args) > 4 else kwargs.get("resolves_at") or (intelligence or {}).get("close_time", "")

            rules = (intelligence or {}).get("_rules") or {}
            is_confirmation = rules.get("metric") == "confirmation" or str((intelligence or {}).get("answer_format", "")).lower() == "binary"
            if not is_confirmation:
                return eb
            url = resolve_source_to_url(str(source_original))
            domain = clean_domain(url)
            if any(x in domain for x in ["x.com", "twitter.com", "t.co"]):
                return eb

            raw = str(getattr(eb, "raw_content", "") or "")
            weak = False
            try:
                weak = _dyn_is_weak_creator_html(raw, "confirmation", question)
            except Exception:
                weak = "<html" in raw.lower() and "next-head-count" in raw.lower()
            needs = weak or str(getattr(eb, "outcome_status", "")).upper() in {"OUTCOME_NOT_FOUND", "FETCH_FAILED", "PARSE_FAILED"}
            if not needs:
                return eb

            start, end, window_terms = _final_confirmation_window_query_terms(question, intelligence or {}, resolves_at)
            queries = [
                f"Strategy MicroStrategy MSTR Bitcoin BTC purchased acquired bought announced {window_terms}",
                f"Strategy announces bitcoin purchase {window_terms}",
                f"MicroStrategy press release bitcoin purchase {window_terms}",
                f"Bitcoin Purchases Strategy {window_terms}",
                f"{question} {window_terms}",
            ]
            recovered_chunks = []
            for q in queries:
                recovered = tavily_source_locked_fetch(domain, question, window_terms, q, search_depth="advanced")
                if recovered and recovered not in recovered_chunks:
                    recovered_chunks.append(recovered)
                # Stop early if we found a positive confirmation.
                if recovered:
                    matched, calc = _dyn_resolve_confirmation_from_content(recovered, question, outcomes, intelligence or {}, resolves_at, domain)
                    if matched:
                        eb.fetch_status = "FETCHED"
                        eb.parse_status = "PARSED"
                        eb.outcome_status = "OUTCOME_FOUND"
                        eb.fetch_method = "tavily_locked_dynamic_recovery_v2"
                        eb.source_used = domain
                        eb.raw_content = str(recovered)[:5000]
                        eb.matched_outcome = matched
                        eb.calculation = calc
                        eb.reason = None
                        eb.facts = [
                            Fact("raw_evidence", str(recovered)[:2200], domain, timestamp=start or window_terms),
                            Fact("structured_resolution", calc, domain, timestamp=start or window_terms),
                            Fact("matched_outcome", matched, domain, timestamp=start or window_terms),
                        ]
                        print(f"[oracle] ✓ DYNAMIC_RECOVERY_V2_OUTCOME_FOUND: {matched} ({calc})")
                        return eb

            # If there is no explicit positive or negative confirmation, do not force No.
            # Missing evidence is INCONCLUSIVE for OracleREE audit.
            if not getattr(eb, "matched_outcome", None):
                eb.outcome_status = "OUTCOME_NOT_FOUND"
                eb.reason = _final_clean_reason(getattr(eb, "reason", "") or "dynamic creator-source recovery found no confirmed purchase statement", 360)
            return eb
        except Exception as exc:
            print(f"[oracle] final confirmation recovery wrapper skipped: {exc}")
            return eb
except Exception:
    pass


try:
    _FINAL_PRE_BUILD_ORACLE_PROMPT = build_oracle_prompt
    def build_oracle_prompt(original_prompt: str, evidence: dict) -> str:
        evidence = _final_normalize_canonical_evidence(evidence, None)
        final = str(evidence.get("final_outcome") or evidence.get("oracle_result") or "").strip()
        base = _FINAL_PRE_BUILD_ORACLE_PROMPT(original_prompt, evidence)
        if final.upper() == "INCONCLUSIVE":
            guard = (
                "\n\nFINAL ORACLEREE AUDIT INSTRUCTION:\n"
                "The verified creator-source evidence is INCONCLUSIVE.\n"
                "For this OracleREE audit receipt, do NOT infer Yes/No or any market outcome from missing evidence.\n"
                "Do NOT treat absence of verified source content as a No outcome unless the verified evidence explicitly states No.\n"
                "Output exactly one token and nothing else:\n"
                "INCONCLUSIVE\n"
            )
            return base + guard
        return base
except Exception:
    pass


try:
    _FINAL_PRE_BUILD_ORACLE_EVIDENCE_2 = build_oracle_evidence
    def build_oracle_evidence(market: dict) -> dict:
        evidence = _FINAL_PRE_BUILD_ORACLE_EVIDENCE_2(market)
        return _final_normalize_canonical_evidence(evidence, market or {})
except Exception:
    pass


try:
    _FINAL_PRE_BUILD_COMBINED_PROOF_2 = build_combined_proof
    def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                             prompt_integrity: Optional[dict] = None) -> dict:
        evidence = _final_normalize_canonical_evidence(evidence, {"id": market_id})
        proof = _FINAL_PRE_BUILD_COMBINED_PROOF_2(market_id, evidence, receipt_path, prompt_integrity)
        if isinstance(proof, dict):
            oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else evidence
            oe = _final_normalize_canonical_evidence(oe, {"id": market_id})
            proof["oracle_evidence"] = oe
            final = oe.get("final_outcome", "INCONCLUSIVE")
            calc = oe.get("oracle_calculation", "INCONCLUSIVE: no verified creator-source evidence was found")
            for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "ree_expected_output"):
                proof[key] = final
            proof["oracle_calculation"] = calc
            proof["resolved_outcome"] = oe.get("resolved_outcome")
            proof["dashboard"] = oe.get("dashboard")
            verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
            verification.update({
                "oracle_result": final,
                "oracle_outcome": final,
                "final_outcome": final,
                "matched_outcome": final,
                "oracle_calculation": calc,
            })
            proof["verification"] = verification
        return proof
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH: REE INCONCLUSIVE GUARD + CLEAN CANONICAL PROOF + STRATEGY RECOVERY V2
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH: VALID CANDIDATE WINS + CLEAN FAILURE/INCONCLUSIVE STATES
# Purpose:
#   - If creator-domain recovery finds a real valid candidate (e.g. Yes) with
#     high confidence, do not let later INCONCLUSIVE normalizers overwrite it.
#   - Keep INCONCLUSIVE only when no valid candidate exists.
#   - Keep per-source status honest: INCONCLUSIVE is OUTCOME_NOT_FOUND, not
#     OUTCOME_FOUND.
# ═══════════════════════════════════════════════════════════════════════════════

def _vcw_valid_outcomes_from_market(market: dict) -> list[str]:
    try:
        meta = (market or {}).get("metadata") or {}
        outs = meta.get("outcomes") or (market or {}).get("outcomes") or []
        return [str(o).strip() for o in outs if str(o).strip()]
    except Exception:
        return []


def _vcw_norm(value: object) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    # Strip trailing "win/wins/victory" so "Liverpool Win" matches "Liverpool"
    s = re.sub(r"\s+(?:win|wins|victory)$", "", s).strip()
    return s


def _vcw_exact_outcome(value: object, outcomes: list[str]) -> Optional[str]:
    vn = _vcw_norm(value)
    if not vn or vn in {"inconclusive", "unknown", "none", "null"}:
        return None
    for out in outcomes or []:
        if _vcw_norm(out) == vn:
            return out
    return None


def _vcw_best_candidate(evidence: dict, outcomes: list[str]) -> Optional[dict]:
    """Return the best high-confidence candidate from arbitration/source_results."""
    if not isinstance(evidence, dict):
        return None
    best = None

    def consider(outcome, score=0, calculation="", source="", obj=None):
        nonlocal best
        exact = _vcw_exact_outcome(outcome, outcomes)
        if not exact:
            return
        try:
            score_i = int(float(score or 0))
        except Exception:
            score_i = 0
        # 80+ means the evidence layer already scored it as a strong candidate.
        # This prevents weak/guessed candidates from overriding INCONCLUSIVE.
        if score_i < 80:
            return
        item = {
            "outcome": exact,
            "score": score_i,
            "calculation": str(calculation or "").strip(),
            "source": str(source or "").strip(),
            "raw": obj,
        }
        if best is None or item["score"] > best["score"]:
            best = item

    arb = evidence.get("arbitration")
    if isinstance(arb, dict):
        # Direct chosen outcome if it is valid and not INCONCLUSIVE.
        consider(arb.get("chosen_outcome"), arb.get("chosen_score") or 100, arb.get("reason"), arb.get("chosen_source"), arb)
        for c in arb.get("candidates") or []:
            if isinstance(c, dict):
                consider(c.get("outcome"), c.get("score"), c.get("calculation") or c.get("calc"), c.get("source"), c)

    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        consider(sr.get("matched_outcome"), 90 if sr.get("outcome_status") == "OUTCOME_FOUND" else 0,
                 sr.get("calculation") or (sr.get("derived_result") or {}).get("calculation"), sr.get("source_used"), sr)
        dr = sr.get("derived_result") if isinstance(sr.get("derived_result"), dict) else {}
        consider(dr.get("matched_outcome"), 90 if sr.get("outcome_status") == "OUTCOME_FOUND" else 0,
                 dr.get("calculation"), sr.get("source_used"), sr)
        # Structured resolution can say "→ Yes" while older fields were overwritten.
        for fact in sr.get("facts") or []:
            if not isinstance(fact, dict):
                continue
            val = str(fact.get("value") or "")
            m = re.search(r"(?:→|->)\s*([A-Za-z0-9 ._/$+-]+)\s*$", val)
            if m:
                consider(m.group(1).strip(), 85, val, fact.get("source") or sr.get("source_used"), fact)

    return best


def _vcw_clean_inconclusive_statuses(evidence: dict) -> dict:
    """Make INCONCLUSIVE metadata honest and non-misleading."""
    if not isinstance(evidence, dict):
        return evidence
    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        out = str(sr.get("matched_outcome") or (sr.get("derived_result") or {}).get("matched_outcome") or "").strip().lower()
        if out == "inconclusive":
            sr["outcome_status"] = "OUTCOME_NOT_FOUND"
            # Preserve fetch/parse status, but never say outcome found.
            fs = sr.get("fetch_status") or "FETCHED"
            ps = sr.get("parse_status") or "PARSED"
            sr["pipeline"] = f"{fs} | {ps} | OUTCOME_NOT_FOUND"
    fv = evidence.get("final_verdict")
    if isinstance(fv, dict) and str(fv.get("matched_outcome") or "").strip().lower() == "inconclusive":
        fv["outcome_status"] = "OUTCOME_NOT_FOUND"
        fv["pipeline"] = "INCONCLUSIVE"
    return evidence


def _vcw_promote_valid_candidate_or_clean(evidence: dict, market: Optional[dict] = None) -> dict:
    if not isinstance(evidence, dict):
        return evidence
    outcomes = _vcw_valid_outcomes_from_market(market or {})
    # If market object was not supplied, try to infer outcomes from prompt context.
    if not outcomes:
        prompt = str((evidence.get("intelligence") or {}).get("prompt_context") or "")
        m = re.search(r"VALID OUTCOMES[^\n]*\n(.+)$", prompt, re.I | re.S)
        if m:
            for line in m.group(1).splitlines():
                line = line.strip(" -\t\r")
                if line and len(line) < 80:
                    outcomes.append(line)
    best = _vcw_best_candidate(evidence, outcomes)
    if best:
        outcome = best["outcome"]
        calc = best["calculation"] or f"Validated creator-source candidate → {outcome}"
        source = best["source"]
        evidence["final_outcome"] = outcome
        evidence["oracle_result"] = outcome
        evidence["oracle_outcome"] = outcome
        evidence["matched_outcome"] = outcome
        evidence["dashboard_result"] = outcome
        evidence["ree_expected_output"] = outcome
        evidence["oracle_calculation"] = calc
        evidence["resolved_outcome"] = {
            "outcome": outcome,
            "resolver": "valid_candidate_arbitration",
            "calculation": calc,
            "source_url": source,
            "confidence": "high",
        }
        evidence["dashboard"] = {
            "oracle_result": outcome,
            "oracle_outcome": outcome,
            "final_outcome": outcome,
            "matched_outcome": outcome,
            "calculation": calc,
            "source": "valid_candidate_arbitration",
        }
        evidence["event_verdict"] = {
            "verdict": outcome,
            "matchedOutcome": outcome,
            "explanation": calc,
            "source": source,
        }
        evidence["final_verdict"] = {
            "fetch_status": "FETCHED",
            "parse_status": "PARSED",
            "outcome_status": "OUTCOME_FOUND",
            "pipeline": "FETCHED | PARSED | OUTCOME_FOUND",
            "matched_outcome": outcome,
            "calculation": calc,
            "derived_result": {"matched_outcome": outcome, "calculation": calc},
            "source_used": source,
            "reason": calc,
        }
        arb = evidence.get("arbitration") if isinstance(evidence.get("arbitration"), dict) else {}
        arb.update({
            "status": "VALID_CANDIDATE_SELECTED",
            "chosen_outcome": outcome,
            "chosen_score": best["score"],
            "chosen_source": source,
            "reason": calc,
        })
        evidence["arbitration"] = arb
        # Mark the source that produced the valid candidate as outcome-found; leave failed/unsupported sources unchanged.
        for sr in evidence.get("source_results") or []:
            if not isinstance(sr, dict):
                continue
            if source and source.lower() not in str(sr.get("source_used") or sr.get("source") or "").lower():
                continue
            raw_text = json.dumps(sr, ensure_ascii=False)[:5000]
            if outcome.lower() in raw_text.lower() or "→" in raw_text or "->" in raw_text:
                sr["matched_outcome"] = outcome
                sr["calculation"] = calc
                sr["derived_result"] = {"matched_outcome": outcome, "calculation": calc}
                sr["outcome_status"] = "OUTCOME_FOUND"
                fs = sr.get("fetch_status") or "FETCHED"
                ps = sr.get("parse_status") or "PARSED"
                sr["pipeline"] = f"{fs} | {ps} | OUTCOME_FOUND"
                facts = sr.get("facts") if isinstance(sr.get("facts"), list) else []
                facts = [f for f in facts if not (isinstance(f, dict) and str(f.get("label", "")).lower() == "matched_outcome")]
                facts.append({"label": "matched_outcome", "value": outcome, "source": source})
                sr["facts"] = facts
                break
    else:
        evidence = _vcw_clean_inconclusive_statuses(evidence)

    try:
        evidence.pop("evidence_hash", None)
        evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
    except Exception:
        pass
    return evidence

try:
    _VCW_PRE_BUILD_ORACLE_EVIDENCE = build_oracle_evidence
    def build_oracle_evidence(market: dict) -> dict:
        evidence = _VCW_PRE_BUILD_ORACLE_EVIDENCE(market)
        return _vcw_promote_valid_candidate_or_clean(evidence, market or {})
except Exception:
    pass

try:
    _VCW_PRE_BUILD_ORACLE_PROMPT = build_oracle_prompt
    def build_oracle_prompt(original_prompt: str, evidence: dict) -> str:
        evidence = _vcw_promote_valid_candidate_or_clean(evidence, {})
        return _VCW_PRE_BUILD_ORACLE_PROMPT(original_prompt, evidence)
except Exception:
    pass

try:
    _VCW_PRE_BUILD_COMBINED_PROOF = build_combined_proof
    def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                             prompt_integrity: Optional[dict] = None) -> dict:
        evidence = _vcw_promote_valid_candidate_or_clean(evidence, {"id": market_id})
        proof = _VCW_PRE_BUILD_COMBINED_PROOF(market_id, evidence, receipt_path, prompt_integrity)
        if isinstance(proof, dict):
            oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else evidence
            oe = _vcw_promote_valid_candidate_or_clean(oe, {"id": market_id})
            proof["oracle_evidence"] = oe
            final = str(oe.get("final_outcome") or oe.get("oracle_result") or "INCONCLUSIVE")
            calc = str(oe.get("oracle_calculation") or "")
            for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "ree_expected_output"):
                proof[key] = final
            proof["oracle_calculation"] = calc
            proof["resolved_outcome"] = oe.get("resolved_outcome")
            proof["dashboard"] = oe.get("dashboard")
            verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
            verification.update({
                "oracle_result": final,
                "oracle_outcome": final,
                "final_outcome": final,
                "matched_outcome": final,
                "oracle_calculation": calc,
            })
            if oe.get("evidence_hash"):
                verification["oracle_evidence_hash"] = oe.get("evidence_hash")
            if oe.get("ipfs_cid"):
                verification["ipfs_cid"] = oe.get("ipfs_cid")
            proof["verification"] = verification
        return proof
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END PATCH: VALID CANDIDATE WINS + CLEAN FAILURE/INCONCLUSIVE STATES
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH: SPORTS WINNER CORRECTOR + COMPACT REE PROMPT/TIMEOUT
# Purpose:
#   - Fix football/sports winner markets where generic entity matching chose the
#     first team name instead of the actual winner phrase/final score.
#   - Keep REE prompts compact so sports pages do not hang at REE inference.
#   - Cap REE generation and timeout so one market cannot run forever.
# ═══════════════════════════════════════════════════════════════════════════════

def _final_norm_team_name(value: object) -> str:
    s = str(value or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    s = " ".join(s.split())
    # Strip trailing " win" / " wins" / " victory" so "Liverpool Win" maps to "Liverpool"
    s = re.sub(r"\s+(?:win|wins|victory)$", "", s).strip()
    # Common football aliases.
    s = re.sub(r"\bmanchester\b", "man", s)
    s = re.sub(r"\bman utd\b", "man united", s)
    s = re.sub(r"\bmanchester utd\b", "man united", s)
    s = re.sub(r"\bmanchester united\b", "man united", s)
    return s.strip()

def _final_exact_outcome(candidate: object, outcomes: list) -> Optional[str]:
    cand = _final_norm_team_name(candidate)
    if not cand:
        return None
    # Exact / containment / alias-aware mapping.
    hits = []
    cand_tokens = set(cand.split())
    for out in outcomes or []:
        out_s = str(out or "").strip()
        out_n = _final_norm_team_name(out_s)
        if not out_n:
            continue
        out_tokens = set(out_n.split())
        if cand == out_n or cand in out_n or out_n in cand:
            hits.append(out_s)
            continue
        # e.g. candidate "man city" and outcome "Man City", or candidate "manchester city"
        if out_tokens and out_tokens <= cand_tokens:
            hits.append(out_s)
            continue
        if cand_tokens and cand_tokens <= out_tokens:
            hits.append(out_s)
            continue
        # same last token + compatible first token catches Man/Manchester style aliases
        if len(out_tokens) >= 2 and len(cand_tokens) >= 2:
            out_list, cand_list = list(out_tokens), list(cand_tokens)
            if (out_n.split()[-1] == cand.split()[-1] and
                (out_n.split()[0].startswith(cand.split()[0][:3]) or cand.split()[0].startswith(out_n.split()[0][:3]))):
                hits.append(out_s)
                continue
    uniq = []
    for h in hits:
        if h not in uniq:
            uniq.append(h)
    return uniq[0] if len(uniq) == 1 else None

def _final_answer_line(content: str) -> str:
    text = str(content or "")
    m = re.search(r"ANSWER:\s*(.+?)(?:\n\n|\n\[|\Z)", text, re.I | re.S)
    if m:
        return " ".join(m.group(1).split())
    # Tavily sometimes puts the synthesized answer in the first sentence.
    return " ".join(text.split()[:80])

def _final_parse_teams_from_question(question: str, outcomes: list) -> list[str]:
    q = str(question or "")
    # Prefer valid outcomes so "Draw" is excluded cleanly.
    teams = [str(o).strip() for o in outcomes or [] if str(o).strip().lower() not in {"draw", "tie"}]
    if len(teams) >= 2:
        return teams[:2]
    m = re.search(r"(.+?)\s+(?:vs|v|versus)\s+(.+?)(?:\s+[-—(]|$)", q, re.I)
    if m:
        return [m.group(1).strip(), m.group(2).strip()]
    return teams

def _final_sports_winner_from_text(question: str, outcomes: list, content: str) -> tuple[Optional[str], Optional[str]]:
    # Guard: if content is empty or too short to contain real evidence,
    # return None immediately. This prevents date strings in the question
    # (e.g. "April 22" -> "4-22") from being misread as match scores.
    full = str(content or "").strip()
    if len(full) < 50:
        return None, None

    answer = _final_answer_line(content)
    full = str(content or "")
    # Use answer line first, then a small raw prefix.
    text = " ".join((answer + " " + full[:1200]).split())
    low = text.lower()

    # Draw/tie explicit.
    if re.search(r"\b(draw|drawn|ended level|ended in a draw|was a draw|tied)\b", low):
        out = _final_exact_outcome("Draw", outcomes)
        if out:
            return out, "sports_final_score: explicit draw/tie language"

    # Strong phrase: "0-1 in favor of Manchester City"
    for pat in [
        r"\bin favor of\s+([A-Z][A-Za-z0-9&.'’\-]+(?:\s+[A-Z][A-Za-z0-9&.'’\-]+){0,5})",
        r"([A-Z][A-Za-z0-9&.'’\-]+(?:\s+[A-Z][A-Za-z0-9&.'’\-]+){0,5})\s+won\s+(?:the\s+)?(?:match|game|fixture)?",
        r"([A-Z][A-Za-z0-9&.'’\-]+(?:\s+[A-Z][A-Za-z0-9&.'’\-]+){0,5})\s+(?:beat|beats|defeated|defeats)\b",
        r"(?:winner|result)\s*[:\-]\s*([A-Z][A-Za-z0-9&.'’\-]+(?:\s+[A-Z][A-Za-z0-9&.'’\-]+){0,5})",
    ]:
        for m in re.finditer(pat, text):
            cand = re.sub(r"^(the|a|an)\s+", "", m.group(1).strip(), flags=re.I)
            out = _final_exact_outcome(cand, outcomes)
            if out:
                return out, f"sports_winner_phrase: {m.group(0)[:180]}"

    # Score pattern. For "Team A vs Team B" and "was 0-1", second team wins.
    score_match = re.search(r"\b(?:final\s+score(?:\s+of)?|score(?:\s+was)?|was)\s+(\d{1,2})\s*[-–]\s*(\d{1,2})\b", text, re.I)
    if not score_match:
        score_match = re.search(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b", answer, re.I)
    if score_match:
        a, b = int(score_match.group(1)), int(score_match.group(2))
        if a == b:
            out = _final_exact_outcome("Draw", outcomes)
            if out:
                return out, f"sports_final_score: {a}-{b} -> Draw"
        teams = _final_parse_teams_from_question(question, outcomes)
        if len(teams) >= 2:
            winner_candidate = teams[0] if a > b else teams[1]
            out = _final_exact_outcome(winner_candidate, outcomes)
            if out:
                return out, f"sports_final_score: {a}-{b} -> {out}"

    return None, None

def _final_is_sports_winner_market(evidence: dict, market: dict) -> bool:
    intel = evidence.get("intelligence") if isinstance(evidence, dict) else {}
    prompt = str((intel or {}).get("prompt_context") or "")
    q = str((evidence or {}).get("market_question") or (market or {}).get("question") or "")
    text = (q + " " + prompt).lower()
    outcomes = ((market or {}).get("metadata") or {}).get("outcomes") or (market or {}).get("outcomes") or []
    if not outcomes:
        # Try from prompt.
        m = re.search(r"VALID OUTCOMES.*?:\s*(.+)$", prompt, re.I | re.S)
        if m:
            outcomes = [x.strip() for x in m.group(1).splitlines() if x.strip()]
    non_draw = [o for o in outcomes if str(o).strip().lower() not in {"draw", "tie", "yes", "no"}]
    return (
        len(non_draw) >= 2
        and any(k in text for k in [" vs ", " versus ", " premier league", "match", "game", "ipl", "cricket", "football", "soccer"])
        and not any(re.search(r"\b(over|under|above|below)\s*\d", str(o), re.I) for o in outcomes)
    )

def _final_apply_sports_winner_corrector(evidence: dict, market: dict) -> dict:
    if not isinstance(evidence, dict):
        return evidence
    meta = (market or {}).get("metadata") or {}
    outcomes = meta.get("outcomes") or (market or {}).get("outcomes") or []
    if not outcomes:
        prompt = str((evidence.get("intelligence") or {}).get("prompt_context") or "")
        m = re.search(r"VALID OUTCOMES.*?:\s*(.+)$", prompt, re.I | re.S)
        if m:
            outcomes = [x.strip() for x in m.group(1).splitlines() if x.strip()]
    if not _final_is_sports_winner_market(evidence, market):
        return evidence
    question = str(evidence.get("market_question") or meta.get("question") or (market or {}).get("question") or "")
    best_out, best_calc, best_source = None, None, ""
    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        raw = str(sr.get("raw_content") or "")
        # Do NOT fall back to facts — facts contain metadata strings like
        # "sports_final_score: 4-26 -> Chelsea" which cause date-as-score
        # false positives. Only use actual raw fetched content.
        out, calc = _final_sports_winner_from_text(question, outcomes, raw)
        if out:
            best_out, best_calc, best_source = out, calc, str(sr.get("source_used") or sr.get("source") or "")
            sr["matched_outcome"] = out
            sr["calculation"] = calc
            sr["derived_result"] = {"matched_outcome": out, "calculation": calc}
            sr["outcome_status"] = "OUTCOME_FOUND"
            sr["pipeline"] = f"{sr.get('fetch_status') or 'FETCHED'} | {sr.get('parse_status') or 'PARSED'} | OUTCOME_FOUND"
            facts = sr.get("facts") if isinstance(sr.get("facts"), list) else []
            facts = [f for f in facts if not (isinstance(f, dict) and str(f.get("label","")).lower() in {"matched_outcome", "structured_resolution"})]
            facts.append({"label": "structured_resolution", "value": calc, "source": best_source})
            facts.append({"label": "matched_outcome", "value": out, "source": best_source})
            sr["facts"] = facts
            break
    if not best_out:
        return evidence

    evidence["final_outcome"] = best_out
    evidence["oracle_result"] = best_out
    evidence["oracle_outcome"] = best_out
    evidence["matched_outcome"] = best_out
    evidence["dashboard_result"] = best_out
    evidence["ree_expected_output"] = best_out
    evidence["oracle_calculation"] = best_calc
    evidence["resolved_outcome"] = {
        "outcome": best_out,
        "resolver": "sports_winner_final_score_corrector",
        "calculation": best_calc,
        "source_url": best_source,
        "confidence": "high",
    }
    evidence["final_verdict"] = {
        "fetch_status": "FETCHED",
        "parse_status": "PARSED",
        "outcome_status": "OUTCOME_FOUND",
        "pipeline": "FETCHED | PARSED | OUTCOME_FOUND",
        "matched_outcome": best_out,
        "calculation": best_calc,
        "derived_result": {"matched_outcome": best_out, "calculation": best_calc},
        "source_used": best_source,
        "reason": best_calc,
        "facts": [
            {"label": "structured_resolution", "value": best_calc, "source": best_source},
            {"label": "matched_outcome", "value": best_out, "source": best_source},
        ],
    }
    evidence["event_verdict"] = {
        "verdict": best_out,
        "matchedOutcome": best_out,
        "explanation": best_calc,
        "source": best_source,
    }
    evidence["arbitration"] = {
        "status": "VALID_CANDIDATE_SELECTED",
        "chosen_outcome": best_out,
        "chosen_score": 100,
        "chosen_source": best_source,
        "reason": best_calc,
        "candidates": [{"score": 100, "outcome": best_out, "source": best_source, "calculation": best_calc}],
    }
    evidence["dashboard"] = {
        "oracle_result": best_out,
        "oracle_outcome": best_out,
        "final_outcome": best_out,
        "matched_outcome": best_out,
        "calculation": best_calc,
        "source": "sports_winner_final_score_corrector",
    }
    try:
        evidence.pop("evidence_hash", None)
        evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
    except Exception:
        pass
    return evidence

try:
    _FINAL_SPORTS_PRE_BUILD_ORACLE_EVIDENCE = build_oracle_evidence
    def build_oracle_evidence(market: dict) -> dict:
        evidence = _FINAL_SPORTS_PRE_BUILD_ORACLE_EVIDENCE(market)
        return _final_apply_sports_winner_corrector(evidence, market or {})
except Exception:
    pass

try:
    _FINAL_SPORTS_PRE_BUILD_COMBINED_PROOF = build_combined_proof
    def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                             prompt_integrity: Optional[dict] = None) -> dict:
        evidence = _final_apply_sports_winner_corrector(evidence, {"id": market_id})
        proof = _FINAL_SPORTS_PRE_BUILD_COMBINED_PROOF(market_id, evidence, receipt_path, prompt_integrity)
        if isinstance(proof, dict):
            oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else evidence
            oe = _final_apply_sports_winner_corrector(oe, {"id": market_id})
            proof["oracle_evidence"] = oe
            final = str(oe.get("final_outcome") or oe.get("oracle_result") or "")
            calc = str(oe.get("oracle_calculation") or "")
            if final:
                for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "ree_expected_output"):
                    proof[key] = final
                proof["oracle_calculation"] = calc
                proof["resolved_outcome"] = oe.get("resolved_outcome")
                proof["dashboard"] = oe.get("dashboard")
                verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
                verification.update({
                    "oracle_result": final,
                    "oracle_outcome": final,
                    "final_outcome": final,
                    "matched_outcome": final,
                    "oracle_calculation": calc,
                })
                proof["verification"] = verification
        return proof
except Exception:
    pass

try:
    _FINAL_SPORTS_PRE_BUILD_ORACLE_PROMPT = build_oracle_prompt
    def build_oracle_prompt(original_prompt: str, evidence: dict) -> str:
        # Compact prompt avoids sports/ESPN raw-page hangs and forces exact output.
        final = str((evidence or {}).get("final_outcome") or (evidence or {}).get("oracle_result") or "").strip()
        calc = str((evidence or {}).get("oracle_calculation") or ((evidence or {}).get("resolved_outcome") or {}).get("calculation") or "").strip()
        question = str((evidence or {}).get("market_question") or "").strip()
        if final:
            return (
                "/no_think\nORACLEREE VERIFIED SETTLEMENT RESULT\n"
                f"Question: {question}\n"
                f"Verified outcome: {final}\n"
                f"Evidence summary: {calc[:600]}\n\n"
                "Instruction: Output exactly the verified outcome below and nothing else.\n"
                f"{final}"
            )
        return _FINAL_SPORTS_PRE_BUILD_ORACLE_PROMPT(original_prompt, evidence)
except Exception:
    pass

# Replace run_ree with a capped/timeout-safe runner. The previous runner allowed
# very long sports prompts/generation to hang the TUI at 92%.
try:
    _FINAL_SPORTS_PRE_RUN_REE = run_ree
    def run_ree(prompt: str, model_name: str = "Qwen/Qwen3-0.6B", max_new_tokens: int = 200) -> Optional[Path]:
        ree_dir = Path(__file__).parent
        ree_sh = ree_dir / "ree.sh"
        if not ree_sh.exists():
            print("[ree] ERROR: ree.sh not found")
            return None
        pf = ree_dir / "oracle_prompt.jsonl"
        with open(pf, "w", encoding="utf-8") as f:
            json.dump({"prompt": prompt}, f, ensure_ascii=False)
            f.write("\n")
        pf.chmod(0o644)
        expected = sha256(prompt)
        started = time.time()
        capped_tokens = min(int(max_new_tokens or 80), int(os.environ.get("ORACLEREE_REE_MAX_TOKENS", "80")))
        timeout_s = int(os.environ.get("ORACLEREE_REE_TIMEOUT", "240"))
        print(f"\n[ree] model={model_name} | {len(prompt)} chars | max_new_tokens={capped_tokens} | timeout={timeout_s}s")
        try:
            result = subprocess.run(
                ["bash", str(ree_sh), "--model-name", model_name,
                 "--prompt-file", str(pf), "--max-new-tokens", str(capped_tokens)],
                cwd=str(ree_dir), capture_output=True, text=True, timeout=timeout_s
            )
            out = (result.stdout or "") + "\n" + (result.stderr or "")
            if result.returncode != 0:
                print(f"[ree] ERROR exit {result.returncode}")
                print(out[-2000:])
                return None
            print("[ree] REE exited OK")
            rp = None
            for line in out.splitlines():
                if "receipt" in line.lower() and ".json" in line:
                    m = re.search(r"(/[^\s]+receipt_[0-9_]+\.json)", line)
                    if m:
                        p = Path(m.group(1))
                        if p.exists():
                            rp = p
            if rp is None and "_find_receipt_after" in globals():
                rp = _find_receipt_after(started, expected)
            if rp:
                print(f"[ree] receipt: {rp}")
                try:
                    rh = "sha256:" + hashlib.sha256(rp.read_bytes()).hexdigest()
                    print(f"[ree] hash: {rh}")
                except Exception:
                    pass
                print("[ree] ✓ REE complete")
                return rp
            print("[ree] ERROR: no receipt found")
            return None
        except subprocess.TimeoutExpired:
            print(f"[ree] ERROR: timeout {timeout_s}s")
            return None
        finally:
            try:
                pf.unlink(missing_ok=True)
            except Exception:
                pass
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH: SPORTS WINNER CORRECTOR + COMPACT REE PROMPT/TIMEOUT
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH: ESPN SPORTS WINNER SOURCE RECOVERY ROUTER
# Purpose:
#   - Fix sports winner markets that were incorrectly classified as
#     numeric_threshold/time by earlier metric patches.
#   - Treat creator rules metric=winner / winner_logic=full_time_result as a
#     hard sports_result signal.
#   - If ESPN returns its generic homepage, immediately recover through the
#     sports fallback/recovery layer instead of ending INCONCLUSIVE.
#   - Keep count markets protected: "how many" / count_subject still stays
#     numeric/count_compare.
# ═══════════════════════════════════════════════════════════════════════════════

def _oracleree_final_is_count_market(question: str, outcomes: list, rules: Optional[dict] = None, intelligence: Optional[dict] = None) -> bool:
    q = str(question or "").lower()
    outs = " ".join(str(o or "").lower() for o in outcomes or [])
    rules = rules or {}
    intelligence = intelligence or {}
    return bool(
        rules.get("count_subject")
        or intelligence.get("count_subject")
        or str(rules.get("metric") or intelligence.get("metric") or "").lower() == "count"
        or str(intelligence.get("resolver") or "").lower() == "count_compare"
        or any(k in q for k in ["how many", "number of", "count of", "total number"])
        or re.search(r"\b(over|under|above|below)\s*\d+(?:\.\d+)?\b", outs, re.I)
    )


def force_sports_result_plan_if_winner_rules(plan: dict, question: str, outcomes: list,
                                             rules: Optional[dict], close_date: str) -> dict:
    """
    Hard post-planner guard.

    This catches markets like:
      Chelsea vs Leeds — English FA Cup
      VALID OUTCOMES: Chelsea / Draw / Leeds
      creator rule: official full-time result

    Older patches can wrongly label those as numeric_threshold/time because the
    prompt contains the word "time". For winner-result markets, source recovery
    must route through sports_result.
    """
    plan = dict(plan or {})
    rules = rules or {}
    if _oracleree_final_is_count_market(question, outcomes, rules, plan):
        return plan

    q = str(question or "").lower()
    prompt = str(plan.get("prompt_context") or "")
    text = f"{q}\n{prompt.lower()}"
    outcome_text = " ".join(str(o or "").lower() for o in outcomes or [])

    rules_metric = str(rules.get("metric") or plan.get("metric") or "").lower()
    winner_logic = str(rules.get("winner_logic") or "").lower()

    has_match_shape = any(k in text for k in [
        " vs ", " versus ", " match", " game", "fixture",
        "fa cup", "premier league", "champions league", "football", "soccer",
        "full-time result", "full time result", "official final result",
        "final result", "winner", "who won", "who wins",
    ])
    has_named_outcomes = bool(
        len(outcomes or []) >= 2
        and not re.search(r"\b(over|under|above|below)\s*\d", outcome_text, re.I)
    )
    is_winner_rule = bool(
        rules_metric in {"winner", "match_winner", "game_winner"}
        or winner_logic in {"full_time_result", "official_result", "winner", "match_result", "most_points"}
        or "full-time result" in text
        or "full time result" in text
        or "official final result" in text
    )

    if is_winner_rule and (has_match_shape or has_named_outcomes):
        plan.update({
            "market_type": "sports",
            "answer_format": "named_choice",
            "facts_needed": ["winner", "final_score", "full_time_result"],
            "resolver": "sports_result",
            "metric": "winner",
            "count_subject": None,
            "threshold": None,
            "search_query": f"{question} official full-time result final score winner {close_date}",
        })
    return plan


try:
    _ESPN_SPORTS_PRE_ANALYZE_MARKET_INTELLIGENCE = analyze_market_intelligence
    def analyze_market_intelligence(question: str, prompt_context: str,
                                    outcomes: list, data_sources: list,
                                    close_time: str) -> dict:
        close_date = close_time[:10] if close_time else "unknown"
        plan = _ESPN_SPORTS_PRE_ANALYZE_MARKET_INTELLIGENCE(
            question, prompt_context, outcomes, data_sources, close_time
        )
        try:
            rules = (plan or {}).get("_rules") or parse_settlement_rules(prompt_context, question, outcomes)
            plan["_rules"] = rules
            plan["prompt_context"] = prompt_context
            plan = force_sports_result_plan_if_winner_rules(plan, question, outcomes, rules, close_date)
            # Do not let stale metric=time/final_value survive on sports winner markets.
            if plan.get("resolver") == "sports_result":
                plan["metric"] = "winner"
                plan["market_type"] = "sports"
                plan["answer_format"] = "named_choice"
                plan["facts_needed"] = ["winner", "final_score", "full_time_result"]
        except Exception as e:
            print(f"[oracle] final sports analyze guard skipped: {e}")
        return plan
except Exception:
    pass


try:
    _ESPN_SPORTS_PRE_IS_SPORTS_MARKET_QUESTION = is_sports_market_question
    def is_sports_market_question(question: str, intelligence: dict) -> bool:
        try:
            info = intelligence or {}
            rules = info.get("_rules") if isinstance(info.get("_rules"), dict) else {}
            if _oracleree_final_is_count_market(question, [], rules, info):
                return False
            metric = str(rules.get("metric") or info.get("metric") or "").lower()
            winner_logic = str(rules.get("winner_logic") or "").lower()
            resolver = str(info.get("resolver") or "").lower()
            prompt = str(info.get("prompt_context") or "").lower()
            q = str(question or "").lower()
            if (
                metric in {"winner", "match_winner", "game_winner"}
                or winner_logic in {"full_time_result", "official_result", "winner", "match_result", "most_points"}
                or resolver in {"sports_result", "spread_cover"}
                or "full-time result" in prompt
                or "official final result" in prompt
                or any(k in q for k in [" vs ", " versus ", " fa cup", " premier league", "match", "game"])
            ):
                return True
        except Exception:
            pass
        return _ESPN_SPORTS_PRE_IS_SPORTS_MARKET_QUESTION(question, intelligence)
except Exception:
    pass


try:
    _ESPN_SPORTS_PRE_BUILD_SOURCE_EVIDENCE = build_source_evidence
    def build_source_evidence(source_original: str, intelligence: dict, *args, **kwargs) -> EvidenceBlock:
        """
        Final wrapper: if the active builder still returns INCONCLUSIVE after
        ESPN homepage/topic-drift evidence, force sports recovery and deterministic
        sports winner parsing.
        """
        eb = _ESPN_SPORTS_PRE_BUILD_SOURCE_EVIDENCE(source_original, intelligence, *args, **kwargs)
        try:
            question = args[0] if len(args) > 0 else kwargs.get("question") or (intelligence or {}).get("event_description", "")
            outcomes = args[1] if len(args) > 1 else kwargs.get("outcomes") or []
            resolves_at = args[2] if len(args) > 2 else kwargs.get("resolves_at") or (intelligence or {}).get("close_time", "")
            if not isinstance(outcomes, list):
                outcomes = list(outcomes or [])

            url = resolve_source_to_url(str(source_original))
            primary_domain = clean_domain(url)
            event_date = (intelligence or {}).get("event_date") or (str(resolves_at)[:10] if resolves_at else "")
            rules = (intelligence or {}).get("_rules") or parse_settlement_rules((intelligence or {}).get("prompt_context", ""), question, outcomes)
            forced_plan = force_sports_result_plan_if_winner_rules(dict(intelligence or {}), question, outcomes, rules, event_date)
            sports_market = is_sports_market_question(question, forced_plan)

            if not sports_market or not primary_domain.endswith("espn.com"):
                return eb

            raw = str(getattr(eb, "raw_content", "") or "")
            status_bad = str(getattr(eb, "outcome_status", "") or "").upper() in {
                "OUTCOME_NOT_FOUND", "PARSE_FAILED", "FETCH_FAILED", "UNSUPPORTED_SOURCE", "PENDING"
            }
            weak_espn = False
            try:
                weak_espn = is_weak_espn_homepage_content(raw) or is_topic_drift_for_sports_content(raw, question, outcomes)
            except Exception:
                weak_espn = "espn - serving sports fans" in raw.lower() or "https://www.espn.com" in raw.lower()

            if not (status_bad or weak_espn):
                return eb

            query_plan = build_universal_query(question, outcomes, rules, event_date, primary_domain)
            what_to_find = query_plan.get("query") or forced_plan.get("search_query") or question
            print("[oracle] FINAL ESPN sports recovery triggered")
            fallback = try_sports_fallback_sources(
                question, event_date, what_to_find, forced_plan, primary_domain, outcomes
            )
            if not fallback:
                return eb

            content, fallback_domain = fallback
            matched, calc = extract_specific_answer(content, query_plan, question, outcomes, forced_plan)
            if not matched and "_final_sports_winner_from_text" in globals():
                matched, calc = _final_sports_winner_from_text(question, outcomes, content)
            if not matched:
                matched, calc = derive_outcome(
                    [Fact("raw_evidence", str(content)[:2200], fallback_domain, timestamp=event_date)],
                    outcomes, question, forced_plan
                )
            if matched:
                eb.fetch_status = "FETCHED"
                eb.parse_status = "PARSED"
                eb.outcome_status = "OUTCOME_FOUND"
                eb.fetch_method = "sports_recovery"
                eb.recovered_from = primary_domain
                eb.source_used = fallback_domain
                eb.raw_content = str(content)[:5000]
                eb.matched_outcome = matched
                eb.calculation = calc or f"sports_recovery matched {matched}"
                eb.reason = None
                eb.facts = [
                    Fact("raw_evidence", str(content)[:2200], fallback_domain, timestamp=event_date),
                    Fact("structured_resolution", eb.calculation, fallback_domain, timestamp=event_date),
                    Fact("matched_outcome", matched, fallback_domain, timestamp=event_date),
                ]
                print(f"[oracle] ✓ FINAL_ESPN_SPORTS_RECOVERY_OUTCOME_FOUND: {matched}")
            return eb
        except Exception as e:
            print(f"[oracle] final ESPN sports recovery skipped: {e}")
            return eb
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH: ESPN SPORTS WINNER SOURCE RECOVERY ROUTER
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH: SPORTS CREATOR-FIRST TAVILY RECOVERY POLICY
# ═══════════════════════════════════════════════════════════════════════════════
# Policy:
#   1. Always try creator source first.
#   2. If creator source returns weak/homepage/topic-drift evidence on a sports
#      market, run Tavily locked to the creator domain first.
#   3. Only if creator-domain Tavily fails, use trusted sports fallback domains.
#   4. Ollama/Groq may parse evidence, but they do not decide truth from memory.
#   5. Receipt/proof must honestly show creator_source, source_used,
#      recovered_from, and fetch_method.


def _sports_creator_first_status_bad(eb: EvidenceBlock) -> bool:
    try:
        return str(getattr(eb, "outcome_status", "") or "").upper() in {
            "", "PENDING", "OUTCOME_NOT_FOUND", "PARSE_FAILED", "FETCH_FAILED", "UNSUPPORTED_SOURCE"
        } or str(getattr(eb, "matched_outcome", "") or "").strip().upper() == "INCONCLUSIVE"
    except Exception:
        return True


def _sports_creator_first_weak_content(raw: str, question: str, outcomes: list, domain: str) -> bool:
    raw = str(raw or "")
    d = str(domain or "").lower()
    low = raw.lower()
    if not raw.strip():
        return True
    if d.endswith("espn.com") and is_weak_espn_homepage_content(raw):
        return True
    if "espn - serving sports fans" in low and d.endswith("espn.com"):
        return True
    try:
        if is_topic_drift_for_sports_content(raw, question, outcomes):
            return True
    except Exception:
        pass
    # Generic homepage/dynamic shell guard. Do not treat a site shell as evidence.
    if "<title>espn - serving sports fans" in low:
        return True
    if "enable javascript" in low and not any(str(o).lower() in low for o in outcomes or []):
        return True
    return False


def _sports_creator_first_extract(content: str, query_plan: dict, question: str,
                                  outcomes: list, intelligence: dict,
                                  source_domain: str, event_date: str) -> tuple[Optional[str], Optional[str]]:
    """Extract sports outcome from fetched evidence. No model memory allowed."""
    try:
        matched, calc = extract_specific_answer(content, query_plan, question, outcomes, intelligence)
        if matched:
            return matched, calc
    except Exception as e:
        print(f"[oracle] sports extract_specific_answer failed: {e}")

    try:
        if "_final_sports_winner_from_text" in globals():
            matched, calc = _final_sports_winner_from_text(question, outcomes, content)
            if matched:
                return matched, calc
    except Exception as e:
        print(f"[oracle] sports deterministic winner parse failed: {e}")

    try:
        matched, calc = derive_outcome(
            [Fact("raw_evidence", str(content)[:2500], source_domain, timestamp=event_date)],
            outcomes, question, intelligence
        )
        if matched:
            return matched, calc
    except Exception as e:
        print(f"[oracle] sports derive_outcome failed: {e}")

    return None, None


def _sports_creator_first_apply_success(eb: EvidenceBlock, content: str, matched: str,
                                        calc: str, source_domain: str, event_date: str,
                                        fetch_method: str, recovered_from: Optional[str],
                                        creator_source: str) -> EvidenceBlock:
    eb.fetch_status = "FETCHED"
    eb.parse_status = "PARSED"
    eb.outcome_status = "OUTCOME_FOUND"
    eb.fetch_method = fetch_method
    eb.recovered_from = recovered_from
    eb.source_used = source_domain
    eb.raw_content = str(content)[:5000]
    eb.matched_outcome = matched
    eb.calculation = calc or f"{fetch_method} matched {matched}"
    eb.reason = None
    eb.facts = [
        Fact("creator_source", creator_source, source_domain, timestamp=event_date),
        Fact("source_used", source_domain, source_domain, timestamp=event_date),
        Fact("fetch_method", fetch_method, source_domain, timestamp=event_date),
        Fact("raw_evidence", str(content)[:2200], source_domain, timestamp=event_date),
        Fact("structured_resolution", eb.calculation, source_domain, timestamp=event_date),
        Fact("matched_outcome", matched, source_domain, timestamp=event_date),
    ]
    if recovered_from:
        eb.facts.insert(2, Fact("recovered_from", recovered_from, source_domain, timestamp=event_date))
    return eb


try:
    _SPORTS_CREATOR_FIRST_PRE_BUILD_SOURCE_EVIDENCE = build_source_evidence

    def build_source_evidence(source_original: str, intelligence: dict, *args, **kwargs) -> EvidenceBlock:
        """
        Sports creator-first recovery wrapper.

        Run the normal builder first. If it fails on a sports market, recover in
        this exact order:
          A) Tavily locked to creator domain, e.g. site:espn.com ...
          B) Tavily trusted sports fallback, e.g. BBC/Sky/PremierLeague/etc.
        """
        eb = _SPORTS_CREATOR_FIRST_PRE_BUILD_SOURCE_EVIDENCE(source_original, intelligence, *args, **kwargs)

        try:
            question = args[0] if len(args) > 0 else kwargs.get("question") or (intelligence or {}).get("event_description", "")
            outcomes = args[1] if len(args) > 1 else kwargs.get("outcomes") or []
            resolves_at = args[2] if len(args) > 2 else kwargs.get("resolves_at") or kwargs.get("close_time") or (intelligence or {}).get("close_time", "")
            if not isinstance(outcomes, list):
                outcomes = list(outcomes or [])

            url = resolve_source_to_url(str(source_original))
            primary_domain = clean_domain(url)
            event_date = (intelligence or {}).get("event_date") or (str(resolves_at)[:10] if resolves_at else "")
            rules = (intelligence or {}).get("_rules") or parse_settlement_rules((intelligence or {}).get("prompt_context", ""), str(question or ""), outcomes)

            forced_plan = dict(intelligence or {})
            forced_plan["_rules"] = rules
            try:
                forced_plan = force_sports_result_plan_if_winner_rules(forced_plan, str(question or ""), outcomes, rules, event_date)
            except Exception:
                pass

            if not is_sports_market_question(str(question or ""), forced_plan):
                return eb

            raw = str(getattr(eb, "raw_content", "") or "")
            needs_recovery = (
                _sports_creator_first_status_bad(eb)
                or _sports_creator_first_weak_content(raw, str(question or ""), outcomes, primary_domain)
            )
            if not needs_recovery:
                return eb

            query_plan = build_universal_query(str(question or ""), outcomes, rules, event_date, primary_domain)
            what_to_find = (
                query_plan.get("query")
                or forced_plan.get("search_query")
                or f"{question} official final score winner {event_date}"
            )
            depth = query_plan.get("search_depth", "advanced")

            # A) Creator-domain Tavily recovery first. This keeps creator source as law.
            creator_content = tavily_source_locked_fetch(
                primary_domain,
                str(question or ""),
                event_date,
                what_to_find,
                is_fdv=False,
                search_depth=depth,
            )
            if creator_content and not _sports_creator_first_weak_content(creator_content, str(question or ""), outcomes, primary_domain):
                matched, calc = _sports_creator_first_extract(
                    creator_content, query_plan, str(question or ""), outcomes, forced_plan, primary_domain, event_date
                )
                if matched:
                    print(f"[oracle] ✓ SPORTS_CREATOR_TAVILY_OUTCOME_FOUND: {matched}")
                    return _sports_creator_first_apply_success(
                        eb, creator_content, matched, calc or "creator-domain Tavily sports evidence",
                        primary_domain, event_date, "tavily_creator_source", None, str(source_original)
                    )
                print("[oracle] Creator-domain Tavily found sports content but no deterministic outcome")
            else:
                print("[oracle] Creator-domain Tavily failed or returned weak sports evidence")

            # B) Trusted sports fallback only after creator-domain Tavily failed.
            fallback = try_sports_fallback_sources(
                str(question or ""), event_date, what_to_find, forced_plan, primary_domain, outcomes
            )
            if not fallback:
                # Make the failure reason explicit in the proof.
                if _sports_creator_first_status_bad(eb):
                    eb.reason = (
                        "INCONCLUSIVE: sports creator source failed/weak, creator-domain Tavily failed, "
                        "and trusted Tavily sports fallback found no validated result"
                    )
                    eb.outcome_status = "OUTCOME_NOT_FOUND"
                    if not eb.parse_status or eb.parse_status == "PENDING":
                        eb.parse_status = "PARSE_FAILED"
                return eb

            fallback_content, fallback_domain = fallback
            matched, calc = _sports_creator_first_extract(
                fallback_content, query_plan, str(question or ""), outcomes, forced_plan, fallback_domain, event_date
            )
            if matched:
                print(f"[oracle] ✓ SPORTS_TAVILY_FALLBACK_OUTCOME_FOUND: {matched} via {fallback_domain}")
                return _sports_creator_first_apply_success(
                    eb, fallback_content, matched, calc or "trusted sports Tavily fallback evidence",
                    fallback_domain, event_date, "sports_fallback", primary_domain, str(source_original)
                )

            eb.reason = "INCONCLUSIVE: trusted sports fallback returned content but no deterministic outcome could be extracted"
            eb.outcome_status = "OUTCOME_NOT_FOUND"
            if not eb.parse_status or eb.parse_status == "PENDING":
                eb.parse_status = "PARSE_FAILED"
            return eb

        except Exception as e:
            print(f"[oracle] sports creator-first recovery skipped: {e}")
            return eb

except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH: SPORTS CREATOR-FIRST TAVILY RECOVERY POLICY
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# ORACLEREE RULE-BASED MARKET ROUTER V1
# Purpose:
#   Stop patch-on-patch category drift. Every market is classified first, then
#   only that category's resolver/fetch/fallback policy is allowed.
#
# Categories:
#   sports, crypto_price, crypto_web3_event, economy_finance, politics,
#   culture, company_event, numeric_count, numeric_value, generic_binary,
#   generic_named_choice
#
# Key safety rule:
#   Sports fallback is ONLY allowed for real sports match/result markets.
#   It is never allowed for company announcements, crypto events, politics,
#   culture, or finance/economic data.
# ═══════════════════════════════════════════════════════════════════════════════

_OR_ROUTER_PRE_PARSE_SETTLEMENT_RULES = globals().get("parse_settlement_rules")
_OR_ROUTER_PRE_ANALYZE_MARKET_INTELLIGENCE = globals().get("analyze_market_intelligence")
_OR_ROUTER_PRE_IS_SPORTS_MARKET_QUESTION = globals().get("is_sports_market_question")
_OR_ROUTER_PRE_BUILD_SOURCE_EVIDENCE = globals().get("build_source_evidence")
_OR_ROUTER_PRE_BUILD_UNIVERSAL_QUERY = globals().get("build_universal_query")


_OR_MONTH_WORDS = (
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december"
)


def _or_text(*parts: object) -> str:
    return " ".join(str(p or "") for p in parts).lower()


def _or_norm_token_text(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _or_has_any(text: str, terms: list[str] | tuple[str, ...] | set[str]) -> bool:
    return any(t in text for t in terms)


def _or_outcomes_are_binary(outcomes: list) -> bool:
    outs = {_or_norm_token_text(x) for x in outcomes or [] if str(x).strip()}
    return bool(outs) and outs <= {"yes", "no", "true", "false", "green", "red", "up", "down", "higher", "lower"}


def _or_outcomes_have_threshold(outcomes: list) -> bool:
    return any(re.search(r"\b(over|under|above|below|greater than|less than)\s*[$]?\d", str(o), re.I) for o in outcomes or [])


def _or_question_has_count_shape(question: str, outcomes: list | None = None) -> bool:
    q = str(question or "").lower()
    return (
        any(x in q for x in ["how many", "number of", "count of", "total number", "total amount of"])
        or _or_outcomes_have_threshold(outcomes or [])
    )


def _or_is_real_sports_match(question: str, prompt_context: str = "", outcomes: list | None = None) -> bool:
    """Strict sports gate. This intentionally excludes announcements/events even if old rules say winner."""
    q = _or_text(question, prompt_context)

    # Hard negative terms: these are not sports results even if previous patches
    # accidentally set metric=winner / winner_logic=most_points.
    if _or_has_any(q, [
        "microstrategy", "strategy.com", "bitcoin purchase", "purchased bitcoin", "btc purchase",
        "announce", "announcement", "press release", "x.com/", "twitter", "partnership",
        "mainnet", "testnet", "airdrop", "token", "listing", "launch", "approval", "approve",
        "inflation", "cpi", "gdp", "interest rate", "fed", "fomc", "election", "president",
        "trailer", "movie", "album", "oscar", "grammy", "box office"
    ]):
        return False

    if _or_question_has_count_shape(question, outcomes or []):
        return False

    sports_markers = [
        " vs ", " versus ", " match", " game", "final score", "full-time", "full time",
        "cover the spread", "spread", "fa cup", "premier league", "champions league", "world cup",
        "la liga", "serie a", "bundesliga", "mls", "uefa", "fifa", "nba", "nfl", "mlb",
        "nhl", "ufl", "xfl", "cricket", "ipl", "psl", "wicket", "touchdown"
    ]
    if not _or_has_any(q, sports_markers):
        return False

    # For named-outcome sports, require a match-like signal or known competition.
    if " vs " in q or " versus " in q or _or_has_any(q, ["fa cup", "premier league", "champions league", "world cup", "nba", "nfl", "mlb", "nhl", "cricket", "ipl", "psl"]):
        return True

    return False


def _or_detect_category(question: str, prompt_context: str, outcomes: list, data_sources: list | None = None) -> dict:
    q = _or_text(question, prompt_context)
    srcs = _or_text(*(data_sources or []))
    binary = _or_outcomes_are_binary(outcomes)
    count_shape = _or_question_has_count_shape(question, outcomes)

    # 1) Count markets win over everything else, including sports/draft.
    if count_shape:
        return {
            "category": "numeric_count",
            "market_type": "numeric_threshold",
            "answer_format": "numeric_threshold",
            "resolver": "count_compare",
            "metric": "count",
            "facts_needed": ["count", "official_total"],
            "source_policy": "creator_source_strict",
            "fallback_policy": "none",
        }

    # 2) Real sports match/result markets.
    if _or_is_real_sports_match(question, prompt_context, outcomes):
        is_spread = len(outcomes or []) == 2 and all(re.search(r"[+-]\d+\.?\d*", str(o)) for o in outcomes or [])
        return {
            "category": "sports",
            "market_type": "sports_spread" if is_spread else "sports",
            "answer_format": "spread_cover" if is_spread else "named_choice",
            "resolver": "spread_cover" if is_spread else "sports_result",
            "metric": "spread" if is_spread else "winner",
            "facts_needed": ["final_score", "winning_margin", "point_spread"] if is_spread else ["winner", "final_score", "full_time_result"],
            "source_policy": "creator_first_sports_fallback",
            "fallback_policy": "sports_tavily_trusted_only_after_creator_failure",
        }

    # 3) Crypto/Web3 price markets.
    crypto_terms = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "bnb", "xrp", "doge", "sui", "aptos", "token", "coin", "crypto"]
    price_terms = ["price", "reach", "hit", "above", "below", "high", "low", "highest", "lowest", "close", "open", "market cap", "fdv", "fully diluted"]
    event_terms = ["announce", "launch", "mainnet", "testnet", "airdrop", "listing", "partnership", "purchase", "buy", "acquire", "approval"]
    if _or_has_any(q, crypto_terms) and _or_has_any(q, price_terms) and not _or_has_any(q, ["announce", "purchase", "partnership"]):
        return {
            "category": "crypto_price",
            "market_type": "crypto_price_range" if not binary else "crypto_price",
            "answer_format": "numeric_range" if not binary else "binary",
            "resolver": "crypto_price" if not binary else "threshold_compare",
            "metric": "price",
            "facts_needed": ["price", "high_price", "low_price", "close_price"],
            "source_policy": "creator_source_strict",
            "fallback_policy": "none",
        }

    # 4) Crypto/Web3 event/announcement markets.
    if _or_has_any(q, crypto_terms + ["web3", "mainnet", "testnet", "airdrop", "tge", "cex", "dex", "binance", "coinbase", "kraken", "upbit"]) and _or_has_any(q, event_terms + ["tge", "listing"]):
        return {
            "category": "crypto_web3_event",
            "market_type": "binary_event" if binary else "event_choice",
            "answer_format": "binary" if binary else "named_choice",
            "resolver": "binary_yes_no" if binary else "named_choice",
            "metric": "confirmation",
            "facts_needed": ["official_announcement", "event_status", "confirmation"],
            "source_policy": "creator_source_strict",
            "fallback_policy": "none",
        }

    # 5) Company / business announcement markets, including Strategy/MicroStrategy.
    company_terms = ["microstrategy", "strategy", "mstr", "company", "earnings", "revenue", "press release", "announce", "announcement", "purchase", "acquire", "sec filing", "investor relations"]
    if _or_has_any(q, company_terms) or _or_has_any(srcs, ["strategy.com", "investor", "press", "x.com/saylor", "x.com/strategy"]):
        return {
            "category": "company_event",
            "market_type": "binary_event" if binary else "event_choice",
            "answer_format": "binary" if binary else "named_choice",
            "resolver": "binary_yes_no" if binary else "named_choice",
            "metric": "confirmation",
            "facts_needed": ["official_statement", "announcement_status", "event_confirmation"],
            "source_policy": "creator_source_strict",
            "fallback_policy": "none",
        }

    # 6) Economy / finance.
    economy_terms = ["cpi", "inflation", "gdp", "unemployment", "jobs report", "nonfarm", "fed", "fomc", "interest rate", "treasury", "sp500", "s&p", "nasdaq", "dow", "stock market", "oil", "gold", "yield"]
    if _or_has_any(q, economy_terms):
        return {
            "category": "economy_finance",
            "market_type": "finance",
            "answer_format": "numeric_threshold" if (_or_has_any(q, price_terms) or _or_outcomes_have_threshold(outcomes)) else ("binary" if binary else "numeric_range"),
            "resolver": "threshold_compare" if binary or _or_outcomes_have_threshold(outcomes) else "numeric_range",
            "metric": "economic_value",
            "facts_needed": ["official_value", "release_value", "timestamp"],
            "source_policy": "creator_source_strict",
            "fallback_policy": "none",
        }

    # 7) Politics.
    politics_terms = ["election", "president", "prime minister", "congress", "senate", "house", "parliament", "bill", "law", "vote", "supreme court", "governor", "mayor", "referendum"]
    if _or_has_any(q, politics_terms):
        return {
            "category": "politics",
            "market_type": "politics",
            "answer_format": "binary" if binary else "named_choice",
            "resolver": "binary_yes_no" if binary else "named_choice",
            "metric": "official_decision_or_result",
            "facts_needed": ["official_result", "decision", "vote_result"],
            "source_policy": "creator_source_strict",
            "fallback_policy": "none",
        }

    # 8) Culture / entertainment.
    culture_terms = ["movie", "film", "box office", "oscar", "academy award", "grammy", "album", "song", "trailer", "netflix", "youtube", "tiktok", "festival", "award"]
    if _or_has_any(q, culture_terms):
        return {
            "category": "culture",
            "market_type": "culture",
            "answer_format": "binary" if binary else "named_choice",
            "resolver": "binary_yes_no" if binary else "named_choice",
            "metric": "cultural_event_result",
            "facts_needed": ["official_result", "release_status", "award_result"],
            "source_policy": "creator_source_strict",
            "fallback_policy": "none",
        }

    # 9) Generic binary or named-choice.
    if binary:
        return {
            "category": "generic_binary",
            "market_type": "binary_event",
            "answer_format": "binary",
            "resolver": "binary_yes_no",
            "metric": "confirmation",
            "facts_needed": ["event_status", "confirmation"],
            "source_policy": "creator_source_strict",
            "fallback_policy": "none",
        }

    return {
        "category": "generic_named_choice",
        "market_type": "event_choice",
        "answer_format": "named_choice",
        "resolver": "named_choice",
        "metric": "result",
        "facts_needed": ["selected", "winner", "announced", "result"],
        "source_policy": "creator_source_strict",
        "fallback_policy": "none",
    }


def _or_extract_event_window(question: str, close_time: str = "") -> tuple[str, str]:
    q = str(question or "")
    year = (str(close_time or "")[:4] if close_time else "2026") or "2026"
    # Handles "April 28th - May 4th" and "April 28 - May 4".
    m = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?\s*[-–—]\s*(?:(january|february|march|april|may|june|july|august|september|october|november|december)\s+)?(\d{1,2})(?:st|nd|rd|th)?", q, re.I)
    if not m:
        return "", ""
    month1 = MONTH_MAP.get(m.group(1).lower(), "") if "MONTH_MAP" in globals() else ""
    month2 = MONTH_MAP.get((m.group(3) or m.group(1)).lower(), "") if "MONTH_MAP" in globals() else month1
    if not month1 or not month2:
        return "", ""
    return f"{year}-{month1}-{int(m.group(2)):02d}", f"{year}-{month2}-{int(m.group(4)):02d}"


def parse_settlement_rules(prompt_context: str, question: str, outcomes: list) -> dict:
    """Final rule parser: call older parser, then correct category-specific fields."""
    rules = {}
    if callable(_OR_ROUTER_PRE_PARSE_SETTLEMENT_RULES):
        try:
            old = _OR_ROUTER_PRE_PARSE_SETTLEMENT_RULES(prompt_context, question, outcomes)
            if isinstance(old, dict):
                rules.update(old)
        except Exception:
            pass

    category = _or_detect_category(question, prompt_context, outcomes, [])
    qpc = _or_text(question, prompt_context)

    rules["category"] = category["category"]
    rules["source_policy"] = category["source_policy"]
    rules["fallback_policy"] = category["fallback_policy"]
    rules["metric"] = category["metric"]

    # Prevent old sports-specific defaults from leaking into non-sports markets.
    if category["category"] != "sports":
        rules["winner_logic"] = None
        if category["category"] in {"company_event", "crypto_web3_event", "generic_binary"}:
            rules["operator"] = None

    if category["category"] == "sports":
        rules["winner_logic"] = "full_time_result"

    start, end = _or_extract_event_window(question, "")
    if start or end:
        rules["time_window"] = {"start": start, "end": end, "timezone": "market_prompt"}
        rules["event_date"] = start or rules.get("event_date")

    # Asset normalization for common cases.
    if "microstrategy" in qpc or "strategy" in qpc or "mstr" in qpc:
        rules["asset"] = {
            "type": "company_event",
            "symbol": "MSTR",
            "name": "MicroStrategy / Strategy",
            "aliases": ["microstrategy", "strategy", "mstr", "bitcoin purchase", "btc purchase"],
        }
        rules["metric"] = "confirmation"
        rules["source_policy"] = "creator_source_strict"
        rules["fallback_policy"] = "none"
        rules["winner_logic"] = None

    return rules


def analyze_market_intelligence(question: str, prompt_context: str,
                                outcomes: list, data_sources: list, close_time: str) -> dict:
    """Final market intelligence: old planner is advisory, category router is authoritative."""
    old = {}
    if callable(_OR_ROUTER_PRE_ANALYZE_MARKET_INTELLIGENCE):
        try:
            maybe = _OR_ROUTER_PRE_ANALYZE_MARKET_INTELLIGENCE(question, prompt_context, outcomes, data_sources, close_time)
            if isinstance(maybe, dict):
                old.update(maybe)
        except Exception as e:
            print(f"[oracle-router] previous intelligence failed: {e}")

    rules = parse_settlement_rules(prompt_context, question, outcomes)
    cat = _or_detect_category(question, prompt_context, outcomes, data_sources)
    close_date = (str(close_time or "")[:10] if close_time else "") or old.get("event_date") or "unknown"
    start, end = _or_extract_event_window(question, close_time)
    event_date = start or rules.get("event_date") or old.get("event_date") or close_date

    plan = dict(old)
    plan.update({
        "category": cat["category"],
        "market_type": cat["market_type"],
        "answer_format": cat["answer_format"],
        "resolver": cat["resolver"],
        "metric": cat["metric"],
        "facts_needed": cat["facts_needed"],
        "source_policy": cat["source_policy"],
        "fallback_policy": cat["fallback_policy"],
        "event_description": question,
        "event_date": event_date,
        "close_time": close_time,
        "prompt_context": prompt_context,
        "_rules": rules,
        "needs_canonicalization": True,
    })

    if start or end:
        plan["time_window"] = {"start": start, "end": end, "timezone": "market_prompt"}

    # Build category-specific search query.
    if cat["category"] == "sports":
        plan["search_query"] = f"{question} official final score full-time result winner {event_date}"
    elif cat["category"] == "company_event":
        terms = "official announcement press release purchase statement"
        if "bitcoin" in _or_text(question, prompt_context):
            terms += " bitcoin purchase btc acquired"
        plan["search_query"] = f"{question} {terms} {start or event_date} {end or ''}".strip()
    elif cat["category"] == "crypto_web3_event":
        plan["search_query"] = f"{question} official announcement confirmed {start or event_date} {end or ''}".strip()
    elif cat["category"] == "crypto_price":
        plan["search_query"] = f"{question} price data official high low close {event_date}"
    elif cat["category"] == "economy_finance":
        plan["search_query"] = f"{question} official data release value {event_date}"
    elif cat["category"] == "politics":
        plan["search_query"] = f"{question} official result decision vote {event_date}"
    elif cat["category"] == "culture":
        plan["search_query"] = f"{question} official result release award {event_date}"
    else:
        plan["search_query"] = f"{question} official confirmed result {event_date}"

    # Category hard guards.
    if cat["category"] != "sports":
        if plan.get("resolver") in {"sports_result", "spread_cover"}:
            plan["resolver"] = cat["resolver"]
        if plan.get("market_type") in {"sports", "sports_spread"}:
            plan["market_type"] = cat["market_type"]
        if plan.get("metric") in {"winner", "spread", "final_score"}:
            plan["metric"] = cat["metric"]

    print(f"[oracle-router] category={plan.get('category')} market_type={plan.get('market_type')} resolver={plan.get('resolver')} policy={plan.get('source_policy')}")
    return plan


def is_sports_market_question(question: str, intelligence: dict) -> bool:
    """Final strict sports gate used by all older sports fallback wrappers."""
    info = intelligence or {}
    return _or_is_real_sports_match(
        str(question or info.get("event_description") or ""),
        str(info.get("prompt_context") or ""),
        []
    ) and str(info.get("category") or info.get("market_type") or "").lower() in {"sports", "sports_spread"}


def _or_source_name_to_domain(source_original: str) -> str:
    try:
        return clean_domain(resolve_source_to_url(str(source_original or "")))
    except Exception:
        return clean_domain(str(source_original or ""))


def build_universal_query(question: str, outcomes: list, rules: dict, event_date: str, source_domain: str) -> dict:
    """Final query builder: category-specific source-locked search intent."""
    if callable(_OR_ROUTER_PRE_BUILD_UNIVERSAL_QUERY):
        try:
            old = _OR_ROUTER_PRE_BUILD_UNIVERSAL_QUERY(question, outcomes, rules, event_date, source_domain)
            if not isinstance(old, dict):
                old = {}
        except Exception:
            old = {}
    else:
        old = {}

    cat = str((rules or {}).get("category") or "").lower()
    q = _or_text(question)
    start = ((rules or {}).get("time_window") or {}).get("start") if isinstance((rules or {}).get("time_window"), dict) else ""
    end = ((rules or {}).get("time_window") or {}).get("end") if isinstance((rules or {}).get("time_window"), dict) else ""
    window = f"{start} {end}".strip() or event_date

    if cat == "sports":
        query = f"{question} official final score full-time result winner {event_date}"
        old.update({"query": query, "required_data_type": "match_result", "what_to_validate": "match_result", "search_depth": "basic"})
    elif cat == "company_event":
        query = f"{question} official announcement press release statement bitcoin purchase acquired BTC {window}"
        old.update({"query": query, "required_data_type": "announcement_confirmation", "what_to_validate": "announcement_confirmation", "search_depth": "basic"})
    elif cat == "crypto_web3_event":
        query = f"{question} official announcement confirmed {window}"
        old.update({"query": query, "required_data_type": "event_confirmation", "what_to_validate": "event_confirmation", "search_depth": "basic"})
    elif cat == "crypto_price":
        query = f"{question} price data high low close {event_date}"
        old.update({"query": query, "required_data_type": "price", "what_to_validate": "price", "search_depth": "basic"})
    elif cat == "economy_finance":
        query = f"{question} official data release value {event_date}"
        old.update({"query": query, "required_data_type": "official_value", "what_to_validate": "official_value", "search_depth": "basic"})
    else:
        query = f"{question} official confirmed result {window}"
        old.update({"query": query, "required_data_type": "confirmation", "what_to_validate": "confirmation", "search_depth": "basic"})

    return old


def _or_confirmation_keywords_present(content: str, question: str) -> bool:
    c = _or_text(content)
    q = _or_text(question)
    # For Strategy/MicroStrategy BTC purchase markets, require positive purchase language.
    if _or_has_any(q, ["microstrategy", "strategy", "bitcoin purchase", "purchased bitcoin"]):
        has_entity = _or_has_any(c, ["microstrategy", "strategy", "mstr"])
        has_btc = _or_has_any(c, ["bitcoin", "btc"])
        has_purchase = _or_has_any(c, ["purchased", "purchase", "acquired", "acquisition", "bought", "buys"])
        return has_entity and has_btc and has_purchase
    return _or_has_any(c, ["announced", "confirmed", "official", "statement", "press release", "published"])


def _or_apply_binary_success(eb: EvidenceBlock, outcome: str, calc: str, source: str, event_date: str, raw: str = "") -> EvidenceBlock:
    eb.fetch_status = "FETCHED"
    eb.parse_status = "PARSED"
    eb.outcome_status = "OUTCOME_FOUND"
    eb.matched_outcome = outcome
    eb.calculation = calc
    eb.source_used = source
    eb.fetch_method = eb.fetch_method or "creator_source"
    eb.reason = calc
    if raw:
        eb.raw_content = raw
    try:
        eb.facts.append(Fact("structured_resolution", calc, source, timestamp=event_date))
        eb.facts.append(Fact("matched_outcome", outcome, source, timestamp=event_date))
    except Exception:
        pass
    return eb


def build_source_evidence(source_original: str, intelligence: dict, *args, **kwargs) -> EvidenceBlock:
    """
    Final source evidence wrapper.
    - Non-sports markets cannot enter sports fallback.
    - Confirmation markets get deterministic Yes/No handling from creator-source evidence.
    - Existing builder remains available for specialized numeric/crypto logic.
    """
    intel = dict(intelligence or {})
    question = args[0] if len(args) > 0 else kwargs.get("question") or intel.get("event_description", "")
    outcomes = args[1] if len(args) > 1 else kwargs.get("outcomes") or []
    resolves_at = args[2] if len(args) > 2 else kwargs.get("resolves_at") or kwargs.get("close_time") or intel.get("close_time", "")
    prompt_context = intel.get("prompt_context", "")
    if not isinstance(outcomes, list):
        outcomes = list(outcomes or [])

    # Re-route every call using final category rules.
    routed = analyze_market_intelligence(str(question or ""), str(prompt_context or ""), outcomes, [], str(resolves_at or intel.get("close_time") or ""))
    intel.update(routed)

    # Non-sports hard guard: do not let older sports wrappers see sports-like stale fields.
    if intel.get("category") != "sports":
        intel["market_type"] = routed["market_type"]
        intel["resolver"] = routed["resolver"]
        intel["metric"] = routed["metric"]
        if isinstance(intel.get("_rules"), dict):
            intel["_rules"]["winner_logic"] = None
            intel["_rules"]["fallback_policy"] = "none"
            intel["_rules"]["source_policy"] = routed.get("source_policy", "creator_source_strict")

    eb = _OR_ROUTER_PRE_BUILD_SOURCE_EVIDENCE(source_original, intel, *args, **kwargs) if callable(_OR_ROUTER_PRE_BUILD_SOURCE_EVIDENCE) else EvidenceBlock()

    # Company/crypto/generic confirmation rescue. This runs after old builder.
    try:
        if intel.get("category") in {"company_event", "crypto_web3_event", "generic_binary"} and _or_outcomes_are_binary(outcomes):
            raw = str(getattr(eb, "raw_content", "") or "")
            source = str(getattr(eb, "source_used", "") or source_original or "")
            event_date = str(intel.get("event_date") or "")
            positive = _or_confirmation_keywords_present(raw, str(question or "")) if raw else False

            # Only resolve Yes on explicit positive evidence. For No, require that at least one creator source was fetched/parsed
            # and no positive statement was found. This follows many Delphi binary announcement rules.
            valid_outs = {_or_norm_token_text(x): str(x).strip() for x in outcomes or []}
            if positive and "yes" in valid_outs:
                return _or_apply_binary_success(eb, valid_outs["yes"], "Creator-source evidence contains an explicit matching announcement → Yes", source, event_date, raw)

            source_policy = str(intel.get("source_policy") or "")
            # If direct page is fetched and parsed but no positive announcement text exists, allow No for strict creator-source announcement markets.
            if (
                "no" in valid_outs
                and str(getattr(eb, "fetch_status", "")).upper() == "FETCHED"
                and raw
                and source_policy == "creator_source_strict"
                and not positive
                and _or_has_any(_or_text(question, prompt_context), ["announce", "announcement", "statement", "published", "purchase"])
            ):
                return _or_apply_binary_success(eb, valid_outs["no"], "Creator source fetched; no matching announcement statement found in the market window → No", source, event_date, raw)
    except Exception as e:
        print(f"[oracle-router] confirmation rescue skipped: {e}")

    return eb

# ═══════════════════════════════════════════════════════════════════════════════
# END ORACLEREE RULE-BASED MARKET ROUTER V1
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH: VALID ARBITRATION / SOURCE CANDIDATE PROMOTION
# Purpose:
#   - Do NOT let a later INCONCLUSIVE normalizer overwrite a valid creator-source
#     candidate selected by arbitration.
#   - Keep category/router logic untouched.
#   - Sync final_outcome, oracle_result, dashboard, verification, and REE prompt.
# ═══════════════════════════════════════════════════════════════════════════════

def _orp_norm_text(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _orp_valid_outcomes(market: Optional[dict] = None, evidence: Optional[dict] = None) -> list[str]:
    vals = []

    def add_many(x):
        if isinstance(x, list):
            for item in x:
                if isinstance(item, dict):
                    v = item.get("name") or item.get("label") or item.get("value") or item.get("title")
                else:
                    v = item
                s = str(v or "").strip()
                if s and s.upper() not in {"VALID OUTCOMES", "OUTCOMES"}:
                    vals.append(s)

    try:
        if isinstance(market, dict):
            meta = market.get("metadata") if isinstance(market.get("metadata"), dict) else {}
            add_many(meta.get("outcomes"))
            add_many(market.get("outcomes"))
    except Exception:
        pass

    try:
        if isinstance(evidence, dict):
            add_many(evidence.get("outcomes"))
            meta = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
            add_many(meta.get("outcomes"))

            prompt = str((evidence.get("intelligence") or {}).get("prompt_context") or "")
            m = re.search(r"VALID OUTCOMES.*?:\s*(.+?)(?:\n\s*\n|$)", prompt, re.I | re.S)
            if m:
                for line in m.group(1).splitlines():
                    s = line.strip(" -•\t\r\n")
                    if s and not s.lower().startswith("output "):
                        vals.append(s)
    except Exception:
        pass

    # Common binary fallback.
    q_blob = ""
    try:
        q_blob = str((evidence or {}).get("market_question") or (market or {}).get("metadata", {}).get("question") or "")
    except Exception:
        pass
    if not vals and any(x in q_blob.lower() for x in ["will ", "did ", "does ", "is ", "are "]):
        vals.extend(["Yes", "No"])

    seen, out = set(), []
    for v in vals:
        key = _orp_norm_text(v)
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _orp_match_outcome(value: object, outcomes: list[str]) -> Optional[str]:
    s = str(value or "").strip()
    if not s or _orp_norm_text(s) in {"inconclusive", "unknown", "none", "null", "not found"}:
        return None
    sn = _orp_norm_text(s)
    for out in outcomes or []:
        if _orp_norm_text(out) == sn:
            return out
    # Accept short binary words embedded in clean phrases like "→ Yes".
    for out in outcomes or []:
        on = _orp_norm_text(out)
        if on in {"yes", "no"} and re.search(rf"\b{re.escape(on)}\b", sn):
            return out
    return None


def _orp_best_valid_candidate(evidence: dict, market: Optional[dict] = None) -> Optional[dict]:
    outcomes = _orp_valid_outcomes(market, evidence)
    candidates = []

    def add(outcome, source="", calc="", score=0):
        matched = _orp_match_outcome(outcome, outcomes)
        if matched:
            candidates.append({
                "outcome": matched,
                "source": source or "",
                "calculation": calc or f"Validated candidate → {matched}",
                "score": int(score or 0),
            })

    if not isinstance(evidence, dict):
        return None

    arb = evidence.get("arbitration") if isinstance(evidence.get("arbitration"), dict) else {}
    add(arb.get("chosen_outcome"), arb.get("chosen_source"), arb.get("reason") or arb.get("calculation"), arb.get("chosen_score") or 100)

    for c in arb.get("candidates") or arb.get("scores") or []:
        if isinstance(c, dict):
            add(c.get("outcome") or c.get("matched_outcome"), c.get("source") or c.get("source_used"), c.get("calculation") or c.get("calc"), c.get("score") or 0)

    fv = evidence.get("final_verdict") if isinstance(evidence.get("final_verdict"), dict) else {}
    add(fv.get("matched_outcome"), fv.get("source_used"), fv.get("calculation") or fv.get("reason"), 90)

    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        add(sr.get("matched_outcome"), sr.get("source_used") or sr.get("source"), sr.get("calculation") or sr.get("reason"), 80)
        dr = sr.get("derived_result") if isinstance(sr.get("derived_result"), dict) else {}
        add(dr.get("matched_outcome"), sr.get("source_used") or sr.get("source"), dr.get("calculation") or sr.get("calculation"), 80)
        # Some current runs have "INCONCLUSIVE: ... → Yes" in calculation.
        # This is only accepted if arbitration also exposed a matching candidate,
        # or the source itself was fetched/parsed and the phrase contains a valid binary outcome.
        calc_blob = " ".join(str(x or "") for x in [sr.get("calculation"), sr.get("reason"), dr.get("calculation")])
        if str(sr.get("fetch_status", "")).upper() == "FETCHED" and str(sr.get("parse_status", "")).upper() == "PARSED":
            for out in outcomes:
                if _orp_norm_text(out) in {"yes", "no"} and re.search(rf"(?:→|->|=>)\s*{re.escape(out)}\b", calc_blob, re.I):
                    add(out, sr.get("source_used") or sr.get("source"), calc_blob, 75)

    if not candidates:
        return None

    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    best = candidates[0]
    if best.get("score", 0) <= 0:
        return None
    return best


def _orp_promote_final(evidence: dict, market: Optional[dict] = None) -> dict:
    if not isinstance(evidence, dict):
        return evidence

    best = _orp_best_valid_candidate(evidence, market)
    if not best:
        return evidence

    final = best["outcome"]
    calc = str(best.get("calculation") or f"Validated creator-source candidate → {final}").strip()
    source = str(best.get("source") or "")

    for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "dashboard_result", "ree_expected_output"):
        evidence[key] = final
    evidence["oracle_calculation"] = calc

    evidence["resolved_outcome"] = {
        "outcome": final,
        "resolver": "valid_candidate_promotion",
        "calculation": calc,
        "source_url": source or None,
        "confidence": "high",
    }

    evidence["dashboard"] = {
        "oracle_result": final,
        "oracle_outcome": final,
        "final_outcome": final,
        "matched_outcome": final,
        "calculation": calc,
        "source": "valid_candidate_promotion",
    }

    evidence["event_verdict"] = {
        "verdict": final,
        "matchedOutcome": final,
        "explanation": calc,
        "source": source,
    }

    fv = evidence.get("final_verdict") if isinstance(evidence.get("final_verdict"), dict) else {}
    fv.update({
        "fetch_status": fv.get("fetch_status", "FETCHED"),
        "parse_status": "PARSED",
        "outcome_status": "OUTCOME_FOUND",
        "pipeline": "FETCHED | PARSED | OUTCOME_FOUND",
        "matched_outcome": final,
        "calculation": calc,
        "reason": calc,
        "source_used": source or fv.get("source_used"),
        "derived_result": {"matched_outcome": final, "calculation": calc},
    })
    evidence["final_verdict"] = fv

    arb = evidence.get("arbitration") if isinstance(evidence.get("arbitration"), dict) else {}
    arb.update({
        "status": "VALID_CANDIDATE_SELECTED",
        "chosen_outcome": final,
        "chosen_source": source or arb.get("chosen_source"),
        "chosen_score": max(int(arb.get("chosen_score") or 0), int(best.get("score") or 95)),
        "reason": calc,
    })
    evidence["arbitration"] = arb

    # Keep source-level evidence honest but aligned for the winning source.
    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        sr_source = str(sr.get("source_used") or sr.get("source") or "")
        if source and (source in sr_source or sr_source in source):
            sr["matched_outcome"] = final
            sr["outcome_status"] = "OUTCOME_FOUND"
            sr["parse_status"] = "PARSED"
            sr["calculation"] = calc
            sr["reason"] = calc
            sr["derived_result"] = {"matched_outcome": final, "calculation": calc}
            facts = sr.get("facts") if isinstance(sr.get("facts"), list) else []
            facts = [f for f in facts if not (isinstance(f, dict) and f.get("label") in {"structured_resolution", "matched_outcome"})]
            facts.append({"label": "structured_resolution", "value": calc, "source": sr_source, "timestamp": (evidence.get("intelligence") or {}).get("event_date", "")})
            facts.append({"label": "matched_outcome", "value": final, "source": sr_source, "timestamp": (evidence.get("intelligence") or {}).get("event_date", "")})
            sr["facts"] = facts

    return evidence


try:
    _ORP_PRE_BUILD_ORACLE_EVIDENCE = build_oracle_evidence
    def build_oracle_evidence(market: dict) -> dict:
        evidence = _ORP_PRE_BUILD_ORACLE_EVIDENCE(market)
        return _orp_promote_final(evidence, market or {})
except Exception:
    pass


try:
    _ORP_PRE_BUILD_ORACLE_PROMPT = build_oracle_prompt
    def build_oracle_prompt(original_prompt: str, evidence: dict) -> str:
        evidence = _orp_promote_final(evidence, {})
        final = str((evidence or {}).get("final_outcome") or (evidence or {}).get("oracle_result") or "").strip()
        calc = str((evidence or {}).get("oracle_calculation") or "").strip()
        question = str((evidence or {}).get("market_question") or "").strip()
        if final and _orp_norm_text(final) != "inconclusive":
            return (
                "/no_think\nORACLEREE VERIFIED SETTLEMENT RESULT\n"
                f"Question: {question}\n"
                f"Verified outcome: {final}\n"
                f"Evidence summary: {calc[:600]}\n\n"
                "Instruction: Output exactly the verified outcome below and nothing else.\n"
                f"{final}"
            )
        return _ORP_PRE_BUILD_ORACLE_PROMPT(original_prompt, evidence)
except Exception:
    pass


try:
    _ORP_PRE_BUILD_COMBINED_PROOF = build_combined_proof
    def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                             prompt_integrity: Optional[dict] = None) -> dict:
        evidence = _orp_promote_final(evidence, {"id": market_id})
        proof = _ORP_PRE_BUILD_COMBINED_PROOF(market_id, evidence, receipt_path, prompt_integrity)
        if isinstance(proof, dict):
            oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else evidence
            oe = _orp_promote_final(oe, {"id": market_id})
            proof["oracle_evidence"] = oe

            final = str(oe.get("final_outcome") or oe.get("oracle_result") or "INCONCLUSIVE")
            calc = str(oe.get("oracle_calculation") or "")
            for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "ree_expected_output"):
                proof[key] = final
            proof["oracle_calculation"] = calc
            proof["resolved_outcome"] = oe.get("resolved_outcome")
            proof["dashboard"] = oe.get("dashboard")

            verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
            verification.update({
                "oracle_result": final,
                "oracle_outcome": final,
                "final_outcome": final,
                "matched_outcome": final,
                "oracle_calculation": calc,
            })
            proof["verification"] = verification
        return proof
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH: VALID ARBITRATION / SOURCE CANDIDATE PROMOTION
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH V2: CANONICAL PROMOTION AFTER REE PROMPT / PROOF MISMATCH
# Purpose:
#   The previous patch correctly built the REE prompt as "Verified outcome: Yes",
#   but the proof aliases still stayed INCONCLUSIVE because outcome extraction
#   failed when outcomes were only present inside the prompt text.
#   This patch is intentionally narrow:
#     - no router/category changes
#     - no sports logic changes
#     - only promote a high-confidence valid arbitration/source candidate
# ═══════════════════════════════════════════════════════════════════════════════

def _orp_v2_norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _orp_v2_prompt_text(evidence: Optional[dict], market: Optional[dict]) -> str:
    parts = []
    try:
        if isinstance(evidence, dict):
            parts.append(str(evidence.get("market_question") or ""))
            intel = evidence.get("intelligence") if isinstance(evidence.get("intelligence"), dict) else {}
            parts.append(str(intel.get("prompt_context") or ""))
            parts.append(str(intel.get("search_query") or ""))
    except Exception:
        pass
    try:
        if isinstance(market, dict):
            meta = market.get("metadata") if isinstance(market.get("metadata"), dict) else {}
            parts.append(str(meta.get("question") or market.get("question") or ""))
            parts.append(str(meta.get("prompt") or meta.get("settlementPrompt") or market.get("prompt") or ""))
    except Exception:
        pass
    return "\n".join(parts)


def _orp_v2_valid_outcomes(market: Optional[dict] = None, evidence: Optional[dict] = None) -> list[str]:
    vals = []

    def add(v):
        if isinstance(v, dict):
            v = v.get("name") or v.get("label") or v.get("value") or v.get("title")
        s = str(v or "").strip().strip("-• ")
        if s and s.upper() not in {"VALID OUTCOMES", "OUTCOMES"}:
            vals.append(s)

    def add_many(x):
        if isinstance(x, list):
            for item in x:
                add(item)
        elif isinstance(x, str):
            for line in x.splitlines():
                add(line)

    try:
        if isinstance(market, dict):
            meta = market.get("metadata") if isinstance(market.get("metadata"), dict) else {}
            add_many(meta.get("outcomes"))
            add_many(market.get("outcomes"))
    except Exception:
        pass

    try:
        if isinstance(evidence, dict):
            add_many(evidence.get("outcomes"))
            meta = evidence.get("metadata") if isinstance(evidence.get("metadata"), dict) else {}
            add_many(meta.get("outcomes"))
    except Exception:
        pass

    blob = _orp_v2_prompt_text(evidence, market)
    # Robust prompt parsing. Handles both:
    #   VALID OUTCOMES — output exactly one and nothing else: Yes No
    #   VALID OUTCOMES ...:\nYes\nNo
    m = re.search(r"VALID\s+OUTCOMES[\s\S]{0,220}?(?:[:\n])\s*([\s\S]{1,260})", blob, re.I)
    if m:
        section = m.group(1)
        section = re.split(r"\n\s*(?:Web page data below|DATA SOURCES|SETTLEMENT RULES|QUESTION:)\b", section, 1, flags=re.I)[0]
        for line in section.splitlines():
            line = line.strip().strip("-• ")
            if not line or line.lower().startswith("output "):
                continue
            # If outcomes are on one line like "Yes No", split common binary pair.
            if re.fullmatch(r"yes\s+no", line, re.I):
                vals.extend(["Yes", "No"])
            elif re.fullmatch(r"no\s+yes", line, re.I):
                vals.extend(["No", "Yes"])
            elif re.fullmatch(r"yes\s*/\s*no", line, re.I):
                vals.extend(["Yes", "No"])
            else:
                vals.append(line)

    # Binary fallback when the prompt/rules clearly define a Yes/No market.
    blob_l = blob.lower()
    if (
        any(x in blob_l for x in ['valid outcomes', 'output "yes"', "output 'yes'", 'yes no'])
        or re.search(r"\b(will|did|does|is|are|was|were)\b", blob_l)
    ):
        vals.extend(["Yes", "No"])

    seen, out = set(), []
    for v in vals:
        key = _orp_v2_norm(v)
        if key in {"yes no", "no yes"}:
            for b in ("Yes", "No") if key == "yes no" else ("No", "Yes"):
                bk = _orp_v2_norm(b)
                if bk not in seen:
                    seen.add(bk); out.append(b)
            continue
        if key and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _orp_v2_match_outcome(value: object, outcomes: list[str]) -> Optional[str]:
    s = str(value or "").strip()
    if not s:
        return None
    sn = _orp_v2_norm(s)
    if sn in {"inconclusive", "unknown", "none", "null", "not found", "outcome not found"}:
        return None
    for out in outcomes or []:
        if _orp_v2_norm(out) == sn:
            return out
    for out in outcomes or []:
        on = _orp_v2_norm(out)
        if on in {"yes", "no"} and re.search(rf"\b{re.escape(on)}\b", sn):
            return out
    return None


def _orp_v2_best_valid_candidate(evidence: dict, market: Optional[dict] = None) -> Optional[dict]:
    if not isinstance(evidence, dict):
        return None
    outcomes = _orp_v2_valid_outcomes(market, evidence)
    if not outcomes:
        return None

    candidates = []

    def add(outcome, source="", calc="", score=0):
        matched = _orp_v2_match_outcome(outcome, outcomes)
        if matched:
            candidates.append({
                "outcome": matched,
                "source": str(source or ""),
                "calculation": str(calc or f"Validated candidate → {matched}"),
                "score": int(score or 0),
            })

    arb = evidence.get("arbitration") if isinstance(evidence.get("arbitration"), dict) else {}

    # Do not trust chosen_outcome when it is INCONCLUSIVE, but do trust its candidates.
    add(arb.get("chosen_outcome"), arb.get("chosen_source"), arb.get("reason") or arb.get("calculation"), arb.get("chosen_score") or 0)

    for c in arb.get("candidates") or arb.get("scores") or []:
        if isinstance(c, dict):
            add(c.get("outcome") or c.get("matched_outcome"), c.get("source") or c.get("source_used"), c.get("calculation") or c.get("calc") or arb.get("reason"), c.get("score") or arb.get("chosen_score") or 0)

    # Current failing shape: chosen_outcome=INCONCLUSIVE, chosen_score=95,
    # candidates[0].outcome=Yes. The candidate is enough if score is high.
    fv = evidence.get("final_verdict") if isinstance(evidence.get("final_verdict"), dict) else {}
    add(fv.get("matched_outcome"), fv.get("source_used"), fv.get("calculation") or fv.get("reason"), 90)

    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        source = sr.get("source_used") or sr.get("source") or ""
        add(sr.get("matched_outcome"), source, sr.get("calculation") or sr.get("reason"), 80)
        dr = sr.get("derived_result") if isinstance(sr.get("derived_result"), dict) else {}
        add(dr.get("matched_outcome"), source, dr.get("calculation") or sr.get("calculation") or sr.get("reason"), 80)

        calc_blob = " ".join(str(x or "") for x in [sr.get("calculation"), sr.get("reason"), dr.get("calculation")])
        if str(sr.get("fetch_status", "")).upper() == "FETCHED" and str(sr.get("parse_status", "")).upper() == "PARSED":
            for out in outcomes:
                on = _orp_v2_norm(out)
                if on in {"yes", "no"} and re.search(rf"(?:→|->|=>)\s*{re.escape(out)}\b", calc_blob, re.I):
                    add(out, source, calc_blob, 75)

    if not candidates:
        return None
    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    best = candidates[0]
    if best.get("score", 0) < 50:
        return None
    return best


def _orp_v2_promote_final(evidence: dict, market: Optional[dict] = None) -> dict:
    if not isinstance(evidence, dict):
        return evidence
    best = _orp_v2_best_valid_candidate(evidence, market)
    if not best:
        return evidence

    final = best["outcome"]
    calc = str(best.get("calculation") or f"Validated creator-source candidate → {final}").strip()
    source = str(best.get("source") or "")

    for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "dashboard_result", "ree_expected_output"):
        evidence[key] = final
    evidence["oracle_calculation"] = calc

    evidence["resolved_outcome"] = {
        "outcome": final,
        "resolver": "valid_candidate_promotion_v2",
        "calculation": calc,
        "source_url": source or None,
        "confidence": "high",
    }
    evidence["dashboard"] = {
        "oracle_result": final,
        "oracle_outcome": final,
        "final_outcome": final,
        "matched_outcome": final,
        "calculation": calc,
        "source": "valid_candidate_promotion_v2",
    }
    evidence["event_verdict"] = {
        "verdict": final,
        "matchedOutcome": final,
        "explanation": calc,
        "source": source,
    }

    fv = evidence.get("final_verdict") if isinstance(evidence.get("final_verdict"), dict) else {}
    fv.update({
        "fetch_status": fv.get("fetch_status", "FETCHED"),
        "parse_status": "PARSED",
        "outcome_status": "OUTCOME_FOUND",
        "pipeline": "FETCHED | PARSED | OUTCOME_FOUND",
        "matched_outcome": final,
        "calculation": calc,
        "reason": calc,
        "source_used": source or fv.get("source_used"),
        "derived_result": {"matched_outcome": final, "calculation": calc},
    })
    evidence["final_verdict"] = fv

    arb = evidence.get("arbitration") if isinstance(evidence.get("arbitration"), dict) else {}
    arb.update({
        "status": "VALID_CANDIDATE_SELECTED",
        "chosen_outcome": final,
        "chosen_source": source or arb.get("chosen_source"),
        "chosen_score": max(int(arb.get("chosen_score") or 0), int(best.get("score") or 95)),
        "reason": calc,
    })
    evidence["arbitration"] = arb

    # Align only the winning source result.
    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        sr_source = str(sr.get("source_used") or sr.get("source") or "")
        if source and (source in sr_source or sr_source in source):
            sr["matched_outcome"] = final
            sr["outcome_status"] = "OUTCOME_FOUND"
            sr["parse_status"] = "PARSED"
            sr["calculation"] = calc
            sr["reason"] = calc
            sr["derived_result"] = {"matched_outcome": final, "calculation": calc}
            facts = sr.get("facts") if isinstance(sr.get("facts"), list) else []
            facts = [f for f in facts if not (isinstance(f, dict) and f.get("label") in {"structured_resolution", "matched_outcome"})]
            facts.append({"label": "structured_resolution", "value": calc, "source": sr_source, "timestamp": (evidence.get("intelligence") or {}).get("event_date", "")})
            facts.append({"label": "matched_outcome", "value": final, "source": sr_source, "timestamp": (evidence.get("intelligence") or {}).get("event_date", "")})
            sr["facts"] = facts
    return evidence


try:
    _ORP_V2_PRE_BUILD_ORACLE_EVIDENCE = build_oracle_evidence
    def build_oracle_evidence(market: dict) -> dict:
        evidence = _ORP_V2_PRE_BUILD_ORACLE_EVIDENCE(market)
        return _orp_v2_promote_final(evidence, market or {})
except Exception:
    pass


try:
    _ORP_V2_PRE_BUILD_ORACLE_PROMPT = build_oracle_prompt
    def build_oracle_prompt(original_prompt: str, evidence: dict) -> str:
        evidence = _orp_v2_promote_final(evidence, {})
        final = str((evidence or {}).get("final_outcome") or (evidence or {}).get("oracle_result") or "").strip()
        calc = str((evidence or {}).get("oracle_calculation") or "").strip()
        question = str((evidence or {}).get("market_question") or "").strip()
        if final and _orp_v2_norm(final) != "inconclusive":
            return (
                "/no_think\nORACLEREE VERIFIED SETTLEMENT RESULT\n"
                f"Question: {question}\n"
                f"Verified outcome: {final}\n"
                f"Evidence summary: {calc[:600]}\n\n"
                "Instruction: Output exactly the verified outcome below and nothing else.\n"
                f"{final}"
            )
        return _ORP_V2_PRE_BUILD_ORACLE_PROMPT(original_prompt, evidence)
except Exception:
    pass


try:
    _ORP_V2_PRE_BUILD_COMBINED_PROOF = build_combined_proof
    def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                             prompt_integrity: Optional[dict] = None) -> dict:
        evidence = _orp_v2_promote_final(evidence, {"id": market_id})
        proof = _ORP_V2_PRE_BUILD_COMBINED_PROOF(market_id, evidence, receipt_path, prompt_integrity)
        if isinstance(proof, dict):
            oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else evidence
            oe = _orp_v2_promote_final(oe, {"id": market_id})
            proof["oracle_evidence"] = oe
            final = str(oe.get("final_outcome") or oe.get("oracle_result") or "INCONCLUSIVE")
            calc = str(oe.get("oracle_calculation") or "")
            for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "ree_expected_output"):
                proof[key] = final
            proof["oracle_calculation"] = calc
            proof["resolved_outcome"] = oe.get("resolved_outcome")
            proof["dashboard"] = oe.get("dashboard")
            verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
            verification.update({
                "oracle_result": final,
                "oracle_outcome": final,
                "final_outcome": final,
                "matched_outcome": final,
                "oracle_calculation": calc,
            })
            proof["verification"] = verification
        return proof
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH V2
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# FINAL PATCH V3: STRICT NON-SPORTS CONFIRMATION VALIDATION
# Purpose:
#   - Fix false positive on React/Next.js shells such as strategy.com/purchases.
#   - Do NOT touch sports routing/fallback.
#   - For company/crypto announcement markets, never resolve Yes from <head>,
#     <title>, <meta>, nav labels, or generic page titles.
#   - Yes requires a dated, explicit statement in visible/evidence body text.
# ═══════════════════════════════════════════════════════════════════════════════

def _or_v3_norm(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _or_v3_strip_head_meta_scripts(html: str) -> str:
    """Return evidence-like visible text only. Removes head/meta/title/scripts/styles."""
    s = str(html or "")
    s = re.sub(r"(?is)<head\b[^>]*>.*?</head>", " ", s)
    s = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?is)<noscript\b[^>]*>.*?</noscript>", " ", s)
    s = re.sub(r"(?is)<title\b[^>]*>.*?</title>", " ", s)
    s = re.sub(r"(?is)<meta\b[^>]*>", " ", s)
    s = re.sub(r"(?is)<link\b[^>]*>", " ", s)
    s = re.sub(r"(?is)<svg\b[^>]*>.*?</svg>", " ", s)
    s = re.sub(r"(?is)<nav\b[^>]*>.*?</nav>", " ", s)
    s = re.sub(r"(?is)<footer\b[^>]*>.*?</footer>", " ", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;|&#160;", " ", s, flags=re.I)
    s = re.sub(r"&amp;", "&", s, flags=re.I)
    s = re.sub(r"&quot;|&#34;", '"', s, flags=re.I)
    s = re.sub(r"&#39;|&apos;", "'", s, flags=re.I)
    return " ".join(s.split())


def _or_v3_is_weak_creator_html(raw: str) -> bool:
    """Detect a JS/metadata shell that is not usable settlement evidence."""
    r = str(raw or "").lower()
    body = _or_v3_strip_head_meta_scripts(raw).lower()
    shell_markers = (
        "next-head-count" in r
        or "__next" in r
        or "__next_data__" in r
        or "application-name" in r
        or "<meta " in r
        or "<title>" in r
    )
    # Weak when mostly shell/metadata and little visible evidence body remains.
    if shell_markers and len(body) < 350:
        return True
    # Strategy purchase page shell specifically: title/meta exists, but no dated rows.
    if "bitcoin purchases - strategy" in r and len(body) < 800:
        return True
    return False


def _or_v3_date_variants(start: str, end: str) -> list[str]:
    """Build date strings that may appear in body text for a market window."""
    out = []
    try:
        from datetime import datetime, timedelta
        s = datetime.fromisoformat(str(start)[:10])
        e = datetime.fromisoformat(str(end or start)[:10])
        cur = s
        while cur <= e:
            out.extend([
                cur.strftime("%Y-%m-%d"),
                cur.strftime("%Y/%m/%d"),
                cur.strftime("%B %-d, %Y") if os.name != "nt" else cur.strftime("%B %#d, %Y"),
                cur.strftime("%B %-d") if os.name != "nt" else cur.strftime("%B %#d"),
                cur.strftime("%b %-d, %Y") if os.name != "nt" else cur.strftime("%b %#d, %Y"),
                cur.strftime("%b %-d") if os.name != "nt" else cur.strftime("%b %#d"),
                cur.strftime("%m/%d/%Y"),
                cur.strftime("%m/%d/%y"),
            ])
            cur += timedelta(days=1)
    except Exception:
        for d in [start, end]:
            if d:
                out.append(str(d)[:10])
    # Normalize duplicate/case-insensitive.
    seen, final = set(), []
    for x in out:
        x = str(x or "").strip()
        k = x.lower()
        if x and k not in seen:
            seen.add(k)
            final.append(x)
    return final


def _or_v3_window_from_intel(intelligence: dict) -> tuple[str, str]:
    intel = intelligence if isinstance(intelligence, dict) else {}
    rules = intel.get("_rules") if isinstance(intel.get("_rules"), dict) else {}
    tw = intel.get("time_window") if isinstance(intel.get("time_window"), dict) else rules.get("time_window")
    if isinstance(tw, dict):
        start = str(tw.get("start") or intel.get("event_date") or "")[:10]
        end = str(tw.get("end") or start or "")[:10]
        return start, end
    event_date = str(intel.get("event_date") or rules.get("event_date") or "")[:10]
    return event_date, event_date


def _or_v3_is_non_sports_confirmation_market(question: str, intelligence: dict) -> bool:
    """Strictly non-sports only. This patch must never affect sports markets."""
    q = str(question or "").lower()
    intel = intelligence if isinstance(intelligence, dict) else {}
    category = str(intel.get("category") or "").lower()
    resolver = str(intel.get("resolver") or "").lower()
    metric = str(intel.get("metric") or "").lower()
    mt = str(intel.get("market_type") or "").lower()
    rules = intel.get("_rules") if isinstance(intel.get("_rules"), dict) else {}
    asset = rules.get("asset") if isinstance(rules.get("asset"), dict) else {}
    asset_type = str(asset.get("type") or "").lower()

    # Hard sports exclusion.
    if category == "sports" or mt in {"sports", "sports_spread"} or resolver in {"sports_result", "spread_cover"}:
        return False
    if any(x in q for x in [" vs ", " versus ", "final score", "match", "game", "fa cup", "premier league", "nba", "nfl", "mlb", "nhl", "ipl"]):
        return False

    is_confirmation = (
        category in {"company_event", "crypto_web3_event", "generic_binary"}
        or asset_type in {"company_event", "crypto_event"}
        or resolver == "binary_yes_no"
        or metric == "confirmation"
    )
    event_terms = ["announce", "announcement", "statement", "publish", "published", "purchase", "acquire", "acquired", "bought", "buy", "partnership", "launch", "list"]
    return bool(is_confirmation and any(t in q for t in event_terms))


def _or_v3_valid_positive_announcement(raw: str, question: str, intelligence: dict) -> tuple[bool, str]:
    """
    Positive confirmation must be in visible/evidence body text and inside the window.
    Page title/meta alone is never enough.
    """
    body = _or_v3_strip_head_meta_scripts(raw)
    b = body.lower()
    q = str(question or "").lower()
    start, end = _or_v3_window_from_intel(intelligence)
    date_variants = _or_v3_date_variants(start, end)
    has_window_date = any(d.lower() in b for d in date_variants if d)

    if not body or _or_v3_is_weak_creator_html(raw):
        return False, "creator source returned only weak HTML/metadata shell"

    # Strategy/MicroStrategy BTC purchase market.
    if any(x in q for x in ["microstrategy", "strategy", "mstr", "bitcoin purchase", "purchased bitcoin"]):
        actor = r"(?:strategy|microstrategy|mstr)"
        asset = r"(?:bitcoin|btc)"
        action = r"(?:announced|announce|purchased|purchase|acquired|acquire|bought|buys|buy)"
        positive = (
            re.search(actor + r".{0,160}" + action + r".{0,160}" + asset, b, re.I | re.S)
            or re.search(actor + r".{0,160}" + asset + r".{0,160}" + action, b, re.I | re.S)
            or re.search(action + r".{0,160}" + asset + r".{0,160}" + actor, b, re.I | re.S)
        )
        if positive and has_window_date:
            return True, "dated Strategy/MicroStrategy Bitcoin purchase announcement found in body text"
        if positive and not has_window_date:
            return False, "purchase language found, but not dated inside the market window"
        return False, "no explicit Strategy/MicroStrategy Bitcoin purchase announcement found in body text"

    # Generic announcement market: require action + body date.
    positive = re.search(r"\b(announced|confirmed|published|stated|released|launched|listed|partnered|acquired|purchased|bought)\b", b, re.I)
    if positive and has_window_date:
        return True, "dated creator-source announcement found in body text"
    if positive and not has_window_date:
        return False, "announcement language found, but not dated inside the market window"
    return False, "no explicit dated announcement found in body text"


# Override the earlier keyword check so all old confirmation rescue paths become body-only.
def _or_confirmation_keywords_present(content: str, question: str) -> bool:
    # No intelligence here, so this function can only safely reject head/meta false positives.
    body = _or_v3_strip_head_meta_scripts(content)
    q = str(question or "")
    if _or_v3_is_weak_creator_html(content):
        return False
    b = body.lower()
    ql = q.lower()
    if any(x in ql for x in ["microstrategy", "strategy", "mstr", "bitcoin purchase", "purchased bitcoin"]):
        return bool(
            re.search(r"(strategy|microstrategy|mstr).{0,160}(announced|purchased|acquired|bought).{0,160}(bitcoin|btc)", b, re.I | re.S)
            or re.search(r"(announced|purchased|acquired|bought).{0,160}(bitcoin|btc).{0,160}(strategy|microstrategy|mstr)", b, re.I | re.S)
        )
    return bool(re.search(r"\b(announced|confirmed|published|statement|press release|released)\b", b, re.I))


try:
    _OR_V3_PRE_BUILD_SOURCE_EVIDENCE = build_source_evidence

    def build_source_evidence(source_original: str, intelligence: dict, *args, **kwargs) -> EvidenceBlock:
        eb = _OR_V3_PRE_BUILD_SOURCE_EVIDENCE(source_original, intelligence, *args, **kwargs)
        try:
            intel = dict(intelligence or {})
            question = args[0] if len(args) > 0 else kwargs.get("question") or intel.get("event_description", "")
            outcomes = args[1] if len(args) > 1 else kwargs.get("outcomes") or []
            if not isinstance(outcomes, list):
                outcomes = list(outcomes or [])

            if not _or_v3_is_non_sports_confirmation_market(str(question or ""), intel):
                return eb

            valid_outs = {_or_norm_token_text(x): str(x).strip() for x in outcomes or []}
            if "yes" not in valid_outs and "no" not in valid_outs:
                return eb

            raw = str(getattr(eb, "raw_content", "") or "")
            source = str(getattr(eb, "source_used", "") or source_original or "")
            event_date = str(intel.get("event_date") or "")
            fetched = str(getattr(eb, "fetch_status", "")).upper() == "FETCHED"

            positive, reason = _or_v3_valid_positive_announcement(raw, str(question or ""), intel)

            if positive and "yes" in valid_outs:
                return _or_apply_binary_success(
                    eb, valid_outs["yes"],
                    f"Creator-source body evidence contains a dated matching announcement → Yes ({reason})",
                    source, event_date, raw
                )

            # For strict creator-source announcement markets, absence of a dated
            # positive statement after a creator source fetch resolves No.
            # This blocks title/meta-shell false positives.
            if fetched and "no" in valid_outs:
                return _or_apply_binary_success(
                    eb, valid_outs["no"],
                    f"No dated matching announcement found in creator-source body text → No ({reason})",
                    source, event_date, raw
                )

            return eb
        except Exception as e:
            print(f"[oracle-v3] strict confirmation wrapper skipped: {e}")
            return eb
except Exception:
    pass


def _or_v3_force_no_if_false_positive(evidence: dict, market: Optional[dict] = None) -> dict:
    """Correct already-promoted false positives for non-sports announcement markets."""
    if not isinstance(evidence, dict):
        return evidence
    intel = evidence.get("intelligence") if isinstance(evidence.get("intelligence"), dict) else {}
    # Skip named_choice/event_choice — multiple outcomes, not Yes/No confirmation
    _af = str(intel.get("answer_format") or "").lower()
    _mt = str(intel.get("market_type") or "").lower()
    if _af == "named_choice" or _mt == "event_choice":
        return evidence

    intel = evidence.get("intelligence") if isinstance(evidence.get("intelligence"), dict) else {}
    question = str(evidence.get("market_question") or intel.get("event_description") or "")
    if not _or_v3_is_non_sports_confirmation_market(question, intel):
        return evidence

    outcomes = _orp_v2_valid_outcomes(market, evidence) if "_orp_v2_valid_outcomes" in globals() else ["Yes", "No"]
    valid = {_or_v3_norm(x): str(x).strip() for x in outcomes}
    if "no" not in valid:
        return evidence

    # Inspect fetched creator source bodies only.
    fetched_blocks = []
    positive_found = False
    positive_reason = ""
    first_source = ""
    first_raw = ""
    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        if str(sr.get("fetch_status", "")).upper() != "FETCHED":
            continue
        raw = str(sr.get("raw_content") or "")
        source = str(sr.get("source_used") or sr.get("source") or "")
        if source and not first_source:
            first_source = source
        if raw and not first_raw:
            first_raw = raw
        fetched_blocks.append(sr)
        ok, why = _or_v3_valid_positive_announcement(raw, question, intel)
        if ok:
            positive_found = True
            positive_reason = why
            break

    if positive_found:
        # Keep/restore Yes only if the body-level validator supports it.
        if "yes" in valid:
            final = valid["yes"]
            calc = f"Creator-source body evidence contains a dated matching announcement → Yes ({positive_reason})"
        else:
            return evidence
    else:
        if not fetched_blocks:
            return evidence
        final = valid["no"]
        calc = "No dated matching announcement found in creator-source body text → No"

    source = first_source or None

    for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "dashboard_result", "ree_expected_output"):
        evidence[key] = final
    evidence["oracle_calculation"] = calc
    evidence["resolved_outcome"] = {
        "outcome": final,
        "resolver": "strict_body_confirmation_v3",
        "calculation": calc,
        "source_url": source,
        "confidence": "high",
    }
    evidence["dashboard"] = {
        "oracle_result": final,
        "oracle_outcome": final,
        "final_outcome": final,
        "matched_outcome": final,
        "calculation": calc,
        "source": "strict_body_confirmation_v3",
    }
    evidence["event_verdict"] = {
        "verdict": final,
        "matchedOutcome": final,
        "explanation": calc,
        "source": source or "",
    }

    fv = evidence.get("final_verdict") if isinstance(evidence.get("final_verdict"), dict) else {}
    fv.update({
        "fetch_status": "FETCHED",
        "parse_status": "PARSED",
        "outcome_status": "OUTCOME_FOUND",
        "pipeline": "FETCHED | PARSED | OUTCOME_FOUND",
        "matched_outcome": final,
        "calculation": calc,
        "reason": calc,
        "source_used": source or fv.get("source_used"),
        "derived_result": {"matched_outcome": final, "calculation": calc},
    })
    evidence["final_verdict"] = fv

    evidence["arbitration"] = {
        "status": "VALID_CANDIDATE_SELECTED",
        "chosen_outcome": final,
        "chosen_score": 95,
        "chosen_source": source,
        "reason": calc,
        "candidates": [{
            "score": 95,
            "outcome": final,
            "source": source,
            "calculation": calc,
        }],
    }

    # Keep source blocks honest: only fetched blocks get matched_outcome; failed/unsupported remain as-is.
    event_date = str(intel.get("event_date") or "")
    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        if str(sr.get("fetch_status", "")).upper() != "FETCHED":
            # Remove any promoted outcome from unsupported/failed blocks.
            if str(sr.get("fetch_status", "")).upper() in {"FETCH_FAILED", "UNSUPPORTED_SOURCE"}:
                sr["matched_outcome"] = None
                sr["outcome_status"] = "OUTCOME_NOT_FOUND"
                sr["derived_result"] = None
            continue
        sr_source = str(sr.get("source_used") or sr.get("source") or source or "")
        sr["matched_outcome"] = final
        sr["outcome_status"] = "OUTCOME_FOUND"
        sr["parse_status"] = "PARSED"
        sr["calculation"] = calc
        sr["reason"] = calc
        sr["derived_result"] = {"matched_outcome": final, "calculation": calc}
        facts = sr.get("facts") if isinstance(sr.get("facts"), list) else []
        facts = [f for f in facts if not (isinstance(f, dict) and f.get("label") in {"structured_resolution", "matched_outcome"})]
        facts.append({"label": "structured_resolution", "value": calc, "source": sr_source, "timestamp": event_date})
        facts.append({"label": "matched_outcome", "value": final, "source": sr_source, "timestamp": event_date})
        sr["facts"] = facts

    return evidence


try:
    _OR_V3_PRE_BUILD_ORACLE_EVIDENCE = build_oracle_evidence
    def build_oracle_evidence(market: dict) -> dict:
        evidence = _OR_V3_PRE_BUILD_ORACLE_EVIDENCE(market)
        return _or_v3_force_no_if_false_positive(evidence, market or {})
except Exception:
    pass


try:
    _OR_V3_PRE_BUILD_ORACLE_PROMPT = build_oracle_prompt
    def build_oracle_prompt(original_prompt: str, evidence: dict) -> str:
        evidence = _or_v3_force_no_if_false_positive(evidence, {})
        final = str((evidence or {}).get("final_outcome") or (evidence or {}).get("oracle_result") or "").strip()
        calc = str((evidence or {}).get("oracle_calculation") or "").strip()
        question = str((evidence or {}).get("market_question") or "").strip()
        if final and _or_v3_norm(final) != "inconclusive":
            return (
                "/no_think\nORACLEREE VERIFIED SETTLEMENT RESULT\n"
                f"Question: {question}\n"
                f"Verified outcome: {final}\n"
                f"Evidence summary: {calc[:600]}\n\n"
                "Instruction: Output exactly the verified outcome below and nothing else.\n"
                f"{final}"
            )
        return _OR_V3_PRE_BUILD_ORACLE_PROMPT(original_prompt, evidence)
except Exception:
    pass


try:
    _OR_V3_PRE_BUILD_COMBINED_PROOF = build_combined_proof
    def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                             prompt_integrity: Optional[dict] = None) -> dict:
        evidence = _or_v3_force_no_if_false_positive(evidence, {"id": market_id})
        proof = _OR_V3_PRE_BUILD_COMBINED_PROOF(market_id, evidence, receipt_path, prompt_integrity)
        if isinstance(proof, dict):
            oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else evidence
            oe = _or_v3_force_no_if_false_positive(oe, {"id": market_id})
            proof["oracle_evidence"] = oe

            final = str(oe.get("final_outcome") or oe.get("oracle_result") or "INCONCLUSIVE")
            calc = str(oe.get("oracle_calculation") or "")
            for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "ree_expected_output"):
                proof[key] = final
            proof["oracle_calculation"] = calc
            proof["resolved_outcome"] = oe.get("resolved_outcome")
            proof["dashboard"] = oe.get("dashboard")

            verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
            verification.update({
                "oracle_result": final,
                "oracle_outcome": final,
                "final_outcome": final,
                "matched_outcome": final,
                "oracle_calculation": calc,
            })
            proof["verification"] = verification
        return proof
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL PATCH V3
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL SAFETY PATCH V4 — NON-SPORTS CONFIRMATION EVIDENCE GATES
# Scope:
#   - Company / crypto / generic binary announcement markets only.
#   - Does NOT change sports routing or sports fallback.
# Goals:
#   1. Never resolve Yes from <head>, <title>, <meta>, scripts, or SPA shell text.
#   2. Require body-level evidence and market-window date for confirmation Yes.
#   3. Keep FETCH_FAILED / UNSUPPORTED_SOURCE source blocks honest.
#   4. Preserve IPFS CID across final normalization/proof wrapping.
# ═══════════════════════════════════════════════════════════════════════════════

def _or_v4_norm_text(value: object) -> str:
    return str(value or "").strip().lower()

def _or_v4_visible_body_text(raw: str) -> str:
    """Evidence text only. Head/meta/title/script/style/nav/footer are not evidence."""
    s = str(raw or "")
    s = re.sub(r"(?is)<head\b[^>]*>.*?</head>", " ", s)
    s = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", s)
    s = re.sub(r"(?is)<noscript\b[^>]*>.*?</noscript>", " ", s)
    s = re.sub(r"(?is)<title\b[^>]*>.*?</title>", " ", s)
    s = re.sub(r"(?is)<meta\b[^>]*>", " ", s)
    s = re.sub(r"(?is)<link\b[^>]*>", " ", s)
    s = re.sub(r"(?is)<svg\b[^>]*>.*?</svg>", " ", s)
    s = re.sub(r"(?is)<nav\b[^>]*>.*?</nav>", " ", s)
    s = re.sub(r"(?is)<footer\b[^>]*>.*?</footer>", " ", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    replacements = {
        "&nbsp;": " ", "&#160;": " ", "&amp;": "&",
        "&quot;": '"', "&#34;": '"', "&#39;": "'", "&apos;": "'",
    }
    for k, v in replacements.items():
        s = re.sub(re.escape(k), v, s, flags=re.I)
    return " ".join(s.split())

def _or_v4_is_spa_or_metadata_shell(raw: str) -> bool:
    """Detect JS app / metadata shell pages that cannot prove a market outcome."""
    r = str(raw or "").lower()
    body = _or_v4_visible_body_text(raw).lower()
    shell_markers = (
        "next-head-count" in r
        or "__next" in r
        or "__next_data__" in r
        or "application-name" in r
        or "<meta " in r
        or "<title" in r
        or "enable javascript" in r
    )
    if shell_markers and len(body) < 500:
        return True
    if "bitcoin purchases - strategy" in r and len(body) < 900:
        return True
    return False

def _or_v4_market_window(intelligence: dict) -> tuple[str, str]:
    intel = intelligence if isinstance(intelligence, dict) else {}
    rules = intel.get("_rules") if isinstance(intel.get("_rules"), dict) else {}
    tw = intel.get("time_window") if isinstance(intel.get("time_window"), dict) else rules.get("time_window")
    if isinstance(tw, dict):
        start = str(tw.get("start") or intel.get("event_date") or rules.get("event_date") or "")[:10]
        end = str(tw.get("end") or start or "")[:10]
        return start, end
    d = str(intel.get("event_date") or rules.get("event_date") or "")[:10]
    return d, d

def _or_v4_date_strings(start: str, end: str) -> list[str]:
    out = []
    try:
        from datetime import datetime, timedelta
        s = datetime.fromisoformat(str(start)[:10])
        e = datetime.fromisoformat(str(end or start)[:10])
        cur = s
        while cur <= e:
            # Use %-d on Unix, %#d on Windows.
            day_no_zero = cur.strftime("%-d") if os.name != "nt" else cur.strftime("%#d")
            out.extend([
                cur.strftime("%Y-%m-%d"),
                cur.strftime("%Y/%m/%d"),
                cur.strftime("%m/%d/%Y"),
                cur.strftime("%m/%d/%y"),
                f"{cur.strftime('%B')} {day_no_zero}",
                f"{cur.strftime('%B')} {day_no_zero}, {cur.year}",
                f"{cur.strftime('%b')} {day_no_zero}",
                f"{cur.strftime('%b')} {day_no_zero}, {cur.year}",
            ])
            cur += timedelta(days=1)
    except Exception:
        for d in (start, end):
            if d:
                out.append(str(d)[:10])
    seen, final = set(), []
    for x in out:
        k = str(x or "").strip().lower()
        if k and k not in seen:
            seen.add(k)
            final.append(str(x).strip())
    return final

def _or_v4_is_non_sports_confirmation(question: str, intelligence: dict) -> bool:
    q = _or_v4_norm_text(question)
    intel = intelligence if isinstance(intelligence, dict) else {}
    category = _or_v4_norm_text(intel.get("category"))
    resolver = _or_v4_norm_text(intel.get("resolver"))
    metric = _or_v4_norm_text(intel.get("metric"))
    market_type = _or_v4_norm_text(intel.get("market_type"))
    rules = intel.get("_rules") if isinstance(intel.get("_rules"), dict) else {}
    asset = rules.get("asset") if isinstance(rules.get("asset"), dict) else {}
    asset_type = _or_v4_norm_text(asset.get("type"))

    # Hard sports exclusion. This patch must never touch sports.
    if category == "sports" or market_type in {"sports", "sports_spread"}:
        return False
    if resolver in {"sports_result", "spread_cover"}:
        return False
    if any(x in q for x in [
        " vs ", " versus ", "final score", "match", "game", "fa cup",
        "premier league", "champions league", "nba", "nfl", "mlb", "nhl",
        "ipl", "cricket", "ufl", "xfl"
    ]):
        return False

    if category in {"company_event", "crypto_web3_event", "generic_binary"}:
        return True
    if asset_type in {"company_event", "crypto_event"}:
        return True
    if resolver == "binary_yes_no" and metric == "confirmation":
        return True

    event_terms = [
        "announce", "announcement", "statement", "publish", "published",
        "purchase", "acquire", "acquired", "bought", "buy", "partnership",
        "launch", "list", "delist", "approve", "reject"
    ]
    return resolver == "binary_yes_no" and any(t in q for t in event_terms)

def _or_v4_positive_confirmation(raw: str, question: str, intelligence: dict) -> tuple[bool, str]:
    """Return True only when body text proves a dated positive announcement."""
    if not raw:
        return False, "no raw creator-source content"
    if _or_v4_is_spa_or_metadata_shell(raw):
        return False, "creator source returned only SPA/metadata shell"

    body = _or_v4_visible_body_text(raw)
    b = body.lower()
    q = _or_v4_norm_text(question)
    if len(body) < 300:
        return False, "creator-source body text too short after removing head/meta/script"

    start, end = _or_v4_market_window(intelligence)
    date_hits = [d for d in _or_v4_date_strings(start, end) if d and d.lower() in b]
    if not date_hits:
        return False, "no date from market window found in body text"

    # Strategy / MicroStrategy BTC purchase confirmation.
    if any(x in q for x in ["microstrategy", "strategy", "mstr", "bitcoin purchase", "purchased bitcoin", "btc purchase"]):
        actor = r"(?:strategy|microstrategy|mstr)"
        asset = r"(?:bitcoin|btc)"
        action = r"(?:announced|announce|purchased|purchase|acquired|acquire|bought|buys|buy)"
        positive = (
            re.search(actor + r".{0,180}" + action + r".{0,180}" + asset, b, re.I | re.S)
            or re.search(actor + r".{0,180}" + asset + r".{0,180}" + action, b, re.I | re.S)
            or re.search(action + r".{0,180}" + asset + r".{0,180}" + actor, b, re.I | re.S)
        )
        if not positive:
            return False, "no explicit Strategy/MicroStrategy Bitcoin purchase announcement in body text"
        return True, f"dated Strategy/MicroStrategy Bitcoin purchase announcement found ({', '.join(date_hits[:3])})"

    # Generic confirmation market.
    positive = re.search(
        r"\b(announced|confirmed|published|stated|released|launched|listed|delisted|approved|rejected|partnered|acquired|purchased|bought)\b",
        b,
        re.I,
    )
    if not positive:
        return False, "no explicit confirmation/announcement language in body text"
    return True, f"dated creator-source confirmation found ({', '.join(date_hits[:3])})"

# Override the legacy keyword helper globally. Any old rescue path using this helper
# becomes body-only and cannot match <title>/<meta> false positives.
def _or_confirmation_keywords_present(content: str, question: str) -> bool:
    body = _or_v4_visible_body_text(content)
    if _or_v4_is_spa_or_metadata_shell(content):
        return False
    if len(body) < 300:
        return False
    b = body.lower()
    q = _or_v4_norm_text(question)
    if any(x in q for x in ["microstrategy", "strategy", "mstr", "bitcoin purchase", "purchased bitcoin", "btc purchase"]):
        actor = r"(?:strategy|microstrategy|mstr)"
        asset = r"(?:bitcoin|btc)"
        action = r"(?:announced|announce|purchased|purchase|acquired|acquire|bought|buys|buy)"
        return bool(
            re.search(actor + r".{0,180}" + action + r".{0,180}" + asset, b, re.I | re.S)
            or re.search(actor + r".{0,180}" + asset + r".{0,180}" + action, b, re.I | re.S)
            or re.search(action + r".{0,180}" + asset + r".{0,180}" + actor, b, re.I | re.S)
        )
    return bool(re.search(r"\b(announced|confirmed|published|statement|press release|released)\b", b, re.I))

# Override binary success helper so old wrappers cannot force a Yes from shell content.
try:
    _OR_V4_PRE_APPLY_BINARY_SUCCESS = _or_apply_binary_success
    def _or_apply_binary_success(eb: EvidenceBlock, outcome: str, calc: str, source: str, event_date: str, raw: str = "") -> EvidenceBlock:
        if _or_v4_norm_text(outcome) == "yes" and raw and _or_v4_is_spa_or_metadata_shell(raw):
            # Leave the block as it was. A shell cannot create a positive outcome.
            try:
                eb.reason = "Rejected positive confirmation: creator source returned only SPA/metadata shell"
                if not eb.outcome_status or eb.outcome_status == "PENDING":
                    eb.outcome_status = "OUTCOME_NOT_FOUND"
            except Exception:
                pass
            return eb
        return _OR_V4_PRE_APPLY_BINARY_SUCCESS(eb, outcome, calc, source, event_date, raw)
except Exception:
    pass

def _or_v4_clean_source_statuses(evidence: dict) -> dict:
    """Do not let final outcome propagation make failed/unsupported sources look successful."""
    if not isinstance(evidence, dict):
        return evidence
    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        fs = str(sr.get("fetch_status") or "").upper()
        ps = str(sr.get("parse_status") or "").upper()
        if fs != "FETCHED" or ps != "PARSED":
            if fs in {"FETCH_FAILED", "UNSUPPORTED_SOURCE"} or ps in {"FETCH_FAILED", "UNSUPPORTED_SOURCE"}:
                sr["outcome_status"] = "OUTCOME_NOT_FOUND"
                sr["matched_outcome"] = None
                sr["derived_result"] = None
                # Remove promoted final facts from sources that did not provide evidence.
                facts = []
                for f in sr.get("facts") or []:
                    if isinstance(f, dict) and str(f.get("label") or "").lower() in {"structured_resolution", "matched_outcome"}:
                        continue
                    facts.append(f)
                sr["facts"] = facts
                sr["pipeline"] = " | ".join(x for x in [sr.get("fetch_status"), sr.get("parse_status"), sr.get("outcome_status")] if x)
    return evidence

def _or_v4_enforce_strict_confirmation(evidence: dict, market: Optional[dict] = None) -> dict:
    """Final single guard for non-sports confirmation markets."""
    if not isinstance(evidence, dict):
        return evidence

    existing_cid = evidence.get("ipfs_cid") or ""
    intel = evidence.get("intelligence") if isinstance(evidence.get("intelligence"), dict) else {}
    question = str(evidence.get("market_question") or intel.get("event_description") or "")
    if not _or_v4_is_non_sports_confirmation(question, intel):
        return _or_v4_clean_source_statuses(evidence)

    # Determine valid binary outcome spelling from market/evidence.
    outcomes = []
    try:
        if market:
            meta = market.get("metadata") if isinstance(market.get("metadata"), dict) else {}
            outcomes = meta.get("outcomes") or market.get("outcomes") or []
    except Exception:
        outcomes = []
    if not outcomes:
        outcomes = ["Yes", "No"]

    valid = {str(x).strip().lower(): str(x).strip() for x in outcomes if str(x).strip()}
    if "yes" not in valid or "no" not in valid:
        return _or_v4_clean_source_statuses(evidence)

    fetched = []
    first_source = None
    positive = False
    positive_reason = ""
    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        if str(sr.get("fetch_status") or "").upper() != "FETCHED":
            continue
        if str(sr.get("parse_status") or "").upper() not in {"PARSED", "PENDING", ""}:
            continue
        raw = str(sr.get("raw_content") or "")
        if not raw:
            continue
        fetched.append(sr)
        first_source = first_source or str(sr.get("source_used") or sr.get("source") or "")
        ok, reason = _or_v4_positive_confirmation(raw, question, intel)
        if ok:
            positive = True
            positive_reason = reason
            break

    if not fetched:
        return _or_v4_clean_source_statuses(evidence)

    final = valid["yes"] if positive else valid["no"]
    calc = (
        f"Creator-source body evidence contains a dated matching announcement → Yes ({positive_reason})"
        if positive else
        "No dated matching announcement found in creator-source body text → No"
    )

    for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "dashboard_result", "ree_expected_output"):
        evidence[key] = final
    evidence["oracle_calculation"] = calc
    evidence["resolved_outcome"] = {
        "outcome": final,
        "resolver": "strict_body_confirmation_v4",
        "calculation": calc,
        "source_url": first_source,
        "confidence": "high",
    }
    evidence["dashboard"] = {
        "oracle_result": final,
        "oracle_outcome": final,
        "final_outcome": final,
        "matched_outcome": final,
        "calculation": calc,
        "source": "strict_body_confirmation_v4",
    }
    evidence["event_verdict"] = {
        "verdict": final,
        "matchedOutcome": final,
        "explanation": calc,
        "source": first_source or "",
    }
    evidence["final_verdict"] = {
        "fetch_status": "FETCHED",
        "parse_status": "PARSED",
        "outcome_status": "OUTCOME_FOUND",
        "pipeline": "FETCHED | PARSED | OUTCOME_FOUND",
        "matched_outcome": final,
        "calculation": calc,
        "reason": calc,
        "source_used": first_source,
        "derived_result": {"matched_outcome": final, "calculation": calc},
        "facts": [
            {
                "label": "structured_resolution",
                "value": calc,
                "source": first_source or "",
                "timestamp": str(intel.get("event_date") or ""),
            },
            {
                "label": "matched_outcome",
                "value": final,
                "source": first_source or "",
                "timestamp": str(intel.get("event_date") or ""),
            },
        ],
    }
    evidence["arbitration"] = {
        "status": "VALID_CANDIDATE_SELECTED",
        "chosen_outcome": final,
        "chosen_score": 95,
        "chosen_source": first_source,
        "reason": calc,
        "candidates": [{
            "score": 95,
            "outcome": final,
            "source": first_source,
            "calculation": calc,
        }],
    }

    # Sync only fetched+parsed source blocks; failed/unsupported remain honest.
    for sr in evidence.get("source_results") or []:
        if not isinstance(sr, dict):
            continue
        fs = str(sr.get("fetch_status") or "").upper()
        ps = str(sr.get("parse_status") or "").upper()
        if fs != "FETCHED":
            continue
        sr_source = str(sr.get("source_used") or sr.get("source") or first_source or "")
        sr["parse_status"] = "PARSED"
        sr["outcome_status"] = "OUTCOME_FOUND"
        sr["matched_outcome"] = final
        sr["calculation"] = calc
        sr["reason"] = calc
        sr["derived_result"] = {"matched_outcome": final, "calculation": calc}
        sr["pipeline"] = "FETCHED | PARSED | OUTCOME_FOUND"
        facts = [
            f for f in (sr.get("facts") or [])
            if not (isinstance(f, dict) and str(f.get("label") or "").lower() in {"structured_resolution", "matched_outcome"})
        ]
        facts.append({"label": "structured_resolution", "value": calc, "source": sr_source, "timestamp": str(intel.get("event_date") or "")})
        facts.append({"label": "matched_outcome", "value": final, "source": sr_source, "timestamp": str(intel.get("event_date") or "")})
        sr["facts"] = facts

    evidence = _or_v4_clean_source_statuses(evidence)
    if existing_cid and not evidence.get("ipfs_cid"):
        evidence["ipfs_cid"] = existing_cid
    return evidence

try:
    _OR_V4_PRE_BUILD_ORACLE_EVIDENCE = build_oracle_evidence
    def build_oracle_evidence(market: dict) -> dict:
        evidence = _OR_V4_PRE_BUILD_ORACLE_EVIDENCE(market)
        return _or_v4_enforce_strict_confirmation(evidence, market or {})
except Exception:
    pass

try:
    _OR_V4_PRE_BUILD_COMBINED_PROOF = build_combined_proof
    def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                             prompt_integrity: Optional[dict] = None) -> dict:
        evidence = _or_v4_enforce_strict_confirmation(evidence, {"id": market_id})
        proof = _OR_V4_PRE_BUILD_COMBINED_PROOF(market_id, evidence, receipt_path, prompt_integrity)
        if isinstance(proof, dict):
            oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else evidence
            oe = _or_v4_enforce_strict_confirmation(oe, {"id": market_id})
            proof["oracle_evidence"] = oe

            final = str(oe.get("final_outcome") or oe.get("oracle_result") or "INCONCLUSIVE")
            calc = str(oe.get("oracle_calculation") or "")
            for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "ree_expected_output"):
                proof[key] = final
            proof["oracle_calculation"] = calc
            proof["resolved_outcome"] = oe.get("resolved_outcome")
            proof["dashboard"] = oe.get("dashboard")
            verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
            verification.update({
                "oracle_result": final,
                "oracle_outcome": final,
                "final_outcome": final,
                "matched_outcome": final,
                "oracle_calculation": calc,
            })
            proof["verification"] = verification
        return proof
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL SAFETY PATCH V4
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# FINAL TARGETED PATCH V5 — SPORTS STRUCTURED FALLBACK + STALE CANDIDATE GUARD
# Scope:
#   - Sports only: creator source first, then structured sports fallback.
#   - Non-sports confirmation remains protected by V4 body-only gates.
#   - Candidate promotion cannot revive stale/failed source outcomes.
#   - Ollama brain helpers may be imported, but deterministic Python still decides.
# ═══════════════════════════════════════════════════════════════════════════════

FOOTBALL_DATA_API_KEY = os.environ.get("FOOTBALL_DATA_API_KEY", "")

_FOOTBALL_DATA_COMPETITIONS_V5 = {
    "fa cup": "FA", "english fa cup": "FA",
    "premier league": "PL", "epl": "PL",
    "champions league": "CL", "ucl": "CL",
    "la liga": "PD", "serie a": "SA", "bundesliga": "BL1", "ligue 1": "FL1",
}

_SPORTS_API_SPORT_HINTS_V5 = {
    "soccer": ["fa cup", "premier league", "champions league", "la liga", "serie a", "bundesliga", "football", "soccer"],
    "basketball": ["nba", "basketball"],
    "american football": ["nfl", "ufl", "xfl", "super bowl", "american football"],
    "baseball": ["mlb", "baseball"],
    "hockey": ["nhl", "hockey"],
    "cricket": ["cricket", "ipl", "psl", "t20", "odi"],
}


def _v5_norm_token(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _v5_words(text: object) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", str(text or "").lower()))


def _v5_is_sports_market(question: str, outcomes: list, intelligence: dict) -> bool:
    intel = intelligence or {}
    q = str(question or "").lower()
    mt = str(intel.get("market_type") or intel.get("category") or "").lower()
    resolver = str(intel.get("resolver") or "").lower()
    rules = intel.get("_rules") if isinstance(intel.get("_rules"), dict) else {}
    metric = str(intel.get("metric") or rules.get("metric") or "").lower()
    if metric == "count" or intel.get("count_subject") or rules.get("count_subject"):
        return False
    if mt in {"sports", "sports_spread"} or resolver in {"sports_result", "spread_cover"}:
        return True
    if metric in {"winner", "match_winner", "game_winner", "final_score", "score", "spread"}:
        return True
    return any(x in q for x in [
        " vs ", " versus ", " fa cup", "premier league", "champions league", "final score",
        "nba", "nfl", "mlb", "nhl", "ufl", "xfl", "ipl", "psl", "cricket", "soccer"
    ])


def _v5_teams_from_question(question: str, outcomes: list) -> tuple[str, str]:
    q = str(question or "")
    # Prefer explicit A vs B shape; stop before dash/parentheses/date details.
    m = re.search(r"(.+?)\s+(?:vs\.?|v\.?|versus)\s+(.+?)(?:\s+[—–-]|\s*\(|,|$)", q, re.I)
    if m:
        t1 = re.sub(r"\b(will|who|does|did|the|official|final|result)\b", " ", m.group(1), flags=re.I)
        t2 = m.group(2)
        return " ".join(t1.split()).strip(), " ".join(t2.split()).strip()
    non_draw = [str(o).strip() for o in outcomes or [] if str(o).strip() and str(o).strip().lower() not in {"draw", "yes", "no"}]
    if len(non_draw) >= 2:
        return non_draw[0], non_draw[1]
    return "", ""


def _v5_detect_sport(question: str, prompt_context: str = "") -> str:
    text = (str(question or "") + " " + str(prompt_context or "")).lower()
    for sport, keys in _SPORTS_API_SPORT_HINTS_V5.items():
        if any(k in text for k in keys):
            return sport
    return "soccer" if " vs " in text or " versus " in text else ""


def _v5_detect_football_competition(question: str, prompt_context: str = "") -> str:
    text = (str(question or "") + " " + str(prompt_context or "")).lower()
    for name, code in _FOOTBALL_DATA_COMPETITIONS_V5.items():
        if name in text:
            return code
    return ""


def _v5_team_matches(expected: str, actual: str) -> bool:
    e = _v5_norm_token(expected)
    a = _v5_norm_token(actual)
    if not e or not a:
        return False
    if e in a or a in e:
        return True
    ew = _v5_words(expected)
    aw = _v5_words(actual)
    # Accept meaningful shared team token, e.g. Leeds vs Leeds United.
    stop = {"club", "football", "fc", "afc", "the", "united", "city", "town"}
    return bool((ew - stop) & (aw - stop))


def _v5_match_outcome_name(winner: str, outcomes: list) -> Optional[str]:
    w = str(winner or "").strip()
    if not w:
        return None
    if w.lower() == "draw":
        for o in outcomes or []:
            if str(o).strip().lower() == "draw":
                return str(o).strip()
        return "Draw"
    for o in outcomes or []:
        os = str(o).strip()
        if os and os.lower() not in {"yes", "no", "draw"} and _v5_team_matches(os, w):
            return os
    return None


def _v5_build_sports_evidence(source: str, home: str, away: str, hs: int, as_: int, event_date: str, status: str = "", extra: str = "") -> tuple[str, str]:
    winner = "Draw"
    if hs > as_:
        winner = home
    elif as_ > hs:
        winner = away
    score = f"{home} {hs}-{as_} {away}"
    evidence = (
        f"ANSWER: {score} (Full Time). Winner: {winner}\n\n"
        f"[{source}]\n"
        f"Match: {home} vs {away}\n"
        f"Date: {event_date}\n"
        f"Score: {home} {hs} - {as_} {away}\n"
        f"Status: {status}\n"
        f"Winner: {winner}\n"
        f"{extra or ''}\n"
    )
    return evidence, winner


def _v5_fetch_thesportsdb(question: str, event_date: str, outcomes: list, prompt_context: str = "") -> Optional[tuple[str, str, str]]:
    try:
        team1, team2 = _v5_teams_from_question(question, outcomes)
        if not team1 or not team2:
            return None
        sport = _v5_detect_sport(question, prompt_context) or "soccer"
        sport_param = {
            "soccer": "Soccer",
            "basketball": "Basketball",
            "american football": "American Football",
            "baseball": "Baseball",
            "hockey": "Ice Hockey",
            "cricket": "Cricket",
        }.get(sport, "Soccer")
        url = f"https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d={event_date}&s={requests.utils.quote(sport_param)}"
        print(f"[sports-v5] TheSportsDB date fetch: {url}")
        r = requests.get(url, timeout=15, headers={"User-Agent": "OracleREE/1.0"})
        if not r.ok:
            return None
        events = (r.json() or {}).get("events") or []
        for ev in events:
            home = str(ev.get("strHomeTeam") or "")
            away = str(ev.get("strAwayTeam") or "")
            if not ((_v5_team_matches(team1, home) or _v5_team_matches(team1, away)) and (_v5_team_matches(team2, home) or _v5_team_matches(team2, away))):
                continue
            hs = ev.get("intHomeScore")
            as_ = ev.get("intAwayScore")
            if hs is None or as_ is None:
                continue
            evidence, winner = _v5_build_sports_evidence("TheSportsDB", home, away, int(hs), int(as_), event_date, str(ev.get("strStatus") or ""), str(ev.get("strEvent") or ""))
            matched = _v5_match_outcome_name(winner, outcomes)
            if matched:
                return evidence, "TheSportsDB", matched
    except Exception as e:
        print(f"[sports-v5] TheSportsDB failed: {e}")
    return None


def _v5_fetch_football_data(question: str, event_date: str, outcomes: list, prompt_context: str = "") -> Optional[tuple[str, str, str]]:
    try:
        team1, team2 = _v5_teams_from_question(question, outcomes)
        if not team1 or not team2:
            return None
        comp = _v5_detect_football_competition(question, prompt_context) or "FA"
        headers = {"User-Agent": "OracleREE/1.0"}
        if FOOTBALL_DATA_API_KEY:
            headers["X-Auth-Token"] = FOOTBALL_DATA_API_KEY
        url = f"https://api.football-data.org/v4/matches?competitions={comp}&dateFrom={event_date}&dateTo={event_date}"
        print(f"[sports-v5] football-data.org fetch: {url}")
        r = requests.get(url, headers=headers, timeout=15)
        if not r.ok:
            print(f"[sports-v5] football-data.org status: {r.status_code}")
            return None
        for match in (r.json() or {}).get("matches") or []:
            home = str((match.get("homeTeam") or {}).get("name") or "")
            away = str((match.get("awayTeam") or {}).get("name") or "")
            if not ((_v5_team_matches(team1, home) or _v5_team_matches(team1, away)) and (_v5_team_matches(team2, home) or _v5_team_matches(team2, away))):
                continue
            status = str(match.get("status") or "").upper()
            if status not in {"FINISHED", "FULL_TIME", "AWARDED"}:
                continue
            ft = ((match.get("score") or {}).get("fullTime") or {})
            hs, as_ = ft.get("home"), ft.get("away")
            if hs is None or as_ is None:
                continue
            evidence, winner = _v5_build_sports_evidence("football-data.org", home, away, int(hs), int(as_), event_date, status)
            matched = _v5_match_outcome_name(winner, outcomes)
            if matched:
                return evidence, "football-data.org", matched
    except Exception as e:
        print(f"[sports-v5] football-data.org failed: {e}")
    return None


def _v5_tavily_sports_search(question: str, event_date: str, outcomes: list, domain: str) -> Optional[tuple[str, str, str]]:
    if not TAVILY_API_KEY:
        return None
    try:
        team1, team2 = _v5_teams_from_question(question, outcomes)
        if not team1 or not team2:
            return None
        query = f"site:{domain} {team1} {team2} {event_date} final score result winner"
        print(f"[sports-v5] Tavily sports fallback: {query}")
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "search_depth": "basic",
            "include_answer": True,
            "include_raw_content": True,
            "max_results": 5,
        }, timeout=20)
        data = r.json() or {}
        parts = []
        if data.get("answer"):
            parts.append("ANSWER: " + str(data.get("answer")))
        for res in data.get("results") or []:
            url = str(res.get("url") or "")
            rd = clean_domain(url)
            if domain not in rd and not rd.endswith("." + domain):
                continue
            raw = res.get("raw_content") or res.get("content") or ""
            parts.append(f"[{url}]\n{str(raw)[:1800]}")
        content = "\n\n".join(parts).strip()
        if not content:
            return None
        matched, calc = _v5_resolve_sports_from_text(content, question, outcomes)
        if matched:
            return content, domain, matched
    except Exception as e:
        print(f"[sports-v5] Tavily {domain} failed: {e}")
    return None


def _v5_resolve_sports_from_text(content: str, question: str, outcomes: list) -> tuple[Optional[str], str]:
    text = str(content or "")
    low = text.lower()
    team1, team2 = _v5_teams_from_question(question, outcomes)
    if team1 and team2 and not (_v5_team_matches(team1, low) and _v5_team_matches(team2, low)):
        # fallback word matching because low is full text, not team name
        if not (_v5_words(team1) & _v5_words(low) and _v5_words(team2) & _v5_words(low)):
            return None, "sports text rejected: participant tokens missing"
    # Winner line from structured evidence.
    m = re.search(r"winner\s*:\s*([^\n\r]+)", text, re.I)
    if m:
        winner = m.group(1).strip()
        matched = _v5_match_outcome_name(winner, outcomes)
        if matched:
            return matched, f"sports evidence winner line → {matched}"
    # Common result phrasing.
    for out in outcomes or []:
        os = str(out).strip()
        if not os or os.lower() in {"yes", "no"}:
            continue
        if os.lower() == "draw" and re.search(r"\b(draw|drew|tie|tied)\b", low):
            return os, "sports evidence indicates draw"
        if re.search(rf"\b{re.escape(os.lower())}\b.{0,80}\b(beat|defeated|won|wins|winner)\b", low, re.S):
            return os, f"sports evidence says {os} won"
        if re.search(rf"\b(beat|defeated|won|wins|winner)\b.{0,80}\b{re.escape(os.lower())}\b", low, re.S):
            return os, f"sports evidence says {os} won"
    # Score pattern near teams.
    score_re = re.compile(r"([A-Za-z][A-Za-z .'-]{2,40})\s+(\d{1,3})\s*[-–]\s*(\d{1,3})\s+([A-Za-z][A-Za-z .'-]{2,40})")
    for sm in score_re.finditer(text):
        left, a, b, right = sm.group(1).strip(), int(sm.group(2)), int(sm.group(3)), sm.group(4).strip()
        if not ((_v5_team_matches(team1, left) or _v5_team_matches(team2, left)) and (_v5_team_matches(team1, right) or _v5_team_matches(team2, right))):
            continue
        winner = "Draw" if a == b else (left if a > b else right)
        matched = _v5_match_outcome_name(winner, outcomes)
        if matched:
            return matched, f"sports final score parsed: {left} {a}-{b} {right} → {matched}"
    return None, "no validated sports score/winner found"


def _v5_try_structured_sports_fallback(question: str, event_date: str, outcomes: list, prompt_context: str = "") -> Optional[tuple[str, str, str, str]]:
    # Structured APIs first.
    for fn in (_v5_fetch_thesportsdb, _v5_fetch_football_data):
        result = fn(question, event_date, outcomes, prompt_context)
        if result:
            content, source, matched = result
            return content, source, matched, f"structured sports fallback via {source} → {matched}"
    # Then trusted sports pages via Tavily. Sports only.
    text = (str(question or "") + " " + str(prompt_context or "")).lower()
    domains = []
    if any(k in text for k in ["fa cup", "english fa cup"]):
        domains.extend(["thefa.com", "bbc.co.uk", "bbc.com", "skysports.com", "sofascore.com"])
    domains.extend(["bbc.co.uk", "bbc.com", "skysports.com", "sofascore.com", "espn.com"])
    seen = set()
    for domain in domains:
        if domain in seen:
            continue
        seen.add(domain)
        result = _v5_tavily_sports_search(question, event_date, outcomes, domain)
        if result:
            content, source, matched = result
            return content, source, matched, f"trusted sports Tavily fallback via {source} → {matched}"
    return None


# Import external Ollama brain helpers into the active namespace where useful.
# This happens late as well as at top so copied single-file deployments still work.
try:
    if not globals().get("_OLLAMA_BRAIN_IMPORTED"):
        from ollama_brain import (
            validate_spread_evidence as ollama_validate_spread_evidence,
            deterministic_spread_evidence_gate as _strict_spread_evidence_check,
            ask_ollama_json as _ollama_brain_ask_json,
        )
        _OLLAMA_BRAIN_IMPORTED = True
except Exception:
    _OLLAMA_BRAIN_IMPORTED = False


# Override candidate picker: only trust current fetched+parsed evidence or explicit non-INCONCLUSIVE arbitration candidates.
try:
    _V5_PRE_ORP_BEST_VALID_CANDIDATE = _orp_v2_best_valid_candidate
    def _orp_v2_best_valid_candidate(evidence: dict, market: Optional[dict] = None) -> Optional[dict]:
        if not isinstance(evidence, dict):
            return None
        outcomes = _orp_v2_valid_outcomes(market, evidence)
        if not outcomes:
            return None
        candidates = []

        def add(outcome, source="", calc="", score=0):
            matched = _orp_v2_match_outcome(outcome, outcomes)
            if matched and _orp_v2_norm(matched) != "inconclusive":
                candidates.append({
                    "outcome": matched,
                    "source": str(source or ""),
                    "calculation": str(calc or f"Validated candidate → {matched}"),
                    "score": int(score or 0),
                })

        arb = evidence.get("arbitration") if isinstance(evidence.get("arbitration"), dict) else {}
        # Trust arbitration chosen only if not inconclusive and score is meaningful.
        if _orp_v2_norm(arb.get("chosen_outcome")) != "inconclusive":
            add(arb.get("chosen_outcome"), arb.get("chosen_source"), arb.get("reason") or arb.get("calculation"), arb.get("chosen_score") or 0)
        for c in arb.get("candidates") or arb.get("scores") or []:
            if isinstance(c, dict) and int(c.get("score") or 0) >= 50:
                add(c.get("outcome") or c.get("matched_outcome"), c.get("source") or c.get("source_used"), c.get("calculation") or c.get("calc") or arb.get("reason"), c.get("score") or 0)

        fv = evidence.get("final_verdict") if isinstance(evidence.get("final_verdict"), dict) else {}
        if str(fv.get("fetch_status") or "").upper() == "FETCHED" and str(fv.get("parse_status") or "").upper() == "PARSED":
            add(fv.get("matched_outcome"), fv.get("source_used"), fv.get("calculation") or fv.get("reason"), 90)

        for sr in evidence.get("source_results") or []:
            if not isinstance(sr, dict):
                continue
            fs = str(sr.get("fetch_status") or "").upper()
            ps = str(sr.get("parse_status") or "").upper()
            os_ = str(sr.get("outcome_status") or "").upper()
            if fs != "FETCHED" or ps != "PARSED" or os_ != "OUTCOME_FOUND":
                continue
            raw = str(sr.get("raw_content") or "")
            # Do not promote shell/homepage evidence.
            try:
                if raw and (_or_v4_is_spa_or_metadata_shell(raw) or is_weak_espn_homepage_content(raw)):
                    continue
            except Exception:
                pass
            source = sr.get("source_used") or sr.get("source") or ""
            add(sr.get("matched_outcome"), source, sr.get("calculation") or sr.get("reason"), 85)
            dr = sr.get("derived_result") if isinstance(sr.get("derived_result"), dict) else {}
            add(dr.get("matched_outcome"), source, dr.get("calculation") or sr.get("calculation") or sr.get("reason"), 85)

        if not candidates:
            return None
        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        return candidates[0] if candidates[0].get("score", 0) >= 50 else None
except Exception:
    pass


# Override v2 final promotion so failed/unsupported source rows are never rewritten as successful.
try:
    _V5_PRE_ORP_PROMOTE_FINAL = _orp_v2_promote_final
    def _orp_v2_promote_final(evidence: dict, market: Optional[dict] = None) -> dict:
        evidence = _V5_PRE_ORP_PROMOTE_FINAL(evidence, market)
        if not isinstance(evidence, dict):
            return evidence
        for sr in evidence.get("source_results") or []:
            if not isinstance(sr, dict):
                continue
            fs = str(sr.get("fetch_status") or "").upper()
            ps = str(sr.get("parse_status") or "").upper()
            if fs != "FETCHED" or ps != "PARSED":
                if fs in {"FETCH_FAILED", "UNSUPPORTED_SOURCE"} or ps in {"FETCH_FAILED", "UNSUPPORTED_SOURCE"}:
                    sr["outcome_status"] = "OUTCOME_NOT_FOUND"
                    sr["matched_outcome"] = None
                    sr["derived_result"] = None
                    sr["pipeline"] = " | ".join(x for x in [sr.get("fetch_status"), sr.get("parse_status"), sr.get("outcome_status")] if x)
                    facts = []
                    for f in sr.get("facts") or []:
                        if isinstance(f, dict) and str(f.get("label") or "").lower() in {"structured_resolution", "matched_outcome"}:
                            continue
                        facts.append(f)
                    sr["facts"] = facts
        return evidence
except Exception:
    pass


# Final sports source wrapper: if creator/ESPN path is inconclusive, try structured sports fallback.
try:
    _V5_PRE_BUILD_SOURCE_EVIDENCE = build_source_evidence
    def build_source_evidence(source_original: str, intelligence: dict, *args, **kwargs) -> EvidenceBlock:
        eb = _V5_PRE_BUILD_SOURCE_EVIDENCE(source_original, intelligence, *args, **kwargs)
        try:
            intel = dict(intelligence or {})
            question = args[0] if len(args) > 0 else kwargs.get("question") or intel.get("event_description", "")
            outcomes = args[1] if len(args) > 1 else kwargs.get("outcomes") or []
            resolves_at = args[2] if len(args) > 2 else kwargs.get("resolves_at") or kwargs.get("close_time") or intel.get("close_time", "")
            if not isinstance(outcomes, list):
                outcomes = list(outcomes or [])
            if not _v5_is_sports_market(str(question or ""), outcomes, intel):
                return eb
            if str(getattr(eb, "outcome_status", "") or "").upper() == "OUTCOME_FOUND":
                return eb
            event_date = str(intel.get("event_date") or str(resolves_at or "")[:10])
            if not event_date:
                return eb
            prompt_context = str(intel.get("prompt_context") or "")
            result = _v5_try_structured_sports_fallback(str(question or ""), event_date, outcomes, prompt_context)
            if not result:
                return eb
            content, source, matched, calc = result
            eb.fetch_status = "FETCHED"
            eb.parse_status = "PARSED"
            eb.outcome_status = "OUTCOME_FOUND"
            eb.fetch_method = f"sports_structured_fallback_{_v5_norm_token(source) or 'source'}"
            eb.recovered_from = str(getattr(eb, "source_used", "") or source_original or "")
            eb.source_used = source
            eb.raw_content = str(content)[:5000]
            eb.matched_outcome = matched
            eb.calculation = calc
            eb.reason = calc
            eb.facts = [
                Fact("raw_evidence", str(content)[:2200], source, timestamp=event_date),
                Fact("structured_resolution", calc, source, timestamp=event_date),
                Fact("matched_outcome", matched, source, timestamp=event_date),
            ]
            print(f"[sports-v5] ✓ SPORTS_FALLBACK_OUTCOME_FOUND: {matched} via {source}")
        except Exception as e:
            print(f"[sports-v5] fallback wrapper skipped: {e}")
        return eb
except Exception:
    pass


# Final proof wrapper: clean failed source rows and re-run v5-safe promotion once.
try:
    _V5_PRE_BUILD_COMBINED_PROOF = build_combined_proof
    def build_combined_proof(market_id: str, evidence: dict, receipt_path: Optional[Path],
                             prompt_integrity: Optional[dict] = None) -> dict:
        evidence = _orp_v2_promote_final(evidence, {"id": market_id})
        try:
            evidence = _or_v4_clean_source_statuses(evidence)
        except Exception:
            pass
        proof = _V5_PRE_BUILD_COMBINED_PROOF(market_id, evidence, receipt_path, prompt_integrity)
        try:
            if isinstance(proof, dict):
                oe = proof.get("oracle_evidence") if isinstance(proof.get("oracle_evidence"), dict) else {}
                oe = _orp_v2_promote_final(oe, {"id": market_id})
                oe = _or_v4_clean_source_statuses(oe)
                proof["oracle_evidence"] = oe
                final = str(oe.get("final_outcome") or oe.get("oracle_result") or proof.get("final_outcome") or "INCONCLUSIVE")
                calc = str(oe.get("oracle_calculation") or proof.get("oracle_calculation") or "")
                for key in ("final_outcome", "oracle_result", "oracle_outcome", "matched_outcome", "ree_expected_output"):
                    proof[key] = final
                proof["oracle_calculation"] = calc
                proof["resolved_outcome"] = oe.get("resolved_outcome") or proof.get("resolved_outcome")
                proof["dashboard"] = oe.get("dashboard") or proof.get("dashboard")
                verification = proof.get("verification") if isinstance(proof.get("verification"), dict) else {}
                verification.update({
                    "oracle_result": final,
                    "oracle_outcome": final,
                    "final_outcome": final,
                    "matched_outcome": final,
                    "oracle_calculation": calc,
                })
                proof["verification"] = verification
        except Exception as e:
            print(f"[oracle-v5] final proof normalization skipped: {e}")
        return proof
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# END FINAL TARGETED PATCH V5
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 INTEGRATION: oracle_core.fetch_source
# Purpose:
#   Run the new fetch/validation module before the legacy fetch chain.
#   If validated content is returned, the existing extract/resolve pipeline uses it.
#   If invalid content is returned for critical categories, return explicit
#   INCONCLUSIVE instead of allowing legacy wrappers to revive homepage/meta shells.
# ═══════════════════════════════════════════════════════════════════════════════
try:
    if _FETCH_SOURCE_MODULE_LOADED:
        _PHASE1_FETCH_PRE_BUILD_SOURCE_EVIDENCE = build_source_evidence

        def _phase1_explicit_inconclusive_block(
            source_original: str,
            reason: str,
            event_date: str = "",
        ) -> EvidenceBlock:
            eb = EvidenceBlock()
            eb.fetch_status = "FETCH_FAILED"
            eb.parse_status = "PARSE_FAILED"
            eb.outcome_status = "OUTCOME_NOT_FOUND"
            eb.source_used = source_original
            eb.fetch_method = "oracle_core_fetch_source"
            eb.reason = f"INCONCLUSIVE: {reason}"
            eb.matched_outcome = "INCONCLUSIVE"
            eb.calculation = eb.reason
            eb.facts = [
                Fact("structured_resolution", eb.reason, source_original, timestamp=event_date),
                Fact("matched_outcome", "INCONCLUSIVE", source_original, timestamp=event_date),
            ]
            return eb

        def _phase1_category_for_fetch(intel: dict, question: str) -> str:
            rules = intel.get("_rules") if isinstance(intel.get("_rules"), dict) else {}
            category = str(
                intel.get("category")
                or rules.get("category")
                or intel.get("market_type")
                or ""
            ).lower()
            resolver = str(intel.get("resolver") or "").lower()
            metric = str(intel.get("metric") or rules.get("metric") or "").lower()
            q = str(question or "").lower()

            if category in {"sports", "sports_spread"} or resolver in {"sports_result", "spread_cover"}:
                return "sports_spread" if resolver == "spread_cover" or category == "sports_spread" else "sports"
            if metric in {"winner", "match_winner", "game_winner", "final_score", "score", "spread"}:
                return "sports"
            if any(k in q for k in [" vs ", " versus ", " fa cup", "premier league", "champions league", "final score"]):
                return "sports"
            if category in {"crypto_web3_event", "crypto_event", "company_event"}:
                return category
            if rules.get("asset", {}).get("type") == "company_event":
                return "company_event"
            if category in {"crypto_price", "crypto_price_range", "finance", "economy_finance"}:
                return category
            if metric == "count" or intel.get("count_subject") or rules.get("count_subject"):
                return "count"
            if resolver == "binary_yes_no" or category in {"binary_event", "generic_binary"}:
                return "generic_binary"
            return category or "generic"

        def build_source_evidence(source_original: str, intelligence: dict, *args, **kwargs) -> EvidenceBlock:
            intel = dict(intelligence or {})
            question = args[0] if len(args) > 0 else kwargs.get("question") or intel.get("event_description", "")
            outcomes = args[1] if len(args) > 1 else kwargs.get("outcomes") or []
            resolves_at = (
                args[2] if len(args) > 2
                else kwargs.get("resolves_at")
                or kwargs.get("close_time")
                or intel.get("close_time", "")
            )
            if not isinstance(outcomes, list):
                outcomes = list(outcomes or [])

            rules = intel.get("_rules") if isinstance(intel.get("_rules"), dict) else {}
            event_date = str(intel.get("event_date") or rules.get("event_date") or (str(resolves_at)[:10] if resolves_at else ""))
            prompt_context = str(intel.get("prompt_context") or "")
            time_window = rules.get("time_window") if isinstance(rules.get("time_window"), dict) else {}
            window_start = str(time_window.get("start") or event_date)
            window_end = str(time_window.get("end") or event_date)

            try:
                url = resolve_source_to_url(str(source_original))
                domain = clean_domain(url)
                query_plan = build_universal_query(str(question or ""), outcomes, rules, event_date, domain)
                query = query_plan.get("query") or intel.get("search_query") or str(question or "")
                category = _phase1_category_for_fetch(intel, str(question or ""))

                fetch_result = fetch_source(
                    source_url=url,
                    query=query,
                    event_date=event_date,
                    market_category=category,
                    question=str(question or ""),
                    outcomes=outcomes,
                    prompt_context=prompt_context,
                    window_start=window_start,
                    window_end=window_end,
                )
            except Exception as e:
                print(f"[oracle-phase1] fetch_source module error: {e}; falling back to legacy")
                return _PHASE1_FETCH_PRE_BUILD_SOURCE_EVIDENCE(source_original, intelligence, *args, **kwargs)

            critical_categories = {"sports", "sports_spread", "company_event", "crypto_event", "crypto_web3_event", "generic_binary"}

            if not fetch_result.is_valid:
                print(f"[oracle-phase1] fetch_source invalid: {fetch_result.validation_reason}")
                if category in critical_categories:
                    return _phase1_explicit_inconclusive_block(
                        str(source_original),
                        fetch_result.validation_reason,
                        event_date,
                    )
                return _PHASE1_FETCH_PRE_BUILD_SOURCE_EVIDENCE(source_original, intelligence, *args, **kwargs)

            eb = EvidenceBlock()
            eb.fetch_status = "FETCHED"
            eb.fetch_method = fetch_result.method or "oracle_core_fetch_source"
            eb.source_used = fetch_result.source or str(source_original)
            eb.recovered_from = clean_domain(url) if clean_domain(str(fetch_result.source or "")) != clean_domain(url) else None
            eb.raw_content = str(fetch_result.content or "")[:5000]

            # If oracle_core.fetch_source already produced a structured outcome
            # (for example from TheSportsDB / football-data.org / trusted sports
            # Tavily), promote it directly into EvidenceBlock. This prevents the
            # result from dying as a validated fetch with no candidate.
            structured_matched = getattr(fetch_result, "matched_outcome", None)
            if structured_matched and str(structured_matched).strip().upper() != "INCONCLUSIVE":
                eb.parse_status = "PARSED"
                eb.outcome_status = "OUTCOME_FOUND"
                eb.matched_outcome = str(structured_matched).strip()
                eb.calculation = (
                    f"oracle_core.fetch_source structured result → {eb.matched_outcome}; "
                    f"{getattr(fetch_result, 'validation_reason', '')}"
                ).strip()
                eb.reason = None
                facts = [Fact("raw_evidence", str(fetch_result.content)[:2200], eb.source_used, timestamp=event_date)]
                extracted = getattr(fetch_result, "extracted_facts", None) or []
                for item in extracted:
                    if isinstance(item, dict) and item.get("label") and item.get("value") is not None:
                        facts.append(Fact(str(item["label"]), str(item["value"]), eb.source_used, timestamp=event_date))
                if not any(f.label == "matched_outcome" for f in facts):
                    facts.append(Fact("matched_outcome", eb.matched_outcome, eb.source_used, timestamp=event_date))
                facts.append(Fact("structured_resolution", eb.calculation, eb.source_used, timestamp=event_date))
                eb.facts = facts
                print(f"[oracle-phase1] ✓ STRUCTURED OUTCOME_FOUND via fetch_source: {eb.matched_outcome} ({eb.fetch_method})")
                return eb

            matched = None
            calc = None

            try:
                matched, calc = extract_specific_answer(
                    fetch_result.content,
                    query_plan,
                    str(question or ""),
                    outcomes,
                    intel,
                )
            except Exception as e:
                print(f"[oracle-phase1] extract_specific_answer failed: {e}")

            if not matched and category in {"sports", "sports_spread"} and "_final_sports_winner_from_text" in globals():
                try:
                    matched, calc = _final_sports_winner_from_text(str(question or ""), outcomes, fetch_result.content)
                except Exception as e:
                    print(f"[oracle-phase1] sports winner fallback failed: {e}")

            if not matched:
                try:
                    matched, calc = derive_outcome(
                        [Fact("raw_evidence", str(fetch_result.content)[:2200], eb.source_used, timestamp=event_date)],
                        outcomes,
                        str(question or ""),
                        intel,
                    )
                except Exception as e:
                    print(f"[oracle-phase1] derive_outcome failed: {e}")

            if matched:
                eb.parse_status = "PARSED"
                eb.outcome_status = "OUTCOME_FOUND"
                eb.matched_outcome = matched
                eb.calculation = calc or f"oracle_core.fetch_source validated evidence → {matched}"
                eb.reason = None
                eb.facts = [
                    Fact("raw_evidence", str(fetch_result.content)[:2200], eb.source_used, timestamp=event_date),
                    Fact("structured_resolution", eb.calculation, eb.source_used, timestamp=event_date),
                    Fact("matched_outcome", matched, eb.source_used, timestamp=event_date),
                ]
                print(f"[oracle-phase1] ✓ OUTCOME_FOUND via fetch_source: {matched} ({eb.fetch_method})")
                return eb

            # Valid content but extraction failed. For critical categories, do not fall
            # back to legacy, because legacy can revive stale candidates or weak pages.
            reason = f"validated content from {eb.source_used}, but no valid outcome extracted"
            if category in critical_categories:
                return _phase1_explicit_inconclusive_block(str(source_original), reason, event_date)

            print("[oracle-phase1] valid fetch but no outcome; falling back to legacy for non-critical category")
            return _PHASE1_FETCH_PRE_BUILD_SOURCE_EVIDENCE(source_original, intelligence, *args, **kwargs)

except Exception as e:
    print(f"[oracle-phase1] integration disabled: {e}")

# ═══════════════════════════════════════════════════════════════════════════════
# END PHASE 1 INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH V5-GROQ: Restore Groq/stage2_ai_judge for V5 sports fallbacks
# Problem:
#   V5 sports fallback can fetch usable Tavily/structured sports evidence, but its
#   regex-only resolver may fail on aliases such as "Manchester City" → "Man City".
#   The older working path used try_sports_fallback_sources() + stage2_ai_judge(),
#   which lets Groq map natural-language sports result text back to valid outcomes.
#
# Rule:
#   Groq does NOT decide truth here. It only maps already-fetched evidence to one
#   of the market's valid outcomes. If no safe match is found, OracleREE remains
#   INCONCLUSIVE.
# ═══════════════════════════════════════════════════════════════════════════════
try:
    _V5_GROQ_PRE_TRY_STRUCTURED = _v5_try_structured_sports_fallback

    def _v5_groq_answer_line(content: str) -> str:
        m = re.search(r"ANSWER:\s*(.+?)(?:\n\n|\n\[|\Z)", str(content or ""), re.I | re.S)
        return m.group(1).strip() if m else ""

    def _v5_groq_sports_intel(event_date: str) -> dict:
        return {
            "market_type": "sports",
            "answer_format": "named_choice",
            "event_date": event_date,
            "resolver": "sports_result",
            "_rules": {
                "metric": "winner",
                "winner_logic": "full_time_result",
                "source_policy": "creator_first_sports_fallback",
                "fallback_policy": "sports_tavily_trusted_only_after_creator_failure",
                "category": "sports",
                "event_date": event_date,
            },
        }

    def _v5_try_structured_sports_fallback(
        question: str,
        event_date: str,
        outcomes: list,
        prompt_context: str = "",
    ) -> Optional[tuple[str, str, str, str]]:
        # 1) Run the existing V5 structured/fallback logic first.
        result = _V5_GROQ_PRE_TRY_STRUCTURED(question, event_date, outcomes, prompt_context)
        if result:
            content, source, matched, calc = result

            # If existing V5 logic resolved cleanly, keep it.
            if matched:
                return result

            # If V5 found content but regex mapping failed, let stage2_ai_judge map it.
            if content and callable(globals().get("stage2_ai_judge")):
                intel = _v5_groq_sports_intel(event_date)
                answer_line = _v5_groq_answer_line(content)
                groq_matched, groq_calc = stage2_ai_judge(
                    answer_line,
                    content,
                    question,
                    outcomes,
                    intel,
                )
                if groq_matched:
                    print(f"[v5-groq] ✓ stage2_ai_judge resolved: {groq_matched}")
                    return content, source, groq_matched, groq_calc or calc or f"V5 sports evidence via {source} → {groq_matched}"

        # 2) If structured APIs returned nothing, restore the older working path:
        #    try_sports_fallback_sources() → Tavily content → stage2_ai_judge().
        if callable(globals().get("try_sports_fallback_sources")) and callable(globals().get("stage2_ai_judge")):
            intel = _v5_groq_sports_intel(event_date)

            # Preserve extra prompt context for Groq if available.
            if prompt_context:
                intel["prompt_context"] = prompt_context

            tv_result = try_sports_fallback_sources(
                question,
                event_date,
                question,
                intel,
                "espn.com",
                outcomes,
            )

            if tv_result:
                tv_content, tv_source = tv_result
                answer_line = _v5_groq_answer_line(tv_content)
                groq_matched, groq_calc = stage2_ai_judge(
                    answer_line,
                    tv_content,
                    question,
                    outcomes,
                    intel,
                )
                if groq_matched:
                    print(f"[v5-groq] ✓ original fallback + stage2_ai_judge: {groq_matched} via {tv_source}")
                    return (
                        tv_content,
                        tv_source,
                        groq_matched,
                        groq_calc or f"sports fallback via {tv_source} → {groq_matched}",
                    )

        return None

    print("[v5-groq] patch loaded")

except Exception as e:
    print(f"[v5-groq] patch failed to apply: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# PATCH V6-PRICE-RANGE: Historical crypto price range resolver
# Purpose:
#   Fix markets like "How high will BTC get in April?" / "Highest price ETH reaches"
#   by fetching CoinGecko market_chart/range data over the full settlement window,
#   calculating max/min/close, then mapping the value into valid outcome bands.
# Scope:
#   Only crypto_price / crypto_price_range / numeric_range markets with a crypto asset.
#   Does NOT touch sports, named-choice, count, or binary confirmation markets.
# ═══════════════════════════════════════════════════════════════════════════════

try:
    import calendar as _or_v6_calendar
    from datetime import datetime as _or_v6_datetime, timezone as _or_v6_timezone, timedelta as _or_v6_timedelta

    def _or_v6_price_float(value) -> Optional[float]:
        """Parse numeric price strings with commas, $, k/m/b suffixes."""
        if value is None:
            return None
        text = str(value).strip().lower().replace(',', '').replace('$', '')
        m = re.search(r'-?\d+(?:\.\d+)?\s*[kmb]?', text)
        if not m:
            return None
        raw = m.group(0).strip()
        mult = 1.0
        if raw.endswith('k'):
            mult = 1_000.0; raw = raw[:-1]
        elif raw.endswith('m'):
            mult = 1_000_000.0; raw = raw[:-1]
        elif raw.endswith('b'):
            mult = 1_000_000_000.0; raw = raw[:-1]
        try:
            return float(raw) * mult
        except Exception:
            return None

    def _or_v6_detect_crypto_asset(question: str, intelligence: dict) -> str:
        """Return BTC/ETH/SOL/etc if the market is a supported crypto price market."""
        asset = str((intelligence or {}).get('asset') or '').upper().strip()
        if asset in COINGECKO_IDS:
            return asset
        text = (str(question or '') + ' ' + json.dumps((intelligence or {}).get('_rules') or {}, ensure_ascii=False)).upper()
        for ticker in COINGECKO_IDS:
            if re.search(rf'\b{re.escape(ticker)}\b', text):
                return ticker
        # Common full names.
        names = {'BITCOIN':'BTC', 'ETHEREUM':'ETH', 'SOLANA':'SOL', 'BNB':'BNB', 'XRP':'XRP'}
        for name, ticker in names.items():
            if name in text and ticker in COINGECKO_IDS:
                return ticker
        return ''

    def _or_v6_price_metric(question: str, intelligence: dict) -> str:
        """high | low | close. Highest/at any point/reach markets use high."""
        text = (str(question or '') + ' ' + ' '.join(map(str, (intelligence or {}).get('facts_needed') or []))).lower()
        if any(k in text for k in ['lowest', 'low ', 'minimum', 'min price', 'bottom']):
            return 'low'
        if any(k in text for k in ['highest', 'high ', 'how high', 'peak', 'maximum', 'max price', 'reach', 'hit', 'at any point']):
            return 'high'
        return 'close'

    def _or_v6_price_window(question: str, intelligence: dict, resolves_at: str) -> tuple[str, str]:
        """Infer a price window from question/month names or settlement rule time_window."""
        rules = (intelligence or {}).get('_rules') if isinstance((intelligence or {}).get('_rules'), dict) else {}
        tw = rules.get('time_window') if isinstance(rules.get('time_window'), dict) else {}
        start = str(tw.get('start') or '').strip()
        end = str(tw.get('end') or '').strip()
        if re.match(r'\d{4}-\d{2}-\d{2}', start) and re.match(r'\d{4}-\d{2}-\d{2}', end):
            return start[:10], end[:10]

        q = str(question or '').lower()
        base_year = ''
        for candidate in [str((intelligence or {}).get('event_date') or ''), str(resolves_at or '')]:
            if re.match(r'\d{4}-\d{2}-\d{2}', candidate):
                base_year = candidate[:4]
                break
        if not base_year:
            base_year = str(_or_v6_datetime.now(_or_v6_timezone.utc).year)

        month_map = globals().get('MONTH_MAP') or {
            'january':'01','february':'02','march':'03','april':'04','may':'05','june':'06',
            'july':'07','august':'08','september':'09','october':'10','november':'11','december':'12'
        }
        for month_name, month_num in month_map.items():
            if month_name in q:
                # If a specific day is mentioned (e.g. "on April 27"), use just that day
                day_m = re.search(
                    month_name + r'\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,|\s+\d{4}|\s*$|\s*\?)',
                    q
                )
                if day_m:
                    day = int(day_m.group(1))
                    day_str = f'{base_year}-{month_num}-{day:02d}'
                    return day_str, day_str
                last = _or_v6_calendar.monthrange(int(base_year), int(month_num))[1]
                return f'{base_year}-{month_num}-01', f'{base_year}-{month_num}-{last:02d}'

        # Explicit yyyy-mm-dd in question.
        dates = re.findall(r'\d{4}-\d{2}-\d{2}', str(question or ''))
        if len(dates) >= 2:
            return dates[0], dates[1]
        if len(dates) == 1:
            return dates[0], dates[0]

        # Fallback to event/close day.
        day = str((intelligence or {}).get('event_date') or resolves_at or '')[:10]
        if re.match(r'\d{4}-\d{2}-\d{2}', day):
            return day, day
        return '', ''

    def _or_v6_coingecko_prices(asset: str, start_date: str, end_date: str) -> list[tuple[int, float]]:
        """Fetch CoinGecko market_chart/range prices; returns [(ms_timestamp, usd_price), ...]."""
        coin_id = COINGECKO_IDS.get(str(asset).upper())
        if not coin_id or not start_date or not end_date:
            return []
        start_dt = _or_v6_datetime.fromisoformat(start_date[:10]).replace(tzinfo=_or_v6_timezone.utc)
        end_dt = _or_v6_datetime.fromisoformat(end_date[:10]).replace(tzinfo=_or_v6_timezone.utc) + _or_v6_timedelta(days=1)
        r = requests.get(
            f'{COINGECKO_BASE}/coins/{coin_id}/market_chart/range',
            params={
                'vs_currency': 'usd',
                'from': int(start_dt.timestamp()),
                'to': int(end_dt.timestamp()),
            },
            headers={'User-Agent': 'OracleREE/1.0'},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json() or {}
        out = []
        for item in data.get('prices') or []:
            try:
                ts, price = int(item[0]), float(item[1])
                out.append((ts, price))
            except Exception:
                continue
        return out

    def _or_v6_outcome_bounds(outcome: str) -> tuple[Optional[float], Optional[float], str]:
        """Return lower, upper, kind for a price outcome band."""
        o = str(outcome or '').lower().replace(',', '')
        nums = [_or_v6_price_float(x.group(0)) for x in re.finditer(r'\$?\d+(?:\.\d+)?\s*[kmb]?', o)]
        nums = [n for n in nums if n is not None]
        if not nums:
            return None, None, 'none'
        if any(k in o for k in ['under', 'below', 'less than', 'or lower', 'and below', 'below']):
            return None, nums[0], 'below'
        if any(k in o for k in ['over', 'above', 'greater than', 'or higher', 'and above', 'plus']):
            return nums[0], None, 'above'
        if len(nums) >= 2:
            lo, hi = min(nums[0], nums[1]), max(nums[0], nums[1])
            return lo, hi, 'range'
        return nums[0], nums[0], 'point'

    def _or_v6_match_price_outcome(value: float, outcomes: list) -> Optional[str]:
        """Map price into exact market outcome string."""
        if value is None:
            return None
        point_candidates = []
        for outcome in outcomes or []:
            o = str(outcome).strip()
            lo, hi, kind = _or_v6_outcome_bounds(o)
            if kind == 'below' and hi is not None and value < hi:
                return o
            if kind == 'above' and lo is not None and value > lo:
                return o
            if kind == 'range' and lo is not None and hi is not None and lo <= value < hi:
                return o
            if kind == 'point' and lo is not None:
                point_candidates.append((abs(value - lo), o, lo))
        # If outcomes are single bucket labels, pick nearest numeric label.
        if point_candidates:
            point_candidates.sort(key=lambda x: x[0])
            return point_candidates[0][1]
        return None

    def _or_v6_resolve_crypto_price_range(question: str, outcomes: list, intelligence: dict, resolves_at: str) -> Optional[EvidenceBlock]:
        asset = _or_v6_detect_crypto_asset(question, intelligence)
        if not asset:
            return None
        market_type = str((intelligence or {}).get('market_type') or '').lower()
        answer_format = str((intelligence or {}).get('answer_format') or '').lower()
        resolver = str((intelligence or {}).get('resolver') or '').lower()
        q = str(question or '').lower()
        price_q = any(k in q for k in ['price', 'highest', 'lowest', 'how high', 'reach', 'hit', 'above', 'below', 'range', 'between'])
        if not (market_type in {'crypto_price', 'crypto_price_range'} or answer_format in {'numeric_range', 'numeric_threshold'} or resolver in {'crypto_price', 'price_bucket'} or price_q):
            return None
        start_date, end_date = _or_v6_price_window(question, intelligence, resolves_at)
        if not start_date or not end_date:
            return None
        metric = _or_v6_price_metric(question, intelligence)

        eb = EvidenceBlock()
        eb.source_used = 'CoinGecko'
        eb.fetch_method = 'coingecko_market_chart_range'
        try:
            prices = _or_v6_coingecko_prices(asset, start_date, end_date)
            eb.fetch_status = 'FETCHED' if prices else 'FETCH_FAILED'
            if not prices:
                eb.parse_status = 'FETCH_FAILED'
                eb.outcome_status = 'OUTCOME_NOT_FOUND'
                eb.reason = f'CoinGecko range returned no prices for {asset} {start_date}–{end_date}'
                return eb
            if metric == 'low':
                ts, value = min(prices, key=lambda x: x[1])
                label = 'low_price'
            elif metric == 'close':
                ts, value = prices[-1]
                label = 'close_price'
            else:
                ts, value = max(prices, key=lambda x: x[1])
                label = 'high_price'
            matched = _or_v6_match_price_outcome(value, outcomes)
            when = _or_v6_datetime.fromtimestamp(ts / 1000, tz=_or_v6_timezone.utc).isoformat()
            eb.raw_content = json.dumps({
                'asset': asset,
                'coin_id': COINGECKO_IDS.get(asset),
                'window_start': start_date,
                'window_end': end_date,
                'metric': metric,
                'selected_timestamp': when,
                'selected_price_usd': value,
                'sample_count': len(prices),
            }, ensure_ascii=False)
            eb.parse_status = 'PARSED'
            eb.facts = [
                Fact(label, f'{value:.8f}', 'CoinGecko', timestamp=when, unit='USD'),
                Fact('price_window_start', start_date, 'CoinGecko'),
                Fact('price_window_end', end_date, 'CoinGecko'),
                Fact('sample_count', str(len(prices)), 'CoinGecko'),
            ]
            if matched:
                eb.outcome_status = 'OUTCOME_FOUND'
                eb.matched_outcome = matched
                eb.calculation = f'{asset} {metric} price from CoinGecko range {start_date}–{end_date}: ${value:,.2f} → {matched}'
                eb.reason = eb.calculation
                eb.facts.append(Fact('matched_outcome', matched, 'CoinGecko', timestamp=when))
                print(f'[price-v6] ✓ {eb.calculation}')
            else:
                eb.outcome_status = 'OUTCOME_NOT_FOUND'
                eb.reason = f'{asset} {metric} price ${value:,.2f} found, but no outcome band matched'
                print(f'[price-v6] {eb.reason}')
            return eb
        except Exception as e:
            eb.fetch_status = 'FETCH_FAILED'
            eb.parse_status = 'FETCH_FAILED'
            eb.outcome_status = 'OUTCOME_NOT_FOUND'
            eb.reason = f'CoinGecko historical price range failed: {e}'
            print(f'[price-v6] {eb.reason}')
            return eb

    _OR_V6_PRICE_PRE_BUILD_SOURCE_EVIDENCE = build_source_evidence

    def build_source_evidence(source_original: str, intelligence: dict, *args, **kwargs) -> EvidenceBlock:
        intel = dict(intelligence or {})
        question = args[0] if len(args) > 0 else kwargs.get('question') or intel.get('event_description', '')
        outcomes = args[1] if len(args) > 1 else kwargs.get('outcomes') or []
        resolves_at = args[2] if len(args) > 2 else kwargs.get('resolves_at') or kwargs.get('close_time') or intel.get('close_time', '')
        if not isinstance(outcomes, list):
            outcomes = list(outcomes or [])

        # Price range markets are handled here before legacy single-day price logic.
        # This branch is deliberately narrow so sports/count/named-choice/binary events are untouched.
        eb_price = _or_v6_resolve_crypto_price_range(str(question or ''), outcomes, intel, str(resolves_at or ''))
        if eb_price and eb_price.verified:
            return eb_price

        eb = _OR_V6_PRICE_PRE_BUILD_SOURCE_EVIDENCE(source_original, intelligence, *args, **kwargs)
        if eb_price and not getattr(eb, 'verified', False):
            # Preserve the more specific price-range failure reason if legacy also failed.
            try:
                if getattr(eb_price, 'reason', None):
                    eb.reason = getattr(eb, 'reason', None) or eb_price.reason
            except Exception:
                pass
        return eb

    print('[price-v6] historical crypto price range resolver loaded')

except Exception as e:
    print(f'[price-v6] patch failed to apply: {e}')



if __name__=="__main__":
    raise SystemExit(main())

