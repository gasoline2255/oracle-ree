# 🔒 OracleREE

**Trustless settlement verification and deterministic evidence infrastructure for [Gensyn Delphi](https://app.delphi.fyi) information markets**

OracleREE and OracleSeal work together to make Delphi market settlement more transparent, reproducible, and independently auditable.

Delphi markets already let creators configure settlement prompts, approved data sources, and AI-based settlement logic at market creation. The remaining challenge is verification: participants need a way to confirm that the correct prompt was used, the correct sources were followed, the right evidence was captured, and the final outcome was resolved correctly.

OracleREE handles settlement verification and reproducible inference.
OracleSeal handles close-time evidence capture and public settlement transparency.

Together, they reduce trust in the market creator and make settlement provable instead of purely trust-based.

---

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

# The Problem

## What Delphi Provides

✅ Market creators configure settlement prompts and approved data sources at market creation

✅ Markets support both verifiable and non-verifiable settlement models

✅ Creators execute settlement at close time and submit the result to Delphi

✅ Creator prompts and approved sources are locked at market creation

---

## What's Missing

❌ No way to verify whether the submitted settlement prompt matches the originally locked prompt

❌ No way to verify whether settlement used the originally approved data sources

❌ No independent verification for most non-verifiable model settlements

❌ No protection against mutable external data updating after market close

❌ No publicly auditable record of what evidence existed at the exact close timestamp

---

## Result

Settlement becomes a black box for most markets.

Participants cannot independently verify:

* which settlement prompt was executed
* which evidence sources were used
* what data existed at close time
* whether the final outcome was resolved correctly

OracleREE and OracleSeal solve this by combining frozen evidence snapshots, source-locked verification, prompt integrity checks, and reproducible settlement execution.

---

# The Solution

## OracleREE

OracleREE is the settlement verification engine.

It verifies that settlement follows the original market configuration and produces a reproducible proof of the result.

OracleREE verifies:

* the market URL
* the official settlement prompt
* the creator-approved data sources
* the resolved market outcome
* the REE execution receipt

To run settlement verification, users provide the market URL and settlement prompt. OracleREE checks that they match the configuration locked when the market was created.

```text
Market verified: ✓
Settlement prompt verified: ✓
Locked sources verified: ✓
```

If the prompt or approved sources do not match, verification fails before settlement execution continues.

---

## OracleSeal

OracleSeal is the evidence capture and transparency layer.

It watches Delphi markets and captures evidence from creator-approved sources at the exact market close timestamp.

Captured evidence is:

* timestamped
* hashed
* pinned to IPFS
* stored before settlement execution
* displayed publicly on the OracleSeal dashboard

This allows OracleREE to verify settlement using frozen close-time evidence instead of mutable live data.

---

# How It Works

```text
Market closes
    │
    ├── OracleSeal watcher captures evidence
    │   from creator-approved sources
    │
    ├── Evidence is timestamped, hashed, and pinned to IPFS
    │
    └── oracle_ree.py runs
        ├── Verifies the locked market configuration
        ├── Loads frozen evidence from OracleSeal
        ├── Runs classify → fetch → extract → resolve
        ├── Executes settlement through REE
        └── Generates a cryptographic receipt
```

---

# Verification Modes

## [1] OracleREE Proof

Used to verify whether a market was settled correctly.

```text
Prompt verified: ✓
Locked sources verified: ✓

Oracle result:   Outcome A
Creator result:  Outcome A

MATCH ✓
```

```text
Prompt verified: ✓
Locked sources verified: ✓

Oracle result:   Outcome A
Creator result:  Outcome B

MISMATCH ✗
```

---

## [2] Settle Market

Used by market creators to generate a verified settlement result before submitting the outcome to Delphi.

```text
Prompt verified: ✓
Frozen evidence: ✓
REE receipt: ✓

Settlement result:
→ Outcome A
```

---

# OracleSeal Dashboard

OracleSeal provides a public view of captured markets, frozen evidence, and REE verification status.

## Market Statuses

| Status         | Description                                       |
| -------------- | ------------------------------------------------- |
| `OPEN`         | Market is active                                  |
| `CAPTURED`     | Evidence was frozen at close time                 |
| `REE VERIFIED` | Settlement proof was generated                    |
| `INCONCLUSIVE` | Evidence was unavailable or could not be resolved |

---

## Stored Settlement Artifacts

OracleSeal stores:

* evidence snapshots
* capture timestamps
* evidence hashes
* IPFS CIDs
* oracle outputs
* REE receipt hashes

These artifacts allow anyone to inspect what evidence existed at market close and how the settlement result was produced.

---

# Why Frozen Evidence Matters

Settlement should use the data that existed at the actual market close timestamp.

Some external data providers can revise or update historical values after a market closes. Without close-time evidence capture, a market may settle against data that did not exist when trading ended.

OracleSeal prevents this by freezing evidence before settlement execution.

This creates:

* deterministic settlement inputs
* immutable settlement evidence
* reproducible verification
* transparent auditability

---

# Oracle Pipeline

```text
classify → fetch → extract → resolve
```

---

# Links

* **OracleSeal Dashboard:** https://oracle-seal.vercel.app
* **Gensyn Delphi:** https://app.delphi.fyi
* **Twitter/X:** https://x.com/gasoline2255

---

**Built by [gasoline](https://x.com/gasoline2255)** | **Gensyn community** | **Built on [Gensyn REE](https://gensyn.ai/ree)**
