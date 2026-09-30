# -*- coding: utf-8 -*-
"""Импорт сессии из папки выгрузки: файлы → строки ленты. Базу НЕ трогает — только читает и строит.

ЗАЧЕМ. Вторая половина `Vision09`: выгрузка задумана правимой руками и возвращаемой обратно
(владелец, 2026-09-29: «сделать импорт сессии из папки с перезаписью текущей сессии»). Запись в
базу — не здесь, а в `_command.run_import`, тем же `commit_replacement`, что у всех операций.

ПОРЯДОК — НОМЕР ФАЙЛА (владелец: «главный признак последовательности это номера файлов»). Снимок
выборки (`_export.export(snapshot=True)`) кладёт номер позиции в ленте первым полем имени:

    007__2026.09.22-23.45.10__m000d__assistant.md

Файлы без номера не угадываются по времени — это ошибка папки, и она называется вслух.

ЧТО В ФАЙЛЕ — ровно то, что пишет `_export.render`, половины через разделитель v2:
  user / system / прочее — весь текст и есть сообщение;
  assistant             — [проза] + вызовы, каждый половиной: имя тула первой строкой, ниже JSON
                          аргументов (или сырая строка, если аргументы не разбирались);
  tool                  — [свой вызов] + тело результата. Вызов едет в файле результата потому,
                          что у вызова нет своего адреса (правило владения).

ПАРЫ. `tool_call_id` в файлах нет вовсе — его и не должно быть в правимом тексте. Он выдаётся
заново: каждому вызову реплики — свой, а результат берёт тот вызов БЛИЖАЙШЕЙ ПРЕДЫДУЩЕЙ реплики,
чьи имя и аргументы совпадают с вызовом в его файле. ВАЛИДАТОР БЕЗ ИСПРАВЛЕНИЯ (владелец,
2026-09-29): любой разрыв — результат без вызова, вызов без результата — называется, и импорт
отказывает целиком. Чинить пары за человека нельзя: мы не знаем, что он имел в виду.

Модуль без относительных импортов верхнего уровня по той же причине, что `_export.py`: его
соседей грузят и пакетом, и по пути.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

try:
    from . import _export
except ImportError:  # загружены по пути, вне пакета — берём соседа тем же способом
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("hermes_filetools_ctx_export",
                                         Path(__file__).with_name("_export.py"))
    _export = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_export)

# `007__2026.09.22-23.45.10__m000d__assistant.md`. Адрес и роль — как их пишет `file_name`.
FILE_RE = re.compile(r"^(?P<seat>\d+)__(?P<stamp>\d{4}\.\d{2}\.\d{2}-\d{2}\.\d{2}\.\d{2})__"
                     r"(?P<addr>[A-Za-z0-9_.-]+)__(?P<role>[a-z_]+)\.md$")
# Первая строка половины-вызова: имя тула. Имена тулов — идентификаторы, прозой такое не бывает
# только в сочетании со вторым признаком (JSON ниже или ничего), поэтому проверяется вместе.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
ROLES = ("user", "assistant", "tool", "system")


def parse_stamp(text: str):
    """`ГГГГ.ММ.ДД-ЧЧ.ММ.СС` → timestamp (локальное время, как пишет `_export.stamp`)."""
    if not text or text == _export.NO_STAMP:
        return None
    try:
        return datetime.strptime(text, "%Y.%m.%d-%H.%M.%S").timestamp()
    except ValueError:
        return None


def parse_call(half: str):
    """Половина → `(имя, аргументы)` или None, если это не вызов.

    Аргументы — разобранный JSON, когда он разбирается; иначе строка как есть (битые аргументы
    выгрузка тоже пишет строкой — факт разговора, возвращаем его тем же).
    """
    text = str(half or "").strip("\n")
    head, _, rest = text.partition("\n")
    head = head.strip()
    if not _NAME_RE.match(head):
        return None
    rest = rest.strip()
    if not rest:
        return head, {}
    try:
        return head, json.loads(rest)
    except (TypeError, ValueError):
        # Сырая строка аргументов бывает, но тогда это должен быть «не JSON, похожий на JSON».
        # Обычная проза под одним словом вызовом не считается.
        return (head, rest) if rest.startswith(("{", "[")) else None


def _canon(args) -> str:
    """Аргументы в одной форме для сравнения вызова реплики с вызовом в файле результата."""
    if isinstance(args, str):
        return args.strip()
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(args)


def _arguments(args) -> str:
    """Поле `arguments` вызова — строка JSON, как её хранит провайдер."""
    if isinstance(args, str):
        return args
    return json.dumps(args, ensure_ascii=False)


def list_files(folder) -> tuple[list, list]:
    """Папка → `(файлы по номеру, проблемы)`. Файл — `{seat, stamp, addr, role, path}`."""
    problems = []
    path = Path(str(folder or "")).expanduser()
    if not str(folder or "").strip():
        return [], ["папка не задана"]
    if not path.is_dir():
        return [], [f"папки нет: {path}"]
    files, seats = [], {}
    for item in sorted(path.iterdir()):
        if not item.is_file() or item.suffix.lower() != ".md":
            continue
        m = FILE_RE.match(item.name)
        if not m:
            problems.append(f"{item.name}: имя без номера позиции — порядок не определить")
            continue
        seat = int(m.group("seat"))
        if seat in seats:
            problems.append(f"{item.name}: номер {m.group('seat')} уже у {seats[seat]}")
            continue
        seats[seat] = item.name
        files.append({"seat": seat, "stamp": m.group("stamp"), "addr": m.group("addr"),
                      "role": m.group("role"), "path": item, "name": item.name})
    files.sort(key=lambda f: f["seat"])
    if not files and not problems:
        problems.append(f"в папке нет файлов сообщений: {path}")
    return files, problems


def build_rows(files, read=None) -> tuple[list, list]:
    """Файлы → `(строки ленты, проблемы)`. Строки — в форме `replace_messages` Hermes.

    `read(path) -> str` — подмена чтения для тестов.
    """
    read = read or (lambda p: Path(p).read_text(encoding="utf-8"))
    rows, problems = [], []
    last_stamp = 0.0
    # Вызовы последней реплики, ещё без результата: `[(канон, имя, id)]`.
    pending: list = []
    for f in files:
        name = f["name"]
        role = f["role"]
        if role not in ROLES:
            problems.append(f"{name}: роль {role!r} не знаем")
            continue
        try:
            text = read(f["path"])
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{name}: не читается — {type(exc).__name__}: {exc}")
            continue
        # Время — из имени; одинаковое или назад идущее чуть сдвигаем вперёд: `(роль, время)` —
        # личность сообщения, и два одинаковых штампа склеили бы адреса.
        ts = parse_stamp(f["stamp"]) or last_stamp
        if ts <= last_stamp:
            ts = last_stamp + 1e-3
        last_stamp = ts
        halves = _export.split_halves(text)

        if role == "assistant":
            calls = []
            prose = ""
            for i, half in enumerate(halves):
                call = parse_call(half)
                if call is None:
                    if i == 0:
                        prose = half
                        continue
                    problems.append(f"{name}: половина {i + 1} не читается как вызов "
                                    "(имя тула первой строкой, ниже JSON)")
                    continue
                calls.append(call)
            row = {"role": "assistant", "content": prose, "timestamp": ts}
            pending = []
            if calls:
                tool_calls = []
                for k, (tool, args) in enumerate(calls):
                    call_id = f"imp_{f['seat']}_{k}"
                    tool_calls.append({"id": call_id, "type": "function",
                                       "function": {"name": tool, "arguments": _arguments(args)}})
                    pending.append((tool, _canon(args), call_id))
                row["tool_calls"] = tool_calls
            rows.append(row)
            continue

        if role == "tool":
            call = parse_call(halves[0]) if len(halves) > 1 else None
            body = _export.RULER.join(halves[1:]) if call is not None else text.strip("\n")
            if call is None:
                old = _export.RULER_MARK not in text and "\n---\n" in text
                problems.append(f"{name}: результат без своего вызова в файле — пару не собрать"
                                + (" (похоже на старый разделитель v1: голый `---`)" if old else ""))
                rows.append({"role": "tool", "content": body, "timestamp": ts})
                continue
            tool, args = call
            key = _canon(args)
            match = next((p for p in pending if p[0] == tool and p[1] == key), None)
            if match is None:
                problems.append(f"{name}: вызова {tool} с такими аргументами нет в предыдущей "
                                "реплике ассистента")
                rows.append({"role": "tool", "content": body, "timestamp": ts, "tool_name": tool})
                continue
            pending.remove(match)
            rows.append({"role": "tool", "content": body, "timestamp": ts,
                         "tool_call_id": match[2], "tool_name": tool})
            continue

        pending = []
        rows.append({"role": role, "content": text.strip("\n"), "timestamp": ts})
    return rows, problems


def load(folder, read=None) -> dict:
    """Папка → `{rows, problems, files}`. Ничего не пишет."""
    files, problems = list_files(folder)
    rows, more = build_rows(files, read) if files else ([], [])
    return {"rows": rows, "problems": problems + more, "files": len(files)}
