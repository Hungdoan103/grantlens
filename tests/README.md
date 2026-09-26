# Tests (no GPU needed, never touch real data)

| Command | What it checks |
|---|---|
| `python tests/test_auth_e2e.py` | Login, sessions, officer/manager/auditor roles, post-audit sampling, consistency lock (43 checks) |
| `python tests/test_i18n.py` | Locale layer: English is the source, no Vietnamese in any API output, the VI dictionary is clean, verbatim regions are never translated, cookie `gl_lang=vi` localises JSON/errors/stream events (30+ checks) |
| `python tests/scan_en.py http://127.0.0.1:8000/` | Playwright scan of every screen: lists any text node / placeholder / title that still contains Vietnamese outside verbatim regions (needs a running server with `GRANTLENS_LLM=mock` and `data/demo-accounts.txt`) |

Both test suites use the mock LLM and a temporary database (`GRANTLENS_DB` points at a temp directory); the account book is patched in memory — nothing is written under `data/`.
