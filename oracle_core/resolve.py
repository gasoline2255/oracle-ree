#!/usr/bin/env python3
"""
oracle_core/match.py

Step 4 of the OracleREE pipeline: map an extracted fact to a valid outcome.

Rules:
  - Input: ExtractedFact + valid outcomes list
  - Output: exact outcome string from the list, or None (INCONCLUSIVE)
  - Deterministic Python first, Ollama alias resolver as last resort
  - NEVER guess — if confidence is low, return None

This is where "Manchester City" → "Man City" happens.
This is where "Liverpool" → "Liverpool Win" happens.
This is where "$2,394.99" → "$2,400" happens.
"""

from __future__ import annotations

import re
from typing import Optional

from oracle_core.extract import ExtractedFact, _call_llm


# ─── Text normalisation ───────────────────────────────────────────────────────

def _norm(text: object) -> str:
    """Canonical normalisation for team/entity name matching."""
    s = str(text or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = " ".join(s.split())
    # Strip trailing win/wins/victory
    s = re.sub(r"\s+(?:win|wins|victory)$", "", s).strip()
    # Manchester → man (common alias)
    s = re.sub(r"\bmanchester\b", "man", s)
    return s


def _words(text: str) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", _norm(text)))


def _fuzzy_team_match(candidate: str, outcome: str) -> bool:
    """True if candidate and outcome refer to the same team."""
    cn = _norm(candidate)
    on = _norm(outcome)

    if not cn or not on:
        return False

    # Exact match after normalisation
    if cn == on:
        return True

    # Containment
    if cn in on or on in cn:
        return True

    # Word overlap (ignoring noise words)
    noise = {"city", "united", "town", "county", "fc", "afc", "club",
             "hotspur", "wanderers", "rovers", "athletic", "albion",
             "the", "a", "an"}
    cw = _words(candidate) - noise
    ow = _words(outcome) - noise
    if cw and ow and cw & ow:
        return True

    return False


# ─── Price band matching ──────────────────────────────────────────────────────

def _parse_price(text: str) -> Optional[float]:
    """Parse a price value from a string."""
    text = str(text or "").replace(",", "").replace("$", "").strip()
    # Handle k/m suffixes
    m = re.match(r"([\d.]+)\s*([km]?)", text.lower())
    if not m:
        return None
    try:
        val = float(m.group(1))
        if m.group(2) == "k":
            val *= 1000
        elif m.group(2) == "m":
            val *= 1_000_000
        return val
    except ValueError:
        return None


def _outcome_price_bounds(outcome: str) -> tuple[Optional[float], Optional[float], str]:
    """Return (low, high, kind) for a price outcome band."""
    o = str(outcome or "").lower().replace(",", "")
    nums_raw = re.findall(r"\$?([\d.]+)\s*[km]?", o)
    nums = []
    for n in nums_raw:
        try:
            val = float(n)
            # Check for k/m suffix
            idx = o.find(n)
            suffix = o[idx + len(n):idx + len(n) + 2].strip()
            if suffix.startswith("k"):
                val *= 1000
            elif suffix.startswith("m"):
                val *= 1_000_000
            nums.append(val)
        except ValueError:
            continue

    if not nums:
        return None, None, "none"

    if any(k in o for k in ["under", "below", "less", "lower"]):
        return None, nums[0], "below"
    if any(k in o for k in ["over", "above", "greater", "higher", "+"]):
        return nums[0], None, "above"
    if len(nums) >= 2:
        lo, hi = min(nums[0], nums[1]), max(nums[0], nums[1])
        return lo, hi, "range"
    return nums[0], nums[0], "point"


def match_price_to_outcome(price_str: str, outcomes: list) -> Optional[str]:
    """Map a numeric price to the correct outcome band."""
    value = _parse_price(price_str)
    if value is None:
        return None

    point_candidates = []
    for outcome in outcomes or []:
        o = str(outcome).strip()
        lo, hi, kind = _outcome_price_bounds(o)

        if kind == "below" and hi is not None and value < hi:
            return o
        if kind == "above" and lo is not None and value >= lo:
            return o
        if kind == "range" and lo is not None and hi is not None:
            if lo <= value < hi:
                return o
        if kind == "point" and lo is not None:
            point_candidates.append((abs(value - lo), o))

    # Point label bands: "$2,300" means $2,300 ≤ price < $2,400.
    # Build sorted list of (numeric_value, outcome_string), then use floor matching:
    # choose the largest band value that is <= the extracted price.
    if point_candidates:
        band_values = []

        for _diff, outcome in point_candidates:
            lo, _hi, _kind = _outcome_price_bounds(outcome)
            if lo is not None:
                band_values.append((lo, outcome))

        # Sort numerically ascending, not alphabetically by outcome string.
        band_values.sort(key=lambda x: x[0])

        if band_values:
            matched_band = None

            for band_lo, band_outcome in band_values:
                if band_lo <= value:
                    matched_band = band_outcome
                else:
                    break

            if matched_band:
                print(f"[match] Price floor band: {value:.2f} → {matched_band}")
                return matched_band

    return None


# ─── Main matcher ─────────────────────────────────────────────────────────────

def match_to_outcome(
    fact: ExtractedFact,
    outcomes: list,
    question: str = "",
    extract_method: str = "",
) -> Optional[str]:
    """
    Map an ExtractedFact to an exact valid outcome string.

    Returns None if:
    - fact.confidence is low
    - no match found after all attempts
    - fact.value is empty
    """
    if not fact or not fact.value or fact.confidence == "low":
        return None

    value = str(fact.value).strip()
    if not value:
        return None

    # ── Price matching ────────────────────────────────────────────────────────
    if fact.fact_type == "price" or extract_method in {"price_band", "price_threshold"}:
        # Threshold Yes/No: compare extracted price against threshold in question.
        # Example: value_usd=79321.16 and outcomes=["Yes", "No"].
        if extract_method == "price_threshold":
            try:
                price_value = float(str(value).replace(",", "").replace("$", "").strip())
                q = str(question or "").lower()

                thresh_m = re.search(
                    r"\$?([\d,]+(?:\.\d+)?)\s*([km]?)\b",
                    q,
                )

                if thresh_m:
                    threshold = float(thresh_m.group(1).replace(",", ""))

                    suffix = thresh_m.group(2).lower()
                    if suffix == "k":
                        threshold *= 1_000
                    elif suffix == "m":
                        threshold *= 1_000_000

                    is_above = any(
                        k in q
                        for k in ["above", "over", "higher", "exceed", "more than", "greater than", "at least"]
                    )
                    is_below = any(
                        k in q
                        for k in ["below", "under", "lower", "less than", "beneath"]
                    )

                    if is_above or not is_below:
                        condition_met = price_value >= threshold
                        comparator = "≥"
                    else:
                        condition_met = price_value < threshold
                        comparator = "<"

                    target = "Yes" if condition_met else "No"

                    for o in outcomes or []:
                        o_norm = str(o).strip().lower()
                        if condition_met and o_norm in {"above", "above "}:
                            print(f"[match] Threshold Above: ${price_value:,.2f} {comparator} ${threshold:,.2f} → {o}")
                            return str(o).strip()
                        if not condition_met and o_norm in {"below", "below "}:
                            print(f"[match] Threshold Below: ${price_value:,.2f} {comparator} ${threshold:,.2f} → {o}")
                            return str(o).strip()
                        if o_norm == target.lower():
                            print(
                                f"[match] Threshold: ${price_value:,.2f} "
                                f"{comparator} ${threshold:,.2f} → {o}"
                            )
                            return str(o).strip()
            except Exception:
                pass

        matched = match_price_to_outcome(value, outcomes)
        if matched:
            print(f"[match] Price {value} → {matched}")
        return matched

    # ── Count / threshold matching ───────────────────────────────────────────
    # Example: extracted "7" vs outcomes ["Over 4.5", "Under 4.5"].
    if extract_method in {"count_compare"} or fact.fact_type in {"count"}:
        try:
            count_val = float(str(fact.value).replace(",", "").strip())

            for o in outcomes or []:
                o_str = str(o).strip()
                o_lower = o_str.lower()

                thresh_m = re.search(r"(\d+(?:\.\d+)?)", o_str)
                if not thresh_m:
                    continue

                thresh = float(thresh_m.group(1))

                if any(k in o_lower for k in ["over", "above", "more"]) and count_val > thresh:
                    print(f"[match] Count {count_val} > {thresh} → {o_str}")
                    return o_str

                if any(k in o_lower for k in ["under", "below", "less"]) and count_val < thresh:
                    print(f"[match] Count {count_val} < {thresh} → {o_str}")
                    return o_str

        except Exception:
            pass

    # ── Draw ─────────────────────────────────────────────────────────────────
    if value.lower() in {"draw", "tie", "tied", "drew"}:
        for o in outcomes or []:
            if str(o).strip().lower() == "draw":
                print(f"[match] Draw → {o}")
                return str(o).strip()
        return None

    # ── Yes/No confirmation ──────────────────────────────────────────────────
    if value in {"Yes", "No"} and fact.fact_type == "confirmation":
        for o in outcomes or []:
            if str(o).strip().lower() == value.lower():
                print(f"[match] Confirmation {value} → {o}")
                return str(o).strip()
        return None

    # ── Team/entity name matching ─────────────────────────────────────────────
    # Pass 1: exact normalised match
    for o in outcomes or []:
        os_ = str(o).strip()
        if os_.lower() in {"yes", "no", "draw"}:
            continue
        if _norm(os_) == _norm(value):
            print(f"[match] Exact norm: {value} → {o}")
            return os_

    # Pass 2: fuzzy team match
    # Skip fuzzy match for date-based outcomes (before/after)
    has_date_outcomes = any(
        any(k in str(o).lower() for k in ["or before", "or after", "or later", "or earlier"])
        for o in (outcomes or [])
    )
    if not has_date_outcomes:
        for o in outcomes or []:
            os_ = str(o).strip()
            if os_.lower() in {"yes", "no", "draw"}:
                continue
            if _fuzzy_team_match(value, os_):
                print(f"[match] Fuzzy: {value} → {o}")
                return os_

    # Pass 2.5: Before/after date outcome matching
    # Example: "after May 27, 2026" → "May 27th or after"
    value_lower = value.lower()
    for o in outcomes or []:
        os_ = str(o).strip()
        os_lower = os_.lower()
        if any(k in os_lower for k in ["or after", "or later", "after"]) and any(k in value_lower for k in ["after", "or later", "or after"]):
            print(f"[match] Date after: {value} → {os_}")
            return os_
        if any(k in os_lower for k in ["or before", "or earlier", "before"]) and any(k in value_lower for k in ["before", "or earlier", "or before"]):
            print(f"[match] Date before: {value} → {os_}")
            return os_

    # Pass 2.5: Before/after date outcome matching
    # Example: "after May 27, 2026" → "May 27th or after"
    value_lower = value.lower()
    for o in outcomes or []:
        os_ = str(o).strip()
        os_lower = os_.lower()
        if any(k in os_lower for k in ["or after", "or later", "after"]) and any(k in value_lower for k in ["after", "or later", "or after"]):
            print(f"[match] Date after: {value} → {os_}")
            return os_
        if any(k in os_lower for k in ["or before", "or earlier", "before"]) and any(k in value_lower for k in ["before", "or earlier", "or before"]):
            print(f"[match] Date before: {value} → {os_}")
            return os_

    # Pass 3: Ollama alias resolver (last resort)
    # Only for medium+ confidence facts
    if fact.confidence in {"high", "medium"}:
        matched = _llm_alias_match(value, outcomes, question)
        if matched:
            print(f"[match] LLM alias: {value} → {matched}")
            return matched

    print(f"[match] No match found for: {value}")
    return None


def _llm_alias_match(
    extracted: str, outcomes: list, question: str = ""
) -> Optional[str]:
    """
    Use Ollama to map an extracted name to a valid outcome.
    Only called when deterministic matching fails.
    Ollama's job: understand aliases only. Not decide who won.
    """
    import json as _json
    valid = [str(o).strip() for o in outcomes or [] if str(o).strip()]
    if not valid:
        return None

    prompt = f"""You are mapping an extracted name to one valid outcome.

Extracted name: {extracted}
Valid outcomes: {_json.dumps(valid)}
Market question: {question}

Rules:
- Match common aliases: "Manchester City" = "Man City", "Liverpool" = "Liverpool Win"
- Only match if clearly the same entity
- Return null if unsure

Return JSON: {{"matched_outcome": null, "confidence": "low"}}"""

    result = _call_llm(prompt, max_tokens=100)
    if not result or not isinstance(result, dict):
        return None

    matched = str(result.get("matched_outcome") or "").strip()
    confidence = str(result.get("confidence") or "low").lower()

    if matched in valid and confidence in {"high", "medium"}:
        return matched
    return None