"""i18n.py — lớp hiển thị tiếng Anh cho GrantLens (VI là bản gốc, EN là lớp áp lên đầu ra).

Cách hoạt động
  • Ngôn ngữ của một request lấy từ cookie `gl_lang` (do nút VI|EN trên giao diện đặt), giữ trong contextvar.
  • Khi lang=en: mọi JSON trả về giao diện đi qua `tr_obj` (I18nJSONResponse); chuỗi nào là NGUYÊN VĂN
    (văn bản hồ sơ, câu tiêu chí, trích dẫn, dữ kiện, thư, mẫu thư, tên người/tổ chức, lý do cán bộ tự ghi)
    KHÔNG BAO GIỜ bị dịch — xem SKIP_KEYS. Phía giao diện cũng có danh sách vùng bỏ qua tương ứng.
  • Ghi chú của AI (note_vi) là văn tự do -> dịch bằng model một lần ngay sau khi đánh giá, lưu `note_en`
    cạnh bản gốc (workflow.assess_stream); phần tiền tố do mã nguồn sinh ([CHẶN FALSE-PASS]…) dịch bằng từ điển.
  • Cùng một từ điển (i18n_en.py) được phục vụ cho giao diện qua GET /api/i18n/en để dịch DOM.

Thuật toán dịch một chuỗi (cả Python lẫn JS làm y hệt):
  1) khớp nguyên chuỗi (đã strip) trong EXACT / tiêu đề rule;
  2) áp các PATTERNS (regex có nhóm) theo thứ tự;
  3) phần còn dính dấu tiếng Việt: thay theo CỤM là khoá EXACT (dài ≥ MIN_PHRASE ký tự, dài trước ngắn sau, có biên từ).
"""
import contextvars, json, re
from . import i18n_en

LANG = contextvars.ContextVar("gl_lang", default="vi")
LANGS = ("vi", "en")
COOKIE = "gl_lang"
MIN_PHRASE = 6   # cụm quá ngắn (Mã, và, cao…) không dùng để thay trong câu — chỉ khớp nguyên chuỗi

VI_RE = re.compile("[àáảãạăằắẳẵặâầấẩẫậđèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ]", re.I)

# khoá JSON KHÔNG dịch (nguyên văn / do người nhập / định danh)
SKIP_KEYS = {"text", "letter", "quote", "rule_quote", "aq", "rq", "ai_quote", "fact", "facts", "key_phrase",
             "template", "applicant", "org", "matched_name", "name", "officer_reason", "attestation", "items",
             "evidence", "username", "hash", "prev_hash", "head", "id", "case_id", "rule_id",
             "confirmed_by", "countersigned_by", "officer", "actor", "approved_by", "generated_by", "saved_by",
             "by", "docs", "source", "context", "value", "directorate", "state", "postcode", "abn", "period"}
# ngoại lệ: một số khoá trên vẫn cần dịch khi giá trị là nhãn hệ thống
FORCE_KEYS = {"actor"}   # "Hệ thống" / "Cán bộ"


def set_lang(lang: str):
    LANG.set(lang if lang in LANGS else "vi")


def get_lang() -> str:
    return LANG.get()


def lang_from_cookie_header(cookie: str) -> str:
    for part in (cookie or "").split(";"):
        name, _, val = part.strip().partition("=")
        if name == COOKIE and val in LANGS:
            return val
    return "vi"


# ---------------- biên dịch từ điển ----------------
def _expand(repl: str):
    """'$1' -> nhóm 1, '$$' -> '$' (cú pháp chung với JS String.replace)."""
    def f(m):
        out, i = [], 0
        while i < len(repl):
            ch = repl[i]
            if ch == "$" and i + 1 < len(repl):
                nx = repl[i + 1]
                if nx == "$":
                    out.append("$"); i += 2; continue
                if nx.isdigit():
                    out.append(m.group(int(nx)) or ""); i += 2; continue
            out.append(ch); i += 1
        return "".join(out)
    return f


_PATS = [(re.compile(p, re.M), _expand(r)) for p, r in i18n_en.PATTERNS]
_PATS_LAST = [(re.compile(p, re.M), _expand(r)) for p, r in i18n_en.PATTERNS_LAST]

_phrase_keys = sorted((k for k in i18n_en.EXACT if len(k) >= MIN_PHRASE), key=len, reverse=True)
_PHRASE_RE = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in _phrase_keys) + r")(?!\w)")

_titles_cache = {"key": None, "map": {}}


def rule_titles() -> dict:
    """title_vi -> title_en cho mọi rule đang nạp (đọc từ ruleset, có cache theo lần nạp)."""
    from . import core
    all_rs = core.load_rulesets()
    if _titles_cache["key"] is not id(all_rs):
        m = {}
        for k, rs in all_rs.items():
            if k == "_default":
                continue
            fallback = i18n_en.RULE_TITLES.get(rs["id"], {})
            for r in rs.get("rules", []):
                en = r.get("title_en") or fallback.get(r["id"])
                if en and r.get("title_vi"):
                    m[r["title_vi"]] = en
        _titles_cache.update(key=id(all_rs), map=m)
    return _titles_cache["map"]


def translate(s: str) -> str:
    """Dịch một chuỗi sang tiếng Anh (bất kể ngôn ngữ của request)."""
    if not isinstance(s, str) or not s:
        return s
    stripped = s.strip()
    if not stripped:
        return s
    lead, trail = s[:len(s) - len(s.lstrip())], s[len(s.rstrip()):]
    if "\n" in stripped:
        return lead + "\n".join(translate(line) for line in stripped.split("\n")) + trail
    out = i18n_en.EXACT.get(stripped)
    if out is None:
        out = rule_titles().get(stripped)
    if out is None:
        if not VI_RE.search(stripped):
            return s
        out = stripped
        for pat, rep in _PATS:
            out = pat.sub(rep, out)
        if VI_RE.search(out):
            out = _PHRASE_RE.sub(lambda m: i18n_en.EXACT.get(m.group(0), m.group(0)), out)
        if VI_RE.search(out):
            for pat, rep in _PATS_LAST:
                out = pat.sub(rep, out)
    return lead + out + trail


def tr(s: str) -> str:
    return translate(s) if LANG.get() == "en" else s


def _walk(o, key=None):
    if isinstance(o, str):
        return translate(o)
    if isinstance(o, list):
        return [_walk(x) for x in o]
    if isinstance(o, dict):
        out = {}
        if o.get("note") and ("ai_verdict" in o or "v" in o):   # dòng kết luận AI (DB: ai_verdict; stream: v)
            if o.get("note_en"):        # đã dịch sẵn bằng model
                o = dict(o, note=o["note_en"])
            else:                       # chưa có bản dịch: chỉ dịch tiền tố do mã nguồn sinh, giữ nguyên câu của model
                o = dict(o, note=_note_without_en(o["note"]))
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                out[k] = _walk(v, k)
            elif isinstance(v, str) and (k not in SKIP_KEYS or k in FORCE_KEYS):
                out[k] = translate(v)
            else:
                out[k] = v
        return out
    return o


def _note_without_en(note: str) -> str:
    if LLM_SEP in note:
        prefix, free = note.split(LLM_SEP, 1)
        return translate(prefix) + " | LLM (vi): " + free
    return note + ("  " + translate("(ghi chú AI bằng tiếng Việt — chưa dịch)") if VI_RE.search(note) else "")


def tr_obj(o):
    """Dịch đệ quy một payload JSON khi lang=en; giữ nguyên khi vi."""
    return _walk(o) if LANG.get() == "en" else o


def dictionary_payload() -> dict:
    """Từ điển phục vụ giao diện (cùng nguồn với backend)."""
    exact = dict(i18n_en.EXACT)
    exact.update(rule_titles())
    return {"lang": "en", "exact": exact, "patterns": i18n_en.PATTERNS, "patterns_last": i18n_en.PATTERNS_LAST,
            "min_phrase": MIN_PHRASE}


# ---------------- ghi chú AI: dịch bằng model, lưu cạnh bản gốc ----------------
SYS_TRANSLATE = ("You translate short Vietnamese notes written by a grant-eligibility reviewer into concise English. "
                 "Keep rule IDs, numbers, currency, quoted text and technical terms unchanged. Output only the translation, "
                 "one or two sentences, no preamble.")
LLM_SEP = " | LLM: "


def note_to_en(note: str):
    """Trả bản tiếng Anh của ghi chú AI, hoặc None nếu không dịch được (giao diện sẽ hiện bản gốc)."""
    if not note:
        return None
    try:
        from . import llm
        if LLM_SEP in note:
            prefix, free = note.split(LLM_SEP, 1)
            prefix_en = translate(prefix) + LLM_SEP.rstrip()
        else:
            prefix_en, free = "", note
        free = free.strip()
        if not free or not VI_RE.search(free):
            free_en = free
        elif llm.describe().get("is_mock"):
            free_en = "[MOCK EN] " + translate(free)
        else:
            free_en = llm.chat_text(SYS_TRANSLATE, free, max_tokens=160).strip().strip('"')
            if not free_en or len(free_en) > 4 * len(free) + 40:
                return None
        return (prefix_en + " " + free_en).strip() if prefix_en else free_en
    except Exception:
        return None


def backfill_notes(limit: int = None) -> int:
    """Dịch bù note_en cho các kết luận AI đã có trước khi bật lớp tiếng Anh (gọi model). Trả số dòng đã dịch."""
    from . import store
    rows = store.db().execute("SELECT case_id, rule_id, note FROM verdicts WHERE note IS NOT NULL AND note != '' "
                              "AND (note_en IS NULL OR note_en = '')").fetchall()
    n = 0
    for r in rows[:limit] if limit else rows:
        en = note_to_en(r["note"])
        if en:
            with store._lock:
                store.db().execute("UPDATE verdicts SET note_en=? WHERE case_id=? AND rule_id=?", (en, r["case_id"], r["rule_id"]))
                store.db().commit()
            n += 1
            print(f"  {r['case_id']} {r['rule_id']}: {en[:90]}")
    return n


# ---------------- FastAPI ----------------
try:
    from fastapi.responses import JSONResponse

    class I18nJSONResponse(JSONResponse):
        """JSON response mặc định của app: dịch payload khi request đang ở lang=en."""

        def render(self, content) -> bytes:
            if LANG.get() == "en":
                content = _walk(content)
            return json.dumps(content, ensure_ascii=False, allow_nan=False, indent=None,
                              separators=(",", ":")).encode("utf-8")
except ImportError:  # dùng ngoài FastAPI (CLI)
    I18nJSONResponse = None


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["backfill"]:
        print("đã dịch bù", backfill_notes(), "ghi chú AI (note_en)")
    elif sys.argv[1:2] == ["tr"]:
        for a in sys.argv[2:]:
            print(translate(a))
    else:
        print("python -m backend.i18n backfill      dịch bù note_en cho kết luận AI cũ (cần model)\n"
              "python -m backend.i18n tr '<chuỗi>'  thử dịch một chuỗi")
