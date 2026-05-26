#!/usr/bin/env python3
"""
oracle_core/fetch_source.py

Phase 1 extraction: all fetch + validation logic in one place.

Design rules:
  - fetch_source() ALWAYS validates before returning.
  - Nothing invalid ever leaves this module.
  - SPA shells, homepages, topic-drift pages → is_valid=False.
  - Head/meta/title/script stripped BEFORE any content is returned.
  - Sports: structured APIs run FIRST (TheSportsDB, football-data.org, ESPN Cricket)
  - All sports get team alias expansion for Tavily queries ("Man City" → "Manchester City")
  - All sports get sport-appropriate fallback domains
  - Company/crypto events: body-only validation, date window required
  - INCONCLUSIVE is the correct return when no valid content is found

Sports coverage:
  Football/Soccer  — TheSportsDB, football-data.org, BBC/Sky/Sofascore/Fotmob
  Basketball (NBA) — ESPN API, Basketball Reference, NBA.com
  American Football (NFL/CFB) — ESPN API, Pro Football Reference
  Baseball (MLB)   — ESPN API, Baseball Reference
  Hockey (NHL)     — ESPN API, Hockey Reference
  Cricket (IPL/PSL/Intl) — ESPNcricinfo API, Cricbuzz
  Tennis           — ATP/WTA Tavily fallback
  Golf             — ESPN, PGA Tour Tavily fallback
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional
import requests


# ─── Env helper ──────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ─── Data class ──────────────────────────────────────────────────────────────

@dataclass
class FetchResult:
    """
    Single return type from fetch_source().
    is_valid=False → caller must return INCONCLUSIVE.
    content is ALWAYS stripped of <head>/<script>/<style> before being set.
    """
    content: str = ""
    source: str = ""           # domain or API name that provided the data
    method: str = ""           # direct | tavily | thesportsdb | football_data | espncricinfo | ...
    is_valid: bool = False
    validation_reason: str = ""
    raw_html: str = ""         # original HTML before stripping, for debugging only
    matched_outcome: str = ""  # pre-extracted outcome if available (skips oracle_ree extraction)
    extracted_facts: list = None  # structured facts extracted from content


# ─── HTML stripping ───────────────────────────────────────────────────────────

def strip_to_body_text(raw: str) -> str:
    """
    Remove <head>, <script>, <style>, <nav>, <footer>, <meta>, <title>.
    Returns visible body text only.
    This is THE canonical stripper — all validation must use this output.
    """
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
    for entity, replacement in {
        "&nbsp;": " ", "&#160;": " ", "&amp;": "&",
        "&quot;": '"', "&#34;": '"', "&#39;": "'", "&apos;": "'",
    }.items():
        s = s.replace(entity, replacement)
    return " ".join(s.split())


# ─── Team / participant name handling ─────────────────────────────────────────

# Common shorthand → full name aliases for all major sports
# Used to expand Tavily queries so "Man City" finds "Manchester City" pages
_TEAM_ALIASES: dict[str, str] = {
    # English football
    "man city": "Manchester City",
    "man utd": "Manchester United",
    "man united": "Manchester United",
    "spurs": "Tottenham Hotspur",
    "wolves": "Wolverhampton Wanderers",
    "brighton": "Brighton & Hove Albion",
    "west ham": "West Ham United",
    "newcastle": "Newcastle United",
    "forest": "Nottingham Forest",
    "villa": "Aston Villa",
    "palace": "Crystal Palace",
    "brentford": "Brentford",
    "luton": "Luton Town",
    "sheff utd": "Sheffield United",
    "sheffield utd": "Sheffield United",
    # US sports
    "la lakers": "Los Angeles Lakers",
    "la clippers": "Los Angeles Clippers",
    "la rams": "Los Angeles Rams",
    "la chargers": "Los Angeles Chargers",
    "ny knicks": "New York Knicks",
    "ny giants": "New York Giants",
    "ny jets": "New York Jets",
    "ny yankees": "New York Yankees",
    "ny mets": "New York Mets",
    "golden state": "Golden State Warriors",
    "gsw": "Golden State Warriors",
    "okc": "Oklahoma City Thunder",
    "sf giants": "San Francisco Giants",
    "sf 49ers": "San Francisco 49ers",
    # Cricket
    "mi": "Mumbai Indians",
    "csk": "Chennai Super Kings",
    "rcb": "Royal Challengers Bengaluru",
    "kkr": "Kolkata Knight Riders",
    "srh": "Sunrisers Hyderabad",
    "dc": "Delhi Capitals",
    "pbks": "Punjab Kings",
    "rr": "Rajasthan Royals",
    "gt": "Gujarat Titans",
    "lsg": "Lucknow Super Giants",
}

def _expand_alias(name: str) -> str:
    """Return full name if a known alias, else return unchanged."""
    return _TEAM_ALIASES.get(name.lower().strip(), name)

def _tavily_team_token(name: str) -> str:
    """
    Build a Tavily search token for a team name.
    If we have a known alias, include both forms: "Man City" OR "Manchester City"
    so Tavily finds pages that use either form.
    """
    name = name.strip()
    expanded = _expand_alias(name)
    if expanded.lower() != name.lower():
        return f'("{name}" OR "{expanded}")'
    return f'"{name}"'

def _norm_token(text: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())

def _words(text: object) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", str(text or "").lower()))

def _team_matches(expected: str, actual: str) -> bool:
    """True if expected team name matches actual (handles aliases, partial names)."""
    # Try with alias expansion
    exp_expanded = _expand_alias(expected)
    act_expanded = _expand_alias(actual)

    for exp in {expected, exp_expanded}:
        for act in {actual, act_expanded}:
            e, a = _norm_token(exp), _norm_token(act)
            if not e or not a:
                continue
            if e in a or a in e:
                return True
    # Word-level overlap ignoring noise words
    stop = {"club", "football", "fc", "afc", "the", "united", "city", "town",
            "hotspur", "wanderers", "rovers", "athletic", "albion"}
    exp_w = _words(exp_expanded) - stop
    act_w = _words(act_expanded) - stop
    if exp_w and act_w and exp_w & act_w:
        return True
    return False

def _teams_from_question(question: str, outcomes: list) -> tuple[str, str]:
    """Extract two team/participant names from question text or outcomes."""
    q = str(question or "")
    m = re.search(r"(.+?)\s+(?:vs\.?|v\.?|versus)\s+(.+?)(?:\s+[—–-]|\s*\(|,|$)", q, re.I)
    if m:
        noise = r"\b(will|who|does|did|the|official|final|result)\b"
        t1 = re.sub(noise, " ", m.group(1), flags=re.I)
        t2 = re.sub(noise, " ", m.group(2), flags=re.I)
        return " ".join(t1.split()).strip(), " ".join(t2.split()).strip()
    non_draw = [
        str(o).strip() for o in outcomes or []
        if str(o).strip() and str(o).strip().lower() not in {"draw", "yes", "no"}
        and not re.search(r"^[+-]?\d", str(o))
    ]
    if len(non_draw) >= 2:
        return non_draw[0], non_draw[1]
    return "", ""

def _match_outcome_name(winner: str, outcomes: list) -> Optional[str]:
    """Map a winner name (from API) to the exact outcome string from the market."""
    w = str(winner or "").strip()
    if not w:
        return None
    if w.lower() in {"draw", "tie", "tied", "drew"}:
        for o in outcomes or []:
            if str(o).strip().lower() == "draw":
                return str(o).strip()
        return "Draw"
    # Direct match
    for o in outcomes or []:
        os_ = str(o).strip()
        if os_ and os_.lower() not in {"yes", "no", "draw"} and _team_matches(os_, w):
            return os_
    # Outcome may have " Win" suffix e.g. "Liverpool Win" — strip and retry
    for o in outcomes or []:
        os_ = str(o).strip()
        os_clean = re.sub(r"\s+(?:Win|Wins|Victory)$", "", os_, flags=re.I).strip()
        if os_clean and os_clean.lower() not in {"yes", "no", "draw"} and _team_matches(os_clean, w):
            return os_
        w_clean = re.sub(r"\s+(?:Win|Wins|Victory)$", "", w, flags=re.I).strip()
        if w_clean and w_clean != w and _team_matches(os_, w_clean):
            return os_
    return None


# ─── Sport detection ─────────────────────────────────────────────────────────

# Maps sport → keywords that appear in market questions/prompts
_SPORT_KEYWORDS: dict[str, list[str]] = {
    "football":         ["premier league", "fa cup", "champions league", "la liga", "serie a",
                         "bundesliga", "ligue 1", "mls", "eredivisie", "primera division",
                         "english football", "soccer"],
    "basketball":       ["nba", "basketball", "ncaa basketball", "wnba"],
    "american_football":["nfl", "ncaa football", "ufl", "xfl", "super bowl", "american football",
                         "college football"],
    "baseball":         ["mlb", "baseball", "world series"],
    "hockey":           ["nhl", "hockey", "stanley cup"],
    "cricket":          ["cricket", "ipl", "psl", "t20", "odi", "test match", "bbl", "cpl",
                         "the hundred", "big bash"],
    "tennis":           ["tennis", "wimbledon", "us open", "french open", "australian open",
                         "atp", "wta", "grand slam"],
    "golf":             ["golf", "pga", "masters", "open championship", "ryder cup"],
    "boxing_mma":       ["boxing", "ufc", "mma", "fight", "bout", "knockout"],
    "rugby":            ["rugby", "six nations", "premiership rugby", "super rugby"],
    "esports":          ["esports", "league of legends", "dota", "cs:go", "valorant"],
}

def _detect_sport(question: str, prompt_context: str = "") -> str:
    """Return the sport category string, or 'football' as default for vs. markets."""
    text = (str(question or "") + " " + str(prompt_context or "")).lower()

    # ── League name detection (highest priority) ──────────────────────────
    # This avoids maintaining a huge list of cricket franchise names.
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

    for sport, keywords in _SPORT_KEYWORDS.items():
        if any(k in text for k in keywords):
            return sport
    # Default: if "vs" in question, it's likely football/soccer
    if " vs " in text or " versus " in text:
        return "football"
    return ""


# ─── Fallback domain map (sport → ordered list of reliable result sources) ───

_SPORT_FALLBACK_DOMAINS: dict[str, list[str]] = {
    # Football broken out by competition — more precise Tavily indexing
    "football": [                   # generic fallback when competition unknown
        "bbc.co.uk",
        "skysports.com",
        "sofascore.com",
        "fotmob.com",
        "flashscore.com",
    ],
    "football_premier_league": [
        "premierleague.com",        # official PL site — best for PL results
        "bbc.co.uk",
        "skysports.com",
        "sofascore.com",
        "fotmob.com",
        "flashscore.com",
        "espn.com",
    ],
    "football_fa_cup": [
        "thefa.com",                # official FA site
        "bbc.co.uk",
        "skysports.com",
        "sofascore.com",
        "fotmob.com",
    ],
    "football_champions_league": [
        "uefa.com",                 # official UCL site
        "bbc.co.uk",
        "skysports.com",
        "sofascore.com",
        "espn.com",
    ],
    "football_la_liga": [
        "laliga.com",
        "bbc.co.uk",
        "sofascore.com",
        "espn.com",
    ],
    "football_serie_a": [
        "legaseriea.it",
        "bbc.co.uk",
        "sofascore.com",
        "espn.com",
    ],
    "football_bundesliga": [
        "bundesliga.com",
        "bbc.co.uk",
        "sofascore.com",
        "espn.com",
    ],
    "basketball": [
        "espn.com",
        "nba.com",
        "basketball-reference.com",
        "bleacherreport.com",
    ],
    "american_football": [
        "espn.com",
        "nfl.com",
        "pro-football-reference.com",
        "cbssports.com",
    ],
    "baseball": [
        "espn.com",
        "mlb.com",
        "baseball-reference.com",
        "cbssports.com",
    ],
    "hockey": [
        "espn.com",
        "nhl.com",
        "hockey-reference.com",
        "cbssports.com",
    ],
    "cricket": [
        "espncricinfo.com",
        "cricbuzz.com",
        "bbc.co.uk",
        "skysports.com",
    ],
    "tennis": [
        "atptour.com",
        "wtatennis.com",
        "espn.com",
        "bbc.co.uk",
    ],
    "golf": [
        "pgatour.com",
        "espn.com",
        "bbc.co.uk",
        "golfchannel.com",
    ],
    "boxing_mma": [
        "espn.com",
        "ufc.com",
        "boxingscene.com",
        "bbc.co.uk",
    ],
    "rugby": [
        "bbc.co.uk",
        "espn.com",
        "rugbypass.com",
        "skysports.com",
    ],
}

_DEFAULT_FALLBACK_DOMAINS = ["bbc.co.uk", "espn.com", "skysports.com", "sofascore.com"]


# ─── Content validators ───────────────────────────────────────────────────────

def is_spa_or_metadata_shell(raw: str) -> bool:
    """True when content is a JS SPA shell that cannot prove any market outcome."""
    r = str(raw or "").lower()
    body = strip_to_body_text(raw).lower()
    shell_markers = (
        "next-head-count" in r
        or "__next_data__" in r
        or "__next" in r
        or "application-name" in r
        or ("enable javascript" in r and len(body) < 300)
        or ("<meta " in r and "<title" in r and len(body) < 500)
    )
    if shell_markers and len(body) < 500:
        return True
    if "bitcoin purchases - strategy" in r and len(body) < 900:
        return True
    return False

def is_espn_homepage(content: str) -> bool:
    """True when ESPN returned generic homepage instead of a match page."""
    c = str(content or "").lower()
    return (
        "espn - serving sports fans" in c
        or 'canonical" href="https://www.espn.com"' in c
        or 'og:url" content="https://www.espn.com"' in c
        or 'og:title" content="espn - serving sports fans' in c
    )


def is_espn_match_url(url: str) -> bool:
    """True when the URL is a specific ESPN match/game/report page."""
    u = str(url or "").lower()
    return any(x in u for x in [
        "/match/", "/report/", "/gameid/", "/game/", "/boxscore/",
        "/soccer/match", "/nfl/game", "/nba/game", "/mlb/game", "/nhl/game",
    ])


def extract_espn_score_from_html(html: str, question: str, outcomes: list) -> Optional[str]:
    """
    ESPN embeds score data in __espnfitt__ or __dataLayer__ JSON inside <script> tags.
    Extract the score directly from the JavaScript before stripping kills it.
    Returns a clean evidence string or None.
    """
    import json as _json

    # Pattern 1: "score":"0" style in dataLayer
    score_pairs = re.findall(r'"score"\s*:\s*"(\d+)"', html)
    if len(score_pairs) >= 2:
        # First two scores are typically home/away
        hs, as_ = int(score_pairs[0]), int(score_pairs[1])
        team1, team2 = _teams_from_question(question, outcomes)
        if team1 and team2:
            winner = team1 if hs > as_ else (team2 if as_ > hs else "Draw")
            evidence = (
                f"ANSWER: {team1} {hs}-{as_} {team2} (Full Time). Winner: {winner}\n\n"
                f"[ESPN]\nMatch: {team1} vs {team2}\n"
                f"Score: {team1} {hs} - {as_} {team2}\nWinner: {winner}\n"
            )
            return evidence

    # Pattern 2: JSON blob with homeScore/awayScore
    m = re.search(r'"homeScore"\s*:\s*(\d+).*?"awayScore"\s*:\s*(\d+)', html, re.S)
    if m:
        hs, as_ = int(m.group(1)), int(m.group(2))
        team1, team2 = _teams_from_question(question, outcomes)
        if team1 and team2:
            winner = team1 if hs > as_ else (team2 if as_ > hs else "Draw")
            evidence = (
                f"ANSWER: {team1} {hs}-{as_} {team2} (Full Time). Winner: {winner}\n\n"
                f"[ESPN]\nMatch: {team1} vs {team2}\n"
                f"Score: {team1} {hs} - {as_} {team2}\nWinner: {winner}\n"
            )
            return evidence

    # Pattern 3: X-Y score pattern near team names
    team1, team2 = _teams_from_question(question, outcomes)
    if team1 and team2:
        t1l, t2l = team1.lower(), team2.lower()
        # Find score near team names
        idx1 = html.lower().find(t1l)
        idx2 = html.lower().find(t2l)
        if idx1 >= 0 and idx2 >= 0:
            window_start = min(idx1, idx2) - 100
            window_end = max(idx1, idx2) + 500
            window = html[max(0, window_start):window_end]
            scores = re.findall(r"\b(\d{1,3})\s*[-–]\s*(\d{1,3})\b", window)
            if scores:
                hs, as_ = int(scores[0][0]), int(scores[0][1])
                winner = team1 if hs > as_ else (team2 if as_ > hs else "Draw")
                evidence = (
                    f"ANSWER: {team1} {hs}-{as_} {team2} (Full Time). Winner: {winner}\n\n"
                    f"[ESPN]\nMatch: {team1} vs {team2}\n"
                    f"Score: {team1} {hs} - {as_} {team2}\nWinner: {winner}\n"
                )
                return evidence

    return None

def has_participant_token(content: str, question: str, outcomes: list) -> bool:
    """True if content mentions at least one meaningful team/participant token."""
    c = str(content or "").lower()
    names = []
    m = re.search(r"(.+?)\s+(?:vs|versus)\s+(.+?)(?:\s+[—-]|\s+\(|$)", str(question or ""), re.I)
    if m:
        names.extend([m.group(1).strip(), m.group(2).strip()])
    for o in outcomes or []:
        s = str(o or "").strip()
        if s and s.lower() not in {"draw", "yes", "no"} and not re.search(r"^[+-]?\d", s):
            names.append(s)
    # Also add alias expansions
    expanded = [_expand_alias(n) for n in names]
    names = list(set(names + expanded))

    stop = {"win", "draw", "the", "and", "english", "premier", "league", "cup",
            "football", "basketball", "cricket", "tennis", "hockey", "baseball"}
    tokens = sorted({
        tok for name in names
        for tok in re.findall(r"[a-zA-Z]{3,}", name.lower())
        if tok not in stop
    })
    return bool(tokens) and any(tok in c for tok in tokens)

def validate_sports_content(content: str, question: str, outcomes: list) -> tuple[bool, str]:
    """
    Sports validation: content must contain participant + score or result language.
    Checks are sport-aware (cricket, American football, etc.).
    """
    c = str(content or "")
    cl = c.lower()

    if is_espn_homepage(c):
        return False, "ESPN homepage returned instead of match page"
    if is_spa_or_metadata_shell(c):
        return False, "SPA/metadata shell — no usable evidence"
    if not has_participant_token(c, question, outcomes):
        return False, "topic drift: no participant token found in content"

    # ANSWER line from structured APIs — accept directly
    answer_m = re.search(r"ANSWER:\s*(.+?)(?:\n\n|\n\[|\Z)", c, re.I | re.S)
    if answer_m:
        answer = answer_m.group(1).lower()
        for o in outcomes or []:
            ol = str(o).strip().lower()
            if len(ol) > 2 and ol != "draw" and ol in answer:
                return True, f"ANSWER line names outcome: {o}"
        if re.search(r"\b(draw|drew|tied)\b", answer):
            return True, "ANSWER line indicates draw"

    sport = _detect_sport(question)

    # Cricket-specific score patterns
    if sport == "cricket":
        if re.search(r"\b\d+\s*/\s*\d+\b", c):
            return True, "cricket innings score found (X/Y)"
        if re.search(r"\b\d+\s+(?:runs?|wickets?|wkts?)\b", cl):
            return True, "cricket result language found"
        if re.search(r"\bwon by\b.{1,60}\b(?:runs?|wickets?)\b", cl):
            return True, "cricket won-by language found"

    # American football — scores like 17-14 or 34–20
    if sport == "american_football":
        if re.search(r"\b\d{1,2}\s*[-–]\s*\d{1,2}\b", c):
            return True, "American football score found"

    # Baseball — scores like 5-3 or W 7-2
    if sport == "baseball":
        if re.search(r"\b[WL]\s+\d{1,2}\s*[-–]\s*\d{1,2}\b", c):
            return True, "baseball win/loss score found"
        if re.search(r"\b\d{1,2}\s*[-–]\s*\d{1,2}\b", c):
            return True, "baseball score found"

    # Tennis — set scores like 6-3, 7-6, 6-4
    if sport == "tennis":
        if re.search(r"\b6\s*[-–]\s*[0-7]\b", c):
            return True, "tennis set score found"
        if re.search(r"\b(?:def\.|defeated|beat)\b", cl):
            return True, "tennis result language found"

    # Generic score pattern (football/basketball/hockey/etc)
    if re.search(r"\b\d{1,3}\s*[-–]\s*\d{1,3}\b", c):
        return True, "score pattern found"

    # Result language
    if re.search(r"\b(won|beat|defeated|full.?time|final score|match result|game over)\b", cl):
        return True, "result language found"

    # Box score format (basketball, baseball)
    if re.search(r"\|\s*\d+\s*\|\s*\d+\s*\|", c):
        return True, "box score table found"

    return False, "no score or result evidence found"

def validate_confirmation_content(
    content: str, question: str, start_date: str, end_date: str,
) -> tuple[bool, str]:
    """
    Company/crypto event: body text must contain entity + action + date in window.
    Head/meta/title alone never confirms anything.
    """
    if is_spa_or_metadata_shell(content):
        return False, "SPA/metadata shell — title/meta cannot confirm event"

    body = strip_to_body_text(content)
    b = body.lower()

    if len(body) < 300:
        return False, "body too short after stripping head/meta/script"

    # Date window check
    has_date = False
    if start_date and start_date[:10] in b:
        has_date = True
    if not has_date and end_date and end_date[:10] in b:
        has_date = True
    if not has_date:
        try:
            from datetime import datetime, timedelta
            s = datetime.fromisoformat(start_date[:10])
            e = datetime.fromisoformat(end_date[:10]) if end_date else s
            cur = s
            while cur <= e and not has_date:
                for fmt in ["%B %d", "%B %-d", "%b %d", "%b %-d"]:
                    try:
                        if cur.strftime(fmt).lstrip("0").lower() in b:
                            has_date = True
                            break
                    except Exception:
                        pass
                cur += timedelta(days=1)
        except Exception:
            pass

    if not has_date:
        return False, "no date from market window found in body text"


    # Generic announcement confirmation
    action_found = bool(re.search(
        r"\b(announced|confirmed|published|stated|released|launched|listed|"
        r"acquired|purchased|bought|approved|signed|completed|closed)\b", b, re.I
    ))
    if not action_found:
        return False, "no confirmation/announcement language in body text"

    return True, "dated confirmation found in body text"


# ─── Low-level fetchers ───────────────────────────────────────────────────────

def _clean_domain(url: str) -> str:
    s = re.sub(r"^https?://", "", str(url or "").lower())
    s = s.split("/")[0].split("?")[0]
    return s[4:] if s.startswith("www.") else s

def _direct_fetch(url: str) -> Optional[tuple[str, str]]:
    """Returns (text, content_type) or None."""
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15",
        "curl/7.88.1",
    ]
    for ua in uas:
        try:
            r = requests.get(
                url,
                headers={"User-Agent": ua, "Accept": "application/json,text/html,*/*"},
                timeout=15,
            )
            if r.status_code == 200 and len(r.text) > 100:
                ct = r.headers.get("content-type", "")
                ctype = "json" if ("json" in ct or r.text.strip()[:1] in ("{", "[")) else "html"
                return r.text, ctype
        except Exception:
            continue
    return None

def _tavily_fetch(
    domain: str, question: str, event_date: str,
    query: str, search_depth: str = "advanced",
    is_fdv: bool = False,
    preferred_path: str = "",
) -> Optional[str]:
    """Tavily fetch, domain-locked by default. is_fdv=True removes the site: restriction."""
    tavily_key = _env("TAVILY_API_KEY")
    if not tavily_key:
        return None

    full_query = f"{query} {event_date}" if is_fdv else f"site:{domain} {query} {event_date}"
    if preferred_path:
        full_query = f"{full_query} {preferred_path}"

    print(f"[fetch] Tavily: {full_query[:120]}")
    try:
        r = requests.post("https://api.tavily.com/search", json={
            "api_key": tavily_key,
            "query": full_query,
            "search_depth": search_depth,
            "max_results": 6,
            "include_answer": True,
        }, timeout=15)
        data = r.json()
        result_parts = []
        for res in data.get("results", [])[:5]:
            url = res.get("url", "")
            res_domain = _clean_domain(url)
            if is_fdv or res_domain == domain or res_domain.endswith("." + domain):
                if preferred_path:
                    path = re.sub(r"^https?://[^/]+", "", url).lower()
                    if not path.startswith(preferred_path):
                        continue
                result_parts.append(f"[{url}]\n{res.get('content', '')[:800]}")

        parts = []
        if result_parts and data.get("answer"):
            parts.append(f"ANSWER: {data['answer']}")
        parts.extend(result_parts)

        content = "\n\n".join(parts)
        if content.strip():
            print(f"[fetch] Tavily: {len(content)} chars from {domain}")
            return content
    except Exception as e:
        print(f"[fetch] Tavily failed ({domain}): {e}")
    return None


# ─── Sports structured API fetchers ──────────────────────────────────────────

_FOOTBALL_DATA_COMPETITIONS = {
    "fa cup": "FA", "english fa cup": "FA",
    "premier league": "PL", "epl": "PL", "english premier league": "PL",
    "champions league": "CL", "ucl": "CL",
    "la liga": "PD", "serie a": "SA",
    "bundesliga": "BL1", "ligue 1": "FL1",
    "mls": "BSA",
}

def _build_evidence(source: str, home: str, away: str, hs: int, as_: int,
                    event_date: str, status: str = "", extra: str = "") -> tuple[str, str]:
    if hs > as_:
        winner = home
    elif as_ > hs:
        winner = away
    else:
        winner = "Draw"
    evidence = (
        f"ANSWER: {home} {hs}-{as_} {away} (Full Time). Winner: {winner}\n\n"
        f"[{source}]\n"
        f"Match: {home} vs {away}\n"
        f"Date: {event_date}\n"
        f"Score: {home} {hs} - {as_} {away}\n"
        f"Status: {status}\n"
        f"Winner: {winner}\n"
        f"{extra or ''}\n"
    )
    return evidence, winner



def _fetch_thesportsdb(question: str, event_date: str, outcomes: list) -> Optional[FetchResult]:
    """
    TheSportsDB free API — no key required.
    Tries Soccer and Football sport strings, then team name search as fallback.
    """
    try:
        team1, team2 = _teams_from_question(question, outcomes)
        if not team1 or not team2:
            return None

        # Try multiple sport strings — TheSportsDB naming is inconsistent
        for sport_str in ["Soccer", "Football", "Basketball", "Baseball",
                          "Ice Hockey", "Cricket", "Tennis", "Golf",
                          "American Football", "Rugby"]:
            url = f"https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d={event_date}&s={sport_str}"
            print(f"[fetch] TheSportsDB ({sport_str}): {url}")
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "OracleREE/1.0"})
                if not r.ok:
                    continue
                for ev in r.json().get("events") or []:
                    home = str(ev.get("strHomeTeam") or "")
                    away = str(ev.get("strAwayTeam") or "")
                    if not (_team_matches(team1, home) or _team_matches(team1, away)):
                        continue
                    if not (_team_matches(team2, home) or _team_matches(team2, away)):
                        continue
                    hs = ev.get("intHomeScore")
                    as_ = ev.get("intAwayScore")
                    if hs is None or as_ is None:
                        print(f"[fetch] TheSportsDB: match found but no score yet ({home} vs {away})")
                        return None

                    # Cricket T20: tied score — search for Super Over winner.
                    # Pass full evidence including Super Over result to extract layer.
                    # extract.py uses settlement prompt to decide if Super Over counts.
                    if sport_str.lower() == "cricket" and int(hs) == int(as_):
                        print(f"[fetch] Cricket tie {hs}-{as_} — searching Super Over result")
                        tavily_key = _env("TAVILY_API_KEY")
                        super_over_content = ""

                        if tavily_key:
                            try:
                                so_query = f"{home} vs {away} {event_date} super over winner result"
                                r_so = requests.post(
                                    "https://api.tavily.com/search",
                                    json={
                                        "api_key": tavily_key,
                                        "query": so_query,
                                        "search_depth": "basic",
                                        "max_results": 5,
                                        "include_answer": True,
                                    },
                                    timeout=15,
                                )
                                data_so = r_so.json()
                                parts = []

                                if data_so.get("answer"):
                                    parts.append("ANSWER: " + str(data_so.get("answer")))

                                for res in data_so.get("results", [])[:4]:
                                    parts.append(
                                        f"[{res.get('url', '')}]\n"
                                        f"{str(res.get('content', ''))[:600]}"
                                    )

                                super_over_content = "\n\n".join(p for p in parts if str(p).strip()).strip()
                                if super_over_content:
                                    print(f"[fetch] Super Over Tavily: {len(super_over_content)} chars")

                            except Exception as e:
                                print(f"[fetch] Super Over Tavily failed: {e}")

                        content = (
                            f"Match result: {home} vs {away}\n"
                            f"Regulation score: {hs}-{as_} (tied)\n"
                            f"Super Over: played\n"
                            f"date: {event_date}\n\n"
                        )

                        if super_over_content:
                            content += f"Super Over evidence:\n{super_over_content}"
                        else:
                            content += "Super Over result: not available from search\n"

                        return FetchResult(
                            content=content,
                            source="TheSportsDB + Tavily",
                            method="structured_api_thesportsdb",
                            is_valid=True,
                            validation_reason=f"tied {hs}-{as_}, Super Over evidence included",
                        )

                    evidence, winner = _build_evidence(
                        "TheSportsDB", home, away, int(hs), int(as_),
                        event_date, str(ev.get("strStatus") or "")
                    )
                    print(f"[fetch] ✓ TheSportsDB ({sport_str}): {home} {hs}-{as_} {away} → {winner}")
                    return FetchResult(
                        content=evidence, source="TheSportsDB",
                        method="structured_api_thesportsdb", is_valid=True,
                        validation_reason=f"TheSportsDB: {home} {hs}-{as_} {away}",
                    )
            except Exception:
                continue

        # Final fallback: search by team name
        t1_encoded = requests.utils.quote(str(_expand_alias(team1)))
        search_url = f"https://www.thesportsdb.com/api/v1/json/3/searchevents.php?e={t1_encoded}"
        print(f"[fetch] TheSportsDB team search: {team1}")
        r2 = requests.get(search_url, timeout=8, headers={"User-Agent": "OracleREE/1.0"})
        if r2.ok:
            for ev in r2.json().get("event") or []:
                ev_date = str(ev.get("dateEvent") or "")
                if ev_date != event_date:
                    continue
                home = str(ev.get("strHomeTeam") or "")
                away = str(ev.get("strAwayTeam") or "")
                if not (_team_matches(team2, home) or _team_matches(team2, away)):
                    continue
                hs, as_ = ev.get("intHomeScore"), ev.get("intAwayScore")
                if hs is None or as_ is None:
                    return None

                # Cricket T20: tied score — search for Super Over winner.
                # Pass full evidence including Super Over result to extract layer.
                # extract.py uses settlement prompt to decide if Super Over counts.
                if _detect_sport(question) == "cricket" and int(hs) == int(as_):
                    print(f"[fetch] Cricket tie {hs}-{as_} — searching Super Over result")
                    tavily_key = _env("TAVILY_API_KEY")
                    super_over_content = ""

                    if tavily_key:
                        try:
                            so_query = f"{home} vs {away} {event_date} super over winner result"
                            r_so = requests.post(
                                "https://api.tavily.com/search",
                                json={
                                    "api_key": tavily_key,
                                    "query": so_query,
                                    "search_depth": "basic",
                                    "max_results": 5,
                                    "include_answer": True,
                                },
                                timeout=15,
                            )
                            data_so = r_so.json()
                            parts = []

                            if data_so.get("answer"):
                                parts.append("ANSWER: " + str(data_so.get("answer")))

                            for res in data_so.get("results", [])[:4]:
                                parts.append(
                                    f"[{res.get('url', '')}]\n"
                                    f"{str(res.get('content', ''))[:600]}"
                                )

                            super_over_content = "\n\n".join(p for p in parts if str(p).strip()).strip()
                            if super_over_content:
                                print(f"[fetch] Super Over Tavily: {len(super_over_content)} chars")

                        except Exception as e:
                            print(f"[fetch] Super Over Tavily failed: {e}")

                    content = (
                        f"Match result: {home} vs {away}\n"
                        f"Regulation score: {hs}-{as_} (tied)\n"
                        f"Super Over: played\n"
                        f"date: {event_date}\n\n"
                    )

                    if super_over_content:
                        content += f"Super Over evidence:\n{super_over_content}"
                    else:
                        content += "Super Over result: not available from search\n"

                    return FetchResult(
                        content=content,
                        source="TheSportsDB + Tavily",
                        method="structured_api_thesportsdb_search",
                        is_valid=True,
                        validation_reason=f"tied {hs}-{as_}, Super Over evidence included",
                    )

                evidence, winner = _build_evidence(
                    "TheSportsDB", home, away, int(hs), int(as_), event_date
                )
                print(f"[fetch] ✓ TheSportsDB (search): {home} {hs}-{as_} {away} → {winner}")
                return FetchResult(
                    content=evidence, source="TheSportsDB",
                    method="structured_api_thesportsdb_search", is_valid=True,
                    validation_reason=f"TheSportsDB search: {home} {hs}-{as_} {away}",
                )

    except Exception as e:
        print(f"[fetch] TheSportsDB failed: {e}")
    return None


def _fetch_football_data_org(
    question: str, event_date: str, outcomes: list, prompt_context: str = ""
) -> Optional[FetchResult]:
    """
    football-data.org free tier.
    Covers: PL, FA, CL, La Liga, Serie A, Bundesliga, Ligue 1, MLS.
    Free without key but rate-limited; add FOOTBALL_DATA_API_KEY to .env.local for higher limits.
    """
    football_data_key = _env("FOOTBALL_DATA_API_KEY")
    try:
        team1, team2 = _teams_from_question(question, outcomes)
        if not team1 or not team2:
            return None

        text = (str(question) + " " + str(prompt_context or "")).lower()

        # Find competition — try all matches, not just first
        comps_to_try = [
            code for name, code in _FOOTBALL_DATA_COMPETITIONS.items()
            if name in text
        ]
        # Deduplicate while preserving order
        seen = set()
        comps_to_try = [c for c in comps_to_try if not (c in seen or seen.add(c))]
        if not comps_to_try:
            comps_to_try = ["PL", "FA"]  # Default to Premier League + FA Cup

        headers = {"User-Agent": "OracleREE/1.0"}
        if football_data_key:
            headers["X-Auth-Token"] = football_data_key

        for comp in comps_to_try:
            url = f"https://api.football-data.org/v4/matches?competitions={comp}&dateFrom={event_date}&dateTo={event_date}"
            print(f"[fetch] football-data.org ({comp}): {url}")
            r = requests.get(url, headers=headers, timeout=10)

            if r.status_code == 403:
                print(f"[fetch] football-data.org: 403 on {comp} (add FOOTBALL_DATA_API_KEY to .env.local)")
                continue
            if r.status_code == 429:
                print("[fetch] football-data.org: rate limited")
                break
            if not r.ok:
                continue

            for match in r.json().get("matches") or []:
                home = str((match.get("homeTeam") or {}).get("name") or "")
                away = str((match.get("awayTeam") or {}).get("name") or "")
                if not (_team_matches(team1, home) or _team_matches(team1, away)):
                    continue
                if not (_team_matches(team2, home) or _team_matches(team2, away)):
                    continue

                status = str(match.get("status") or "").upper()
                if status not in ("FINISHED", "FULL_TIME"):
                    print(f"[fetch] football-data.org: match found, status={status} (not finished)")
                    return None

                score = match.get("score") or {}
                ft = score.get("fullTime") or {}
                hs, as_ = ft.get("home"), ft.get("away")
                if hs is None or as_ is None:
                    return None

                pen = score.get("penalties") or {}
                et = score.get("extraTime") or {}
                if pen.get("home") is not None:
                    ph, pa = int(pen["home"]), int(pen["away"])
                    winner = home if ph > pa else away
                    evidence = (
                        f"ANSWER: {home} {hs}-{as_} {away} (After Penalties: {home} {ph}-{pa} {away}). Winner: {winner}\n\n"
                        f"[football-data.org]\nMatch: {home} vs {away}\nDate: {event_date}\n"
                        f"Full Time: {home} {hs} - {as_} {away}\n"
                        f"Penalties: {home} {ph} - {pa} {away}\nWinner: {winner}\n"
                    )
                elif et.get("home") is not None:
                    eh, ea = int(et["home"]), int(et["away"])
                    winner = home if eh > ea else (away if ea > eh else "Draw")
                    evidence, _ = _build_evidence("football-data.org", home, away, eh, ea, event_date, "AET")
                else:
                    evidence, winner = _build_evidence(
                        "football-data.org", home, away, int(hs), int(as_), event_date, "FT"
                    )

                print(f"[fetch] ✓ football-data.org ({comp}): {home} {hs}-{as_} {away} → {winner}")
                return FetchResult(
                    content=evidence, source="football-data.org",
                    method="structured_api_football_data", is_valid=True,
                    validation_reason=f"football-data.org: {home} {hs}-{as_} {away}",
                )

    except Exception as e:
        print(f"[fetch] football-data.org failed: {e}")
    return None


def _fetch_espncricinfo(question: str, event_date: str, outcomes: list) -> Optional[FetchResult]:
    """
    ESPNcricinfo API for cricket match results.
    Uses the public scores API — no key required.
    """
    try:
        team1, team2 = _teams_from_question(question, outcomes)
        if not team1 or not team2:
            return None

        # ESPNcricinfo live scores endpoint
        url = "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current?lang=en&latest=true"
        print(f"[fetch] ESPNcricinfo: checking current matches")
        r = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36"
        })
        if not r.ok:
            return None

        data = r.json()
        for match in (data.get("matches") or data.get("content", {}).get("matches") or []):
            teams = match.get("teams") or []
            team_names = [str((t.get("team") or {}).get("longName") or (t.get("team") or {}).get("name") or "") for t in teams]
            if len(team_names) < 2:
                continue
            if not (_team_matches(team1, team_names[0]) or _team_matches(team1, team_names[1])):
                continue
            if not (_team_matches(team2, team_names[0]) or _team_matches(team2, team_names[1])):
                continue

            # Check date
            start_date = str(match.get("startDate") or match.get("date") or "")[:10]
            if start_date != event_date:
                continue

            status_text = str(match.get("statusText") or match.get("status") or "")
            state = str(match.get("state") or "").lower()

            if state not in {"complete", "finished"} and "won" not in status_text.lower():
                print(f"[fetch] ESPNcricinfo: match found but not complete (state={state})")
                return None

            # Build evidence from status text
            evidence = (
                f"ANSWER: {status_text}\n\n"
                f"[ESPNcricinfo]\n"
                f"Match: {team_names[0]} vs {team_names[1]}\n"
                f"Date: {event_date}\n"
                f"Result: {status_text}\n"
            )
            print(f"[fetch] ✓ ESPNcricinfo: {status_text}")
            return FetchResult(
                content=evidence, source="ESPNcricinfo",
                method="structured_api_espncricinfo", is_valid=True,
                validation_reason=f"ESPNcricinfo: {status_text}",
            )

    except Exception as e:
        print(f"[fetch] ESPNcricinfo failed: {e}")
    return None


def _detect_competition(question: str, prompt_context: str = "") -> str:
    """Return a human-readable competition name for Tavily queries."""
    text = (str(question or "") + " " + str(prompt_context or "")).lower()
    if "premier league" in text or "epl" in text:
        return "Premier League"
    if "fa cup" in text or "english fa cup" in text:
        return "FA Cup"
    if "champions league" in text or "ucl" in text:
        return "Champions League"
    if "la liga" in text:
        return "La Liga"
    if "serie a" in text:
        return "Serie A"
    if "bundesliga" in text:
        return "Bundesliga"
    if "ligue 1" in text:
        return "Ligue 1"
    if "mls" in text:
        return "MLS"
    if "nba" in text:
        return "NBA"
    if "nfl" in text:
        return "NFL"
    if "mlb" in text:
        return "MLB"
    if "nhl" in text:
        return "NHL"
    if "ipl" in text:
        return "IPL"
    if "psl" in text:
        return "PSL"
    return ""


def _competition_fallback_key(question: str, prompt_context: str = "") -> str:
    """Return the _SPORT_FALLBACK_DOMAINS key for this competition."""
    text = (str(question or "") + " " + str(prompt_context or "")).lower()
    if "premier league" in text or "epl" in text:
        return "football_premier_league"
    if "fa cup" in text or "english fa cup" in text:
        return "football_fa_cup"
    if "champions league" in text or "ucl" in text:
        return "football_champions_league"
    if "la liga" in text:
        return "football_la_liga"
    if "serie a" in text:
        return "football_serie_a"
    if "bundesliga" in text:
        return "football_bundesliga"
    return "football"


def _fetch_tavily_sports(
    question: str, event_date: str, outcomes: list, domain: str,
    prompt_context: str = "",
) -> Optional[FetchResult]:
    """
    Tavily fetch against a trusted sports domain.
    Query is competition-aware — Premier League search uses "Premier League",
    not "FA Cup". Team aliases expanded so "Man City" finds "Manchester City".
    """
    team1, team2 = _teams_from_question(question, outcomes)
    competition = _detect_competition(question, prompt_context)
    if team1:
        t1_token = _tavily_team_token(team1)
        t2_token = _tavily_team_token(team2)
        query = f"{t1_token} {t2_token} {competition} result final score".strip()
    else:
        query = f"{question} result final score"

    content = _tavily_fetch(domain, question, event_date, query, search_depth="advanced")
    if not content:
        return None

    ok, reason = validate_sports_content(content, question, outcomes)
    if ok:
        return FetchResult(
            content=content, source=domain,
            method=f"tavily_{domain.replace('.', '_').replace('-', '_')}",
            is_valid=True, validation_reason=reason,
        )
    print(f"[fetch] Rejected {domain}: {reason}")
    return None


# ─── Main entry point ─────────────────────────────────────────────────────────

def fetch_source(
    source_url: str,
    query: str,
    event_date: str,
    market_category: str,
    question: str,
    outcomes: list,
    prompt_context: str = "",
    window_start: str = "",
    window_end: str = "",
    asset: str = "",
    close_time: str = "",
) -> FetchResult:
    """
    Fetch content for a market and validate it before returning.

    market_category: sports | company_event | crypto_price | crypto_event | count | generic_binary

    Returns FetchResult(is_valid=True) only when content is confirmed usable.
    Returns FetchResult(is_valid=False) when nothing valid found — caller returns INCONCLUSIVE.
    """
    domain = _clean_domain(source_url)
    cat = str(market_category or "").lower()

    # ── SPORTS ────────────────────────────────────────────────────────────────
    if cat == "sports":
        sport = _detect_sport(question, prompt_context)
        print(f"[fetch] Sports market — sport={sport or 'unknown'}, structured APIs first")

        # 1. ESPN Scoreboard — covers ALL sports/competitions worldwide, no key needed
        espn_sb = _fetch_espn_scoreboard(question, event_date, outcomes)
        if espn_sb and espn_sb.is_valid:
            return espn_sb

        # 2. Structured APIs — sport-aware
        if sport in ("football", ""):
            # Football/Soccer: TheSportsDB + football-data.org
            for api_fn in (_fetch_thesportsdb, _fetch_football_data_org):
                kwargs = {"prompt_context": prompt_context} if api_fn == _fetch_football_data_org else {}
                result = api_fn(question, event_date, outcomes, **kwargs)
                if result and result.is_valid:
                    return result
        elif sport == "cricket":
            result = _fetch_espncricinfo(question, event_date, outcomes)
            if result and result.is_valid:
                return result
            result = _fetch_thesportsdb(question, event_date, outcomes)
            if result and result.is_valid:
                return result
        else:
            # All other sports: TheSportsDB covers basketball, NFL, NHL, MLB, Tennis, Golf
            result = _fetch_thesportsdb(question, event_date, outcomes)
            if result and result.is_valid:
                return result

        # 2. Creator source via Tavily (honours creator-source-is-law)
        team1, team2 = _teams_from_question(question, outcomes)
        if team1:
            creator_query = f"{_tavily_team_token(team1)} {_tavily_team_token(team2)} result final score"
        else:
            creator_query = query

        creator_content = _tavily_fetch(domain, question, event_date, creator_query, search_depth="advanced")
        if creator_content:
            ok, reason = validate_sports_content(creator_content, question, outcomes)
            if ok:
                return FetchResult(
                    content=creator_content, source=domain,
                    method="tavily_creator_domain", is_valid=True, validation_reason=reason,
                )
            print(f"[fetch] Creator Tavily ({domain}) rejected: {reason}")

        # 3. Direct fetch creator URL
        direct = _direct_fetch(source_url)
        if direct:
            raw, ctype = direct
            if not is_espn_homepage(raw):
                # For ESPN match pages: extract score from JS data before stripping kills it
                if "espn.com" in source_url.lower() and is_espn_match_url(source_url):
                    espn_evidence = extract_espn_score_from_html(raw, question, outcomes)
                    if espn_evidence:
                        print(f"[fetch] ✓ ESPN match score extracted from JS data")
                        return FetchResult(
                            content=espn_evidence, source=domain, method="direct_espn_js",
                            is_valid=True,
                            validation_reason="ESPN match page: score extracted from JS data layer",
                            raw_html=raw,
                        )
                # Standard path: strip HTML then validate
                text = strip_to_body_text(raw) if ctype == "html" else raw
                ok, reason = validate_sports_content(text, question, outcomes)
                if ok:
                    return FetchResult(
                        content=text, source=domain, method="direct",
                        is_valid=True, validation_reason=reason, raw_html=raw,
                    )

        # 4. Competition-specific fallback domains
        # For football: Premier League, FA Cup, UCL each get different domains
        if sport == "football":
            comp_key = _competition_fallback_key(question, prompt_context)
            fallback_domains = _SPORT_FALLBACK_DOMAINS.get(comp_key, _SPORT_FALLBACK_DOMAINS["football"])
            print(f"[fetch] Football fallback: {comp_key} -> {fallback_domains[:3]}")
        else:
            fallback_domains = _SPORT_FALLBACK_DOMAINS.get(sport, _DEFAULT_FALLBACK_DOMAINS)

        # Don't re-try the creator domain
        fallback_domains = [d for d in fallback_domains if d != domain]

        for fb_domain in fallback_domains:
            result = _fetch_tavily_sports(question, event_date, outcomes, fb_domain, prompt_context)
            if result and result.is_valid:
                return result

        return FetchResult(
            is_valid=False,
            validation_reason=(
                f"Sports ({sport}): structured APIs, creator source ({domain}), "
                f"and fallback domains all failed for '{question}' on {event_date}."
            )
        )

    # ── COMPANY EVENT / CRYPTO EVENT (confirmation markets) ──────────────────
    if cat in {"company_event", "crypto_event", "crypto_web3_event", "generic_binary", "confirmation_yes_no"}:
        print(f"[fetch] Confirmation market — body-only, date window required")

        path = re.sub(r"^https?://[^/]+", "", source_url).lower().rstrip("/")
        preferred_path = path if len(path) > 1 else ""

        direct = _direct_fetch(source_url)
        if direct:
            raw, ctype = direct
            ok, reason = validate_confirmation_content(
                raw, question, window_start or event_date, window_end or event_date
            )
            if ok:
                return FetchResult(
                    content=strip_to_body_text(raw), source=domain, method="direct",
                    is_valid=True, validation_reason=reason, raw_html=raw,
                )
            print(f"[fetch] Direct confirmation rejected: {reason}")

        content = _tavily_fetch(
            domain, question, event_date, query,
            search_depth="advanced",
            preferred_path=preferred_path,
        )
        if content:
            ok, reason = validate_confirmation_content(
                content, question, window_start or event_date, window_end or event_date
            )
            if ok:
                return FetchResult(
                    content=strip_to_body_text(content), source=domain,
                    method="tavily_path_filtered", is_valid=True, validation_reason=reason,
                )
            print(f"[fetch] Tavily confirmation rejected: {reason}")


        # Level 2: broad Tavily — no site: restriction
        # Handles Cloudflare-blocked sources (eurovision.tv, strategy.com SPA)
        print(f"[fetch] Confirmation broad Tavily fallback for: {question[:60]}")
        tavily_key = _env("TAVILY_API_KEY")
        if tavily_key:
            try:
                broad_query = f"{question} {window_start or event_date}"
                r = requests.post("https://api.tavily.com/search", json={
                    "api_key": tavily_key,
                    "query": broad_query,
                    "search_depth": "basic",
                    "max_results": 6,
                    "include_answer": True,
                }, timeout=15)
                data = r.json()
                parts = []
                if data.get("answer"):
                    parts.append(f"ANSWER: {data['answer']}")
                for res in data.get("results", [])[:5]:
                    res_url = res.get("url", "")
                    res_text = res.get("content", "")[:600]
                    parts.append("[" + res_url + "]" + chr(10) + res_text)
                broad_content = chr(10).join(parts)
                if broad_content.strip() and len(broad_content) > 80:
                    print(f"[fetch] ✓ Broad Tavily: {len(broad_content)} chars")
                    # For named choice (many outcomes): extract outcome then return
                    if len(outcomes or []) > 2:
                        # Try to find a valid outcome in the broad content
                        broad_lower = broad_content.lower()
                        found_outcome = None
                        import re as _re
                        # Score each outcome by how strongly it appears as a winner
                        best_outcome = None
                        best_score = 0
                        for o in (outcomes or []):
                            if str(o).strip().lower() in {"yes", "no", "draw"}:
                                continue
                            o_lower = str(o).strip().lower()
                            if o_lower not in broad_lower:
                                continue
                            score = 0
                            o_pattern = _re.escape(o_lower)
                            # Strong winner signals
                            if _re.search(r"winner\s+(?:was|is|:)\s*" + o_pattern, broad_lower):
                                score += 10
                            if _re.search(o_pattern + r"\s+(?:won|wins|wins the)", broad_lower):
                                score += 10
                            if _re.search(r"(won|wins|victory|champion).{0,50}" + o_pattern, broad_lower):
                                score += 5
                            if _re.search(o_pattern + r".{0,50}(won|wins|victory|champion)", broad_lower):
                                score += 5
                            # Weak signal: just mentioned
                            if score == 0:
                                score += 1
                            if score > best_score:
                                best_score = score
                                best_outcome = str(o).strip()
                        # Only accept if strong winner signal found
                        if best_score >= 5:
                            found_outcome = best_outcome
                        # Fallback: first outcome mentioned in ANSWER line
                        if not found_outcome:
                            answer_m = _re.search("ANSWER:" + r"(.*)", broad_content, _re.I)
                            if answer_m:
                                answer_text = answer_m.group(1).lower()
                                for o in (outcomes or []):
                                    if str(o).strip().lower() in answer_text:
                                        found_outcome = str(o).strip()
                                        break
                        if found_outcome:
                            print(f"[fetch] ✓ Named choice extracted: {found_outcome}")
                        return FetchResult(
                            content=broad_content, source="Tavily broad",
                            method="tavily_broad_named_choice", is_valid=True,
                            validation_reason="broad Tavily for named choice",
                            matched_outcome=found_outcome or "",
                        )
                    # For Yes/No: validate with confirmation rules
                    ok2, reason2 = validate_confirmation_content(
                        broad_content, question,
                        window_start or event_date, window_end or event_date
                    )
                    if ok2:
                        return FetchResult(
                            content=broad_content, source="Tavily broad",
                            method="tavily_broad_confirmation", is_valid=True,
                            validation_reason=reason2,
                        )

                    # Date validation failed but we have broad content —
                    # for Yes/No markets pass to extract layer anyway.
                    # extract.py + settlement prompt decides the answer.
                    # This handles niche events (Enhanced Games, entertainment)
                    # where sources don't contain exact market dates.
                    out_lower = {str(o).strip().lower() for o in (outcomes or [])}
                    if out_lower == {"yes", "no"} and len(broad_content) > 200:
                        print("[fetch] Yes/No market — passing broad content to extract despite date validation failure")
                        return FetchResult(
                            content=broad_content, source="Tavily broad",
                            method="tavily_broad_confirmation", is_valid=True,
                            validation_reason="yes/no market broad content passed to extract layer",
                        )
            except Exception as e:
                print(f"[fetch] Broad Tavily failed: {e}")

        return FetchResult(
            is_valid=False,
            validation_reason=(
                f"Confirmation: {domain} fetched but no dated body-text confirmation "
                f"in window {window_start or event_date}–{window_end or event_date}."
            )
        )

    # ── CRYPTO PRICE DAILY/MONTHLY/THRESHOLD ─────────────────────────────────
    if cat in {"crypto_price_daily", "crypto_price_monthly", "crypto_threshold"}:
        from oracle_core.classify import COINGECKO_IDS
        import calendar as _cal
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td

        # Get asset from MarketClass passed by pipeline, or detect from question.
        detected_asset = str(asset or "").upper().strip()
        if not detected_asset:
            q_asset = str(question or "").lower()
            for sym in COINGECKO_IDS:
                if re.search(rf"\b{re.escape(str(sym).lower())}\b", q_asset):
                    detected_asset = str(sym).upper()
                    break
            if not detected_asset:
                for name, sym in [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]:
                    if name in q_asset:
                        detected_asset = sym
                        break

        coin_id = COINGECKO_IDS.get(detected_asset)
        if not detected_asset or not coin_id:
            return FetchResult(
                is_valid=False,
                validation_reason="crypto price: could not detect asset from question",
            )

        # Build date window from classifier output.
        start_date = (window_start or event_date or "")[:10]
        end_date = (window_end or event_date or "")[:10]

        # If monthly window was not set by classify.py, infer it from the question month.
        if not start_date:
            months = {
                "january": "01", "february": "02", "march": "03", "april": "04",
                "may": "05", "june": "06", "july": "07", "august": "08",
                "september": "09", "october": "10", "november": "11", "december": "12",
            }
            base_year = (event_date or close_time or "2026")[:4]
            q_month = str(question or "").lower()
            for mname, mnum in months.items():
                if mname in q_month:
                    last = _cal.monthrange(int(base_year), int(mnum))[1]
                    start_date = f"{base_year}-{mnum}-01"
                    end_date = f"{base_year}-{mnum}-{last:02d}"
                    break

        if not end_date:
            end_date = start_date

        if not start_date:
            return FetchResult(
                is_valid=False,
                validation_reason="crypto price: could not determine date window",
            )

        # Determine the required metric.
        q = str(question or "").lower()
        if any(k in q for k in ["highest", "high", "peak", "max", "how high", "at any point", "reach"]):
            metric = "high"
        elif any(k in q for k in ["lowest", "low", "min", "bottom"]):
            metric = "low"
        else:
            metric = "close"

        print(f"[fetch] CoinGecko {detected_asset} {metric} {start_date}→{end_date}")

        try:
            start_dt = _dt.fromisoformat(start_date).replace(tzinfo=_tz.utc)
            end_dt = _dt.fromisoformat(end_date).replace(tzinfo=_tz.utc) + _td(days=1)
            r = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart/range",
                params={
                    "vs_currency": "usd",
                    "from": int(start_dt.timestamp()),
                    "to": int(end_dt.timestamp()),
                },
                headers={"User-Agent": "OracleREE/2.0"},
                timeout=20,
            )
            r.raise_for_status()
            prices = [(int(p[0]), float(p[1])) for p in r.json().get("prices", [])]
            if not prices:
                return FetchResult(
                    is_valid=False,
                    validation_reason=f"CoinGecko: no price data for {detected_asset} {start_date}–{end_date}",
                )

            if metric == "high":
                ts, value = max(prices, key=lambda x: x[1])
            elif metric == "low":
                ts, value = min(prices, key=lambda x: x[1])
            else:
                ts, value = prices[-1]

            when = _dt.fromtimestamp(ts / 1000, tz=_tz.utc).strftime("%Y-%m-%d")
            content = (
                f"ANSWER: {detected_asset} {metric} price between {start_date} and {end_date} "
                f"was ${value:,.2f} on {when}\n\n"
                f"asset: {detected_asset}\n"
                f"coin_id: {coin_id}\n"
                f"metric: {metric}\n"
                f"value_usd: {value:.8f}\n"
                f"date: {when}\n"
                f"window_start: {start_date}\n"
                f"window_end: {end_date}\n"
                f"sample_count: {len(prices)}\n"
            )
            print(f"[fetch] ✓ CoinGecko {detected_asset} {metric}: ${value:,.2f} on {when}")
            return FetchResult(
                content=content,
                source="CoinGecko",
                method="coingecko_range",
                is_valid=True,
                validation_reason=f"{detected_asset} {metric} ${value:,.2f} on {when}",
            )

        except Exception as e:
            return FetchResult(
                is_valid=False,
                validation_reason=f"CoinGecko fetch failed: {e}",
            )


    # ── SPREAD MARKETS ───────────────────────────────────────────────────────
    if cat == "spread":
        tavily_key = _env("TAVILY_API_KEY")
        if not tavily_key:
            return FetchResult(
                is_valid=False,
                validation_reason="spread: TAVILY_API_KEY missing",
            )

        # Extract team names from spread outcomes e.g. "Kings +10.5" → "Kings"
        import re as _re
        teams = []
        for o in (outcomes or []):
            t = _re.sub(r"\s*[+-]\d+(?:\.\d+)?\s*$", "", str(o)).strip()
            if t:
                teams.append(t)
        team_query = " vs ".join(teams) if teams else question

        tavily_query = f"{team_query} final score result {event_date}"
        print(f"[fetch] Spread market — Tavily: {tavily_query[:80]}")

        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_key,
                    "query": tavily_query,
                    "search_depth": "basic",
                    "max_results": 5,
                    "include_answer": True,
                },
                timeout=15,
            )
            data = r.json()
            parts = []
            if data.get("answer"):
                parts.append("ANSWER: " + str(data.get("answer")))
            for res in data.get("results", [])[:4]:
                parts.append(
                    f"[{res.get('url', '')}]\n"
                    f"{str(res.get('content', ''))[:800]}"
                )
            content = "\n\n".join(p for p in parts if str(p).strip()).strip()
            if content and len(content) > 80:
                print(f"[fetch] Spread: {len(content)} chars")
                return FetchResult(
                    content=content,
                    source="Tavily",
                    method="tavily_spread",
                    is_valid=True,
                    validation_reason="spread market Tavily search",
                )
        except Exception as e:
            print(f"[fetch] Spread Tavily failed: {e}")

        return FetchResult(
            is_valid=False,
            validation_reason="spread: no content found",
        )

    # ── NAMED CHOICE (Eurovision, awards, elections, winners, etc.) ──────────
    if cat == "named_choice":
        print("[fetch] Named choice market — broad Tavily search")

        tavily_key = _env("TAVILY_API_KEY")
        if not tavily_key:
            return FetchResult(
                is_valid=False,
                validation_reason="Named choice: TAVILY_API_KEY missing",
            )

        # source_url may be a short source name like "Eurovision", not a URL/domain.
        # Do NOT use site:eurovision. For named-choice markets we intentionally use
        # broad Tavily because the result can be reported by multiple pages, while
        # extraction + outcome matching still happen downstream.
        source_hint = str(source_url or "").strip()
        source_hint_l = source_hint.lower().replace(" ", "")

        source_aliases = {
            "eurovision": "eurovision.tv",
            "esc": "eurovision.tv",
            "uefa": "uefa.com",
            "fifa": "fifa.com",
            "espn": "espn.com",
            "nfl": "nfl.com ESPN",
            "nba": "nba.com ESPN",
            "mlb": "mlb.com ESPN",
            "nhl": "nhl.com ESPN",
        }
        source_hint = source_aliases.get(source_hint_l, source_hint)

        # Build a broad query. Avoid site: because short source names like
        # "eurovision" are not valid domains and caused false INCONCLUSIVE.
        tavily_query = f"{source_hint} {question} winner result official {event_date}".strip()

        try:
            r = requests.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": tavily_key,
                    "query": tavily_query,
                    "search_depth": "basic",
                    "max_results": 8,
                    "include_answer": True,
                },
                timeout=20,
            )
            data = r.json()
            parts = []

            if data.get("answer"):
                parts.append("ANSWER: " + str(data.get("answer")))

            for res in data.get("results", [])[:8]:
                res_url = str(res.get("url") or "")
                res_text = str(res.get("content") or "")[:900]
                if res_text.strip():
                    parts.append("[" + res_url + "]\n" + res_text)

            content_str = "\n\n".join(parts).strip()

            if content_str and len(content_str) > 80:
                print(f"[fetch] Named choice: {len(content_str)} chars")
                return FetchResult(
                    content=content_str,
                    source="Tavily broad",
                    method="tavily_named_choice_broad",
                    is_valid=True,
                    validation_reason="named choice: broad Tavily search",
                )

            return FetchResult(
                is_valid=False,
                validation_reason="Named choice: Tavily returned no usable content",
            )

        except Exception as e:
            return FetchResult(
                is_valid=False,
                validation_reason=f"Named choice Tavily failed: {e}",
            )

    # ── CRYPTO / FINANCE PRICE ────────────────────────────────────────────────
    if cat in {"crypto_price", "crypto_price_range", "finance", "economy_finance"}:
        print(f"[fetch] Price market — direct API then Tavily")

        if any(x in source_url.lower() for x in ["/api/", "history", "/price", "coingecko", "yahoo"]):
            direct = _direct_fetch(source_url)
            if direct:
                raw, _ = direct
                if re.search(r"\b\d{3,6}(?:\.\d+)?\b", raw):
                    return FetchResult(
                        content=raw, source=domain, method="direct_api",
                        is_valid=True, validation_reason="price API response contains numeric value",
                    )

        content = _tavily_fetch(domain, question, event_date, query, search_depth="advanced")
        if content and re.search(r"\b\d{3,6}(?:,\d{3})?(?:\.\d+)?\b", content):
            return FetchResult(
                content=content, source=domain, method="tavily_price",
                is_valid=True, validation_reason="price value found",
            )

        return FetchResult(is_valid=False, validation_reason=f"Price: no numeric value from {domain}")

    # ── COUNT MARKETS ─────────────────────────────────────────────────────────
    if cat in {"count", "numeric_count"}:
        print(f"[fetch] Count market")
        content = _tavily_fetch(domain, question, event_date, query, search_depth="advanced")
        if content and re.search(r"\b\d+\b", content):
            return FetchResult(
                content=content, source=domain, method="tavily_count",
                is_valid=True, validation_reason="count evidence found",
            )
        return FetchResult(is_valid=False, validation_reason=f"Count: no count evidence from {domain}")

    # ── GENERIC FALLBACK ──────────────────────────────────────────────────────
    print(f"[fetch] Generic market — Tavily then direct")

    content = _tavily_fetch(domain, question, event_date, query, search_depth="advanced")
    if content and len(content.strip()) > 80:
        return FetchResult(
            content=strip_to_body_text(content), source=domain,
            method="tavily_generic", is_valid=True, validation_reason="content found via Tavily",
        )

    direct = _direct_fetch(source_url)
    if direct:
        raw, ctype = direct
        text = strip_to_body_text(raw) if ctype == "html" else raw
        if len(text.strip()) > 100:
            return FetchResult(
                content=text, source=domain, method="direct_generic",
                is_valid=True, validation_reason="content found via direct fetch",
                raw_html=raw if ctype == "html" else "",
            )

    return FetchResult(is_valid=False, validation_reason=f"No content found from {domain}")


def _fetch_espn_scoreboard(question: str, event_date: str, outcomes: list) -> Optional[FetchResult]:
    """
    ESPN Soccer Scoreboard page — free, no key, covers ALL competitions worldwide.
    Fetches https://www.espn.com/soccer/scoreboard/_/date/YYYYMMDD
    Extracts match winner using HTML class markers:
      ScoreboardScoreCell__Item--winner / --loser
    Falls back to match page JS extraction using the gameId found in the scoreboard.
    """
    try:
        team1, team2 = _teams_from_question(question, outcomes)
        if not team1 or not team2:
            return None

        date_compact = event_date.replace("-", "")
        url = f"https://www.espn.com/soccer/scoreboard/_/date/{date_compact}"
        print(f"[fetch] ESPN scoreboard: {url}")

        html = None
        for ua in [
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
        ]:
            try:
                r = requests.get(url, headers={"User-Agent": ua}, timeout=15)
                if r.ok and len(r.text) > 10000:
                    html = r.text
                    break
            except Exception:
                continue

        if not html or is_espn_homepage(html):
            return None

        html_l = html.lower()
        t1_idx = html_l.find(team1.lower())
        t2_idx = html_l.find(team2.lower())

        # If teams not found try alias expansion
        if t1_idx < 0:
            t1_idx = html_l.find(_expand_alias(team1).lower())
        if t2_idx < 0:
            t2_idx = html_l.find(_expand_alias(team2).lower())

        if t1_idx < 0 or t2_idx < 0:
            print(f"[fetch] ESPN scoreboard: teams not found in page")
            return None

        window_start = max(0, min(t1_idx, t2_idx) - 3000)
        window_end = max(t1_idx, t2_idx) + 3000
        window = html[window_start:window_end]

        # Extract winner from CSS class markers
        winner_m = re.search(
            r'ScoreboardScoreCell__Item--winner[^>]*>.*?ScoreCell__TeamName[^>]*>([^<]+)<',
            window, re.S
        )
        loser_m = re.search(
            r'ScoreboardScoreCell__Item--loser[^>]*>.*?ScoreCell__TeamName[^>]*>([^<]+)<',
            window, re.S
        )

        winner_name = winner_m.group(1).strip() if winner_m else None
        loser_name = loser_m.group(1).strip() if loser_m else None

        # Extract scores
        scores = re.findall(r'ScoreCell_Score--scoreboard[^>]*>(\d+)</div>', window)

        if winner_name:
            matched = _match_outcome_name(winner_name, outcomes)
            if matched:
                score_str = f"{scores[0]}-{scores[1]}" if len(scores) >= 2 else "?"
                evidence = (
                    f"ANSWER: {winner_name} won. Winner: {winner_name}\n\n"
                    f"[ESPN Scoreboard]\n"
                    f"Match: {team1} vs {team2}\n"
                    f"Date: {event_date}\n"
                    f"Score: {score_str}\n"
                    f"Winner: {winner_name}\n"
                    f"Loser: {loser_name or 'unknown'}\n"
                )
                print(f"[fetch] ✓ ESPN scoreboard: {winner_name} won ({score_str})")
                return FetchResult(
                    content=evidence, source="espn.com",
                    method="espn_scoreboard", is_valid=True,
                    validation_reason=f"ESPN scoreboard: {winner_name} won",
                )

        # Fallback: find gameId and use match page JS extractor
        game_id_m = re.search(
            r'/soccer/match/_/gameId/(\d+)/[^\s"\']+', window
        )
        if game_id_m:
            game_id = game_id_m.group(1)
            match_url = f"https://www.espn.com/soccer/match/_/gameId/{game_id}"
            print(f"[fetch] ESPN scoreboard -> match page: {match_url}")
            direct = _direct_fetch(match_url)
            if direct:
                raw, _ = direct
                if not is_espn_homepage(raw):
                    espn_evidence = extract_espn_score_from_html(raw, question, outcomes)
                    if espn_evidence:
                        print(f"[fetch] ✓ ESPN scoreboard -> match JS extraction")
                        return FetchResult(
                            content=espn_evidence, source="espn.com",
                            method="espn_scoreboard_match_page", is_valid=True,
                            validation_reason="ESPN scoreboard -> match page JS extraction",
                            raw_html=raw,
                        )

    except Exception as e:
        print(f"[fetch] ESPN scoreboard failed: {e}")
    return None