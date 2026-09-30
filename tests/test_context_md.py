# -*- coding: utf-8 -*-
"""Нормализация уровней заголовков в ответе модели.

Замысел — `__HQ/vision/Vision08__thread-distillate.md`. Скелет наш: `#` документ, `##` тема,
`###` секция; заголовкам модели остаётся четвёртый уровень, а в документных разделах — третий.
"""
from hermes_filetools.context_ctl import _md

NL = chr(10)


def _levels(text):
    return [h["level"] for h in _md.headings(text)]


# ── Что вообще считается заголовком ─────────────────────────────────────────

def test_a_hash_inside_a_fence_is_not_a_heading():
    """В питоне это комментарий, и трогать его нельзя ни в коем случае."""
    text = NL.join(["## настоящий", "", "```python", "# комментарий", "#### тоже нет", "```"])
    assert _levels(text) == [2]


def test_a_fence_of_four_backticks_is_respected():
    text = NL.join(["````", "### внутри", "````", "", "## снаружи"])
    assert _levels(text) == [2]


def test_an_indented_block_is_not_a_heading_either():
    assert _levels(NL.join(["    # под отступом", "", "## настоящий"])) == [2]


def test_a_hash_without_a_space_is_not_a_heading():
    assert _levels("#безпробела" + NL * 2 + "## настоящий") == [2]


def test_an_underlined_title_IS_a_heading():
    """Ручной сканер этого не видит вовсе — ради этого случая парсер и чужой."""
    found = _md.headings("Заголовок" + NL + "=========" + NL)
    assert found and found[0]["level"] == 1 and found[0]["setext"]


# ── Нормализация ────────────────────────────────────────────────────────────

def test_the_topmost_heading_becomes_the_floor_whatever_it_was():
    """Модель может начать и с первого уровня, и сразу с третьего."""
    for start in range(1, 4):
        text = "#" * start + " Заголовок"
        assert _md.normalize(text, 4) == "#### Заголовок", start


def test_relative_nesting_inside_the_answer_survives():
    text = NL.join(["## Первый", "текст", "### Внутри", "#### Глубже"])
    out = _md.normalize(text, 4).split(NL)
    assert out[0].startswith("#### ") and out[2].startswith("##### ")
    assert out[3].startswith("###### ")


def test_nothing_goes_deeper_than_markdown_allows():
    text = NL.join(["### Раз", "#### Два", "##### Три", "###### Четыре"])
    out = [line for line in _md.normalize(text, 4).split(NL) if line.startswith("#")]
    assert all(line.count("#") <= _md.MAX_LEVEL for line in out)
    assert out[-1].startswith("###### "), "схлопывание на шестёрке — нормально"


def test_a_text_without_headings_is_returned_byte_for_byte():
    """Не трогать лучше, чем переписать одинаково."""
    text = "просто проза" + NL * 2 + "```" + NL + "# не заголовок" + NL + "```"
    assert _md.normalize(text, 4) is not None
    assert _md.normalize(text, 4) == text


def test_the_body_around_a_heading_is_untouched():
    text = NL.join(["## Тема", "", "тело со ссылкой [x](y) и `кодом`", "", "```", "# c", "```"])
    out = _md.normalize(text, 4)
    assert "тело со ссылкой [x](y) и `кодом`" in out
    assert "# c" in out, "комментарий в заборе не тронут"
    assert out.count("```") == 2


def test_an_underlined_title_is_converted_not_shifted():
    """Подчёркивание уровня не несёт — его нечем сдвинуть, только переписать решётками."""
    out = _md.normalize("Заголовок" + NL + "=========" + NL + NL + "тело", 4)
    assert "#### Заголовок" in out
    assert "=========" not in out
    assert "тело" in out


# ── Запасной путь ───────────────────────────────────────────────────────────

def test_the_scanner_alone_still_handles_fences():
    """Парсер живёт в ИХ венве и однажды может оттуда исчезнуть."""
    text = NL.join(["## настоящий", "```", "### внутри забора", "```", "#### после"])
    assert [h["level"] for h in _md._by_scanner(text)] == [2, 4]


def test_a_missing_parser_leaves_a_trace(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_markdown(name, *a, **kw):
        if name == "markdown_it":
            raise ImportError("нет такого")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_markdown)
    assert _md._by_parser("## x") is None
    assert "markdown_it" in _md.PARSER_PROBLEM


# ── Как это ложится в документ ──────────────────────────────────────────────

def test_topic_sections_get_the_fourth_level_and_document_ones_the_third():
    from hermes_filetools.context_ctl import _distill
    # По два заголовка: одиночный считается подписью к ответу и снимается целиком.
    topic = {"title": "Тема", "class": "work",
             "settled": NL.join(["## Своё", "текст", "## Второе", "текст"])}
    doc = _distill.build_document(
        [topic], "",
        NL.join(["## В добавлениях", "т", "## Ещё", "т"]),
        NL.join(["## В запретах", "т", "## Ещё", "т"]))
    assert "#### Своё" in doc, "под нашей `###` секцией модель начинается с четвёртого"
    assert "### В добавлениях" in doc, "а документные разделы висят прямо под `##`"
    assert "### В запретах" in doc


def test_the_model_can_no_longer_pretend_to_be_a_topic():
    """Ради этого всё и затевалось: в оглавлении было двадцать две темы вместо двух."""
    from hermes_filetools.context_ctl import _distill
    topic = {"title": "Тема", "class": "work",
             "settled": NL.join(["## Проблема", "текст", "## Итог", "текст"])}
    doc = _distill.build_document([topic], "", "", "")
    tops = [line for line in doc.split(NL) if line.startswith("## ")]
    assert tops == ["## Тема"], tops


def test_a_lone_heading_is_still_stripped_as_a_caption():
    """Единственный заголовок — подпись к ответу, а она дублирует нашу же секцию."""
    from hermes_filetools.context_ctl import _distill
    assert _distill._strip_heading("# Тема: что-то" + NL * 2 + "тело") == "тело"


def test_but_a_structure_keeps_all_of_its_headings():
    """Прежнее правило снимало первый всегда: из `## Проблема … ## Итог` исчезала
    «Проблема», а «Итог» оставался — один из братьев пропадал без причины."""
    from hermes_filetools.context_ctl import _distill
    text = NL.join(["## Проблема", "текст", "", "## Итог", "текст"])
    assert _distill._strip_heading(text) == text


# ── Шов с их редакцией секретов ─────────────────────────────────────────────
# `ACHTUNG__foreign_seams.yaml::compact.summary_helpers_import`. Редакцию секретов мы НЕ
# копируем: наша копия отстала бы от их правил, а сводка живёт во всех последующих промптах.

def test_secret_redaction_is_imported_from_them_never_copied():
    from hermes_filetools.context_ctl import _render
    import inspect
    body = inspect.getsource(_render)
    assert "from agent.context_compressor import _redact_compaction_text" in body
    assert "IMPORT_PROBLEM" in body, "отказ импорта обязан оставлять след, а не молчать"


def test_a_missing_redactor_refuses_by_name_instead_of_passing_text_through():
    """Молчаливый обход здесь означал бы, что мы тихо перестали вычищать секреты."""
    from hermes_filetools.context_ctl import _render
    if _render.IMPORT_PROBLEM:
        import pytest
        pytest.skip("их модуль недоступен в этом окружении — шов и так разорван")
    import types
    broken = types.FunctionType(
        compile("def f(text):\n raise RuntimeError('нет их модуля')", "<t>", "exec")
        .co_consts[0], {})
    saved = _render._redact_compaction_text
    try:
        _render._redact_compaction_text = broken
        try:
            _render.render_row({"role": "user", "content": "секрет"})
        except RuntimeError as exc:
            assert "нет их модуля" in str(exc), "отказ обязан быть по имени"
        else:
            raise AssertionError("текст проехал мимо редакции секретов")
    finally:
        _render._redact_compaction_text = saved
