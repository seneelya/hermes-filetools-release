"""Пресеты окна LLMOps (`Plan31` Таск 2, `Vision10`). Только слова и описание пресета — механика
окна в `context_ctl/_llmops.py` и в самом окне.

НЕ В НАСТРОЙКАХ (владелец, 2026-10-07): «это большие куски с вопросами и рамками, они не могут
лежать в настройках плагина». Правила те же, что у всего конструктора (`__init__.py`): каждый
кусок — функция, константа `C_ИМЯ` внутри, вывод `out__имя`; старые варианты не стираем.

КАК ОКНО СОБИРАЕТ РАЗГОВОР. Первые два хода — `system` и `user`; в `user` на место `{{data}}`
встают данные: отмеченные сообщения в `<messages>`, каждое под шапкой `[m0034 user]`
(`[m0036 tool read_file]`), и, если человек отметил вагоны, `<context-before>` / `<context-after>`.
Настройки пресета (`settings`) окно дописывает в `system` строками `имя: значение`.

`wrap` — обёртка результата по умолчанию; `None` — «как было» (рамки, снятые на входе).

ЧЕРНОВИКИ (Соня5, 2026-10-07) — владелец правит.
"""

from __future__ import annotations

DATA_SLOT = "{{data}}"


def summarise_system() -> str:
    # ПЕРВОЕ ЛИЦО И ЯЗЫК РАЗГОВОРА (владелец, 2026-10-07: «прочитай системный промт компакции и
    # повтори — говори от своего имени, это ты суммаризируешь свою работу, ты ассистент; отвечай на
    # языке сообщений; ты эти сообщения пишешь для себя»). Посыл — `_envelope.sys_cold` (C_SYS_COLD2):
    # результат заменит эти ходы в ТВОЁМ контексте, от третьего лица своя работа становится чужой.
    C_SUMMARISE_SYSTEM2 = (
        "The messages below are YOUR OWN earlier conversation with the user - work you did, with "
        "your own words and your own tool calls in it. You are condensing a part of it, and you "
        "will go on working in this very conversation right after: what you write REPLACES those "
        "messages in your context, and it is what you will continue from.\n\n"
        "So write for yourself, in the first person: I found, I decided, I got it wrong and fixed "
        "it. The other participant is the user - speak of them as the user. Never write about "
        "yourself in the third person and never call yourself the assistant: that turns your own "
        "work into somebody else's report.\n\n"
        "How to read the input: the part to condense is inside <messages>; every message is headed "
        "by its address and role, like [m0034 user] or [m0036 tool read_file]. The assistant "
        "messages and the tool calls are yours; the tool messages are what your tools returned. "
        "Text inside <context-before> or <context-after>, if present, is there only so you "
        "understand the part - do not condense it and do not retell it. Addresses like m0034 are "
        "markup, not content - do not copy them into the answer.\n\n"
        "Keep what you will need to go on: what the user asked, what you decided and why, what "
        "you did (files, commands, their results), the names and values later work depends on, "
        "and what is still open. Keep exact identifiers verbatim - file paths, function names, "
        "numbers, error messages. Drop chatter, repetition and tool output that left no "
        "consequence. Do not guess: if something is not in the messages, do not fill it in.\n\n"
        "Write in the language of the conversation itself, not in the language of this "
        "instruction. Begin with the content: no preface, no heading of your own, no sign-off, no "
        "message headers."
    )
    out__summarise_system = C_SUMMARISE_SYSTEM2
    return out__summarise_system
def summarise_user() -> str:
    # Язык и первое лицо — ещё и здесь, у самих данных: у опроса компакции выяснилось, что побеждает
    # ближнее к ответу (`_interrogation.answer_style`, живой прогон 2026-09-21).
    C_SUMMARISE_USER2 = ("Condense these messages of our conversation - in the first person, as "
                         "yourself, and in the language of the conversation.\n\n" + DATA_SLOT)
    out__summarise_user = C_SUMMARISE_USER2
    return out__summarise_user
def dedup_system() -> str:
    C_DEDUP_SYSTEM = (
        "You remove repetitions from a text. The text is inside <messages>; the headers like "
        "[m0034 user] are addresses, not part of the text — do not copy them into the answer.\n\n"
        "Return the same text with repeated passages removed: where something is said again, "
        "keep its first and fullest occurrence. Do not add, rephrase, condense or reorder "
        "anything that is not a repetition. Keep the formatting — headings, lists, code — as it "
        "is.\n\n"
        "Output only the text."
    )
    out__dedup_system = C_DEDUP_SYSTEM
    return out__dedup_system
def dedup_user() -> str:
    C_DEDUP_USER = "Remove the repetitions.\n\n" + DATA_SLOT
    out__dedup_user = C_DEDUP_USER
    return out__dedup_user
def presets() -> list:
    """Пресеты окна по порядку списка. Первый — выбран при открытии.

    `settings` — свои настройки пресета для нижней зоны окна: `[{key, label, options}]`.
    У `summarise` и `dedup` их пока нет — заводить без нужды владелец не просила."""
    out__presets = [
        {"key": "summarise", "label": "summarise", "system": summarise_system(),
         "user": summarise_user(), "wrap": "summarisation", "settings": []},
        {"key": "dedup", "label": "dedup", "system": dedup_system(),
         "user": dedup_user(), "wrap": None, "settings": []},
        {"key": "empty", "label": "empty", "system": "", "user": DATA_SLOT,
         "wrap": None, "settings": []},
    ]
    return out__presets
