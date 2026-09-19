"""store.py — lưu trữ bền vững (SQLite) cho ca hồ sơ, kết luận và nhật ký kiểm toán.

Nhật ký kiểm toán là chuỗi băm (hash chain): mỗi sự kiện chứa SHA-256 của sự kiện trước.
Sửa/xóa một dòng ở giữa sẽ làm mọi hash phía sau sai -> verify_chain() phát hiện được.
Đây là cơ chế "tamper-evident" đủ cho thanh tra nội bộ; bản triển khai lớn có thể
neo hash cuối ngày ra hệ thống ngoài (ký số / WORM storage).
"""
import os, json, sqlite3, hashlib, threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("GRANTLENS_DB", ROOT / "data" / "grantlens.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY,
  applicant TEXT, org TEXT, directorate TEXT, scenario TEXT, tags TEXT, pair TEXT,
  source TEXT DEFAULT 'dataset',
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  officer TEXT, created_at TEXT, updated_at TEXT,
  assessed_at TEXT, review_started_at TEXT, signed_at TEXT,
  spot_rule TEXT, spot_answer TEXT, spot_ai TEXT,
  letter TEXT, letter_status TEXT, letter_approved_at TEXT,
  llm_model TEXT, embed_backend TEXT, screening TEXT, crosscheck TEXT,
  ruleset_id TEXT, ruleset_version TEXT, ruleset_snapshot TEXT,
  countersigned_by TEXT, countersigned_at TEXT,
  supplement TEXT, supplement_round INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS verdicts (
  case_id TEXT, rule_id TEXT,
  ai_verdict TEXT, ai_confidence TEXT, facts TEXT, rq TEXT, aq TEXT,
  chunk_id INTEGER, cite_ok INTEGER, note TEXT, retrieval TEXT, needs_attention INTEGER DEFAULT 0,
  guard_level TEXT,
  final_verdict TEXT, officer_reason TEXT, confirmed_by TEXT, confirmed_at TEXT,
  PRIMARY KEY (case_id, rule_id)
);
CREATE TABLE IF NOT EXISTS post_audit (
  case_id TEXT, rule_id TEXT, auditor TEXT, outcome TEXT, note TEXT, audited_at TEXT,
  PRIMARY KEY (case_id, rule_id)
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL, case_id TEXT, actor TEXT NOT NULL, role TEXT, action TEXT NOT NULL,
  detail TEXT, prev_hash TEXT, hash TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


_db = None


def db():
    global _db
    if _db is None:
        _db = _conn()
        _db.executescript(SCHEMA)
        vcols = {r[1] for r in _db.execute("PRAGMA table_info(verdicts)")}
        if "guard_level" not in vcols:
            _db.execute("ALTER TABLE verdicts ADD COLUMN guard_level TEXT")
        cols = {r[1] for r in _db.execute("PRAGMA table_info(cases)")}
        for col, typ in [("screening", "TEXT"), ("crosscheck", "TEXT"), ("ruleset_id", "TEXT"),
                         ("ruleset_version", "TEXT"), ("ruleset_snapshot", "TEXT"),
                         ("countersigned_by", "TEXT"), ("countersigned_at", "TEXT"),
                         ("supplement", "TEXT"), ("supplement_round", "INTEGER DEFAULT 0")]:
            if col not in cols:  # migration cho DB cũ
                _db.execute(f"ALTER TABLE cases ADD COLUMN {col} {typ}")
        _db.commit()
    return _db


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in ("tags", "facts", "retrieval", "detail", "screening", "crosscheck", "ruleset_snapshot", "supplement"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except (json.JSONDecodeError, TypeError):
                pass
    return d


# ---------------- cases ----------------
def upsert_case(c: dict):
    with _lock:
        db().execute(
            """INSERT INTO cases (id, applicant, org, directorate, scenario, tags, pair, source, text, status, created_at, updated_at)
               VALUES (:id,:applicant,:org,:directorate,:scenario,:tags,:pair,:source,:text,'new',:t,:t)
               ON CONFLICT(id) DO UPDATE SET applicant=excluded.applicant, org=excluded.org, directorate=excluded.directorate,
               scenario=excluded.scenario, tags=excluded.tags, pair=excluded.pair""",
            {**c, "tags": json.dumps(c.get("tags", []), ensure_ascii=False), "pair": c.get("pair"),
             "source": c.get("source", "dataset"), "t": now()},
        )
        db().commit()


def case_exists(case_id: str) -> bool:
    return db().execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is not None


def get_case(case_id: str, with_text=True):
    r = _row(db().execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())
    if r and not with_text:
        r.pop("text", None)
    return r


def list_cases():
    rows = [_row(r) for r in db().execute("SELECT * FROM cases ORDER BY id").fetchall()]
    out = []
    for r in rows:
        r.pop("text", None)
        v = db().execute(
            "SELECT COUNT(*) n, SUM(confirmed_at IS NOT NULL) c, SUM(ai_verdict='not_met') nm, "
            "SUM(ai_verdict IN ('unclear','not_addressed')) attn, SUM(final_verdict='not_met') fnm FROM verdicts WHERE case_id=?",
            (r["id"],)).fetchone()
        r["n_rules"] = v["n"] or 0
        r["n_confirmed"] = v["c"] or 0
        r["ai_not_met"] = v["nm"] or 0
        r["ai_attention"] = v["attn"] or 0
        r["final_not_met"] = v["fnm"] or 0
        out.append(r)
    return out


def update_case(case_id: str, **fields):
    fields["updated_at"] = now()
    sets = ", ".join(f"{k}=:{k}" for k in fields)
    with _lock:
        db().execute(f"UPDATE cases SET {sets} WHERE id=:id", {**fields, "id": case_id})
        db().commit()


def next_upload_id() -> str:
    r = db().execute("SELECT COUNT(*) n FROM cases WHERE source='upload'").fetchone()
    return f"UP-{r['n'] + 1:03d}"


# ---------------- verdicts ----------------
def save_ai_verdict(case_id: str, v: dict):
    with _lock:
        db().execute(
            """INSERT INTO verdicts (case_id, rule_id, ai_verdict, ai_confidence, facts, rq, aq, chunk_id, cite_ok, note, retrieval, needs_attention,
                                     guard_level, final_verdict, officer_reason, confirmed_by, confirmed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)
               ON CONFLICT(case_id, rule_id) DO UPDATE SET ai_verdict=excluded.ai_verdict, ai_confidence=excluded.ai_confidence,
                 facts=excluded.facts, rq=excluded.rq, aq=excluded.aq, chunk_id=excluded.chunk_id, cite_ok=excluded.cite_ok,
                 note=excluded.note, retrieval=excluded.retrieval, needs_attention=excluded.needs_attention,
                 guard_level=excluded.guard_level,
                 final_verdict=NULL, officer_reason=NULL, confirmed_by=NULL, confirmed_at=NULL""",
            (case_id, v["r"], v["v"], v.get("confidence"), json.dumps(v.get("facts", []), ensure_ascii=False),
             v["rq"], v["aq"], v.get("chunk_id"), int(bool(v.get("cite_app_ok"))), v.get("note", ""),
             json.dumps(v.get("retrieval_scores", [])), int(bool(v.get("needs_attention"))),
             v.get("guard_level")),
        )
        db().commit()


def get_verdicts(case_id: str):
    return [_row(r) for r in db().execute(
        "SELECT * FROM verdicts WHERE case_id=? ORDER BY rule_id", (case_id,)).fetchall()]


def get_verdict(case_id: str, rule_id: str):
    return _row(db().execute("SELECT * FROM verdicts WHERE case_id=? AND rule_id=?", (case_id, rule_id)).fetchone())


def confirm_verdict(case_id, rule_id, final_verdict, reason, officer):
    with _lock:
        db().execute(
            "UPDATE verdicts SET final_verdict=?, officer_reason=?, confirmed_by=?, confirmed_at=? WHERE case_id=? AND rule_id=?",
            (final_verdict, reason, officer, now(), case_id, rule_id))
        db().commit()


def unconfirm_verdict(case_id, rule_id):
    with _lock:
        db().execute(
            "UPDATE verdicts SET final_verdict=NULL, officer_reason=NULL, confirmed_by=NULL, confirmed_at=NULL WHERE case_id=? AND rule_id=?",
            (case_id, rule_id))
        db().commit()


def clear_verdicts(case_id):
    with _lock:
        db().execute("DELETE FROM verdicts WHERE case_id=?", (case_id,))
        db().commit()


# ---------------- audit (hash chain) ----------------
def _hash(prev: str, ts: str, case_id, actor: str, role, action: str, detail: str) -> str:
    payload = "|".join([prev or "", ts, case_id or "", actor, role or "", action, detail or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def log(actor: str, action: str, case_id: str = None, role: str = None, detail: dict = None) -> dict:
    with _lock:
        last = db().execute("SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else "GENESIS"
        ts = now()
        d = json.dumps(detail, ensure_ascii=False) if detail else None
        h = _hash(prev, ts, case_id, actor, role, action, d)
        cur = db().execute(
            "INSERT INTO audit (ts, case_id, actor, role, action, detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
            (ts, case_id, actor, role, action, d, prev, h))
        db().commit()
        return {"seq": cur.lastrowid, "ts": ts, "case_id": case_id, "actor": actor, "role": role,
                "action": action, "detail": detail, "prev_hash": prev, "hash": h}


def audit_events(case_id: str = None, limit: int = 500):
    q = "SELECT * FROM audit" + (" WHERE case_id=?" if case_id else "") + " ORDER BY seq DESC LIMIT ?"
    args = (case_id, limit) if case_id else (limit,)
    return [_row(r) for r in db().execute(q, args).fetchall()]


def verify_chain() -> dict:
    rows = db().execute("SELECT * FROM audit ORDER BY seq").fetchall()
    prev = "GENESIS"
    for r in rows:
        expect = _hash(prev, r["ts"], r["case_id"], r["actor"], r["role"], r["action"], r["detail"])
        if r["prev_hash"] != prev or r["hash"] != expect:
            return {"ok": False, "broken_at_seq": r["seq"], "total": len(rows)}
        prev = r["hash"]
    return {"ok": True, "total": len(rows), "head": prev}


# ---------------- hậu kiểm lấy mẫu ----------------
UNGUARDED = ("needs-manual-guard", "llm-only")


def post_audit_candidates(auditor: str):
    """Các lần cán bộ xác nhận ĐẠT ở tiêu chí KHÔNG có lưới đỡ mã nguồn, hồ sơ đã ký, chưa ai hậu kiểm,
    và KHÔNG do chính người hậu kiểm xác nhận (không tự kiểm việc của mình)."""
    q = """SELECT v.*, c.applicant, c.status, c.officer AS case_officer, c.signed_at
           FROM verdicts v JOIN cases c ON c.id = v.case_id
           LEFT JOIN post_audit p ON p.case_id = v.case_id AND p.rule_id = v.rule_id
           WHERE v.final_verdict = 'met' AND v.guard_level IN (?, ?) AND v.confirmed_at IS NOT NULL
             AND c.signed_at IS NOT NULL AND p.case_id IS NULL AND COALESCE(v.confirmed_by, '') != ?"""
    return [_row(r) for r in db().execute(q, (*UNGUARDED, auditor or "")).fetchall()]


def save_post_audit(case_id, rule_id, auditor, outcome, note):
    with _lock:
        db().execute("INSERT OR REPLACE INTO post_audit (case_id, rule_id, auditor, outcome, note, audited_at) "
                     "VALUES (?,?,?,?,?,?)", (case_id, rule_id, auditor, outcome, note, now()))
        db().commit()


def post_audit_stats() -> dict:
    d = db()
    total = d.execute("SELECT COUNT(*) n FROM verdicts v JOIN cases c ON c.id=v.case_id WHERE v.final_verdict='met' "
                      "AND v.guard_level IN (?, ?) AND v.confirmed_at IS NOT NULL AND c.signed_at IS NOT NULL",
                      UNGUARDED).fetchone()["n"]
    r = d.execute("SELECT COUNT(*) n, SUM(outcome='disagree') bad FROM post_audit").fetchone()
    return {"eligible": total, "audited": r["n"] or 0, "disagree": r["bad"] or 0,
            "coverage": ((r["n"] or 0) / total) if total else None}


def post_audit_list(limit: int = 100):
    return [_row(r) for r in db().execute("SELECT * FROM post_audit ORDER BY audited_at DESC LIMIT ?", (limit,)).fetchall()]


# ---------------- stats ----------------
def stats():
    d = db()
    by_status = {r["status"]: r["n"] for r in d.execute("SELECT status, COUNT(*) n FROM cases GROUP BY status")}
    overrides = d.execute(
        "SELECT COUNT(*) n FROM verdicts WHERE confirmed_at IS NOT NULL AND final_verdict != ai_verdict").fetchone()["n"]
    confirmed = d.execute("SELECT COUNT(*) n FROM verdicts WHERE confirmed_at IS NOT NULL").fetchone()["n"]
    cites = d.execute("SELECT COUNT(*) n, SUM(cite_ok) ok FROM verdicts WHERE aq != ''").fetchone()
    rev = d.execute(
        "SELECT AVG((julianday(signed_at)-julianday(review_started_at))*86400) s FROM cases WHERE signed_at IS NOT NULL").fetchone()["s"]
    unguarded = d.execute(
        "SELECT COUNT(*) n FROM verdicts WHERE confirmed_at IS NOT NULL AND final_verdict='met' "
        "AND guard_level='needs-manual-guard'").fetchone()["n"]
    spot = d.execute(
        "SELECT COUNT(*) n, SUM(spot_answer = spot_ai) agree FROM cases WHERE spot_answer IS NOT NULL").fetchone()
    return {
        "cases_total": sum(by_status.values()),
        "by_status": by_status,
        "confirmed_verdicts": confirmed,
        "override_rate": (overrides / confirmed) if confirmed else 0.0,
        "citation_ok_rate": ((cites["ok"] or 0) / cites["n"]) if cites["n"] else None,
        "avg_review_seconds": rev,
        "spot_checks": {"n": spot["n"] or 0, "agree": spot["agree"] or 0},
        "unguarded_met_confirmations": unguarded,
        "post_audit": post_audit_stats(),
        "audit": verify_chain(),
    }
