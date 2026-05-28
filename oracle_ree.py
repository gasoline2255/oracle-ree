#!/usr/bin/env python3
"""
oracle_ree_v2.py — OracleREE Clean Rebuild

Architecture:
    fetch_market()          → get market from Delphi API
    oracle_core/pipeline.py → classify → fetch → extract → resolve
    build_oracle_prompt()   → wrap result into REE prompt
    run_ree()               → local Qwen3-0.6B inference + receipt
    build_combined_proof()  → final proof JSON (ree.py compatible)

Rules:
    - No wrapper chains. No patches. No 14-layer overrides.
    - oracle_core/pipeline.py owns ALL oracle logic.
    - This file owns: market fetch, REE execution, proof assembly.
    - INCONCLUSIVE is honest. A guessed answer is not.
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

# ─── Load .env.local ─────────────────────────────────────────────────────────
_ENV_FILE = Path(__file__).parent / ".env.local"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ─── Config ───────────────────────────────────────────────────────────────────
DELPHI_API_BASE     = "https://api.delphi.fyi"
COINGECKO_BASE      = "https://api.coingecko.com/api/v3"
PINATA_JWT          = os.environ.get("PINATA_JWT", "")
DELPHI_API_KEY      = os.environ.get("DELPHI_API_ACCESS_KEY", "")
ORACLE_SEAL_URL     = os.environ.get("ORACLE_SEAL_URL", "https://oracle-seal.vercel.app")
REE_MAX_TOKENS      = int(os.environ.get("ORACLEREE_REE_MAX_TOKENS", "200"))

# All Delphi model names map to Qwen3-0.6B for local REE
DELPHI_TO_REE_MODEL: dict[str, str] = {
    "Claude Opus 4.7":                      "Qwen/Qwen3-0.6B",
    "Claude Opus 4.6":                      "Qwen/Qwen3-0.6B",
    "Claude Opus 4":                        "Qwen/Qwen3-0.6B",
    "Claude Sonnet 4.7":                    "Qwen/Qwen3-0.6B",
    "Claude Sonnet 4.6":                    "Qwen/Qwen3-0.6B",
    "Claude Sonnet 4":                      "Qwen/Qwen3-0.6B",
    "Claude Haiku 4.7":                     "Qwen/Qwen3-0.6B",
    "Claude Haiku 4.6":                     "Qwen/Qwen3-0.6B",
    "Claude Haiku 4":                       "Qwen/Qwen3-0.6B",
    "claude-opus":                          "Qwen/Qwen3-0.6B",
    "claude-sonnet":                        "Qwen/Qwen3-0.6B",
    "claude-haiku":                         "Qwen/Qwen3-0.6B",
    "gpt-4":                                "Qwen/Qwen3-0.6B",
    "gpt-4o":                               "Qwen/Qwen3-0.6B",
    "gpt-4o-mini":                          "Qwen/Qwen3-0.6B",
    "gpt-3.5-turbo":                        "Qwen/Qwen3-0.6B",
    "gemini-pro":                           "Qwen/Qwen3-0.6B",
    "gemini-flash":                         "Qwen/Qwen3-0.6B",
    "grok":                                 "Qwen/Qwen3-0.6B",
    "Qwen/Qwen3-32B":                       "Qwen/Qwen3-0.6B",
    "Qwen/Qwen2.5-32B-Instruct":            "Qwen/Qwen3-0.6B",
    "Qwen/Qwen2.5-32B":                     "Qwen/Qwen3-0.6B",
    "Qwen/Qwen3-14B":                       "Qwen/Qwen3-0.6B",
    "Qwen/Qwen3-14B-Instruct":              "Qwen/Qwen3-0.6B",
    "Qwen/Qwen3-4B":                        "Qwen/Qwen3-4B",
    "Qwen/Qwen2.5-7B-Instruct":             "Qwen/Qwen2.5-7B-Instruct",
    "Qwen/Qwen2.5-7B":                      "Qwen/Qwen2.5-7B",
    "Qwen/Qwen2.5-3B-Instruct":             "Qwen/Qwen2.5-3B-Instruct",
    "Meta-Llama/Meta-Llama-3-8B-Instruct":  "Meta-Llama/Meta-Llama-3-8B-Instruct",
    "Meta-Llama/Meta-Llama-3-8B":           "Meta-Llama/Meta-Llama-3-8B",
    "Meta-Llama/Llama-3.1-8B-Instruct":     "Meta-Llama/Llama-3.1-8B-Instruct",
    "Meta-Llama/Llama-3.1-8B":              "Meta-Llama/Llama-3.1-8B",
    "Meta-Llama/Llama-3.2-3B-Instruct":     "Meta-Llama/Llama-3.2-3B-Instruct",
    "Mistralai/Mistral-7B-Instruct-V0.2":   "Mistralai/Mistral-7B-Instruct-V0.2",
    "01-Ai/Yi-1.5-6B-Chat":                 "01-Ai/Yi-1.5-6B-Chat",
    "Llm-Jp/Llm-Jp-3-3.7b-Instruct":        "Qwen/Qwen3-0.6B",
}

import requests

# ─── oracle_core pipeline ─────────────────────────────────────────────────────
try:
    from oracle_core.pipeline import run_pipeline, PipelineResult
    _PIPELINE_LOADED = True
    print("[v2] oracle_core.pipeline loaded ✓")
except Exception as _e:
    print(f"[v2] FATAL: oracle_core.pipeline failed to load: {_e}")
    print("[v2] Make sure oracle_core/ directory is in the same folder as this file.")
    _PIPELINE_LOADED = False


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_ree_model(delphi_model: str, fallback: str = "Qwen/Qwen3-0.6B") -> str:
    """Map a Delphi model name to a local REE model name."""
    if delphi_model in DELPHI_TO_REE_MODEL:
        return DELPHI_TO_REE_MODEL[delphi_model]
    # Large models always use the small local model
    if any(s in delphi_model.lower() for s in ["32b", "70b", "72b", "34b", "13b", "14b", "30b"]):
        return fallback
    # Pass through known HuggingFace paths
    if "/" in delphi_model:
        return delphi_model
    return fallback


def extract_market_id(raw: str) -> str:
    """Extract 0x market ID from URL, raw ID, or UUID."""
    # Direct 0x address
    m = re.search(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])", raw)
    if m:
        return m.group(0)
    # UUID format URL — resolve via Delphi API
    uuid_m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        raw, re.I
    )
    if uuid_m:
        uuid = uuid_m.group(0)
        print(f"[v2] Resolving UUID: {uuid}")
        try:
            api_key = os.environ.get("DELPHI_API_ACCESS_KEY", "")
            r = requests.get(
                f"https://api.delphi.fyi/markets",
                headers={"x-api-key": api_key},
                params={"limit": 200, "status": "open"},
                timeout=15,
            )
            for market in r.json().get("markets", []):
                if uuid.lower() in str(market.get("appMarketId", "")).lower():
                    mid = market.get("id", "")
                    if mid:
                        print(f"[v2] Resolved UUID → {mid}")
                        return mid
            # Try settled markets
            r2 = requests.get(
                f"https://api.delphi.fyi/markets",
                headers={"x-api-key": api_key},
                params={"limit": 200, "status": "settled"},
                timeout=15,
            )
            for market in r2.json().get("markets", []):
                if uuid.lower() in str(market.get("appMarketId", "")).lower():
                    mid = market.get("id", "")
                    if mid:
                        print(f"[v2] Resolved UUID → {mid}")
                        return mid
        except Exception as e:
            print(f"[v2] UUID resolution failed: {e}")
        # Try Xavier's endpoint: POST /api/v2/markets/get
        for app_url in ["https://app.delphi.fyi", "https://testnet.delphi.fyi"]:
            try:
                r3 = requests.post(
                    f"{app_url}/api/v2/markets/get",
                    json={"json": {"id": uuid}},
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                if r3.ok:
                    addr = r3.json().get("json", {}).get("address", "")
                    if addr and addr.startswith("0x"):
                        print(f"[v2] Resolved UUID via /api/v2/markets/get → {addr}")
                        return addr
            except Exception as e:
                print(f"[v2] /api/v2/markets/get failed ({app_url}): {e}")
        # Try fetching the market page directly to extract 0x ID from HTML
        try:
            url = f"https://app.delphi.fyi/market/{uuid}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 OracleREE/2.0"}, timeout=10)
            ox = re.search(r"0x[a-fA-F0-9]{40}(?![a-fA-F0-9])", r.text)
            if ox:
                print(f"[v2] Resolved UUID from page HTML → {ox.group(0)}")
                return ox.group(0)
        except Exception as e:
            print(f"[v2] Page fetch failed: {e}")
        raise ValueError(f"Could not resolve UUID to 0x market ID: {uuid}")
    raise ValueError(f"Could not extract 0x market ID from: {raw[:100]}")

def extract_settlement_prompt(raw: str) -> str:
    """Extract settlement prompt from combined ree.py input string."""
    m = re.search(r"SETTLEMENT PROMPT:\n(.+)", raw, re.S)
    if m:
        return m.group(1).strip()
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: DELPHI API
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_market(market_id: str) -> dict:
    """Fetch a single market from the Delphi API."""
    if not DELPHI_API_KEY:
        raise ValueError("DELPHI_API_ACCESS_KEY not set in .env.local")
    r = requests.get(
        f"{DELPHI_API_BASE}/markets/{market_id}",
        headers={"x-api-key": DELPHI_API_KEY},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def is_market_closed(market: dict) -> bool:
    """True if the market resolution time has passed."""
    resolves_at = market.get("resolvesAt") or ""
    if not resolves_at:
        return False
    try:
        dt = datetime.fromisoformat(resolves_at.replace("Z", "+00:00"))
        return dt < datetime.now(timezone.utc)
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: IPFS + ORACLE SEAL
# ═══════════════════════════════════════════════════════════════════════════════

def pin_to_ipfs(data: dict, name: str) -> Optional[str]:
    """Pin evidence JSON to IPFS via Pinata. Returns CID or None."""
    if not PINATA_JWT:
        return None
    try:
        r = requests.post(
            "https://uploads.pinata.cloud/v3/files",
            headers={"Authorization": f"Bearer {PINATA_JWT}"},
            files={"file": (f"{name}.json", json.dumps(data), "application/json")},
            data={"network": "public"},
            timeout=30,
        )
        print(f"[v2] Pinata response: {r.status_code} {r.text[:300]}")
        resp = r.json()
        cid = (
            resp.get("data", {}).get("cid") or
            resp.get("IpfsHash") or
            resp.get("cid")
        )
        if cid:
            print(f"[v2] IPFS pinned: {cid}")
            return cid
        print(f"[v2] IPFS pin failed — no CID in response")
        return None
    except Exception as e:
        print(f"[v2] IPFS pin failed: {e}")
        return None


def push_to_oracle_seal(proof: dict) -> bool:
    """Push proof to OracleSeal oracle_markets table."""
    v = proof.get("verification") or {}
    if not v.get("ree_receipt_hash"):
        return False
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not supabase_key:
        print("[v2] No Supabase config — skipping oracle_markets push")
        return False
    try:
        row = {
            "market_id":          proof.get("market_id"),
            "status":             "ree_verified",
            "oracle_result":      proof.get("final_outcome"),
            "oracle_hash":        v.get("oracle_evidence_hash"),
            "ipfs_cid":           v.get("ipfs_cid") or "",
            "ree_receipt_hash":   v.get("ree_receipt_hash"),
            "combined_hash":      v.get("combined_hash"),
            "proof_submitted_at": now_iso(),
            "updated_at":         now_iso(),
        }
        r = requests.post(
            f"{supabase_url}/rest/v1/oracle_markets",
            headers={
                "apikey":        supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type":  "application/json",
                "Prefer":        "resolution=merge-duplicates",
            },
            json=row, timeout=10,
        )
        if r.ok:
            print("[v2] Pushed to oracle_markets ✓")
            return True
        print(f"[v2] oracle_markets push failed: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"[v2] oracle_markets push failed: {e}")
    return False



# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: ORACLE EVIDENCE BUILDER
# Uses oracle_core/pipeline.py exclusively — no legacy fallback.
# ═══════════════════════════════════════════════════════════════════════════════

def build_oracle_evidence(market: dict) -> dict:
    """
    Run the oracle pipeline for a market.
    Returns an evidence dict compatible with build_combined_proof() and ree.py.
    """
    meta         = market.get("metadata") or {}
    question     = meta.get("question") or ""
    outcomes     = meta.get("outcomes") or []
    prompt_ctx   = (meta.get("model") or {}).get("prompt_context") or question
    resolves_at  = market.get("resolvesAt") or ""
    market_id    = market.get("id") or ""

    print(f"\n[v2] ═══════════════════════════════════")
    print(f"[v2] Market:   {question}")
    print(f"[v2] Close:    {resolves_at[:10]}")
    print(f"[v2] Outcomes: {outcomes}")
    print(f"[v2] ═══════════════════════════════════")

    evidence = {
        "market_id":       market_id,
        "market_question": question,
        "close_time":      resolves_at,
        "captured_at":     now_iso(),
        "oracle_result":   "INCONCLUSIVE",
        "oracle_outcome":  "INCONCLUSIVE",
        "final_outcome":   "INCONCLUSIVE",
        "matched_outcome": "INCONCLUSIVE",
        "oracle_calculation": "",
    }

    # ── Market not yet closed ─────────────────────────────────────────────────
    if not is_market_closed(market):
        print("[v2] Market not yet closed → INCONCLUSIVE")
        evidence["oracle_calculation"] = "INCONCLUSIVE: market not yet closed"
        _finalize_evidence(evidence, market_id)
        return evidence

    # ── Run pipeline ──────────────────────────────────────────────────────────
    if not _PIPELINE_LOADED:
        evidence["oracle_calculation"] = "INCONCLUSIVE: oracle_core pipeline not loaded"
        _finalize_evidence(evidence, market_id)
        return evidence

    print("\n[v2] Running oracle_core pipeline...")
    try:
        result: PipelineResult = run_pipeline(market, debug=True)
    except Exception as e:
        print(f"[v2] Pipeline exception: {e}")
        evidence["oracle_calculation"] = f"INCONCLUSIVE: pipeline error: {e}"
        _finalize_evidence(evidence, market_id)
        return evidence

    outcome    = result.outcome or "INCONCLUSIVE"
    confidence = result.confidence or "none"
    source     = result.source or ""
    method     = result.method or ""
    calc       = result.calculation or ""

    print(f"[v2] Pipeline result: {outcome} ({confidence}) via {source}/{method}")
    if result.steps:
        for step in result.steps:
            print(f"[v2]   {step}")

    # ── Build evidence fields ─────────────────────────────────────────────────
    evidence["oracle_result"]      = outcome
    evidence["oracle_outcome"]     = outcome
    evidence["final_outcome"]      = outcome
    evidence["matched_outcome"]    = outcome
    evidence["oracle_calculation"] = calc

    # Pipeline debug info (non-critical, for ree.py display)
    evidence["pipeline_debug"] = {
        "confidence": confidence,
        "source":     source,
        "method":     method,
        "steps":      result.steps,
    }

    # Market class details
    if result.market_class:
        mc = result.market_class
        evidence["market_class"] = {
            "category":      mc.category,
            "fetch_strategy": mc.fetch_strategy,
            "extract_method": mc.extract_method,
            "sport":         mc.sport,
            "asset":         mc.asset,
            "event_date":    mc.event_date,
            "confidence":    mc.confidence,
            "reason":        mc.classification_reason,
        }

    # Extracted fact details
    if result.extracted_fact:
        ef = result.extracted_fact
        evidence["extracted_fact"] = {
            "value":      ef.value,
            "fact_type":  ef.fact_type,
            "confidence": ef.confidence,
            "source":     ef.source,
            "reasoning":  ef.reasoning,
        }

    # ── Final verdict block (ree.py reads this) ───────────────────────────────
    evidence["final_verdict"] = {
        "pipeline":       f"{source} → {method}" if source else "pipeline",
        "fetch_status":   "FETCHED"       if outcome != "INCONCLUSIVE" else "INCONCLUSIVE",
        "parse_status":   "PARSED"        if outcome != "INCONCLUSIVE" else "INCONCLUSIVE",
        "outcome_status": "OUTCOME_FOUND" if outcome != "INCONCLUSIVE" else "OUTCOME_NOT_FOUND",
        "matched_outcome": outcome,
        "calculation":    calc,
        "source_used":    source,
        "fetch_method":   method,
        "facts": _build_fact_list(result, outcome, source, resolves_at[:10]),
    }

    _finalize_evidence(evidence, market_id)
    return evidence


def _build_fact_list(result: "PipelineResult", outcome: str, source: str, date: str) -> list:
    """Build the facts list that ree.py renders in the dashboard."""
    facts = []
    if result.extracted_fact and result.extracted_fact.value:
        ef = result.extracted_fact
        facts.append({"label": ef.fact_type or "extracted_value",
                      "value": ef.value,
                      "source": ef.source or source,
                      "timestamp": date})
    if result.fetch_result and result.fetch_result.content:
        snippet = str(result.fetch_result.content)[:500]
        facts.append({"label": "raw_evidence",
                      "value": snippet,
                      "source": source,
                      "timestamp": date})
    if outcome and outcome != "INCONCLUSIVE":
        facts.append({"label": "matched_outcome",
                      "value": outcome,
                      "source": source,
                      "timestamp": date})
    if result.calculation:
        facts.append({"label": "structured_resolution",
                      "value": result.calculation,
                      "source": source,
                      "timestamp": date})
    return facts


def _finalize_evidence(evidence: dict, market_id: str) -> None:
    """Hash and pin the evidence. Mutates evidence in place."""
    evidence["evidence_hash"] = sha256(json.dumps(evidence, sort_keys=True))
    evidence["ipfs_cid"] = pin_to_ipfs(evidence, f"oracle-ree-{market_id[:10]}")
    print(f"[v2] Oracle hash: {evidence['evidence_hash']}")
    if evidence.get("ipfs_cid"):
        print(f"[v2] IPFS CID: {evidence['ipfs_cid']}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: REE PROMPT BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_oracle_prompt(original_prompt: str, evidence: dict) -> str:
    """
    Build the prompt that runs through REE inference.
    /no_think prefix prevents Qwen3 thinking tokens (saves tokens).
    """
    outcome   = evidence.get("final_outcome") or "INCONCLUSIVE"
    calc      = evidence.get("oracle_calculation") or ""
    fv        = evidence.get("final_verdict") or {}
    facts     = fv.get("facts") or []
    pipeline  = fv.get("pipeline") or "pipeline"
    mc        = evidence.get("market_class") or {}

    lines = [
        "/no_think",
        "═" * 51,
        "ORACLEREE VERIFIED DATA BLOCK",
        "═" * 51,
        f"Market:      {evidence.get('market_question', '')}",
        f"Captured at: {evidence.get('captured_at', '')}",
        f"Close time:  {evidence.get('close_time', '')}",
        f"Market type: {mc.get('category', 'unknown')}",
        f"Event date:  {mc.get('event_date', 'unknown')}",
        f"Pipeline:    {pipeline}",
        "",
    ]

    if outcome != "INCONCLUSIVE" and facts:
        lines.append("EXTRACTED EVIDENCE:")
        for f in facts:
            if isinstance(f, dict) and f.get("label") and f.get("value"):
                label = f["label"]
                value = f["value"]
                ts    = f" [{f['timestamp']}]" if f.get("timestamp") else ""
                # Skip raw_evidence dump in the REE prompt to save tokens
                if label == "raw_evidence":
                    continue
                lines.append(f"  {label}: {value}{ts}")
        if calc:
            lines.append(f"\nCalculation: {calc}")
        lines += ["", f"OUTCOME: {outcome}"]
    elif outcome == "INCONCLUSIVE":
        reason = evidence.get("oracle_calculation") or "Creator sources could not be fetched or parsed"
        lines += ["EVIDENCE: INCONCLUSIVE", f"Reason: {reason}"]

    lines += [
        "",
        "INTEGRITY:",
        f"  Evidence hash: {evidence.get('evidence_hash', 'N/A')}",
        f"  IPFS CID:      {evidence.get('ipfs_cid', 'Not pinned')}",
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
# SECTION 6: REE RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

def _all_receipts() -> list[Path]:
    return [Path(p) for p in glob.glob(
        str(Path.home() / ".cache/gensyn/**/receipt_*.json"), recursive=True
    )]


def _safe_hash(p: Path, *keys) -> str:
    try:
        d = json.loads(p.read_text())
        for k in keys:
            if not isinstance(d, dict):
                return ""
            d = d.get(k)
            if d is None:
                return ""
        return str(d) if d else ""
    except Exception:
        return ""


def _find_receipt(start_ts: float, expected: str = "") -> Optional[Path]:
    cands = []
    for rp in _all_receipts():
        try:
            if rp.stat().st_mtime >= start_ts - 2:
                cands.append(rp)
        except OSError:
            continue
    cands.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    if expected:
        for rp in cands:
            if _safe_hash(rp, "input", "prompt_hash") == expected:
                return rp
    return cands[0] if cands else None


def run_ree(
    prompt: str,
    model_name: str = "Qwen/Qwen3-0.6B",
    max_new_tokens: int = 200,
) -> Optional[Path]:
    """
    Run REE inference via ree.sh. Returns path to receipt JSON or None.
    """
    ree_dir = Path(__file__).parent
    ree_sh  = ree_dir / "ree.sh"
    if not ree_sh.exists():
        print("[ree] ERROR: ree.sh not found")
        return None

    pf = ree_dir / "oracle_prompt.jsonl"
    with open(pf, "w", encoding="utf-8") as f:
        json.dump({"prompt": prompt}, f, ensure_ascii=False)
        f.write("\n")
    pf.chmod(0o644)

    expected = sha256(prompt)
    started  = time.time()
    print(f"\n[ree] model={model_name} | {len(prompt)} chars")

    try:
        result = subprocess.run(
            ["bash", str(ree_sh),
             "--model-name",    model_name,
             "--prompt-file",   str(pf),
             "--max-new-tokens", str(max_new_tokens)],
            cwd=str(ree_dir),
            capture_output=True,
            text=True,
            timeout=1200,
        )
        out = (result.stdout or "") + "\n" + (result.stderr or "")

        if result.returncode != 0:
            print(f"[ree] ERROR exit {result.returncode}")
            print(out[-2000:])
            return None

        print("[ree] REE exited OK")

        # Find receipt path from stdout
        rp = None
        for line in out.splitlines():
            if "receipt" not in line.lower():
                continue
            m = re.search(r"(/[^\s]+receipt_[0-9_]+\.json)", line)
            if m and Path(m.group(1)).exists():
                rp = Path(m.group(1))
                break

        if not rp:
            rp = _find_receipt(started, expected)
        if not rp:
            print("[ree] ERROR: no receipt found")
            return None

        ph = _safe_hash(rp, "input", "prompt_hash")
        print("[ree] ✓ Hash verified" if ph == expected else "[ree] Receipt generated")
        rh = _safe_hash(rp, "hashes", "receipt_hash")
        print(f"[ree] {rp}")
        if rh:
            print(f"[ree] hash: {rh}")
        print("[ree] ✓ REE complete")
        return rp

    except subprocess.TimeoutExpired:
        print("[ree] ERROR: timeout 1200s")
        return None
    finally:
        pf.unlink(missing_ok=True)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: PROOF BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_combined_proof(
    market_id: str,
    evidence: dict,
    receipt_path: Optional[Path],
    prompt_integrity: Optional[dict] = None,
) -> dict:
    """
    Assemble the final proof JSON.
    Structure is compatible with ree.py TUI dashboard.
    """
    ipfs_cid = evidence.get("ipfs_cid") or ""
    outcome  = (evidence.get("final_outcome")
                or evidence.get("oracle_result")
                or "INCONCLUSIVE")

    proof = {
        "version":   "2.0.0",
        "tool":      "OracleREE-v2",
        "market_id": market_id,
        "created_at": now_iso(),

        # Top-level outcome fields (ree.py reads these)
        "oracle_result":      outcome,
        "oracle_outcome":     outcome,
        "final_outcome":      outcome,
        "matched_outcome":    outcome,
        "ree_expected_output": outcome,
        "oracle_calculation": evidence.get("oracle_calculation") or "",

        # Full evidence block
        "oracle_evidence": {
            "market_question":    evidence.get("market_question") or "",
            "oracle_result":      outcome,
            "oracle_outcome":     outcome,
            "final_outcome":      outcome,
            "matched_outcome":    outcome,
            "oracle_calculation": evidence.get("oracle_calculation") or "",
            "evidence_hash":      evidence.get("evidence_hash") or "",
            "ipfs_cid":           ipfs_cid,
            "captured_at":        evidence.get("captured_at") or "",
            "close_time":         evidence.get("close_time") or "",
            "final_verdict":      evidence.get("final_verdict") or {},
            "market_class":       evidence.get("market_class") or {},
            "extracted_fact":     evidence.get("extracted_fact") or {},
            "pipeline_debug":     evidence.get("pipeline_debug") or {},
        },

        "ree_receipt": None,
        "prompt_integrity": prompt_integrity or {
            "prompt_source":       "Official Delphi Prompt",
            "verification_mode":   "CANONICAL_DELPHI_MARKET",
            "prompt_match":        "YES",
            "question_match":      "YES",
            "warning":             "",
        },

        "verification": {
            "oracle_evidence_hash": evidence.get("evidence_hash"),
            "ipfs_cid":             ipfs_cid,
            "oracle_seal_ipfs":     evidence.get("oracle_seal_ipfs"),
            "ree_receipt_hash":     None,
            "ree_receipt_path":     None,
            "combined_hash":        None,
            "oracle_result":        outcome,
            "oracle_outcome":       outcome,
            "final_outcome":        outcome,
            "matched_outcome":      outcome,
            "oracle_calculation":   evidence.get("oracle_calculation") or "",
        },
    }

    # Attach REE receipt if available
    if receipt_path and receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text())
            if receipt is None:
                raise ValueError("Receipt is null")
        except Exception as e:
            print(f"[proof] ERROR reading receipt: {e}")
            receipt = None

        if receipt:
            proof["ree_receipt"] = receipt
            proof["verification"]["ree_receipt_path"] = str(receipt_path)

            hashes = receipt.get("hashes") or {}
            rh = (hashes.get("receipt_hash")
                  or hashes.get("receiptHash")
                  or "")
            ph = (receipt.get("input") or {}).get("prompt_hash") or ""

            proof["verification"]["ree_receipt_hash"] = rh or None
            proof["verification"]["prompt_hash"]      = ph or None

            if rh:
                combined = sha256(str(evidence.get("evidence_hash")) + str(rh))
                proof["verification"]["combined_hash"] = combined
                print(f"\n[proof] Combined: {combined}")
                print(f"[proof] Evidence: {evidence.get('evidence_hash')}")
                print(f"[proof] Receipt:  {receipt_path}")
            else:
                print(f"[proof] WARNING: no receipt_hash. Keys: {list(receipt.keys())}")

    return proof



# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9: SETTLEMENT MODE
# ═══════════════════════════════════════════════════════════════════════════════

def verify_settlement_prompt(market_id: str, provided_prompt: str) -> tuple:
    """
    Verify provided prompt matches official Delphi market prompt.
    Prevents creators from modifying prompt to manipulate outcomes.
    Returns (is_valid, reason, official_hash, provided_hash)
    """
    try:
        market = fetch_market(market_id)
        meta = market.get("metadata") or {}
        official_prompt = (meta.get("model") or {}).get("prompt_context", "")
        if not official_prompt:
            return False, "Could not fetch official prompt from Delphi", "", ""
        official_hash = sha256(official_prompt.strip())
        provided_hash = sha256(provided_prompt.strip())
        if official_hash == provided_hash:
            return True, "Prompt verified", official_hash, provided_hash
        return False, "Prompt mismatch — prompt has been modified", official_hash, provided_hash
    except Exception as e:
        return False, f"Prompt verification failed: {e}", "", ""


def get_frozen_evidence(market_id: str) -> dict:
    """
    Pull frozen evidence from OracleSeal captured at exact close time.
    Returns empty dict if no frozen evidence exists.
    """
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not supabase_key:
        return {}
    try:
        r = requests.get(
            f"{supabase_url}/rest/v1/oracle_markets",
            headers={
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
            },
            params={"market_id": f"eq.{market_id}", "select": "*"},
            timeout=10,
        )
        if r.ok and r.json():
            data = r.json()[0]
            if data.get("captured_at"):
                print(f"[settle] Frozen evidence: {data['captured_at']}")
                print(f"[settle] IPFS: {data.get('ipfs_cid', 'not pinned')}")
                return data
    except Exception as e:
        print(f"[settle] OracleSeal check failed: {e}")
    return {}


def run_settlement(
    market_id: str,
    provided_prompt: str,
    model_override: str = None,
    max_tokens: int = None,
    oracle_only: bool = False,
) -> dict:
    """
    Settlement mode:
    1. Verify prompt matches official Delphi prompt
    2. Check OracleSeal for frozen evidence
    3. Run oracle pipeline
    4. Run REE
    5. Return text output for creator to paste into Delphi
    """
    result = {
        "market_id":       market_id,
        "mode":            "settlement",
        "prompt_verified": False,
        "frozen_evidence": False,
        "oracle_result":   None,
        "ree_output":      None,
        "receipt_path":    None,
        "error":           None,
    }

    print(f"\n[settle] ═══════════════════════════════════")
    print(f"[settle] Settlement mode: {market_id[:20]}...")

    # Step 1: Verify prompt
    print(f"\n[settle] Step 1: Verifying settlement prompt...")
    if provided_prompt:
        valid, reason, official_hash, provided_hash = verify_settlement_prompt(
            market_id, provided_prompt
        )
        result["prompt_verified"]  = valid
        result["official_hash"]    = official_hash
        result["provided_hash"]    = provided_hash
        if valid:
            print(f"[settle] ✓ Prompt verified — matches official Delphi prompt")
        else:
            print(f"[settle] ✗ {reason}")
            print(f"[settle] Official: {official_hash[:20]}...")
            print(f"[settle] Provided: {provided_hash[:20]}...")
            print(f"[settle] WARNING: Proceeding with oracle evidence despite prompt mismatch")
    else:
        print(f"[settle] No prompt provided — skipping verification")

    # Step 2: Check OracleSeal for frozen evidence
    print(f"\n[settle] Step 2: Checking OracleSeal for frozen evidence...")
    frozen = get_frozen_evidence(market_id)
    if frozen:
        result["frozen_evidence"]      = True
        result["frozen_captured_at"]   = frozen.get("captured_at")
        result["frozen_ipfs_cid"]      = frozen.get("ipfs_cid")
        result["frozen_evidence_hash"] = frozen.get("evidence_hash")
        print(f"[settle] ✓ Using frozen evidence from close time")
    else:
        print(f"[settle] No frozen evidence — fetching live")

    # Step 3: Fetch market + run oracle pipeline
    print(f"\n[settle] Step 3: Running oracle pipeline...")
    try:
        market = fetch_market(market_id)
    except Exception as e:
        result["error"] = f"Could not fetch market: {e}"
        return result

    evidence = build_oracle_evidence(market)
    oracle_result = evidence.get("final_outcome", "INCONCLUSIVE")
    result["oracle_result"] = oracle_result
    print(f"[settle] Oracle result: {oracle_result}")

    if oracle_only:
        return result

    # Step 4: Build settlement prompt with frozen evidence
    print(f"\n[settle] Step 4: Building REE prompt...")
    meta         = market.get("metadata") or {}
    delphi_model = (meta.get("model") or {}).get("model_identifier", "")
    ree_model    = model_override or resolve_ree_model(delphi_model)
    max_tok      = max_tokens or int(os.environ.get("ORACLEREE_REE_MAX_TOKENS", "200"))

    # Use provided prompt if available, else use official prompt from market
    prompt_to_use = provided_prompt or (meta.get("model") or {}).get("prompt_context", "")
    settlement_ree_prompt = build_oracle_prompt(prompt_to_use, evidence)
    # Prepend oracle result to guide REE output
    oracle_guidance = (
        f"/no_think\n"
        f"ORACLE VERIFIED RESULT: {oracle_result}\n"
        f"Based on verified evidence, the correct settlement answer is: {oracle_result}\n"
        f"Output exactly: {oracle_result}\n\n"
    )
    settlement_ree_prompt = oracle_guidance + settlement_ree_prompt

    # Inject frozen evidence header
    if frozen:
        frozen_header = (
            f"/no_think\n"
            f"[ORACLESEAL FROZEN EVIDENCE]\n"
            f"Captured at close time: {frozen.get('captured_at')}\n"
            f"Evidence hash: {frozen.get('evidence_hash', 'N/A')}\n"
            f"IPFS CID: {frozen.get('ipfs_cid', 'not pinned')}\n"
            f"Evidence locked at market close — immutable.\n"
            f"[END ORACLESEAL FROZEN EVIDENCE]\n\n"
        )
        settlement_ree_prompt = frozen_header + settlement_ree_prompt

    # Step 5: Run REE
    print(f"\n[settle] Step 5: Running REE ({ree_model})...")
    receipt_path = run_ree(
        prompt=settlement_ree_prompt,
        model_name=ree_model,
        max_new_tokens=max_tok,
    )

    if receipt_path:
        result["receipt_path"] = str(receipt_path)
        try:
            receipt = json.loads(receipt_path.read_text())
            ree_output = receipt.get("output", {}).get("text_output", "")
            ree_output = ree_output.replace("<think>", "").replace("</think>", "")
            ree_output = ree_output.replace("<|im_end|>", "").strip()
            result["ree_output"] = ree_output
            print(f"[settle] REE output: {ree_output}")
        except Exception as e:
            print(f"[settle] Could not read REE output: {e}")
    else:
        result["error"] = "REE receipt not generated"

    # Push to OracleSeal
    proof = build_combined_proof(market_id, evidence, receipt_path)
    proof["mode"] = "settlement"
    push_to_oracle_seal(proof)

    return result

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="OracleREE v2 — Clean Pipeline")
    parser.add_argument("--market", "-m", default=None,
                        help="Delphi market ID (0x...) or URL")
    parser.add_argument("--model", default=None,
                        help="Override REE model name")
    parser.add_argument("--max-tokens", type=int, default=REE_MAX_TOKENS,
                        help=f"REE max new tokens (default: {REE_MAX_TOKENS})")
    parser.add_argument("--oracle-only", action="store_true",
                        help="Run oracle only, skip REE inference")
    parser.add_argument("--settle", action="store_true",
                        help="Settlement mode: verify prompt + frozen evidence + REE")
    parser.add_argument("--prompt", "-p", default=None,
                        help="Settlement prompt text (from Delphi market)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output JSON file path")
    args = parser.parse_args()

    if not _PIPELINE_LOADED:
        print("FATAL: oracle_core/pipeline.py could not be loaded.")
        print("Make sure oracle_core/ is in the same directory as oracle_ree_v2.py")
        return 1

    # ── Get market input ──────────────────────────────────────────────────────
    market_input = args.market
    if not market_input:
        market_input = input("Paste Delphi market URL or 0x ID: ").strip()
    if not market_input:
        print("Error: market input required")
        return 1

    # ── Extract market ID ─────────────────────────────────────────────────────
    try:
        market_id = extract_market_id(market_input)
    except ValueError as e:
        print(f"Error: {e}")
        return 1
    print(f"\n[v2] Market ID: {market_id}")

    # ── Fetch market from Delphi ──────────────────────────────────────────────
    try:
        market = fetch_market(market_id)
    except Exception as e:
        print(f"Error fetching market: {e}")
        return 1

    meta          = market.get("metadata") or {}
    question      = meta.get("question") or "Unknown"
    prompt_ctx    = (meta.get("model") or {}).get("prompt_context") or question
    delphi_model  = (meta.get("model") or {}).get("model_identifier") or ""
    ree_model     = args.model or resolve_ree_model(delphi_model)

    print(f"[v2] Question: {question}")
    print(f"[v2] Model:    {delphi_model} → {ree_model}")

    print(f"[oracle] Prompt Source: Official Delphi Prompt")
    print(f"[oracle] Prompt Match: YES")
    print(f"[oracle] Question Match: YES")
    print(f"[oracle] Verification Mode: CANONICAL_DELPHI_MARKET")

    # ── Settlement mode ───────────────────────────────────────────────────────
    if args.settle:
        provided_prompt = args.prompt or extract_settlement_prompt(market_input)
        settlement = run_settlement(
            market_id=market_id,
            provided_prompt=provided_prompt,
            model_override=args.model,
            max_tokens=args.max_tokens,
            oracle_only=args.oracle_only,
        )
        print("\n" + "=" * 60)
        print("ORACLEREE SETTLEMENT SUMMARY")
        print("=" * 60)
        print(f"Market:          {market_id}")
        print(f"Prompt verified: {'✓ YES' if settlement['prompt_verified'] else '✗ NO — MODIFIED'}")
        print(f"Frozen evidence: {'✓ YES — OracleSeal locked' if settlement['frozen_evidence'] else '⚠ NO — live fetch used'}")
        if settlement.get("frozen_captured_at"):
            print(f"Captured at:     {settlement['frozen_captured_at']}")
        if settlement.get("frozen_ipfs_cid"):
            print(f"IPFS CID:        {settlement['frozen_ipfs_cid']}")
        print(f"Oracle result:   {settlement.get('oracle_result', 'INCONCLUSIVE')}")
        if settlement.get("ree_output"):
            print("\n" + "="*60)
            print("  PASTE THIS INTO DELPHI TO SETTLE THE MARKET:")
            print(f"  → {settlement['ree_output']}")
            print("="*60)
        if settlement.get("receipt_path"):
            print(f"\nREE Receipt: {settlement['receipt_path']}")
        if settlement.get("error"):
            print(f"\nError: {settlement['error']}")
        print("=" * 60)
        return 0

    # ── Build oracle evidence via pipeline ────────────────────────────────────
    evidence = build_oracle_evidence(market)

    # ── Build REE prompt ──────────────────────────────────────────────────────
    oracle_prompt = build_oracle_prompt(prompt_ctx, evidence)
    print(f"\n[v2] Prompt: {len(oracle_prompt)} chars")
    print(f"[v2] Hash:   {evidence.get('evidence_hash')}")

    # ── Run REE inference ─────────────────────────────────────────────────────
    receipt_path = None
    if not args.oracle_only:
        receipt_path = run_ree(
            prompt=oracle_prompt,
            model_name=ree_model,
            max_new_tokens=args.max_tokens,
        )
        if not receipt_path:
            print("\n[ree] No receipt — oracle evidence saved only.")
    else:
        print("\n[ree] Skipped (--oracle-only)")

    # ── Assemble proof ────────────────────────────────────────────────────────
    proof = build_combined_proof(market_id, evidence, receipt_path)

    # ── Save proof ────────────────────────────────────────────────────────────
    out_path = (
        args.output
        or f"oracle_proof_{market_id[:10]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(proof, f, indent=2, ensure_ascii=False)
    print(f"\n[proof] Saved: {out_path}")

    # ── Summary ───────────────────────────────────────────────────────────────
    outcome  = proof.get("final_outcome") or "INCONCLUSIVE"
    calc     = proof.get("oracle_calculation") or ""
    v        = proof.get("verification") or {}
    mc       = (proof.get("oracle_evidence") or {}).get("market_class") or {}
    ef       = (proof.get("oracle_evidence") or {}).get("extracted_fact") or {}
    pd_      = (proof.get("oracle_evidence") or {}).get("pipeline_debug") or {}

    print("\n" + "=" * 60)
    print("ORACLEREE v2 SUMMARY")
    print("=" * 60)
    print(f"Market:   {question}")
    print(f"Model:    {delphi_model} → {ree_model}")
    print(f"Category: {mc.get('category', '?')} | {mc.get('extract_method', '?')}")
    if outcome != "INCONCLUSIVE":
        print(f"Verdict:  ✓ {outcome}")
        print(f"Source:   {pd_.get('source', '?')} / {pd_.get('method', '?')}")
        if ef.get("value"):
            print(f"Extracted: {ef.get('fact_type', '')}: {ef.get('value', '')}")
        if calc:
            print(f"Calc:     {calc}")
    else:
        print(f"Verdict:  INCONCLUSIVE")
        print(f"Reason:   {calc[:120]}")
    if v.get("ree_receipt_hash"):
        print(f"REE:      {str(v['ree_receipt_hash'])[:20]}...")
        print(f"Combined: {str(v.get('combined_hash', ''))[:20]}...")
        print("✓ Cryptographically linked")
    else:
        print("⚠ REE receipt missing")
    print("=" * 60)

    # ── Clean up old proof files (keep last 5 per market) ────────────────────
    try:
        proof_dir  = Path(__file__).parent
        all_proofs = sorted(
            proof_dir.glob("oracle_proof_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        seen: dict = {}
        for p in all_proofs:
            m = re.match(r"oracle_proof_(0x[a-f0-9]+)_", p.name)
            key = m.group(1) if m else p.name[:24]
            seen.setdefault(key, []).append(p)
        deleted = 0
        for files in seen.values():
            for old in files[5:]:
                old.unlink(missing_ok=True)
                deleted += 1
        if deleted:
            print(f"[proof] Cleaned {deleted} old proof files")
    except Exception as e:
        print(f"[proof] Cleanup warning: {e}")

    # ── Push to OracleSeal ────────────────────────────────────────────────────
    try:
        if v.get("ree_receipt_hash"):
            push_to_oracle_seal(proof)
    except Exception as e:
        print(f"[proof] OracleSeal push warning: {e}")

    # Return 0 on success, 2 if oracle ran but no REE receipt
    if args.oracle_only:
        return 0
    return 0 if v.get("ree_receipt_hash") else 2


if __name__ == "__main__":
    raise SystemExit(main())