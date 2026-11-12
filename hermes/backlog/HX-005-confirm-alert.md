---
id: HX-005
title: 프런트엔드 모달 팝업이 뷰포트 정중앙에 위치하지 않는 문제 해결 및 confirm/alert 대체
priority: P1
status: todo
area: frontend
stages: [pm, design, dev, qa]
acceptance:
  - "모든 중요 입력 및 상세 출력 모달이 뷰포트 가로·세로 정중앙에 position: fixed; inset: 0; margin: auto로 위치하도록 수정한다."
  - 브라우저 기본 confirm/alert 대신 커스텀 모달 컴포넌트를 사용하도록 변경한다.
  - 모달 열림 시 포커스 이동과 포커스 트랩이 정상 작동하는지 확인한다.
  - qa/fe_guide_check.js 또는 Playwright 측정으로 모달 위치와 동작을 검증한다.
---
## 배경

hermes_fleet/monitor/static/app.js (lines 300-350, 800-870)에서 모달 관련 함수(openDialog, confirmModal 등)와 티켓 상세 모달 구현을 확인. 현재 모달이 상단 고정 또는 바닥 시트 형태로 표시되거나 브라우저 기본 confirm/alert를 사용하는 부분이 있음. FE 가이드에 따라 정중앙 모달과 커스텀 모달 사용이 필요함.

## 범위

- 모달 컴포넌트(openDialog 등) 위치 스타일을 뷰포트 정중앙으로 수정
- 브라우저 기본 confirm/alert 호출 부분을 커스텀 모달로 교체
- 모달 열림 시 포커스 이동 및 포커스 트랩 구현 보완
- 관련 테스트 및 qa/fe_guide_check.js로 검증 자동화

## 참고 파일

- hermes_fleet/monitor/static/app.js
- hermes_fleet/monitor/static/style.css
- qa/fe_guide_check.js

## 출처

Tech Lead 에이전트 제안. 사람이 검토 후 `hermes-fleet approve <repo> <ID>`로 승인한다.
