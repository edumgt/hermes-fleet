/* 크루 룸 3D — Three.js(ES module, import map 으로 고정 버전 로드). 역할별 AI 를 사람 아바타로 그리고 상태(작업 중·발언·완료·실패)를 몸짓과 말풍선으로 보인다.
   app.js(클래식 스크립트)는 window.CrewRoom 으로 쓴다. 이 모듈이 로드되지 않으면(CDN 실패) app.js 가 안내 문구를 보인다. */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const SKIN = [0xf2c9a6, 0xe8b794, 0xd9a07e, 0xf5d3b3, 0xc98a63];
const HAIR = [0x2b2118, 0x4a2c17, 0x111111, 0x6b4a2a, 0x3a3a3a, 0x8a5a2b];
const DEG = Math.PI / 180;

function makeTextTexture(draw, w = 512, h = 160) {
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  const ctx = c.getContext("2d");
  draw(ctx, w, h);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return tex;
}
function roundRect(ctx, x, y, w, h, r) { ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r); ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath(); }
function wrapLines(ctx, text, maxWidth, maxLines) {
  const out = [];
  for (const para of String(text).replace(/\r/g, "").split("\n")) {
    let line = "";
    for (const ch of para) {
      const t = line + ch;
      if (ctx.measureText(t).width > maxWidth && line) { out.push(line); line = ch; } else line = t;
      if (out.length >= maxLines) break;
    }
    if (out.length >= maxLines) break;
    out.push(line);
  }
  if (out.length > maxLines) out.length = maxLines;
  if (out.length === maxLines && String(text).length > out.join("").length) out[maxLines - 1] = out[maxLines - 1].slice(0, -1) + "…";
  return out;
}

class Avatar {
  constructor(role, idx, theme) {
    this.role = role; this.idx = idx; this.theme = theme;
    this.state = "idle"; this.sayUntil = 0; this.phase = Math.random() * Math.PI * 2;
    this.group = new THREE.Group();
    this.color = new THREE.Color(role.color || "#2a78d6");
    const skin = SKIN[idx % SKIN.length], hair = HAIR[idx % HAIR.length];
    const mat = (color, opts = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.75, metalness: 0.05, ...opts });
    const add = (geo, m, x, y, z, parent = this.group) => { const mesh = new THREE.Mesh(geo, m); mesh.position.set(x, y, z); mesh.castShadow = true; mesh.receiveShadow = true; parent.add(mesh); return mesh; };

    // 의자
    add(new THREE.BoxGeometry(0.56, 0.08, 0.52), mat(0x3b3b3b), 0, 0.5, -0.05);
    add(new THREE.BoxGeometry(0.56, 0.62, 0.08), mat(0x3b3b3b), 0, 0.85, -0.3);
    add(new THREE.CylinderGeometry(0.04, 0.04, 0.46, 8), mat(0x777777), 0, 0.23, -0.05);
    add(new THREE.CylinderGeometry(0.26, 0.26, 0.03, 16), mat(0x555555), 0, 0.02, -0.05);
    // 다리(앉은 자세)
    add(new THREE.BoxGeometry(0.16, 0.14, 0.42), mat(0x2e3a59), -0.13, 0.6, 0.18);
    add(new THREE.BoxGeometry(0.16, 0.14, 0.42), mat(0x2e3a59), 0.13, 0.6, 0.18);
    add(new THREE.CylinderGeometry(0.07, 0.07, 0.5, 10), mat(0x2e3a59), -0.13, 0.3, 0.38);
    add(new THREE.CylinderGeometry(0.07, 0.07, 0.5, 10), mat(0x2e3a59), 0.13, 0.3, 0.38);
    add(new THREE.BoxGeometry(0.17, 0.08, 0.26), mat(0x222222), -0.13, 0.04, 0.46);
    add(new THREE.BoxGeometry(0.17, 0.08, 0.26), mat(0x222222), 0.13, 0.04, 0.46);
    // 몸통(역할 색 셔츠)
    this.torso = add(new THREE.CapsuleGeometry(0.23, 0.4, 4, 14), mat(this.color), 0, 1.02, 0);
    // 머리
    this.head = new THREE.Group(); this.head.position.set(0, 1.58, 0); this.group.add(this.head);
    add(new THREE.CylinderGeometry(0.07, 0.08, 0.12, 10), mat(skin), 0, -0.14, 0, this.head);
    add(new THREE.SphereGeometry(0.2, 22, 16), mat(skin), 0, 0, 0, this.head);
    add(new THREE.SphereGeometry(0.205, 22, 12, 0, Math.PI * 2, 0, 1.35), mat(hair), 0, 0.02, -0.01, this.head);
    const eye = mat(0x1a1a1a, { roughness: 0.3 });
    add(new THREE.SphereGeometry(0.024, 10, 8), eye, -0.07, 0.02, 0.18, this.head);
    add(new THREE.SphereGeometry(0.024, 10, 8), eye, 0.07, 0.02, 0.18, this.head);
    this.mouth = add(new THREE.BoxGeometry(0.07, 0.016, 0.02), mat(0x8a3b3b), 0, -0.08, 0.19, this.head);
    this.accessory(idx, mat, add);
    // 팔(어깨 피벗)
    this.arms = [-1, 1].map((side) => {
      const piv = new THREE.Group(); piv.position.set(side * 0.3, 1.26, 0.02); this.group.add(piv);
      add(new THREE.CapsuleGeometry(0.06, 0.26, 4, 10), mat(this.color), 0, -0.17, 0, piv);
      const elbow = new THREE.Group(); elbow.position.set(0, -0.32, 0); piv.add(elbow);
      add(new THREE.CapsuleGeometry(0.055, 0.24, 4, 10), mat(skin), 0, -0.14, 0, elbow);
      add(new THREE.SphereGeometry(0.06, 10, 8), mat(skin), 0, -0.3, 0, elbow);
      return { piv, elbow, side };
    });
    // 책상 + 모니터 + 키보드
    const deskMat = mat(0x9c7a55);
    add(new THREE.BoxGeometry(1.25, 0.05, 0.62), deskMat, 0, 0.78, 0.62);
    [[-0.58, 0.36], [0.58, 0.36], [-0.58, 0.88], [0.58, 0.88]].forEach(([x, z]) => add(new THREE.BoxGeometry(0.05, 0.76, 0.05), mat(0x5c4631), x, 0.39, z));
    add(new THREE.BoxGeometry(0.42, 0.02, 0.14), mat(0x2b2b2b), 0, 0.81, 0.5);
    add(new THREE.BoxGeometry(0.12, 0.12, 0.03), mat(0x444444), 0, 0.87, 0.86);
    add(new THREE.BoxGeometry(0.64, 0.4, 0.03), mat(0x1f1f1f), 0, 1.16, 0.88);
    this.screen = add(new THREE.PlaneGeometry(0.58, 0.34), new THREE.MeshStandardMaterial({ color: 0x0d1b2a, emissive: new THREE.Color(0x173a5c), emissiveIntensity: 0.4, roughness: 0.4 }), 0, 1.16, 0.864);
    this.screen.rotation.y = Math.PI;   // 화면은 아바타(−z) 쪽을 본다
    // 바닥 상태 링
    this.ring = new THREE.Mesh(new THREE.RingGeometry(0.62, 0.72, 40), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0, side: THREE.DoubleSide }));
    this.ring.rotation.x = -Math.PI / 2; this.ring.position.y = 0.012; this.group.add(this.ring);
    // 이름표 · 말풍선
    this.label = new THREE.Sprite(new THREE.SpriteMaterial({ transparent: true, depthWrite: false }));
    this.label.position.set(0, 2.25, 0); this.group.add(this.label);
    this.bubble = new THREE.Sprite(new THREE.SpriteMaterial({ transparent: true, depthWrite: false, opacity: 0 }));
    this.bubble.position.set(0.55, 2.95, 0.2); this.bubble.scale.set(2.6, 1.46, 1); this.bubble.visible = false; this.group.add(this.bubble);
    this.group.traverse((o) => { o.userData.roleKey = role.key; });
    this.updateLabel();
  }
  accessory(idx, mat, add) {
    const k = this.role.key;
    if (k === "pm") add(new THREE.BoxGeometry(0.07, 0.3, 0.02), mat(0xb02a2a), 0, 1.1, 0.23);                               // 넥타이
    else if (k === "designer") { const b = add(new THREE.CylinderGeometry(0.24, 0.2, 0.07, 18), mat(0xb5443b), 0.03, 0.17, -0.02, this.head); b.rotation.z = 0.25; }   // 베레모
    else if (k === "developer") { const h = add(new THREE.TorusGeometry(0.21, 0.025, 8, 24, Math.PI), mat(0x222222), 0, 0.03, 0, this.head); h.rotation.z = Math.PI; h.rotation.y = Math.PI / 2; add(new THREE.SphereGeometry(0.06, 10, 8), mat(0x222222), -0.21, 0.0, 0, this.head); add(new THREE.SphereGeometry(0.06, 10, 8), mat(0x222222), 0.21, 0.0, 0, this.head); }  // 헤드폰
    else if (k === "qa") { [-0.07, 0.07].forEach((x) => { const g = add(new THREE.TorusGeometry(0.05, 0.008, 8, 20), mat(0x111111), x, 0.02, 0.19, this.head); g.rotation.x = 0; }); }  // 안경
    else if (k === "techlead") { add(new THREE.BoxGeometry(0.14, 0.09, 0.01), mat(0xf0f0f0), 0.14, 0.95, 0.24); add(new THREE.BoxGeometry(0.02, 0.3, 0.01), mat(0x2a78d6), 0.14, 1.15, 0.24); }  // 사원증
    else add(new THREE.BoxGeometry(0.1, 0.1, 0.02), mat(0xdddddd), 0.15, 1.0, 0.24);
  }
  statusText() {
    return { working: "작업 중", speaking: "발언 중", done: "완료", fail: "실패/결함", idle: "대기" }[this.state] || this.state;
  }
  updateLabel() {
    const r = this.role, th = this.theme, stateColor = { working: th.accent, speaking: th.accent, done: th.good, fail: th.critical, idle: th.muted }[this.state] || th.muted;
    const tex = makeTextTexture((ctx, w, h) => {
      ctx.fillStyle = th.labelBg; roundRect(ctx, 6, 6, w - 12, h - 12, 22); ctx.fill();
      ctx.fillStyle = "#" + this.color.getHexString(); roundRect(ctx, 6, 6, 18, h - 12, 9); ctx.fill();
      ctx.fillStyle = th.text; ctx.font = "600 46px " + th.font; ctx.textBaseline = "middle";
      ctx.fillText(r.name_ko || r.key, 40, 52);
      ctx.font = "400 28px " + th.font; ctx.fillStyle = th.muted;
      ctx.fillText(`${r.title || ""}${r.model ? " · " + r.model : ""}`.slice(0, 40), 40, 104);
      ctx.fillStyle = stateColor; ctx.beginPath(); ctx.arc(w - 48, 52, 14, 0, Math.PI * 2); ctx.fill();
      ctx.font = "600 26px " + th.font; ctx.textAlign = "right"; ctx.fillText(this.statusText(), w - 72, 52);
    }, 640, 160);
    if (this.label.material.map) this.label.material.map.dispose();
    this.label.material.map = tex; this.label.material.needsUpdate = true;
    this.label.scale.set(1.6, 0.4, 1);
  }
  say(text, ms = 12000) {
    const th = this.theme;
    const tex = makeTextTexture((ctx, w, h) => {
      ctx.fillStyle = th.bubbleBg; roundRect(ctx, 8, 8, w - 16, h - 60, 28); ctx.fill();
      ctx.beginPath(); ctx.moveTo(90, h - 52); ctx.lineTo(40, h - 8); ctx.lineTo(150, h - 52); ctx.closePath(); ctx.fill();
      ctx.strokeStyle = "#" + this.color.getHexString(); ctx.lineWidth = 6; roundRect(ctx, 8, 8, w - 16, h - 60, 28); ctx.stroke();
      ctx.fillStyle = th.bubbleText; ctx.font = "400 30px " + th.font; ctx.textBaseline = "top";
      wrapLines(ctx, text, w - 64, 7).forEach((ln, i) => ctx.fillText(ln, 32, 30 + i * 40));
    }, 720, 400);
    if (this.bubble.material.map) this.bubble.material.map.dispose();
    this.bubble.material.map = tex; this.bubble.material.needsUpdate = true;
    this.bubble.visible = true; this.bubble.material.opacity = 1;
    this.sayUntil = performance.now() + ms;
  }
  setState(state) { if (state !== this.state) { this.state = state; this.updateLabel(); } }
  animate(t, now) {
    const speaking = now < this.sayUntil;
    const st = speaking ? "speaking" : this.state;
    const th = this.theme;
    if (!speaking && this.bubble.visible) { this.bubble.material.opacity -= 0.04; if (this.bubble.material.opacity <= 0) this.bubble.visible = false; }
    const ph = this.phase;
    this.torso.scale.y = 1 + Math.sin(t * 1.8 + ph) * 0.012;
    const ring = { working: th.accent, speaking: th.accent, done: th.good, fail: th.critical }[st];
    this.ring.material.color.set(ring || "#ffffff");
    this.ring.material.opacity = ring ? (st === "working" || st === "speaking" ? 0.45 + Math.sin(t * 4 + ph) * 0.25 : 0.55) : 0;
    const [L, R] = this.arms;
    const ease = (obj, key, target, k = 0.12) => { obj[key] += (target - obj[key]) * k; };
    const armTo = (a, x, z, ex) => { ease(a.piv.rotation, "x", x); ease(a.piv.rotation, "z", z); ease(a.elbow.rotation, "x", ex); };
    this.screen.material.emissiveIntensity = st === "working" ? 0.9 + Math.sin(t * 9 + ph) * 0.25 + (Math.random() < 0.03 ? 0.4 : 0) : 0.35;
    if (st === "working") {
      ease(this.head.rotation, "x", 0.28); ease(this.head.rotation, "y", Math.sin(t * 0.9 + ph) * 0.12);
      armTo(L, -0.95 + Math.sin(t * 13 + ph) * 0.1, 0.12, -1.0 + Math.cos(t * 11 + ph) * 0.12);
      armTo(R, -0.95 + Math.cos(t * 12.5 + ph) * 0.1, -0.12, -1.0 + Math.sin(t * 10 + ph) * 0.12);
    } else if (st === "speaking") {
      ease(this.head.rotation, "x", -0.05 + Math.sin(t * 5.5) * 0.05); ease(this.head.rotation, "y", Math.sin(t * 1.3) * 0.2);
      armTo(L, -1.2, 0.25 + Math.sin(t * 3) * 0.1, -1.5);
      armTo(R, -0.6, -0.1, -0.9);
      this.mouth.scale.y = 1.6 + Math.sin(t * 16) * 1.2;
    } else if (st === "done") {
      ease(this.head.rotation, "x", -0.1); ease(this.head.rotation, "y", Math.sin(t * 0.6 + ph) * 0.1);
      armTo(L, -0.5, 0.1, -0.7);
      armTo(R, -2.6, -0.35 + Math.sin(t * 2.5) * 0.08, -0.4);
    } else if (st === "fail") {
      ease(this.head.rotation, "x", 0.15); ease(this.head.rotation, "y", Math.sin(t * 4.5) * 0.22);
      armTo(L, -1.25, 0.75, -1.4);
      armTo(R, -1.25, -0.75, -1.4);
    } else {
      ease(this.head.rotation, "x", 0.05); ease(this.head.rotation, "y", Math.sin(t * 0.5 + ph) * 0.25);
      armTo(L, -0.55, 0.1, -0.5);
      armTo(R, -0.55, -0.1, -0.5);
    }
    if (st !== "speaking") this.mouth.scale.y += (1 - this.mouth.scale.y) * 0.2;
  }
  dispose() {
    this.group.traverse((o) => { if (o.geometry) o.geometry.dispose(); if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); } });
  }
}

export class CrewRoom {
  constructor(container, { onSelect } = {}) {
    this.container = container; this.onSelect = onSelect;
    this.avatars = new Map(); this.roleKeys = []; this.active = true; this.room = null;
    this.theme = this.readTheme();
    const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "low-power" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true; renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.domElement.className = "crew-canvas";
    container.appendChild(renderer.domElement);
    this.renderer = renderer;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
    this.camera.position.set(0, 4.2, 8.6);
    this.controls = new OrbitControls(this.camera, renderer.domElement);
    Object.assign(this.controls, { enableDamping: true, dampingFactor: 0.08, minDistance: 3.5, maxDistance: 14, maxPolarAngle: 1.45, minPolarAngle: 0.35, enablePan: false, autoRotate: true, autoRotateSpeed: 0.35 });
    this.controls.target.set(0, 1.25, -0.6);
    this.controls.addEventListener("start", () => { this.controls.autoRotate = false; clearTimeout(this._ar); this._ar = setTimeout(() => (this.controls.autoRotate = true), 25000); });
    this.buildRoom();
    this.raycaster = new THREE.Raycaster(); this.pointer = new THREE.Vector2(); this.hover = null;
    renderer.domElement.addEventListener("pointermove", (e) => this.pick(e, false));
    renderer.domElement.addEventListener("click", (e) => this.pick(e, true));
    this.ro = new ResizeObserver(() => this.resize()); this.ro.observe(container);
    this.resize();
    this.clock = new THREE.Clock();
    this.loop = this.loop.bind(this);
    this.raf = requestAnimationFrame(this.loop);
  }
  readTheme() {
    const v = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    const dark = window.matchMedia("(prefers-color-scheme: dark)").matches && document.documentElement.dataset.theme !== "light" || document.documentElement.dataset.theme === "dark";
    return { dark, bg: v("--surface-2") || (dark ? "#222220" : "#f0efeb"), floor: dark ? "#2a2a28" : "#d8d4c8", wall: dark ? "#1d1d1c" : "#ece9e0", accent: v("--accent") || "#2a78d6",
             good: v("--good") || "#0ca30c", critical: v("--critical") || "#d03b3b", muted: v("--muted") || "#7a7974", text: v("--text") || "#0b0b0b",
             labelBg: dark ? "rgba(26,26,25,0.92)" : "rgba(252,252,251,0.94)", bubbleBg: dark ? "rgba(34,34,32,0.96)" : "rgba(255,255,255,0.97)", bubbleText: v("--text") || "#0b0b0b",
             font: '"Pretendard Variable", Pretendard, -apple-system, "Segoe UI", "Noto Sans KR", "Apple SD Gothic Neo", sans-serif' };
  }
  buildRoom() {
    const th = this.theme, s = this.scene;
    s.background = new THREE.Color(th.bg);
    s.fog = new THREE.Fog(new THREE.Color(th.bg), 14, 26);
    this.hemi = new THREE.HemisphereLight(0xffffff, 0x8899aa, th.dark ? 0.65 : 0.9); s.add(this.hemi);
    const sun = new THREE.DirectionalLight(0xffffff, th.dark ? 1.4 : 1.8); sun.position.set(4, 9, 6); sun.castShadow = true;
    sun.shadow.mapSize.set(1024, 1024); sun.shadow.camera.near = 1; sun.shadow.camera.far = 30; Object.assign(sun.shadow.camera, { left: -9, right: 9, top: 9, bottom: -9 }); sun.shadow.bias = -0.0005; s.add(sun);
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(22, 16), new THREE.MeshStandardMaterial({ color: th.floor, roughness: 0.95 }));
    floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; s.add(floor); this.floor = floor;
    const grid = new THREE.GridHelper(22, 22, th.dark ? 0x3a3a38 : 0xc9c4b6, th.dark ? 0x30302e : 0xd2cdbf); grid.position.y = 0.005; grid.material.transparent = true; grid.material.opacity = th.dark ? 0.22 : 0.45; s.add(grid); this.grid = grid;
    const wall = new THREE.Mesh(new THREE.PlaneGeometry(22, 6), new THREE.MeshStandardMaterial({ color: th.wall, roughness: 1 }));
    wall.position.set(0, 3, -6); wall.receiveShadow = true; s.add(wall); this.wall = wall;
    // 화이트보드(현재 레포·티켓·단계)
    this.board = new THREE.Mesh(new THREE.PlaneGeometry(4.8, 2.2), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    this.board.position.set(0, 2.55, -5.95); s.add(this.board);
    const frame = new THREE.Mesh(new THREE.BoxGeometry(5.0, 2.4, 0.06), new THREE.MeshStandardMaterial({ color: 0x8a8a8a })); frame.position.set(0, 2.55, -5.99); s.add(frame);
    // 가운데 회의 테이블 + 화분
    const table = new THREE.Mesh(new THREE.CylinderGeometry(1.1, 1.1, 0.06, 32), new THREE.MeshStandardMaterial({ color: 0xa88d6a, roughness: 0.7 })); table.position.set(0, 0.76, -0.4); table.castShadow = table.receiveShadow = true; s.add(table);
    const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.08, 0.3, 0.74, 16), new THREE.MeshStandardMaterial({ color: 0x555555 })); leg.position.set(0, 0.37, -0.4); s.add(leg);
    const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.12, 0.22, 12), new THREE.MeshStandardMaterial({ color: 0xb5651d })); pot.position.set(0, 0.9, -0.4); s.add(pot);
    const plant = new THREE.Mesh(new THREE.IcosahedronGeometry(0.28, 1), new THREE.MeshStandardMaterial({ color: 0x3f8f3f, flatShading: true })); plant.position.set(0, 1.2, -0.4); plant.castShadow = true; s.add(plant);
    this.updateBoard({});
  }
  updateBoard({ repo, ticket, stage, daemon, roles = [], status = {} }) {
    const th = this.theme;
    const tex = makeTextTexture((ctx, w, h) => {
      ctx.fillStyle = "#fbfbf8"; ctx.fillRect(0, 0, w, h);
      ctx.fillStyle = "#1b3a6b"; ctx.font = "700 60px " + th.font; ctx.textBaseline = "top";
      ctx.fillText(repo ? `${repo}` : "크루 룸", 36, 30);
      ctx.font = "400 34px " + th.font; ctx.fillStyle = "#333";
      const lines = [];
      if (ticket) lines.push(`티켓: ${ticket}`);
      if (stage) lines.push(`단계: ${({ pm: "PRD", design: "디자인", dev: "구현", qa: "QA 검증", discover: "발굴", merge: "자동 머지" })[stage] || stage}`);
      if (daemon) lines.push(`데몬: ${daemon}`);
      if (!lines.length) lines.push(repo ? "진행 중인 작업 없음" : "레포를 선택하세요");
      lines.forEach((ln, i) => ctx.fillText(ln, 36, 120 + i * 48));
      ctx.font = "500 30px " + th.font;
      roles.forEach((r, i) => {
        const st = (status[r.key] || {}).state || "idle";
        const col = { working: "#2a78d6", done: "#0ca30c", fail: "#d03b3b" }[st] || "#9a9a9a";
        const x = 36 + (i % 3) * 300, y = 300 + Math.floor(i / 3) * 44;
        ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x + 12, y + 16, 11, 0, Math.PI * 2); ctx.fill();
        ctx.fillStyle = "#222"; ctx.fillText(`${r.name_ko}: ${({ working: "작업 중", done: "완료", fail: "실패", idle: "대기" })[st]}`, x + 36, y);
      });
      // 핀 4개
      ctx.fillStyle = "#c0392b"; [[20, 20], [w - 20, 20], [20, h - 20], [w - 20, h - 20]].forEach(([x, y]) => { ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.fill(); });
    }, 1040, 480);
    if (this.board.material.map) this.board.material.map.dispose();
    this.board.material.map = tex; this.board.material.needsUpdate = true;
  }
  setRoom(room) {
    this.room = room;
    const roles = room.roles || [];
    const keys = roles.map((r) => r.key).join("|");
    if (keys !== this.roleKeys.join("|")) {
      for (const a of this.avatars.values()) { this.scene.remove(a.group); a.dispose(); }
      this.avatars.clear();
      const n = roles.length, radius = 4.1, spread = Math.min(150, 40 + n * 24);
      roles.forEach((r, i) => {
        const a = new Avatar(r, i, this.theme);
        const ang = (n === 1 ? 0 : -spread / 2 + (spread / (n - 1)) * i) * DEG;
        a.group.position.set(Math.sin(ang) * radius, 0, -0.4 - Math.cos(ang) * radius * 0.78);
        a.group.rotation.y = Math.atan2(-a.group.position.x, -(a.group.position.z + 0.4)) + Math.PI;  // 가운데 테이블을 본다
        this.scene.add(a.group);
        this.avatars.set(r.key, a);
      });
      this.roleKeys = roles.map((r) => r.key);
    }
    for (const r of roles) {
      const a = this.avatars.get(r.key), st = (room.status || {})[r.key] || {};
      a.role = r; a.setState(st.state || "idle");
    }
    this.updateBoard({ repo: room.name, ticket: room.ticket, stage: room.stage, daemon: room.daemonText, roles, status: room.status || {} });
  }
  say(key, text, ms) { const a = this.avatars.get(key); if (a) a.say(text, ms); }
  setActive(on) { this.active = on; if (on && !this.raf) this.raf = requestAnimationFrame(this.loop); }
  refreshTheme() {
    this.theme = this.readTheme();
    const th = this.theme;
    this.scene.background.set(th.bg); this.scene.fog.color.set(th.bg);
    this.floor.material.color.set(th.floor); this.wall.material.color.set(th.wall);
    this.hemi.intensity = th.dark ? 0.65 : 0.9;
    for (const a of this.avatars.values()) { a.theme = th; a.updateLabel(); }
    if (this.room) this.updateBoard({ repo: this.room.name, ticket: this.room.ticket, stage: this.room.stage, daemon: this.room.daemonText, roles: this.room.roles, status: this.room.status });
  }
  pick(e, click) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);
    const hits = this.raycaster.intersectObjects([...this.avatars.values()].map((a) => a.group), true);
    const key = hits.length ? hits[0].object.userData.roleKey : null;
    this.renderer.domElement.style.cursor = key ? "pointer" : "grab";
    if (click && key && this.onSelect) this.onSelect(key);
  }
  resize() {
    const w = this.container.clientWidth || 600, h = this.container.clientHeight || 480;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
  }
  loop() {
    this.raf = null;
    if (!this.active || !this.container.isConnected) return;
    const t = this.clock.getElapsedTime(), now = performance.now();
    this.controls.update();
    for (const a of this.avatars.values()) a.animate(t, now);
    this.renderer.render(this.scene, this.camera);
    this.raf = requestAnimationFrame(this.loop);
  }
  destroy() {
    this.active = false; if (this.raf) cancelAnimationFrame(this.raf); this.ro.disconnect();
    for (const a of this.avatars.values()) a.dispose();
    this.controls.dispose(); this.renderer.dispose(); this.renderer.domElement.remove();
  }
}

window.CrewRoom = CrewRoom;
window.dispatchEvent(new Event("crewroom-ready"));
