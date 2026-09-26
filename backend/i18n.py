"""i18n.py — optional UI locale layer. ENGLISH IS THE SOURCE LANGUAGE of the code, the data and every message;
another language (currently Vietnamese, locale_vi.py) is only a DISPLAY LAYER applied to the output.

How it works
  • The request language comes from the cookie `gl_lang` (set by the language switch in the UI) and is kept in a
    context variable. Default: "en" (no translation at all).
  • For a non-English language every JSON response passes through `localize` (I18nJSONResponse). Strings that are
    VERBATIM content (application text, rule quotes, quotations, extracted facts, letters, templates, names of people
    and organisations, reasons typed by officers) are NEVER translated — see SKIP_KEYS. The UI has a matching list
    of untranslated regions.
  • AI notes are free text written by the model in English and are shown as-is in every locale.
  • The same dictionary is served to the UI through GET /api/i18n/<lang> to translate the DOM.

Translation of one string (Python and the JS engine behave identically):
  1) exact match of the whole (stripped) string in EXACT / rule titles;
  2) PATTERNS (regexes with groups) applied in order — replacement groups written $1..$9, a literal $ as $$;
  3) any remaining text is replaced PHRASE by phrase (EXACT keys of ≥ MIN_PHRASE characters, longest first, on word boundaries);
  4) PATTERNS_LAST for short labels that must run after everything else.
"""
import contextvars, json, re
from . import locale_vi

LANG = contextvars.ContextVar("gl_lang", default="en")
LANGS = ("en", "vi")
COOKIE = "gl_lang"
MIN_PHRASE = 6   # phrases shorter than this are only matched as whole strings, never inside a sentence

_LOCALES = {"vi": locale_vi}

# JSON keys that are NEVER translated (verbatim content / user input / identifiers)
SKIP_KEYS = {"text", "letter", "quote", "rule_quote", "aq", "rq", "ai_quote", "fact", "facts", "key_phrase",
             "template", "applicant", "org", "matched_name", "name", "officer_reason", "attestation", "items",
             "evidence", "username", "hash", "prev_hash", "head", "id", "case_id", "rule_id", "note",
             "confirmed_by", "countersigned_by", "officer", "actor", "approved_by", "generated_by", "saved_by",
             "by", "docs", "source", "context", "value", "directorate", "state", "postcode", "abn", "period"}
# exceptions: system labels stored under one of the keys above
FORCE_KEYS = {"actor"}   # "System" / "AI"


def set_lang(lang: str):
    LANG.set(lang if lang in LANGS else "en")


def get_lang() -> str:
    return LANG.get()


def lang_from_cookie_header(cookie: str) -> str:
    for part in (cookie or "").split(";"):
        name, _, val = part.strip().partition("=")
        if name == COOKIE and val in LANGS:
            return val
    return "en"


# ---------------- dictionary compilation ----------------
def _expand(repl: str):
    """'$1' -> group 1, '$$' -> '$' (same syntax as JS String.replace)."""
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


class _Compiled:
    def __init__(self, mod):
        self.exact = mod.EXACT
        self.pats = [(re.compile(p, re.M), _expand(r)) for p, r in mod.PATTERNS]
        self.pats_last = [(re.compile(p, re.M), _expand(r)) for p, r in mod.PATTERNS_LAST]
        keys = sorted((k for k in mod.EXACT if len(k) >= MIN_PHRASE), key=len, reverse=True)
        self.phrase_re = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(k) for k in keys) + r")(?!\w)") if keys else None
        self.rule_titles = mod.RULE_TITLES
        self._titles_cache = {"key": None, "map": {}}

    def titles(self) -> dict:
        """rule title (English) -> localised title, read from the loaded rulesets (cached per load)."""
        from . import core
        all_rs = core.load_rulesets()
        if self._titles_cache["key"] is not id(all_rs):
            m = {}
            for k, rs in all_rs.items():
                if k == "_default":
                    continue
                loc = self.rule_titles.get(rs["id"], {})
                for r in rs.get("rules", []):
                    if r.get("title") and loc.get(r["id"]):
                        m[r["title"]] = loc[r["id"]]
            self._titles_cache.update(key=id(all_rs), map=m)
        return self._titles_cache["map"]


_compiled: dict = {}


def _loc(lang: str) -> _Compiled:
    if lang not in _compiled:
        _compiled[lang] = _Compiled(_LOCALES[lang])
    return _compiled[lang]


def translate(s: str, lang: str) -> str:
    """Translate one string into `lang` (no-op for English)."""
    if lang == "en" or lang not in _LOCALES or not isinstance(s, str) or not s:
        return s
    stripped = s.strip()
    if not stripped:
        return s
    lead, trail = s[:len(s) - len(s.lstrip())], s[len(s.rstrip()):]
    if "\n" in stripped:
        return lead + "\n".join(translate(line, lang) for line in stripped.split("\n")) + trail
    L = _loc(lang)
    out = L.exact.get(stripped)
    if out is None:
        out = L.titles().get(stripped)
    if out is None:
        out = stripped
        for pat, rep in L.pats:
            out = pat.sub(rep, out)
        if L.phrase_re:
            out = L.phrase_re.sub(lambda m: L.exact.get(m.group(0), m.group(0)), out)
        for pat, rep in L.pats_last:
            out = pat.sub(rep, out)
    return lead + out + trail


def tr(s: str) -> str:
    return translate(s, LANG.get())


def localize(o, lang: str):
    """Translate a JSON payload recursively for `lang`; returned unchanged for English."""
    if lang == "en" or lang not in _LOCALES:
        return o
    if isinstance(o, str):
        return translate(o, lang)
    if isinstance(o, list):
        return [localize(x, lang) for x in o]
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if isinstance(v, (dict, list)):
                out[k] = localize(v, lang)
            elif isinstance(v, str) and (k not in SKIP_KEYS or k in FORCE_KEYS):
                out[k] = translate(v, lang)
            else:
                out[k] = v
        return out
    return o


def tr_obj(o):
    return localize(o, LANG.get())


def dictionary_payload(lang: str) -> dict:
    """Dictionary served to the UI (same source as the server side)."""
    if lang == "en":
        return {"lang": "en", "exact": {}, "patterns": [], "patterns_last": [], "min_phrase": MIN_PHRASE}
    L = _loc(lang)
    exact = dict(L.exact)
    exact.update(L.titles())
    return {"lang": lang, "exact": exact, "patterns": _LOCALES[lang].PATTERNS,
            "patterns_last": _LOCALES[lang].PATTERNS_LAST, "min_phrase": MIN_PHRASE}


# ---------------- FastAPI ----------------
try:
    from fastapi.responses import JSONResponse

    class I18nJSONResponse(JSONResponse):
        """Default JSON response of the app: localised when the request selected a non-English UI language."""

        def render(self, content) -> bytes:
            content = localize(content, LANG.get())
            return json.dumps(content, ensure_ascii=False, allow_nan=False, indent=None,
                              separators=(",", ":")).encode("utf-8")
except ImportError:  # used outside FastAPI (CLI)
    I18nJSONResponse = None


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["tr"] and len(sys.argv) > 3:
        for a in sys.argv[3:]:
            print(translate(a, sys.argv[2]))
    else:
        print("python -m backend.i18n tr <lang> '<text>'   try translating a string")
