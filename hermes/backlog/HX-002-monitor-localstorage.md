---
id: HX-002
title: Monitor 앱에서의 localStorage 오류 처리 개선
priority: P1
status: todo
area: frontend
stages: [dev, qa]
acceptance:
  - localStorage 오류가 발생할 때 토스트 메시지가 제대로 표시되어야 함
  - localStorage가 없는 경우에도 앱이 정상적으로 동작해야 함
---
## 배경

hermes_fleet/monitor/static/app.js:67, 177, 184, 209, 212

## 범위

- localStorage 오류 처리 로직을 try-catch 블록으로 감싸서 예외를 무시하도록 수정
- localStorage가 없는 경우에도 앱이 정상적으로 동작하도록 기본값 설정

## 참고 파일

- hermes_fleet/monitor/static/app.js

## 출처

Tech Lead 에이전트 제안. 사람이 검토 후 `hermes-fleet approve <repo> <ID>`로 승인한다.
