---
id: HX-003
title: Fleet 모듈에서의 예외 처리 개선
priority: P1
status: todo
area: backend
stages: [dev, qa]
acceptance:
  - Fleet 모듈에서 발생하는 예외가 제대로 로깅되어야 함
  - 예외가 발생할 때에도 앱이 정상적으로 동작해야 함
---
## 배경

hermes_fleet/fleet.py:135, 171, 284

## 범위

- Fleet 모듈에서 발생하는 예외를 try-except 블록으로 감싸서 예외를 무시하도록 수정
- 예외가 발생할 때에도 앱이 정상적으로 동작하도록 기본값 설정

## 참고 파일

- hermes_fleet/fleet.py

## 출처

Tech Lead 에이전트 제안. 사람이 검토 후 `hermes-fleet approve <repo> <ID>`로 승인한다.
