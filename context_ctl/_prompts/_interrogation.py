def q1_count() -> str:
    """Сколько тем. Вопрос выглядит лишним (число = длина будущего списка), но он даёт
    РАЗРЕШЕНИЕ на список из одного: без него модель, увидев просьбу дать массив, дотянет до
    двух-трёх, потому что форма просит множественного (довод владельца, 2026-09-21)."""
    C_Q1_COUNT = (
        "We are compressing the session above so the work can continue from a shorter "
        "context.\n\n"
        "First: how many independent topics does this session contain that deserve a "
        "separate pass of processing? Do not count examples, metaphors or asides as "
        "separate topics. Do not split one piece of work into its sub-aspects. One topic "
        "is a perfectly normal answer.\n\n"
        "Reply with JSON only: a single integer in the field {key}."
    )
    out__q1_count = C_Q1_COUNT.format(key=COUNT_KEY)
    return out__q1_count
COUNT_KEY = "how_many_independent_topics_the_work_zone_contains"
def s1_count() -> tuple:
    """Схема к q1_count. Целое — жёсткий запрет, модель не может ответить иначе."""
    C_S1_NAME = "filetools_topic_count"
    C_S1_COUNT2 = {"type": "object",
                   "properties": {COUNT_KEY: {"type": "integer"}},
                   "required": [COUNT_KEY]}
    out__s1_count = (C_S1_COUNT2, C_S1_NAME)
    return out__s1_count
def q2_titles(n: int) -> str:
    """Названия тем. Расхождение длины с `n` НЕ переспрашиваем — принимаем массив."""
    C_Q2_TITLES = (
        "You found {n} independent topic(s) in the session above.\n\n"
        "Write a short title for each one. These titles are how we will refer to the "
        "topics from here on, so make each recognisable on its own.\n\n"
        "If the number of titles you actually see is not {n}, write the array you see. Do "
        "not pad it and do not drop titles just to match {n}.\n\n"
        "Reply with JSON only: the field titles, an array of strings."
    )
    out__q2_titles = C_Q2_TITLES.format(n=n)
    return out__q2_titles
def s2_titles() -> tuple:
    C_S2_NAME = "filetools_topic_titles"
    C_S2_TITLES = {"type": "object",
                   "properties": {"titles": {"type": "array", "items": {"type": "string"}}},
                   "required": ["titles"]}
    out__s2_titles = (C_S2_TITLES, C_S2_NAME)
    return out__s2_titles
def q2_recount(n: int, titles_block: str) -> str:
    """Расхождение числа и списка — РАЗБИРАЕМ, а не принимаем молча.

    Живой прогон 2026-09-21 (владелец): на одних и тех же данных один прогон дал одну тему,
    другой насчитал одну и НАРЕЗАЛ много — отчёт раздулся на пустом месте. Прежнее правило
    «принимаем массив как есть» это пропускало.

    Спрашиваем только при `титулов > числа`: в эту сторону работает форма (просьба дать
    массив тянет к множественному числу, ради чего вопрос про число и заведён). В обратную
    сторону расхождение подозрительным не считаем — там модель просто успокоилась.

    Варианты `enum` — полными предложениями и одной формы: точка расхождения стоит в первом
    слове и НАЗЫВАЕТ направление (`Vision08`, механика энамок).
    """
    C_Q2_RECOUNT = (
        "A moment ago you counted {n} independent topic(s) in the session above. Then you "
        "wrote this list of titles:\n\n{titles}\n\n"
        "The two answers disagree: the list is longer than the count. Decide which one "
        "describes the session better.\n\n"
        "Reply with JSON only: the field verdict, one of the given options."
    )
    out__q2_recount = C_Q2_RECOUNT.format(n=n, titles=titles_block)
    return out__q2_recount
C_RECOUNT_MERGE = ("Oversplit: the longer list breaks one single piece of work into its "
                   "sub-aspects, and the smaller count was the honest one.")
C_RECOUNT_KEEP = ("Separate: the longer list names work that really is independent, and "
                  "the smaller count was an undercount.")
def s2_recount() -> tuple:
    C_S2R_NAME = "filetools_topic_recount"
    C_S2R = {"type": "object",
             "properties": {"verdict": {"enum": [C_RECOUNT_MERGE, C_RECOUNT_KEEP]}},
             "required": ["verdict"]}
    out__s2_recount = (C_S2R, C_S2R_NAME)
    return out__s2_recount
def recount_says_merge(verdict: str) -> bool:
    """Вердикт → решение кодом. Сравниваем по началу: вариант выбран целиком, но модель
    может вернуть его с чужими пробелами, а первое слово и есть точка расхождения."""
    out__recount_says_merge = str(verdict or "").strip().lower().startswith("oversplit")
    return out__recount_says_merge
def q2_titles_merge(n: int, titles_block: str) -> str:
    """Свести список к названному числу. Зовётся ТОЛЬКО после вердикта «нарезано слишком
    мелко» — иначе это была бы потеря тем по приказу, а не по суждению."""
    C_Q2_MERGE = (
        "You decided that the list below splits one piece of work into sub-aspects:\n\n"
        "{titles}\n\n"
        "Write the list again as {n} title(s), merging the parts that belong to the same "
        "piece of work. Keep a title recognisable on its own. Do not invent a topic that is "
        "not in the list above, and do not drop work — merge it into the title it belongs to."
        "\n\nReply with JSON only: the field titles, an array of strings."
    )
    out__q2_titles_merge = C_Q2_MERGE.format(n=n, titles=titles_block)
    return out__q2_titles_merge
def q3_split(titles_block: str) -> str:
    """Главные и филер ОДНИМ вопросом, двумя массивами.

    Заменил прежние два вопроса с индексами (Cursor-версия Vision08). Индексы модель
    сдвигает молча; названия она же и придумала шагом раньше — это её собственный словарь.
    Массив главных, а не одна главная: две равные работы обе получают полный разбор.
    """
    C_Q3_SPLIT = (
        "These are the topics you identified in the session above:\n\n{titles}\n\n"
        "We now split them. A MAIN topic gets a full breakdown, because the work continues "
        "there. A FILLER topic is worth one mention after compression, but not a full "
        "breakdown.\n\n"
        "Filler: examples told as illustration, stories about a different conversation, "
        "asides about the process itself.\n\n"
        "Not filler: a second piece of real work that was parked and meant to be resumed; "
        "a rejected design choice belonging to a main topic — that stays inside the main "
        "topic and is not a topic of its own.\n\n"
        "Reply with JSON only: main and filler, each an array of titles taken verbatim "
        "from the list above. A topic may appear in neither."
    )
    out__q3_split = C_Q3_SPLIT.format(titles=titles_block)
    return out__q3_split
def s3_split(titles) -> tuple:
    """Схема к q3_split. `enum` из ЖИВЫХ названий: выдумать тему физически нельзя.

    Гарантия ровно одна — принадлежность списку. Что выбор ПРАВИЛЬНЫЙ, не гарантирует
    ничто (`Vision06`: три формы ловушки из трёх провалились). Известная слепая зона.
    """
    C_S3_NAME = "filetools_topic_split"
    live = [str(t) for t in titles]
    C_S3_SPLIT = {
        "type": "object",
        "properties": {"main": {"type": "array", "items": {"enum": live}},
                       "filler": {"type": "array", "items": {"enum": live}}},
        "required": ["main", "filler"],
    }
    out__s3_split = (C_S3_SPLIT, C_S3_NAME)
    return out__s3_split
def topic_scope(title: str, index: int = 0, total: int = 0) -> str:
    """Преамбула ко всем вопросам разбора: где мы, что уже установлено, о чём спрашиваем.

    Переписана 2026-09-21 по замечанию владельца: прежняя начиналась прямо с «Consider only
    the topic…», то есть вопрос падал на модель без объяснения, чем мы заняты. Холодный
    клиент истории не имеет, и такую преамбулу надо давать КАЖДЫЙ раз.

    Оговорка честная только наполовину: данные перед моделью — вся лента, и «смотри только
    сюда» это просьба, а не ограничение. Проверить исполнение нечем (`Vision08`).
    """
    C_TOPIC_SCOPE = (
        "We are turning the session above into a distillate for the agent that will "
        "continue this work.\n\n"
        "You already found that the session consists of {total} separate topic(s), and we "
        "are working through them ONE AT A TIME so that nothing gets averaged away. Right "
        "now we need your answer about topic {index} of {total}, and about nothing else:"
        "{banner}"
        "The other topics get their own passes; do not describe them here and do not try "
        "to summarise the whole session. Speak only about this topic.\n\n"
    )
    C_TOPIC_SCOPE_BARE = (
        "We are turning the session above into a distillate for the agent that will "
        "continue this work, one topic at a time. Right now we need your answer about this "
        "topic only:{banner}"
        "Do not summarise the whole session; speak only about this topic.\n\n"
    )
    banner = topic_banner(title, index, total)
    out__topic_scope = (C_TOPIC_SCOPE.format(banner=banner, index=index, total=total)
                        if index and total
                        else C_TOPIC_SCOPE_BARE.format(banner=banner))
    return out__topic_scope
def q5_settled() -> str:
    C_Q5_SETTLED = (
        "What currently holds as true for this topic: the working goal, decisions that "
        "survived correction and disagreement, constraints that still apply. For every "
        "decision, give the reason behind it as well - a decision without its reason "
        "becomes dogma once the discussion it came from is gone.\n\n"
        "Who said what does not matter - the outcome does. Answer in prose, as fully as "
        "the transcript allows."
    )
    out__q5_settled = C_Q5_SETTLED + answer_style()
    return out__q5_settled
def q6_open() -> str:
    C_Q6_OPEN = (
        "What is still open on this topic: unfinished work, disagreement that never "
        "converged, errors without a fix, things the work was going to return to.\n\n"
        "Answer in prose. If something was left open without being named as such, say so."
    )
    out__q6_open = C_Q6_OPEN + answer_style()
    return out__q6_open
def q7_pack(kind: str, settled: str, open_text: str) -> str:
    """Пачка по классу ПОВЕРХ ядра — и она видит, что уже сказано.

    Подстановка прошлых ответов кодом обязательна: каждый вопрос это отдельный вызов, своих
    ответов модель не помнит. Это же чинит поломку прежнего Q9 (`Vision08`).
    """
    C_PACK_CODE = (
        "Which objects were in play - paths, commands, tests, services? What is built, "
        "what is broken, what was never verified? Which technical or runtime limits apply?")
    C_PACK_CANON = (
        "Which names, numbers and facts are already canon here? Which of them would be "
        "lost if someone paraphrased this topic? What is explicitly NOT canon yet?")
    C_PACK_PLAN = (
        "Which decisions were accepted, and WHY was each one taken? Which were rejected, "
        "why, and must not come back after compression? Which options were listed and left "
        "unchosen? Never give a decision without its reason: after compaction the "
        "discussion is gone, and a reasonless decision becomes dogma nobody can explain or "
        "undo.")
    # «НЕ ПОВТОРЯЙ СКАЗАННОЕ ВЫШЕ» — единственное лечение повтора, которое не возвращает
    # усреднение (2026-09-21). Разделы теперь складываются кодом и стоят рядом, поэтому
    # пересечение видно читателю; но убирать его сведением текстов нельзя — так мы и
    # потеряли две трети на прошлом устройстве.
    C_PACK_HEAD = ("Here is what you already established for this topic. Your answers are "
                   "kept side by side in the final document, so do NOT restate what is "
                   "below - add only what these questions did not cover.\n\n"
                   "SETTLED:\n{settled}\n\nOPEN:\n{open}\n\n")
    # Пачки под ВКУСЫ тем (2026-09-21). Старые имена оставлены рядом: они ещё могут приехать
    # из ответа, и падать на этом незачем.
    C_PACK_RECON = (
        "What did this investigation establish? Separate what was CONFIRMED by an "
        "observation - a run, a measurement, a file actually read - from what stayed a "
        "guess. Where was it checked, and what is still unknown? A hypothesis that was "
        "disproved is a result, not a failure: say which ones fell and why.")
    C_PACK_RULES = (
        "Which rules, preferences and prohibitions were stated here? Quote each one "
        "verbatim - a paraphrase of a rule is not the rule - and say what it applies to.")
    packs = {"work": C_PACK_CODE, "investigation": C_PACK_RECON, "recon": C_PACK_RECON, "design": C_PACK_PLAN,
             "rules": C_PACK_RULES, "general": C_PACK_CANON,
             "code": C_PACK_CODE, "canon": C_PACK_CANON, "plan": C_PACK_PLAN}
    out__q7_pack = C_PACK_HEAD.format(settled=frame_block("SETTLED", settled), open=frame_block("OPEN", open_text)) \
        + packs.get(str(kind or "").strip().lower(), C_PACK_CODE) + answer_style()
    return out__q7_pack
def q10_selfcheck(glue: str) -> str:
    """Самопроверка СОБРАННОГО РАЗДЕЛА — единственная проверка, которая у нас реально есть.

    Проверяем то, что поедет дальше: с отменой клея (2026-09-21) дальше едут сами ответы,
    склеенные кодом, — значит и спрашиваем про них.

    Заведомо ложная канарейка выброшена (ловит только грубую поломку). Владелец,
    2026-09-21: модели почти всегда ловят собственные выдумки, если спросить прямо.
    """
    # ПОРОГ ПОДНЯТ И ОТКАЗ РАЗРЕШЁН (владелец, 2026-09-22, по живому прогону: ответ был
    # `true` всегда). Кроме имени ключа этому мешало и отсутствие явного разрешения
    # ответить «нет»: вопрос звучал как поручение что-нибудь найти.
    C_Q10_SELFCHECK2 = (
        "Below is the distillate we assembled from your answers about the session "
        "above.\n\n{glue}\n\n"
        "Does it state anything the transcript does not support - a fact, a number, a "
        "name, a decision that is not actually there?\n\n"
        "Flag only what would MISLEAD the agent continuing this work: an invented fact, a "
        "wrong number, a decision nobody took. Condensing several statements into one is "
        "not a problem, and neither is wording tighter than the original, and neither is "
        "leaving something out.\n\n"
        "A section that is simply accurate is the normal case: answering false is not a "
        "failure to find anything, it is the honest report of a good section. Do not look "
        "for something to flag.\n\n"
        "Reply with JSON only: the boolean first, then what_exactly (a short list in one "
        "string, empty if there is nothing)."
    )
    out__q10_selfcheck = (C_Q10_SELFCHECK2.format(glue=frame_block("DISTILLATE", glue))
                          + lang_note())
    return out__q10_selfcheck
C_S10_KEY2 = "this_section_states_things_the_transcript_does_not_support"
def s10_selfcheck() -> tuple:
    """Булево ПЕРВЫМ полем: измерено, что булево модель держит намертво (`Vision06`)."""
    C_S10_NAME = "filetools_selfcheck"
    C_S10_SELFCHECK2 = {
        "type": "object",
        "properties": {C_S10_KEY2: {"type": "boolean"},
                       "what_exactly": {"type": "string"}},
        "required": [C_S10_KEY2],
    }
    out__s10_selfcheck = (C_S10_SELFCHECK2, C_S10_NAME)
    return out__s10_selfcheck
def selfcheck_key() -> str:
    """Имя ключа вердикта — одним местом на всех: схема, чтение ответа и тесты."""
    out__selfcheck_key = C_S10_KEY2
    return out__selfcheck_key
def q11_filler(titles_block: str) -> str:
    C_Q11_FILLER = (
        "These topics from the session above were set aside as filler:\n\n{titles}\n\n"
        "Write one short paragraph: enough that a reader knows they happened and what they "
        "were about. No settled/open/glue structure, no recap of the main work."
    )
    out__q11_filler = C_Q11_FILLER.format(titles=titles_block) + lang_note()
    return out__q11_filler
def q12_additions(document: str, known: str = "") -> str:
    """Добавления ПО ГОТОВОМУ ДОКУМЕНТУ — вместо открытого «что мы забыли».

    Ход владельца, 2026-09-21: любой конечный список вопросов молча теряет то, чего в нём
    нет, и потеря невидима. Спрашивать вслепую бесполезно; спрашивать по собранному тексту —
    работает, потому что перед моделью и лента, и результат.

    `known` — то, что самопроверка уже пометила. Живой прогон 2026-09-21: без этого модель
    добросовестно повторяла найденное самопроверкой первым же абзацем, и добавления наполовину
    состояли из уже сказанного.
    """
    C_Q12_KNOWN = ("Our own check already flagged this, and it is written in the document "
                   "above - do not repeat it, look for what it missed:\n{known}\n\n")
    C_Q12_ADDITIONS = (
        "Above is the session. Below is the document we produced from it together.\n\n"
        "{document}\n\n"
        "{known_block}"
        "Is anything MISSING or WRONG in it that would change what the next agent does?\n\n"
        "The bar is high on purpose. Report a fact that is absent and matters, or a "
        "statement that is factually wrong. Do NOT report wording that could be more "
        "precise, a nuance that could be added, a count that could be recounted, or an "
        "interpretation you would have phrased differently: this is a working input, not a "
        "text under review, and re-arguing its wording makes it worse, not better.\n\n"
        "A good document deserves false. That is the expected answer for a good document, "
        "not a failure to find something.\n\n"
        "Reply with JSON only: {key} (boolean) first, then text. If there is "
        "nothing to add, answer false and leave text empty - the closing line is written "
        "by us, not by you."
    )
    out__q12_additions = C_Q12_ADDITIONS.format(
        key=ADDITIONS_KEY,
        document=frame_block("DOCUMENT", document),
        known_block=C_Q12_KNOWN.format(known=known) if str(known or "").strip() else ""
    ) + lang_note()
    return out__q12_additions
ADDITIONS_KEY = "this_document_is_missing_or_gets_wrong_something_that_matters"
def s12_additions() -> tuple:
    C_S12_NAME = "filetools_additions"
    C_S12_ADDITIONS2 = {
        "type": "object",
        "properties": {ADDITIONS_KEY: {"type": "boolean"}, "text": {"type": "string"}},
        "required": [ADDITIONS_KEY],
    }
    out__s12_additions = (C_S12_ADDITIONS2, C_S12_NAME)
    return out__s12_additions
# ── Правка 2026-09-21 (вторая): ворота, стиль ответа, рамки ──────────────────
# Замечания владельца по живому отчёту, все четыре структурные:
#   — секция не создаётся, если материала нет: сперва да/нет, потом вопрос;
#   — «сжато, но полно»: без объяснений и без захода на чужие вопросы;
#   — данные ВСЕГДА в понятной рамке, и встроенные куски тоже;
#   — имя темы выделено переносами, чтобы читалось как важное.


def answer_style() -> str:
    """Общий хвост ко всем вопросам разбора. Без него модель объясняет, а не отвечает."""
    # ЯЗЫК И ЗАЧИН — В ХВОСТЕ КАЖДОГО ВОПРОСА (живой прогон 2026-09-21). Правило языка стояло
    # только в системном промте, и два раздела из шести вернулись по-английски: вопрос
    # английский и стоит последним, а побеждает ближнее. Туда же запрет на преамбулу —
    # «Here is the distillate for Topic 1 of 1» это потраченная строка и чужой голос.
    C_ANSWER_STYLE2 = (
        "\n\nAnswer compactly but completely: facts, not explanations. Do not justify your "
        "reasoning and do not describe what you are about to do. Other aspects of this "
        "topic are covered by other questions - stay strictly on this one.\n\n"
        "Write your answer IN THE LANGUAGE OF THE CONVERSATION, not in the language of this "
        "question. This question is in English for precision; your answer belongs to the "
        "conversation and must read as part of it.\n\n"
        "Begin with the content itself. No opening sentence announcing what you are about "
        "to write, no heading of your own, no sign-off.\n\n"
        "Where a tool output is cut short, you know the call was made; you do NOT know "
        "what it printed. Never fill that in."
    )
    out__answer_style = C_ANSWER_STYLE2
    return out__answer_style
def lang_note() -> str:
    """Короткая приписка про язык — для вопросов, которым полный `answer_style` не нужен.

    Живой прогон 2026-09-21: филер и самопроверка вернулись по-английски, потому что хвост
    со стилем к ним не приклеивался, а их тексты попадают в документ наравне со всеми.
    Побеждает ближнее к ответу — значит правило должно стоять у каждого ответа, который
    человек потом читает.
    """
    C_LANG_NOTE = ("\n\nWrite in the language of the conversation, not in the language of "
                   "this question: your text goes into the distillate and must read as part "
                   "of it.")
    out__lang_note = C_LANG_NOTE
    return out__lang_note
def frame_block(name: str, body: str) -> str:
    """Встроенный кусок в рамке — тот же приём, что у блока данных, и по той же причине.

    Без рамки подставленный текст сливается с вопросом: модель не отличает, где кончается
    то, что она уже написала, и где начинается то, о чём её спрашивают.
    """
    C_FRAME_HEAD = "---------- BEGIN {name} ----------"
    C_FRAME_TAIL = "---------- END {name} ----------"
    out__frame_block = (C_FRAME_HEAD.format(name=name) + "\n" + str(body or "").strip()
                        + "\n" + C_FRAME_TAIL.format(name=name))
    return out__frame_block
def topic_banner(title: str, index: int = 0, total: int = 0) -> str:
    """Имя темы отдельным блоком с переносами: структурно это самое важное в вопросе."""
    C_TOPIC_BANNER = (
        "\n"
        "========================================\n"
        "  TOPIC{pos}: {title}\n"
        "========================================\n"
        "\n"
    )
    pos = f" {index} OF {total}" if index and total else ""
    out__topic_banner = C_TOPIC_BANNER.format(title=title, pos=pos)
    return out__topic_banner
def q_gate(title: str, index: int = 0, total: int = 0, fields=None) -> str:
    """ВОРОТА: какие секции вообще есть смысл заполнять по этой теме.

    Замысел владельца 2026-09-21. Дешёвые булевы (измерено: держатся намертво) решают, какие
    дорогие вопросы задавать вовсе. Секция, на которую пришло `false`, не создаётся и не
    спрашивается — вместо «в ленте таких сведений нет», которое через компакцию читается
    как содержание.
    """
    C_Q_GATE = (
        "We are turning the session above into a distillate, one topic at a time. Before "
        "we ask you to write anything, we need to know which sections are worth writing "
        "at all for this topic:"
        "{banner}"
        "For each section below, answer whether the transcript actually contains material "
        "for it ON THIS TOPIC. Answer false freely: a false simply means we will not create "
        "that section and will not ask you about it. Do not answer true out of politeness.\n\n"
        "{fields}\n"
        "Reply with JSON only: one boolean per field above."
    )
    # Поля — те, что у опросника под воротами (`Plan32.1`): нейтральный набор не спрашивает
    # про ошибки. Не указаны — все пять, как прежде.
    names = list(fields) if fields else C_GATE_FIELDS
    fields = "".join(f"  {gate_field(n)} - {C_GATE_MEANING[n]}\n" for n in names)
    out__q_gate = C_Q_GATE.format(banner=topic_banner(title, index, total), fields=fields)
    return out__q_gate
C_GATE_FIELDS = ["pending_task", "open_items", "errors_fixes",
                 "critical_values", "class_details"]
C_GATE_FIELDS2 = [
    "the_user_asked_for_something_and_did_not_get_it",
    "something_in_this_topic_is_still_open",
    "something_went_wrong_here_and_was_dealt_with",
    "this_topic_carries_exact_values_worth_keeping",
    "this_topic_has_details_of_its_own_kind_worth_writing",
]
# Что значит каждое поле — для текста вопроса; ключ в тексте тот же, что в схеме.
C_GATE_MEANING = {
    "pending_task": "a request, question or decision from the user that was never fulfilled, "
                    "or (if there is no user) an objective the work was heading towards;",
    "open_items": "unfinished work, unresolved disagreement, things to return to;",
    "errors_fixes": "errors that were hit, and how they were resolved;",
    "critical_values": "exact values, paths, numbers, error texts, configuration that would "
                       "be lost if someone paraphrased this topic;",
    "class_details": "substantial material for the extra pack chosen for this topic.",
}
# Старое имя → новое: код читает ворота по СМЫСЛУ поля, а не по его написанию.
C_GATE_RENAMED = dict(zip(C_GATE_FIELDS, C_GATE_FIELDS2))
def gate_field(name: str) -> str:
    """Смысловое имя поля ворот → ключ в схеме. Одно место на схему, чтение и тесты."""
    out__gate_field = C_GATE_RENAMED.get(name, name)
    return out__gate_field
def s_gate(fields=None) -> tuple:
    """Схема ворот. Все поля обязательны: необязательные модель обрабатывает плохо, и
    молчание нельзя отличить от «нет» (`Vision06`)."""
    C_S_GATE_NAME = "filetools_sections_gate"
    keys = [gate_field(n) for n in fields] if fields else list(C_GATE_FIELDS2)
    C_S_GATE2 = {
        "type": "object",
        "properties": {f: {"type": "boolean"} for f in keys},
        "required": list(keys),
    }
    out__s_gate = (C_S_GATE2, C_S_GATE_NAME)
    return out__s_gate
def q_pending_task() -> str:
    """Невыполненная просьба человека — ДОСЛОВНО. Их «самое важное поле», и оно у нас
    отсутствовало: «пришли» говорит про итоги, а не про то, чего человек не получил.

    Их формулировка оплачена их же граблями и потому взята почти целиком: разговор, где
    человек просто задал вопрос, — это тоже активная задача; отдельно ловится ОБРАТНЫЙ
    СИГНАЛ (стоп, откати, неважно), который отменяет прежнюю работу.
    """
    C_Q_PENDING_TASK = (
        "What did the user ask for and not get, on this topic? Quote their words verbatim - "
        "the exact wording, not your rendering of it.\n\n"
        "A conversation where the user merely asked a question IS an unfulfilled request: "
        "the task is to answer it. If several things are outstanding, list only those not "
        "yet done.\n\n"
        "If the user's latest message was a reverse signal - stop, undo, roll back, never "
        "mind, just check this instead - quote that verbatim and say that the earlier work "
        "is cancelled; do not carry the cancelled task forward.\n\n"
        "If this session has no user turns, state the objective the work itself was heading "
        "towards, and do not invent a user."
    )
    out__q_pending_task = C_Q_PENDING_TASK + answer_style()
    return out__q_pending_task
def q_errors_fixes() -> str:
    """Ошибки и чем их починили. «Открыто» ловит только НЕпочиненные — вместе с починенными
    пропадает урок, а он и есть самое дорогое в отладке."""
    C_Q_ERRORS_FIXES = (
        "Which errors were hit on this topic, and how was each resolved? Give the error "
        "text as it appeared, not a description of it.\n\n"
        "Pay separate attention to corrections the USER made: quote the correction in their "
        "words and say what changed because of it."
    )
    out__q_errors_fixes = C_Q_ERRORS_FIXES + answer_style()
    return out__q_errors_fixes
def q_critical_values() -> str:
    """Точные значения. У них это отдельная секция для всей сводки; у нас было только у
    одного класса — а числа теряются одинаково во всех."""
    C_Q_CRITICAL_VALUES = (
        "Which exact values must survive on this topic: numbers, paths, names, commands, "
        "versions, error texts, configuration - anything that a paraphrase would round off "
        "or drop.\n\n"
        "List them as they appear. Never write out API keys, tokens, passwords or "
        "credentials - write [REDACTED] instead."
    )
    out__q_critical_values = C_Q_CRITICAL_VALUES + answer_style()
    return out__q_critical_values
def q_constraints_verbatim() -> str:
    """Запреты и ограничения — ДОСЛОВНО, один раз на всю сессию.

    Vision08 (форма результата): пересказ убивает именно формулировку, а она критична ровно
    там, где человек что-то запретил. Их промпт требует того же и по той же причине.
    """
    C_Q_CONSTRAINTS = (
        "Across the whole session above: what did the user forbid, require, or insist on? "
        "Rules about how to work, what not to touch, what must always be done.\n\n"
        "Quote each one VERBATIM - a paraphrase of a prohibition is not a prohibition. Say "
        "nothing about topics, decisions or progress; only standing rules.\n\n"
        "Reply with JSON only: {key} (boolean) first, then text. Answer false if "
        "the user stated no rules - we will simply not create that section."
    )
    out__q_constraints_verbatim = C_Q_CONSTRAINTS.format(key=CONSTRAINTS_KEY)
    return out__q_constraints_verbatim
CONSTRAINTS_KEY = "the_user_stated_a_rule_or_a_ban_somewhere_in_this_session"
def s_constraints_verbatim() -> tuple:
    C_S_CONSTRAINTS_NAME = "filetools_constraints"
    C_S_CONSTRAINTS2 = {
        "type": "object",
        "properties": {CONSTRAINTS_KEY: {"type": "boolean"}, "text": {"type": "string"}},
        "required": [CONSTRAINTS_KEY],
    }
    out__s_constraints_verbatim = (C_S_CONSTRAINTS2, C_S_CONSTRAINTS_NAME)
    return out__s_constraints_verbatim
# ── Вкус темы: классификация внутри кодовой области (2026-09-21) ─────────────
# Глобальный классификатор («это вообще код, книга или разбор данных?») приедет СВЕРХУ
# отдельным шагом — теми же функциями, другими константами. Здесь ось внутри кодовой
# тематики: чем эта конкретная тема была, и на каком языке про неё спрашивать.
#
# ЗАЧЕМ ЭТО ВООБЩЕ: замысел владельца 2026-09-21, по её живому случаю. Их компактор
# уничтожил сессию про создание мира, потому что у него нет классификации вовсе — есть
# жёсткий шаблон под код, и всё, что в него не влезло, пропало. Вопрос «какие ошибки были и
# чем починили» в разговоре про Формулу-1 не просто пустой: технический словарь разворачивает
# модель в техническую рамку, и она начинает пересказывать разговор как отладку.
#
# ВКУС И ФИЛЕР — РАЗНЫЕ ОСИ. Филер это решение МАРШРУТА (полный разбор или одно упоминание),
# вкус — решение о ЯЗЫКЕ. Разговор, к которому возвращались трижды, заслуживает полного
# разбора, но человеческими вопросами.

def kind_work() -> str:
    # ВЫБОР ПАДАЕТ В ТОЧКУ РАСХОЖДЕНИЯ (владелец, 2026-09-21). Грамматика режет токен за
    # токеном и спокойно ветвится: одинаковый зачин у вариантов НЕ ошибка, решение просто
    # переезжает туда, где они расходятся. Ошибка была в другом — в этой точке у прежних
    # формулировок стояли `hands`, `about`, `none`: служебные слова, которые ничего не
    # называют и потому конкурируют вслепую. Мы писали фразы, не думая про грамматику.
    # Теперь расхождение приходится на сильное существительное, называющее направление.
    C_KIND_WORK2 = ("Work on the system: writing, changing or fixing something that runs.")
    out__kind_work = C_KIND_WORK2
    return out__kind_work
def kind_recon() -> str:
    C_KIND_RECON2 = ("Investigation: finding out how something already works, by reading, "
                     "measuring or experimenting.")
    out__kind_recon = C_KIND_RECON2
    return out__kind_recon
def kind_design() -> str:
    # «…в системе или в работе» (2026-10-11): без этого «как устроен выдуманный мир» тоже
    # выглядело как проектирование, и тема ехала в технические вопросы.
    C_KIND_DESIGN2 = ("Design: weighing options and settling on how something in the system "
                      "or the work should be done.")
    out__kind_design = C_KIND_DESIGN2
    return out__kind_design
def kind_rules() -> str:
    C_KIND_RULES2 = ("Rules: preferences and prohibitions the user stated about how we "
                     "work together.")
    out__kind_rules = C_KIND_RULES2
    return out__kind_rules
def kind_talk() -> str:
    C_KIND_TALK2 = ("Conversation outside the work: life, examples, digressions, nothing "
                    "about the system.")
    out__kind_talk = C_KIND_TALK2
    return out__kind_talk
def kind_general() -> str:
    """НЕЙТРАЛЬНЫЙ НАБОР — он же запасной (владелец, 2026-10-11, по живому случаю: сессию, где
    придумывали мир — наклон оси планеты, религию от «ока» в небе, — их компактор опросил как
    код). Всё, что не работа над системой, спрашивается простыми словами, без инженерного словаря.

    Запасной вариант ТОЙ ЖЕ ФОРМЫ И ДЛИНЫ, что остальные (`Vision06`, владелец 2026-09-21):
    модель берёт то, что больше похоже на ФОРМУ запрошенного, и короткий отказ среди длинных
    описаний не выигрывает никогда. Первое слово — сильное, как у остальных.
    """
    C_KIND_GENERAL = ("General: anything that is not work on a system - a story, a made-up "
                      "world, a plan or an idea being developed; asked in plain words, not as "
                      "engineering.")
    out__kind_general = C_KIND_GENERAL
    return out__kind_general
def kind_brief() -> str:
    """Короткий опросник (`Plan32.1`): небольшой простой обмен, где хватает «чем кончилось» и
    «что открыто» — владелец: «на 20 простых сообщениях нет смысла прогонять весь цикл»."""
    C_KIND_BRIEF = ("Brief: a small, simple exchange where it is enough to say where it landed "
                    "and what is still open.")
    out__kind_brief = C_KIND_BRIEF
    return out__kind_brief
def kind_free() -> str:
    """ВЫХОД ИЗ МЕНЮ (`Plan34`, владелец 2026-10-14: «как в телефонном разговоре — ни один вариант,
    соедините с оператором»): ни один набор не подошёл — агент сам говорит, что из темы должно
    остаться. По умолчанию ВЫКЛЮЧЕН (`compact_sets_off`) — включается галочкой. Той же формы и
    длины, что остальные описания; первое слово сильное."""
    C_KIND_FREE = ("Free: none of the sets above fits this topic - I say in my own words what of "
                   "it must survive, in the form that suits it.")
    # Слово `freeform` и описание БЕЗ отрицания (владелец, 2026-10-15): вариант читается целиком —
    # «the set that fits this topic best is (free): none of the sets fits» спорил сам с собой и
    # звучал отказом; `free` двусмысленно («бесплатный», «пустой»).
    C_KIND_FREE2 = ("Freeform: no fixed questions - I say in my own words what of this topic must "
                    "survive, in the form that suits it.")
    out__kind_free = C_KIND_FREE2
    return out__kind_free
def q_free() -> str:
    """Единственный вопрос набора `free`. Без общего `answer_style`: тот запрещает свои заголовки
    и говорит «другие аспекты — в других вопросах», а здесь других вопросов нет — ответ и есть вся
    тема. Язык, зачин и обрезанный вывод тулов — те же правила, что у всех."""
    C_Q_FREE = (
        "None of the sets of questions fitted this topic, so there is no list of questions for "
        "it. Say yourself what of this topic you will need in order to go on with the work - in "
        "the form that suits it: your own subheadings, your own order.\n\n"
        "Keep what was settled and why, what is still open or unfinished, and the exact names, "
        "numbers and wording that a retelling would lose. Leave out what no longer matters. "
        "Answer as fully as the transcript allows: this answer is all that will remain of the "
        "topic."
    )
    C_Q_FREE_TAIL = (
        "\n\nBegin with the content itself: no opening sentence announcing what you are about "
        "to write, no sign-off.\n\n"
        "Where a tool output is cut short, you know the call was made; you do NOT know what it "
        "printed. Never fill that in."
    )
    out__q_free = C_Q_FREE + lang_note() + C_Q_FREE_TAIL
    return out__q_free
def q_which_set(titles_block: str, menu: str, exit_word: str = "") -> str:
    """УЗЕЛ ВЫБОРА ОПРОСНИКА (`Plan32.1`, замысел владельца 2026-10-11): модели показываем ВСЕ
    наборы вопросов, которые можем задать, — что каждый спросит, — и она выбирает набор для
    каждой темы. Заменил `q_kinds`: тот выбирал «вид темы», и от вида зависела одна пачка;
    теперь выбор — это рельсы, по которым пойдёт весь опрос темы.

    Варианты ответа строит генератор (`_scenario.Choice`): фраза-разгон, ведущая по токенам до
    имени набора, — `which_set_lead()`.
    """
    C_Q_WHICH_SET = (
        "We are turning the session above into a distillate, and we ask about each topic "
        "separately. Before we do, we need to choose WHICH SET OF QUESTIONS to ask about each "
        "topic, because the questions worth asking differ: what makes sense for work on a "
        "system is useless, even harmful, for a conversation that was not about the system "
        "at all, and a small simple exchange does not need the full set.\n\n"
        "These are the sets we can ask. Each one says what it is for and lists what it will "
        "ask:\n\n{menu}\n\n"
        "These are the topics you identified:\n\n{titles}\n\n"
        "For each topic, weigh the sets against what the topic actually contains and choose "
        "the ONE set whose questions fit it best. Choose the last option if none of them "
        "really fits - that is a normal answer, not a failure.\n\n"
        "Reply with JSON only: one field per topic title, its value one of the given "
        "options, copied exactly."
    )
    # Выход из меню (`free`, `Plan34`) в меню — «ни один не подходит» указывает на него, а не на
    # последний вариант (тот — запасной `general`, обычный нейтральный набор). Нет — текст прежний.
    C_WHICH_SET_LAST = "Choose the last option if none of them really fits"
    C_WHICH_SET_EXIT = "Choose ({exit}) if none of the other sets really fits"
    text = C_Q_WHICH_SET.replace(C_WHICH_SET_LAST, C_WHICH_SET_EXIT.format(exit=exit_word)) \
        if exit_word else C_Q_WHICH_SET
    out__q_which_set = text.format(titles=titles_block, menu=menu)
    return out__q_which_set
def which_set_lead() -> str:
    """Зачин каждого варианта выбора набора: общий разгон, точка расхождения — имя набора в
    скобке (владелец: «оценив все вопросы, я считаю, что лучше всего подойдёт набор (название»)."""
    C_WHICH_SET_LEAD = "Having weighed every set of questions above, the set that fits this topic best is ("
    out__which_set_lead = C_WHICH_SET_LEAD
    return out__which_set_lead
def q_talk() -> str:
    """Разговор вне работы. ОДИН вопрос и ни одного технического слова.

    Замысел владельца 2026-09-21: если человек с агентом заговорили про жизнь или про
    Формулу-1, спрашивать там про файлы и ошибки вредно — модель начнёт пересказывать
    болтовню как отладку. Но и терять это нельзя: разговор бывает важнее работы.
    """
    C_Q_TALK = (
        "This topic was not about the work. Say what it was about and what came of it - a "
        "conclusion, an opinion they settled on, something they agreed about, or simply "
        "that it passed without a conclusion.\n\n"
        "Do not look for tasks, errors or files here: there are none, and inventing them "
        "would distort what actually happened."
    )
    out__q_talk = C_Q_TALK + answer_style()
    return out__q_talk
