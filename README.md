# OracleREE + OracleSeal

**Trustless settlement verification for Gensyn Delphi prediction markets.**

OracleREE and OracleSeal are two components of the same product. OracleREE is the local settlement engine. OracleSeal is the public transparency layer. Together they make every part of Delphi market settlement independently auditable.

Oracle REE proves the judge ran correctly. OracleSeal proves the judge had correct data. Together they make Delphi trustless.

---

## The Problem
 
Delphi markets let creators configure the judge model, settlement prompt, and data sources at market creation. That configuration is locked. But there is no way to verify it was actually followed at settlement time.

When a creator submits a settlement result, participants have no way to confirm:

- Whether the submitted prompt matches the one locked at creation
- Whether settlement fetched from the exact data sources specified at creation or substituted different ones
- Whether the data used actually existed at close time
- Whether the final outcome was resolved correctly

**Settlement becomes a black box. Participants are left trusting the creator.**
 
A further problem: some external data providers silently revise historical values after a market closes. Without a snapshot taken at the exact close timestamp, settlement can be run against data that did not exist when trading ended. There is no way to prove what the data said at close time or that it was not updated afterward.


## The Solution
 
OracleREE and OracleSeal solve this at two levels.

**OracleSeal** is the evidence capture layer. A watcher runs every five minutes and monitors open Delphi markets. When a market closes, OracleSeal immediately fetches evidence from the creator's approved sources at that exact timestamp, hashes it, and pins it to IPFS. The snapshot is immutable from that point forward. No post-close data updates can affect it.

**OracleREE** is the settlement verification engine. It takes a Delphi market, verifies that the settlement prompt matches the one locked at creation, loads the frozen evidence snapshot from OracleSeal, runs a full classify → fetch → extract → resolve pipeline, and executes the result through Gensyn's REE. The output is a cryptographic receipt that proves the correct prompt was used, the correct sources were followed, and the correct evidence was present, all in a single verifiable artifact.



# Quick Start

## OracleSeal Dashboard

**Dashboard:** https://oracle-seal.vercel.app

View captured markets, frozen evidence, IPFS proofs, and REE verification status.

---

## Run OracleREE

```bash
git clone https://github.com/gasoline2255/oracle-ree.git
cd oracle-ree
python3 ree.py
```

---
 
## What This Adds to Gensyn REE
 
Gensyn REE already proves that a model ran correctly and produced a specific output. What it does not prove is whether the input to that model was correct — whether the right prompt was used, the right sources were fetched, and the right data existed at the time of settlement.
 
OracleREE wraps REE with that missing layer:
 
| What REE proves | What OracleREE adds |
|---|---|
| The model ran correctly | The correct prompt was used |
| The output hash is valid | The approved sources were followed |
| The receipt is reproducible | The evidence existed at close time |
| | The combined proof is publicly auditable |
 
OracleSeal provides the public record. Every captured market, every frozen snapshot, every IPFS CID, and every REE-verified settlement is visible on the dashboard. Anyone can inspect what evidence existed at market close and verify how the outcome was produced.
 
---
 
## Market Status Flow
 
```
OPEN → CAPTURED → REE VERIFIED
```
 
| Status | Description |
|---|---|
| `OPEN` | Market is active, watcher is monitoring |
| `CAPTURED` | Evidence frozen at close time, pinned to IPFS |
| `REE VERIFIED` | Settlement proof generated and anchored to REE |
| `INCONCLUSIVE` | Evidence was unavailable or could not be resolved |
 
---
 
## Why It Matters
 
For **market participants**: settlement is no longer trust-based. Anyone can verify that the correct configuration was followed and that the evidence used was frozen at close time.
 
For **market creators**: a verified settlement proof provides a public record that the process was followed correctly, reducing disputes.
 
For **the Gensyn ecosystem**: every settled market becomes a publicly auditable data point — what the question was, what evidence existed, what the pipeline resolved, and what REE confirmed.
 
---
 
## Architecture
 
```
oracle-ree/              ← Local settlement engine (this repo)
├── ree.py               ← TUI — interactive settlement dashboard
├── oracle_ree.py        ← Core verification and settlement engine
└── oracle_core/         ← classify → fetch → extract → resolve pipeline
 
OracleSeal (separate repo)
├── watcher/watcher.py   ← Evidence capture (GitHub Actions, every 5 min)
├── app/page.tsx         ← Public transparency dashboard
└── app/api/             ← Market feed and captures API
```
 
---
 
## Links
 
- OracleSeal Dashboard: [oracle-seal.vercel.app](https://oracle-seal.vercel.app)
- OracleSeal repo: [github.com/gasoline2255/OracleSeal](https://github.com/gasoline2255/OracleSeal)
- Gensyn Delphi: [app.delphi.fyi](https://app.delphi.fyi)
- Built by [gasoline](https://x.com/gasoline2255) · Gensyn community · Built on Gensyn REE
