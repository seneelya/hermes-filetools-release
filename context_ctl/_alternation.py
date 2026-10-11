"""Чередование реплик в собранной ленте — под ИХ правило, не ломая наше (владелец, 2026-09-29).

ИХ ПРАВИЛО (`agent/agent_runtime_helpers.py::repair_message_sequence`): два `assistant` подряд
склеиваются в один, два `user` подряд — тоже; `tool` между ними соседство разрывает. Чинит это
Hermes перед каждым запросом, а в автокомпакции (путь B) — и в базе: живьём дистиллят от лица
ассистента сразу за репликой ассистента `m0002` склеился с ней в одну строку и потерял свой адрес.

НАШ ОТВЕТ — поезд, который их правилу удовлетворяет сам:
- граница — ПЕРВАЯ ИЗМЕНЁННАЯ СТРОКА (владелец, 2026-10-03). До неё лента — старый,
  закешированный у провайдера префикс: его стыки не трогаем, даже кривые (их загрузчик такую
  пару и так склеивает в памяти), — иначе сброс кеша сдвинулся бы раньше, чем его сдвигает
  сама правка. С неё и дальше кеш всё равно потерян, и там чиним ВСЕ стыки одной роли — новые
  (вокруг дистиллята, скобки, пузырьков) и старые: лента после места вставки уходит чистой.
  До 2026-10-03 старые стыки не трогались нигде;
- на стыке одной роли: если одна сторона — наша закрывающая скобка, сначала пробуем
  сменить ЕЁ роль (она говорит голосом системы, `[◆FT-DISTILL-END…]`, чужих слов от имени
  человека не будет); не вышло — вставляем мостик противоположной роли, тоже голосом системы и
  ОБЫЧНЫМИ СЛОВАМИ, понятными агенту (владелец: «агент понятия не имеет, что за зверь
  `◆FT-BRIDGE◆`»). С 2026-09-30 — ОДИН АВТОР (владелец: «чтобы был виден один автор»): шапка из
  семьи дистиллята и скобки, `[CONTEXT COMPACTION ENGINE: … ◆bridge-message◆]` (метка с 2026-10-07), и слова дистиллята.
  Метки дистиллята `◆FT-DISTILL_v1◆` в ней нет (замыкающий ромб): `is_distillate` мостик не
  примет за дистиллят — иначе проверка «новый дистиллят на месте» прошла бы на мостике.
  Старые мостики `[system — context compaction:` узнаются по-прежнему.

ПРОВЕРКА — ИХ ЖЕ ФУНКЦИЕЙ (`their_repairs`): в новой ленте их починок не больше, чем в старой.
Иначе лента не уходит — лучше не сжать, чем отдать поезд, который они разрежут. Сравнивать —
ПОДОБНОЕ С ПОДОБНЫМ (2026-10-03): ленту, голова которой собрана из строк базы, — с лентой до
правки тоже в виде строк базы, а не только с памятью хода. Их загрузчик в памяти старые пары
склеивает, и сравнение с памятью давало «1 против 0» на нарушении, которое мы не вносили
(автокомпакция сессии `4fb201`: импорт оставил в голове пару ассистентов — реплика + старый
дистиллят, — а голову защищал `head_keep`).

ДВА ПОТРЕБИТЕЛЯ (2026-09-30): компакция и `erase`. Стирание тоже рождает новые стыки одной роли
(ответ между двумя репликами человека, реплика между двумя ответами, начало ленты), но сводки у
него нет — поэтому свои, короткие тексты мостика (`fit(..., kind="erase")`), та же метка.
ТРЕТИЙ — импорт (`Plan27`, 2026-10-05): стыки на входе в импортированное и на выходе из него
(`tail`/`head`) и внутри него (папку правили руками) — `fit(..., kind="import")`, свои тексты.
"""
from __future__ import annotations

import copy
import time

from ._prompts import bridge_mark, bridge_texts, is_distill_end, is_distillate
from ._prompts import is_bridge as _is_bridge_text

BRIDGE_MARK = bridge_mark()
_TALK = ("user", "assistant")


# Текст мостика — ПО МЕСТУ, не по роли (владелец 2026-09-30: иначе мостик врёт агенту — «сводка
# ниже», когда она уже выше). Место определяют соседи: вставка справа/слева, скобка справа.
# СЛОВА — У ОПЕРАЦИИ (владелец, 2026-10-07): наборы по местам живут в `_prompts.bridge_texts(kind)`,
# операция может передать свой (`fit(..., texts=)`); здесь — только наборы по умолчанию.
BRIDGE_TEXT = bridge_texts("compact")
# Мостики стирания (`erase`, 2026-09-30) — короткие (владелец: «толку от удаления мало»).
ERASE_TEXT = bridge_texts("erase")
# Мостики импорта (`Plan27`, 2026-10-05): по месту — вход в импортированное, выход из него, стык
# внутри него. Какая сторона импортирована, `fit` знает по `old` (импортированных строк там нет).
IMPORT_TEXT = bridge_texts("import")


def _text(row) -> str:
    return str(row.get("content") or "") if isinstance(row, dict) else ""


def _is_distillate(row) -> bool:
    return is_distillate(_text(row))


def bridge_text(prev, row, kind: str = "compact", is_new=None, texts=None, is_insert=None) -> str:
    """Текст мостика между `prev` и `row` — по тому, что стоит рядом; `kind` — чья правка.

    `is_new(row) -> bool` — строки не было в ленте до правки. Нужен импорту: отличить вход в
    импортированное от выхода из него. `texts` — набор операции по местам (`_prompts.bridge_texts`);
    `is_insert(row)` — это вставка операции (по умолчанию — наш дистиллят по метке).
    """
    if kind == "erase":
        return ERASE_TEXT["start"] if prev is None else ERASE_TEXT["gap"]
    if kind == "import":
        new = is_new or (lambda r: True)
        if prev is None or (new(row) and not new(prev)):
            return IMPORT_TEXT["start"]
        if new(prev) and not new(row):
            return IMPORT_TEXT["end"]
        return IMPORT_TEXT["inside"]
    words = texts or BRIDGE_TEXT
    inserted = is_insert or _is_distillate
    if inserted(row):
        return words["before"]
    if prev is not None and inserted(prev):
        return words["after"]
    if _is_closing(row):
        return words["kept_end"]
    return words["continue"]


def is_bridge(row) -> bool:
    return _is_bridge_text(_text(row))


def _is_closing(row) -> bool:
    """Закрывающая скобка — строка с меткой конца, которая НЕ дистиллят: дистиллят без пузырьков
    сам заканчивается текстом скобки. Метка ищется подстрокой всех версий (`/1`, `_v1`)."""
    text = _text(row)
    return ("FT-DISTILL-END" in text or is_distill_end(text)) and not is_distillate(text)


def _role(row) -> str:
    return str(row.get("role") or "") if isinstance(row, dict) else ""


def _other(role: str) -> str:
    return "user" if role == "assistant" else "assistant"


def fit(new: list, old: list, kind: str = "compact", texts=None, inserts=None) -> tuple[list, int]:
    """Лента `new` без стыков одной роли. `(лента, сколько мостиков вставлено)`.

    `old` — лента до сборки. По ней находится ПЕРВАЯ ИЗМЕНЁННАЯ СТРОКА (первая позиция, где в
    `new` стоит не тот же объект, что в `old`): стыки до неё — старый закешированный префикс, не
    трогаем; стык, правая сторона которого — она или дальше, чиним, и старый тоже (2026-10-03, см.
    шапку). Ещё по `old` узнаётся НОВОЕ начало ленты (см. ниже). Объекты строк не копируются —
    пересобирается только список (у закрывающей скобки может смениться `role`).
    Скобку переворачиваем, лишь когда она стоит СЛЕВА на стыке и переворот не сталкивает её с
    левым соседом; во всех остальных случаях — мостик.

    `texts` — слова операции по местам (`_prompts.bridge_texts`); `inserts` — `id` строк-вставок
    операции: они, а не угадывание по метке, решают места `before`/`after` (прежний дистиллят в ленте
    тоже считается вставкой — как было).
    """
    out, bridges = [], 0
    stamp = time.time() + 0.0005
    first_changed = next((i for i, row in enumerate(new) if i >= len(old) or old[i] is not row),
                         len(new))
    old_ids = {id(r) for r in old}

    def is_new(row) -> bool:
        return id(row) not in old_ids

    ins = set(inserts or ())

    def is_insert(row) -> bool:
        return id(row) in ins or _is_distillate(row)

    for index, row in enumerate(new):
        # ПЕРВАЯ реплика после системного промпта — от пользователя (многие провайдеры иначе
        # отказывают; их собственная компакция в начале ленты тоже пишет от пользователя). Если
        # лента начинается с НАШЕЙ строки от ассистента (голова `head_keep: 0`) — мостик перед ней.
        # У `erase` первая строка — СТАРАЯ (стёрто то, что стояло перед ней), поэтому для него
        # новое начало — «лента раньше начиналась не с неё».
        new_start = is_new(row) or (kind == "erase" and (not old or old[0] is not row))
        if not out and _role(row) == "assistant" and new_start:
            out.append({"role": "user", "content": bridge_text(None, row, kind, is_new, texts, is_insert),
                        "timestamp": stamp})
            bridges += 1
        prev = out[-1] if out else None
        if (index >= first_changed and prev is not None
                and _role(prev) == _role(row) and _role(row) in _TALK):
            before = out[-2] if len(out) > 1 else None
            if _is_closing(prev) and _role(before) != _other(_role(prev)):
                prev["role"] = _other(_role(prev))
            else:
                role = _other(_role(row))
                out.append({"role": role, "content": bridge_text(prev, row, kind, is_new, texts, is_insert),
                            "timestamp": stamp + bridges * 1e-3})
                bridges += 1
        out.append(row)
    return out, bridges


def their_repairs(messages: list):
    """Сколько починок чередования сделала бы ИХ функция на копии ленты; `None` — их кода нет."""
    try:
        from agent.agent_runtime_helpers import repair_message_sequence
    except Exception:  # noqa: BLE001 - тесты / голый python: проверять нечем
        return None
    try:
        return int(repair_message_sequence(None, copy.deepcopy(list(messages))))
    except Exception:  # noqa: BLE001 - их сигнатура поменялась — проверку честно пропускаем
        return None
