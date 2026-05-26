#!/usr/bin/env python3
"""
oracle_core/classify.py

Step 1 of the OracleREE pipeline: understand what the market is asking.

Rules:
  - Pure Python only. No LLM. No fetch. No network calls.
  - Input: question, outcomes, prompt_context, rules dict
  - Output: MarketClass dataclass with category, fetch_strategy, extract_method

Design principle:
  A market that is misclassified will produce a wrong answer no matter how
  good the downstream pipeline is. Get this right first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MarketClass:
    """
    Complete classification of a Delphi market.
    Everything downstream uses this to decide how to fetch and extract.
    """
    # Primary category
    category: str = ""
    # sport | price_daily | price_monthly | price_threshold |
    # confirmation_yes_no | named_choice | spread | count | generic

    # What to fetch
    fetch_strategy: str = ""
    # espn_scoreboard | coingecko_daily | coingecko_monthly |
    # coingecko_threshold | tavily_broad | tavily_creator | direct_api | none

    # How to extract the answer
    extract_method: str = ""
    # score_winner | price_band | price_threshold | llm_confirmation |
    # llm_named_choice | spread_cover | count_compare | llm_generic

    # Asset details (for price markets)
    asset: str = ""           # BTC, ETH, SOL, etc.
    asset_coin_id: str = ""   # coingecko coin id

    # Date window
    event_date: str = ""      # YYYY-MM-DD
    window_start: str = ""    # YYYY-MM-DD
    window_end: str = ""      # YYYY-MM-DD
    is_single_day: bool = False

    # Sport details
    sport: str = ""           # football | cricket | basketball | american_football | etc
    team1: str = ""
    team2: str = ""

    # Threshold (for price/count markets)
    threshold: Optional[float] = None
    threshold_operator: str = ""  # > | < | >= | <=

    # Confirmation details
    confirmation_entity: str = ""  # MicroStrategy, Strategy, etc.
    confirmation_action: str = ""  # bitcoin purchase, announcement, etc.

    # Outcome metadata
    outcomes: list = field(default_factory=list)
    has_draw: bool = False
    has_win_suffix: bool = False  # True if outcomes say "Liverpool Win" style
    is_spread: bool = False

    # Confidence in classification
    confidence: str = "high"  # high | medium | low
    classification_reason: str = ""


# ─── Asset detection ─────────────────────────────────────────────────────────

COINGECKO_IDS = {
    "BTC": "bitcoin", "BITCOIN": "bitcoin",
    "ETH": "ethereum", "ETHEREUM": "ethereum",
    "SOL": "solana", "SOLANA": "solana",
    "BNB": "binancecoin",
    "MATIC": "matic-network", "POL": "matic-network",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "ADA": "cardano",
    "LINK": "chainlink",
    "UNI": "uniswap",
    "DOGE": "dogecoin",
    "XRP": "ripple",
    "LTC": "litecoin",
    "ATOM": "cosmos",
    "AI": "gensyn",  # $AI token
}

def _detect_asset(question: str, prompt_context: str = "") -> tuple[str, str]:
    """Return (asset_symbol, coingecko_id) or ('', '')."""
    text = (str(question or "") + " " + str(prompt_context or "")).upper()
    for symbol, coin_id in COINGECKO_IDS.items():
        pattern = rf"\b{re.escape(symbol)}\b"
        if re.search(pattern, text):
            return symbol, coin_id
    # Common names
    text_l = text.lower()
    if "bitcoin" in text_l:
        return "BTC", "bitcoin"
    if "ethereum" in text_l:
        return "ETH", "ethereum"
    if "solana" in text_l:
        return "SOL", "solana"
    return "", ""


# ─── Outcome analysis ────────────────────────────────────────────────────────

def _analyse_outcomes(outcomes: list) -> dict:
    """Extract structural information from the outcomes list."""
    result = {
        "has_draw": False,
        "has_yes_no": False,
        "has_win_suffix": False,
        "is_spread": False,
        "is_price_band": False,
        "is_count_threshold": False,
        "count": len(outcomes),
        "non_draw": [],
    }
    if not outcomes:
        return result

    spread_re = re.compile(r"^.+?\s+[+-]\d+(?:\.\d+)?\s*$")
    price_re = re.compile(r"[\$\d][\d,k.]+[kmb]?\+?$|below|above|over|under", re.I)
    count_re = re.compile(r"^(?:over|under|above|below)\s+\d+(?:\.\d+)?$", re.I)

    for o in outcomes:
        s = str(o or "").strip().lower()
        if s == "draw":
            result["has_draw"] = True
        if s in {"yes", "no"}:
            result["has_yes_no"] = True
        if re.search(r"\s+(?:win|wins|victory)$", s, re.I):
            result["has_win_suffix"] = True
        if spread_re.match(str(o or "").strip()):
            result["is_spread"] = True
        if price_re.search(s) or re.search(r"\$\d", s):
            result["is_price_band"] = True
        if count_re.match(s):
            result["is_count_threshold"] = True
        if str(o or "").strip().lower() not in {"draw", "yes", "no"}:
            result["non_draw"].append(str(o).strip())

    # Yes/No with only 2 outcomes
    outcome_lower = {str(o).strip().lower() for o in outcomes}
    if outcome_lower == {"yes", "no"}:
        result["has_yes_no"] = True

    return result


# ─── Date/window detection ───────────────────────────────────────────────────

def _detect_date_window(question: str, event_date: str, close_time: str,
                         rules: dict) -> tuple[str, str, str, bool]:
    """
    Returns (event_date, window_start, window_end, is_single_day).
    """
    # Rules time_window takes priority
    tw = rules.get("time_window") if isinstance(rules.get("time_window"), dict) else {}
    if tw.get("start") and tw.get("end"):
        start = str(tw["start"])[:10]
        end = str(tw["end"])[:10]
        is_single = start == end
        return event_date or start, start, end, is_single

    q = str(question or "").lower()

    # Month map
    months = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
    }

    # Get base year from event_date or close_time
    base_year = ""
    for candidate in [event_date, close_time]:
        if candidate and re.match(r"\d{4}", str(candidate)):
            base_year = str(candidate)[:4]
            break
    if not base_year:
        base_year = "2026"

    for month_name, month_num in months.items():
        if month_name in q:
            # Detect "April 21-27" or "April 21 to 27" date range
            range_m = re.search(
                month_name +
                r"\s+(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|\sto\s)\s*(\d{1,2})(?:st|nd|rd|th)?",
                q,
                re.I,
            )
            if range_m:
                d1 = int(range_m.group(1))
                d2 = int(range_m.group(2))
                start = f"{base_year}-{month_num}-{d1:02d}"
                end = f"{base_year}-{month_num}-{d2:02d}"
                return event_date or start, start, end, False

            # Check for specific day: "on April 27" or "April 27,"
            day_m = re.search(
                month_name + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,|\s+\d{4}|\s*$|\s*\?|\s*\()",
                q
            )
            if day_m:
                day = int(day_m.group(1))
                day_str = f"{base_year}-{month_num}-{day:02d}"
                return day_str, day_str, day_str, True

            # Full month range
            import calendar
            last = calendar.monthrange(int(base_year), int(month_num))[1]
            start = f"{base_year}-{month_num}-01"
            end = f"{base_year}-{month_num}-{last:02d}"
            return event_date or start, start, end, False

    # Use event_date as single day
    if event_date:
        return event_date, event_date, event_date, True

    return "", "", "", False


# ─── Sport detection ─────────────────────────────────────────────────────────

SPORT_KEYWORDS = {
    "football": ["premier league", "fa cup", "champions league", "la liga",
                 "serie a", "bundesliga", "ligue 1", "mls", "eredivisie",
                 "championship", "sky bet", "english football", "soccer",
                 "primera division"],
    "cricket":  ["cricket", "ipl", "psl", "t20", "odi", "test match",
                 "bbl", "cpl", "the hundred", "big bash", "super kings",
                 "knight riders", "royals", "titans", "capitals",
                 "gladiators", "sunrisers", "mumbai indians",
                 "indian premier league", "pakistan super league",
                 "big bash league", "caribbean premier league",
                 "sa20", "ilt20", "lanka premier league"],
    "basketball": ["nba", "basketball", "wnba", "ncaa basketball"],
    "american_football": ["nfl", "ufl", "xfl", "super bowl", "american football",
                          "college football", "ncaa football"],
    "baseball": ["mlb", "baseball", "world series"],
    "hockey":   ["nhl", "hockey", "stanley cup"],
    "tennis":   ["tennis", "wimbledon", "us open", "french open",
                 "australian open", "atp", "wta"],
    "golf":     ["golf", "pga", "masters", "open championship"],
    "boxing_mma": ["boxing", "ufc", "mma", "fight", "bout"],
    "rugby":    ["rugby", "six nations", "premiership rugby"],
    "esports":  ["esports", "league of legends", "dota", "cs:go", "valorant"],
}

def _detect_sport(question: str, prompt_context: str = "") -> str:
    text = (str(question or "") + " " + str(prompt_context or "")).lower()

    # ── League name detection (highest priority) ──────────────────────────
    # This is more scalable than hardcoding every cricket team/franchise name.
    # Most cricket markets include the league/competition in the title or prompt.
    cricket_leagues = [
        "indian premier league", "ipl",
        "pakistan super league", "psl",
        "big bash league", "bbl",
        "caribbean premier league", "cpl",
        "sa20", "the hundred", "ilt20",
        "lanka premier league", "lpl",
        "t20 world cup", "icc",
    ]
    if any(k in text for k in cricket_leagues):
        return "cricket"

    # ── Keyword detection ─────────────────────────────────────────────────
    for sport, keywords in SPORT_KEYWORDS.items():
        if any(k in text for k in keywords):
            return sport

    if " vs " in text or " versus " in text or " v " in text:
        return "football"  # default for vs. markets
    return ""


def _extract_teams(question: str, outcomes: list) -> tuple[str, str]:
    """Extract two team names from question or outcomes."""
    q = str(question or "")
    m = re.search(
        r"(.+?)\s+(?:vs\.?|v\.?|versus)\s+(.+?)(?:\s*[—–-]\s*|\s*\(|,|$)",
        q, re.I
    )
    if m:
        noise = r"\b(will|who|does|did|the|official|final|result|winner|match|game)\b"
        t1 = re.sub(noise, " ", m.group(1), flags=re.I)
        t2 = re.sub(noise, " ", m.group(2), flags=re.I)
        t2 = re.split(r"\s*[—–-]\s*|\s*\(|,", t2, maxsplit=1)[0]
        t1 = " ".join(t1.split()).strip()
        t2 = " ".join(t2.split()).strip()
        if t1 and t2:
            return t1, t2

    non_draw = [
        str(o).strip() for o in outcomes or []
        if str(o).strip() and str(o).strip().lower() not in {"draw", "yes", "no"}
        and not re.search(r"^[+-]?\d", str(o))
        and not re.search(r"\s+(?:win|wins|victory)$", str(o), re.I)
    ]
    if len(non_draw) >= 2:
        return non_draw[0], non_draw[1]
    return "", ""


# ─── Main classifier ──────────────────────────────────────────────────────────

def classify_market(
    question: str,
    outcomes: list,
    prompt_context: str = "",
    rules: dict = None,
    event_date: str = "",
    close_time: str = "",
) -> MarketClass:
    """
    Classify a Delphi market into a category with fetch/extract strategy.

    This is deterministic — no LLM, no network calls.
    If unsure, default to 'generic' with llm_generic extract method.
    """
    rules = rules or {}
    mc = MarketClass()
    mc.outcomes = list(outcomes or [])

    q = str(question or "").lower()
    text = q + " " + str(prompt_context or "").lower()

    # Analyse outcomes structure
    oa = _analyse_outcomes(outcomes)
    mc.has_draw = oa["has_draw"]
    mc.has_win_suffix = oa["has_win_suffix"]
    mc.is_spread = oa["is_spread"]

    # Detect date window
    ev_date, w_start, w_end, is_single = _detect_date_window(
        question, event_date, close_time, rules
    )
    mc.event_date = ev_date
    mc.window_start = w_start
    mc.window_end = w_end
    mc.is_single_day = is_single

    # ── SPREAD MARKETS (check before sports) ────────────────────────────────
    if oa["is_spread"] and len(outcomes) == 2:
        mc.category = "spread"
        mc.fetch_strategy = "espn_scoreboard"
        mc.extract_method = "spread_cover"
        mc.is_spread = True
        mc.team1, mc.team2 = _extract_teams(question, outcomes)
        mc.sport = _detect_sport(question, prompt_context) or "american_football"
        mc.classification_reason = "spread market (X +/-N.N outcomes)"
        return mc

    # ── SPORTS ───────────────────────────────────────────────────────────────
    sport = _detect_sport(question, prompt_context)
    if sport and (" vs " in q or " versus " in q or " v " in q or
                  any(k in q for k in ["match", "game", "fixture"])):
        mc.category = "sports"
        mc.sport = sport
        mc.team1, mc.team2 = _extract_teams(question, outcomes)
        mc.fetch_strategy = "espn_scoreboard"
        mc.extract_method = "score_winner"
        mc.classification_reason = f"vs. pattern + sport keyword ({sport})"

        # Cricket T20: no draws, Super Over decides ties
        if sport == "cricket" and not oa["has_draw"]:
            mc.fetch_strategy = "thesportsdb_then_espn"
            mc.classification_reason += " | cricket T20 no draw"

        return mc

    # ── PRICE MARKETS ────────────────────────────────────────────────────────
    asset, coin_id = _detect_asset(question, prompt_context)
    if asset and (oa["is_price_band"] or
                  any(k in q for k in ["price", "highest", "lowest", "how high",
                                       "reach", "hit", "above", "below", "close above",
                                       "close below", "open above", "open below"])):
        mc.asset = asset
        mc.asset_coin_id = coin_id

        # Yes/No threshold: "will BTC be above $80k"
        if oa["has_yes_no"] or len(outcomes) == 2:
            mc.category = "crypto_threshold"
            mc.fetch_strategy = "coingecko_threshold"
            mc.extract_method = "price_threshold"
            # Extract threshold value
            thresh_m = re.search(r"\$?([\d,]+(?:\.\d+)?)\s*(?:k|m)?", q)
            if thresh_m:
                val_str = thresh_m.group(1).replace(",", "")
                try:
                    mc.threshold = float(val_str)
                    if "k" in q[thresh_m.end():thresh_m.end()+2].lower():
                        mc.threshold *= 1000
                except ValueError:
                    pass
            mc.threshold_operator = ">" if any(
                k in q for k in ["above", "higher", "over", "exceed"]
            ) else "<"
            mc.classification_reason = f"crypto threshold market ({asset})"
            return mc

        # Price band: "highest price on April 27"
        if is_single:
            mc.category = "crypto_price_daily"
            mc.fetch_strategy = "coingecko_daily"
            mc.extract_method = "price_band"
        else:
            mc.category = "crypto_price_monthly"
            mc.fetch_strategy = "coingecko_monthly"
            mc.extract_method = "price_band"

        mc.classification_reason = f"crypto price band market ({asset}, single_day={is_single})"
        return mc

    # ── S&P 500 / INDEX MARKETS ──────────────────────────────────────────────
    if any(k in q for k in ["s&p", "sp500", "s&p 500", "s&p500", "nasdaq",
                              "dow jones", "ftse", "index"]):
        mc.category = "index_threshold"
        mc.fetch_strategy = "yahoo_finance"
        mc.extract_method = "price_threshold"
        mc.threshold_operator = ">" if any(
            k in q for k in ["above", "higher", "over", "green"]
        ) else "<"
        mc.classification_reason = "stock index market"
        return mc

    # ── CONFIRMATION YES/NO ──────────────────────────────────────────────────
    if oa["has_yes_no"]:
        mc.category = "confirmation_yes_no"
        mc.fetch_strategy = "tavily_broad"
        mc.extract_method = "llm_confirmation"

        # Detect entity and action
        if any(k in q for k in ["microstrategy", "strategy", "mstr"]):
            mc.confirmation_entity = "Strategy/MicroStrategy"
            mc.confirmation_action = "bitcoin purchase"
            mc.fetch_strategy = "tavily_broad_confirmation"
        elif any(k in q for k in ["etf", "approval", "launch", "listing"]):
            mc.confirmation_entity = _extract_entity(question)
            mc.confirmation_action = "approval/launch"

        mc.classification_reason = "yes/no confirmation market"
        return mc

    # ── COUNT MARKETS ────────────────────────────────────────────────────────
    if oa["is_count_threshold"] or any(
        k in q for k in ["how many", "number of", "total", "count"]
    ):
        mc.category = "count"
        mc.fetch_strategy = "tavily_broad"
        mc.extract_method = "count_compare"
        mc.classification_reason = "count/over-under market"
        return mc

    # ── NAMED CHOICE (Eurovision, NFL Draft, etc.) ───────────────────────────
    # Many outcomes, named entities, not a sport
    if len(outcomes) > 4 and not oa["is_price_band"] and not oa["has_yes_no"]:
        mc.category = "named_choice"
        mc.fetch_strategy = "tavily_broad"
        mc.extract_method = "llm_named_choice"
        mc.classification_reason = f"named choice market ({len(outcomes)} outcomes)"
        return mc

    # ── GENERIC FALLBACK ─────────────────────────────────────────────────────
    mc.category = "generic"
    mc.fetch_strategy = "tavily_broad"
    mc.extract_method = "llm_generic"
    mc.confidence = "low"
    mc.classification_reason = "no specific pattern matched"
    return mc


def _extract_entity(question: str) -> str:
    """Best-effort entity extraction from question."""
    # Capitalised words that aren't common question words
    words = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b", str(question or ""))
    skip = {"Will", "The", "What", "When", "How", "Who", "Which", "Is", "Are",
            "Does", "Did", "Has", "Have", "Can", "Could", "Should", "Would"}
    entities = [w for w in words if w not in skip]
    return entities[0] if entities else ""