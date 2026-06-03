#!/usr/bin/env python3
"""
oracle_core/extract.py

Step 3 of the OracleREE pipeline: extract a fact from validated content.

Rules:
  - LLMs (Groq, Ollama) live here ONLY.
  - LLMs extract facts — they do NOT pick outcomes.
  - Python does the final outcome matching (Step 4 / resolve.py).
  - The settlement prompt (prompt_context) is ALWAYS passed to the LLM.
  - The LLM follows creator rules — no hardcoded edge cases.
  - If extraction confidence is low → return None (INCONCLUSIVE upstream).

Extraction methods:
  score_winner      → "Manchester City won 1-0"
  price_band        → "$2,394.99"
  price_threshold   → "closed at $5,312.45"
  llm_confirmation  → "Yes, Strategy announced a Bitcoin purchase on April 22"
  llm_named_choice  → "Bulgaria won Eurovision 2026"
  spread_cover      → "Kings covered +10.5, final score 98-85"
  count_compare     → "7 trades occurred in round 1"
  llm_generic       → free-form extraction
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

import requests


# ─── Config ──────────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
OLLAMA_URL   = _env("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = _env("OLLAMA_MODEL", "qwen2.5:3b-instruct")


# ─── Data class ──────────────────────────────────────────────────────────────

@dataclass
class ExtractedFact:
    """
    Result of extraction from validated content.
    The 'value' is a raw extracted string — NOT a valid outcome.
    Outcome matching happens in resolve.py.
    """
    value: str = ""            # raw extracted value e.g. "Manchester City", "$2,394"
    fact_type: str = ""        # winner | price | count | confirmation | named_entity
    confidence: str = "low"    # high | medium | low
    source: str = ""           # which extractor found this
    reasoning: str = ""        # brief explanation
    raw_content_used: str = "" # first 200 chars of content used


# ─── LLM callers ─────────────────────────────────────────────────────────────

def _call_groq(prompt: str, system: str = "", max_tokens: int = 400) -> Optional[dict]:
    """Call Groq API, return parsed JSON or None."""
    api_key = _env("GROQ_API_KEY")
    if not api_key:
        return None

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        r = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "temperature": 0,
                "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )

        # Groq rate limit: return None so _call_llm() automatically falls back to Ollama.
        if r.status_code == 429:
            print("[extract] Groq rate limited — falling back to Ollama")
            return None

        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return json.loads(content)

    except Exception as e:
        print(f"[extract] Groq failed: {e}")
        return None


def _call_ollama(prompt: str, max_tokens: int = 400) -> Optional[dict]:
    """Call local Ollama, return parsed JSON or None."""
    use_ollama = _env("USE_OLLAMA_BRAIN", "1").lower() not in {"0", "false", "no", "off"}
    if not use_ollama:
        return None
    try:
        r = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "stream": False,
            "format": "json",
            "prompt": prompt,
            "options": {"temperature": 0, "num_predict": max_tokens},
        }, timeout=60)
        r.raise_for_status()
        raw = r.json().get("response", "{}")
        raw = re.sub(r"```json|```", "", raw).strip()
        result = json.loads(raw)
        # If Ollama returns answer without confidence, default to medium
        if isinstance(result, dict) and result.get("answer") and not result.get("confidence"):
            result["confidence"] = "medium"
        return result
    except Exception as e:
        print(f"[extract] Ollama failed: {e}")
        return None


def _call_llm(prompt: str, system: str = "", max_tokens: int = 400) -> Optional[dict]:
    """Try Groq first, fall back to Ollama."""
    result = _call_groq(prompt, system, max_tokens)
    if result:
        return result
    return _call_ollama(prompt, max_tokens)


def _settlement_rules_block(prompt_context: str) -> str:
    """Format the settlement rules block for inclusion in LLM prompts."""
    if not prompt_context or not prompt_context.strip():
        return ""
    return (
        "SETTLEMENT RULES (defined by market creator — follow exactly):\n"
        + str(prompt_context).strip()[:1500]
        + "\n\n"
    )


# ─── Extractors ──────────────────────────────────────────────────────────────

def extract_score_winner(
    content: str,
    question: str,
    outcomes: list,
    team1: str,
    team2: str,
    prompt_context: str = "",
) -> ExtractedFact:
    """
    Extract the winner of a sports match from validated content.
    Tries deterministic patterns first, falls back to LLM.
    The settlement prompt is passed to the LLM so it can handle
    edge cases (Super Over, overtime, penalties, etc.) per creator rules.
    """
    fact = ExtractedFact(fact_type="winner", source="extract_score_winner")
    fact.raw_content_used = content[:200]

    # 1. ANSWER line (from structured APIs / Tavily)
    answer_m = re.search(r"ANSWER:\s*(.+?)(?:\n\n|\n\[|\Z)", content, re.I | re.S)
    if answer_m:
        answer = answer_m.group(1).strip()
        # "Winner: Liverpool" pattern
        winner_m = re.search(r"Winner:\s*([^\n]+)", answer, re.I)
        if winner_m:
            winner = winner_m.group(1).strip()
            if winner.lower() not in {"draw", "unknown", "none", ""}:
                fact.value = winner
                fact.confidence = "high"
                fact.source = "answer_line_winner"
                fact.reasoning = f"Winner field in ANSWER line: {winner}"
                return fact
        # Draw detection — only if no Super Over language nearby
        if re.search(r"\b(draw|tied|tie)\b", answer, re.I):
            if not re.search(r"\b(super over|superover|tiebreaker|bowl-off)\b",
                             content, re.I):
                fact.value = "Draw"
                fact.confidence = "high"
                fact.source = "answer_line_draw"
                return fact

    # 2. Score pattern with team context
    if team1 and team2:
        t1_idx = content.lower().find(team1.lower())
        t2_idx = content.lower().find(team2.lower())
        if t1_idx >= 0 and t2_idx >= 0:
            win_start = max(0, min(t1_idx, t2_idx) - 500)
            win_end   = max(t1_idx, t2_idx) + 500
            window    = content[win_start:win_end]

            scores = re.findall(r"\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b", window)
            if scores:
                hs, as_ = int(scores[0][0]), int(scores[0][1])
                if hs != as_:
                    fact.value = team1 if (
                        (t1_idx < t2_idx and hs > as_) or
                        (t1_idx > t2_idx and as_ > hs)
                    ) else team2
                    fact.confidence = "medium"
                    fact.source = "score_pattern"
                    fact.reasoning = f"Score {hs}-{as_} near team names"
                    return fact
                # Tied score — fall through to LLM which will check settlement rules

    # 3. LLM extraction — receives full settlement prompt
    rules_block = _settlement_rules_block(prompt_context)
    valid_str   = json.dumps([str(o) for o in outcomes or []], ensure_ascii=False)

    system = (
        "You are OracleREE's fact extractor for sports markets. "
        "Extract the match winner from the evidence. "
        "Follow the settlement rules exactly. "
        "If rules say regulation time only → ignore Super Over/overtime/penalties. "
        "If rules say nothing special → use the official final result including tiebreakers. "
        "Return the winning team name as it appears in the evidence — do NOT guess. "
        "Return JSON: {\"winner\": \"team name or Draw\", \"score\": \"X-Y or null\", "
        "\"confidence\": \"high|medium|low\", \"reasoning\": \"one sentence\"}"
    )
    prompt = (
        f"{rules_block}"
        f"Question: {question}\n"
        f"Teams: {team1} vs {team2}\n"
        f"Valid outcomes (for reference only): {valid_str}\n\n"
        f"Evidence:\n{content[:3000]}\n\n"
        f"Extract the match winner following the settlement rules above."
    )

    result = _call_llm(prompt, system, max_tokens=200)
    if result and isinstance(result, dict):
        winner     = str(result.get("winner") or "").strip()
        confidence = str(result.get("confidence") or "low").lower()
        reasoning  = str(result.get("reasoning") or "")
        if winner and confidence in {"high", "medium"}:
            fact.value      = winner
            fact.confidence = confidence
            fact.source     = "groq_extraction"
            fact.reasoning  = reasoning
            print(f"[extract] LLM winner: {winner} ({confidence})")
    return fact


def extract_price_value(
    content: str,
    question: str,
    asset: str,
    metric: str = "high",
    prompt_context: str = "",
    event_date: str = "",
) -> ExtractedFact:
    """
    Extract a price value from content.
    Reads structured CoinGecko output first, then regex, then LLM.
    """
    fact = ExtractedFact(fact_type="price", source="extract_price_value")
    fact.raw_content_used = content[:200]

    # 1. Structured CoinGecko content — read value_usd directly
    vline = re.search(r"value_usd:\s*([\d.]+)", content)
    if vline:
        fact.value      = vline.group(1)
        fact.confidence = "high"
        fact.source     = "coingecko_structured"
        fact.reasoning  = "read value_usd directly from CoinGecko structured content"
        return fact

    # 2. Try JSON parse
    try:
        data = json.loads(content)
        if "selected_price_usd" in data:
            fact.value      = str(data["selected_price_usd"])
            fact.confidence = "high"
            fact.source     = "coingecko_json"
            return fact
        # ORNN API: {"success": true, "data": [{"timestamp": "...", "index_value": 3.45}]}
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            from datetime import datetime, timezone
            import re as _re
            # Extract target date from question
            date_m = _re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(january|february|march|april|may|june|july|august|september|october|november|december)|(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:st|nd|rd|th)?", content.lower() + " " + asset.lower())
            entries = data["data"]
            if entries:
                # Find entry closest to close time or just use last entry
                best = entries[-1]
                if event_date:
                    for entry in entries:
                        ts = str(entry.get("timestamp", ""))
                        if event_date in ts:
                            best = entry
                            break
                val = best.get("index_value") or best.get("value") or best.get("price")
                if val is not None:
                    fact.value      = str(val)
                    fact.confidence = "high"
                    fact.source     = "ornn_api_json"
                    fact.reasoning  = f"extracted index_value from ORNN API data array"
                    return fact
    except Exception:
        pass

    # 3. Regex — find dollar amounts, avoid years
    prices = re.findall(r"\$?([\d,]+(?:\.\d+)?)", content)
    prices = [float(p.replace(",", "")) for p in prices
              if float(p.replace(",", "")) > 100
              and not (1900 <= float(p.replace(",", "")) <= 2100)]
    if prices:
        if metric == "high":
            val = max(prices)
        elif metric == "low":
            val = min(prices)
        else:
            val = prices[-1]
        fact.value      = str(val)
        fact.confidence = "medium"
        fact.source     = "regex_price"
    return fact


def extract_confirmation(
    content: str,
    question: str,
    entity: str,
    action: str,
    window_start: str,
    window_end: str,
    prompt_context: str = "",
) -> ExtractedFact:
    """
    Extract whether a confirmation event happened in a date window.
    Returns "Yes", "No", or "" (unknown → INCONCLUSIVE upstream).

    Key rule: absence of evidence ≠ evidence of absence.
    Only return "No" if source explicitly states no event occurred.
    The settlement prompt drives what counts as confirmation.
    """
    fact = ExtractedFact(fact_type="confirmation", source="extract_confirmation")
    fact.raw_content_used = content[:200]

    if not content.strip():
        fact.value     = ""
        fact.confidence = "low"
        fact.reasoning  = "no content to extract from"
        return fact

    rules_block = _settlement_rules_block(prompt_context)

    system = (
        "You are OracleREE's fact extractor for confirmation markets. "
        "Determine if the event described occurred during the specified time window. "
        "Follow the creator's settlement rules exactly. "
        "CRITICAL RULES:\n"
        "- The ANSWER line is Tavily's synthesis — it can be WRONG. Always check the actual article content below it.\n"
        "- If any article URL or content mentions the event occurring in the window, return 'Yes'.\n"
        "- Return 'Yes' if you find ANY clear evidence the event occurred in the window.\n"
        "- CRITICAL: A failed attempt does NOT count as the event occurring. 'Attempted but failed', 'attempt was unsuccessful', 'did not complete' = No.\n"
        "- Return 'No' ONLY if multiple sources explicitly confirm the event did NOT occur.\n"
        "- Return 'Unknown' if content is unclear or contradictory.\n"
        "- 'Unknown' is NOT the same as 'No'. Absence of evidence ≠ No.\n"
        "- For purchase/announcement markets: a news article title saying 'Company buys X' IS confirmation.\n"
        "Return JSON: {\"occurred\": \"Yes|No|Unknown\", \"confidence\": \"high|medium|low\", "
        "\"evidence_quote\": \"brief quote from article content not ANSWER line\", "
        "\"reasoning\": \"one sentence\"}"
    )
    prompt = (
        f"{rules_block}"
        f"Question: {question}\n"
        f"Entity: {entity}\n"
        f"Action: {action}\n"
        f"Time window: {window_start} to {window_end}\n\n"
        f"Content to analyse (NOTE: The ANSWER line is a summary that may be inaccurate. "
        f"Read the actual article content in the URLs below it for ground truth):\n"
        f"{content[:4000]}\n\n"
        f"Did the event occur in the time window? "
        f"Check article content carefully, not just the ANSWER summary."
    )

    result = _call_llm(prompt, system, max_tokens=300)
    if result and isinstance(result, dict):
        occurred   = str(result.get("occurred") or "Unknown").strip()
        confidence = str(result.get("confidence") or "low").lower()
        reasoning  = str(result.get("reasoning") or "")
        if occurred in {"Yes", "No"} and confidence in {"high", "medium"}:
            fact.value      = occurred
            fact.confidence = confidence
            fact.reasoning  = reasoning
            fact.source     = "groq_confirmation"
            print(f"[extract] Confirmation: {occurred} ({confidence})")
        else:
            fact.value      = ""
            fact.confidence = "low"
            fact.reasoning  = f"LLM returned Unknown or low confidence: {reasoning}"
    return fact


def extract_named_choice(
    content: str,
    question: str,
    outcomes: list,
    prompt_context: str = "",
) -> ExtractedFact:
    """
    Extract the answer to a named choice market (Eurovision, NFL Draft, etc.)
    The settlement prompt tells the LLM exactly what to look for.
    LLM returns a raw extracted name — resolve.py maps it to the exact outcome.
    """
    fact = ExtractedFact(fact_type="named_entity", source="extract_named_choice")
    fact.raw_content_used = content[:200]

    if not content.strip():
        return fact

    rules_block = _settlement_rules_block(prompt_context)
    valid_str   = json.dumps([str(o) for o in outcomes or []], ensure_ascii=False)

    system = (
        "You are OracleREE's fact extractor. "
        "Extract the answer from the evidence following the creator's settlement rules. "
        "Return the answer as it appears in the evidence — NOT from the valid outcomes list. "
        "Return JSON: {\"answer\": \"extracted value\", \"confidence\": \"high|medium|low\", "
        "\"evidence_quote\": \"brief quote\", \"reasoning\": \"one sentence\"}"
    )
    prompt = (
        f"{rules_block}"
        f"Question: {question}\n"
        f"Valid outcomes (for reference only): {valid_str}\n\n"
        f"Evidence:\n{content[:3000]}\n\n"
        f"What is the answer to the question based on the evidence and settlement rules above?"
    )

    result = _call_llm(prompt, system, max_tokens=300)
    if result and isinstance(result, dict):
        answer     = str(result.get("answer") or "").strip()
        confidence = str(result.get("confidence") or "low").lower()
        reasoning  = str(result.get("reasoning") or "")
        if answer and confidence in {"high", "medium"}:
            fact.value      = answer
            fact.confidence = confidence
            fact.reasoning  = reasoning
            fact.source     = "groq_named_choice"
            print(f"[extract] Named choice: {answer} ({confidence})")
    return fact



def extract_spread_cover(
    content: str,
    question: str,
    outcomes: list,
    prompt_context: str = "",
) -> ExtractedFact:
    """
    Extract spread cover result.
    Needs final score + spread line to calculate who covered.
    LLM does the math per creator rules.
    """
    fact = ExtractedFact(fact_type="spread", source="extract_spread_cover")
    fact.raw_content_used = content[:200]

    if not content.strip():
        return fact

    rules_block = _settlement_rules_block(prompt_context)
    valid_str = json.dumps([str(o) for o in outcomes or []], ensure_ascii=False)

    system = (
        "You are OracleREE's spread cover calculator. "
        "Given a final score and spread line, calculate who covered. "
        "Rules:\n"
        "- Favorite (negative spread) must win by MORE than the line to cover\n"
        "- Underdog (positive spread) covers if they win OR lose by less than the line\n"
        "- Extract the exact outcome string from the valid outcomes list\n"
        "Return JSON: {\"covered\": \"exact outcome string\", "
        "\"final_score\": \"X-Y\", \"margin\": number, "
        "\"confidence\": \"high|medium|low\", \"reasoning\": \"one sentence\"}"
    )
    prompt = (
        f"{rules_block}"
        f"Question: {question}\n"
        f"Valid outcomes: {valid_str}\n\n"
        f"Evidence:\n{content[:3000]}\n\n"
        f"Calculate who covered the spread based on the final score."
    )

    result = _call_llm(prompt, system, max_tokens=300)
    if result and isinstance(result, dict):
        covered = str(result.get("covered") or "").strip()
        confidence = str(result.get("confidence") or "low").lower()
        reasoning = str(result.get("reasoning") or "")
        if covered and confidence in {"high", "medium"}:
            fact.value = covered
            fact.confidence = confidence
            fact.reasoning = reasoning
            fact.source = "groq_spread"
            print(f"[extract] Spread cover: {covered} ({confidence})")
    return fact


# ─── Main entry point ─────────────────────────────────────────────────────────

def extract_fact(
    content: str,
    question: str,
    outcomes: list,
    extract_method: str,
    team1: str = "",
    team2: str = "",
    asset: str = "",
    confirmation_entity: str = "",
    confirmation_action: str = "",
    window_start: str = "",
    window_end: str = "",
    prompt_context: str = "",   # ← settlement prompt, passed to every LLM call
    event_date: str = "",
) -> ExtractedFact:
    """
    Main entry point. Routes to the right extractor based on extract_method.
    prompt_context (the market's settlement prompt) is passed to every LLM call
    so the LLM follows creator rules for edge cases automatically.
    Always returns ExtractedFact — never raises.
    """
    try:
        if extract_method == "score_winner":
            return extract_score_winner(
                content, question, outcomes, team1, team2,
                prompt_context=prompt_context,
            )

        if extract_method in {"price_band", "price_threshold"}:
            metric = (
                "high" if any(k in question.lower() for k in
                              ["highest", "high", "max", "peak", "how high", "reach"])
                else "low" if any(k in question.lower() for k in
                                  ["lowest", "low", "min", "bottom"])
                else "close"
            )
            return extract_price_value(
                content, question, asset, metric,
                prompt_context=prompt_context,
                event_date=event_date,
            )

        if extract_method == "llm_confirmation":
            return extract_confirmation(
                content, question,
                confirmation_entity, confirmation_action,
                window_start, window_end,
                prompt_context=prompt_context,
            )

        if extract_method == "llm_named_choice":
            return extract_named_choice(
                content, question, outcomes,
                prompt_context=prompt_context,
            )

        if extract_method == "spread_cover":
            return extract_spread_cover(
                content, question, outcomes,
                prompt_context=prompt_context,
            )

        if extract_method in {"llm_generic", "count_compare"}:
            fact = extract_named_choice(
                content, question, outcomes,
                prompt_context=prompt_context,
            )

            # Convert word numbers to digits for count markets.
            # Example: "seven" → "7", so resolve.py can compare against "Over 4.5".
            if extract_method == "count_compare" and fact.value:
                word_to_num = {
                    "zero": "0",
                    "one": "1",
                    "two": "2",
                    "three": "3",
                    "four": "4",
                    "five": "5",
                    "six": "6",
                    "seven": "7",
                    "eight": "8",
                    "nine": "9",
                    "ten": "10",
                    "eleven": "11",
                    "twelve": "12",
                    "thirteen": "13",
                    "fourteen": "14",
                    "fifteen": "15",
                    "sixteen": "16",
                    "seventeen": "17",
                    "eighteen": "18",
                    "nineteen": "19",
                    "twenty": "20",
                }

                lower = str(fact.value).lower().strip()

                if lower in word_to_num:
                    fact.value = word_to_num[lower]
                    fact.fact_type = "count"
                    print(f"[extract] Word→digit: '{lower}' → '{fact.value}'")
                elif re.fullmatch(r"\d+(?:\.\d+)?", lower):
                    fact.fact_type = "count"

            return fact

    except Exception as e:
        print(f"[extract] extract_fact error ({extract_method}): {e}")

    return ExtractedFact(
        fact_type="unknown",
        confidence="low",
        reasoning=f"extract_method={extract_method} failed or not implemented",
    )