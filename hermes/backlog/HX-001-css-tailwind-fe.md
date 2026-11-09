---
id: HX-001
title: 모니터 CSS 를 Tailwind 유틸리티로 전면 전환 (FE 가이드 준수)
priority: P2
status: blocked
area: frontend
stages: [pm, design, dev, qa]
acceptance:
  - hermes_fleet/monitor/static/style.css 의 레이아웃·컴포넌트 규칙(카드, 타일, 배지, 버튼, 폼, 로그, 워크플로 보드, 달력, 크루 룸)이 index.html/app.js 의 Tailwind 유틸리티 클래스로 옮겨지고 style.css 에는 CSS 변수 토큰, 다크 모드 토큰, 키프레임, Tailwind 로 표현할 수 없는 규칙만 남는다
  - "@theme 매핑(index.html 의 text/tailwindcss 블록)으로 기존 색·간격 토큰을 재사용하고 새 색상 값을 만들지 않는다"
  - 1920×1080 에서 모든 탭의 document.scrollHeight ≤ innerHeight 가 유지되고 라이트/다크 모드 모두 시각 회귀가 없다 (Playwright 스크린샷 비교)
  - Tailwind CDN 로드 실패 시 html.no-tw 폴백으로 페이지가 깨지지 않는다
---
## 배경
FE 가이드는 새 화면·컴포넌트 스타일을 Tailwind 유틸리티로 쓰도록 한다. 모니터는 Tailwind 도입 전에 작성된 수제 CSS(style.css 약 450줄)가 대부분이라, 셸(햄버거+오프캔버스 LNB)만 Tailwind 로 되어 있다. 가이드의 '전면 교체는 별도 티켓' 규칙에 따라 분리한 작업이다.

## 범위
- style.css 의 컴포넌트 규칙을 Tailwind 유틸리티(또는 @apply 가 아닌 유틸리티 조합)로 치환
- AG Grid/ApexCharts 테마 파라미터는 그대로 CSS 변수에서 읽는다
- 반응형 분기(1366~1920, 모바일)는 Tailwind 브레이크포인트로 옮긴다

## 제외
- Three.js 크루 룸 캔버스 내부(스프라이트 텍스처) 스타일
