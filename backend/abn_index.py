"""abn_index.py — lập chỉ mục ABR Bulk Extract (1 lần) vào SQLite FTS5.
Chạy:  python -m backend.abn_index
Đọc STREAM trực tiếp từ 2 file zip (không giải nén 12.6 GB), ~20.5 triệu bản ghi.
Kết quả: data/external/abn/abn.sqlite (bảng abn + abn_fts) — backend/screening.py tra cứu tên tổ chức.
Chạy lại an toàn: xoá file cũ và lập lại từ đầu.
"""
import re, sqlite3, sys, time, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from .screening import EXT, ABN_DB, norm

ZIPS = sorted((EXT / "abn").glob("public_split_*.zip"))


def iter_records():
    for zp in ZIPS:
        with zipfile.ZipFile(zp) as z:
            for info in sorted(z.infolist(), key=lambda i: i.filename):
                with z.open(info) as f:
                    for _, el in ET.iterparse(f, events=("end",)):
                        if el.tag != "ABR":
                            continue
                        abn_el = el.find("ABN")
                        abn = (abn_el.text or "").strip() if abn_el is not None else ""
                        status = abn_el.get("status", "") if abn_el is not None else ""
                        et_el = el.find("EntityType/EntityTypeInd")
                        etype = (et_el.text or "") if et_el is not None else ""
                        me = el.find("MainEntity")
                        le = el.find("LegalEntity")
                        name = state = pc = ""
                        holder = me if me is not None else le
                        if holder is not None:
                            n = holder.find("NonIndividualName/NonIndividualNameText")
                            if n is not None:
                                name = n.text or ""
                            else:
                                gn = [x.text or "" for x in holder.findall("IndividualName/GivenName")]
                                fn = holder.find("IndividualName/FamilyName")
                                name = " ".join(gn + ([fn.text or ""] if fn is not None else [])).strip()
                            st = holder.find("BusinessAddress/AddressDetails/State")
                            pce = holder.find("BusinessAddress/AddressDetails/Postcode")
                            state = st.text or "" if st is not None else ""
                            pc = pce.text or "" if pce is not None else ""
                        if abn and name:
                            yield abn, name, status, etype, state, pc
                        el.clear()
                print(f"  xong {info.filename}", flush=True)


def main():
    if ABN_DB.exists():
        ABN_DB.unlink()
    con = sqlite3.connect(ABN_DB)
    con.executescript("""
      PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-200000;
      CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
      CREATE TABLE abn (abn TEXT, name TEXT, status TEXT, entity_type TEXT, state TEXT, postcode TEXT);
      CREATE VIRTUAL TABLE abn_fts USING fts5(name_norm, content='');
    """)
    t0, n, batch = time.time(), 0, []
    for rec in iter_records():
        batch.append(rec)
        if len(batch) >= 20000:
            n = _flush(con, batch, n, t0)
    n = _flush(con, batch, n, t0)
    con.execute("INSERT INTO meta VALUES ('done', datetime('now'))")
    con.commit(); con.close()
    print(f"HOÀN TẤT: {n:,} bản ghi trong {(time.time()-t0)/60:.1f} phút → {ABN_DB}")


def _flush(con, batch, n, t0):
    if not batch:
        return n
    con.executemany("INSERT INTO abn VALUES (?,?,?,?,?,?)", batch)
    con.executemany("INSERT INTO abn_fts(rowid, name_norm) VALUES (?,?)",
                    [(n + i + 1, norm(b[1])) for i, b in enumerate(batch)])
    con.commit()
    n += len(batch)
    if n % 1000000 < 20000:
        print(f"  {n:,} bản ghi · {(time.time()-t0)/60:.1f} phút", flush=True)
    batch.clear()
    return n


if __name__ == "__main__":
    sys.exit(main())
