# GrantLens

GrantLens is a human-in-the-loop assistant for assessing grant eligibility. It reads an application against a versioned set of criteria, drafts a per-criterion verdict with verbatim citations, and hands the decision to an authenticated officer. The language model runs locally through Ollama (default: Qwen3-8B), so application data never leaves the host.

The guiding principle is that the AI supplies source-checked evidence while people retain the decision. Every control that enforces this principle is implemented on the server; the web interface cannot bypass it.

## Table of contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Running the application](#running-the-application)
7. [Running the tests](#running-the-tests)
8. [Evaluation and measurement](#evaluation-and-measurement)
9. [Data](#data)
10. [Known limitations](#known-limitations)
11. [Deployment](#deployment)
12. [Project layout](#project-layout)

## Features

- Four-layer assessment pipeline: retrieval, fact extraction (LLM pass 1), judgement (LLM pass 2), and citation by code. The judgement pass never sees the original prose, so writing style cannot influence the verdict.
- Verbatim citations by construction. The model points at a chunk and a few key words; code cuts the matching sentence and string-matches it against the source.
- False-pass guards. After the LLM, code re-checks quantitative and pattern-checkable rules. Guards can downgrade a verdict to NOT MET or UNCLEAR but never upgrade to MET.
- Server-enforced review workflow: blind spot-check, one-by-one confirmation, mandatory reasons for overrides, attestation on criteria without a code safety net, manager countersignature on adverse outcomes, and post-audit sampling.
- SHA-256 hash-chained audit log. Editing or deleting any event breaks the chain.
- Authentication with HMAC-signed sessions and role separation (officer, manager, auditor). Identity is always taken from the session, never from the request body.
- Multi-fund rulesets with versioning. Each case locks a snapshot of the ruleset at first assessment.
- Denied-party screening against ASIC, DFAT, and ABR/ABN lists, presented as evidence rather than automatic rejection.
- Outcome letter drafting from an office template, with two-sided quotations, supplement and appeal sections.
- English-only web UI. The backend retains an optional locale layer for tests; the product interface does not expose a language switch.

When packaging for submission, zip this `grantlens/` directory only (omit `.git`). Sibling folders such as `_extras/` are not required to run the system.

## Architecture

```
frontend/index.html   Single-page web application
backend/
  app.py              FastAPI routes, streaming assessment progress (NDJSON)
  workflow.py         Case state machine and server-side trust rules
  core.py             Assessment pipeline: RAG -> EXTRACT -> JUDGE -> CITE -> GUARD
  guards.py           False-pass guards (hand-written per fund + rule compiler)
  verify.py           Citation by retrieval and string match
  rag.py              Paragraph chunking with offsets; TF-IDF (default) or BGE-M3 + FAISS
  llm.py              LLM backend: ollama (default), openai-compatible, or mock
  store.py            SQLite persistence and hash-chained audit log
  auth.py             Login, sessions, identity middleware, account CLI
  coi.py              Conflict-of-interest check against the officer register
  screening.py        ASIC / DFAT / ABN denied-party screening
  crosscheck.py       Cross-check of figures and dates across attached documents
  tables.py           PDF / DOCX extraction with table regions preserved
  casegen.py          Generated test sets, label review and approval, per-fund evaluation
  eval.py             Ground-truth evaluation (accuracy, citation, bias, false pass)
  feedback.py         Officer decisions to few-shot corrections and fine-tuning export
  i18n.py             Optional UI locale layer (locale_vi.py)
data/
  rulesets/           One criteria set per fund, versioned, with approval status
  applications/       Labelled synthetic applications (manifest.json) and generated test sets
  labels/             ground-truth.json and generated-<fund>.json
  officers.json       Officer register (roles, affiliations)
  letter-template.txt Office letter template
  external/           Screening lists (ASIC, DFAT, ABN)
tests/                Automated test suites
```

Case states: `new -> assessed -> in_review -> signed -> letter_drafted -> letter_approved`. A case may enter `awaiting_supplement` for a supplement round or be reopened with a reason.

| Step | Actor | Mechanism enforced by the server |
|---|---|---|
| Run AI assessment | AI | Two model calls per criterion; results are drafts with a confidence level; code guards block false passes |
| Start review | Officer | Blind spot-check on one random criterion; all AI results hidden until the officer answers |
| Confirm each criterion | Officer | No bulk approval; overriding the AI or an AI UNCLEAR / NOT ADDRESSED requires a reason; MET on a criterion without a code safety net requires a verbatim quotation checked by code |
| Sign off | Officer | Only when every criterion is confirmed; signing faster than the configured threshold triggers a warning and second confirmation |
| Countersign | Manager | Required for cases with NOT MET or UNCLEAR criteria; the manager cannot be the reviewer and is COI-checked |
| Draft the letter | AI | Only after sign-off; plain English, two-sided quotations, supplement and appeal sections |
| Approve the letter | Officer | Hand edits allowed and logged |
| Post-audit sampling | Manager / auditor | Random sample of MET confirmations on unguarded criteria; the sampling itself is logged |

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11 or later | Tested with Python 3.12 |
| pip | Any recent version |
| Ollama | Required for real assessments. Not required for the test suites or the mock demo mode. See https://ollama.com |
| GPU with 8 GB VRAM or more | Recommended for Qwen3-8B. Smaller GPUs can use a quantised variant (see Configuration) |
| Docker | Optional, for containerised deployment |

## Installation

Clone the repository and install the Python dependencies from the `grantlens` directory.

```bash
cd grantlens
python -m venv .venv
```

Activate the virtual environment.

```bash
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate
```

Install dependencies.

```bash
pip install -r requirements.txt
```

Optional: semantic retrieval with BGE-M3 and FAISS instead of TF-IDF.

```bash
pip install sentence-transformers faiss-cpu
```

Install Ollama (official HTTPS) and pull the assessment model — or skip this if you only intend to run tests / mock demo:

```bash
python _install_ollama.py          # downloads from ollama.com + ollama pull qwen3:8b
# manual alternative:
#   install from https://ollama.com  then:  ollama pull qwen3:8b
```

Start with the real model:

```bash
python _start_ollama.py
```

Two demo accounts are ready to use for signing in and testing the workflow:

| Username | Password | Role |
|---|---|---|
| `sarah.mitchell` | `demo-officer-123` | officer (reviews cases) |
| `david.thompson` | `demo-manager-123` | manager (countersigns rejections) |

To create random passwords for every account instead, or to reset accounts that have none, run:

```bash
python -m backend.auth init-demo
```

Passwords from `init-demo` are written to `data/demo-accounts.txt`, which is excluded from version control.

To manage accounts manually, edit `data/officers.json` (name, role `officer|manager|auditor`, affiliations) and then run:

```bash
python -m backend.auth set-password <username> <password>
python -m backend.auth list
```

Optional: build the ABN index if the ABR bulk extract is present under `data/external/abn/`.

```bash
python -m backend.abn_index
```

## Configuration

All settings are read from environment variables. Defaults are suitable for a local workstation.

| Variable | Default | Description |
|---|---|---|
| `GRANTLENS_LLM` | `ollama` | LLM backend: `ollama`, `openai`, or `mock`. Mock mode simulates verdicts for workflow demonstrations and is clearly labelled in the UI; it must never be used for reported figures. |
| `GRANTLENS_MODEL` | `qwen3:8b` | Model name. For GPUs with less VRAM, use for example `qwen3:8b-q4_K_M`. |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `OPENAI_BASE_URL`, `OPENAI_API_KEY` | — | Used when `GRANTLENS_LLM=openai` |
| `GRANTLENS_NUM_CTX` | `8192` | Context window passed to the model |
| `GRANTLENS_LLM_TIMEOUT` | `300` | Seconds to wait for a model response |
| `EMBED_BACKEND` | `tfidf` | Set to `bge` for BGE-M3 + FAISS retrieval |
| `GRANTLENS_DB` | `data/grantlens.db` | SQLite path. Delete the file to reset demo data. |
| `GRANTLENS_SECRET` | generated | Session signing key. Set explicitly before any real deployment. |
| `GRANTLENS_SESSION_HOURS` | `8` | Session lifetime |
| `GRANTLENS_AUTH` | `on` | Set to `off` only in tests |
| `GRANTLENS_ACCESS_KEY` | — | When set, the site requires `/?key=<value>` (private online demo) |
| `GRANTLENS_FEEDBACK` | `on` | Few-shot feedback store from officer decisions |
| `GRANTLENS_MIN_SECONDS_PER_RULE` | `15` | Threshold for the fast-signing warning |
| `GRANTLENS_ATTESTATION_MIN_CHARS` | `25` | Minimum attestation length on criteria without a code safety net |
| `GRANTLENS_ATTESTATION_MIN_CHARS_LLM` | `15` | Minimum attestation length on LLM-only criteria |
| `GRANTLENS_ATTESTATION_MIN_QUOTE_WORDS` | `5` | Consecutive words that must exist verbatim in the application |
| `GRANTLENS_CASEGEN_MODEL`, `GRANTLENS_VERIFIER_MODEL`, `GRANTLENS_PLANNER_MODEL` | system model | Models used to generate and verify test sets; should differ from the system under test |

## Running the application

Start the server from the `grantlens` directory.

```bash
uvicorn backend.app:app --port 8000
```

Open http://localhost:8000 and sign in with `sarah.mitchell` / `demo-officer-123` (officer) or `david.thompson` / `demo-manager-123` (manager).

To run the full workflow without a GPU or a model (verdicts are simulated and labelled as such):

```bash
# Windows (PowerShell)
$env:GRANTLENS_LLM = "mock"; uvicorn backend.app:app --port 8000

# Linux / macOS
GRANTLENS_LLM=mock uvicorn backend.app:app --port 8000
```

Suggested walkthrough once the server is running:

1. Overview: review the queue and key indicators (officer override rate, verbatim citations, spot-checks, audit chain status).
2. Open an application, run the AI assessment, start the review, complete the blind spot-check, confirm criteria one by one, and sign.
3. Open an application where the AI returns UNCLEAR or NOT ADDRESSED and observe that a human decision with a reason is required.
4. Draft the outcome letter, edit it, approve it, and print to PDF.
5. Bias Lab: compare a bias pair (for example HS-11A and HS-11B) and confirm the verdicts match.
6. Audit log: open an event and inspect the hash chain.
7. Data intake: upload your own `.txt`, `.docx`, or `.pdf` application and run it. Uploads default to the Australian demonstration ruleset (`au-demo-fund`). Other funds, including NSF CAREER (`nsf-22-586`), remain selectable in the same dropdown for matching applications.

## Running the tests

The test suites use the mock LLM and a temporary SQLite database. They do not require a GPU or a running model and never write under `data/`.

Run from the `grantlens` directory with the virtual environment activated.

```bash
python tests/test_auth_e2e.py
```

Covers authentication, session integrity, forged cookies, role separation, countersignature, post-audit sampling, the consistency lock, and the audit chain. Expected output ends with a summary line and a non-zero exit code if any check fails.

```bash
python tests/test_i18n.py
```

Covers the backend locale layer: English is the source language, no Vietnamese in API output by default, and verbatim regions are never translated.

The UI scan requires Playwright and a running server in mock mode with demo accounts created.

```bash
pip install playwright
playwright install chromium
# in a separate terminal: GRANTLENS_LLM=mock uvicorn backend.app:app --port 8000
python tests/scan_en.py http://127.0.0.1:8000/
```

The scan lists any UI text that still contains non-English content outside verbatim regions.

A quick smoke check of the running API without authentication:

```bash
curl http://127.0.0.1:8000/api/meta
```

## Evaluation and measurement

Two evaluation paths exist. Both require a real model (`GRANTLENS_LLM=ollama`); figures produced in mock mode have no reporting value and are flagged as such.

### Ground-truth evaluation

Scores the pipeline against hand-authored labels in `data/labels/ground-truth.json`. Reports verdict accuracy, citation match rate, bias-pair agreement, and false-pass rate. Results are saved to `eval-results-<model>.json`.

```bash
python -m backend.eval
python -m backend.eval --only HS-AU-01,HS-AU-02
```

### Per-fund generated test sets

Each fund runs its own chain. A human reviewer must read every generated case before the labels can be approved; evaluation results are provisional until approval, and editing labels after approval revokes it.

```bash
python -m backend.casegen <ruleset_id> --gen [N] [--target-only]
python -m backend.casegen <ruleset_id> --confirm <case_id> "reason"
python -m backend.casegen <ruleset_id> --confirm-control <case_id> <rule_id> "reason"
python -m backend.casegen <ruleset_id> --dispute <case_id> "reason"
python -m backend.casegen <ruleset_id> --flag-weak <case_id> <rule_id> "reason"
python -m backend.casegen <ruleset_id> --approve "Reviewer name"
python -m backend.casegen <ruleset_id> --eval
```

Available ruleset identifiers: `au-demo-fund` (default for new uploads), `au-cyber-skills-r2`, `au-female-founders-r1`, `au-onfarm-water`, `au-wine-tourism-r8`, and `nsf-22-586` (US reference — selectable on upload, not the default). `GET /api/measurement-status` and the Criteria sets screen show which step each fund has reached.

### Latest results (Australian funds, Qwen3-8B, 7 October 2026)

Target evaluation on reviewed and approved labels. One fund's figure is never used to speak for another, and all sets are small samples.

| Fund | Violations caught | False pass | Routed to officer | Clean control correct / false alarms |
|---|---|---|---|---|
| Wine Tourism and Cellar Door R8 | 2/2 | 0/2 | 0 | 2/2 / 0 |
| Cyber Security Skills R2 | 2/2 | 0/2 | 0 | 2/2 / 0 |
| Boosting Female Founders R1 | 1/2 | 0/2 | 1 | 2/2 / 0 |
| On-farm Emergency Water Infrastructure | 1/1 | 0/1 | 0 | 1/1 / 0 |
| Demonstration fund | 1/1 | 0/1 | 0 | 1/1 / 0 |
| Total | 7/8 | 0/8 | 1 | 8/8 / 0 |

The single routed case (Female Founders F05) is one where the model leaned towards MET for an income-tax-exempt applicant; the hand-written guard tagged a suspected false pass and downgraded the verdict to UNCLEAR for the officer.

Ground-truth full-matrix evaluation on the Australian trap applications HS-AU-01 and HS-AU-02: exact verdict agreement 14/17 (82.4%), citation match 17/17 (100%), false passes 0/5.

## Data

- Guidelines: four Australian grant guidelines (Cyber Security Skills R2, Boosting Female Founders R1, On-farm Emergency Water Infrastructure, Wine Tourism and Cellar Door R8) plus a demonstration fund. Rulesets are stored verbatim in `data/rulesets/`.
- Applications: synthetic, with ground-truth labels. Real applications are not public. Includes bias-test pairs, Australian trap cases, a multi-document fraud case, and generated per-fund test sets.
- Screening lists: ASIC Banned and Disqualified Persons, DFAT Consolidated Sanctions List, ABR/ABN bulk extract. See `data/external/README.md`. These are reference data for screening, not training data.
- A previously generated Wine Tourism test set was withdrawn because the generated texts leaked the answers. It is kept under `data/labels/withdrawn/` for traceability only.

## Known limitations

- False-pass figures are only valid for the fund measured with an approved-label test set. A new fund is guaranteed only the generic compiler layer; rules marked `needs-manual-guard` still require a hand-written guard and officer attention.
- Results are not bit-for-bit reproducible across runs. When the model does not fit entirely in GPU memory, the GPU/CPU split changes with free VRAM and close decisions can flip even at temperature 0. The safety metric (false pass) has remained zero across runs; variation falls on the false-alarm side. Report ranges from repeated runs rather than single numbers, and deploy on a GPU that holds the whole model.
- Sample sizes are small. Per-fund figures are preliminary evidence, not population rates.
- Screening validity depends on the freshness of the registers and on fuzzy-name matching.
- The system is an eligibility assistant, not a grants management system. Payment, contracting, and enterprise single sign-on are out of scope.

## Deployment

See `DEPLOY.md` for three scenarios: an online demo in mock mode on a host without a GPU, a GPU VPS running the real model, and on-premises deployment inside an organisation's network.

Build and run with Docker (mock mode by default):

```bash
docker build -t grantlens .
docker run -p 8000:8000 grantlens
```

Before any real deployment: change the demo passwords, set `GRANTLENS_SECRET`, and mount a persistent volume for `GRANTLENS_DB`.

## Project layout

```
grantlens/
  backend/          Application code
  frontend/         Single-page web application
  data/             Rulesets, applications, labels, screening lists, letter template
  docs/             System pipeline.html + GrantLens-External-Test-Guide.pdf (UI testing guide)
  tests/            Automated test suites
  requirements.txt  Python dependencies
  Dockerfile        Container image (mock mode by default)
  DEPLOY.md         Deployment guide
  eval-generated-*.json  Per-fund measurement results shown in Criteria sets
```

Challenge report PDFs and other packaging-only material live outside this package (see the sibling `_extras/` folder if present).
