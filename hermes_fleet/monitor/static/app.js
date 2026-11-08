/* Hermes Fleet Monitor — AG Grid Community(표) + ApexCharts(차트). /api/* 를 10초마다 읽고, 로그는 SSE 로 따라간다. */
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const STATUSES = ["proposed", "todo", "in-progress", "review", "done", "blocked"];
  const STATUS_KO = { proposed: "승인 대기", todo: "대기", "in-progress": "진행 중", review: "리뷰 대기", done: "완료", blocked: "막힘" };
  const STAGES = ["pm", "design", "dev", "qa"];
  const STAGE_KO = { pm: "PM", design: "디자인", dev: "개발", qa: "QA" };
  const REFRESH_MS = 10000;
  const state = { overview: null, stats: null, repos: [], tickets: [], tab: "overview", es: null, logLines: [], repo: null, repoDetail: null, grids: {}, charts: {}, skew: 0 };

  // ---------- 유틸 ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTs = (iso) => (iso ? String(iso).replace("T", " ") : "-");
  const ago = (iso) => {
    if (!iso) return "-";
    const d = (Date.now() + state.skew - new Date(String(iso).replace(" ", "T")).getTime()) / 1000;   // 서버 시각 기준(브라우저 시간대가 달라도 맞도록)
    if (!isFinite(d)) return iso;
    if (d < 60) return `${Math.round(d)}초 전`;
    if (d < 3600) return `${Math.round(d / 60)}분 전`;
    if (d < 86400) return `${(d / 3600).toFixed(1)}시간 전`;
    return `${Math.round(d / 86400)}일 전`;
  };
  const dur = (s) => (s == null ? "-" : s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}분 ${Math.round(s % 60)}s` : `${(s / 3600).toFixed(1)}시간`);
  const mins = (s) => Math.round((s || 0) / 6) / 10;
  const badge = (text, cls = "", dotCls = "") => `<span class="badge ${cls}">${dotCls ? `<i class="${dotCls}"></i>` : ""}${esc(text)}</span>`;
  const stBadge = (st) => (st ? badge(`${STATUS_KO[st] || st}`, "", `st-${st}`) : "-");
  const verdictBadge = (v) => (v === "PASS" ? badge("PASS", "good") : v === "FAIL" ? badge("FAIL", "crit") : v === "ERROR" ? badge("ERROR", "serious") : badge(v || "-"));
  const toast = (msg, ok = false) => { const t = $("#toast"); t.textContent = msg; t.hidden = false; t.style.background = ok ? "var(--good)" : "var(--critical)"; clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 4500); };
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const isDark = () => window.matchMedia("(prefers-color-scheme: dark)").matches && document.documentElement.dataset.theme !== "light";
  const palette = () => ({
    status: STATUSES.map((s) => cssVar(`--st-${s}`)), verdict: [cssVar("--good"), cssVar("--critical"), cssVar("--serious")],
    stages: [cssVar("--series-1"), cssVar("--series-2"), cssVar("--series-3"), cssVar("--series-4")], merged: cssVar("--series-7"),
    text: cssVar("--text-2"), muted: cssVar("--muted"), grid: cssVar("--grid"), surface: cssVar("--surface"),
  });

  async function api(path, opts) {
    const r = await fetch(path, { cache: "no-store", ...opts });
    if (!r.ok) {
      let detail = `${r.status}`;
      try { detail = (await r.json()).detail || detail; } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    return r.json();
  }

  // ---------- 라이브러리 가용성 ----------
  const hasGrid = typeof agGrid !== "undefined" && typeof agGrid.createGrid === "function";
  const hasApex = typeof ApexCharts !== "undefined";
  if (hasGrid && agGrid.ModuleRegistry && agGrid.AllCommunityModule) agGrid.ModuleRegistry.registerModules([agGrid.AllCommunityModule]);
  if (!hasGrid || !hasApex) toast("AG Grid/ApexCharts 를 CDN 에서 불러오지 못했습니다. 인터넷 연결을 확인하세요.");

  // ---------- AG Grid ----------
  function gridTheme() {
    if (!hasGrid) return undefined;
    const dark = isDark();
    let t = agGrid.themeQuartz;
    if (dark && agGrid.colorSchemeDark) t = t.withPart(agGrid.colorSchemeDark);
    return t.withParams({
      backgroundColor: cssVar("--surface"), foregroundColor: cssVar("--text"), headerBackgroundColor: cssVar("--surface-2"),
      headerTextColor: cssVar("--muted"), borderColor: cssVar("--border"), accentColor: cssVar("--accent"), rowHoverColor: cssVar("--surface-2"),
      oddRowBackgroundColor: cssVar("--surface"), fontFamily: "inherit", fontSize: 13, headerFontSize: 12, spacing: 6, wrapperBorderRadius: 8,
      chromeBackgroundColor: cssVar("--surface-2"), browserColorScheme: dark ? "dark" : "light",
    });
  }

  class SwitchRenderer {
    init(p) {
      this.p = p;
      const d = p.data;
      const disabled = !d.fe_be_ok || !(state.overview && state.overview.config_writable);
      const title = !d.fe_be_ok ? "FE/BE 가 모두 확인되지 않아 대상이 될 수 없음" : !(state.overview && state.overview.config_writable) ? "fleet.yaml 에 쓸 수 없음(읽기 전용)" : d.selected ? "클릭하면 제외(exclude)" : "클릭하면 대상에 포함";
      this.eGui = document.createElement("label");
      this.eGui.className = "switch cell-switch";
      this.eGui.title = title;
      this.eGui.innerHTML = `<input type="checkbox" ${d.selected ? "checked" : ""} ${disabled ? "disabled" : ""}><span class="slider"></span>`;
      this.input = this.eGui.querySelector("input");
      this.eGui.addEventListener("click", (ev) => ev.stopPropagation());
      this.input.addEventListener("change", () => toggleRepo(d.name, this.input.checked, this.input));
    }
    getGui() { return this.eGui; }
    refresh(p) { this.p = p; this.input.checked = !!p.data.selected; return true; }
  }

  function makeGrid(key, el, columnDefs, opts = {}) {
    if (!hasGrid || !el) return null;
    if (state.grids[key]) return state.grids[key];
    const api = agGrid.createGrid(el, {
      theme: gridTheme(), columnDefs, rowData: [], suppressCellFocus: true, animateRows: false, enableCellTextSelection: true, ensureDomOrder: true,
      defaultColDef: { sortable: true, resizable: true, filter: false, minWidth: 70, suppressHeaderMenuButton: true, cellDataType: false },
      overlayNoRowsTemplate: `<span class="muted">${esc(opts.empty || "데이터가 없습니다.")}</span>`,
      ...opts.gridOptions,
    });
    state.grids[key] = api;
    return api;
  }
  const setRows = (key, rows) => state.grids[key] && state.grids[key].setGridOption("rowData", rows);
  const htmlCell = (fn) => (p) => (p.data ? fn(p.data, p) : "");
  const miniSeg = (c) => {
    const total = STATUSES.reduce((a, s) => a + (c?.[s] || 0), 0);
    if (!total) return `<span class="muted">-</span>`;
    return `<span class="mini-seg" title="${STATUSES.map((s) => `${STATUS_KO[s]} ${c[s] || 0}`).join(" · ")}">${STATUSES.filter((s) => c[s]).map((s) => `<span class="st-${s}" style="flex:${c[s]}"></span>`).join("")}</span>`;
  };

  // ---------- ApexCharts ----------
  function baseChart(type, height = 260) {
    const pal = palette();
    return {
      chart: { type, height, background: "transparent", toolbar: { show: false }, fontFamily: "inherit", animations: { enabled: false }, parentHeightOffset: 0 },
      theme: { mode: isDark() ? "dark" : "light" }, grid: { borderColor: pal.grid, strokeDashArray: 3, padding: { left: 12, right: 12 } },
      dataLabels: { enabled: false }, legend: { position: "bottom", labels: { colors: pal.text }, markers: { size: 5, shape: "square" }, itemMargin: { horizontal: 8 } },
      tooltip: { theme: isDark() ? "dark" : "light" }, xaxis: { labels: { style: { colors: pal.muted } }, axisBorder: { color: pal.grid }, axisTicks: { color: pal.grid } },
      yaxis: { labels: { style: { colors: pal.muted } } }, states: { active: { filter: { type: "none" } } }, stroke: { width: 0 },
    };
  }
  const deep = (a, b) => { for (const k of Object.keys(b)) { if (b[k] && typeof b[k] === "object" && !Array.isArray(b[k]) && a[k] && typeof a[k] === "object" && !Array.isArray(a[k])) deep(a[k], b[k]); else a[k] = b[k]; } return a; };
  function renderChart(key, el, options, emptyMsg) {
    if (!hasApex || !el) return;
    const empty = el.querySelector(".empty") || Object.assign(document.createElement("div"), { className: "empty" });
    if (emptyMsg) {
      if (state.charts[key]) { state.charts[key].destroy(); delete state.charts[key]; }
      empty.textContent = emptyMsg;
      if (!empty.parentNode) el.appendChild(empty);
      return;
    }
    if (empty.parentNode) empty.remove();
    if (state.charts[key]) { state.charts[key].updateOptions(options, false, false); return; }
    const ch = new ApexCharts(el, options);
    state.charts[key] = ch;
    ch.render();
  }
  function destroyCharts() { Object.values(state.charts).forEach((c) => c.destroy()); state.charts = {}; }

  // ---------- 탭 ----------
  function showTab(name) {
    state.tab = name;
    $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
    location.hash = name === "repo" && state.repo ? `repo=${state.repo}` : name;
    if (name === "logs") startLogStream();
    if (name === "timeline") loadTimeline();
    if (name === "tickets") loadTickets();
    if (name === "repo") { if (state.repo) loadRepo(state.repo); else { $("#r-empty").hidden = false; fillRepoSelect(); } }
    if (name === "workflow") loadWorkflow();
  }
  $$(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  // ---------- 개요 ----------
  function daemonView(d, sch) {
    if (d.paused) return { cls: "paused", text: "일시정지", color: "c-serious", icon: "⏸", sub: d.pause_note || "state/PAUSE 존재" };
    if (d.status === "running") return { cls: "running", text: "실행 중", color: "c-good", icon: "●", sub: `${d.method === "flock" ? "잠금 확인" : "로그 기준"} · 시작 ${fmtTs(d.started_at)}` };
    if (d.status === "stale") return { cls: "stale", text: "응답 없음", color: "c-warn", icon: "▲", sub: `로그 갱신 ${ago(d.log_mtime)} (종료 마커 없음)` };
    if (d.expected_running) return { cls: "missing", text: "미실행", color: "c-crit", icon: "✕", sub: "작동 시간대인데 데몬이 없음 — cron/flock 확인" };
    return { cls: "stopped", text: "대기", color: "c-muted", icon: "○", sub: sch.next_window_start ? `다음 시작 ${fmtTs(sch.next_window_start)}` : "시간대 밖" };
  }

  function kv(sel, rows) { $(sel).innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join(""); }

  function renderOverview(o) {
    const d = o.daemon, sch = o.schedule, llm = o.llm;
    const dv = daemonView(d, sch);
    $("#daemon-dot").className = `dot ${dv.cls}`;
    document.title = `${dv.text} · Hermes Fleet Monitor`;
    const total = Object.values(o.totals).reduce((a, b) => a + b, 0);
    const active = o.totals["in-progress"] + o.totals.review;
    const processed = o.repos.filter((r) => r.eligible).reduce((a, r) => a + (r.processed_count || 0), 0);
    const tiles = [
      { label: "데몬", value: `<span class="ic ${dv.color}">${dv.icon}</span><span class="${dv.color}">${dv.text}</span>`, sub: dv.sub },
      { label: "작동 시간대", value: sch.in_window ? `<span class="c-good">활성</span>` : `<span class="c-muted">비활성</span>`, sub: sch.in_window ? `종료까지 ${sch.minutes_until_end}분 · 새 작업 ${sch.can_start_work ? "가능" : "중단"}` : `${sch.active_hours || "항상"} · ${sch.next_window_start ? "다음 " + fmtTs(sch.next_window_start).slice(5) : ""}` },
      { label: "LLM 엔드포인트", value: llm.ok ? `<span class="c-good">정상</span>` : `<span class="c-crit">불가</span>`, sub: llm.ok ? `${llm.model}${llm.missing_models.length ? ` · 누락: ${llm.missing_models.join(", ")}` : ""}` : (llm.error || "") },
      { label: "대상 레포", value: `${o.eligible_count}`, sub: `${o.repos.length}개 저장소 중 선택 ${o.repos.filter((r) => r.selected).length} · 커서 ${o.cursor || "-"}` },
      { label: "진행/리뷰 티켓", value: `${active}`, sub: `전체 ${total} · 승인 대기 ${o.totals.proposed} · 막힘 ${o.totals.blocked} · 처리 누적 ${processed}` },
      { label: "전개 범위", value: state.workflowTotals ? `<span style="font-size:16px">AWS <span class="c-good">${state.workflowTotals.aws}</span> · push <span class="c-good">${state.workflowTotals.push}</span> / ${state.workflowTotals.targets}</span>` : `<span class="c-muted">…</span>`, sub: state.workflowTotals ? `대상 ${state.workflowTotals.targets}개 중 자동 배포 ${state.workflowTotals.deploy_auto} · CI/CD ${state.workflowTotals.cicd}` : "워크플로 탭에서 계산" },
      { label: "현재 작업", value: d.activity ? `<span class="c-good" style="font-size:16px">${esc(d.activity.kind === "ticket" ? d.activity.id : d.activity.kind)}</span>` : `<span class="c-muted">없음</span>`, sub: d.activity ? `${d.activity.repo}${d.activity.stage ? ` · ${d.activity.stage.name} #${d.activity.stage.iteration}` : ""} · ${ago(d.activity.since)} 시작` : d.status === "running" ? "대기(폴링) 중" : "-" },
    ];
    $("#tiles").innerHTML = tiles.map((t) => `<div class="tile"><div class="label">${t.label}</div><div class="value">${t.value}</div><div class="sub">${esc(t.sub)}</div></div>`).join("");
    $("#totals-sum").textContent = `${total}개`;

    kv("#schedule-kv", [
      ["작동 시간대", esc(sch.active_hours || "항상")], ["현재", `${fmtTs(o.now)} (${esc(o.tz)})`],
      ["새 작업 허용", sch.can_start_work ? "예" : `아니오 (종료 ${sch.no_new_work_minutes_before_end}분 전부터 중단)`],
      ["자동 머지", sch.merge_at ? `${esc(sch.merge_at)}${sch.merge_window_open ? " · 오늘 머지 창 열림" : ""}` : "안 함"],
      ["폴링 주기", `${sch.poll_seconds}s`], ["다음 시작", fmtTs(sch.next_window_start)],
      ["로그 갱신", d.log_exists ? `${ago(d.log_mtime)} · ${(d.log_size / 1024).toFixed(0)} KB` : "daemon.log 없음 (아직 실행된 적 없음)"],
      ["마지막 실행", o.last_run ? `${esc(o.last_run.repo)} @ ${fmtTs(o.last_run.at)}` : "-"],
    ]);
    const a = d.activity;
    $("#activity").innerHTML = a
      ? `<div><strong>${esc(a.kind === "ticket" ? `${a.id} ${a.title}` : a.kind === "merge" ? "일일 자동 머지" : "Tech Lead 발굴")}</strong> ${a.priority ? badge(a.priority) : ""}</div>
         <dl class="kv" style="margin-top:8px"><dt>레포</dt><dd><a href="#" data-open-repo="${esc(a.repo || "")}">${esc(a.repo || "-")}</a></dd><dt>시작</dt><dd>${fmtTs(a.since)} (${ago(a.since)})</dd>
         ${a.stage ? `<dt>단계</dt><dd>${esc(a.stage.name)} · ${a.stage.iteration}회차 · ${ago(a.stage.since)} 시작 → <span class="mono">${esc(a.stage.output)}</span></dd>` : ""}
         ${a.note ? `<dt>주의</dt><dd class="c-warn">${esc(a.note)}</dd>` : ""}</dl>`
      : `<div class="empty">${d.status === "running" ? "데몬이 대기(폴링) 중입니다." : "진행 중인 작업이 없습니다."}</div>`;
    $$("#activity a[data-open-repo]").forEach((el) => el.addEventListener("click", (ev) => { ev.preventDefault(); openRepo(el.dataset.openRepo); }));
    kv("#llm-kv", [
      ["상태", llm.ok ? `<span class="c-good">정상</span>` : `<span class="c-crit">불가 — ${esc(llm.error || "")}</span>`], ["provider / model", esc(`${llm.provider} / ${llm.model}`)], ["URL", esc(llm.url || llm.base_url)],
      ["역할별 모델", esc(Object.entries(llm.role_models).map(([k, v]) => `${k}: ${v}`).join(", "))],
      ["누락 모델", llm.missing_models.length ? `<span class="c-crit">${esc(llm.missing_models.join(", "))}</span>` : "없음"],
      ["엔드포인트 모델", esc(llm.models.length ? llm.models.slice(0, 12).join(", ") + (llm.models.length > 12 ? ` 외 ${llm.models.length - 12}` : "") : "-")],
      ["확인 시각", fmtTs(llm.checked_at)],
    ]);
    kv("#cfg-kv", [
      ["root", esc(o.root)], ["fleet.yaml", `${esc(o.config)} ${o.config_writable ? badge("쓰기 가능", "good") : badge("읽기 전용", "warn")}`],
      ["include / exclude", `${o.include.length ? esc(o.include.join(", ")) : "(전체)"} / ${o.exclude.length ? esc(o.exclude.join(", ")) : "(없음)"}`],
      ["auto_merge", esc(`verify=${o.auto_merge.verify} require_verification=${o.auto_merge.require_verification} push=${o.auto_merge.push} timeout=${o.auto_merge.verify_timeout}s`)],
      ["discover", esc(`max_proposals=${o.discover.max_proposals} max_open_proposed=${o.discover.max_open_proposed}`)],
      ["FE 가이드", (o.frontend_guide || []).length ? o.frontend_guide.map((g) => `${esc(g.area)}: <strong>${esc(g.use)}</strong>${g.npm ? ` <span class="muted">(${esc(g.npm)})</span>` : ""}`).join("<br>") : "없음"],
      ["모니터", esc(`${o.monitor.in_docker ? "Docker" : "로컬"} · 가동 ${dur(o.monitor.uptime_seconds)}`)],
    ]);
    $("#repo-note").textContent = o.config_writable
      ? "체크 = fleet 작업 대상(fleet.yaml include/exclude 를 편집, 데몬은 다음 tick 에 반영). 행을 클릭하면 작업 내역을 봅니다."
      : "fleet.yaml 이 읽기 전용이라 선택을 바꿀 수 없습니다. 행을 클릭하면 작업 내역을 봅니다.";
    renderControl(o);
    renderRepoGrid(o.repos);
    renderOverviewCharts(o, state.stats);
  }

  function renderRepoGrid(repos) {
    const cols = [
      { headerName: "대상", field: "selected", width: 72, cellRenderer: SwitchRenderer, sortable: true, cellClass: "cell-center" },
      { headerName: "레포", field: "name", minWidth: 150, flex: 1.2, cellRenderer: htmlCell((d) => `<strong>${esc(d.name)}</strong>${d.has_remote ? "" : ` <span class="muted" title="원격 없음">⌂</span>`}${d.paused ? ` ${badge("일시정지", "serious")}` : ""}${(d.pending_requests || []).length ? ` <span class="muted" title="${esc(d.pending_requests.join(", "))}">⏳${d.pending_requests.length}</span>` : ""}`) },
      { headerName: "제어", field: "actions", width: 300, sortable: false, cellRenderer: ActionsRenderer, cellClass: "cell-center" },
      { headerName: "FE", field: "frontend", minWidth: 120, flex: 1, valueFormatter: (p) => p.value || "-" },
      { headerName: "BE", field: "backend", minWidth: 140, flex: 1, valueFormatter: (p) => p.value || "-" },
      { headerName: "브랜치", field: "current_branch", minWidth: 100, cellRenderer: htmlCell((d) => d.current_branch ? `${esc(d.current_branch)}${d.on_base ? "" : ` <span class="c-warn" title="기준 브랜치 아님">≠${esc(d.base_branch)}</span>`}` : `<span class="muted">${esc(d.base_branch || "-")}</span>`) },
      { headerName: "트리", field: "clean", width: 80, cellRenderer: htmlCell((d) => d.clean == null ? "-" : d.clean ? `<span class="c-good">clean</span>` : `<span class="c-warn">dirty</span>`) },
      { headerName: "검증", field: "has_verification", width: 90, cellRenderer: htmlCell((d) => d.has_verification == null ? "-" : d.has_verification ? `<span class="c-good">${d.verify_steps.length}단계</span>` : `<span class="c-serious" title="자동 머지 제외">없음</span>`) },
      { headerName: "티켓", field: "ticket_count", width: 130, cellRenderer: htmlCell((d) => d.counts ? `${miniSeg(d.counts)} <span class="muted">${d.ticket_count}</span>` : "-") },
      { headerName: "처리", field: "processed_count", width: 110, cellRenderer: htmlCell((d) => d.verdicts ? `<span class="c-good">${d.verdicts.PASS}</span> / <span class="c-crit">${d.verdicts.FAIL}</span> / <span class="c-serious">${d.verdicts.ERROR}</span>` : "-"), headerTooltip: "PASS / FAIL / ERROR" },
      { headerName: "마지막 머지", field: "last_merge", width: 150, cellRenderer: htmlCell((d) => d.last_merge ? `${fmtTs(d.last_merge.at).slice(5, 16)} · ${(d.last_merge.merged || []).length}건${d.last_merge.pushed ? " ↑" : ""}` : "-"), comparator: (a, b) => String(a?.at || "").localeCompare(String(b?.at || "")) },
      { headerName: "사유", field: "reasons", minWidth: 200, flex: 2, valueFormatter: (p) => (p.value || []).join("; "), tooltipValueGetter: (p) => (p.value || []).join("; ") },
    ];
    const grid = makeGrid("repos", $("#grid-repos"), cols, {
      empty: "root 아래에 git 저장소가 없습니다.",
      gridOptions: {
        domLayout: "autoHeight", getRowId: (p) => p.data.name, rowHeight: 38,
        rowClassRules: { "ag-row-dim": (p) => !p.data.eligible, "ag-row-pointer": () => true },
        onRowClicked: (e) => { if (e.event && e.event.target.closest && e.event.target.closest(".cell-switch, .cell-actions")) return; openRepo(e.data.name); },
        tooltipShowDelay: 300,
      },
    });
    if (grid) setRows("repos", repos);
    $("#repo-count").textContent = `(${repos.length})`;
  }

  async function toggleRepo(name, selected, inputEl) {
    try {
      const res = await api(`/api/repos/${encodeURIComponent(name)}/selection`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ selected }) });
      toast(`${name}: ${res.now_selected ? "fleet 대상에 포함" : "fleet 대상에서 제외"} (fleet.yaml 저장됨)`, true);
      await refresh();
      if (state.repo === name && state.tab === "repo") loadRepo(name);
    } catch (e) {
      if (inputEl) inputEl.checked = !selected;
      toast(`선택 변경 실패: ${e.message}`);
    }
  }

  // ---------- 제어 ----------
  const ACTION_KO = { run: "실행", discover: "발굴", merge: "머지", approve: "승인", retry: "재시도", stop: "중지", deploy: "배포" };
  async function sendRequest(repo, action, ticket) {
    try {
      const r = await api(`/api/repos/${encodeURIComponent(repo)}/requests`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, ticket: ticket || null }) });
      toast(`${repo}: ${ACTION_KO[action] || action}${ticket ? " " + ticket : ""} 요청을 큐에 넣었습니다 (${r.id.slice(0, 15)}…)`, true);
      await refresh();
      if (state.tab === "repo" && state.repo) loadRepo(state.repo);
    } catch (e) { toast(`요청 실패: ${e.message}`); }
  }
  async function setRepoPaused(repo, paused) {
    try {
      await api(`/api/repos/${encodeURIComponent(repo)}/pause`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paused }) });
      toast(`${repo}: ${paused ? "일시정지" : "재개"}`, true);
      await refresh();
      if (state.tab === "repo" && state.repo) loadRepo(state.repo);
    } catch (e) { toast(`변경 실패: ${e.message}`); }
  }
  async function cancelRequest(id) {
    try { await api(`/api/requests/${encodeURIComponent(id)}`, { method: "DELETE" }); toast("요청을 취소했습니다", true); refresh(); } catch (e) { toast(`취소 실패: ${e.message}`); }
  }
  function actionButtons(r, size = "sm") {
    const w = state.overview && state.overview.control && state.overview.control.writable;
    const busy = (a) => (r.pending_requests || []).some((x) => x === a || x.startsWith(a + " "));
    const dis = (a) => (!w || !r.eligible || r.paused || busy(a) ? "disabled" : "");
    const b = (a, label, cls = "", title = "") => `<button class="btn ${size} ${cls}" data-act="${a}" data-repo="${esc(r.name)}" ${dis(a)} title="${esc(title)}">${busy(a) ? "⏳ " : ""}${label}</button>`;
    return [
      b("run", "▶ 실행", "", "다음 todo 티켓을 지금 실행(시간대 무관, 호스트 러너)"),
      b("discover", "🔍 발굴", "", "Tech Lead 발굴 → proposed 티켓"),
      b("merge", "⇅ 머지", "", "review 브랜치를 지금 머지·검증"),
      r.paused
        ? `<button class="btn ${size}" data-act="resume" data-repo="${esc(r.name)}" ${w ? "" : "disabled"} title="일시정지 해제">▶ 재개</button>`
        : `<button class="btn ${size} warn" data-act="stop" data-repo="${esc(r.name)}" ${w && r.fe_be_ok ? "" : "disabled"} title="진행 중 작업을 단계 경계에서 멈추고 이 레포를 일시정지">⏹ 중지</button>`,
    ].join("");
  }
  function bindActions(root) {
    $$("button[data-act]", root).forEach((b) => b.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const { act, repo, ticket } = b.dataset;
      if (act === "resume") return setRepoPaused(repo, false);
      if (act === "pause") return setRepoPaused(repo, true);
      if (act === "stop" && !confirm(`${repo}: 진행 중인 작업을 단계 경계에서 중단하고 레포를 일시정지합니다. 계속할까요?`)) return;
      return sendRequest(repo, act, ticket);
    }));
  }
  class ActionsRenderer {
    init(p) { this.eGui = document.createElement("span"); this.eGui.className = "cell-actions"; this.eGui.innerHTML = actionButtons(p.data); this.eGui.addEventListener("click", (ev) => ev.stopPropagation()); bindActions(this.eGui); }
    getGui() { return this.eGui; }
    refresh(p) { this.eGui.innerHTML = actionButtons(p.data); bindActions(this.eGui); return true; }
  }
  function renderControl(o) {
    const c = o.control || {};
    const w = !!c.writable;
    $("#ctl-note").textContent = w ? "" : "hermes-fleet 디렉터리가 읽기 전용이라 제어할 수 없습니다.";
    const pa = $("#ctl-pause-all");
    pa.textContent = c.paused_all ? "▶ 전체 재개" : "⏸ 전체 일시정지";
    pa.className = `btn ${c.paused_all ? "primary" : "warn"}`;
    pa.disabled = !w;
    pa.onclick = async () => {
      try { await api("/api/pause", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ paused: !c.paused_all }) }); toast(c.paused_all ? "재개했습니다" : "전체 일시정지했습니다", true); refresh(); } catch (e) { toast(`실패: ${e.message}`); }
    };
    const f = $("#sched-form");
    if (document.activeElement && f.contains(document.activeElement)) { /* 편집 중이면 값 덮어쓰지 않음 */ } else {
      f.active_hours.value = o.schedule.active_hours || "";
      f.merge_at.value = o.schedule.merge_at || "";
      f.no_new_work_minutes_before_end.value = o.schedule.no_new_work_minutes_before_end;
      f.poll_seconds.value = o.schedule.poll_seconds;
    }
    $$("input,button", f).forEach((el) => (el.disabled = !c.config_writable));
    const reqRow = (r, cancellable) => `<div class="req ${esc(r.status)}"><span class="ts">${fmtTs(r.finished_at || r.started_at || r.requested_at).slice(5)}</span><span class="act">${esc(r.status)} · ${esc(ACTION_KO[r.action] || r.action)}${r.ticket ? " " + esc(r.ticket) : ""}</span><span class="detail" title="${esc(r.detail || "")}"><a href="#" data-open-repo="${esc(r.repo)}">${esc(r.repo)}</a>${r.detail ? " — " + esc(r.detail) : ""}</span><span>${cancellable && r.status === "pending" ? `<button class="btn sm" data-cancel="${esc(r.id)}">취소</button>` : ""}</span></div>`;
    $("#req-pending").innerHTML = (c.pending || []).length ? c.pending.map((r) => reqRow(r, true)).join("") : `<div class="empty">대기 중인 요청이 없습니다.</div>`;
    $("#req-recent").innerHTML = (c.recent || []).length ? c.recent.map((r) => reqRow(r, false)).join("") : `<div class="empty">아직 처리된 요청이 없습니다.</div>`;
    $$("#req-pending button[data-cancel]").forEach((b) => b.addEventListener("click", () => cancelRequest(b.dataset.cancel)));
    $$("#req-pending a[data-open-repo], #req-recent a[data-open-repo]").forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); openRepo(a.dataset.openRepo); }));
  }
  $("#sched-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const f = ev.target;
    const body = { active_hours: f.active_hours.value.trim(), merge_at: f.merge_at.value.trim(), no_new_work_minutes_before_end: Number(f.no_new_work_minutes_before_end.value), poll_seconds: Number(f.poll_seconds.value) };
    try {
      const r = await api("/api/schedule", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      $("#sched-msg").textContent = r.changed ? "저장됨 — 데몬은 다음 tick 에 반영" : "변경 없음";
      toast("스케줄을 저장했습니다", true);
      document.activeElement.blur();
      refresh();
    } catch (e) { toast(`저장 실패: ${e.message}`); }
  });

  function renderOverviewCharts(o, st) {
    const pal = palette();
    const totals = STATUSES.map((s) => o.totals[s] || 0);
    const sum = totals.reduce((a, b) => a + b, 0);
    renderChart("status", $("#ch-status"), deep(baseChart("donut", 260), {
      series: totals, labels: STATUSES.map((s) => STATUS_KO[s]), colors: pal.status, stroke: { width: 2, colors: [pal.surface] },
      plotOptions: { pie: { donut: { size: "68%", labels: { show: true, name: { color: pal.text }, value: { color: pal.text, fontSize: "20px" }, total: { show: true, label: "전체", color: pal.muted, formatter: () => String(sum) } } } } },
      legend: { show: true },
    }), sum ? null : "대상 레포에 티켓이 없습니다.\n데몬의 첫 발굴(discover) 후 채워집니다.");

    const targets = o.repos.filter((r) => r.fe_be_ok);
    const anyTickets = targets.some((r) => r.ticket_count);
    renderChart("repos", $("#ch-repos"), deep(baseChart("bar", 260), {
      chart: { stacked: true }, colors: pal.status,
      series: STATUSES.map((s, i) => ({ name: STATUS_KO[s], data: targets.map((r) => r.counts?.[s] || 0) })),
      xaxis: { categories: targets.map((r) => r.name + (r.eligible ? "" : " (제외)")), labels: { style: { colors: pal.muted } } },
      plotOptions: { bar: { horizontal: true, barHeight: "55%", borderRadius: 2 } }, stroke: { width: 1, colors: [pal.surface] },
    }), anyTickets ? null : "티켓이 있는 레포가 없습니다.");

    if (st) {
      const days = st.daily;
      const any = days.some((d) => d.PASS || d.FAIL || d.ERROR || d.merged);
      renderChart("daily", $("#ch-daily"), dailyChart(days, pal), any ? null : "최근 14일 처리 기록이 없습니다.");
    }
  }

  function dailyChart(days, pal) {
    return deep(baseChart("line", 260), {
      chart: { stacked: true }, colors: [...pal.verdict, pal.merged],
      series: [
        { name: "QA PASS", type: "column", data: days.map((d) => d.PASS) }, { name: "QA FAIL", type: "column", data: days.map((d) => d.FAIL) },
        { name: "오류", type: "column", data: days.map((d) => d.ERROR) }, { name: "머지", type: "line", data: days.map((d) => d.merged) },
      ],
      xaxis: { categories: days.map((d) => d.date.slice(5)), labels: { style: { colors: pal.muted }, rotate: -45, rotateAlways: false } },
      yaxis: { labels: { style: { colors: pal.muted }, formatter: (v) => (Number.isInteger(v) ? v : "") }, min: 0, forceNiceScale: true },
      stroke: { width: [0, 0, 0, 2], curve: "straight" }, markers: { size: 0, hover: { size: 5 } },
      plotOptions: { bar: { columnWidth: "55%", borderRadius: 2 } }, tooltip: { shared: true, intersect: false },
    });
  }

  // ---------- 레포 상세 ----------
  function fillRepoSelect() {
    const sel = $("#r-select");
    const names = state.repos.map((r) => r.name);
    sel.innerHTML = `<option value="">레포 선택…</option>` + names.map((n) => `<option value="${esc(n)}"${n === state.repo ? " selected" : ""}>${esc(n)}</option>`).join("");
  }
  $("#r-select").addEventListener("change", (e) => { if (e.target.value) openRepo(e.target.value); });
  $("#r-switch").addEventListener("change", (e) => { if (state.repo) toggleRepo(state.repo, e.target.checked, e.target); });

  function openRepo(name) { state.repo = name; showTab("repo"); }

  async function loadRepo(name) {
    fillRepoSelect();
    $("#r-empty").hidden = true;
    try {
      const r = await api(`/api/repos/${encodeURIComponent(name)}`);
      state.repoDetail = r;
      renderRepo(r);
    } catch (e) {
      $("#r-body").hidden = false; $("#r-content").hidden = true;
      $("#r-kv").innerHTML = `<dt>오류</dt><dd class="c-crit">${esc(e.message)}</dd>`;
    }
  }

  function renderRepo(r) {
    const sw = $("#r-switch");
    sw.checked = !!r.selected;
    sw.disabled = !r.fe_be_ok || !(state.overview && state.overview.config_writable);
    $("#r-switch-label").textContent = r.eligible ? "fleet 대상" : r.selected ? "선택됨 (FE/BE 미확인)" : "제외됨";
    $("#r-actions").innerHTML = actionButtons(r, "");
    bindActions($("#r-actions"));
    $("#r-badges").innerHTML = [
      r.paused ? badge("일시정지", "serious") : "",
      (r.pending_requests || []).length ? badge(`요청 대기: ${r.pending_requests.join(", ")}`) : "",
      r.fe_be_ok ? badge("FE·BE 확인", "good") : badge("FE/BE 미확인", "serious"),
      r.current_branch ? (r.on_base ? badge(`on ${r.base_branch}`, "good") : badge(`on ${r.current_branch}`, "warn")) : "",
      r.clean == null ? "" : r.clean ? badge("clean", "good") : badge("dirty · 건너뜀", "warn"),
      r.has_remote ? badge("origin") : badge("원격 없음", "serious"),
      r.has_verification === false ? badge("검증 없음 · 자동 머지 제외", "serious") : "",
    ].join(" ");
    $("#r-body").hidden = false;
    kv("#r-kv", [
      ["경로", `<span class="mono">${esc(r.root)}</span>`], ["사유", esc(r.reasons.join("; "))],
      ["FE / BE", esc(`${r.frontend || "-"} / ${r.backend || "-"}`)],
      ...(r.fe_be_ok ? [
        ["org", `<span class="mono">${esc(r.org_source || "-")}</span>`],
        ["마지막 커밋", r.last_commit ? `<span class="mono">${esc(r.last_commit.hash)}</span> ${esc(r.last_commit.subject)} <span class="muted">(${esc(r.last_commit.date)})</span>` : "-"],
        ["검증 단계", r.verify_steps.length ? r.verify_steps.map((s) => `<span class="mono">(${esc(s.cwd)}) ${esc(s.cmd)}</span>`).join("<br>") : `<span class="c-serious">없음 → 자동 머지 제외</span>`],
        ["hermes/* 브랜치", r.hermes_branches.length ? r.hermes_branches.map((b) => `<a href="#" data-ticket="${esc(b.ticket_id)}" data-repo="${esc(r.name)}">${esc(b.branch)}</a>`).join(", ") : "-"],
        ["처리 누적", `${r.processed_count}건 · PASS ${r.verdicts.PASS} / FAIL ${r.verdicts.FAIL} / ERROR ${r.verdicts.ERROR} · 총 ${dur(r.total_seconds)}`],
        ["마지막 자동 머지", r.last_merge ? `${fmtTs(r.last_merge.at)} · merged=${esc(JSON.stringify(r.last_merge.merged))} pushed=${r.last_merge.pushed}${r.last_merge.issues?.length ? ` · <span class="c-crit">issues=${esc(r.last_merge.issues.join(", "))}</span>` : ""}${r.last_merge.note ? ` · ${esc(r.last_merge.note)}` : ""}` : "-"],
        ["백로그", `<span class="mono">${esc(r.backlog_dir)}</span> ${r.scaffolded ? "" : `<span class="muted">(아직 없음 — 데몬 첫 발굴 때 생성)</span>`}${r.backlog_error ? `<div class="c-crit">${esc(r.backlog_error)}</div>` : ""}`],
      ] : []),
    ]);
    $$("#r-kv a[data-ticket]").forEach((el) => el.addEventListener("click", (ev) => { ev.preventDefault(); openTicket(el.dataset.repo, el.dataset.ticket); }));
    $("#r-content").hidden = !r.fe_be_ok;
    if (!r.fe_be_ok) return;

    const pal = palette();
    const hist = r.history || [];
    renderChart("r-stages", $("#ch-r-stages"), deep(baseChart("bar", 260), {
      chart: { stacked: true }, colors: pal.stages,
      series: STAGES.map((s) => ({ name: STAGE_KO[s], data: [...hist].reverse().map((h) => mins(h.stage_seconds?.[s])) })),
      xaxis: { categories: [...hist].reverse().map((h) => h.id), labels: { style: { colors: pal.muted } } },
      yaxis: { labels: { style: { colors: pal.muted }, formatter: (v) => `${v}분` } }, plotOptions: { bar: { columnWidth: "50%", borderRadius: 2 } },
      tooltip: { y: { formatter: (v) => `${v}분` }, shared: true, intersect: false }, stroke: { width: 1, colors: [pal.surface] },
    }), hist.some((h) => h.stage_events) ? null : "단계 실행 기록(run.jsonl)이 아직 없습니다.");
    const any = (r.daily || []).some((d) => d.PASS || d.FAIL || d.ERROR || d.merged);
    renderChart("r-daily", $("#ch-r-daily"), dailyChart(r.daily || [], pal), any ? null : "최근 14일 처리 기록이 없습니다.");

    makeGrid("history", $("#grid-history"), [
      { headerName: "ID", field: "id", width: 100, cellClass: "mono" },
      { headerName: "결과", field: "verdict", width: 90, cellRenderer: htmlCell((d) => verdictBadge(d.verdict)) },
      { headerName: "상태", field: "status", width: 110, cellRenderer: htmlCell((d) => stBadge(d.status)) },
      { headerName: "반복", field: "iterations", width: 70 },
      { headerName: "소요", field: "seconds", width: 100, valueFormatter: (p) => dur(p.value) },
      { headerName: "PM/디자인/개발/QA(분)", field: "stage_seconds", width: 170, valueFormatter: (p) => (p.value ? STAGES.map((s) => mins(p.value[s])).join(" / ") : "-"), sortable: false },
      { headerName: "브랜치", field: "branch", width: 150, cellClass: "mono" },
      { headerName: "시작", field: "started_at", width: 150, valueFormatter: (p) => fmtTs(p.value) },
      { headerName: "끝", field: "finished_at", width: 150, valueFormatter: (p) => fmtTs(p.value), sort: "desc" },
      { headerName: "커밋 / 오류", field: "commit", flex: 1, minWidth: 220, cellRenderer: htmlCell((d) => `<span class="mono">${esc((d.commit || "").split("\n")[0].slice(0, 90))}</span>${d.error ? `<div class="c-crit">${esc(d.error.slice(0, 160))}</div>` : ""}`) },
    ], { empty: "아직 처리한 티켓이 없습니다.", gridOptions: { domLayout: "autoHeight", getRowId: (p) => p.data.id, rowClassRules: { "ag-row-pointer": () => true }, onRowClicked: (e) => openTicket(r.name, e.data.id) } });
    setRows("history", hist);
    $("#r-hist-count").textContent = `(${hist.length})`;

    makeGrid("r-tickets", $("#grid-r-tickets"), ticketCols(false), { empty: "티켓이 없습니다.", gridOptions: { domLayout: "autoHeight", getRowId: (p) => p.data.id, rowClassRules: { "ag-row-pointer": () => true }, onRowClicked: (e) => openTicket(r.name, e.data.id) } });
    setRows("r-tickets", r.tickets || []);
    $("#r-ticket-count").textContent = `(${(r.tickets || []).length})`;
    $("#r-ticket-note").textContent = r.scaffolded ? "* = 브랜치에서 처리된 상태(기준 브랜치 파일은 todo)" : "hermes/backlog 가 아직 없습니다.";

    makeGrid("merges", $("#grid-merges"), [
      { headerName: "시각", field: "at", width: 160, sort: "desc" },
      { headerName: "머지", field: "merged", width: 180, valueFormatter: (p) => (p.value?.length ? p.value.join(", ") : "-") },
      { headerName: "push", field: "pushed", width: 80, cellRenderer: htmlCell((d) => d.pushed ? `<span class="c-good">예</span>` : `<span class="muted">아니오</span>`) },
      { headerName: "문제", field: "issues", width: 200, cellRenderer: htmlCell((d) => d.issues?.length ? `<span class="c-crit">${esc(d.issues.join(", "))}</span>` : "-") },
      { headerName: "결과 상세", field: "outcomes", flex: 1, minWidth: 260, valueFormatter: (p) => (p.value || []).map((o) => `${o.result} ${o.branch}${o.detail ? " · " + o.detail : ""}`).join(" | "), tooltipValueGetter: (p) => (p.value || []).map((o) => `${o.result} ${o.branch} ${o.detail || ""}`).join("\n"), sortable: false },
      { headerName: "비고", field: "note", flex: 1, minWidth: 160 },
    ], { empty: "자동 머지 기록이 없습니다.", gridOptions: { domLayout: "autoHeight", getRowId: (p) => p.data.at, tooltipShowDelay: 300 } });
    setRows("merges", r.merge_history || []);
    $("#r-merge-count").textContent = `(${(r.merge_history || []).length})`;

    renderDelivery(r);
    $("#r-log").innerHTML = (r.log_lines || []).length ? r.log_lines.map(logLineHtml).join("") : `<span class="ln plain">이 레포에 대한 로그가 아직 없습니다.</span>`;
    $("#r-log").scrollTop = $("#r-log").scrollHeight;
  }

  // ---------- 티켓 ----------
  function ticketCols(withRepo) {
    return [
      ...(withRepo ? [{ headerName: "레포", field: "repo", width: 150 }] : []),
      { headerName: "ID", field: "id", width: 100, cellClass: "mono" },
      { headerName: "우선순위", field: "priority", width: 95 },
      { headerName: "상태", field: "status", width: 120, cellRenderer: htmlCell((d) => `${stBadge(d.status)}${d.status_source === "state" ? ` <span class="muted" title="브랜치에서 처리됨 (기준 브랜치 파일: ${esc(d.file_status)})">*</span>` : ""}`), comparator: (a, b) => STATUSES.indexOf(a) - STATUSES.indexOf(b) },
      { headerName: "영역", field: "area", width: 100 },
      { headerName: "제목", field: "title", flex: 2, minWidth: 240, tooltipField: "title" },
      { headerName: "단계", field: "stages", width: 180, valueFormatter: (p) => (p.value || []).join(" → ") },
      { headerName: "수용 기준", field: "acceptance", width: 90, valueFormatter: (p) => `${(p.value || []).length}개` },
    ];
  }

  async function loadTickets() {
    const ok = state.repos.filter((r) => r.fe_be_ok);
    const details = await Promise.all(ok.map((r) => api(`/api/repos/${encodeURIComponent(r.name)}`).catch(() => null)));
    state.tickets = details.flatMap((d) => (d ? d.tickets.map((t) => ({ ...t, repo: d.name })) : []));
    const sel = $("#t-repo"), cur = sel.value;
    sel.innerHTML = `<option value="">모든 레포</option>` + ok.map((r) => `<option${r.name === cur ? " selected" : ""}>${esc(r.name)}</option>`).join("");
    makeGrid("tickets", $("#grid-tickets"), ticketCols(true), {
      empty: "티켓이 없습니다. 데몬의 첫 발굴(discover) 후 채워집니다.",
      gridOptions: { getRowId: (p) => `${p.data.repo}/${p.data.id}`, rowClassRules: { "ag-row-pointer": () => true }, onRowClicked: (e) => openTicket(e.data.repo, e.data.id), tooltipShowDelay: 300,
        isExternalFilterPresent: () => !!($("#t-repo").value || $("#t-status").value), doesExternalFilterPass: (n) => (!$("#t-repo").value || n.data.repo === $("#t-repo").value) && (!$("#t-status").value || n.data.status === $("#t-status").value),
        onModelUpdated: (e) => { $("#t-count").textContent = `${e.api.getDisplayedRowCount()} / ${state.tickets.length}`; } },
    });
    setRows("tickets", state.tickets);
  }
  ["#t-repo", "#t-status"].forEach((s) => $(s).addEventListener("change", () => state.grids.tickets && state.grids.tickets.onFilterChanged()));
  $("#t-q").addEventListener("input", (e) => state.grids.tickets && state.grids.tickets.setGridOption("quickFilterText", e.target.value));

  async function openTicket(repo, id) {
    showTab("tickets");
    const box = $("#ticket-detail");
    box.hidden = false;
    box.innerHTML = `<div class="empty">불러오는 중…</div>`;
    try {
      const t = await api(`/api/repos/${encodeURIComponent(repo)}/tickets/${encodeURIComponent(id)}`);
      const e = t.state;
      box.innerHTML = `
        <div class="detail-head"><h2><span class="mono">${esc(t.id)}</span> ${esc(t.title)}</h2>${badge(t.priority)} ${stBadge(t.status)} ${badge(t.area)}
          <span class="actions" id="ticket-actions">${ticketButtons(t)}</span><button class="ghost close" id="ticket-close">닫기</button></div>
        <dl class="kv" style="margin-top:10px">
          <dt>레포</dt><dd><a href="#" id="ticket-repo-link">${esc(t.repo)}</a></dd>
          <dt>파일</dt><dd class="mono">${esc(t.path || "(기준 브랜치에 없음 — 브랜치에서 읽음)")}</dd>
          <dt>단계</dt><dd>${esc(t.stages.join(" → "))}</dd>
          <dt>브랜치</dt><dd>${t.branch ? `<span class="mono">${esc(t.branch)}</span> (브랜치의 티켓 상태: ${esc(t.branch_status || "-")})` : "없음"}</dd>
          ${e ? `<dt>처리 결과</dt><dd>${verdictBadge(e.verdict)} → ${stBadge(e.status)} · ${e.iterations ?? "-"}회 · ${dur(e.seconds)} · ${fmtTs(e.started_at)} ~ ${fmtTs(e.finished_at)}${e.error ? `<div class="c-crit">${esc(e.error)}</div>` : ""}</dd>` : ""}
          ${e?.commit ? `<dt>커밋</dt><dd class="mono">${esc(e.commit.split("\n")[0])}</dd>` : ""}
        </dl>
        <div class="grid two" style="margin-top:12px">
          <div class="card chart-card" style="margin-bottom:0"><div class="card-head"><h2>단계 타임라인</h2><span class="muted">run.jsonl · 종료 시각 − 소요</span></div><div class="chart" id="ch-t-timeline"></div></div>
          <div class="card" style="margin-bottom:0"><h2>수용 기준</h2><ul class="plain">${t.acceptance.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>
            ${t.events.length ? `<h3>단계 실행</h3><table class="plain-table"><thead><tr><th>시각</th><th>단계</th><th>반복</th><th>소요</th><th>크기</th></tr></thead><tbody>${t.events.map((ev) => `<tr><td class="nowrap mono">${fmtTs(ev.ts)}</td><td>${esc(ev.stage)}</td><td>${ev.iteration}</td><td>${dur(Math.round(ev.seconds))}</td><td>${ev.chars ?? "-"}</td></tr>`).join("")}</tbody></table>` : ""}
          </div>
        </div>
        <h3>본문</h3><pre class="doc">${esc(t.body || "(없음)")}</pre>
        ${t.diff_stat ? `<h3>변경 파일 (git diff --stat ${esc(t.branch)})</h3><pre class="doc">${esc(t.diff_stat)}</pre>` : ""}
        <h3>산출물</h3>
        ${t.artifacts.length ? `<div class="artifact-tabs" id="art-tabs">${t.artifacts.map((a) => `<button data-name="${esc(a.name)}" title="${a.source === "branch" ? "브랜치에서 읽음" : esc(a.mtime || "")}">${esc(a.name)}${a.source === "branch" ? " ⎇" : ""}</button>`).join("")}</div><pre class="doc" id="art-body">파일을 선택하세요.</pre>` : `<div class="empty">산출물이 아직 없습니다.</div>`}`;
      $("#ticket-close").addEventListener("click", () => (box.hidden = true));
      bindActions($("#ticket-actions"));
      $("#ticket-repo-link").addEventListener("click", (ev) => { ev.preventDefault(); openRepo(t.repo); });
      $$("#art-tabs button").forEach((b) => b.addEventListener("click", async () => {
        $$("#art-tabs button").forEach((x) => x.classList.toggle("active", x === b));
        $("#art-body").textContent = "불러오는 중…";
        try {
          const a = await api(`/api/repos/${encodeURIComponent(repo)}/tickets/${encodeURIComponent(t.id)}/artifacts/${encodeURIComponent(b.dataset.name)}`);
          $("#art-body").textContent = a.content || "(빈 파일)";
        } catch (err) { $("#art-body").textContent = `불러오기 실패: ${err.message}`; }
      }));
      const first = $("#art-tabs button[data-name='summary.md'], #art-tabs button[data-name='qa-report.md'], #art-tabs button");
      if (first) first.click();
      renderTicketTimeline(t);
      box.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      box.innerHTML = `<div class="c-crit">불러오기 실패: ${esc(err.message)}</div>`;
    }
  }

  function ticketButtons(t) {
    const repo = state.repos.find((r) => r.name === t.repo) || {};
    const w = state.overview && state.overview.control && state.overview.control.writable && repo.eligible && !repo.paused;
    const b = (a, label, title) => `<button class="btn sm" data-act="${a}" data-repo="${esc(t.repo)}" data-ticket="${esc(t.id)}" ${w ? "" : "disabled"} title="${esc(title)}">${label}</button>`;
    if (t.status === "proposed") return b("approve", "✓ 승인", "proposed → todo (호스트 러너가 커밋)");
    if (t.status === "blocked") return b("retry", "↺ 재시도 준비", "blocked → todo, 이전 처리 기록 삭제") ;
    if (t.status === "todo") return b("run", "▶ 이 티켓 실행", "지금 이 티켓만 실행(시간대 무관)");
    return "";
  }

  function renderTicketTimeline(t) {
    if (state.charts["t-timeline"]) { state.charts["t-timeline"].destroy(); delete state.charts["t-timeline"]; }
    const pal = palette();
    const el = $("#ch-t-timeline");
    if (!t.events.length) { renderChart("t-timeline", el, {}, "단계 실행 기록이 없습니다."); return; }
    const series = STAGES.map((s, i) => ({
      name: STAGE_KO[s],
      data: t.events.filter((ev) => ev.stage === s).map((ev) => { const end = new Date(String(ev.ts)).getTime(); return { x: `${STAGE_KO[s]} #${ev.iteration}`, y: [end - (ev.seconds || 0) * 1000, end] }; }),
    })).filter((s) => s.data.length);
    renderChart("t-timeline", el, deep(baseChart("rangeBar", 240), {
      series, colors: STAGES.filter((s) => t.events.some((ev) => ev.stage === s)).map((s) => pal.stages[STAGES.indexOf(s)]),
      plotOptions: { bar: { horizontal: true, barHeight: "60%", borderRadius: 2, rangeBarGroupRows: true } },
      xaxis: { type: "datetime", labels: { datetimeUTC: false, style: { colors: pal.muted }, format: "HH:mm" } },
      tooltip: { x: { format: "HH:mm:ss" }, custom: ({ seriesIndex, dataPointIndex, w }) => { const d = w.config.series[seriesIndex].data[dataPointIndex]; return `<div style="padding:6px 10px">${esc(d.x)} · ${dur(Math.round((d.y[1] - d.y[0]) / 1000))}</div>`; } },
      legend: { show: false },
    }));
  }

  // ---------- 워크플로 보드 ----------
  const MODE_KO = { auto: "자동", manual: "사람", none: "—", actions: "Actions 자동", fleet: "fleet 자동" };
  function stageCell(s, repo, single) {
    const count = s.count != null && s.mode !== "none" ? `<span class="big">${s.count}${s.fail ? ` <span class="c-crit" style="font-size:12px">/ ${s.fail}</span>` : ""}</span>` : (s.target ? `<span class="big" style="font-size:12px">${esc(s.target)}</span>` : "");
    const link = s.link ? `<a href="${esc(s.link)}" target="_blank" rel="noopener" title="열기">↗</a>` : "";
    const btn = single && s.key === "deploy" && s.can_request ? `<button class="btn sm" data-act="deploy" data-repo="${esc(repo)}" title="호스트가 fleet.yaml deploy.command 실행">🚀 배포 요청</button>` : "";
    return `<div class="wf-stage mode-${esc(s.mode)} status-${esc(s.status || "idle")}" title="${esc(s.detail || "")}">
      ${s.aws ? `<span class="aws">AWS</span>` : ""}
      <span class="lb"><span class="dot"></span>${esc(s.label)} <span class="muted" style="font-weight:400">${esc(MODE_KO[s.mode] || s.mode)}</span> ${link}</span>
      ${count}<span class="dt">${esc(s.detail || "")}</span>${s.at ? `<span class="ts">${fmtTs(s.at).slice(5, 16)}</span>` : ""}${btn}</div>`;
  }
  function renderBoard(el, rows, single) {
    el.innerHTML = rows.map((r) => `<div class="wf-row ${r.eligible ? "" : "dim"}" data-repo="${esc(r.name)}">
      ${single ? "" : `<div class="wf-repo"><span class="name">${esc(r.name)} ${r.paused ? badge("일시정지", "serious") : ""}${r.eligible ? "" : r.fe_be_ok ? badge("제외", "warn") : badge("FE/BE 없음")}</span><span class="sum">${esc(r.summary || "")}</span></div>`}
      <div class="wf-chain">${r.stages.map((s) => stageCell(s, r.name, single)).join("")}</div></div>`).join("");
    $$(".wf-repo", el).forEach((x) => x.addEventListener("click", () => openRepo(x.parentElement.dataset.repo)));
    bindActions(el);
  }
  async function loadWorkflow() {
    const ci = $("#wf-ci").checked;
    $("#wf-meta").textContent = "불러오는 중…";
    try {
      const b = await api(`/api/workflow?ci=${ci}`);
      state.workflowTotals = b.totals;
      $("#wf-meta").textContent = `${fmtTs(b.generated_at)} 기준${ci ? (b.github_token ? " · Actions 조회(토큰)" : " · Actions 조회(공개 레포만, 토큰 없음)") : ""}`;
      const t = b.totals;
      $("#wf-summary").innerHTML = `대상 레포 <strong>${t.targets}</strong>개 중 GitHub push 까지 <strong>${t.push}</strong>, CI/CD 까지 <strong>${t.cicd}</strong>, 배포 자동 <strong>${t.deploy_auto}</strong>, AWS 배포 <strong>${t.aws}</strong>. 각 셀은 그 단계가 이 레포에서 자동인지(파랑) · 사람이 하는지(빗금) · 없는지(점선)를 뜻하고, 점은 최근 상태입니다.`;
      renderBoard($("#wf-board"), b.rows, false);
    } catch (e) { $("#wf-meta").textContent = `실패: ${e.message}`; }
  }
  $("#wf-ci").addEventListener("change", loadWorkflow);

  function renderDelivery(r) {
    const d = r.delivery || {};
    $("#r-wf-summary").textContent = d.summary || "";
    renderBoard($("#r-wf"), [{ name: r.name, eligible: r.eligible, fe_be_ok: r.fe_be_ok, stages: r.workflow || [] }], true);
    kv("#r-delivery-kv", [
      ["원격", d.remote_url ? `<a href="${esc(d.remote_web || "#")}" target="_blank" rel="noopener">${esc(d.remote_url)}</a> (${esc(d.remote_host)})` : "없음 → push 단계 없음"],
      ["origin 동기화", d.sync && d.sync.tracked ? `ahead ${d.sync.ahead} · behind ${d.sync.behind} · origin HEAD ${esc(d.sync.origin_head_date || "")}` : "추적 브랜치 없음(또는 fetch 전)"],
      ["워크플로", (d.workflows || []).length ? d.workflows.map((w) => `<span class="mono">${esc(w.file)}</span> ${esc(w.name)} <span class="muted">[${esc(w.triggers.join(", "))}]</span> ${w.on_base_push ? badge("기준 브랜치 push 반응", "good") : ""} ${w.deployish ? badge("배포성", "serious") : ""} ${w.targets.length ? `<span class="muted">${esc(w.targets.join(", "))}</span>` : ""}`).join("<br>") : "없음"],
      ["push 모드", esc(MODE_KO[d.push_mode] || d.push_mode || "-")], ["CI/CD 모드", esc(MODE_KO[d.cicd_mode] || d.cicd_mode || "-")],
    ]);
    const ov = d.override || {};
    kv("#r-deploy-kv", [
      ["모드", `${esc(MODE_KO[d.deploy_mode] || d.deploy_mode || "-")}${d.aws ? ` ${badge("AWS", "serious")}` : ""}`],
      ["대상", esc(d.deploy_target || "-")],
      ["탐지된 흔적", (d.markers || []).length ? d.markers.slice(0, 10).map((m) => `<span class="mono">${esc(m.path)}</span> <span class="muted">(${esc(m.kind)}${m.note ? " · " + esc(m.note) : ""})</span>`).join("<br>") + (d.markers.length > 10 ? `<br><span class="muted">외 ${d.markers.length - 10}</span>` : "") : "없음"],
      ["fleet.yaml deploy", Object.keys(ov).length ? Object.entries(ov).map(([k, v]) => `${esc(k)}: <span class="mono">${esc(String(v))}</span>`).join("<br>") : `<span class="muted">선언 없음 — repos.${esc(r.name)}.deploy 에 target/trigger/command 를 적으면 fleet 이 push 뒤 배포하거나 웹에서 배포 요청을 보낼 수 있습니다</span>`],
      ["마지막 fleet 배포", r.last_deploy ? `${r.last_deploy.ok ? badge("성공", "good") : badge("실패", "crit")} ${fmtTs(r.last_deploy.at)} (${esc(r.last_deploy.source || "")})<pre class="doc" style="max-height:160px">${esc(r.last_deploy.detail || "")}</pre>` : "-"],
    ]);
    const ci = r.ci;
    $("#r-ci-runs").innerHTML = ci ? (ci.ok ? `<h3>GitHub Actions 최근 실행 <a href="${esc(ci.web)}" target="_blank" rel="noopener">↗</a></h3>` + (ci.runs.length ? ci.runs.map((x) => `<div class="ci-run">${x.conclusion === "success" ? badge("success", "good") : x.status !== "completed" ? badge(x.status, "") : badge(x.conclusion || "?", "crit")} <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.name)}</a> <span class="muted">${esc(x.event)} · ${esc(x.sha)} · ${fmtTs((x.updated_at || "").replace("Z", "")).slice(0, 16)}</span></div>`).join("") : `<div class="muted">실행 기록 없음</div>`) : `<h3>GitHub Actions</h3><div class="muted">조회 실패: ${esc(ci.error || "")}</div>`) : "";
  }

  // ---------- 타임라인 ----------
  async function loadTimeline() {
    try {
      const ev = await api("/api/timeline?limit=500");
      const KIND = { ticket: "티켓", merge: "머지", log: "로그", cursor: "커서" };
      makeGrid("timeline", $("#grid-timeline"), [
        { headerName: "시각", field: "ts", width: 160, valueFormatter: (p) => fmtTs(p.value), sort: "desc" },
        { headerName: "종류", field: "kind", width: 90, valueFormatter: (p) => KIND[p.value] || p.value, filter: true },
        { headerName: "레포", field: "repo", width: 150, valueFormatter: (p) => p.value || "fleet", filter: true },
        { headerName: "결과", field: "verdict", width: 90, cellRenderer: htmlCell((d) => d.kind === "ticket" ? verdictBadge(d.verdict) : d.kind === "merge" ? (d.issues?.length ? badge("문제", "crit") : badge(`${(d.merged || []).length}건`, "good")) : d.level === "error" ? badge("오류", "crit") : "") },
        { headerName: "요약", field: "summary", flex: 1, minWidth: 320, tooltipField: "summary", cellRenderer: htmlCell((d) => d.kind === "ticket" ? `<a href="#">${esc(d.summary)}</a>` : esc(d.summary)) },
      ], { empty: "아직 기록이 없습니다. 데몬이 01:00 이후 첫 작업을 하면 여기에 쌓입니다.", gridOptions: { getRowId: (p) => `${p.data.ts}|${p.data.kind}|${p.data.repo}|${p.data.id || ""}|${p.data.summary.slice(0, 40)}`, onRowClicked: (e) => { if (e.data.kind === "ticket") openTicket(e.data.repo, e.data.id); else if (e.data.repo) openRepo(e.data.repo); }, rowClassRules: { "ag-row-pointer": (p) => p.data.kind === "ticket" || !!p.data.repo }, tooltipShowDelay: 300 } });
      setRows("timeline", ev);
    } catch (e) { toast(`타임라인 실패: ${e.message}`); }
  }

  // ---------- 로그 ----------
  const logLineHtml = (l) => `<span class="ln ${l.ts ? l.level : "plain"}">${l.ts ? `<span class="ts">${esc(l.ts)}</span> ` : ""}${esc(l.msg)}</span>`;
  function startLogStream() {
    if (state.es) return;
    const es = new EventSource("/api/logs/stream?tail=300");
    state.es = es;
    es.addEventListener("init", (ev) => { const d = JSON.parse(ev.data); state.logLines = d.lines; renderLog(true); });
    es.addEventListener("lines", (ev) => { state.logLines.push(...JSON.parse(ev.data)); if (state.logLines.length > 4000) state.logLines.splice(0, state.logLines.length - 4000); renderLog(false); });
    es.onerror = () => { $("#l-meta").textContent = "스트림 끊김 — 재연결 중…"; };
    es.onopen = () => { $("#l-meta").textContent = ""; };
  }
  function renderLog(reset) {
    const repo = $("#l-repo").value, q = $("#l-q").value.trim().toLowerCase(), fleetOnly = $("#l-fleet-only").checked;
    const pre = $("#log");
    const rows = state.logLines.filter((l) => (!repo || l.repo === repo) && (!q || l.raw.toLowerCase().includes(q)) && (!fleetOnly || /\[(fleet|merge|discover|qa|git)\]/.test(l.msg)));
    pre.innerHTML = rows.map(logLineHtml).join("") || `<span class="ln plain">${state.logLines.length ? "필터에 맞는 줄이 없습니다." : "daemon.log 가 비어 있거나 아직 없습니다."}</span>`;
    $("#l-meta").textContent = `${rows.length} / ${state.logLines.length} 줄`;
    if ($("#l-follow").checked || reset) pre.scrollTop = pre.scrollHeight;
  }
  ["#l-repo", "#l-fleet-only"].forEach((s) => $(s).addEventListener("change", () => renderLog(false)));
  $("#l-q").addEventListener("input", () => renderLog(false));
  $("#log").addEventListener("scroll", () => { const p = $("#log"); if (p.scrollHeight - p.scrollTop - p.clientHeight > 40) $("#l-follow").checked = false; });

  // ---------- 메인 루프 ----------
  async function refresh() {
    try {
      const [o, st, wf] = await Promise.all([api("/api/overview"), api("/api/stats").catch(() => null), state.workflowTotals ? Promise.resolve(null) : api("/api/workflow").catch(() => null)]);
      if (wf) state.workflowTotals = wf.totals;
      state.overview = o; state.stats = st; state.repos = o.repos;
      const serverNow = new Date(String(o.now)).getTime();
      if (isFinite(serverNow)) state.skew = serverNow - Date.now();
      renderOverview(o);
      const lsel = $("#l-repo"), cur = lsel.value;
      lsel.innerHTML = `<option value="">모든 레포</option>` + o.repos.filter((r) => r.fe_be_ok).map((r) => `<option${r.name === cur ? " selected" : ""}>${esc(r.name)}</option>`).join("");
      $("#conn").textContent = `갱신 ${new Date().toLocaleTimeString("ko-KR", { hour12: false })}`;
      $("#conn").className = "pill ok";
      if (state.tab === "timeline") loadTimeline();
      if (state.tab === "repo" && state.repo) loadRepo(state.repo);
    } catch (e) {
      $("#conn").textContent = "연결 실패";
      $("#conn").className = "pill bad";
      toast(`갱신 실패: ${e.message}`);
    }
  }
  $("#refresh").addEventListener("click", refresh);
  setInterval(() => { $("#clock").textContent = new Date().toLocaleString("ko-KR", { hour12: false }); }, 1000);
  setInterval(refresh, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    destroyCharts();
    Object.values(state.grids).forEach((g) => g.setGridOption("theme", gridTheme()));
    if (state.overview) renderOverview(state.overview);
    if (state.tab === "repo" && state.repoDetail) renderRepo(state.repoDetail);
  });

  const hash = location.hash.replace("#", "");
  if (hash.startsWith("repo=")) state.repo = decodeURIComponent(hash.slice(5));
  const initial = hash.startsWith("repo=") ? "repo" : hash;
  refresh().then(() => showTab(["overview", "workflow", "repo", "tickets", "timeline", "logs"].includes(initial) ? initial : "overview"));
})();
