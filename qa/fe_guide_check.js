/* FE 가이드 준수 자동 측정 (Playwright) — hermes-fleet 웹 모니터를 실제 브라우저로 열어 수치로 채점한다.
   실행: scripts/monitor.sh qa      (docker compose --profile qa run --rm playwright)
   환경: MONITOR_URL(기본 http://monitor:8787), QA_OUT(기본 /qa/out), QA_STRICT_WIDTHS(기본 1920 — 이 폭에서의 위반은 실패)
   결과: QA_OUT/report.json + 탭별 스크린샷(라이트/다크). 위반이 하나라도 있으면 종료 코드 1 (QA 의 VERDICT: FAIL 근거). */
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const URL = process.env.MONITOR_URL || "http://monitor:8787";
const OUT = process.env.QA_OUT || "/qa/out";
const STRICT = (process.env.QA_STRICT_WIDTHS || "1920").split(",").map(Number);
const TABS = ["overview", "workflow", "crew", "repos", "repo", "tickets", "timeline", "logs"];
const RULES = { topH: 56, lnbOpen: 248, lnbRail: 68, triggerGap: 16, gutterX: 24, gutterY: 16, footerH: 10, maxGapBottom: 80 };

fs.mkdirSync(OUT, { recursive: true });
const findings = [];
const note = (level, check, expected, actual, where = "") => findings.push({ level, check, expected: String(expected), actual: String(actual), where });
const expect = (strict, check, ok, expected, actual, where) => { if (!ok) note(strict ? "FAIL" : "WARN", check, expected, actual, where); return ok; };

async function measurePanel(page) {
  return page.evaluate(() => {
    let maxB = 0;
    for (const el of document.querySelectorAll("main .panel.active *")) {
      const cs = getComputedStyle(el);
      if (cs.position === "fixed" || cs.display === "none" || cs.visibility === "hidden") continue;
      const r = el.getBoundingClientRect();
      if (r.height > 0 && r.bottom > maxB && r.bottom <= innerHeight + 2) maxB = r.bottom;
    }
    window.scrollTo(0, 99999);
    const sm = scrollY; window.scrollTo(0, 0);
    return { docH: document.documentElement.scrollHeight, docW: document.documentElement.scrollWidth, winH: innerHeight, winW: innerWidth, scrollMax: sm, gapBottom: Math.round(innerHeight - maxB) };
  });
}

async function shellMetrics(page) {
  return page.evaluate(() => {
    const r = (s) => { const el = document.querySelector(s); return el ? el.getBoundingClientRect() : null; };
    const lnb = r("#lnb"), top = r(".top"), tg = r("#lnb-toggle"), footer = r(".site-footer"), head = r("#lnb > div");
    const panel = document.querySelector("main .panel.active");
    const firstCard = panel ? panel.querySelector(".card, .tile") : null;
    const card = firstCard ? firstCard.getBoundingClientRect() : null;
    const cs = (s, p) => { const el = document.querySelector(s); return el ? getComputedStyle(el)[p] : null; };
    return {
      mode: document.body.dataset.lnb, lnbW: lnb && Math.round(lnb.width), lnbRight: lnb && Math.round(lnb.right), lnbBottom: lnb && Math.round(lnb.bottom), lnbHeadH: head && Math.round(head.height),
      topH: top && Math.round(top.height), triggerGap: tg && lnb && Math.round(tg.left - lnb.right), contentGap: card && lnb && Math.round(card.left - lnb.right),
      topGap: card && top && Math.round(card.top - top.bottom), rightGap: panel && Math.round(innerWidth - panel.getBoundingClientRect().right),
      footer: footer && { top: Math.round(footer.top), h: Math.round(footer.height), w: Math.round(footer.width), bg: cs(".site-footer", "backgroundColor"), text: document.querySelector(".site-footer").textContent.trim() },
      tailwind: cs("#tw-probe", "display") === "none", font: cs("body", "fontFamily"), clock: (document.querySelector("#clock") || {}).textContent || "",
      current: document.querySelector('.lnb-item[aria-current="page"]')?.dataset.tab, topTabs: !!document.querySelector("header .tabs"),
    };
  });
}

async function centered(page, sel) {
  return page.evaluate((sel) => { const d = document.querySelector(sel); if (!d) return null; const r = d.getBoundingClientRect(); return { open: d.open, dx: Math.round((r.left + r.width / 2) - innerWidth / 2), dy: Math.round((r.top + r.height / 2) - innerHeight / 2), h: Math.round(r.height), w: Math.round(r.width) }; }, sel);
}

(async () => {
  const browser = await chromium.launch({ args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const report = { url: URL, at: new Date().toISOString(), viewports: {}, findings };
  for (const [w, h] of [[1920, 1080], [1366, 768]]) {
    const strict = STRICT.includes(w);
    const ctx = await browser.newContext({ viewport: { width: w, height: h }, timezoneId: "America/New_York", locale: "ko-KR" });   // 브라우저 시간대가 달라도 KST 로 보여야 한다
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    page.on("console", (m) => { if (m.type() === "error") errors.push("console: " + m.text().slice(0, 160)); });
    await page.goto(`${URL}/#overview`, { waitUntil: "load", timeout: 90000 });
    await page.waitForTimeout(7000);
    const vp = { tabs: {}, shell: {}, modals: {}, theme: {}, errors };
    const sh = await shellMetrics(page);
    vp.shell.open = sh;
    const tag = `${w}x${h}`;
    if (w >= 1024) {
      expect(strict, "상단 바 높이 56px", sh.topH === RULES.topH, RULES.topH, sh.topH, tag);
      expect(strict, "사이드바 헤더 높이 56px", sh.lnbHeadH === RULES.topH, RULES.topH, sh.lnbHeadH, tag);
      expect(strict, "사이드바 펼침 폭 248px", sh.lnbW === RULES.lnbOpen, RULES.lnbOpen, sh.lnbW, tag);
      expect(strict, "트리거는 사이드바 경계에서 16px", sh.triggerGap === RULES.triggerGap, RULES.triggerGap, sh.triggerGap, tag);
      expect(strict, "본문 좌측 여백 24px", sh.contentGap === RULES.gutterX, RULES.gutterX, sh.contentGap, tag);
      expect(strict, "본문 우측 여백 24px", sh.rightGap === RULES.gutterX, RULES.gutterX, sh.rightGap, tag);
      expect(strict, "본문 상단 여백 16px", sh.topGap === RULES.gutterY, RULES.gutterY, sh.topGap, tag);
    }
    expect(strict, "상단 탭 내비게이션 없음(LNB 가 유일한 1차 메뉴)", !sh.topTabs, "없음", sh.topTabs ? "있음" : "없음", tag);
    expect(strict, "푸터 10px 검정 EDUMGT", sh.footer && sh.footer.h === RULES.footerH && sh.footer.bg === "rgb(0, 0, 0)" && sh.footer.text === "EDUMGT" && sh.footer.top === h - RULES.footerH && sh.footer.w === w, "10px #000 EDUMGT 전폭", JSON.stringify(sh.footer), tag);
    expect(strict, "사이드바는 푸터 위에서 끝남", sh.lnbBottom === h - RULES.footerH, h - RULES.footerH, sh.lnbBottom, tag);
    expect(strict, "Tailwind CDN 로드", sh.tailwind, true, sh.tailwind, tag);
    expect(strict, "Pretendard 글꼴", /Pretendard/.test(sh.font), "Pretendard", sh.font.slice(0, 40), tag);
    expect(strict, "시계가 KST 표기(브라우저 America/New_York)", /KST$/.test(sh.clock), "… KST", sh.clock, tag);
    const now = await (await page.request.get(`${URL}/api/overview`)).json();
    expect(strict, "시계 시각 = 서버(KST) 시각", sh.clock.slice(0, 13) === String(now.now).replace("T", " ").slice(0, 13), String(now.now).slice(0, 13), sh.clock.slice(0, 13), tag);

    for (const t of TABS) {
      await page.click(`.lnb-item[data-tab=${t}]`);
      if (t === "repo") { const opts = await page.$$eval("#r-select option", (o) => o.map((x) => x.value).filter(Boolean)); if (opts.length) await page.selectOption("#r-select", opts[0]); }
      await page.waitForTimeout(t === "crew" ? 5000 : 2500);
      const m = await measurePanel(page);
      vp.tabs[t] = m;
      expect(strict, `[${t}] 세로 스크롤 없음`, m.docH <= m.winH && m.scrollMax === 0, `≤ ${m.winH}`, `${m.docH} (scrollMax ${m.scrollMax})`, tag);
      expect(true, `[${t}] 가로 스크롤 없음`, m.docW <= m.winW, `≤ ${m.winW}`, m.docW, tag);
      if (w >= 1024) expect(strict, `[${t}] 바닥 빈 띠 ≤ ${RULES.maxGapBottom}px`, m.gapBottom <= RULES.maxGapBottom, `≤ ${RULES.maxGapBottom}`, m.gapBottom, tag);
      await page.screenshot({ path: path.join(OUT, `${tag}-${t}.png`) });
    }
    if (w >= 1024) {
      // 사이드바 레일 · 지속성
      await page.click("#lnb-toggle"); await page.waitForTimeout(600);
      const rail = await shellMetrics(page); vp.shell.rail = rail;
      expect(strict, "레일 모드 폭 68px", rail.mode === "rail" && rail.lnbW === RULES.lnbRail, RULES.lnbRail, `${rail.mode} ${rail.lnbW}`, tag);
      expect(strict, "레일 모드에서도 본문 여백 24px", rail.contentGap === RULES.gutterX, RULES.gutterX, rail.contentGap, tag);
      await page.screenshot({ path: path.join(OUT, `${tag}-rail.png`) });
      await page.reload({ waitUntil: "load" }); await page.waitForTimeout(5000);
      const persisted = await page.evaluate(() => document.body.dataset.lnb);
      expect(strict, "사이드바 상태 localStorage 지속", persisted === "rail", "rail", persisted, tag);
      await page.click("#lnb-toggle"); await page.waitForTimeout(400);
      // 모달 정중앙
      await page.click("#sched-open"); await page.waitForTimeout(700);
      vp.modals.sched = await centered(page, "#sched-dialog");
      expect(strict, "스케줄 모달 정중앙", vp.modals.sched && vp.modals.sched.open && Math.abs(vp.modals.sched.dx) <= 1 && Math.abs(vp.modals.sched.dy) <= 1, "dx=dy=0", JSON.stringify(vp.modals.sched), tag);
      await page.screenshot({ path: path.join(OUT, `${tag}-modal-sched.png`) });
      await page.keyboard.press("Escape"); await page.waitForTimeout(300);
      const closedByEsc = await page.evaluate(() => !document.querySelector("#sched-dialog").open);
      expect(strict, "모달 Esc 닫힘", closedByEsc, true, closedByEsc, tag);
      await page.click(".lnb-item[data-tab=repos]"); await page.waitForTimeout(1500);
      const stopBtn = await page.$("#grid-repos button[data-act=stop]:not([disabled])");
      if (stopBtn) { await page.evaluate((b) => b.click(), stopBtn); await page.waitForTimeout(400); vp.modals.confirm = await centered(page, "#confirm-dialog"); expect(strict, "확인 모달(앱 모달, confirm 아님) 정중앙", vp.modals.confirm && vp.modals.confirm.open && Math.abs(vp.modals.confirm.dx) <= 1 && Math.abs(vp.modals.confirm.dy) <= 1, "dx=dy=0", JSON.stringify(vp.modals.confirm), tag); await page.keyboard.press("Escape"); }
      await page.click(".lnb-item[data-tab=tickets]"); await page.waitForTimeout(2000);
      const closedTicket = await page.evaluate(() => { const d = document.querySelector("#ticket-detail"); return { open: d.open, display: getComputedStyle(d).display }; });
      expect(strict, "닫힌 티켓 모달이 화면에 남지 않음", !closedTicket.open && closedTicket.display === "none", "display none", JSON.stringify(closedTicket), tag);
      const row = await page.$("#grid-tickets .ag-row");
      if (row) { await row.click(); await page.waitForTimeout(2500); vp.modals.ticket = await centered(page, "#ticket-detail"); expect(strict, "티켓 상세 모달 정중앙", vp.modals.ticket && vp.modals.ticket.open && Math.abs(vp.modals.ticket.dx) <= 1 && Math.abs(vp.modals.ticket.dy) <= 1, "dx=dy=0", JSON.stringify(vp.modals.ticket), tag); await page.screenshot({ path: path.join(OUT, `${tag}-modal-ticket.png`) }); await page.keyboard.press("Escape"); }
      // 테마
      await page.click(".lnb-item[data-tab=overview]"); await page.waitForTimeout(1000);
      const bgLight = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      await page.click("#theme-toggle"); await page.click("#theme-toggle"); await page.waitForTimeout(1500);
      const dark = await page.evaluate(() => ({ theme: document.documentElement.dataset.theme, bg: getComputedStyle(document.body).backgroundColor, text: getComputedStyle(document.body).color, stored: localStorage.getItem("hermes.theme") }));
      vp.theme = { bgLight, dark };
      expect(strict, "다크 테마 토글(data-theme=dark, 배경 변경, 저장)", dark.theme === "dark" && dark.bg !== bgLight && dark.stored === "dark", "dark", JSON.stringify(dark), tag);
      for (const t of ["overview", "crew", "repos"]) { await page.click(`.lnb-item[data-tab=${t}]`); await page.waitForTimeout(t === "crew" ? 4000 : 1500); await page.screenshot({ path: path.join(OUT, `${tag}-${t}-dark.png`) }); }
      await page.click("#theme-toggle"); await page.waitForTimeout(500);
      const sys = await page.evaluate(() => (document.documentElement.dataset.theme || "none") + "|" + localStorage.getItem("hermes.theme"));
      expect(strict, "테마 시스템으로 복귀", sys === "none|system", "none|system", sys, tag);
    } else {
      // 모바일 시트
      await page.click("#lnb-toggle"); await page.waitForTimeout(500);
      const sheet = await page.evaluate(() => ({ mode: document.body.dataset.lnb, backdrop: !document.querySelector("#lnb-backdrop").hidden }));
      expect(strict, "모바일/좁은 폭 시트 모드", sheet.mode === "sheet" && sheet.backdrop, "sheet+dim", JSON.stringify(sheet), tag);
      await page.keyboard.press("Escape"); await page.waitForTimeout(300);
    }
    expect(strict, "콘솔/페이지 오류 없음", errors.length === 0, 0, errors.slice(0, 3).join(" | ") || 0, tag);
    report.viewports[tag] = vp;
    await ctx.close();
  }
  await browser.close();
  const fails = findings.filter((f) => f.level === "FAIL"), warns = findings.filter((f) => f.level === "WARN");
  report.summary = { fail: fails.length, warn: warns.length, verdict: fails.length ? "FAIL" : "PASS" };
  fs.writeFileSync(path.join(OUT, "report.json"), JSON.stringify(report, null, 2));
  const lines = [`FE 가이드 준수 측정 — ${URL} — ${report.summary.verdict} (FAIL ${fails.length} · WARN ${warns.length})`];
  for (const f of findings) lines.push(`  [${f.level}] ${f.where} ${f.check}: 기대 ${f.expected} / 실제 ${f.actual}`);
  if (!findings.length) lines.push("  모든 항목 통과");
  lines.push(`  리포트: ${path.join(OUT, "report.json")} · 스크린샷 ${fs.readdirSync(OUT).filter((f) => f.endsWith(".png")).length}장`);
  console.log(lines.join("\n"));
  fs.writeFileSync(path.join(OUT, "report.txt"), lines.join("\n") + "\n");
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error("FAILED", e); process.exit(2); });
