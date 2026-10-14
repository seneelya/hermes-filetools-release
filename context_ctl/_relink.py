# -*- coding: utf-8 -*-
"""Импорт заводит операции в журнал ЦЕЛЕВОЙ сессии (`Plan27-Task02`, владелец 2026-10-02).

ЗАЧЕМ. После импорта у всех вызовов стоял `no ledger record`: журнал связывает запись операции с
вызовом по `tool_call_id`, а импорт выдаёт вызовам новые id и журнал не трогал. Угадывать по
журналу ДРУГОЙ сессии нельзя: номер `0006` там — другая операция или его нет вовсе. Поэтому
записи едут с папкой (спутник `ledger.jsonl`, `_export.LEDGER_FILE`), и здесь каждая находит
СВОЙ результат по номеру из его текста.

ПРАВИЛА (план, Task02):
1. Результат с `Operation ID` в тексте и записью с этим `op` в спутнике — кандидат.
2. Имя тула записи = имя вызова в файле. Не сходится — не привязывать, назвать.
3. Один `op` спутника — один результат. Повтор — второй не привязывать, назвать.
4. Новый номер — из счётчика ЦЕЛЕВОЙ сессии: номер — адрес `/ctx-drop` и панели, обязан быть
   уникальным в сессии. В тексте результата меняются ТОЛЬКО ЦИФРЫ — строка обёрнута знаком
   комментария (`# …`, `◆…`, `<!-- … -->`, `deferred/01`), и замена строки целиком снесла бы обёртку.
5. Запись — копия: новый `op`, `tool_call_id` импорта, `session` целевая, `session_src: "import"`,
   `imported_from: {session, op}`. `sha8`/`chars` — по новому тексту; не сходились со старым —
   текст правили руками, `edited: true`.
6. Нет записи или нет `Operation ID` — без привязки, как раньше.

`cmd_sidecar` и `turn` из копии сняты: приложение лежит в папке ИСХОДНОЙ сессии и с выгрузкой не
едет, номер хода — тоже её.

Строки правятся НА МЕСТЕ (это свежие словари импорта), записи возвращаются — пишет их вызывающий
ПОСЛЕ успешного коммита, иначе отказ коммита оставил бы в журнале записи о том, чего в ленте нет.
"""
from __future__ import annotations

from . import _render
from ..file_ops import _ledger

# Поля, которые в копии записи теряют смысл: приложение команды лежит в папке исходной сессии,
# номер хода — тоже её.
_DROPPED = ("cmd_sidecar", "turn")


def _same_text(rec, text: str) -> bool:
    """Текст тот же, что записан в журнале? Переводы строк ПО КРАЯМ не в счёт: файл выгрузки
    режется на половины без них (`_export.split_halves`), и честный ответ с `\\n` в конце
    иначе всегда выглядел бы правленым. До двух с каждого края (живьём 2026-10-14: `execute_code`
    с `\\n\\n` в конце)."""
    want = str(rec.get("sha8") or "")
    core = text.strip("\n")
    edges = ("", "\n", "\n\n")
    return _ledger.sha8(text) == want or any(
        _ledger.sha8(lead + core + trail) == want for lead in edges for trail in edges)


def relink(rows, records, session: str, allocate=None) -> tuple[list, list]:
    """`(записи для журнала целевой сессии, заметки)`. `allocate(session) -> str` — новый номер."""
    allocate = allocate or _ledger.next_op
    by_op, twice = {}, set()
    for rec in records or []:
        op = str(rec.get("op") or "").lower()
        if not op:
            continue
        if op in by_op:
            twice.add(op)
        else:
            by_op[op] = rec

    entries, notes, used = [], [], set()
    for row in rows or []:
        if str(row.get("role") or "") != "tool" or not row.get("tool_call_id"):
            continue
        text = str(row.get("content") or "")
        found = _render._OP_RE.search(text)
        if not found:
            continue
        old = found.group(1).lower()
        rec = by_op.get(old)
        if rec is None:
            continue
        if old in twice:
            notes.append(f"операция {old}: в спутнике дважды — не привязана")
            continue
        if old in used:
            notes.append(f"операция {old}: второй результат с тем же номером — не привязан")
            continue
        tool = str(row.get("tool_name") or "")
        if str(rec.get("tool") or "") != tool:
            notes.append(f"операция {old}: в записи {rec.get('tool')!r}, у вызова {tool!r} — не привязана")
            continue
        new = allocate(session)
        if not new:
            notes.append("счётчик операций целевой сессии недоступен — остальные без привязки")
            break
        used.add(old)
        edited = not _same_text(rec, text)
        new_text = text[:found.start(1)] + new + text[found.end(1):]
        row["content"] = new_text
        entry = {k: v for k, v in rec.items() if k not in _DROPPED}
        entry.update(op=new, tool_call_id=str(row["tool_call_id"]), session=session,
                     session_src="import",
                     imported_from={"session": str(rec.get("session") or ""), "op": old},
                     sha8=_ledger.sha8(new_text), chars=len(new_text))
        if edited:
            entry["edited"] = True
        entries.append(entry)
    return entries, notes
