#!/usr/bin/env python3
"""
OracleREE Ollama Evidence Brain

Important rule:
- Ollama helps understand and validate evidence.
- Ollama does NOT own final settlement.
- Python resolvers must make the final deterministic decision.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional
from pathlib import Path

import requests

# Load .env.local when this module is used directly or imported.
_ENV_FILE = Path(__file__).parent / ".env.local"
if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b-instruct")
USE_OLLAMA_BRAIN = os.environ.get("USE_OLLAMA_BRAIN", "1").strip().lower() not in {"0", "false", "no", "off"}


def extract_json_object(text: str) -> dict:
    """Parse the first JSON object from a model response safely."""
    text = (text or "").strip()
    if not text:
        return {}

    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass

    clean = text.replace("```json", "").replace("```", "").strip()
    match = re.search(r"\{[\s\S]*\}", clean)
    if not match:
        return {}

    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def ask_ollama_json(prompt: str, timeout: int = 120, num_predict: int = 900) -> dict:
    """
    Ask Ollama for JSON.

    This wrapper is defensive:
    - respects USE_OLLAMA_BRAIN=0
    - temperature 0 for stable outputs
    - extracts JSON even if the model adds extra text
    - returns a safe INCONCLUSIVE-style object on failure
    """
    if not USE_OLLAMA_BRAIN:
        return {
            "error": "ollama_disabled",
            "evidence_sufficient": False,
            "must_return_inconclusive": True,
            "reason": "Ollama disabled by USE_OLLAMA_BRAIN",
        }

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "prompt": prompt,
        "options": {
            "temperature": 0,
            "num_predict": num_predict,
        },
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        response.raise_for_status()
        raw = response.json().get("response", "{}")
        parsed = extract_json_object(raw)
        if parsed:
            return parsed
        return {
            "error": "empty_or_invalid_json",
            "evidence_sufficient": False,
            "must_return_inconclusive": True,
            "reason": "Ollama returned no valid JSON object",
        }
    except Exception as exc:
        return {
            "error": str(exc),
            "evidence_sufficient": False,
            "must_return_inconclusive": True,
            "reason": "Ollama JSON call failed",
        }


def is_spread_market(outcomes: list[str]) -> bool:
    """True for outcomes like 'Kings +10.5' and 'Defenders -10.5'."""
    if not outcomes or len(outcomes) != 2:
        return False
    spread_re = re.compile(r"^.+?\s+[+-]\d+(?:\.\d+)?\s*$")
    return all(spread_re.match(str(outcome or "").strip()) for outcome in outcomes)


def spread_team_names(outcomes: list[str]) -> list[str]:
    teams: list[str] = []
    for outcome in outcomes or []:
        team = re.sub(r"\s+[+-]\d+(?:\.\d+)?\s*$", "", str(outcome or "").strip()).strip()
        if team:
            teams.append(team)
    return teams


def deterministic_spread_evidence_gate(question: str, outcomes: list[str], evidence: str) -> dict:
    """
    Minimum-evidence gate for spread markets.

    A spread market cannot settle unless evidence contains:
    - no "not available" / missing-score language
    - both teams
    - an explicit final-score signal
    """
    evidence_text = str(evidence or "")
    evidence_l = evidence_text.lower()

    result = {
        "market_type": "sports_spread" if is_spread_market(outcomes) else "unknown",
        "has_final_score": False,
        "final_score": None,
        "has_both_teams": False,
        "has_unavailable_language": False,
        "covered_team": None,
        "matched_outcome": None,
        "evidence_sufficient": False,
        "must_return_inconclusive": True,
        "reason": "",
    }

    if not is_spread_market(outcomes):
        result["reason"] = "Not a sports spread market"
        return result

    unavailable_phrases = [
        "spread is not available",
        "final score is not available",
        "score is not available",
        "odds are not available",
        "not available",
        "has not been released",
        "not yet available",
        "closer to game day",
        "to be announced",
        "no final score",
        "no data available",
    ]

    if any(phrase in evidence_l for phrase in unavailable_phrases):
        result["has_unavailable_language"] = True
        result["reason"] = "Evidence says spread/final score is not available"
        return result

    teams = spread_team_names(outcomes)
    result["has_both_teams"] = all(
        re.search(rf"\b{re.escape(team.lower())}\b", evidence_l)
        for team in teams
    )

    score_present = bool(
        re.search(r"\b\d{1,3}\s*[-–]\s*\d{1,3}\b", evidence_text)
        or re.search(r"\bfinal(?:\s+score)?\b.{0,80}\b\d{1,3}\b.{0,40}\b\d{1,3}\b", evidence_text, re.I | re.S)
        or re.search(r"\|\s*[^|\n]{2,30}\s*\|\s*(?:\d+\s*\|){2,}\s*\d+\s*\|", evidence_text)
    )
    result["has_final_score"] = score_present

    if not result["has_both_teams"]:
        result["reason"] = "Evidence does not mention both spread teams; possible unrelated/contaminated result"
        return result

    if not score_present:
        result["reason"] = "No explicit final score found; spread cover cannot be calculated"
        return result

    result["evidence_sufficient"] = True
    result["must_return_inconclusive"] = False
    result["reason"] = "Evidence passes minimum spread requirements"
    return result


def validate_spread_evidence(question: str, outcomes: list[str], evidence: str) -> dict:
    """
    Use Ollama as an explanation layer, but deterministic gate wins.

    If deterministic gate says evidence is insufficient, final result must be INCONCLUSIVE.
    """
    gate = deterministic_spread_evidence_gate(question, outcomes, evidence)
    if not is_spread_market(outcomes):
        return gate

    prompt = f"""
You are OracleREE EvidenceBrain.

Return valid JSON only. Do not guess. Do not settle the market unless evidence explicitly supports it.

Market type: sports_spread

Question:
{question}

Valid outcomes:
{json.dumps(outcomes, ensure_ascii=False)}

Evidence:
{str(evidence or '')[:5000]}

Rules:
- A spread market requires explicit final score evidence with both teams.
- If evidence says spread/final score is unavailable, evidence_sufficient=false.
- Do not infer from odds pages, unrelated games, same-name teams, or generic scoreboard pages.
- If no final score exists, must_return_inconclusive=true.
- Never output a spread outcome based only on "not available" text.

Return JSON:
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
    model_report = ask_ollama_json(prompt)

    if gate.get("must_return_inconclusive"):
        model_report.update(gate)
        model_report["matched_outcome"] = None
        return model_report

    model_report["market_type"] = "sports_spread"
    model_report["has_final_score"] = True
    model_report["has_both_teams"] = True
    model_report["has_unavailable_language"] = False
    model_report["evidence_sufficient"] = True
    model_report["must_return_inconclusive"] = False
    model_report.setdefault("reason", gate.get("reason", "Evidence passes deterministic spread gate"))
    return model_report


if __name__ == "__main__":
    print("OracleREE Ollama Evidence Brain loaded successfully.")
