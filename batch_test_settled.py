#!/usr/bin/env python3
"""
batch_test.py — OracleREE batch accuracy tester

Fetches N settled Delphi markets, runs oracle_ree.py on each,
compares OracleREE result vs creator result, and prints a summary.

Usage:
    python3 batch_test.py --count 20
    python3 batch_test.py --count 10 --category sports
    python3 batch_test.py --output results.json
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─── Load .env.local ─────────────────────────────────────────────────────────
env_file = Path(__file__).parent / ".env.local"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

DELPHI_API = "https://api.delphi.fyi"
DELPHI_API_KEY = os.environ.get("DELPHI_API_ACCESS_KEY", "")
ORACLE_SCRIPT = Path(__file__).parent / "oracle_ree.py"


# ─── Fetch settled markets ────────────────────────────────────────────────────

def fetch_settled_markets(count: int = 20, category: str = "") -> list[dict]:
    """Fetch recently settled Delphi markets."""
    print(f"[batch] Fetching {count} settled markets...")
    try:
        params = {
            "status": "settled",
            "limit": min(count * 3, 100),  # fetch more to allow filtering
            "offset": 0,
        }
        r = requests.get(f"{DELPHI_API}/markets", params=params,
            headers={"x-api-key": DELPHI_API_KEY}, timeout=15)
        r.raise_for_status()
        data = r.json()
        markets = data.get("markets") or data.get("data") or data.get("results") or []

        if not markets:
            # Try alternate endpoint
            r2 = requests.get(f"{DELPHI_API}/v1/markets", params=params,
                headers={"x-api-key": DELPHI_API_KEY}, timeout=15)
            r2.raise_for_status()
            data2 = r2.json()
            markets = data2.get("markets") or data2.get("data") or []

        print(f"[batch] Got {len(markets)} markets from API")

        # Filter to markets with a clear creator result
        valid = []
        for m in markets:
            # Delphi API: metadata nested under "metadata" key
            meta = m.get("metadata") or {}

            # winningOutcomeIdx is a string "0", "1", etc.
            winning_idx_raw = m.get("winningOutcomeIdx")
            if winning_idx_raw is None:
                continue
            try:
                winning_idx = int(winning_idx_raw)
            except (ValueError, TypeError):
                continue

            outcomes = meta.get("outcomes") or m.get("outcomes") or []
            if not outcomes or winning_idx >= len(outcomes):
                continue

            creator_result = outcomes[winning_idx]
            question = meta.get("question") or m.get("question") or ""
            prompt_context = (meta.get("model") or {}).get("prompt_context") or ""

            m["_creator_result"] = creator_result
            m["_winning_idx"] = winning_idx
            m["_question"] = question
            m["_prompt_context"] = prompt_context

            # Optional category filter
            if category:
                q = question.lower()
                pc = prompt_context.lower()
                sport_keywords = ["premier league", "fa cup", "champions league", "nba", "nfl", "ipl", "psl", "cricket", "vs ", " v "]
                crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "microstrategy", "strategy"]
                if category == "sports" and not any(k in q or k in pc for k in sport_keywords):
                    continue
                if category == "crypto" and not any(k in q or k in pc for k in crypto_keywords):
                    continue

            valid.append(m)
            if len(valid) >= count:
                break

        print(f"[batch] {len(valid)} markets with valid creator results")
        return valid[:count]

    except Exception as e:
        print(f"[batch] API fetch failed: {e}")
        return []


# ─── Run oracle on single market ─────────────────────────────────────────────

def run_oracle(market: dict, timeout: int = 120) -> dict:
    """Run oracle_ree.py --settle on a single market, return result dict."""
    market_id = market.get("id") or market.get("marketId") or ""
    question = market.get("_question") or market.get("question") or market.get("title") or ""
    creator_result = market.get("_creator_result", "")

    result = {
        "market_id": market_id,
        "question": question[:80],
        "creator_result": creator_result,
        "oracle_result": None,
        "status": "pending",
        "elapsed": 0,
        "error": None,
        "proof_file": None,
    }

    if not market_id:
        result["status"] = "error"
        result["error"] = "no market_id"
        return result

    # Delete old proof files for this market to force a fresh run
    for old_proof in Path(ORACLE_SCRIPT.parent).glob(f"oracle_proof_{market_id[:10]}*.json"):
        old_proof.unlink()

    t0 = time.time()
    try:
        cmd = [
            sys.executable, str(ORACLE_SCRIPT),
            "--market", market_id,
            "--oracle-only",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,
            cwd=str(ORACLE_SCRIPT.parent),
        )
        result["elapsed"] = round(time.time() - t0, 1)

        # Find the proof file
        proof_files = sorted(
            Path(ORACLE_SCRIPT.parent).glob(f"oracle_proof_{market_id[:10]}*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if proof_files:
            result["proof_file"] = str(proof_files[0])
            try:
                proof = json.loads(proof_files[0].read_text())
                oracle_outcome = (
                    proof.get("oracle_result")
                    or proof.get("oracle_outcome")
                    or proof.get("final_outcome")
                    or proof.get("matched_outcome")
                    or ""
                )
                result["oracle_result"] = oracle_outcome
                result["fetch_method"] = (
                    (proof.get("oracle_evidence") or {})
                    .get("source_results", [{}])[0]
                    .get("fetch_method", "")
                )
            except Exception as e:
                result["error"] = f"proof parse error: {e}"

        # Fallback: parse from stdout
        if not result["oracle_result"]:
            for line in (proc.stdout + proc.stderr).splitlines():
                if "oracle_result" in line.lower() or "final_outcome" in line.lower():
                    result["oracle_result"] = line.strip()
                    break

        if proc.returncode != 0 and not result["oracle_result"]:
            result["status"] = "error"
            result["error"] = proc.stderr[-500:] if proc.stderr else "non-zero exit"
        else:
            result["status"] = "done"

    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["elapsed"] = timeout
        result["error"] = f"timed out after {timeout}s"
    except Exception as e:
        result["status"] = "error"
        result["elapsed"] = round(time.time() - t0, 1)
        result["error"] = str(e)

    return result


# ─── Classify result ─────────────────────────────────────────────────────────

def classify(oracle: str, creator: str) -> str:
    if not oracle or oracle.upper() == "INCONCLUSIVE":
        return "INCONCLUSIVE"
    if oracle.strip().lower() == creator.strip().lower():
        return "MATCH"
    return "WRONG"


# ─── Print report ─────────────────────────────────────────────────────────────

def print_report(results: list[dict]) -> None:
    match = [r for r in results if classify(r["oracle_result"], r["creator_result"]) == "MATCH"]
    wrong = [r for r in results if classify(r["oracle_result"], r["creator_result"]) == "WRONG"]
    inconclusive = [r for r in results if classify(r["oracle_result"], r["creator_result"]) == "INCONCLUSIVE"]
    errors = [r for r in results if r["status"] in ("error", "timeout")]

    total = len(results)
    print("\n" + "═" * 70)
    print(f"  ORACLEREE BATCH TEST RESULTS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("═" * 70)
    print(f"  Total markets tested : {total}")
    print(f"  ✓ MATCH              : {len(match)}  ({100*len(match)//total if total else 0}%)")
    print(f"  ✗ WRONG              : {len(wrong)}  ({100*len(wrong)//total if total else 0}%)")
    print(f"  ~ INCONCLUSIVE       : {len(inconclusive)}  ({100*len(inconclusive)//total if total else 0}%)")
    print(f"  ! ERRORS/TIMEOUT     : {len(errors)}")
    print("═" * 70)

    if match:
        print(f"\n{'─'*70}")
        print("  ✓ MATCHES")
        print(f"{'─'*70}")
        for r in match:
            print(f"  [{r['elapsed']}s] {r['question'][:60]}")
            print(f"         Oracle: {r['oracle_result']}  Creator: {r['creator_result']}")

    if wrong:
        print(f"\n{'─'*70}")
        print("  ✗ WRONG ANSWERS")
        print(f"{'─'*70}")
        for r in wrong:
            print(f"  [{r['elapsed']}s] {r['question'][:60]}")
            print(f"         Oracle: {r['oracle_result']}  Creator: {r['creator_result']}")

    if inconclusive:
        print(f"\n{'─'*70}")
        print("  ~ INCONCLUSIVE")
        print(f"{'─'*70}")
        for r in inconclusive:
            print(f"  [{r['elapsed']}s] {r['question'][:60]}")
            print(f"         Creator: {r['creator_result']}  Method: {r.get('fetch_method','')}")
            if r.get("error"):
                print(f"         Error: {r['error'][:100]}")

    if errors:
        print(f"\n{'─'*70}")
        print("  ! ERRORS")
        print(f"{'─'*70}")
        for r in errors:
            print(f"  [{r['status']}] {r['question'][:60]}")
            print(f"         Error: {r.get('error','')[:100]}")

    print("\n" + "═" * 70)

    # Pattern analysis
    if inconclusive:
        print("\n  INCONCLUSIVE PATTERNS:")
        fetch_methods = {}
        for r in inconclusive:
            m = r.get("fetch_method") or "unknown"
            fetch_methods[m] = fetch_methods.get(m, 0) + 1
        for method, count in sorted(fetch_methods.items(), key=lambda x: -x[1]):
            print(f"    {method}: {count}")

    print("═" * 70 + "\n")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OracleREE batch accuracy tester")
    parser.add_argument("--count", type=int, default=20, help="Number of markets to test")
    parser.add_argument("--category", default="", help="Filter: sports, crypto, or empty for all")
    parser.add_argument("--output", default="", help="Save results to JSON file")
    parser.add_argument("--timeout", type=int, default=90, help="Timeout per market in seconds")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay between markets in seconds")
    args = parser.parse_args()

    markets = fetch_settled_markets(args.count, args.category)
    if not markets:
        print("[batch] No markets found. Check your Delphi API connection.")
        sys.exit(1)

    results = []
    for i, market in enumerate(markets, 1):
        q = market.get("question") or market.get("title") or ""
        print(f"\n[{i}/{len(markets)}] {q[:70]}")
        print(f"       Creator: {market['_creator_result']} | ID: {(market.get('id') or '')[:12]}")

        result = run_oracle(market, timeout=args.timeout)
        verdict = classify(result["oracle_result"], result["creator_result"])
        icon = {"MATCH": "✓", "WRONG": "✗", "INCONCLUSIVE": "~"}.get(verdict, "!")
        print(f"       Oracle: {result['oracle_result']} → {icon} {verdict} [{result['elapsed']}s]")

        results.append(result)

        if i < len(markets):
            time.sleep(args.delay)

    print_report(results)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total": len(results),
            "match": sum(1 for r in results if classify(r["oracle_result"], r["creator_result"]) == "MATCH"),
            "wrong": sum(1 for r in results if classify(r["oracle_result"], r["creator_result"]) == "WRONG"),
            "inconclusive": sum(1 for r in results if classify(r["oracle_result"], r["creator_result"]) == "INCONCLUSIVE"),
            "results": results,
        }, indent=2))
        print(f"Results saved to {output_path}")


if __name__ == "__main__":
    main()