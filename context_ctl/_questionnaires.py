"""РЕЕСТР опросников тем (`Plan32.1`). Опросник — описание, а не код: новая категория — новая
запись здесь. Тексты — построители `_prompts`, механика — `_scenario`. ТЛДР: что за наборы и как
добавить свой — `__HQ/HowTo__Questionnaires.md`.

Как тема получает опросник (владелец, 2026-10-11): сначала узлы-ФИЛЬТРЫ (`FILTERS`) убирают
неуместные наборы, потом модели показывается МЕНЮ оставшихся — для чего каждый и что он спросит —
и она выбирает набор для каждой темы (`which_set`, думание включено). «Не всё подряд вываливаем».

Перенос 2026-10-11: `work` / `recon` / `design` / `rules` — общее ядро и своя «пачка» (`q7_pack`
по имени), `talk` — один вопрос без ворот и самопроверки. Добавлены `brief` и нейтральный запасной
`general` (вместо прежнего `other` с пачкой про код).
"""
from __future__ import annotations

from . import _prompts
from ._scenario import (Choice, ChooseEach, Gate, Names, Number, Option, Q, Questionnaire,
                        Section, Split, Verdict, Yes)

NL_ = "\n"


def titles_block(titles) -> str:
    """Список названий для подстановки в вопрос. Нумерация для человека в отчёте; модель
    отвечает НАЗВАНИЯМИ, не номерами, — индексы она сдвигает молча."""
    return NL_.join(f"{i}. {t}" for i, t in enumerate(titles, 1))


def resolve_split(titles: list, parsed: dict) -> tuple:
    """Ответ про главные/филер → `(полный разбор, филер)`. Отдельной функцией, потому что
    вырожденные случаи разрешает КОД, а не лишний вопрос модели.

    Тема не попала никуда — идёт в полный разбор: разобрать лишнее дешевле, чем потерять.
    Попала в оба списка — главная побеждает. Порядок сохраняется исходный, чтобы ответ на
    вопрос про классы (массив по порядку) ложился на те же темы.
    """
    parsed = parsed or {}
    filler = [t for t in parsed.get("filler") or [] if t in titles]
    main = [t for t in parsed.get("main") or [] if t in titles]
    full = [t for t in titles if t in main or t not in filler]
    return full, [t for t in titles if t in filler and t not in full]


# ── Вопросы уровня сессии (сценарий компакции `_distill._compaction`) ─────────────────
# Схема и чтение ответа — форма вопроса (`_scenario`); сценарий сырого JSON не видит.

Q_COUNT = Q("q1_count", lambda **kw: _prompts.q1_count() + _prompts.zone_note(),
            Number(_prompts.s1_count, _prompts.COUNT_KEY))
Q_TITLES = Q("q2_titles", lambda count, **kw: _prompts.q2_titles(count) + _prompts.zone_note(),
             Names(_prompts.s2_titles))
Q_RECOUNT = Q("q2_recount", lambda count, names, **kw: _prompts.q2_recount(
    count, titles_block(names)), Verdict(_prompts.s2_recount))
Q_MERGE = Q("q2_merge", lambda count, names, **kw: _prompts.q2_titles_merge(
    count, titles_block(names)), Names(_prompts.s2_titles))
Q_SPLIT = Q("q3_split", lambda names, **kw: _prompts.q3_split(titles_block(names))
            + _prompts.zone_note(), Split(_prompts.s3_split, resolve_split))
Q_FILLER = Q("q11_filler", lambda names, **kw: _prompts.q11_filler(titles_block(names)))
# Запреты и ограничения — ОДИН раз на сессию, а не по темам: правило, сказанное в одной теме,
# действует во всех. Дословно, потому что пересказ запрета запретом не является.
Q_CONSTRAINTS = Q("q_constraints", lambda **kw: _prompts.q_constraints_verbatim(),
                  Yes(_prompts.s_constraints_verbatim, _prompts.CONSTRAINTS_KEY))
# НЕЧЕГО ДОБАВИТЬ — НЕ ПИШЕМ НИЧЕГО (владелец, 2026-09-21): `false` означает молчание, своей
# строки «дополнений нет» код не пишет — пустой раздел через следующую компакцию читается как
# содержание.
Q_ADDITIONS = Q("q12_additions", lambda document, known, **kw: _prompts.q12_additions(
    document, known), Yes(_prompts.s12_additions, _prompts.ADDITIONS_KEY))

# ── Вопросы темы ──────────────────────────────────────────────────────────────────────────────

# Ворота: какие разделы вообще есть смысл писать. Своя рамка темы внутри вопроса.
GATE = Q("q_gate", lambda title, n, total, fields, **kw: _prompts.q_gate(title, n, total, fields),
         Gate(_prompts.s_gate), scoped=False)

# «Пришли» не за воротами: тема без единого итога — не тема.
SETTLED = Q("q5_settled", lambda **kw: _prompts.q5_settled())
PENDING = Q("q_pending_task", lambda **kw: _prompts.q_pending_task())
OPEN = Q("q6_open", lambda **kw: _prompts.q6_open())
ERRORS = Q("q_errors_fixes", lambda **kw: _prompts.q_errors_fixes())
VALUES = Q("q_critical_values", lambda **kw: _prompts.q_critical_values())
# Пачка по виду темы ПОВЕРХ ядра — и она видит, что уже сказано («пришли», «открыто»).
PACK = Q("q7_pack", lambda kind, answers, **kw: _prompts.q7_pack(
    kind, answers.get("settled", ""), answers.get("open", "")))

# Самопроверка собранного раздела — своя рамка, данных темы перед ней нет.
SELFCHECK = Q("q10_check", lambda section, **kw: _prompts.q10_selfcheck(section),
              Yes(_prompts.s10_selfcheck, _prompts.selfcheck_key(), "what_exactly"),
              scoped=False)

# Разговор не допрашиваем: технические вопросы вредны — модель начнёт пересказывать болтовню
# как отладку (замысел владельца 2026-09-21).
TALK = Q("q_talk", lambda **kw: _prompts.q_talk())


def _head(fn):
    return lambda kind: fn()


# Ядро полного разбора: порядок ВОПРОСОВ. Под воротами — смысловые имена полей ворот.
CORE = (
    Section("settled", SETTLED, head=_head(_prompts.doc_settled_head)),
    Section("pending", PENDING, gate="pending_task", head=_head(_prompts.doc_pending_head)),
    Section("open", OPEN, gate="open_items", head=_head(_prompts.doc_open_head)),
    Section("errors", ERRORS, gate="errors_fixes", head=_head(_prompts.doc_errors_head)),
    Section("values", VALUES, gate="critical_values", head=_head(_prompts.doc_values_head)),
    Section("pack", PACK, gate="class_details", head=_prompts.doc_pack_head),
)


def _tail(sentence: str) -> str:
    """«Work on the system: writing…» → «writing…»: имя набора уже стоит в точке расхождения."""
    return sentence.split(": ", 1)[-1]


def _full(name: str, word: str, sentence: str) -> Questionnaire:
    return Questionnaire(name=name, word=word, pick=_tail(sentence), sections=CORE,
                         gate=GATE, after=SELFCHECK)


# ── Реестр ────────────────────────────────────────────────────────────────────────────────────
# `word` — слово-направление в точке расхождения выбора: сильное существительное, у каждого своё
# первое слово (`_scenario.Choice` проверяет при сборке). `other` — запасной вариант выбора.
REGISTRY = {
    "work": _full("work", "work", _prompts.kind_work()),
    "recon": _full("recon", "investigation", _prompts.kind_recon()),
    "design": _full("design", "design", _prompts.kind_design()),
    "rules": _full("rules", "rules", _prompts.kind_rules()),
    "talk": Questionnaire(name="talk", word="conversation", pick=_tail(_prompts.kind_talk()),
                          sections=(Section("talk", TALK, head=_head(_prompts.doc_talk_head)),)),
    "brief": Questionnaire(name="brief", word="brief", pick=_tail(_prompts.kind_brief()),
                           sections=(Section("settled", SETTLED, head=_head(_prompts.doc_settled_head)),
                                     Section("open", OPEN, head=_head(_prompts.doc_open_head)))),
    # НЕЙТРАЛЬНЫЙ НАБОР, он же запасной (владелец, 2026-10-11): всё, что не работа над системой, —
    # история, выдуманный мир, план, идея — простыми словами: без «ошибок и починок» и без пачки
    # про пути и команды; вместо неё — КАНОН (имена, числа, факты, которые потеряет пересказ).
    "general": Questionnaire(
        name="general", word="general", pick=_tail(_prompts.kind_general()), gate=GATE,
        after=SELFCHECK,
        sections=(Section("settled", SETTLED, head=_head(_prompts.doc_settled_head)),
                  Section("pending", PENDING, gate="pending_task",
                          head=_head(_prompts.doc_pending_head)),
                  Section("open", OPEN, gate="open_items", head=_head(_prompts.doc_open_head)),
                  Section("pack", PACK, gate="class_details", head=_prompts.doc_pack_head))),
}
FALLBACK = "general"


def of(name: str) -> Questionnaire:
    """Опросник по имени; неизвестное — запасной (разобрать лишнее дешевле, чем потерять)."""
    return REGISTRY.get(name) or REGISTRY[FALLBACK]


# ── Узлы-фильтры: что вообще предлагать ───────────────────────────────────────────────────────
# Фильтр — функция `(ctx) -> имена, которые НЕ предлагать`. `ctx`: `work_rows` (строк в зоне
# работ), `topics` (названия тем). Фильтр может быть и вопросом модели — тогда он получает вид
# `ctx["view"]` и спрашивает через него; сейчас такие не заведены.

# Короткий набор уместен только на небольшом куске (владелец: «на 20 простых сообщениях»).
# Порог — черновой, подгоняется по живым прогонам.
BRIEF_MAX_ROWS = 40


def _brief_only_when_small(ctx: dict) -> set:
    return set() if ctx.get("work_rows", 0) <= BRIEF_MAX_ROWS else {"brief"}


FILTERS = (_brief_only_when_small,)


def offered(ctx: dict) -> list:
    """Опросники, которые переживут все фильтры; запасной — всегда (он вариант той же формы)."""
    hidden: set = set()
    for f in FILTERS:
        hidden |= set(f(ctx))
    hidden.discard(FALLBACK)
    return [q for name, q in REGISTRY.items() if name not in hidden]


# ── Узел выбора ───────────────────────────────────────────────────────────────────────────────

def menu(sets: list) -> str:
    """Меню для модели: слово набора, для чего он, что спросит. Запасной — последним."""
    order = [q for q in sets if q.name != FALLBACK] + [q for q in sets if q.name == FALLBACK]
    return "\n".join(f"({q.word}): {q.pick}\n    asks: {' · '.join(q.asks)}" for q in order)


def option(q: Questionnaire) -> Option:
    return Option(q.word, q.pick)


def choice_of(sets: list) -> Choice:
    """Развилка выбора набора: зачин-разгон `which_set_lead()` до слова набора; запасной —
    `other`, последним."""
    return Choice(_prompts.which_set_lead(), [option(q) for q in sets if q.name != FALLBACK],
                  option(of(FALLBACK)))


def which_set(sets: list) -> Q:
    """Вопрос выбора набора для каждой темы. Варианты — генератор (`choice_of`). Думание
    включено: решение одно, на нём держится весь опрос темы. Ответ читается сразу в опросник:
    `{тема: Questionnaire}`."""
    choice = choice_of(sets)
    fallback = of(FALLBACK)
    by_word = {q.word: q for q in sets}
    by_word.setdefault(fallback.word, fallback)
    text = menu(sets)
    return Q("q_which_set",
             lambda names, **kw: _prompts.q_which_set(titles_block(names), text) + _prompts.zone_note(),
             _Pick(ChooseEach(choice, "filetools_topic_sets"), by_word), think=True)


class _Pick(ChooseEach):
    """`ChooseEach`, читающий сразу в опросник: слово набора → `Questionnaire`."""

    def __init__(self, inner: ChooseEach, by_word: dict):
        super().__init__(inner.choice, inner.name)
        self.by_word = by_word

    def read(self, parsed, names=(), **kw):
        return {t: self.by_word[w] for t, w in super().read(parsed, names=names).items()}
