"""Тексты промптов и вопросов — КОНСТРУКТОР. Здесь только слова, ни капли механики.

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ И ПОЧЕМУ КАЖДЫЙ ТЕКСТ — ФУНКЦИЯ. У Hermes все формулировки размазаны
по четырёхтысячному `context_compressor.py`, и найти нужную можно только чтением. Мы так не
хотим, и дело не во вкусе: питон склеивает подряд идущие КОНСТАНТЫ в одну ленту, и
`get_codeblock --outline` показывает их одной строкой — навигировать нечем. ФУНКЦИИ он
нумерует поимённо.

Поэтому здесь каждый кусок промпта — функция без аргументов, возвращающая строку. Цена —
пара лишних скобок при вызове. Выгода — оглавление файла одной командой:

    get_codeblock.py --file context_ctl/_prompts.py --outline

и в другой сессии нужный текст берётся по имени, без чтения соседних.

ЧТО ЗДЕСЬ НЕ ЛЕЖИТ. Их дословная копия (`_compact_prompt.py`) — она отдельно и намеренно:
её ценность в побайтовом совпадении с оригиналом, расхождение ловится диффом. Отсюда на неё
только ссылаются (`frame_prefix`, `frame_end`), а текст не дублируют.

ПРАВИЛО ЯЗЫКА (владелец, 2026-09-17): директивная часть — по-английски, содержательная — нет.
Английский это язык буквального исполнения инструкции; на не-доминирующем в обучении языке
модель меньше катится по заученным шаблонам. ПОМЕТКА: на сравнении это НЕ проверено, и
первое измерение легло против (см. `Vision06`).
"""

from __future__ import annotations

from ._envelope import (
    _GAP, distill_mark, distill_end_mark, is_distillate, is_distill_end, engine_voice, bridge_mark,
    is_bridge, service_kind,
    distill_frame, distill_end,
    wrap_distillate, persona, other_distillates_note, sys_cold, _ZONE_RULE, info,
    zone_open, zone_close, zone_context_title, zone_work_title, zone_prefix_note,
    zone_work_note, zone_suffix_note, zone_note, zone_map, rules_block,
    past_distillate_tag, _FRAME_HEADS_PAST, _FRAME_ENDS_PAST, known_frame_lines,
    kept_tag, kept_notice, data_header, data_footer,
)
from ._interrogation import (
    q1_count, COUNT_KEY, s1_count, q2_titles, s2_titles, q2_recount,
    C_RECOUNT_MERGE, C_RECOUNT_KEEP, s2_recount, recount_says_merge, q2_titles_merge,
    q3_split, s3_split, topic_scope, q5_settled, q6_open, q7_pack, q10_selfcheck,
    C_S10_KEY, C_S10_KEY2, s10_selfcheck, selfcheck_key, q11_filler, q12_additions,
    ADDITIONS_KEY, s12_additions, answer_style, lang_note, frame_block, topic_banner,
    q_gate, C_GATE_FIELDS, C_GATE_FIELDS2, C_GATE_RENAMED, gate_field, s_gate,
    q_pending_task, q_errors_fixes, q_critical_values, q_constraints_verbatim,
    CONSTRAINTS_KEY, s_constraints_verbatim, kind_work, kind_recon, kind_design,
    kind_rules, kind_talk, kind_other, kinds_all, kind_of, q_kinds, s_kinds, q_talk,
)
from ._document import (
    doc_title, doc_topic_head, doc_filler_head, doc_settled_head, doc_open_head,
    doc_pack_head, doc_nothing_to_add, doc_additions_head, doc_suspect,
    doc_pending_head, doc_errors_head, doc_values_head, doc_constraints_head,
    doc_talk_head,
)
