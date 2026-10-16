"""Фреймворк сценариев опроса (`Plan32.1`, владелец и Соня5, 2026-10-11).

ЗАЧЕМ. Опрос компакции — многоходовая операция над ОДНИМ блоком данных. Сценарий пишется простым
Python (`if`, `for`), а всю возню берёт на себя этот модуль: сборку вопроса, схему, вызов модели,
чтение ответа, отчёт и ход для панели. Ради чего это строится — ветвление по КАТЕГОРИЯМ
опросников и выбор опросника агентом (`__HQ/plans/done/Plan32__scenario-framework-design.md`).

ТРИ СЛОЯ:
  `Run`    — один прогон: блок данных, системный промт, шаги. Единственная точка вызова модели.
  `View`   — что видит вопрос: все данные; у темы — плюс рамка «говорим о теме N из M» перед
             вопросом (`about`). Данные НЕ режутся (решение владельца: вырезы — не цель).
  `Q`      — вопрос: ключ шага, построитель текста из `_prompts`, форма ответа, думание.

ФОРМА НЕСЁТ СВОЙ КЛЮЧ И СВОЁ ЧТЕНИЕ — одно место. Сценарий сырого JSON не видит: три бага
2026-10-11 (схема спрашивала один ключ, код читал другой) при таком устройстве невозможны.

Опросник (`Questionnaire`) — ОПИСАНИЕ: разделы, ворота, шаг «после». Сами описания живут в
реестре `_questionnaires.py`, тексты — построителями в `_prompts`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from . import _llm, _progress, _prompts

NL_ = "\n"


# ── Вопрос и формы ответа ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Q:
    """Вопрос. `text(**kw)` — наш построитель; `kw` — тема (`title`, `n`, `total`) и то, что
    передал сценарий. `form=None` — проза без схемы. `scoped` — ставить ли рамку темы перед
    текстом (у ворот и самопроверки своя рамка внутри — им `False`). `takes_wish` — узел
    принимает пожелание пользователя (`Plan34`): блок `_prompts.wish_block` встаёт ПОСЛЕДНИМ, после
    текста вопроса и его правил ответа; узлы факта (ворота, самопроверка, запреты) его не берут."""
    key: str
    text: Callable[..., str]
    form: "Form | None" = None
    think: bool = False
    scoped: bool = True
    takes_wish: bool = False


class Form:
    """Форма JSON-ответа: схема и чтение ответа в одном месте."""

    def schema(self, **kw) -> tuple:
        raise NotImplementedError

    def read(self, parsed, **kw):
        raise NotImplementedError


class Number(Form):
    """Целое по ключу-разгону. Нет ответа — `0`; что значит ноль, решает сценарий."""

    def __init__(self, build: Callable[[], tuple], key: str):
        self.build, self.key = build, key

    def schema(self, **kw):
        return self.build()

    def read(self, parsed, **kw):
        return int((parsed or {}).get(self.key) or 0)


class Names(Form):
    """Список строк; пустые выброшены."""

    def __init__(self, build: Callable[[], tuple], field: str = "titles"):
        self.build, self.field = build, field

    def schema(self, **kw):
        return self.build()

    def read(self, parsed, **kw):
        return [str(t).strip() for t in (parsed or {}).get(self.field) or [] if str(t).strip()]


class Verdict(Form):
    """Одно значение `enum` по полю — как есть, строкой."""

    def __init__(self, build: Callable[[], tuple], field: str = "verdict"):
        self.build, self.field = build, field

    def schema(self, **kw):
        return self.build()

    def read(self, parsed, **kw):
        return str((parsed or {}).get(self.field, "") or "")


@dataclass(frozen=True)
class Answer:
    """Булево с текстом. `text` пуст, когда `value` ложно: прозы за «нет» не ждут."""
    value: bool
    text: str


class Yes(Form):
    """Булево по ключу-разгону + текстовое поле."""

    def __init__(self, build: Callable[[], tuple], key: str, text_field: str = "text"):
        self.build, self.key, self.text_field = build, key, text_field

    def schema(self, **kw):
        return self.build()

    def read(self, parsed, **kw):
        parsed = parsed or {}
        value = bool(parsed.get(self.key))
        return Answer(value, str(parsed.get(self.text_field) or "").strip() if value else "")


class Each(Form):
    """Одна развилка на каждое название ОДНИМ вызовом: ключи схемы — сами названия.
    `build(names)` — схема, `pick(value)` — значение ответа → имя варианта."""

    def __init__(self, build: Callable[[list], tuple], pick: Callable[[str], str]):
        self.build, self.pick = build, pick

    def schema(self, names=(), **kw):
        return self.build(names)

    def read(self, parsed, names=(), **kw):
        parsed = parsed or {}
        return {n: self.pick(parsed.get(n, "")) for n in names}


class Split(Form):
    """Названия → два списка. Вырожденные случаи разрешает КОД (`resolve(names, parsed)`)."""

    def __init__(self, build: Callable[[list], tuple], resolve: Callable[[list, dict], tuple]):
        self.build, self.resolve = build, resolve

    def schema(self, names=(), **kw):
        return self.build(names)

    def read(self, parsed, names=(), **kw):
        return self.resolve(list(names), parsed)


class Gate(Form):
    """Ворота: разобранный ответ как есть; что закрыто, решает опросник (`Questionnaire.closed`).
    `build(fields)` — схема по смысловым именам полей, которые у опросника под воротами."""

    def __init__(self, build: Callable[[list], tuple]):
        self.build = build

    def schema(self, fields=(), **kw):
        return self.build(list(fields))

    def read(self, parsed, **kw):
        return parsed or {}


# ── Прогон ────────────────────────────────────────────────────────────────────────────────────

class Run:
    """Один прогон опроса. `data` и `system` — ОДНИ на все вопросы: общий префикс запроса,
    десять вопросов подряд стоят один префилл. `state` — общий словарь состояния (`done`),
    `save()` — отчёт на диск после каждого шага. `wish` — проза пользователя к прогону
    (`Plan34`); каждый вопрос получает её в `kw`, узел, который её не берёт, просто не читает."""

    def __init__(self, session_id: str, data: str, system: str, lines: list,
                 state: dict, save: Callable[[], None], wish: str = "", menu=None):
        self.session_id, self.data, self.system = session_id, data, system
        self.lines, self.state, self.save = lines, state, save
        self.wish = str(wish or "").strip()
        # Наборы, выбранные человеком на этот прогон (`Plan34` Task 8); `None` — по настройке.
        self.menu = list(menu) if menu else None
        self.steps: list = []
        self.plan = {"total": 0, "topics": 0}       # ход для панели (`_progress`)

    def step(self, key: str, question: str, schema_and_name=None, think: bool = False) -> dict:
        """Один вопрос: задать, записать, сохранить."""
        label, n = _progress.label_of(key)
        total = self.plan["total"]
        _progress.progress(self.session_id, stage="questions", step=len(self.steps) + 1,
                           total=max(total, len(self.steps) + 1) if total else 0,
                           label=label, topic=f"{n}/{self.plan['topics']}" if n else "")
        note = {"key": key, "q": question}
        started = time.monotonic()
        if schema_and_name is None:
            # Проза — без схемы НАРОЧНО: `{"type": "string"}` не ограничивает ничего, а JSON
            # вокруг длинной прозы только добавляет экранирования (`Vision06`).
            answer = _llm.ask(self.data + NL_ * 2 + question, system=self.system, think=think)
            note["answer"] = str(answer.get("text") or "")
            note["parsed"] = None
        else:
            schema, name = schema_and_name
            answer = _llm.ask_structured(self.data, question, schema, schema_name=name,
                                         system=self.system, think=think)
            note["raw"] = answer.get("text", "")
            note["parsed"] = answer.get("parsed")
        note.update({"ms": int((time.monotonic() - started) * 1000),
                     "tokens": answer.get("tokens", 0), "error": answer.get("error", ""),
                     "ok": bool(answer.get("ok"))})
        self.steps.append(note)
        self.state["done"] = len(self.steps)
        mark = "✓" if note.get("ok") else "✗"
        self.lines.append(f"  {mark} {key}: {note.get('ms', 0)} мс"
                          + (f" — {note['error']}" if note.get("error") else ""))
        self.save()
        return note


class View:
    """Что видит вопрос. Без темы — все данные; `about(тема)` — те же данные и рамка темы
    перед вопросом. Ключ шага у темы получает номер: `q5_settled[2]`."""

    def __init__(self, run: Run, title: str = "", n: int = 0, total: int = 0):
        self.run, self.title, self.n, self.total = run, title, n, total
        self.scope = _prompts.topic_scope(title, n, total) if n else ""

    def about(self, title: str, n: int, total: int) -> "View":
        return View(self.run, title, n, total)

    def ask(self, q: Q, **kw):
        """Задать вопрос. Проза → `str`; JSON → то, что читает форма вопроса."""
        kw = {"title": self.title, "n": self.n, "total": self.total, "wish": self.run.wish, **kw}
        text = q.text(**kw)
        if q.takes_wish and self.run.wish:
            # ПОСЛЕДНИМ, после вопроса и его правил ответа (живой прогон 2026-10-15): стоявшее перед
            # вопросом пожелание «выброси всё» перебила наша же фраза «as fully as the transcript
            # allows» — побеждает ближнее к ответу (`Doc__prompting.md`).
            text = text + NL_ * 2 + _prompts.wish_block(self.run.wish)
        if q.scoped and self.n:
            text = self.scope + text
        key = f"{q.key}[{self.n}]" if self.n else q.key
        if q.form is None:
            return self.run.step(key, text, None, q.think)["answer"]
        note = self.run.step(key, text, q.form.schema(**kw), q.think)
        return q.form.read(note["parsed"], **kw)


# ── Опросник — описание ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Section:
    """Раздел темы: поле в записи темы, вопрос, (если раздел под воротами) смысловое имя
    ворот (`_prompts.gate_field` даёт ключ схемы) и заголовок раздела `head(kind)` — из него
    меню выбора говорит, ЧТО этот опросник спросит."""
    field: str
    q: Q
    gate: str | None = None
    head: Callable[[str], str] | None = None


@dataclass(frozen=True)
class Questionnaire:
    """Опросник темы. Порядок `sections` — порядок ВОПРОСОВ; порядок в документе — дело
    документа (`_distill.section_parts`). `after` — вопрос про собранный раздел (самопроверка:
    `Yes`, найденное → пометка ⚠ под темой). `word` — слово-направление в меню выбора (точка
    расхождения `enum`), `pick` — для чего опросник."""
    name: str
    pick: str
    sections: tuple
    gate: Q | None = None
    after: Q | None = None
    word: str = ""

    @property
    def asks(self) -> list:
        """Что опросник спросит — заголовками разделов, для меню."""
        return [s.head(self.name).lstrip("# ").strip() for s in self.sections if s.head]

    @property
    def max_steps(self) -> int:
        """Сколько вопросов задаст опросник, если ворота открыты все."""
        return len(self.sections) + (1 if self.gate else 0) + (1 if self.after else 0)

    @staticmethod
    def closed(gate: dict, section: Section) -> bool:
        """Раздел закрыт, если ворота ответили на его поле `false`. Ворот нет (сбой схемы) —
        открыто: потерять раздел дороже, чем задать лишний вопрос."""
        if not section.gate:
            return False
        key = _prompts.gate_field(section.gate)
        return key in gate and not gate.get(key)

    def run(self, t: View, render: Callable[[dict], str]) -> dict:
        """Опросить тему. Каждый вопрос видит прошлые ответы темы (`answers`) и имя опросника
        (`kind`). Возвращает запись темы для документа."""
        topic: dict = {"title": t.title, "class": self.name}
        # Заголовки разделов из описания: по ним документ ставит поля, которых сам не знает.
        heads = [(s.field, s.head(self.name)) for s in self.sections if s.head]
        if heads:
            topic["heads"] = heads
        gate: dict = {}
        if self.gate:
            # Ворота спрашивают ровно про разделы ЭТОГО опросника — текст и схема из описания.
            gate = t.ask(self.gate, fields=[s.gate for s in self.sections if s.gate])
            t.run.plan["total"] -= sum(1 for s in self.sections if self.closed(gate, s))
        answers: dict = {}
        for s in self.sections:
            answers[s.field] = "" if self.closed(gate, s) else \
                t.ask(s.q, answers=answers, kind=self.name)
            topic[s.field] = answers[s.field]
        topic["suspect"] = ""
        if self.after:
            check = t.ask(self.after, section=render(topic))
            if check.value:
                topic["suspect"] = check.text
        return topic


# ── Генератор развилки — правила `Doc__prompting.md` применяет код ───────────────────────────

# Служебные слова в точке расхождения ничего не называют и конкурируют вслепую (2026-09-21:
# `hands`, `about`, `none`). Имя варианта — слово-направление, не такое.
WEAK_WORDS = {"a", "an", "the", "about", "none", "no", "not", "yes", "hands", "it", "this", "that"}


@dataclass(frozen=True)
class Option:
    """Вариант развилки. `name` — слово-направление в точке расхождения (и имя, которое вернёт
    чтение); `says` — что он значит, идёт ПОСЛЕ имени."""
    name: str
    says: str


class Choice:
    """Значения `enum` строятся как ФРАЗА, ведущая по токенам до точки выбора:
    `lead + name + close + says` — «having weighed every question, the set that fits best is
    (work): …». Общий зачин — разгон (он нужен и при думании: грамматика режет словарь только
    после закрытия тега думания), точка расхождения — имя варианта.

    Запасной вариант — ТОЙ ЖЕ формы (это обычный `Option` с тем же зачином), иначе модель
    выберет «более похожий на ответ» (`Doc__prompting.md`, «1000 и один»).

    Правила проверяются ПРИ СБОРКЕ (`ValueError`), а не в прогоне: тест падает раньше модели."""

    def __init__(self, lead: str, options, fallback: Option, close: str = "): "):
        self.lead, self.close = lead, close
        self.options, self.fallback = tuple(options), fallback
        self._check()

    @property
    def all(self) -> tuple:
        return self.options + (self.fallback,)

    def value(self, o: Option) -> str:
        return f"{self.lead}{o.name}{self.close}{o.says}"

    def values(self) -> list:
        return [self.value(o) for o in self.all]

    def pick(self, value: str) -> str:
        """Значение ответа → имя варианта; чужое значение → запасной."""
        value = str(value or "").strip()
        return next((o.name for o in self.all if self.value(o) == value), self.fallback.name)

    def _check(self) -> None:
        if not self.lead.strip():
            raise ValueError("развилка без зачина: разгона к точке выбора нет")
        names = [o.name for o in self.all]
        if len(set(names)) != len(names):
            raise ValueError(f"имена вариантов повторяются: {names}")
        firsts = [n.split()[0].lower() if n.split() else "" for n in names]
        if len(set(firsts)) != len(firsts):
            raise ValueError(f"варианты не расходятся на первом слове: {names}")
        weak = [n for n, f in zip(names, firsts) if f in WEAK_WORDS or not f.isalpha()]
        if weak:
            raise ValueError(f"в точке расхождения служебное слово, а не направление: {weak}")
        for a in names:
            for b in names:
                if a != b and b.lower().startswith(a.lower()):
                    raise ValueError(f"имя «{a}» — начало имени «{b}»: точки расхождения нет")
        shortest = min(len(o.says) for o in self.options) if self.options else 0
        if len(self.fallback.says) * 2 < shortest:
            raise ValueError("запасной вариант заметно короче остальных — не той же формы")


class Choose(Form):
    """Одна развилка по ключу-разгону: `{key: enum}` → имя варианта."""

    def __init__(self, choice: Choice, key: str, name: str):
        self.choice, self.key, self.name = choice, key, name

    def schema(self, **kw):
        return ({"type": "object", "properties": {self.key: {"enum": self.choice.values()}},
                 "required": [self.key]}, self.name)

    def read(self, parsed, **kw):
        return self.choice.pick((parsed or {}).get(self.key, ""))


class ChooseEach(Form):
    """Развилка на каждое название ОДНИМ вызовом: ключи — сами названия, значения — `enum`
    из генератора (`Doc__prompting.md`: схема под живые данные, адресуемся названиями)."""

    def __init__(self, choice: Choice, name: str):
        self.choice, self.name = choice, name

    def schema(self, names=(), **kw):
        live = [str(n) for n in names]
        return ({"type": "object", "properties": {n: {"enum": self.choice.values()} for n in live},
                 "required": live}, self.name)

    def read(self, parsed, names=(), **kw):
        parsed = parsed or {}
        return {n: self.choice.pick(parsed.get(n, "")) for n in names}
