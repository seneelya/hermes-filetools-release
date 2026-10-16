# ── Слова самого документа: их читает следующий агент, не исполняет ──

def doc_title() -> str:
    # ЗАГОЛОВКИ ПО-АНГЛИЙСКИ (владелец, 2026-09-21). Скелет документа — служебное, и
    # английский отделяет его от ПРОЗЫ, которая идёт на языке сессии: агент по одному взгляду
    # видит, где структура, а где содержание.
    C_DOC_TITLE2 = "# Session distillate"
    out__doc_title = C_DOC_TITLE2
    return out__doc_title
def doc_topic_head(title: str) -> str:
    C_DOC_TOPIC_HEAD = "## {title}"
    out__doc_topic_head = C_DOC_TOPIC_HEAD.format(title=title)
    return out__doc_topic_head
def doc_filler_head() -> str:
    C_DOC_FILLER_HEAD2 = "## Mentioned in passing"
    out__doc_filler_head = C_DOC_FILLER_HEAD2
    return out__doc_filler_head
def doc_settled_head() -> str:
    C_DOC_SETTLED_HEAD2 = "### Where we landed"
    out__doc_settled_head = C_DOC_SETTLED_HEAD2
    return out__doc_settled_head
def doc_open_head() -> str:
    C_DOC_OPEN_HEAD2 = "### Still open"
    out__doc_open_head = C_DOC_OPEN_HEAD2
    return out__doc_open_head
def doc_pack_head(kind: str) -> str:
    """Заголовок пачки по классу. Имя говорит, ЧТО в разделе, а не как он назывался в коде."""
    C_PACK_HEAD_CODE2 = "### Objects and state of the work"
    C_PACK_HEAD_CANON2 = "### Names, numbers, facts"
    C_PACK_HEAD_PLAN2 = "### Decisions taken and rejected"
    C_PACK_HEAD_RECON2 = "### What we found out"
    C_PACK_HEAD_RULES2 = "### Rules stated here"
    heads = {"work": C_PACK_HEAD_CODE2, "investigation": C_PACK_HEAD_RECON2, "recon": C_PACK_HEAD_RECON2,
             "design": C_PACK_HEAD_PLAN2, "rules": C_PACK_HEAD_RULES2,
             "code": C_PACK_HEAD_CODE2, "canon": C_PACK_HEAD_CANON2,
             "general": C_PACK_HEAD_CANON2,
             "plan": C_PACK_HEAD_PLAN2}
    out__doc_pack_head = heads.get(str(kind or "").strip().lower(), C_PACK_HEAD_CODE2)
    return out__doc_pack_head
def doc_additions_head() -> str:
    """Заголовок хвоста добавлений. Раньше строка жила прямо в `_distill` — единственный
    текст документа не в конструкторе, и нашёлся он только когда заголовки поехали на
    английский."""
    C_DOC_ADDITIONS_HEAD = "## Additions"
    out__doc_additions_head = C_DOC_ADDITIONS_HEAD
    return out__doc_additions_head
def doc_suspect(what: str) -> str:
    """Пометка самопроверки. Не удаляем ничего: решать человеку, а не нам."""
    C_DOC_SUSPECT2 = "⚠ SELF-CHECK: I find no support for this in the transcript — {what}"
    out__doc_suspect = C_DOC_SUSPECT2.format(what=what)
    return out__doc_suspect
# --- заголовки новых секций документа ---

def doc_pending_head() -> str:
    C_DOC_PENDING_HEAD2 = "### Asked for and not delivered"
    out__doc_pending_head = C_DOC_PENDING_HEAD2
    return out__doc_pending_head
def doc_errors_head() -> str:
    C_DOC_ERRORS_HEAD2 = "### Errors and their fixes"
    out__doc_errors_head = C_DOC_ERRORS_HEAD2
    return out__doc_errors_head
def doc_values_head() -> str:
    C_DOC_VALUES_HEAD2 = "### Exact values"
    out__doc_values_head = C_DOC_VALUES_HEAD2
    return out__doc_values_head
def doc_constraints_head() -> str:
    C_DOC_CONSTRAINTS_HEAD2 = "## Constraints and prohibitions (verbatim)"
    out__doc_constraints_head = C_DOC_CONSTRAINTS_HEAD2
    return out__doc_constraints_head
def doc_talk_head() -> str:
    C_DOC_TALK_HEAD2 = "### What the conversation was about"
    out__doc_talk_head = C_DOC_TALK_HEAD2
    return out__doc_talk_head
def doc_free_head() -> str:
    """Раздел темы, ответ на которую агент дал сам (`free`, `Plan34`)."""
    C_DOC_FREE_HEAD = "### In my own words"
    out__doc_free_head = C_DOC_FREE_HEAD
    return out__doc_free_head
