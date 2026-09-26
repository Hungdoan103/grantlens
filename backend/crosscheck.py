"""crosscheck.py — anti-cheating cross-check between the documents of ONE application.

Customer case: the form declares $500k revenue while the attached financial statements say $1.5M; forged dates.
Approach: an application may contain several documents, separated by a marker line
    === DOCUMENT: <document name> ===
(the part before the first marker is the "Main form"; attachments uploaded through the API insert the marker).

The engine is pure CODE (regex + number normalisation) — not an LLM — so results are reproducible and every figure
can be explained. Quantities are labelled by context and then compared by label:
  budget_total   total budget/cost (including the grand-total row inside [TABLE n] blocks)
  revenue        revenue / turnover
  phd_year       year the doctorate was awarded
  pd_pages       number of Project Description pages
  letter_pages   number of departmental-letter pages
The same label with different values across documents (or between the narrative and a table inside the same
document) -> a DISCREPANCY finding with source + context excerpt.
Also checks implausible dates: end date before start date, year in the future.

As with every other layer: this is EVIDENCE for the officer — the system never concludes fraud by itself.
"""
import re
from datetime import date

DOC_SPLIT = re.compile(r"^===\s*DOCUMENT:\s*(.+?)\s*===\s*$", re.M)
MAIN_DOC = "Main form"

CATEGORY_LABELS = {
    "budget_total": "Total budget",
    "revenue": "Revenue",
    "phd_year": "PhD year",
    "pd_pages": "Project description pages",
    "letter_pages": "Departmental letter pages",
}


def split_documents(text: str):
    """[(document name, body)] — the part before the first marker is the Main form."""
    parts = DOC_SPLIT.split(text)
    docs = []
    if parts[0].strip():
        docs.append((MAIN_DOC, parts[0]))
    for i in range(1, len(parts), 2):
        docs.append((parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else ""))
    return docs


def _money(s: str) -> int:
    return int(re.sub(r"[,.](?=\d{3}\b)", "", s).split(".")[0].replace(",", ""))


def _ctx(text: str, start: int, end: int, w: int = 55) -> str:
    return re.sub(r"\s+", " ", text[max(0, start - w):min(len(text), end + w)]).strip()


def extract_values(doc_name: str, text: str):
    """[(category, value, source, context)] — source distinguishes narrative vs table."""
    out = []
    # --- separate table blocks from narrative ---
    tables = [(m.group(1), m.group(0)) for m in re.finditer(r"\[TABLE (\d+)\](.*?)\[/TABLE \1\]", text, re.S)]
    prose = re.sub(r"\[TABLE (\d+)\].*?\[/TABLE \1\]", " ", text, flags=re.S)

    # --- amounts with a context label (narrative) ---
    for m in re.finditer(r"\$\s?([\d][\d,]*(?:\.\d+)?)", prose):
        before = prose[max(0, m.start() - 90):m.start()].lower()
        val = _money(m.group(1))
        if val < 1000:
            continue
        if re.search(r"(total|grand)[^.]*?(budget|cost|amount|request)|budget[^.]*?total", before) \
                or re.search(r"total (requested )?budget", before):
            out.append(("budget_total", val, doc_name, _ctx(prose, m.start(), m.end())))
        elif re.search(r"revenue|turnover", before):
            out.append(("revenue", val, doc_name, _ctx(prose, m.start(), m.end())))
    # --- amounts inside tables: grand total / total rows ---
    for tid, ttext in tables:
        for line in ttext.splitlines():
            if re.search(r"grand total|(^|\|)\s*total\b", line, re.I):
                amts = re.findall(r"\$\s?([\d][\d,]*)", line)
                if amts:
                    out.append(("budget_total", _money(amts[-1]), f"{doc_name} · TABLE {tid}",
                                re.sub(r"\s+", " ", line).strip()[:120]))
    # --- PhD year ---
    for m in re.finditer(r"Ph\.?\s?D\.?[^.\n]{0,90}?\b(19\d{2}|20\d{2})\b", text, re.I):
        out.append(("phd_year", int(m.group(1)), doc_name, _ctx(text, m.start(), m.end())))
    # --- page counts ---
    for m in re.finditer(r"Project Description[^.\n]{0,50}?\b(\d{1,2})\s?pages", text, re.I):
        out.append(("pd_pages", int(m.group(1)), doc_name, _ctx(text, m.start(), m.end())))
    for m in re.finditer(r"[Ll]etter[^.\n]{0,60}?\b(\d{1,2})\s?pages?", text):
        out.append(("letter_pages", int(m.group(1)), doc_name, _ctx(text, m.start(), m.end())))
    return out


def _date_sanity(doc_name: str, text: str):
    finds = []
    year_now = date.today().year
    for m in re.finditer(r"\b(\d{2})/(\d{2})/(\d{4})\b", text):
        y = int(m.group(3))
        if y > year_now + 1:
            finds.append({"category": "date_future", "label": "Date in the future",
                          "severity": "medium", "values": [{"value": m.group(0), "source": doc_name,
                                                            "context": _ctx(text, m.start(), m.end())}]})
    m = re.search(r"start(?:ed|ing)? (?:date|in)?[^.\n]{0,20}?(\d{4})[^.\n]{0,80}?end(?:ed|ing)? (?:date|in)?[^.\n]{0,20}?(\d{4})", text, re.I)
    if m and int(m.group(2)) < int(m.group(1)):
        finds.append({"category": "date_order", "label": "End date before start date",
                      "severity": "high", "values": [{"value": f"{m.group(1)} → {m.group(2)}", "source": doc_name,
                                                     "context": _ctx(text, m.start(), m.end())}]})
    return finds


def run(text: str) -> dict:
    """Cross-check the whole application. Returns {docs, findings, checked, risk}."""
    docs = split_documents(text)
    values, findings = [], []
    for name, body in docs:
        values.extend(extract_values(name, body))
        findings.extend(_date_sanity(name, body))
    by_cat = {}
    for cat, val, src, ctx in values:
        by_cat.setdefault(cat, []).append({"value": val, "source": src, "context": ctx})
    for cat, vals in by_cat.items():
        distinct = sorted({v["value"] for v in vals})
        if len(distinct) > 1:
            spread = (max(distinct) - min(distinct)) / max(distinct)
            findings.append({
                "category": cat, "label": CATEGORY_LABELS.get(cat, cat),
                "severity": "high" if cat in ("budget_total", "revenue") or spread > 0.2 else "medium",
                "values": vals, "distinct": distinct,
            })
    return {
        "n_docs": len(docs), "docs": [d[0] for d in docs],
        "checked": {cat: len(vs) for cat, vs in by_cat.items()},
        "findings": findings,
        "risk": ("mismatch_high" if any(f["severity"] == "high" for f in findings)
                 else "mismatch" if findings else "consistent"),
    }
