# GrantLens — grant eligibility screening assistant
**AI supplies the evidence — people decide.** AI core: Qwen3-8B running locally through Ollama (data never leaves the machine, no API fees); an OpenAI-compatible endpoint can be used instead for customers with their own infrastructure.

English is the source language of the code, the data files and every message. A Vietnamese UI locale (`backend/locale_vi.py`, VI/EN switch in the top bar) is an optional display layer; verbatim application and rule text is never translated.

## Architecture
```
frontend/index.html   Single-page web app: Overview · Applications · Criteria sets · Bias Lab · Audit log · Data intake
backend/
  app.py       FastAPI — REST + streaming NDJSON assessment progress
  workflow.py  Case state machine + the "trust twist" rules enforced ON THE SERVER
  core.py      4-layer pipeline: RAG → EXTRACT (LLM pass 1) → JUDGE (LLM pass 2) → CITE (code) → GUARD (code)
  guards.py    False-pass guards: hand-written per fund + a compiler that derives constraints from the verbatim rule
  casegen.py   AI-generated labelled test sets per fund, anti-leak checks, label review/approval, per-fund evaluation
  store.py     SQLite: cases, verdicts, post-audit, SHA-256 hash-chained audit log (tamper-evident)
  auth.py      Login + HMAC-signed sessions + middleware that enforces identity from the session (never from the request body)
  coi.py       Conflict-of-interest check against the officer register; screening.py  ASIC/DFAT/ABN denied-party screening
  crosscheck.py Anti-cheating cross-check of figures/dates across the documents of one application
  feedback.py  Officer decisions -> few-shot corrections + fine-tuning export
  i18n.py      Optional UI locale layer (locale_vi.py); verbatim content is never translated
  llm.py       LLM backend: ollama (default) | openai-compatible | mock (tests without a GPU)
  rag.py       Paragraph chunking (with offsets) + TF-IDF (default) / BGE-M3+FAISS (optional)
  verify.py    Citation-by-retrieval: code cuts the verbatim sentence + second-layer string match
  eval.py      Accuracy / citation / bias / false-pass scoring against the ground truth
data/
  rulesets/    One criteria set per fund (6: NSF 22-586 CAREER + 4 Australian funds + a demo fund), versioned, with approval status
  applications/ 18 labelled synthetic applications (manifest.json) — 2 bias-test pairs, 2 Australian trap cases, 1 fraud case
               generated/<fund>/  AI-generated test sets (one clean + N violation cases per fund)
  labels/      ground-truth.json + generated-<fund>.json (labels with the reviewer's decisions and reasons)
  officers.json Officer register (roles, affiliations, SLA)   letter-template.txt  Office letter template
  grantlens.db SQLite created on first run (delete the file to reset the demo data)
tests/         test_auth_e2e.py · test_i18n.py · scan_en.py (Playwright UI scan)
```

## Workflow (case states)
`new → assessed → in_review → signed → letter_drafted → letter_approved` (`awaiting_supplement` for supplement rounds; `reopen` with a reason)

| Step | Who | Mandatory mechanism (checked by the server; the frontend cannot bypass it) |
|---|---|---|
| Run AI assessment | AI | 2 model calls per criterion; results are DRAFTS with a confidence level; code guards block false passes |
| Start review | Officer | **Blind spot-check**: one random criterion, every AI result hidden until the officer answers |
| Confirm each criterion | Officer | No "approve all"; overriding the AI or an AI UNCLEAR/NOT ADDRESSED ⇒ reason required; MET on a criterion without a code safety net ⇒ a verbatim quotation from the application, checked by code |
| Sign off | Officer | Only when every criterion is confirmed; signing faster than 15 s/criterion ⇒ warning, logged, second confirmation |
| Countersign | Manager | Cases with NOT MET / UNCLEAR criteria need a manager (not the reviewer, COI-checked) before a letter can be issued |
| Draft the letter | AI | Only after sign-off; B1 English, two-sided verbatim quotations, supplement + 30-day appeal sections |
| Approve the letter | Officer | Hand edits allowed (logged as "edited"); print / save PDF |
| Post-audit sampling | Manager / auditor | Random sample of MET confirmations on unguarded criteria; sampling itself is logged |

## Method
**Style-bias control (2 real model calls):** pass 1 extracts neutral *facts* only (figures, dates, directorate…) with chunk_id + key_phrase; pass 2 judges **only the fact list**, never the original text ⇒ grammar / fluency cannot influence the verdict. Verified with two bias pairs HS-04A/B and HS-11A/B (Bias Lab + eval).

**Citation-by-retrieval:** the 8B model never generates a quotation. It only points at a chunk + 3–8 key words; code cuts the *verbatim sentence* that best matches and string-matches it against the application ⇒ quotations are verbatim "by construction". Clicking a quotation highlights it in the original text.

**Consistency lock (code, `core.judge_consistency`):** NOT MET must point at a fact stating the violation; a `not_met` with `supporting_fact = 0` or `coverage = none` is downgraded to UNCLEAR and routed to the officer.

**False-pass guards (`backend/guards.py`):** after the LLM, code re-checks quantitative / pattern-checkable rules. Hand guards per fund and a compiler that derives thresholds, prohibitions and exclusion lists from the verbatim rule always **both** run; the LLM saying "met" while code sees a violation ⇒ override to not_met (quantitative) or downgrade to unclear (qualitative). Guards never upgrade to met. Coverage is reported honestly in 3 levels: `code-guarded` / **`needs-manual-guard`** (quantitative logic the compiler cannot express — the riskiest spot) / `llm-only`; `GET /api/guard-backlog` lists what is still open.

**Hash-chained audit log:** every event carries the SHA-256 of the previous one; editing or deleting a row breaks the chain (`GET /api/audit` returns `chain.ok`).

**Identity from the session:** `IdentityMiddleware` rejects unauthenticated `/api/*` calls and overwrites the `officer`/`role` fields of every request with the session identity, so the audit log records authenticated names. Four gates are role-locked (approve criteria set, approve test-set labels, countersign → `manager`; post-audit → `manager`/`auditor`). Auditors can only read and post-audit. Whoever reviewed a label set cannot approve it.

## Install & run
```bash
pip install -r requirements.txt
ollama pull qwen3:8b                 # ~6 GB VRAM; smaller GPUs: qwen3:8b-q4_K_M (set GRANTLENS_MODEL)
python -m backend.auth init-demo     # demo accounts + random passwords -> data/demo-accounts.txt (not committed)
uvicorn backend.app:app --port 8000
# open http://localhost:8000 — sign in with an account from data/demo-accounts.txt
```
Accounts: edit `data/officers.json` (name, role `officer|manager|auditor`, affiliations), then `python -m backend.auth set-password <username> <password>`; `python -m backend.auth list` to inspect. **Change the demo passwords and set `GRANTLENS_SECRET` before a real deployment.**

Environment variables: `GRANTLENS_LLM=ollama|openai|mock` (`mock` demos the workflow without a model — clearly labelled in the UI, never for reporting figures), `GRANTLENS_MODEL`, `OLLAMA_URL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `EMBED_BACKEND=bge`, `GRANTLENS_DB`, `GRANTLENS_SECRET`, `GRANTLENS_SESSION_HOURS` (default 8), `GRANTLENS_AUTH=off` (tests only), `GRANTLENS_FEEDBACK=on|off`, `GRANTLENS_MIN_SECONDS_PER_RULE` (default 15), `GRANTLENS_ATTESTATION_MIN_CHARS` (25) / `_LLM` (15) / `GRANTLENS_ATTESTATION_MIN_QUOTE_WORDS` (5). See DEPLOY.md.

## Tests
```bash
python tests/test_auth_e2e.py     # 43 checks: sessions, roles, forged cookies, post-audit, countersignature, consistency lock, audit chain
python tests/test_i18n.py         # locale layer: English source, no Vietnamese in API output, verbatim regions untouched, VI cookie
python tests/scan_en.py http://127.0.0.1:8000/   # Playwright scan of every screen for leftover non-English UI text
```

## Measurement — per fund, with human-approved labels
Each fund runs its own chain: `python -m backend.casegen <fund> --gen --target-only` → a reviewer reads every generated application (`--confirm` / `--confirm-control` / `--dispute` / `--flag-weak`, reasons stored in `data/labels/generated-<fund>.json`) → `--approve` → `--eval`. `GET /api/measurement-status` (and the table on the Criteria sets screen) shows which step each fund is at; an evaluation must match the exact label set that was approved (generation + approval timestamps), otherwise it reads "evaluation must be re-run". **One fund's figure is never used to speak for another.**

Why a human reviewer is mandatory: of 18 AI-generated violation cases that had passed **every automated gate**, **9 (50%) were rejected by the reviewer** — 5 wrong labels, 4 ambiguous answers (details and reasons in the label files). Generated tests cannot serve as a benchmark without a person reading each one. The remaining 1–2 violation cases per fund are **preliminary per-fund evidence, not a statistical rate**.

<!-- MEASUREMENT -->
**Results** — system under test `qwen3:8b` (English-note judgement prompt, feedback store empty, measured 2026-09-26), only on reviewed + approved labels (source: `GET /api/measurement-status`):

| Fund | Violations caught | False pass | Routed to officer (unclear) | Clean application: correct · false alarms |
|---|---|---|---|---|
| NSF 22-586 CAREER (US) | 1/1 | 0/1 | 0 | 1/1 · 0/1 |
| Wine Tourism R8 | 2/2 | 0/2 | 0 | 2/2 · 0/2 |
| Cyber Security Skills R2 | 2/2 | 0/2 | 0 | 2/2 · 0/2 |
| Boosting Female Founders R1 | 1/2 | 0/2 | 1 | 2/2 · 0/2 |
| On-farm Water | 1/1 | 0/1 | 0 | 1/1 · 0/1 |
| Demonstration fund | 1/1 | 0/1 | 0 | 1/1 · 0/1 |
| **All 6 funds** | **8/9** | **0/9** | 1 | 9/9 · **0/9** |

Same figures as the previous two runs (2026-09-14 and 2026-09-19) despite the prompt change. The one case routed to the officer is Female Founders F05: the model said MET for an income-tax-exempt applicant, the F05 hand guard caught it and downgraded to UNCLEAR with a `[SUSPECTED FALSE PASS]` tag — a real false pass blocked by code.

**NSF subset evaluation (`python -m backend.eval --only HS-02,HS-04A,HS-04B,HS-07`)** — the earlier result files were removed because their AI notes came from the Vietnamese-note prompt. Historical figures for the record: qwen3:8b 24/24 (2026-09-07) and 23/24 (2026-09-19) on HS-02 + HS-07, false pass 0/6 in every run, citation match 100%, bias pairs 12/12; qwen3:4b 83.3% accuracy with 5/6 false passes (unacceptable — the reason 8b is the system model). The 2026-09-26 re-run with the English prompt was aborted: Ollama timed out (5-minute reads) because another training job occupied the 4 GB GPU. Re-run it on a free GPU and commit the new `eval-results-qwen3_8b.json` before quoting a subset figure.
<!-- /MEASUREMENT -->

**Honest boundaries:** a false-pass figure is only valid for the fund measured with an approved-label test set; a new fund is only guaranteed the basic compiler layer, and `needs-manual-guard` rules still need a hand guard + the officer. The system is **not bit-for-bit reproducible across runs**: qwen3:8b (5.2 GB) on a 4 GB GPU is split between GPU and CPU, the split changes with free VRAM, and at temperature 0 close decisions can still flip (observed: NSF 24/24 on one day, 23/24 on another with identical inputs). The safety metric (false pass) has stayed 0 across every run; differences fall on the false-alarm side, where a human reviews and a manager countersigns. Report ranges from repeated runs, not single numbers; deploy on a GPU that holds the whole model (≥ 8 GB).

## Data
- Guidelines: NSF 22-586 CAREER (verbatim from nsf.gov, public domain); four Australian grant guidelines provided by the customer (Cyber Security Skills R2, Boosting Female Founders R1, On-farm Emergency Water Infrastructure, Wine Tourism and Cellar Door R8) plus a demonstration fund. Denied-party lists: ASIC Banned & Disqualified, DFAT Consolidated List, ABR/ABN bulk extract (index with `python -m backend.abn_index`).
- Applications: synthetic with ground truth (real applications are not public), labels in `data/labels/ground-truth.json`. Each of the 15 NSF applications targets one business scenario (clear pass, plain-English fail, unclear, bias pairs, competition-limit, foreign institution, BIO budget + letter length, page limit + cost sharing, prior award, missing sections, museum equivalent, community college); HS-14 is a multi-document fraud case; HS-AU-01/02 are Australian fund traps.
- The old Wine test set (2026-09-09, "3/3") was **withdrawn** because the generated texts leaked the answers (a case literally said "violating W08"); kept under `data/labels/withdrawn/` for traceability only.

## Change history (what the customer's critiques changed)
1. **Tables in PDF/DOCX** kept as `[TABLE n]` blocks so RAG never splits a financial table; scanned pages flagged for OCR. **Feedback loop** from signed decisions. **Conflict-of-interest** check before taking a case. **Cross-check** of figures/dates across attached documents (`=== DOCUMENT: name ===` marker, `/attach`). Letters follow the office template in `data/letter-template.txt`.
2. **Multi-fund rulesets with versioning** (each case locks a snapshot at first assessment). **False-pass guards.** **Countersignature** for rejections. **Supplement rounds** with SLA.
3. **The compiler admits what it cannot express** (derived logic, alternative branches, conditional thresholds) instead of silently skipping it; 3-level coverage; hand guards and compiler run together; **casegen** got an independent verifier, label approval, disputes, and the Wine set was withdrawn for answer leaks; violations are now fixed by code first and inserted verbatim; rule IDs and self-commentary are removed by code; models are separated by role (planner / writer / verifier ≠ system under test); monologue detection; flags never drop cases silently; controls on clean applications; `GET /api/measurement-status`.
4. **Server-side friction where there is no code safety net**: MET on `needs-manual-guard` criteria requires a self-verified attestation (≥ 25 characters with a verbatim passage); verifier votes never trusted absolutely (two differently phrased votes, code veto); the [TARGET] metric is separated from noisy secondary labels.
5. **Risk-tiered friction**: `llm-only` criteria also require evidence (≥ 15 characters or confirming the AI's quotation); attestations must contain ≥ 5 consecutive words that really exist in the application and cannot be pasted twice; hand guards W04 and C06 closed the high-priority backlog (now 0); currency normalisation (`A$`, `AUD`, `dollars`); compiler fixes (neither/nor negation, single-word exclusions, conditional exclusion items, `max` constraints only compare figures with ≥ 2 rule keywords right before them).
6. **Structural closure**: login + HMAC sessions + identity middleware; four role-locked gates; separation of duties on label approval; post-audit sampling; consistency lock on the judgement pass (`supporting_fact or 1` bug fixed). Non-reproducibility across runs documented instead of patched (no guard was added for R07 to chase 24/24).
7. **English as the source language** (this version): code, comments, data files, labels, README rewritten in English; the judgement prompt now asks for an English note (`note`), so every measurement was re-run; a Vietnamese UI locale remains as an optional display layer. Also fixed: the streamed `verdict` event was overwritten by the rule's own `type` field, so the UI never received it.

## 10-minute demo script
1. **Overview** — queue, KPIs: officer override rate, verbatim citations, spot-checks, audit chain intact.
2. Open **HS-02** → Run AI assessment (per-criterion progress) → Start review → **blind spot-check** → open R04: pass-1 facts, two-sided quotations, click to highlight → confirm line by line → try signing fast (warning) → sign.
3. Open **HS-03** — AI returns UNCLEAR / NOT ADDRESSED; the system forces a human decision + reason.
4. Draft the **B1 letter**, edit by hand, approve, print PDF.
5. **Bias Lab** HS-11A vs HS-11B → 12/12 match.
6. **Audit log** — open the "details" of an event, show the hash chain.
7. **Data intake** — upload the customer's own application and run it.
