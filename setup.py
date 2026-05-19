#!/usr/bin/env python3
"""
OracleREE Setup — run once to configure your environment.
Gets you set up in under 2 minutes.
"""
from pathlib import Path

env_file = Path(__file__).parent / ".env.local"

print()
print("=" * 60)
print("OracleREE Setup")
print("=" * 60)
print()

if env_file.exists():
    print("✓ .env.local already exists")
    overwrite = input("Overwrite? (y/N): ").strip().lower()
    if overwrite != "y":
        print("Setup cancelled.")
        raise SystemExit(0)

print("Step 1 of 3 — Delphi API Key (required)")
print("Get your free key at: https://api-access.delphi.fyi/")
print()
delphi_key = input("Paste DELPHI_API_ACCESS_KEY: ").strip()
if not delphi_key:
    print("Error: Delphi API key is required.")
    raise SystemExit(1)

print()
print("Step 2 of 3 — Groq API Key (optional — better market classification)")
print("Get a free key at: https://console.groq.com")
print()
groq_key = input("Paste GROQ_API_KEY (or Enter to skip): ").strip()

print()
print("Step 3 of 3 — Pinata JWT (optional — IPFS evidence pinning)")
print("Get a free key at: https://app.pinata.cloud")
print()
pinata_key = input("Paste PINATA_JWT (or Enter to skip): ").strip()

lines = [
    f"DELPHI_API_ACCESS_KEY={delphi_key}",
    "DELPHI_NETWORK=mainnet",
]
if groq_key:
    lines.append(f"GROQ_API_KEY={groq_key}")
if pinata_key:
    lines.append(f"PINATA_JWT={pinata_key}")

env_file.write_text("\n".join(lines) + "\n")

print()
print("=" * 60)
print("✓ Setup complete!")
print()
print("Run OracleREE:")
print("  python3 oracle_ree.py --market 0xabc123...")
print()
print("Run original REE:")
print("  python3 ree.py")
print("=" * 60)
print()
