---
id: HX-004
title: 프런트엔드 주요 화면에서 브라우저 세로 스크롤 발생 및 바닥 빈 공간 문제 개선
priority: P1
status: todo
area: frontend
stages: [pm, design, dev, qa]
acceptance:
  - 1920×1080 뷰포트에서 주요 화면(개요, 레포 상세, 티켓 상세 등)의 document.scrollHeight가 window.innerHeight를 초과하지 않도록 조정한다.
  - 바닥에 80px 이상의 빈 띠가 남지 않도록 카드 및 레이아웃을 조정한다.
  - 긴 목록, 로그, 표는 내부 스크롤 영역으로 분리해 페이지 세로 스크롤이 생기지 않도록 한다.
  - qa/fe_guide_check.js 또는 Playwright 측정으로 세로 스크롤 없음과 바닥 빈 공간 조건을 검증한다.
---
## 배경

hermes_fleet/monitor/static/app.js (lines 300-400, 600-700, 1300-1380), style.css (lines 200-350)에서 주요 화면 렌더링과 레이아웃 스타일을 확인. 현재 주요 화면에서 document.scrollHeight가 window.innerHeight를 초과해 브라우저 세로 스크롤이 발생하고, 카드 하단에 빈 공간이 과도하게 남는 문제 발견됨. 이는 FE 가이드의 화면 품질 기준 위반임.

## 범위

- 주요 화면(overview, repo, tickets 등) 렌더링 함수에서 레이아웃 높이와 카드 높이 조정
- 긴 목록과 로그 영역에 내부 스크롤 적용
- style.css에서 카드 및 레이아웃 관련 CSS 조정
- qa/fe_guide_check.js를 활용해 수정 후 검증 자동화

## 참고 파일

- hermes_fleet/monitor/static/app.js
- hermes_fleet/monitor/static/style.css
- qa/fe_guide_check.js

## 출처

Tech Lead 에이전트 제안. 사람이 검토 후 `hermes-fleet approve <repo> <ID>`로 승인한다.
