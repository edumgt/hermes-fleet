/* 크루 룸 3D (Hermes Fleet Virtual Office) — Three.js(ES module)
   - 실사풍 모던 오피스: 중앙 메인 회의실, 좌측 HERMES 카페·팬트리, 우측 릴랙스 힐링 라운지
   - AI 에이전트들의 업무 중 휴식(Rest & Recharge)과 상호 양보(Harmony & Concession) 연출
   - 파노라마 통창(자연 채광/도시 뷰), 우드 플랭크 바닥, 인체공학적 가구, 유리 파티션, 플랜테리어
   - 카메라 뷰 프리셋(전체·회의실·카페·라운지) 및 부드러운 시점 전환
*/
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const SKIN = [0xf2c9a6, 0xe8b794, 0xd9a07e, 0xf5d3b3, 0xc98a63];
const HAIR = [0x221c18, 0x3d2719, 0x111111, 0x5a3e26, 0x303030, 0x7a4e28];
const DEG = Math.PI / 180;

// 휴식 모드 시 역할별 힐링 멘트
const ROLE_REST_QUOTES = {
  pm: "잠시 커피 한 잔 마시며 로드맵 우선순위를 정리합니다 ☕",
  designer: "차 한 잔과 함께 더 직관적이고 편안한 UI 아이디어를 구상 중이에요 🍵",
  developer: "카페인 충전 완료! 잠시 숨 고르고 깨끗하고 견고한 코드로 이어갑니다 ☕",
  qa: "꼼꼼한 테스트 후 잠시 힐링 타임. 품질은 마음의 여유에서 나옵니다 🧃",
  techlead: "동료들의 쉼과 밸런스를 살피며 따뜻한 차 한 잔 나누고 있습니다 🍵",
};

// 상호 양보 & 화합 모드 시 역할별 배려 멘트
const ROLE_HARMONY_QUOTES = {
  pm: "팀원들의 피드백을 적극 경청하며 스코프를 유연하게 양보·조율하겠습니다 🤝",
  designer: "개발과 QA의 기술적 제안에 깊이 공감하며 더 합리적인 안으로 양보합니다 ✨",
  developer: "지적해주신 개선점에 깊이 감사드리며 기꺼이 안전한 방향으로 양보하겠습니다 💡",
  qa: "결함 지적이 아닌 함께 완성도를 높이는 동반자로서 기꺼이 협력합니다 🕊️",
  techlead: "모두의 소중한 의견이 조화를 이루도록 서로 배려하며 최적의 절충안을 찾았습니다 🤝",
};

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

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

function makeTextTexture(draw, w = 512, h = 160) {
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  const ctx = c.getContext("2d");
  draw(ctx, w, h);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return tex;
}

// 실사풍 우드 플랭크 바닥 텍스처 생성 (절묘한 오크 나뭇결과 플랭크 음영)
function createWoodFloorTexture(dark = false) {
  const c = document.createElement("canvas"); c.width = 1024; c.height = 1024;
  const ctx = c.getContext("2d");
  const baseColor = dark ? "#2c2621" : "#cfbba6";
  const plankColors = dark
    ? ["#26201b", "#2a241f", "#2f2823", "#231e1a", "#332c25"]
    : ["#d6c4b2", "#cfbca8", "#c7b39d", "#decbb9", "#c0ab94"];

  ctx.fillStyle = baseColor;
  ctx.fillRect(0, 0, 1024, 1024);

  const rows = 16;
  const rowH = 1024 / rows;
  const cols = 5;

  for (let r = 0; r < rows; r++) {
    const y = r * rowH;
    const offset = (r % 2) * (1024 / cols / 2);
    const colW = 1024 / cols;

    for (let cIdx = -1; cIdx <= cols + 1; cIdx++) {
      const x = cIdx * colW + offset;
      const color = plankColors[(r * 3 + Math.abs(cIdx) * 2) % plankColors.length];
      ctx.fillStyle = color;
      ctx.fillRect(x + 2, y + 2, colW - 4, rowH - 4);

      // 나뭇결 미세 라인
      ctx.strokeStyle = dark ? "rgba(255,255,255,0.03)" : "rgba(0,0,0,0.04)";
      ctx.lineWidth = 1;
      for (let g = 0; g < 4; g++) {
        const gy = y + 8 + g * (rowH / 4) + ((r + cIdx) % 3);
        ctx.beginPath();
        ctx.moveTo(x + 4, gy);
        ctx.lineTo(x + colW - 6, gy + ((g % 2 === 0) ? 2 : -2));
        ctx.stroke();
      }

      // 플랭크 모서리 음영 (베벨)
      ctx.strokeStyle = dark ? "rgba(0,0,0,0.6)" : "rgba(80,60,40,0.35)";
      ctx.lineWidth = 2;
      ctx.strokeRect(x + 1, y + 1, colW - 2, rowH - 2);
    }
  }

  const tex = new THREE.CanvasTexture(c);
  tex.wrapS = THREE.RepeatWrapping;
  tex.wrapT = THREE.RepeatWrapping;
  tex.repeat.set(4, 3);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// 통유리 너머 도시 스카이라인 & 자연 뷰 텍스처
function createSkylineTexture(dark = false) {
  const c = document.createElement("canvas"); c.width = 1024; c.height = 512;
  const ctx = c.getContext("2d");

  // 하늘 그라데이션
  const grad = ctx.createLinearGradient(0, 0, 0, 512);
  if (dark) {
    grad.addColorStop(0, "#080e1a");
    grad.addColorStop(0.55, "#152238");
    grad.addColorStop(1, "#21324e");
  } else {
    grad.addColorStop(0, "#90caf9");
    grad.addColorStop(0.5, "#bbdefb");
    grad.addColorStop(0.85, "#e3f2fd");
    grad.addColorStop(1, "#fff9c4");
  }
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, 1024, 512);

  // 원경 산 / 구릉지대 실루엣
  ctx.fillStyle = dark ? "rgba(18, 28, 48, 0.7)" : "rgba(165, 195, 180, 0.55)";
  ctx.beginPath();
  ctx.moveTo(0, 360);
  ctx.bezierCurveTo(240, 310, 480, 380, 720, 330);
  ctx.bezierCurveTo(860, 300, 960, 340, 1024, 330);
  ctx.lineTo(1024, 512);
  ctx.lineTo(0, 512);
  ctx.closePath();
  ctx.fill();

  // 고층 빌딩 스카이라인
  const bldgColor = dark ? "rgba(10, 18, 32, 0.9)" : "rgba(130, 150, 175, 0.65)";
  ctx.fillStyle = bldgColor;
  const bldgs = [
    [40, 180, 48, 300], [95, 120, 60, 360], [165, 200, 50, 280], [230, 150, 70, 330],
    [320, 220, 45, 260], [380, 110, 55, 370], [450, 170, 65, 310], [535, 140, 50, 340],
    [605, 210, 60, 270], [680, 95, 75, 385], [775, 160, 55, 320], [845, 130, 65, 350],
    [925, 190, 55, 290]
  ];
  bldgs.forEach(([x, y, w, h]) => {
    ctx.fillRect(x, y, w, h);
    if (dark) {
      // 밤 창문 불빛
      ctx.fillStyle = "rgba(255, 235, 160, 0.75)";
      for (let by = y + 14; by < y + h - 14; by += 16) {
        for (let bx = x + 8; bx < x + w - 8; bx += 10) {
          if (Math.random() > 0.45) ctx.fillRect(bx, by, 4, 6);
        }
      }
      ctx.fillStyle = bldgColor;
    }
  });

  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// 카페 네온 간판 텍스처
function createNeonTexture() {
  const c = document.createElement("canvas"); c.width = 512; c.height = 140;
  const ctx = c.getContext("2d");
  ctx.fillStyle = "#121418";
  roundRect(ctx, 4, 4, 504, 132, 18);
  ctx.fill();
  ctx.strokeStyle = "#333b48"; ctx.lineWidth = 4;
  roundRect(ctx, 4, 4, 504, 132, 18);
  ctx.stroke();

  // 글로우 네온 텍스트
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.font = '700 42px "Pretendard Variable", sans-serif';

  // 1차 글로우
  ctx.shadowColor = "#ff9800";
  ctx.shadowBlur = 18;
  ctx.fillStyle = "#ffb74d";
  ctx.fillText("☕ HERMES CAFE", 256, 52);

  ctx.shadowColor = "#4fc3f7";
  ctx.shadowBlur = 12;
  ctx.font = '500 22px "Pretendard Variable", sans-serif';
  ctx.fillStyle = "#b3e5fc";
  ctx.fillText("COFFEE & RECHARGE LOUNGE", 256, 98);

  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// ==========================================
// 아바타 클래스 (개선된 인체 비율 & 디테일 애니메이션)
// ==========================================
class Avatar {
  constructor(role, idx, theme) {
    this.role = role;
    this.idx = idx;
    this.theme = theme;
    this.state = "idle";
    this.sayUntil = 0;
    this.phase = Math.random() * Math.PI * 2;
    this.blinkTimer = Math.random() * 3 + 2;
    this.isBlinking = false;
    this.group = new THREE.Group();
    this.color = new THREE.Color(role.color || "#2a78d6");

    const skin = SKIN[idx % SKIN.length];
    const hair = HAIR[idx % HAIR.length];
    const mat = (color, opts = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.65, metalness: 0.08, ...opts });
    const add = (geo, m, x, y, z, parent = this.group) => {
      const mesh = new THREE.Mesh(geo, m);
      mesh.position.set(x, y, z);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      parent.add(mesh);
      return mesh;
    };

    // 1. 인체공학적 오피스 체어 (Ergonomic Mesh Chair)
    const chairMat = mat(0x282c34, { roughness: 0.55 });
    const chromeMat = mat(0xcccccc, { roughness: 0.2, metalness: 0.8 });
    // 좌판
    add(new THREE.BoxGeometry(0.56, 0.08, 0.52), chairMat, 0, 0.48, -0.05);
    // 곡면 등받이 & 요추 지지대
    add(new THREE.BoxGeometry(0.52, 0.68, 0.07), chairMat, 0, 0.86, -0.3);
    add(new THREE.BoxGeometry(0.42, 0.16, 0.04), mat(0x1a1a1a), 0, 0.72, -0.27);
    // 중심축 & 5발 스타 베이스
    add(new THREE.CylinderGeometry(0.035, 0.035, 0.42, 12), chromeMat, 0, 0.22, -0.05);
    for (let i = 0; i < 5; i++) {
      const legAng = (i / 5) * Math.PI * 2;
      const starLeg = add(new THREE.BoxGeometry(0.04, 0.03, 0.32), chromeMat, 0, 0.04, 0.14);
      starLeg.rotation.y = legAng;
      starLeg.position.set(Math.sin(legAng) * 0.16, 0.04, -0.05 + Math.cos(legAng) * 0.16);
    }
    // 팔걸이
    [-0.27, 0.27].forEach((sideX) => {
      add(new THREE.BoxGeometry(0.04, 0.22, 0.04), chromeMat, sideX, 0.62, -0.05);
      add(new THREE.BoxGeometry(0.07, 0.03, 0.28), chairMat, sideX, 0.74, 0.02);
    });

    // 2. 다리 (앉은 자세, 세련된 슬랙스)
    const pantsMat = mat(0x232936, { roughness: 0.8 });
    add(new THREE.BoxGeometry(0.16, 0.14, 0.44), pantsMat, -0.13, 0.58, 0.18);
    add(new THREE.BoxGeometry(0.16, 0.14, 0.44), pantsMat, 0.13, 0.58, 0.18);
    add(new THREE.CylinderGeometry(0.07, 0.065, 0.48, 12), pantsMat, -0.13, 0.28, 0.38);
    add(new THREE.CylinderGeometry(0.07, 0.065, 0.48, 12), pantsMat, 0.13, 0.28, 0.38);
    // 모던 스니커즈/구두
    const shoeMat = mat(0x18181a, { roughness: 0.4 });
    add(new THREE.BoxGeometry(0.15, 0.08, 0.28), shoeMat, -0.13, 0.04, 0.46);
    add(new THREE.BoxGeometry(0.15, 0.08, 0.28), shoeMat, 0.13, 0.04, 0.46);

    // 3. 상체 (역할 컬러 셔츠 + 옷깃)
    this.torso = add(new THREE.CapsuleGeometry(0.24, 0.42, 6, 16), mat(this.color), 0, 1.02, 0);
    // 화이트 이너 카라
    add(new THREE.BoxGeometry(0.14, 0.06, 0.08), mat(0xffffff), 0, 1.25, 0.12);

    // 4. 머리 & 표정
    this.head = new THREE.Group();
    this.head.position.set(0, 1.58, 0);
    this.group.add(this.head);
    // 목
    add(new THREE.CylinderGeometry(0.075, 0.085, 0.13, 12), mat(skin), 0, -0.14, 0, this.head);
    // 얼굴
    add(new THREE.SphereGeometry(0.205, 24, 18), mat(skin), 0, 0, 0, this.head);
    // 입체감 있는 헤어스타일
    add(new THREE.SphereGeometry(0.215, 24, 14, 0, Math.PI * 2, 0, 1.4), mat(hair), 0, 0.03, -0.015, this.head);
    add(new THREE.BoxGeometry(0.38, 0.1, 0.14), mat(hair), 0, 0.17, 0.09, this.head); // 앞머리 볼륨

    // 눈 (깜빡임 적용)
    const eyeMat = mat(0x151515, { roughness: 0.2 });
    this.leftEye = add(new THREE.SphereGeometry(0.024, 12, 10), eyeMat, -0.07, 0.02, 0.186, this.head);
    this.rightEye = add(new THREE.SphereGeometry(0.024, 12, 10), eyeMat, 0.07, 0.02, 0.186, this.head);
    // 입
    this.mouth = add(new THREE.BoxGeometry(0.07, 0.018, 0.02), mat(0x944242), 0, -0.08, 0.192, this.head);

    // 역할별 액세서리
    this.accessory(idx, mat, add);

    // 5. 양팔 (어깨 피벗 + 팔꿈치)
    this.arms = [-1, 1].map((side) => {
      const piv = new THREE.Group();
      piv.position.set(side * 0.31, 1.26, 0.02);
      this.group.add(piv);
      add(new THREE.CapsuleGeometry(0.065, 0.28, 6, 12), mat(this.color), 0, -0.17, 0, piv);

      const elbow = new THREE.Group();
      elbow.position.set(0, -0.34, 0);
      piv.add(elbow);
      add(new THREE.CapsuleGeometry(0.06, 0.24, 6, 12), mat(skin), 0, -0.14, 0, elbow);
      add(new THREE.SphereGeometry(0.065, 12, 10), mat(skin), 0, -0.3, 0, elbow); // 손
      return { piv, elbow, side };
    });

    // 6. 개인 워크스테이션: 슬림 데스크 & 메탈 노트북 (MacBook 스타일)
    const deskMat = mat(0x7c5d3b, { roughness: 0.5 }); // 프리미엄 월넛 데스크
    add(new THREE.BoxGeometry(1.22, 0.045, 0.62), deskMat, 0, 0.77, 0.62);
    // 모던 슬림 메탈 다리
    const deskLegMat = mat(0x333333, { metalness: 0.7, roughness: 0.3 });
    [[-0.56, 0.38], [0.56, 0.38], [-0.56, 0.86], [0.56, 0.86]].forEach(([x, z]) => {
      add(new THREE.CylinderGeometry(0.02, 0.02, 0.75, 12), deskLegMat, x, 0.375, z);
    });

    // 슬림 노트북
    const laptopMat = mat(0x8a929e, { metalness: 0.85, roughness: 0.25 });
    // 키보드 베이스
    add(new THREE.BoxGeometry(0.38, 0.015, 0.26), laptopMat, 0, 0.8, 0.58);
    // 키보드 트랙패드 디테일
    add(new THREE.BoxGeometry(0.32, 0.003, 0.14), mat(0x222222), 0, 0.808, 0.54);
    add(new THREE.BoxGeometry(0.12, 0.002, 0.08), mat(0x7a828e), 0, 0.808, 0.66);
    // 오픈된 노트북 화면 상판
    const screenLid = add(new THREE.BoxGeometry(0.38, 0.25, 0.015), laptopMat, 0, 0.93, 0.71);
    screenLid.rotation.x = -0.22;
    // 빛나는 노트북 화면
    this.screen = add(
      new THREE.PlaneGeometry(0.36, 0.23),
      new THREE.MeshStandardMaterial({
        color: 0x0a192f,
        emissive: new THREE.Color(0x1e4976),
        emissiveIntensity: 0.6,
        roughness: 0.3
      }),
      0, 0.93, 0.702
    );
    this.screen.rotation.x = -0.22;
    this.screen.rotation.y = Math.PI; // 화면은 아바타를 바라봄

    // 커피 머그잔 (책상 위 또는 손에 들기)
    this.deskMug = add(new THREE.CylinderGeometry(0.04, 0.035, 0.09, 14), mat(0xf0ece1), 0.32, 0.84, 0.52);
    // 커피 액체
    add(new THREE.CylinderGeometry(0.037, 0.037, 0.01, 14), mat(0x3e2723), 0.32, 0.88, 0.52);

    // 7. 바닥 상태 링 & 머리 위 화합 헤일로
    this.ring = new THREE.Mesh(
      new THREE.RingGeometry(0.68, 0.82, 48),
      new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0, side: THREE.DoubleSide })
    );
    this.ring.rotation.x = -Math.PI / 2;
    this.ring.position.y = 0.015;
    this.group.add(this.ring);

    // 머리 위 화합 헤일로 (상호 양보 및 평화 모드 시 활성화)
    this.halo = new THREE.Mesh(
      new THREE.TorusGeometry(0.28, 0.02, 12, 32),
      new THREE.MeshBasicMaterial({ color: 0x34d399, transparent: true, opacity: 0 })
    );
    this.halo.rotation.x = Math.PI / 2;
    this.halo.position.set(0, 1.95, 0);
    this.group.add(this.halo);

    // 8. 이름표(Label) 및 말풍선(Bubble)
    this.label = new THREE.Sprite(new THREE.SpriteMaterial({ transparent: true, depthWrite: false }));
    this.label.position.set(0, 2.3, 0);
    this.group.add(this.label);

    this.bubble = new THREE.Sprite(new THREE.SpriteMaterial({ transparent: true, depthWrite: false, opacity: 0 }));
    this.bubble.position.set(0.6, 3.0, 0.2);
    this.bubble.scale.set(2.8, 1.55, 1);
    this.bubble.visible = false;
    this.group.add(this.bubble);

    this.group.traverse((o) => { o.userData.roleKey = role.key; });
    this.updateLabel();
  }

  accessory(idx, mat, add) {
    const k = this.role.key;
    if (k === "pm") {
      // 버건디 넥타이 & 스마트 워치
      add(new THREE.BoxGeometry(0.07, 0.32, 0.02), mat(0xb02a2a), 0, 1.08, 0.24);
      add(new THREE.TorusGeometry(0.065, 0.015, 8, 16), mat(0x1a1a1a), -0.31, 0.88, 0.02);
    } else if (k === "designer") {
      // 스타일리시 베레모 & 터치펜
      const b = add(new THREE.CylinderGeometry(0.24, 0.19, 0.07, 20), mat(0xc2410c), 0.03, 0.18, -0.02, this.head);
      b.rotation.z = 0.22;
      b.rotation.x = -0.1;
    } else if (k === "developer") {
      // 프로페셔널 스튜디오 헤드폰
      const h = add(new THREE.TorusGeometry(0.22, 0.025, 8, 28, Math.PI), mat(0x222222), 0, 0.04, 0, this.head);
      h.rotation.z = Math.PI; h.rotation.y = Math.PI / 2;
      add(new THREE.SphereGeometry(0.065, 12, 10), mat(0x1e3a8a), -0.22, 0.01, 0, this.head);
      add(new THREE.SphereGeometry(0.065, 12, 10), mat(0x1e3a8a), 0.22, 0.01, 0, this.head);
    } else if (k === "qa") {
      // 정밀 메탈 안경
      [-0.07, 0.07].forEach((x) => {
        add(new THREE.TorusGeometry(0.045, 0.007, 8, 20), mat(0x0f172a), x, 0.02, 0.19, this.head);
      });
      add(new THREE.BoxGeometry(0.05, 0.007, 0.007), mat(0x0f172a), 0, 0.02, 0.195, this.head);
    } else if (k === "techlead") {
      // 엔지니어링 ID 카드 & 랜야드
      add(new THREE.BoxGeometry(0.14, 0.1, 0.01), mat(0xf8fafc), 0.14, 0.94, 0.24);
      add(new THREE.BoxGeometry(0.025, 0.32, 0.01), mat(0x2563eb), 0.14, 1.14, 0.24);
    } else {
      add(new THREE.BoxGeometry(0.12, 0.1, 0.02), mat(0xe2e8f0), 0.15, 0.98, 0.24);
    }
  }

  statusText() {
    return {
      working: "작업 중",
      speaking: "발언 중",
      done: "완료",
      fail: "검토/조율",
      resting: "휴식 중 ☕",
      harmony: "상호 양보 🤝",
      idle: "대기"
    }[this.state] || this.state;
  }

  updateLabel() {
    const r = this.role, th = this.theme;
    const stateColor = {
      working: th.accent,
      speaking: th.accent,
      done: th.good,
      fail: th.critical,
      resting: "#f59e0b",
      harmony: "#10b981",
      idle: th.muted
    }[this.state] || th.muted;

    const tex = makeTextTexture((ctx, w, h) => {
      // 글래스모피즘 배경
      ctx.fillStyle = th.labelBg;
      roundRect(ctx, 6, 6, w - 12, h - 12, 22);
      ctx.fill();
      ctx.strokeStyle = "rgba(255,255,255,0.12)";
      ctx.lineWidth = 2;
      roundRect(ctx, 6, 6, w - 12, h - 12, 22);
      ctx.stroke();

      // 역할 컬러 바
      ctx.fillStyle = "#" + this.color.getHexString();
      roundRect(ctx, 6, 6, 18, h - 12, 9);
      ctx.fill();

      // 이름 및 타이틀
      ctx.fillStyle = th.text;
      ctx.font = "600 44px " + th.font;
      ctx.textBaseline = "middle";
      ctx.fillText(r.name_ko || r.key, 40, 52);

      ctx.font = "400 28px " + th.font;
      ctx.fillStyle = th.muted;
      ctx.fillText(`${r.title || ""}${r.model ? " · " + r.model : ""}`.slice(0, 38), 40, 104);

      // 상태 인디케이터
      ctx.fillStyle = stateColor;
      ctx.beginPath();
      ctx.arc(w - 48, 52, 14, 0, Math.PI * 2);
      ctx.fill();

      ctx.font = "600 26px " + th.font;
      ctx.textAlign = "right";
      ctx.fillText(this.statusText(), w - 74, 52);
    }, 660, 160);

    if (this.label.material.map) this.label.material.map.dispose();
    this.label.material.map = tex;
    this.label.material.needsUpdate = true;
    this.label.scale.set(1.65, 0.4, 1);
  }

  say(text, ms = 12000) {
    const th = this.theme;
    const tex = makeTextTexture((ctx, w, h) => {
      ctx.fillStyle = th.bubbleBg;
      roundRect(ctx, 8, 8, w - 16, h - 60, 26);
      ctx.fill();
      // 말풍선 꼬리
      ctx.beginPath();
      ctx.moveTo(90, h - 52);
      ctx.lineTo(40, h - 8);
      ctx.lineTo(150, h - 52);
      ctx.closePath();
      ctx.fill();

      // 테두리 강조
      ctx.strokeStyle = "#" + this.color.getHexString();
      ctx.lineWidth = 6;
      roundRect(ctx, 8, 8, w - 16, h - 60, 26);
      ctx.stroke();

      ctx.fillStyle = th.bubbleText;
      ctx.font = "500 30px " + th.font;
      ctx.textBaseline = "top";
      wrapLines(ctx, text, w - 64, 7).forEach((ln, i) => ctx.fillText(ln, 32, 28 + i * 40));
    }, 720, 400);

    if (this.bubble.material.map) this.bubble.material.map.dispose();
    this.bubble.material.map = tex;
    this.bubble.material.needsUpdate = true;
    this.bubble.visible = true;
    this.bubble.material.opacity = 1;
    this.sayUntil = performance.now() + ms;
  }

  setState(state) {
    if (state !== this.state) {
      this.state = state;
      this.updateLabel();
    }
  }

  animate(t, now, delta) {
    const speaking = now < this.sayUntil;
    const st = speaking ? "speaking" : this.state;
    const th = this.theme;

    // 말풍선 페이드아웃
    if (!speaking && this.bubble.visible) {
      this.bubble.material.opacity -= 0.04;
      if (this.bubble.material.opacity <= 0) this.bubble.visible = false;
    }

    // 눈 깜빡임 루프
    this.blinkTimer -= delta;
    if (this.blinkTimer <= 0) {
      this.isBlinking = true;
      this.leftEye.scale.y = 0.15;
      this.rightEye.scale.y = 0.15;
      if (this.blinkTimer < -0.15) {
        this.isBlinking = false;
        this.leftEye.scale.y = 1;
        this.rightEye.scale.y = 1;
        this.blinkTimer = Math.random() * 4 + 2.5;
      }
    }

    const ph = this.phase;
    // 자연스러운 호흡 모션
    this.torso.scale.y = 1 + Math.sin(t * 1.8 + ph) * 0.015;

    // 바닥 상태 링 & 헤일로 색상/투명도
    const ringColor = {
      working: th.accent,
      speaking: th.accent,
      done: th.good,
      fail: th.critical,
      resting: "#f59e0b",
      harmony: "#10b981"
    }[st];

    this.ring.material.color.set(ringColor || "#ffffff");
    this.ring.material.opacity = ringColor
      ? (st === "working" || st === "speaking" || st === "harmony"
          ? 0.5 + Math.sin(t * 4 + ph) * 0.25
          : 0.55)
      : 0;

    // 상호 양보(harmony) 상태일 때 머리 위 은은한 헤일로 발광
    if (st === "harmony") {
      this.halo.material.opacity = 0.65 + Math.sin(t * 5 + ph) * 0.25;
      this.halo.rotation.z += delta * 0.5;
    } else {
      this.halo.material.opacity = 0;
    }

    const [L, R] = this.arms;
    const ease = (obj, key, target, k = 0.12) => { obj[key] += (target - obj[key]) * k; };
    const armTo = (a, x, z, ex) => {
      ease(a.piv.rotation, "x", x);
      ease(a.piv.rotation, "z", z);
      ease(a.elbow.rotation, "x", ex);
    };

    // 모니터 화면 발광 효과
    this.screen.material.emissiveIntensity = (st === "working")
      ? 0.95 + Math.sin(t * 9 + ph) * 0.3 + (Math.random() < 0.03 ? 0.4 : 0)
      : 0.35;

    // 상태별 디테일 모션
    if (st === "working") {
      // 키보드 타이핑 & 집중
      ease(this.head.rotation, "x", 0.28);
      ease(this.head.rotation, "y", Math.sin(t * 0.9 + ph) * 0.12);
      armTo(L, -0.95 + Math.sin(t * 13 + ph) * 0.1, 0.12, -1.0 + Math.cos(t * 11 + ph) * 0.12);
      armTo(R, -0.95 + Math.cos(t * 12.5 + ph) * 0.1, -0.12, -1.0 + Math.sin(t * 10 + ph) * 0.12);
    } else if (st === "speaking") {
      // 생동감 있는 발언 및 제스처
      ease(this.head.rotation, "x", -0.05 + Math.sin(t * 5.5) * 0.05);
      ease(this.head.rotation, "y", Math.sin(t * 1.3) * 0.2);
      armTo(L, -1.2, 0.25 + Math.sin(t * 3) * 0.1, -1.5);
      armTo(R, -0.6, -0.1, -0.9);
      this.mouth.scale.y = 1.6 + Math.sin(t * 16) * 1.2;
    } else if (st === "resting") {
      // ☕ 휴식 모드: 편안하게 등받이에 기대어 커피 머그잔 들기 & 고개 젖힘
      ease(this.head.rotation, "x", -0.15 + Math.sin(t * 0.8 + ph) * 0.05);
      ease(this.head.rotation, "y", Math.sin(t * 0.4 + ph) * 0.15);
      // 오른손으로 커피잔 들기
      armTo(L, -0.4, 0.18, -0.6);
      armTo(R, -1.4 + Math.sin(t * 1.2) * 0.08, -0.25, -1.6);
    } else if (st === "harmony") {
      // 🤝 상호 양보 모드: 정중한 끄덕임(Nodding) & 경청과 양보의 제스처 (가슴에 손)
      ease(this.head.rotation, "x", 0.18 + Math.sin(t * 2.5) * 0.15);
      ease(this.head.rotation, "y", Math.sin(t * 0.8) * 0.1);
      armTo(L, -0.6, 0.15, -0.7);
      armTo(R, -1.25, -0.45, -1.35); // 가슴에 손을 얹어 존중과 배려 표현
    } else if (st === "done") {
      // 완료: 성취감 넘치는 가벼운 손 흔들기
      ease(this.head.rotation, "x", -0.1);
      ease(this.head.rotation, "y", Math.sin(t * 0.6 + ph) * 0.1);
      armTo(L, -0.5, 0.1, -0.7);
      armTo(R, -2.6, -0.35 + Math.sin(t * 2.5) * 0.08, -0.4);
    } else if (st === "fail") {
      // 실패/조율: 비난 대신 진지하게 사색(생각하는 로댕 포즈)
      ease(this.head.rotation, "x", 0.22);
      ease(this.head.rotation, "y", 0.1);
      armTo(L, -0.7, 0.15, -0.9);
      armTo(R, -1.4, -0.3, -1.8); // 턱을 괴는 듯한 포즈
    } else {
      // idle: 평온한 대기
      ease(this.head.rotation, "x", 0.05);
      ease(this.head.rotation, "y", Math.sin(t * 0.5 + ph) * 0.25);
      armTo(L, -0.55, 0.1, -0.5);
      armTo(R, -0.55, -0.1, -0.5);
    }

    if (st !== "speaking") this.mouth.scale.y += (1 - this.mouth.scale.y) * 0.2;
  }

  dispose() {
    this.group.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) {
        if (o.material.map) o.material.map.dispose();
        o.material.dispose();
      }
    });
  }
}

// ==========================================
// 메인 크루 룸 (Virtual Office Stage)
// ==========================================
export class CrewRoom {
  constructor(container, { onSelect } = {}) {
    this.container = container;
    this.onSelect = onSelect;
    this.avatars = new Map();
    this.roleKeys = [];
    this.active = true;
    this.room = null;
    this.currentMode = "normal"; // normal | rest | harmony
    this.cameraTween = null;
    this.theme = this.readTheme();

    // 렌더러 설정
    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      powerPreference: "high-performance"
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.domElement.className = "crew-canvas";
    container.appendChild(renderer.domElement);
    this.renderer = renderer;

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(42, 1, 0.1, 120);
    this.camera.position.set(0, 5.2, 11.2);

    this.controls = new OrbitControls(this.camera, renderer.domElement);
    Object.assign(this.controls, {
      enableDamping: true,
      dampingFactor: 0.08,
      minDistance: 3.2,
      maxDistance: 22,
      maxPolarAngle: 1.48,
      minPolarAngle: 0.25,
      enablePan: true,
      autoRotate: true,
      autoRotateSpeed: 0.25
    });
    this.controls.target.set(0, 1.25, -0.6);

    this.controls.addEventListener("start", () => {
      this.controls.autoRotate = false;
      clearTimeout(this._ar);
      this._ar = setTimeout(() => { if (this.active) this.controls.autoRotate = true; }, 35000);
    });

    this.buildRoom();

    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    renderer.domElement.addEventListener("pointermove", (e) => this.pick(e, false));
    renderer.domElement.addEventListener("click", (e) => this.pick(e, true));

    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(container);
    this.resize();

    this.clock = new THREE.Clock();
    this.loop = this.loop.bind(this);
    this.raf = requestAnimationFrame(this.loop);
  }

  readTheme() {
    const v = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    const dark =
      (window.matchMedia("(prefers-color-scheme: dark)").matches && document.documentElement.dataset.theme !== "light") ||
      document.documentElement.dataset.theme === "dark";

    return {
      dark,
      bg: v("--surface-2") || (dark ? "#181a1f" : "#f4f5f8"),
      floor: dark ? "#24201c" : "#d8c7b4",
      wall: dark ? "#1a1c22" : "#f0f2f5",
      accent: v("--accent") || "#2a78d6",
      good: v("--good") || "#0ca30c",
      critical: v("--critical") || "#d03b3b",
      muted: v("--muted") || "#7a7974",
      text: v("--text") || "#0b0b0b",
      labelBg: dark ? "rgba(22,25,32,0.92)" : "rgba(255,255,255,0.95)",
      bubbleBg: dark ? "rgba(28,32,42,0.96)" : "rgba(255,255,255,0.98)",
      bubbleText: v("--text") || "#0b0b0b",
      font: '"Pretendard Variable", Pretendard, -apple-system, "Segoe UI", sans-serif'
    };
  }

  buildRoom() {
    const th = this.theme, s = this.scene;
    s.background = new THREE.Color(th.bg);
    s.fog = new THREE.Fog(new THREE.Color(th.bg), 22, 45);

    // 1. 조명 (따뜻한 실사 오피스 채광)
    this.hemi = new THREE.HemisphereLight(0xffffff, 0x8899aa, th.dark ? 0.75 : 0.95);
    s.add(this.hemi);

    // 메인 방향광 (창밖 태양 채광 & 부드러운 그림자)
    const sun = new THREE.DirectionalLight(0xfffaed, th.dark ? 1.5 : 1.9);
    sun.position.set(6, 12, 8);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.camera.near = 1;
    sun.shadow.camera.far = 40;
    Object.assign(sun.shadow.camera, { left: -16, right: 16, top: 14, bottom: -14 });
    sun.shadow.bias = -0.0004;
    s.add(sun);

    // 2. 바닥 (고급 우드 플랭크 바닥)
    this.woodTex = createWoodFloorTexture(th.dark);
    const floorMat = new THREE.MeshStandardMaterial({
      map: this.woodTex,
      roughness: 0.55,
      metalness: 0.08
    });
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(30, 20), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    s.add(floor);
    this.floor = floor;

    // 3. 메인 백월 & 대형 파노라마 통창
    const wallMat = new THREE.MeshStandardMaterial({ color: th.wall, roughness: 0.9 });
    // 좌측 벽체 (카페 배경)
    const leftWall = new THREE.Mesh(new THREE.BoxGeometry(10, 7, 0.4), wallMat);
    leftWall.position.set(-9.8, 3.5, -6.2);
    leftWall.receiveShadow = true;
    s.add(leftWall);

    // 파노라마 통유리 창틀 & 통창 (중앙~우측 라운지 배경: x = -4.5 ~ +14.5)
    const frameMat = new THREE.MeshStandardMaterial({ color: 0x1e2430, roughness: 0.4, metalness: 0.6 });
    const glassMat = new THREE.MeshPhysicalMaterial({
      color: 0xffffff,
      transparent: true,
      opacity: 0.22,
      roughness: 0.1,
      transmission: 0.9,
      ior: 1.5
    });

    // 창문 외부 스카이라인 백드롭
    this.skylineTex = createSkylineTexture(th.dark);
    const skyMat = new THREE.MeshBasicMaterial({ map: this.skylineTex });
    const skyMesh = new THREE.Mesh(new THREE.PlaneGeometry(28, 12), skyMat);
    skyMesh.position.set(4, 5.2, -7.5);
    s.add(skyMesh);

    // 창틀 프레임 구조
    const windowFrame = new THREE.Group();
    // 상/하/좌/우 외곽 프레임
    const addFrame = (w, h, d, x, y, z) => {
      const f = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), frameMat);
      f.position.set(x, y, z);
      f.castShadow = true;
      windowFrame.add(f);
      return f;
    };
    addFrame(19.5, 0.18, 0.25, 4.8, 6.9, -6.1);  // 상단
    addFrame(19.5, 0.18, 0.25, 4.8, 0.1, -6.1);  // 하단
    addFrame(0.18, 6.8, 0.25, -4.9, 3.5, -6.1);  // 좌측
    addFrame(0.18, 6.8, 0.25, 14.5, 3.5, -6.1);  // 우측
    // 중간 세로 기둥들
    [-0.1, 4.8, 9.7].forEach((xPos) => {
      addFrame(0.14, 6.8, 0.2, xPos, 3.5, -6.1);
    });
    // 유리창 페인
    const glassPane = new THREE.Mesh(new THREE.PlaneGeometry(19.2, 6.6), glassMat);
    glassPane.position.set(4.8, 3.5, -6.08);
    windowFrame.add(glassPane);
    s.add(windowFrame);

    // 천장 노출 블랙 트러스 빔들
    for (let b = 0; b < 4; b++) {
      const beam = new THREE.Mesh(new THREE.BoxGeometry(29, 0.22, 0.22), frameMat);
      beam.position.set(0, 6.9, -5.2 + b * 3.4);
      s.add(beam);
    }

    // 공간 분리용 반투명 스트라이프 유리 파티션
    this.buildGlassPartition(-4.6, -0.6); // 회의실 - 카페 사이
    this.buildGlassPartition(4.6, -0.6);  // 회의실 - 라운지 사이

    // 4. 구역별 인테리어 구축
    this.buildConferenceArea(); // 중앙 회의실
    this.buildCafeLounge();      // 좌측 카페·팬트리
    this.buildRelaxLounge();     // 우측 릴랙스 라운지
  }

  // 공간 분리 유리 파티션
  buildGlassPartition(x, z) {
    const s = this.scene;
    const part = new THREE.Group();
    const frameMat = new THREE.MeshStandardMaterial({ color: 0x1e2430, roughness: 0.3, metalness: 0.7 });
    const glassMat = new THREE.MeshPhysicalMaterial({
      color: 0xecf3f8,
      transparent: true,
      opacity: 0.35,
      roughness: 0.25,
      transmission: 0.8
    });
    // 외곽 프레임
    const border = new THREE.Mesh(new THREE.BoxGeometry(0.08, 3.4, 3.2), frameMat);
    border.position.set(0, 1.7, 0);
    // 내부 유리
    const glass = new THREE.Mesh(new THREE.BoxGeometry(0.03, 3.3, 3.1), glassMat);
    glass.position.set(0, 1.7, 0);
    part.add(border, glass);
    part.position.set(x, 0, z);
    s.add(part);
  }

  // 1) 중앙 메인 회의실 (Conference & Collaboration Zone)
  buildConferenceArea() {
    const s = this.scene;
    const tableMat = new THREE.MeshStandardMaterial({ color: 0x826445, roughness: 0.45 }); // 프리미엄 월넛 상판
    const legMat = new THREE.MeshStandardMaterial({ color: 0x22262e, metalness: 0.8, roughness: 0.3 });

    // 중앙 대형 라운드 회의 테이블 (타원/라운드 렉탱글)
    const tableTop = new THREE.Mesh(new THREE.CylinderGeometry(1.6, 1.6, 0.07, 36), tableMat);
    tableTop.scale.set(1.4, 1, 0.95);
    tableTop.position.set(0, 0.76, -0.4);
    tableTop.castShadow = tableTop.receiveShadow = true;
    s.add(tableTop);

    // 스틸 T자 베이스
    const baseCol = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.28, 0.73, 20), legMat);
    baseCol.position.set(0, 0.37, -0.4);
    baseCol.castShadow = true;
    s.add(baseCol);

    // 테이블 중앙 화분 및 회의용 옴니 마이크 바
    const micBar = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.025, 0.09), legMat);
    micBar.position.set(0, 0.81, -0.4);
    s.add(micBar);

    const potMat = new THREE.MeshStandardMaterial({ color: 0xb45309, roughness: 0.7 });
    const pot = new THREE.Mesh(new THREE.CylinderGeometry(0.15, 0.11, 0.2, 16), potMat);
    pot.position.set(0, 0.9, -0.4);
    pot.castShadow = true;
    s.add(pot);

    const plant = new THREE.Mesh(new THREE.DodecahedronGeometry(0.24, 1), new THREE.MeshStandardMaterial({ color: 0x2e7d32, roughness: 0.8 }));
    plant.position.set(0, 1.14, -0.4);
    plant.castShadow = true;
    s.add(plant);

    // 전면 대형 인터랙티브 스마트 스크린 (Whiteboard)
    this.board = new THREE.Mesh(new THREE.PlaneGeometry(4.8, 2.3), new THREE.MeshBasicMaterial({ color: 0xffffff }));
    this.board.position.set(0, 2.7, -5.94);
    s.add(this.board);

    const frame = new THREE.Mesh(new THREE.BoxGeometry(5.0, 2.5, 0.08), new THREE.MeshStandardMaterial({ color: 0x1f242d, metalness: 0.8, roughness: 0.3 }));
    frame.position.set(0, 2.7, -5.99);
    s.add(frame);

    // 천장 모던 사각 슬림 펜던트 램프
    const lamp = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.05, 0.8), new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xffffff, emissiveIntensity: 0.8 }));
    lamp.position.set(0, 4.6, -0.4);
    s.add(lamp);

    // 회의실 집중 스팟 조명
    const spot = new THREE.SpotLight(0xfffaed, 1.2, 12, Math.PI / 4, 0.4);
    spot.position.set(0, 5.0, -0.4);
    spot.target.position.set(0, 0.7, -0.4);
    s.add(spot, spot.target);

    this.updateBoard({});
  }

  // 2) 좌측 커피 & 음료 바 / 팬트리 (HERMES Cafe & Pantry)
  buildCafeLounge() {
    const s = this.scene;
    const cafeGroup = new THREE.Group();
    cafeGroup.userData.zone = "cafe";

    const woodMat = new THREE.MeshStandardMaterial({ color: 0x5a3e26, roughness: 0.6 });
    const marbleMat = new THREE.MeshStandardMaterial({ color: 0xf5f5f7, roughness: 0.25 }); // 화이트 마블
    const chromeMat = new THREE.MeshStandardMaterial({ color: 0xd8d8d8, metalness: 0.9, roughness: 0.15 });

    // L자형 카페 바 카운터
    const counterBody = new THREE.Mesh(new THREE.BoxGeometry(3.4, 1.05, 0.85), woodMat);
    counterBody.position.set(-8.2, 0.525, -0.6);
    counterBody.castShadow = counterBody.receiveShadow = true;
    const counterTop = new THREE.Mesh(new THREE.BoxGeometry(3.6, 0.08, 0.95), marbleMat);
    counterTop.position.set(-8.2, 1.09, -0.6);
    counterTop.castShadow = counterTop.receiveShadow = true;
    cafeGroup.add(counterBody, counterTop);

    // 벽면 네온 사인 "☕ HERMES CAFE"
    this.neonTex = createNeonTexture();
    const neonMesh = new THREE.Mesh(
      new THREE.PlaneGeometry(3.2, 0.88),
      new THREE.MeshBasicMaterial({ map: this.neonTex })
    );
    neonMesh.position.set(-8.2, 4.2, -5.95);
    cafeGroup.add(neonMesh);

    // 카페 뒷벽 월넛 선반 & 소품
    for (let shelf = 0; shelf < 3; shelf++) {
      const sMesh = new THREE.Mesh(new THREE.BoxGeometry(3.2, 0.05, 0.32), woodMat);
      sMesh.position.set(-8.2, 2.2 + shelf * 0.55, -5.8);
      cafeGroup.add(sMesh);

      // 선반 위 컬러풀 머그잔 및 커피 보틀 진열
      const mugColors = [0xef4444, 0x3b82f6, 0x10b981, 0xf59e0b, 0xffffff];
      for (let m = 0; m < 5; m++) {
        const mug = new THREE.Mesh(
          new THREE.CylinderGeometry(0.045, 0.04, 0.09, 12),
          new THREE.MeshStandardMaterial({ color: mugColors[m], roughness: 0.3 })
        );
        mug.position.set(-9.4 + m * 0.6, 2.28 + shelf * 0.55, -5.8);
        cafeGroup.add(mug);
      }
    }

    // 프리미엄 상업용 듀얼 에스프레소 머신
    const espressoMachine = new THREE.Group();
    espressoMachine.position.set(-8.6, 1.13, -0.6);
    const espBody = new THREE.Mesh(new THREE.BoxGeometry(0.65, 0.42, 0.45), chromeMat);
    espBody.position.y = 0.21;
    // 그룹 헤드 & 포타필터
    const groupHead1 = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.04, 0.08, 12), chromeMat);
    groupHead1.position.set(-0.14, 0.12, 0.24);
    const groupHead2 = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.04, 0.08, 12), chromeMat);
    groupHead2.position.set(0.14, 0.12, 0.24);
    // 스팀 완드
    const steamWand = new THREE.Mesh(new THREE.CylinderGeometry(0.015, 0.015, 0.18, 8), chromeMat);
    steamWand.rotation.z = 0.3;
    steamWand.position.set(0.3, 0.15, 0.24);
    espressoMachine.add(espBody, groupHead1, groupHead2, steamWand);
    espressoMachine.castShadow = true;
    cafeGroup.add(espressoMachine);

    // 원두 그라인더
    const grinder = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.11, 0.44, 16), new THREE.MeshStandardMaterial({ color: 0x1f2937, metalness: 0.6 }));
    grinder.position.set(-7.7, 1.35, -0.6);
    grinder.castShadow = true;
    cafeGroup.add(grinder);

    // 유리 도어 음료 쇼케이스 냉장고
    const fridge = new THREE.Group();
    fridge.position.set(-10.6, 0.95, -0.6);
    const fridgeBody = new THREE.Mesh(new THREE.BoxGeometry(0.9, 1.9, 0.7), new THREE.MeshStandardMaterial({ color: 0x111827, metalness: 0.8 }));
    const fridgeGlass = new THREE.Mesh(
      new THREE.PlaneGeometry(0.78, 1.6),
      new THREE.MeshPhysicalMaterial({ color: 0xffffff, transparent: true, opacity: 0.4, transmission: 0.85 })
    );
    fridgeGlass.position.set(0, 0, 0.36);
    fridge.add(fridgeBody, fridgeGlass);
    // 냉장고 내부 캔 음료 (빨강, 파랑, 노랑, 초록 캔들)
    const canColors = [0xd32f2f, 0x1976d2, 0x388e3c, 0xfbc02d];
    for (let fRow = 0; fRow < 3; fRow++) {
      for (let fCol = 0; fCol < 3; fCol++) {
        const can = new THREE.Mesh(
          new THREE.CylinderGeometry(0.04, 0.04, 0.11, 12),
          new THREE.MeshStandardMaterial({ color: canColors[(fRow + fCol) % canColors.length], metalness: 0.7, roughness: 0.3 })
        );
        can.position.set(-0.25 + fCol * 0.25, -0.4 + fRow * 0.45, 0.15);
        fridge.add(can);
      }
    }
    cafeGroup.add(fridge);

    // 바 스툴 3개 (우드 시트 + 블랙 스틸 프레임)
    const stoolMat = new THREE.MeshStandardMaterial({ color: 0x8d5b34, roughness: 0.5 });
    const stoolLegMat = new THREE.MeshStandardMaterial({ color: 0x111111, metalness: 0.8 });
    [-9.0, -8.2, -7.4].forEach((sX) => {
      const stool = new THREE.Group();
      stool.position.set(sX, 0, 0.25);
      const seat = new THREE.Mesh(new THREE.CylinderGeometry(0.18, 0.18, 0.05, 20), stoolMat);
      seat.position.y = 0.74;
      const leg1 = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.72, 10), stoolLegMat);
      leg1.position.y = 0.36;
      stool.add(seat, leg1);
      cafeGroup.add(stool);
    });

    // 웜 앰버 펜던트 조명 3개
    for (let p = 0; p < 3; p++) {
      const pLamp = new THREE.Mesh(
        new THREE.ConeGeometry(0.14, 0.18, 16),
        new THREE.MeshStandardMaterial({ color: 0xf59e0b, emissive: 0xd97706, emissiveIntensity: 0.6 })
      );
      pLamp.position.set(-9.0 + p * 0.8, 3.2, -0.6);
      pLamp.rotation.x = Math.PI;
      cafeGroup.add(pLamp);
    }

    // 카페 전용 따뜻한 포인트 라이트
    const cafeLight = new THREE.PointLight(0xffb74d, 1.4, 8);
    cafeLight.position.set(-8.2, 2.8, -0.6);
    cafeGroup.add(cafeLight);

    s.add(cafeGroup);
  }

  // 3) 우측 릴랙스 & 힐링 라운지 (Zen Rest Lounge)
  buildRelaxLounge() {
    const s = this.scene;
    const loungeGroup = new THREE.Group();
    loungeGroup.userData.zone = "lounge";

    const fabricMat = new THREE.MeshStandardMaterial({ color: 0xd5cebe, roughness: 0.85 }); // 포근한 오트밀 패브릭
    const woodMat = new THREE.MeshStandardMaterial({ color: 0x6d4c41, roughness: 0.6 });

    // 부드러운 원형 패브릭 러그 (Carpet)
    const rugMat = new THREE.MeshStandardMaterial({ color: 0xc8c2b5, roughness: 0.95 });
    const rug = new THREE.Mesh(new THREE.CircleGeometry(2.4, 32), rugMat);
    rug.rotation.x = -Math.PI / 2;
    rug.position.set(8.2, 0.012, -0.4);
    rug.receiveShadow = true;
    loungeGroup.add(rug);

    // 대형 L자형 모던 코너 패브릭 소파
    const sofaGroup = new THREE.Group();
    sofaGroup.position.set(8.4, 0, -1.2);
    // 메인 좌방석
    const mainSeat = new THREE.Mesh(new THREE.BoxGeometry(3.0, 0.35, 0.95), fabricMat);
    mainSeat.position.set(0, 0.28, 0);
    // 등받이
    const mainBack = new THREE.Mesh(new THREE.BoxGeometry(3.0, 0.55, 0.28), fabricMat);
    mainBack.position.set(0, 0.65, -0.38);
    // 사이드 팔걸이
    const armL = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.48, 0.95), fabricMat);
    armL.position.set(-1.4, 0.42, 0);
    const armR = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.48, 0.95), fabricMat);
    armR.position.set(1.4, 0.42, 0);

    // 알록달록한 포인트 쿠션들 (머스터드, 에메랄드, 번트오렌지)
    const cushionColors = [0xf59e0b, 0x10b981, 0xe11d48, 0x3b82f6];
    [-0.9, -0.3, 0.3, 0.9].forEach((cx, idx) => {
      const cushion = new THREE.Mesh(
        new THREE.BoxGeometry(0.35, 0.32, 0.12),
        new THREE.MeshStandardMaterial({ color: cushionColors[idx], roughness: 0.7 })
      );
      cushion.position.set(cx, 0.55, -0.22);
      cushion.rotation.x = -0.15;
      sofaGroup.add(cushion);
    });
    sofaGroup.add(mainSeat, mainBack, armL, armR);
    sofaGroup.castShadow = sofaGroup.receiveShadow = true;
    loungeGroup.add(sofaGroup);

    // 둥근 원목 커피 테이블 & 티 세트
    const coffeeTable = new THREE.Group();
    coffeeTable.position.set(8.2, 0, 0.45);
    const cTop = new THREE.Mesh(new THREE.CylinderGeometry(0.65, 0.65, 0.05, 32), woodMat);
    cTop.position.y = 0.42;
    for (let cLeg = 0; cLeg < 3; cLeg++) {
      const legAngle = (cLeg / 3) * Math.PI * 2;
      const leg = new THREE.Mesh(new THREE.CylinderGeometry(0.025, 0.025, 0.42, 10), woodMat);
      leg.position.set(Math.sin(legAngle) * 0.42, 0.21, Math.cos(legAngle) * 0.42);
      coffeeTable.add(leg);
    }
    // 티포트 & 찻잔
    const ceramicMat = new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.25 });
    const teapot = new THREE.Mesh(new THREE.SphereGeometry(0.08, 16, 12), ceramicMat);
    teapot.position.set(0.1, 0.5, 0.05);
    const teaCup = new THREE.Mesh(new THREE.CylinderGeometry(0.04, 0.03, 0.06, 12), ceramicMat);
    teaCup.position.set(-0.15, 0.48, 0.12);
    coffeeTable.add(cTop, teapot, teaCup);
    loungeGroup.add(coffeeTable);

    // 플랜테리어: 대형 몬스테라 / 극락조 화분
    const plantGroup = new THREE.Group();
    plantGroup.position.set(10.8, 0, -1.8);
    const whitePot = new THREE.Mesh(
      new THREE.CylinderGeometry(0.28, 0.22, 0.55, 20),
      new THREE.MeshStandardMaterial({ color: 0xf3f4f6, roughness: 0.3 })
    );
    whitePot.position.y = 0.275;
    plantGroup.add(whitePot);
    // 풍성한 나뭇잎들
    const leafMat = new THREE.MeshStandardMaterial({ color: 0x1b5e20, roughness: 0.65, side: THREE.DoubleSide });
    for (let leaf = 0; leaf < 9; leaf++) {
      const leafMesh = new THREE.Mesh(new THREE.SphereGeometry(0.32, 8, 8), leafMat);
      leafMesh.scale.set(0.9, 0.1, 1.4);
      const angle = (leaf / 9) * Math.PI * 2;
      leafMesh.position.set(Math.sin(angle) * 0.35, 0.65 + (leaf % 3) * 0.2, Math.cos(angle) * 0.35);
      leafMesh.rotation.y = angle;
      leafMesh.rotation.x = 0.4;
      plantGroup.add(leafMesh);
    }
    plantGroup.castShadow = true;
    loungeGroup.add(plantGroup);

    // 아늑한 플로어 아치 스탠드 조명 (Floor Arc Lamp)
    const lampGroup = new THREE.Group();
    lampGroup.position.set(6.2, 0, -2.0);
    const lampBase = new THREE.Mesh(new THREE.CylinderGeometry(0.22, 0.22, 0.04, 20), woodMat);
    const lampPole = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 2.2, 12), new THREE.MeshStandardMaterial({ color: 0x222222, metalness: 0.8 }));
    lampPole.position.y = 1.1;
    const lampShade = new THREE.Mesh(
      new THREE.ConeGeometry(0.24, 0.26, 20),
      new THREE.MeshStandardMaterial({ color: 0xffedd5, emissive: 0xf59e0b, emissiveIntensity: 0.6 })
    );
    lampShade.position.set(0.35, 2.1, 0.4);
    lampShade.rotation.x = Math.PI;
    lampGroup.add(lampBase, lampPole, lampShade);
    loungeGroup.add(lampGroup);

    // 라운지 은은한 웜 포인트 라이트
    const loungeLight = new THREE.PointLight(0xffecd2, 1.2, 9);
    loungeLight.position.set(8.2, 2.6, 0);
    loungeGroup.add(loungeLight);

    // 모던 원목 북셸프 (Bookshelf)
    const shelfGroup = new THREE.Group();
    shelfGroup.position.set(11.8, 1.6, 0.4);
    shelfGroup.rotation.y = -Math.PI / 2;
    const sBody = new THREE.Mesh(new THREE.BoxGeometry(2.4, 3.2, 0.4), woodMat);
    shelfGroup.add(sBody);
    // 책장에 꽂힌 다채로운 책들
    const bookColors = [0xef4444, 0x3b82f6, 0x10b981, 0x8b5cf6, 0xf59e0b];
    for (let r = 0; r < 3; r++) {
      for (let b = 0; b < 6; b++) {
        const book = new THREE.Mesh(
          new THREE.BoxGeometry(0.08, 0.26, 0.22),
          new THREE.MeshStandardMaterial({ color: bookColors[(r + b) % bookColors.length] })
        );
        book.position.set(-0.8 + b * 0.18, -0.9 + r * 0.75, 0.08);
        shelfGroup.add(book);
      }
    }
    loungeGroup.add(shelfGroup);

    s.add(loungeGroup);
  }

  // 스마트 스크린 보드 업데이트 (화합 지표 및 힐링 밸런스 위젯 통합)
  updateBoard({ repo, ticket, stage, daemon, roles = [], status = {} }) {
    const th = this.theme;
    const tex = makeTextTexture((ctx, w, h) => {
      // 보드 배경
      ctx.fillStyle = "#f8fafc";
      ctx.fillRect(0, 0, w, h);

      // 상단 프리미엄 헤더 바
      const headGrad = ctx.createLinearGradient(0, 0, w, 0);
      headGrad.addColorStop(0, "#0f172a");
      headGrad.addColorStop(1, "#1e3a8a");
      ctx.fillStyle = headGrad;
      ctx.fillRect(0, 0, w, 90);

      ctx.fillStyle = "#ffffff";
      ctx.font = "700 44px " + th.font;
      ctx.textBaseline = "middle";
      ctx.fillText(repo ? `⚡ ${repo} — Hermes Crew Space` : "Hermes Fleet — Virtual Crew Space", 36, 45);

      // 우측 상단 모드/화합 인디케이터
      ctx.font = "600 24px " + th.font;
      ctx.textAlign = "right";
      const modeLabels = {
        normal: "💼 협업 회의 모드",
        rest: "☕ 힐링 & 리프레시 모드",
        harmony: "🤝 상호 양보 & 화합 모드"
      };
      ctx.fillStyle = "#38bdf8";
      ctx.fillText(modeLabels[this.currentMode] || "💼 협업 모드", w - 36, 45);

      // 본문 좌측: 현재 프로젝트 & 티켓 진행 상황
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      ctx.font = "600 28px " + th.font;
      ctx.fillStyle = "#1e293b";
      ctx.fillText("📌 실시간 진행 현황", 36, 115);

      ctx.font = "400 24px " + th.font;
      ctx.fillStyle = "#475569";
      const lines = [];
      if (ticket) lines.push(`• 작업 티켓: ${ticket}`);
      if (stage) {
        const stageKo = { pm: "PRD 기획", design: "디자인 스펙", dev: "코드 구현", qa: "QA 자동 검증", discover: "티켓 발굴", merge: "자동 머지" }[stage] || stage;
        lines.push(`• 현재 단계: ${stageKo}`);
      }
      if (daemon) lines.push(`• 데몬 상태: ${daemon}`);
      if (!lines.length) lines.push(repo ? "• 현재 대기 중인 티켓을 탐색하고 있습니다." : "• 대상 레포지토리를 선택해주세요.");
      lines.forEach((ln, i) => ctx.fillText(ln, 36, 160 + i * 38));

      // 본문 우측: 웰니스 & 상호 양보 지표 (Harmony & Rest Metric Card)
      ctx.fillStyle = "#f1f5f9";
      roundRect(ctx, w - 480, 110, 444, 150, 16);
      ctx.fill();
      ctx.strokeStyle = "#e2e8f0";
      ctx.lineWidth = 2;
      roundRect(ctx, w - 480, 110, 444, 150, 16);
      ctx.stroke();

      ctx.fillStyle = "#0f172a";
      ctx.font = "600 22px " + th.font;
      ctx.fillText("🌿 크루 웰니스 & 양보 지표", w - 460, 126);

      ctx.font = "500 20px " + th.font;
      ctx.fillStyle = "#059669";
      ctx.fillText("🤝 상호 양보율: 100% (원활한 합의 완료)", w - 460, 168);
      ctx.fillStyle = "#d97706";
      ctx.fillText("☕ 리프레시 밸런스: 최상 (번아웃 방지)", w - 460, 206);

      // 하단: 각 역할별 상태 배지
      ctx.fillStyle = "#e2e8f0";
      ctx.fillRect(36, 280, w - 72, 2);

      ctx.font = "600 22px " + th.font;
      ctx.fillStyle = "#334155";
      ctx.fillText("👥 에이전트 크루 상태", 36, 298);

      roles.forEach((r, i) => {
        const st = (status[r.key] || {}).state || "idle";
        const col = {
          working: "#2563eb",
          done: "#16a34a",
          fail: "#dc2626",
          resting: "#d97706",
          harmony: "#059669"
        }[st] || "#94a3b8";

        const colIdx = i % 3;
        const rowIdx = Math.floor(i / 3);
        const bx = 36 + colIdx * 380;
        const by = 340 + rowIdx * 56;

        ctx.fillStyle = "#ffffff";
        roundRect(ctx, bx, by, 350, 44, 10);
        ctx.fill();
        ctx.strokeStyle = "#e2e8f0";
        ctx.lineWidth = 1.5;
        roundRect(ctx, bx, by, 350, 44, 10);
        ctx.stroke();

        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.arc(bx + 22, by + 22, 10, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = "#0f172a";
        ctx.font = "600 20px " + th.font;
        const stText = {
          working: "작업 중",
          done: "완료",
          fail: "검토",
          resting: "휴식 중 ☕",
          harmony: "양보 🤝",
          idle: "대기"
        }[st] || "대기";
        ctx.fillText(`${r.name_ko}: ${stText}`, bx + 42, by + 12);
      });
    }, 1200, 520);

    if (this.board.material.map) this.board.material.map.dispose();
    this.board.material.map = tex;
    this.board.material.needsUpdate = true;
  }

  // 룸 데이터 설정 및 아바타 배치
  setRoom(room) {
    this.room = room;
    const roles = room.roles || [];
    const keys = roles.map((r) => r.key).join("|");

    if (keys !== this.roleKeys.join("|")) {
      for (const a of this.avatars.values()) {
        this.scene.remove(a.group);
        a.dispose();
      }
      this.avatars.clear();

      // 아바타들을 중앙 회의 테이블 둘레에 반원형으로 자연스럽게 착석 배치
      const n = roles.length;
      const radius = 3.9;
      const spread = Math.min(150, 50 + n * 24);

      roles.forEach((r, i) => {
        const a = new Avatar(r, i, this.theme);
        const ang = (n === 1 ? 0 : -spread / 2 + (spread / (n - 1)) * i) * DEG;
        a.group.position.set(Math.sin(ang) * radius, 0, -0.4 - Math.cos(ang) * radius * 0.76);
        // 중앙 회의 테이블을 바라봄
        a.group.rotation.y = Math.atan2(-a.group.position.x, -(a.group.position.z + 0.4)) + Math.PI;
        this.scene.add(a.group);
        this.avatars.set(r.key, a);
      });
      this.roleKeys = roles.map((r) => r.key);
    }

    for (const r of roles) {
      const a = this.avatars.get(r.key);
      const st = (room.status || {})[r.key] || {};
      a.role = r;
      if (this.currentMode === "normal") {
        a.setState(st.state || "idle");
      }
    }

    this.updateBoard({
      repo: room.name,
      ticket: room.ticket,
      stage: room.stage,
      daemon: room.daemonText,
      roles,
      status: room.status || {}
    });
  }

  // 카메라 뷰 프리셋 이동 (부드러운 Tween 보간)
  setCameraPreset(name) {
    const presets = {
      overview: { pos: new THREE.Vector3(0, 5.4, 11.5), target: new THREE.Vector3(0, 1.25, -0.6) },
      meeting: { pos: new THREE.Vector3(0, 3.4, 6.2), target: new THREE.Vector3(0, 1.1, -0.4) },
      cafe: { pos: new THREE.Vector3(-8.2, 3.2, 5.0), target: new THREE.Vector3(-8.2, 1.2, -0.6) },
      lounge: { pos: new THREE.Vector3(8.2, 3.2, 5.0), target: new THREE.Vector3(8.2, 1.2, -0.4) }
    };
    const targetConfig = presets[name] || presets.overview;
    this.controls.autoRotate = false;
    this.cameraTween = {
      startPos: this.camera.position.clone(),
      endPos: targetConfig.pos,
      startTarget: this.controls.target.clone(),
      endTarget: targetConfig.target,
      progress: 0,
      duration: 1.2 // 초
    };
  }

  // 휴식 모드 (Coffee Break) 트리거
  triggerRestMode() {
    this.currentMode = this.currentMode === "rest" ? "normal" : "rest";
    for (const [key, a] of this.avatars.entries()) {
      if (this.currentMode === "rest") {
        a.setState("resting");
        const quote = ROLE_REST_QUOTES[key] || "커피 한 잔의 여유로 마음을 가다듬고 있습니다 ☕";
        a.say(quote, 14000);
      } else {
        const origSt = ((this.room && this.room.status) || {})[key]?.state || "idle";
        a.setState(origSt);
      }
    }
    if (this.currentMode === "rest") this.setCameraPreset("cafe");
    else this.setCameraPreset("meeting");
    if (this.room) this.setRoom(this.room);
  }

  // 상호 양보 & 화합 모드 (Harmony & Concession) 트리거
  triggerHarmonyMode() {
    this.currentMode = this.currentMode === "harmony" ? "normal" : "harmony";
    for (const [key, a] of this.avatars.entries()) {
      if (this.currentMode === "harmony") {
        a.setState("harmony");
        const quote = ROLE_HARMONY_QUOTES[key] || "동료의 의견을 깊이 존중하며 한 걸음 양보합니다 🤝";
        a.say(quote, 14000);
      } else {
        const origSt = ((this.room && this.room.status) || {})[key]?.state || "idle";
        a.setState(origSt);
      }
    }
    if (this.currentMode === "harmony") this.setCameraPreset("meeting");
    if (this.room) this.setRoom(this.room);
  }

  say(key, text, ms) {
    const a = this.avatars.get(key);
    if (a) a.say(text, ms);
  }

  setActive(on) {
    this.active = on;
    if (on && !this.raf) this.raf = requestAnimationFrame(this.loop);
  }

  refreshTheme() {
    this.theme = this.readTheme();
    const th = this.theme;
    this.scene.background.set(th.bg);
    this.scene.fog.color.set(th.bg);
    this.hemi.intensity = th.dark ? 0.75 : 0.95;

    // 바닥 및 벽 재질 업데이트
    if (this.woodTex) this.woodTex.dispose();
    this.woodTex = createWoodFloorTexture(th.dark);
    this.floor.material.map = this.woodTex;
    this.floor.material.needsUpdate = true;

    if (this.skylineTex) this.skylineTex.dispose();
    this.skylineTex = createSkylineTexture(th.dark);

    for (const a of this.avatars.values()) {
      a.theme = th;
      a.updateLabel();
    }
    if (this.room) {
      this.updateBoard({
        repo: this.room.name,
        ticket: this.room.ticket,
        stage: this.room.stage,
        daemon: this.room.daemonText,
        roles: this.room.roles,
        status: this.room.status
      });
    }
  }

  pick(e, click) {
    const rect = this.renderer.domElement.getBoundingClientRect();
    this.pointer.set(((e.clientX - rect.left) / rect.width) * 2 - 1, -((e.clientY - rect.top) / rect.height) * 2 + 1);
    this.raycaster.setFromCamera(this.pointer, this.camera);

    // 1. 아바타 클릭 감지
    const hits = this.raycaster.intersectObjects([...this.avatars.values()].map((a) => a.group), true);
    const key = hits.length ? hits[0].object.userData.roleKey : null;

    if (key) {
      this.renderer.domElement.style.cursor = "pointer";
      if (click && this.onSelect) this.onSelect(key);
      return;
    }

    // 2. 가상 오피스 구역 클릭 감지
    if (click) {
      const allHits = this.raycaster.intersectObjects(this.scene.children, true);
      for (const h of allHits) {
        let cur = h.object;
        while (cur) {
          if (cur.userData && cur.userData.zone) {
            this.setCameraPreset(cur.userData.zone);
            return;
          }
          cur = cur.parent;
        }
      }
    }

    this.renderer.domElement.style.cursor = "grab";
  }

  resize() {
    const w = this.container.clientWidth || 600, h = this.container.clientHeight || 480;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  loop() {
    this.raf = null;
    if (!this.active || !this.container.isConnected) return;
    const delta = Math.min(this.clock.getDelta(), 0.1);
    const t = this.clock.getElapsedTime(), now = performance.now();

    // 부드러운 카메라 보간 (Tween)
    if (this.cameraTween) {
      this.cameraTween.progress += delta / this.cameraTween.duration;
      const p = Math.min(this.cameraTween.progress, 1);
      const ease = 0.5 - Math.cos(p * Math.PI) / 2; // Sine ease-in-out
      this.camera.position.lerpVectors(this.cameraTween.startPos, this.cameraTween.endPos, ease);
      this.controls.target.lerpVectors(this.cameraTween.startTarget, this.cameraTween.endTarget, ease);
      if (p >= 1) this.cameraTween = null;
    }

    this.controls.update();
    for (const a of this.avatars.values()) a.animate(t, now, delta);
    this.renderer.render(this.scene, this.camera);
    this.raf = requestAnimationFrame(this.loop);
  }

  destroy() {
    this.active = false;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.ro.disconnect();
    for (const a of this.avatars.values()) a.dispose();
    this.controls.dispose();
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

window.CrewRoom = CrewRoom;
window.dispatchEvent(new Event("crewroom-ready"));
