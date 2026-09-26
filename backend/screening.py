"""screening.py — denied-party screening layer.

Data sources (data/external/, see the README there):
  ASIC  Banned & Disqualified Persons (7.2k individuals, with effective dates)
  DFAT  Australian Sanctions Consolidated List (11k rows, individuals + entities, aliases grouped by Reference)
  ABN   ABR Bulk Extract — looked up through a SQLite FTS index (built with: python -m backend.abn_index)

Same philosophy as the assessment: screening only SUPPLIES EVIDENCE (hit + match score + source);
it never rejects an application by itself; hits at the possible level or above must be reviewed by an officer.
"""
import csv, re, sqlite3, unicodedata
from datetime import date, datetime
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

EXT = Path(__file__).resolve().parent.parent / "data" / "external"
ABN_DB = EXT / "abn" / "abn.sqlite"
STRONG, WEAK = 0.90, 0.78  # strong-match / possible-match thresholds


def norm(name: str) -> str:
    """Normalise a name for matching: strip accents, upper-case, keep letters+digits only, collapse whitespace."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).upper()
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _sorted_tokens(s: str) -> str:
    return " ".join(sorted(s.split()))


def score(query_norm: str, cand_norm: str) -> float:
    """Match score 0..1: max of the ratio on token-sorted strings and token Jaccard (tolerates reordered names)."""
    if not query_norm or not cand_norm:
        return 0.0
    r = SequenceMatcher(None, _sorted_tokens(query_norm), _sorted_tokens(cand_norm)).ratio()
    qa, ca = set(query_norm.split()), set(cand_norm.split())
    j = len(qa & ca) / len(qa | ca) if qa | ca else 0.0
    return round(max(r, 0.55 * j + 0.45 * r), 3)


def _parse_dmy(s):
    try:
        return datetime.strptime(s.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None


# ---------------- ASIC banned persons ----------------
@lru_cache(maxsize=1)
def load_asic():
    p = next((EXT / "sanctions" / "individuals" / "csv").glob("*.csv"), None)
    if not p:
        return []
    out = []
    with open(p, encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            raw = row.get("BD_PER_NAME", "")
            # "ABBOTT, BILL" -> add the variant "BILL ABBOTT"
            names = [raw]
            if "," in raw:
                last, _, first = raw.partition(",")
                names.append(f"{first.strip()} {last.strip()}")
            end = _parse_dmy(row.get("BD_PER_END_DT", ""))
            out.append({
                "name": raw, "norms": [norm(n) for n in names],
                "type": row.get("BD_PER_TYPE", ""), "doc": row.get("BD_PER_DOC_NUM", ""),
                "start": row.get("BD_PER_START_DT", ""), "end": row.get("BD_PER_END_DT", ""),
                "active": end is None or end >= date.today(),
                "state": row.get("BD_PER_ADD_STATE", ""), "country": row.get("BD_PER_ADD_COUNTRY", ""),
            })
    return out


# ---------------- DFAT consolidated list ----------------
@lru_cache(maxsize=1)
def load_dfat():
    p = EXT / "sanctions" / "companies" / "Australian_Sanctions_Consolidated_List.xlsx"
    if not p.exists():
        return []
    from openpyxl import load_workbook
    ws = load_workbook(p, read_only=True).active
    rows = ws.iter_rows(values_only=True)
    header = [str(h or "") for h in next(rows)]
    idx = {h: i for i, h in enumerate(header)}
    def g(r, col):
        i = idx.get(col)
        return str(r[i]).strip() if i is not None and i < len(r) and r[i] is not None else ""
    groups = {}
    for r in rows:
        ref = re.sub(r"[a-z]+$", "", g(r, "Reference"))  # 2a, 2b -> 2
        if not ref:
            continue
        e = groups.setdefault(ref, {"ref": ref, "names": [], "type": "", "birth": "", "citizenship": "", "committees": ""})
        nm = g(r, "Name of Individual or Entity")
        if nm:
            e["names"].append({"name": nm, "norm": norm(nm), "kind": g(r, "Name Type")})
        for k, col in [("type", "Type"), ("birth", "Date of Birth"), ("citizenship", "Citizenship"), ("committees", "Committees")]:
            e[k] = e[k] or g(r, col)
    return list(groups.values())


# ---------------- matching ----------------
def match_asic(name: str, top: int = 5):
    q = norm(name)
    if len(q) < 4:
        return []
    hits = []
    for rec in load_asic():
        s = max(score(q, n) for n in rec["norms"])
        if s >= WEAK:
            hits.append({"source": "ASIC Banned & Disqualified", "score": s,
                         "matched_name": rec["name"], "detail": rec["type"],
                         "period": f"{rec['start']} → {rec['end'] or '...'}",
                         "active": rec["active"], "strength": "strong" if s >= STRONG else "possible"})
    hits.sort(key=lambda h: (-h["score"], not h["active"]))
    return hits[:top]


def match_dfat(name: str, want_type: str = None, top: int = 5):
    q = norm(name)
    if len(q) < 4:
        return []
    hits = []
    for e in load_dfat():
        if want_type and e["type"] and e["type"].lower() != want_type.lower():
            continue
        best = max(((score(q, n["norm"]), n) for n in e["names"]), key=lambda t: t[0], default=(0, None))
        s, n = best
        if s >= WEAK and n:
            hits.append({"source": "DFAT Consolidated List", "score": s, "ref": e["ref"],
                         "matched_name": n["name"], "entity_type": e["type"],
                         "detail": f"{e['citizenship'] or e['birth'] or ''} · {e['committees'][:60]}".strip(" ·"),
                         "n_aliases": len(e["names"]), "active": True,
                         "strength": "strong" if s >= STRONG else "possible"})
    hits.sort(key=lambda h: -h["score"])
    return hits[:top]


# ---------------- ABN lookup ----------------
def abn_status():
    if not ABN_DB.exists():
        return {"indexed": False, "n": 0}
    try:
        con = sqlite3.connect(f"file:{ABN_DB}?mode=ro", uri=True)
        n = con.execute("SELECT count(*) FROM abn").fetchone()[0]
        done = con.execute("SELECT value FROM meta WHERE key='done'").fetchone()
        con.close()
        return {"indexed": bool(done), "building": not done, "n": n}
    except sqlite3.Error:
        return {"indexed": False, "building": True, "n": 0}


def abn_lookup(org_name: str, top: int = 5):
    st = abn_status()
    if not st["n"]:
        return {**st, "hits": []}
    q = norm(org_name)
    toks = [t for t in q.split() if len(t) > 1]
    if not toks:
        return {**st, "hits": []}
    con = sqlite3.connect(f"file:{ABN_DB}?mode=ro", uri=True)
    try:
        fts = " ".join(f'"{t}"' for t in toks)
        rows = con.execute(
            "SELECT a.abn, a.name, a.status, a.entity_type, a.state, a.postcode FROM abn_fts f "
            "JOIN abn a ON a.rowid = f.rowid WHERE abn_fts MATCH ? LIMIT 40", (fts,)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    hits = []
    for abn, nm, status, etype, state, pc in rows:
        s = score(q, norm(nm))
        if s >= 0.7:
            hits.append({"abn": abn, "name": nm, "status": status, "entity_type": etype,
                         "state": state, "postcode": pc, "score": s})
    hits.sort(key=lambda h: -h["score"])
    return {**st, "hits": hits[:top]}


# ---------------- case-level ----------------
def screen_case(applicant: str, org: str) -> dict:
    res = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "query": {"applicant": applicant or "", "org": org or ""},
        "applicant": {"asic": match_asic(applicant) if applicant else [],
                      "dfat": match_dfat(applicant, want_type="Individual") if applicant else []},
        "org": {"dfat": match_dfat(org, want_type="Entity") if org else [],
                "abn": abn_lookup(org) if org else {"indexed": ABN_DB.exists(), "n": 0, "hits": []}},
        "sources": {"asic": len(load_asic()), "dfat": len(load_dfat()), "abn": abn_status()},
    }
    all_hits = res["applicant"]["asic"] + res["applicant"]["dfat"] + res["org"]["dfat"]
    active_hits = [h for h in all_hits if h.get("active")]
    res["n_hits"] = len(all_hits)
    res["risk"] = ("review_strong" if any(h["strength"] == "strong" for h in active_hits)
                   else "review" if active_hits
                   else "note_expired" if all_hits else "clear")
    return res
