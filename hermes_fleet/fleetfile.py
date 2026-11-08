"""fleet.yaml 의 include/exclude 를 주석을 보존하며 편집한다(레포 선택/해제).

규칙: exclude 가 항상 우선. include 가 비어 있지 않으면 그 안의 레포만 대상.
- 선택(selected=True): exclude 에서 제거하고, include 가 비어 있지 않으면 include 에 추가한다.
- 해제(selected=False): exclude 에 추가한다(include 는 건드리지 않는다 — include 를 비우면 '전체'가 되어 버리므로).
텍스트 수준에서 해당 키 블록만 고치므로 다른 줄·주석은 그대로 남는다. 결과는 YAML 로 다시 읽어 검증한 뒤에만 저장한다.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_ITEM = re.compile(r"^(\s*)-\s+(.+?)\s*$")
_INLINE = re.compile(r"^(\[.*?\])\s*(#.*)?$")


class FleetFileError(ValueError):
    pass


def is_selected(name: str, include: list[str] | None, exclude: list[str] | None) -> bool:
    return name not in (exclude or []) and (not include or name in include)


def _key_line(key: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(key)}:(.*)$")


def _item_value(line: str) -> str | None:
    m = _ITEM.match(line)
    if not m:
        return None
    value = m.group(2)
    value = value.split(" #", 1)[0].strip() if not value.startswith(("'", '"')) else value
    try:
        loaded = yaml.safe_load(value)
    except yaml.YAMLError:
        return value
    return str(loaded) if loaded is not None else value


def _find_block(lines: list[str], key: str) -> tuple[int, list[int], str] | None:
    """(key 줄 인덱스, 항목 줄 인덱스들, 인라인 값) — key 가 없으면 None."""
    pat = _key_line(key)
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        rest = m.group(1).strip()
        items = []
        j = i + 1
        while j < len(lines) and lines[j][:1].isspace() and _ITEM.match(lines[j]):
            items.append(j)
            j += 1
        return i, items, rest
    return None


def _set_membership(text: str, key: str, name: str, present: bool) -> str:
    lines = text.splitlines()
    block = _find_block(lines, key)
    if block is None:
        if not present:
            return text
        sep = "" if not lines or lines[-1].strip() == "" else "\n"
        return text.rstrip("\n") + f"\n{sep}{key}:\n  - {name}\n"
    key_idx, item_idx, rest = block
    comment = ""
    inline_items: list[str] | None = None
    if rest:
        m = _INLINE.match(rest)
        if m:
            loaded = yaml.safe_load(m.group(1)) or []
            inline_items = [str(x) for x in loaded]
            comment = (" " + m.group(2)) if m.group(2) else ""
        elif rest.startswith("#"):
            comment = " " + rest
        else:
            raise FleetFileError(f"{key}: 지원하지 않는 형식입니다: {rest!r}")

    if inline_items is not None:
        current = inline_items
        if present and name not in current:
            current = current + [name]
        elif not present and name in current:
            current = [x for x in current if x != name]
        else:
            return text
        if current:
            new = [f"{key}:{comment}"] + [f"  - {x}" for x in current]
        else:
            new = [f"{key}: []{comment}"]
        lines[key_idx : key_idx + 1] = new
        return "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    values = {idx: _item_value(lines[idx]) for idx in item_idx}
    if present:
        if name in values.values():
            return text
        indent = _ITEM.match(lines[item_idx[0]]).group(1) if item_idx else "  "
        insert_at = (item_idx[-1] + 1) if item_idx else key_idx + 1
        lines.insert(insert_at, f"{indent}- {name}")
    else:
        hits = [idx for idx, v in values.items() if v == name]
        if not hits:
            return text
        for idx in sorted(hits, reverse=True):
            del lines[idx]
        if len(item_idx) == len(hits):   # 비게 되면 인라인 빈 목록으로
            lines[key_idx] = f"{key}: []{comment}"
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def toggle_text(text: str, name: str, selected: bool) -> str:
    data = yaml.safe_load(text) or {}
    include = [str(x) for x in (data.get("include") or [])]
    if selected:
        text = _set_membership(text, "exclude", name, present=False)
        if include and name not in include:
            text = _set_membership(text, "include", name, present=True)
    else:
        text = _set_membership(text, "exclude", name, present=True)
    try:
        new = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise FleetFileError(f"편집 결과가 유효한 YAML 이 아닙니다: {exc}") from exc
    for key in ("include", "exclude"):
        if new.get(key) is not None and not isinstance(new.get(key), list):
            raise FleetFileError(f"{key} 가 목록이 아닙니다")
    if is_selected(name, new.get("include"), new.get("exclude")) != selected:
        raise FleetFileError("편집 후에도 선택 상태가 바뀌지 않았습니다")
    return text


def set_repo_selected(path: Path, name: str, selected: bool) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise FleetFileError(f"잘못된 레포 이름: {name!r}")
    text = path.read_text(encoding="utf-8")
    new_text = toggle_text(text, name, selected)
    changed = new_text != text
    if changed:
        with path.open("w", encoding="utf-8") as fh:   # 제자리 쓰기(바인드 마운트 inode 유지)
            fh.write(new_text)
    data = yaml.safe_load(new_text) or {}
    return {"name": name, "selected": selected, "changed": changed, "include": data.get("include") or [], "exclude": data.get("exclude") or []}


# ---------- 최상위 스칼라 키 (스케줄 등) ----------

_SCALAR_LINE = re.compile(r"^([A-Za-z_][\w-]*):([ \t]*)([^#\n]*?)([ \t]*#.*)?$")
SCHEDULE_KEYS = ("active_hours", "merge_at", "no_new_work_minutes_before_end", "poll_seconds", "endpoint_retry_seconds")


def _yaml_scalar(value: Any) -> str:
    if value is None or value == "":
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    return '"' + text.replace('"', '\\"') + '"' if (":" in text or text[:1] in "'\"[{#&*!|>%@`" or text.strip() != text) else text


def set_scalar(text: str, key: str, value: Any) -> str:
    """최상위 `key: value` 줄의 값만 바꾼다(끝 주석 보존). 없으면 끝에 추가."""
    lines = text.splitlines()
    rendered = _yaml_scalar(value)
    for i, line in enumerate(lines):
        m = _SCALAR_LINE.match(line)
        if m and m.group(1) == key:
            lines[i] = f"{key}:{m.group(2) or ' '}{rendered}{m.group(4) or ''}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    return text.rstrip("\n") + f"\n{key}: {rendered}\n"


def update_schedule(path: Path, **fields: Any) -> dict[str, Any]:
    """active_hours / merge_at / no_new_work_minutes_before_end / poll_seconds / endpoint_retry_seconds 를 검증 후 제자리 저장."""
    from .schedule import parse_clock, parse_window  # noqa: WPS433

    unknown = [k for k in fields if k not in SCHEDULE_KEYS]
    if unknown:
        raise FleetFileError(f"지원하지 않는 설정 키: {unknown}")
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "active_hours":
            value = (value or "").strip()
            parse_window(value or None)                      # 형식 검증 (빈 값 = 항상)
            clean[key] = value
        elif key == "merge_at":
            value = (value or "").strip()
            if value:
                t = parse_clock(value)
                if t is None:
                    raise FleetFileError("merge_at 형식은 HH:MM")
                value = f"{t.hour:02d}:{t.minute:02d}"
            clean[key] = value
        else:
            try:
                n = int(value)
            except (TypeError, ValueError) as exc:
                raise FleetFileError(f"{key} 는 정수여야 합니다") from exc
            lo = 0 if key == "no_new_work_minutes_before_end" else 10
            if n < lo or n > 86400:
                raise FleetFileError(f"{key} 범위 벗어남 ({lo}~86400)")
            clean[key] = n
    text = path.read_text(encoding="utf-8")
    new_text = text
    for key, value in clean.items():
        new_text = set_scalar(new_text, key, value)
    try:
        data = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:
        raise FleetFileError(f"편집 결과가 유효한 YAML 이 아닙니다: {exc}") from exc
    for key, value in clean.items():
        got = data.get(key)
        if (got or "") != (value or "") and got != value:
            raise FleetFileError(f"{key} 저장 결과가 일치하지 않습니다: {got!r} != {value!r}")
    if new_text != text:
        with path.open("w", encoding="utf-8") as fh:
            fh.write(new_text)
    return {"changed": new_text != text, **{k: data.get(k) for k in SCHEDULE_KEYS}}


# ---------- 날짜별 시간대(calendar 블록) ----------

_TOPLEVEL = re.compile(r"^[A-Za-z_][\w-]*:")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _calendar_block(lines: list[str]) -> tuple[int, int] | None:
    """`calendar:` 최상위 키 줄과 그 블록 끝(다음 최상위 키 직전) 인덱스. 없으면 None."""
    for i, line in enumerate(lines):
        if line.startswith("calendar:"):
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j][:1].isspace() or lines[j].lstrip().startswith("#")):
                j += 1
            # 블록 끝의 빈 줄/주석은 블록 밖으로 돌려준다
            while j - 1 > i and (not lines[j - 1].strip() or lines[j - 1].lstrip().startswith("#") and not lines[j - 1][:1].isspace()):
                j -= 1
            return i, j
    return None


def update_calendar(path: Path, entries: dict[str, Any]) -> dict[str, str]:
    """calendar 블록을 통째로 다시 쓴다. entries: {date: 'HH:MM-HH:MM' | 'off' | None(삭제)}. 결과 = 저장된 calendar(spec 문자열)."""
    from .schedule import OFF, parse_window  # noqa: WPS433

    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise FleetFileError(f"fleet.yaml 을 읽을 수 없습니다: {exc}") from exc
    current: dict[str, str] = {str(k): ("off" if v is None or v is False else str(v).strip()) for k, v in (data.get("calendar") or {}).items()}
    for key, value in entries.items():
        day = str(key).strip()
        if not _DATE.match(day):
            raise FleetFileError(f"날짜 형식은 YYYY-MM-DD: {day!r}")
        try:
            import datetime as _dt
            _dt.date.fromisoformat(day)
        except ValueError as exc:
            raise FleetFileError(f"유효하지 않은 날짜: {day}") from exc
        if value is None or str(value).strip() == "":
            current.pop(day, None)
            continue
        spec = str(value).strip()
        if spec.lower() in ("off", "쉼", "휴무", "none", "x", "-"):
            current[day] = OFF
        else:
            w = parse_window(spec)
            current[day] = f"{w[0].hour:02d}:{w[0].minute:02d}-{w[1].hour:02d}:{w[1].minute:02d}"
    block = ["calendar:"] + [f'  "{d}": {"off" if v == OFF else chr(34) + v + chr(34)}' for d, v in sorted(current.items())]
    if not current:
        block = ["calendar: {}"]
    lines = text.splitlines()
    found = _calendar_block(lines)
    if found:
        i, j = found
        lines[i:j] = block
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["# 날짜별 작동 시간대 예외(웹 모니터 달력에서 편집). 값은 'HH:MM-HH:MM' 또는 off(그 날은 쉼). 없는 날은 active_hours 를 따른다."] + block
    new_text = "\n".join(lines) + "\n"
    try:
        check = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:
        raise FleetFileError(f"편집 결과가 유효한 YAML 이 아닙니다: {exc}") from exc
    saved = {str(k): ("off" if v is None or v is False else str(v)) for k, v in (check.get("calendar") or {}).items()}
    if saved != current:
        raise FleetFileError(f"calendar 저장 결과가 일치하지 않습니다: {saved!r} != {current!r}")
    if new_text != text:
        with path.open("w", encoding="utf-8") as fh:
            fh.write(new_text)
    return current


# ---------- 사람 승인 모드(approval 블록) ----------

def _top_block(lines: list[str], key: str) -> tuple[int, int] | None:
    for i, line in enumerate(lines):
        if line.startswith(key + ":"):
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j][:1].isspace() or lines[j].lstrip().startswith("#")):
                j += 1
            while j - 1 > i and (not lines[j - 1].strip() or lines[j - 1].lstrip().startswith("#") and not lines[j - 1][:1].isspace()):
                j -= 1
            return i, j
    return None


def update_approval(path: Path, default: str | None = None, repos: dict[str, str | None] | None = None) -> dict[str, Any]:
    """approval 블록을 다시 쓴다. default: auto|manual, repos: {name: auto|manual|None(예외 삭제)}. 결과 = 저장된 approval."""
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise FleetFileError(f"fleet.yaml 을 읽을 수 없습니다: {exc}") from exc
    cur = data.get("approval") or {}
    if isinstance(cur, str):
        cur = {"default": cur}
    cur_default = str(cur.get("default") or "auto").lower()
    cur_repos = {str(k): str(v).lower() for k, v in (cur.get("repos") or {}).items()}
    valid = ("auto", "manual")
    if default is not None:
        d = str(default).strip().lower()
        if d not in valid:
            raise FleetFileError("approval.default 는 auto|manual")
        cur_default = d
    for name, mode in (repos or {}).items():
        if mode is None or str(mode).strip().lower() in ("", "default"):
            cur_repos.pop(str(name), None)
            continue
        m = str(mode).strip().lower()
        if m not in valid:
            raise FleetFileError(f"approval 모드는 auto|manual: {name}={mode!r}")
        cur_repos[str(name)] = m
    block = ["approval:", f"  default: {cur_default}"] + (["  repos:"] + [f'    "{k}": {v}' for k, v in sorted(cur_repos.items())] if cur_repos else ["  repos: {}"])
    lines = text.splitlines()
    found = _top_block(lines, "approval")
    if found:
        i, j = found
        lines[i:j] = block
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["# 사람 승인 모드: auto = 제안이 바로 todo(승인 불필요) / manual = proposed 로 두고 사람이 승인. 웹 모니터에서 편집."] + block
    new_text = "\n".join(lines) + "\n"
    try:
        check = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:
        raise FleetFileError(f"편집 결과가 유효한 YAML 이 아닙니다: {exc}") from exc
    saved = check.get("approval") or {}
    if str(saved.get("default")) != cur_default or {str(k): str(v) for k, v in (saved.get("repos") or {}).items()} != cur_repos:
        raise FleetFileError("approval 저장 결과가 일치하지 않습니다")
    if new_text != text:
        with path.open("w", encoding="utf-8") as fh:
            fh.write(new_text)
    return {"default": cur_default, "repos": cur_repos}
