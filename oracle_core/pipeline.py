#!/usr/bin/env python3
"""
oracle_core/pipeline.py

The clean OracleREE pipeline. Orchestrates all steps.

Usage:
    from oracle_core.pipeline import run_pipeline
    result = run_pipeline(market_dict)
    # result.outcome = "Man City" or "INCONCLUSIVE"

Steps:
    1. classify_market()  → what type, what source, how to extract
    2. fetch_source()     → get validated content
    3. extract_fact()     → what does content say? (LLM extracts, not decides)
    4. match_to_outcome() → map to valid outcome (deterministic + LLM alias)
    5. build_result()     → structured output for oracle_ree.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Load .env.local
_ENV_FILE = Path(__file__).parent.parent / ".env.local"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

from oracle_core.classify import classify_market, MarketClass
from oracle_core.fetch_source import fetch_source, FetchResult
from oracle_core.extract import extract_fact, ExtractedFact
from oracle_core.resolve import match_to_outcome


@dataclass
class PipelineResult:
    """Final output of the pipeline."""
    outcome: str = "INCONCLUSIVE"
    confidence: str = "none"
    source: str = ""
    method: str = ""
    calculation: str = ""
    fetch_result: Optional[FetchResult] = None
    extracted_fact: Optional[ExtractedFact] = None
    market_class: Optional[MarketClass] = None
    steps: list = field(default_factory=list)  # debug trace


def run_pipeline(
    market: dict,
    debug: bool = False,
) -> PipelineResult:
    """
    Run the full OracleREE pipeline for a single market.

    market dict expected fields:
        question / title
        outcomes (list)
        promptContext / prompt_context
        metadata.model.prompt_context
        metadata.outcomes
        _rules (dict)
        event_date
        closeTime / close_time
    """
    result = PipelineResult()

    # ── Extract market fields ─────────────────────────────────────────────────
    meta = market.get("metadata") or {}
    outcomes = (
        market.get("outcomes")
        or meta.get("outcomes")
        or []
    )
    question = (
        market.get("question")
        or market.get("title")
        or meta.get("question")
        or ""
    )
    prompt_context = (
        market.get("promptContext")
        or market.get("prompt_context")
        or (meta.get("model") or {}).get("prompt_context")
        or ""
    )
    rules = market.get("_rules") or {}
    event_date = str(market.get("event_date") or "")[:10]
    close_time = str(
        market.get("closeTime") or market.get("close_time") or ""
    )

    # Also get resolvesAt for close_time fallback
    if not close_time:
        close_time = str(market.get("resolvesAt") or "")

    # Get event_date from resolvesAt if not set
    if not event_date:
        resolves_at = str(market.get("resolvesAt") or "")
        if resolves_at:
            event_date = resolves_at[:10]

    # Creator source URL from rules or prompt_context
    source_url = ""
    source_order = rules.get("source_order") or []
    if source_order:
        first_source = str(source_order[0])
        if first_source.startswith("http"):
            source_url = first_source
        else:
            # Map known source names to URLs
            source_map = {
                "espn": "https://www.espn.com/",
                "coinmarketcap": "https://coinmarketcap.com/",
                "coingecko": "https://www.coingecko.com/",
                "yahoo": "https://finance.yahoo.com/",
                "strategy.com": "https://www.strategy.com/purchases",
            }
            source_url = source_map.get(first_source.lower(), f"https://{first_source}")

    if not source_url:
        # Try to extract from prompt_context
        import re
        url_m = re.search(r"https?://[^\s)\"']+", str(prompt_context or ""))
        if url_m:
            source_url = url_m.group(0).rstrip(".,)")

    if debug:
        result.steps.append(f"question: {question[:80]}")
        result.steps.append(f"outcomes: {outcomes}")
        result.steps.append(f"source_url: {source_url}")

    # ── Step 1: Classify ──────────────────────────────────────────────────────
    try:
        mc = classify_market(
            question=question,
            outcomes=outcomes,
            prompt_context=prompt_context,
            rules=rules,
            event_date=event_date,
            close_time=close_time or str(market.get("resolvesAt") or ""),
        )
        result.market_class = mc
        if debug:
            result.steps.append(
                f"classify: {mc.category} | {mc.fetch_strategy} | {mc.extract_method} | {mc.classification_reason}"
            )
        print(f"[pipeline] classify: {mc.category} | {mc.extract_method} | {mc.classification_reason}")
    except Exception as e:
        print(f"[pipeline] classify failed: {e}")
        result.calculation = f"INCONCLUSIVE: classify failed: {e}"
        return result

    # ── Step 2: Fetch ─────────────────────────────────────────────────────────
    try:
        fetch_result = fetch_source(
            source_url=source_url or "https://www.espn.com/",
            query=question,
            event_date=mc.event_date or event_date,
            market_category=mc.category,
            question=question,
            outcomes=outcomes,
            prompt_context=prompt_context,
            window_start=mc.window_start,
            window_end=mc.window_end,
            asset=mc.asset,
            close_time=close_time,
        )
        result.fetch_result = fetch_result
        if debug:
            result.steps.append(
                f"fetch: is_valid={fetch_result.is_valid} | method={fetch_result.method} | source={fetch_result.source}"
            )
        if not fetch_result.is_valid:
            result.calculation = f"INCONCLUSIVE: fetch failed — {fetch_result.validation_reason}"
            print(f"[pipeline] fetch failed: {fetch_result.validation_reason}")
            return result
        print(f"[pipeline] fetch ok: {fetch_result.method} from {fetch_result.source}")
    except Exception as e:
        print(f"[pipeline] fetch failed: {e}")
        result.calculation = f"INCONCLUSIVE: fetch error: {e}"
        return result

    # ── Step 3: Extract ───────────────────────────────────────────────────────
    # Check if fetch already pre-resolved the outcome (structured API result)
    if fetch_result.matched_outcome and str(fetch_result.matched_outcome).strip().upper() != "INCONCLUSIVE":
        pre_matched = str(fetch_result.matched_outcome).strip()
        pre_fact = ExtractedFact(
            value=pre_matched,
            fact_type="winner",
            confidence="high",
            source=fetch_result.method,
            reasoning=f"pre-resolved by fetch layer: {fetch_result.validation_reason}",
        )
        matched = match_to_outcome(pre_fact, outcomes, question, mc.extract_method)
        if matched:
            result.outcome = matched
            result.confidence = "high"
            result.source = fetch_result.source
            result.method = fetch_result.method
            result.calculation = f"pipeline: {mc.category} | {fetch_result.source} → '{pre_matched}' → {matched}"
            result.extracted_fact = pre_fact
            if debug:
                result.steps.append(f"pre-resolved: '{pre_matched}' → '{matched}'")
            print(f"[pipeline] ✓ pre-resolved: '{pre_matched}' → '{matched}'")
            return result
        print(f"[pipeline] pre-resolved '{pre_matched}' could not match outcomes — continuing to extract")

    try:
        fact = extract_fact(
            content=fetch_result.content,
            question=question,
            outcomes=outcomes,
            extract_method=mc.extract_method,
            team1=mc.team1,
            team2=mc.team2,
            asset=mc.asset,
            confirmation_entity=mc.confirmation_entity,
            confirmation_action=mc.confirmation_action,
            window_start=mc.window_start,
            window_end=mc.window_end,
        )
        result.extracted_fact = fact
        if debug:
            result.steps.append(
                f"extract: value={fact.value!r} | confidence={fact.confidence} | source={fact.source}"
            )
        if not fact.value or fact.confidence == "low":
            result.calculation = (
                f"INCONCLUSIVE: extraction low confidence — {fact.reasoning}"
            )
            print(f"[pipeline] extract low confidence: {fact.reasoning}")
            return result
        print(f"[pipeline] extract: {fact.value!r} ({fact.confidence}) via {fact.source}")
    except Exception as e:
        print(f"[pipeline] extract failed: {e}")
        result.calculation = f"INCONCLUSIVE: extract error: {e}"
        return result

    # ── Step 4: Match ─────────────────────────────────────────────────────────
    try:
        matched = match_to_outcome(
            fact=fact,
            outcomes=outcomes,
            question=question,
            extract_method=mc.extract_method,
        )
        if debug:
            result.steps.append(f"match: {fact.value!r} → {matched!r}")
        if not matched:
            result.calculation = (
                f"INCONCLUSIVE: no outcome match for extracted value {fact.value!r}"
            )
            print(f"[pipeline] no match for: {fact.value!r}")
            return result
        print(f"[pipeline] ✓ matched: {fact.value!r} → {matched!r}")
    except Exception as e:
        print(f"[pipeline] match failed: {e}")
        result.calculation = f"INCONCLUSIVE: match error: {e}"
        return result

    # ── Build result ──────────────────────────────────────────────────────────
    result.outcome = matched
    result.confidence = fact.confidence
    result.source = fetch_result.source
    result.method = fetch_result.method
    result.calculation = (
        f"pipeline: {mc.category} | {fetch_result.source} → {fact.value!r} → {matched}"
    )
    return result