"""Quét giao diện ở chế độ EN bằng Playwright: đi qua mọi màn, mở mọi hồ sơ theo trạng thái, bung mọi dòng tiêu chí,
liệt kê text node / placeholder / title còn dính dấu tiếng Việt (ngoài vùng nguyên văn). Mục tiêu: 0 dòng.
  python scan_en.py [url]      (server chạy sẵn, GRANTLENS_LLM=mock)"""
import asyncio, re, sys, pathlib, json
from playwright.async_api import async_playwright

SP = pathlib.Path(__file__).parent
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8022/"
acc = {}
for line in (pathlib.Path(r"E:/AI/project_earn_money/FILE_CHUA_NEN/grantlens-qwen/data/demo-accounts.txt")).read_text(encoding="utf-8").splitlines():
    m = re.match(r"(officer|manager|auditor)\s+(\S+)\s+(\S+)", line)
    if m:
        acc[m.group(2)] = m.group(3)

COLLECT = r"""
() => {
  const VI=/[àáảãạăằắẳẵặâầấẩẫậđèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ]/i;
  const SKIP='script,style,textarea,input,pre,.doc,.rquote,.qt,.letter,.facts,.notranslate,#whoname,#langlink';
  const out=[];
  const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  let n;
  while((n=w.nextNode())){const p=n.parentElement;if(!p||p.closest(SKIP))continue;
    if(!p.closest('.view.on, #login:not([hidden]), .modal.on, .topbar, aside, .toast.on'))continue;
    const v=n.data.trim();if(v&&VI.test(v))out.push((p.tagName.toLowerCase()+(p.className?'.'+String(p.className).split(' ')[0]:''))+' | '+v.slice(0,160));}
  for(const el of document.querySelectorAll('[placeholder],[title]')){for(const a of ['placeholder','title']){const v=el.getAttribute(a);if(v&&VI.test(v))out.push('@'+a+' | '+v.slice(0,160));}}
  return out;
}
"""


async def main():
    found = {}
    errs = []

    async def grab(pg, where):
        for line in await pg.evaluate(COLLECT):
            found.setdefault(line, set()).add(where)

    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1400, "height": 900})
        host = re.sub(r"^https?://", "", URL).split("/")[0].split(":")[0]
        await ctx.add_cookies([{"name": "gl_lang", "value": "en", "domain": host, "path": "/"}])
        pg = await ctx.new_page()
        pg.on("pageerror", lambda e: errs.append(str(e)))
        dialogs = []
        pg.on("dialog", lambda d: (dialogs.append(d.message), asyncio.ensure_future(d.dismiss())))
        await pg.goto(URL); await pg.wait_for_timeout(1500)
        await grab(pg, "login")
        await pg.fill("#lg-user", "pham.van.quyet"); await pg.fill("#lg-pass", acc["pham.van.quyet"]); await pg.click("#login button[type=submit]")
        await pg.wait_for_timeout(3000)
        await grab(pg, "dash")
        print("title:", await pg.title(), "| lang:", await pg.evaluate("document.documentElement.lang"))
        await pg.click("#postaudit button"); await pg.wait_for_timeout(1200); await grab(pg, "dash/postaudit")
        for v in ["cases", "rules", "bias", "audit", "ingest"]:
            await pg.keyboard.press({"cases": "2", "rules": "3", "bias": "4", "audit": "5", "ingest": "6"}[v]); await pg.wait_for_timeout(1800)
            await grab(pg, v)
        # rules: từng bộ
        await pg.keyboard.press("3"); await pg.wait_for_timeout(500)
        ids = await pg.evaluate("Array.from(document.querySelectorAll('#rulesetsel option')).map(o=>o.value)")
        for rid in ids:
            await pg.select_option("#rulesetsel", rid); await pg.wait_for_timeout(1500); await grab(pg, "rules/" + rid)
        # cases: một hồ sơ mỗi trạng thái
        cases = await pg.evaluate("fetch('/api/cases').then(r=>r.json()).then(d=>d.cases)")
        seen = {}
        for c in cases:
            seen.setdefault(c["status"], c["id"])
        for st, cid in seen.items():
            await pg.evaluate(f"go('case','{cid}')"); await pg.wait_for_timeout(2200)
            await grab(pg, f"case/{st}/{cid}")
            if await pg.is_visible("#spotmodal.on"):
                await grab(pg, f"case/{st}/{cid}/spot")
                await pg.click("#spotmodal .vopts button"); await pg.wait_for_timeout(1500)
                await grab(pg, f"case/{st}/{cid}/after-spot")
            await pg.evaluate("document.querySelectorAll('.vrow').forEach(r=>r.classList.add('open'))"); await pg.wait_for_timeout(300)
            await grab(pg, f"case/{st}/{cid}/rows")
            if await pg.is_visible("#ccbox button.btn.sm.ghost"):
                await pg.evaluate("typeof showAttach==='function'&&showAttach()"); await pg.wait_for_timeout(300); await grab(pg, f"case/{st}/{cid}/attach")
        await pg.screenshot(path=str(SP / "ui-en-case.png"), full_page=False)
        await pg.keyboard.press("1"); await pg.wait_for_timeout(1000)
        await pg.screenshot(path=str(SP / "ui-en-dash.png"))
        await b.close()
    print("\n=== còn dính tiếng Việt:", len(found), "dòng ===")
    for k in sorted(found):
        print(f"[{'/'.join(sorted(found[k]))[:60]}] {k}")
    print("\nhộp thoại:", dialogs)
    print("lỗi JS:", errs or "không")


asyncio.run(main())
