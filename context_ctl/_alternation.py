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
  семьи дистиллята и скобки, `[◆FT-DISTILL-BRIDGE_v1◆ CONTEXT COMPACTION: …]`, и слова дистиллята.
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
"""
from __future__ import annotations

import copy
import time

from ._prompts import bridge_mark, engine_voice, is_distill_end, is_distillate
from ._prompts import is_bridge as _is_bridge_text

BRIDGE_MARK = bridge_mark()
_TALK = ("user", "assistant")


def _say(text: str) -> str:
    """Служебная строка компакции: говорящий впереди, метка в конце штампом (владелец, 2026-09-30)."""
    return f"[{engine_voice()} {text}. {BRIDGE_MARK}]"


# Текст мостика — ПО МЕСТУ, не по роли (владелец 2026-09-30: иначе мостик врёт агенту — «сводка
# ниже», когда она уже выше). Место определяют соседи: дистиллят справа/слева, скобка справа.
BRIDGE_TEXT = {
    "before": _say("the earlier part of this conversation was compacted here, the summary follows"),
    "after": _say("the summary above replaces the compacted part; the turns below were kept as "
                  "they were"),
    "kept_end": _say("the kept turns end here"),
    "continue": _say("end of the compacted part, the conversation continues"),
}

# Мостики стирания (`erase`, 2026-09-30) — короткие (владелец: «толку от удаления мало»).
ERASE_TEXT = {
    "start": _say("conversation start removed"),
    "gap": _say("messages removed here"),
}


def _text(row) -> str:
    return str(row.get("content") or "") if isinstance(row, dict) else ""


def _is_distillate(row) -> bool:
    return is_distillate(_text(row))


def bridge_text(prev, row, kind: str = "compact") -> str:
    """Текст мостика между `prev` и `row` — по тому, что стоит рядом; `kind` — чья правка."""
    if kind == "erase":
        return ERASE_TEXT["start"] if prev is None else ERASE_TEXT["gap"]
    if _is_distillate(row):
        return BRIDGE_TEXT["before"]
    if _is_distillate(prev):
        return BRIDGE_TEXT["after"]
    if _is_closing(row):
        return BRIDGE_TEXT["kept_end"]
    return BRIDGE_TEXT["continue"]


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


def fit(new: list, old: list, kind: str = "compact") -> tuple[list, int]:
    """Лента `new` без стыков одной роли. `(лента, сколько мостиков вставлено)`.

    `old` — лента до сборки. По ней находится ПЕРВАЯ ИЗМЕНЁННАЯ СТРОКА (первая позиция, где в
    `new` стоит не тот же объект, что в `old`): стыки до неё — старый закешированный префикс, не
    трогаем; стык, правая сторона которого — она или дальше, чиним, и старый тоже (2026-10-03, см.
    шапку). Ещё по `old` узнаётся НОВОЕ начало ленты (см. ниже). Объекты строк не копируются —
    пересобирается только список (у закрывающей скобки может смениться `role`).
    Скобку переворачиваем, лишь когда она стоит СЛЕВА на стыке и переворот не сталкивает её с
    левым соседом; во всех остальных случаях — мостик.
    """
    out, bridges = [], 0
    stamp = time.time() + 0.0005
    first_changed = next((i for i, row in enumerate(new) if i >= len(old) or old[i] is not row),
                         len(new))
    for index, row in enumerate(new):
        # ПЕРВАЯ реплика после системного промпта — от пользователя (многие провайдеры иначе
        # отказывают; их собственная компакция в начале ленты тоже пишет от пользователя). Если
        # лента начинается с НАШЕЙ строки от ассистента (голова `head_keep: 0`) — мостик перед ней.
        # У `erase` первая строка — СТАРАЯ (стёрто то, что стояло перед ней), поэтому для него
        # новое начало — «лента раньше начиналась не с неё».
        new_start = (id(row) not in {id(r) for r in old}) or (
            kind == "erase" and (not old or old[0] is not row))
        if not out and _role(row) == "assistant" and new_start:
            out.append({"role": "user", "content": bridge_text(None, row, kind), "timestamp": stamp})
            bridges += 1
        prev = out[-1] if out else None
        if (index >= first_changed and prev is not None
                and _role(prev) == _role(row) and _role(row) in _TALK):
            before = out[-2] if len(out) > 1 else None
            if _is_closing(prev) and _role(before) != _other(_role(prev)):
                prev["role"] = _other(_role(prev))
            else:
                role = _other(_role(row))
                out.append({"role": role, "content": bridge_text(prev, row, kind),
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
