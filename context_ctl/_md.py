"""Уровни заголовков в ответе модели: нормализация под наш скелет.

ЗАЧЕМ. Документ собирает КОД, и скелет у него наш: `#` документ, `##` тема, `###` секция.
Модель же внутри своего ответа пишет собственные `##` и `###` — и они встают на наши уровни.
Живой прогон 2026-09-22: в документе 36 заголовков, из них наших десяток, а два десятка
чужих сидели на уровне тем и секций. Оглавление показывало двадцать две темы вместо двух;
структура не портилась, она ВРАЛА.

ПОЧЕМУ НОРМАЛИЗАЦИЯ, А НЕ СДВИГ НА ФИКСИРОВАННОЕ ЧИСЛО (владелец): модель может начать сразу
с третьего уровня или с первого. Смещаем так, чтобы САМЫЙ ВЕРХНИЙ заголовок ответа стал
`####`, а относительная вложенность внутри ответа сохранилась.

ПОЧЕМУ КОДОМ, А НЕ ПРОСЬБОЙ. Владелец, 2026-09-22: просьба о формате — лишняя нагрузка на
модель, а гарантию она всё равно не даёт. Код даёт.

ПОЧЕМУ ЧУЖОЙ ПАРСЕР. Заголовок markdown — не «строка, начинающаяся с решётки». Не считаются:
решётка внутри блока кода (в питоне это комментарий), внутри забора любой длины, под отступом
в четыре пробела, без пробела после решётки. Зато СЧИТАЕТСЯ строка, подчёркнутая `===` снизу,
— и её ручной сканер не увидел бы вовсе. Всё это `markdown_it` уже умеет, и даёт номера строк:
значит правим строки НА МЕСТЕ, а не гоняем текст через дерево и обратно.

Их венв, не наш, — поэтому импорт ленивый и с запасным сканером, как у `strip_think_blocks`.
След отказа остаётся в `PARSER_PROBLEM`: молча упавший на свой сканер модуль искать негде.
"""
from __future__ import annotations

import re

# Наш скелет занимает три уровня, ответу модели остаётся четвёртый и ниже.
FLOOR = 4
# Глубже markdown не умеет. Схлопывание на шестёрке владелец признала нормальным: вещи на
# этом уровне часто живут наравне с родителем.
MAX_LEVEL = 6

PARSER_PROBLEM = ""

_ATX = re.compile(r"^(#{1,6})(\s+)(.*)$")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _by_parser(text: str):
    """Заголовки через `markdown_it`. `None` — парсера нет, причина в `PARSER_PROBLEM`."""
    global PARSER_PROBLEM
    try:
        from markdown_it import MarkdownIt
    except Exception as exc:  # noqa: BLE001
        PARSER_PROBLEM = f"markdown_it: {exc}"
        return None
    found = []
    for token in MarkdownIt("commonmark").parse(text):
        if token.type != "heading_open":
            continue
        start, stop = token.map
        level = int(token.tag[1:])
        # `markup` у setext-заголовка это `=` или `-`, у обычного — сами решётки.
        setext = set(token.markup) <= {"=", "-"}
        found.append({"line": start, "stop": stop, "level": level, "setext": setext})
    return found


def _by_scanner(text: str):
    """Запасной сканер: только решёточные заголовки, зато с учётом заборов.

    Setext здесь НЕ ловится — честная потеря, и ровно поэтому основной путь чужой.
    """
    found, fence = [], ""
    for i, line in enumerate(text.split(chr(10))):
        m = _FENCE.match(line)
        if m:
            mark = m.group(1)
            if not fence:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence):
                fence = ""
            continue
        if fence or line.startswith("    "):
            continue
        m = _ATX.match(line)
        if m:
            found.append({"line": i, "stop": i + 1, "level": len(m.group(1)),
                          "setext": False})
    return found


def headings(text: str) -> list:
    """Заголовки текста: `[{line, stop, level, setext}]`, строки нумеруются с нуля."""
    found = _by_parser(text)
    return _by_scanner(text) if found is None else found


def normalize(text: str, floor: int = FLOOR) -> str:
    """Опустить заголовки ответа так, чтобы верхний стал `floor`-м уровнем.

    Текст, в котором заголовков нет, возвращается БАЙТ В БАЙТ: не трогать лучше, чем
    переписать одинаково.
    """
    body = str(text or "")
    found = headings(body)
    if not found:
        return body
    shift = floor - min(h["level"] for h in found)
    if shift == 0 and not any(h["setext"] for h in found):
        return body

    lines = body.split(chr(10))
    drop = set()
    for h in found:
        level = min(MAX_LEVEL, max(1, h["level"] + shift))
        i = h["line"]
        if h["setext"]:
            # Подчёркивание уровня не несёт: превращаем в решёточный и убираем черту.
            lines[i] = "#" * level + " " + lines[i].strip()
            drop.update(range(i + 1, h["stop"]))
            continue
        m = _ATX.match(lines[i])
        if m:
            lines[i] = "#" * level + m.group(2) + m.group(3)
    return chr(10).join(line for i, line in enumerate(lines) if i not in drop)
