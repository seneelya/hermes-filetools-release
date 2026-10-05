# -*- coding: utf-8 -*-
"""Выгрузка сообщений в файлы. Читает ленту, пишет markdown, базу не трогает.

ЗАЧЕМ. Все наши операции над контекстом ВЫЧИТАЮЩИЕ: `drop content`, `erase tool`,
`erase turn`, компакция. Выгрузка — первая прибавляющая, и первая, где цена ошибки равна
неверному файлу на диске (`Plan16-Task05`, `Vision09`). Мотив владельца дословно: «меня
бесит, что я не могу сохранить сессию в md, и что приходится просить агента сохранить то,
что он только что ответил».

ИМЯ ФАЙЛА — `<время сообщения>__<адрес>__<роль>.md`, например

    2026.09.23-04.12.33__m000a__assistant.md

Разделитель внутри штампа — точка, между полями имени — двойное подчёркивание: сплошная
лента цифр не читается глазом (владелец, 2026-09-23), а два разных разделителя сразу
говорят, где кончается время и начинается адрес.

СНИМОК ВЫБОРКИ (`snapshot=True`) кладёт то же самое в папку С ДАТОЙ СОХРАНЕНИЯ внутри папки
сессии и добавляет к имени номер позиции в ленте:

    <сессия>/2026.09.23-16.42.37/007__2026.09.22-23.45.10__m000d__assistant.md

Это ВТОРОЙ предмет, а не вариант первого, и гарантии у него другие — разбор в докстринге
`export()`. Коротко: обычная выгрузка это ФАЙЛ СООБЩЕНИЯ, снимок это МОМЕНТ ЛЕНТЫ.

Время берётся у САМОГО СООБЩЕНИЯ, не у момента сохранения, и это несущее решение:
`(роль, время)` — личность сообщения (`file_ops/_msgindex.py::place`), мы её не трогаем
никогда. Отсюда два свойства разом:

- папка сортируется по имени в порядке истории, даже если сохраняли по одному и в разные
  дни (владелец, 2026-09-23: «как бы так сделать, чтобы сообщения в папке становились +-
  по очереди, как они стоят в реальной истории»);
- повторное сохранение ПЕРЕЗАПИСЫВАЕТ свой файл, а не плодит копию.

Честная цена, названная вслух: часы на соседних строках врут, поэтому две подряд идущие
реплики теоретически могут поменяться местами; а у дистиллята время свежее, и в папке он
ляжет в конец, а не туда, где стоит в ленте. Владелец, 2026-09-23: «ничего страшного, мы
тут не сессию сохраняем, а сообщения» — строгий порядок будет у выгрузки сессии целиком,
там он расставляется внутри одного документа.

ВНУТРИ ФАЙЛА НЕТ НИЧЕГО СЛУЖЕБНОГО. Ни шапки, ни адреса, ни времени — всё это уже сказано
именем файла. Решение владельца 2026-09-23: чистый текст можно править где угодно и вернуть
обратно, не выковыривая из обвязки (вторая половина `Vision09`).

РАЗДЕЛИТЕЛЬ — три пустых строки, `---`, пустая, комментарий-метка, пустая, `---`, три пустых
(версия 2, владелец 2026-09-28; до этого — голый `---`, версия 1). Голая черта была слабой:
в markdown это обычная горизонтальная линия, и в тексте сообщения она встречается постоянно, а
выгрузка задумана и как будущий импорт сессии. Метка `<!-- tool call separator … -->` в тексте
случайно не встречается, и любой просмотрщик её прячет (Obsidian — в режиме чтения).

Пустые строки ВОКРУГ метки не украшение: строка текста прямо над `---` — это заголовок
второго уровня (setext), и Obsidian так и сделал с меткой без них (проверено владельцем
2026-09-28). По той же причине пустые строки перед первой чертой.

Прогон одного ASCII-символа дёшев в токенах (юникодные линейки `─══` стоят по токену на
символ), поэтому черты остались `---`.

Ищет разделитель `split_halves`, и ищет ПО МЕТКЕ, не обращая внимания на число пустых строк
вокруг (владелец, 2026-09-28: «при сравнении удалять ненужные переводы строк»): файл
задуман правимым руками, и лишняя или съеденная пустая строка не должна рвать пару.

ПОЛОЖЕНИЕ РЕШАЕТ, ЧТО ЭТО (версия 3, владелец 2026-10-05). В файле реплики ассистента первая
половина — ВСЕГДА проза, даже пустая: нет прозы — файл начинается с разделителя. Каждая
следующая — вызов: имя тула и под ним JSON аргументов, ВСЕГДА, пустые — `{}`. До версии 3
вызов без аргументов писался голым именем, а реплика без прозы начиналась с вызова, и ответ
ассистента `done` / `OK` был неотличим от вызова `done()` — импорт принимал его за вызов.

ЕДИНИЦА ВЫГРУЗКИ — та же, что у правила владения (`context_ctl/_pairs.py`):

    проза  → судьба СВОЕГО сообщения
    вызов  → судьба СВОЕГО результата

Поэтому файл тул-результата несёт ВЫЗОВ и результат (у вызова своего адреса нет — приехать
он может только с результатом), а файл реплики ассистента несёт прозу и свои вызовы. Обе
половины самодостаточны и читаются отдельно — ради этого и терпится то, что при выборе
обеих текст вызова окажется в двух файлах.

ПОЧЕМУ ЭТОТ МОДУЛЬ БЕЗ ЗАВИСИМОСТЕЙ ОТ НАШЕГО ПАКЕТА — та же причина, что у `_select.py`:
его грузит `dashboard/plugin_api.py` ПО ПУТИ (`spec_from_file_location`, вне пакета), где
относительный импорт невозможен в принципе. Отсюда осознанная копия со своим тестом-сверкой:
`_safe_session` (из `file_ops/_ledger.py`).

Исключения — два файла, которые копировать нельзя. `_addresses.py` (Plan16-Task06): правило
владения — ровно та вторая ветка одного функционала, которой мы избегаем. `_home.py`: дом
плагина — одно правило на все пути данных. Оба берём ОБОИМИ способами: пакетом, когда нас
импортируют пакетом (агент, тесты), и по пути файла, когда нас самих загрузили по пути
(дашборд). Голый `from ..` на втором пути падает: пакета нет.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

try:
    from . import _addresses
except ImportError:  # загружены по пути, вне пакета — берём соседа тем же способом
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("hermes_filetools_ctx_addresses",
                                         Path(__file__).with_name("_addresses.py"))
    _addresses = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_addresses)

VERSION = 3

# Спутник снимка: записи журнала об операциях выгруженных результатов (`Plan27-Task01`).
LEDGER_FILE = "ledger.jsonl"

# Разделитель половин внутри файла (версия 2, 2026-09-28). Разбор — в шапке модуля.
RULER_MARK = "<!-- tool call separator (do not remove or edit) -->"
RULER = "\n\n\n\n---\n\n" + RULER_MARK + "\n\n---\n\n\n\n"

# Разделитель при чтении: черта, метка, черта — с ЛЮБЫМ числом пустых строк вокруг и между.
# В НАЧАЛЕ файла (реплика без прозы, версия 3) перевода строки перед чертой может не быть —
# руками стёрли пустые строки сверху; отсюда `\A`.
_RULER_RE = re.compile(r"(?:\A|\n)\s*^---[ \t]*\n\s*^" + re.escape(RULER_MARK) +
                       r"[ \t]*\n\s*^---[ \t]*$\s*", re.M)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
UNKNOWN_SESSION = "_unknown"

# Когда у строки нет времени вовсе (быть не должно, но лента приходит из чужой базы):
# сортируется в самое начало и видно глазом, что это не настоящий штамп.
NO_STAMP = "0000.00.00-00.00.00"
NO_ADDRESS = "nomsg"


def _safe_session(session: str | None) -> str:
    """Копия из `file_ops/_ledger.py`. Сверяется тестом на неудобных именах."""
    name = _SAFE_NAME.sub("_", (session or "").strip())
    name = name.lstrip(".")
    return name or UNKNOWN_SESSION


# Разбор поля `tool_calls` — из `_addresses.py`, одно место на всех.
calls_of = _addresses.calls_of


def _home_module():
    """`<корень плагина>/_home.py` — одно правило дома плагина.

    Пакетом — относительный импорт. Панель грузит этот файл по пути (пакета нет) — тогда
    тот же `_home.py` по пути, один на процесс. Тот же приём, что у `_select.py`.
    """
    try:
        from .. import _home
        return _home
    except ImportError:
        import importlib.util
        import sys
        mod = sys.modules.get("hermes_filetools_home")
        if mod is None:
            path = Path(__file__).resolve().parents[1] / "_home.py"
            spec = importlib.util.spec_from_file_location("hermes_filetools_home", path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["hermes_filetools_home"] = mod
            spec.loader.exec_module(mod)
        return mod


def split_halves(text) -> list:
    """Файл → половины, по разделителю версии 2. Пустые строки вокруг — не в счёт.

    Половины отдаются без хвостовых и ведущих переводов строк: они принадлежат разделителю,
    а не тексту. Разделителя нет — одна половина, весь текст.
    """
    parts = _RULER_RE.split(str(text or ""))
    return [p.strip("\n") for p in parts]


def export_dir(root: str | None = "") -> Path:
    """Куда пишем. Пустая строка в конфиге — ЗАКОННОЕ значение: «дом плагина».

    Путь разрешается на каждом вызове, не кэшируется: `get_hermes_home()` переопределяем
    (тесты и профили это делают), а константа модуля пришпилила бы первый увиденный дом.
    """
    configured = str(root or "").strip()
    if configured:
        return Path(configured).expanduser()
    # НЕ внутри `ctxctl/` (владелец увидела путь живьём, 2026-09-23: «слишком большая
    # вложенность лишняя»). Соседи по `ctxctl/` — служебные файлы механизма (`select`,
    # `applied`, `compact_state`), которые человек не открывает никогда; выгрузка наоборот,
    # она ДЛЯ человека, и лежать ей в подвале нашего движка незачем.
    return _home_module().plugin_home() / "export"


def session_dir(session: str | None, root: str | None = "") -> Path:
    """Папка на сессию. Имя санитайзится тем же правилом, что у леджера и файлов выбора."""
    return export_dir(root) / _safe_session(session)


def stamp(value) -> str:
    """`timestamp` строки → `ГГГГ.ММ.ДД-ЧЧ.ММ.СС`. Не разобралось — `NO_STAMP`, не исключение.

    Локальное время, а не UTC: папку читает человек, и он сверяет её со своей перепиской.

    ТОЧКИ, а не сплошные цифры (владелец, 2026-09-23: «для меня это шум из цифр»). Расширение
    от этого не страдает: и `Path.suffix`, и `splitext` смотрят на ПОСЛЕДНЮЮ точку, и `.md`
    ею остаётся. Ширина групп постоянная, значит сортировка по имени по-прежнему сортировка по
    времени. Единственная цена названа вслух: чужой скрипт вида `name.split(".")[0]` увидит
    `2026` — у нас такого нет, и на `.md` он спотыкался бы так же.
    """
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y.%m.%d-%H.%M.%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return NO_STAMP


def file_name(row, address: str | None = "") -> str:
    """`<время>__<адрес>__<роль>.md`. Имя ЧИСТО из личности сообщения, см. шапку модуля."""
    role = str((row or {}).get("role") or "norole")
    addr = _SAFE_NAME.sub("_", str(address or "").strip()) or NO_ADDRESS
    return f"{stamp((row or {}).get('timestamp'))}__{addr}__{role}.md"


def body_of(row) -> str:
    """Что реально уехало провайдеру: `api_content` вербатим, если он есть.

    Показывать `content` там, где провайдер получил другое, значит выгрузить не тот
    разговор — та же ошибка, ради которой строилось зеркало (`/messages`).
    """
    body = (row or {}).get("api_content")
    return str(body if body else ((row or {}).get("content") or ""))


def render_call(call) -> str:
    """Один вызов: имя тула, под ним аргументы — ВСЕГДА, пустые как `{}` (версия 3, шапка).

    Аргументы приходят строкой JSON — печатаем разобранными и с отступом, потому что
    однострочный JSON человек не читает. Не разобралось — отдаём строку как есть, а не
    прячем: битые аргументы это факт разговора, и он должен доехать.
    """
    if not isinstance(call, dict):
        return str(call)
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = fn.get("name") or call.get("name") or ""
    raw = fn.get("arguments") if fn else call.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            pass
    if raw in (None, "", {}):
        args = "{}"
    elif isinstance(raw, str):
        args = raw
    else:
        try:
            args = json.dumps(raw, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            args = str(raw)
    head = str(name) if name else "(вызов без имени)"
    return f"{head}\n{args}" if args else head


def render(row, carrier=None) -> str:
    """Текст файла для одной строки ленты.

    `carrier` — реплика, объявившая вызов, и она нужна ТОЛЬКО для тул-результата: у вызова
    нет своего адреса, приехать он может лишь с результатом. Для реплики ассистента
    носитель и есть она сама.
    """
    role = str((row or {}).get("role") or "")
    body = body_of(row)

    if role == "tool":
        call_id = str((row or {}).get("tool_call_id") or "")
        parts = []
        for call in calls_of(carrier):
            if not isinstance(call, dict):
                continue
            cid = call.get("id") or (call.get("function") or {}).get("id")
            if call_id and str(cid or "") != call_id:
                continue
            parts.append(render_call(call))
        parts.append(body)
        return RULER.join(p for p in parts if p != "")

    # Проза — ВСЕГДА первая половина, даже пустая: у реплики без прозы файл начинается с
    # разделителя, и первая половина никогда не бывает вызовом (версия 3, шапка).
    calls = [render_call(c) for c in calls_of(row)]
    if not calls:
        return body
    return RULER.join([body] + calls)


def carriers_by_call(rows) -> dict:
    """`id вызова` → строка, которая его объявила. Один проход по ленте.

    Связь считается по `tool_call_id`, а не по нашему `op id`: первый есть всегда (иначе
    провайдер не связал бы ответ с вызовом), а у чужого или упавшего тула конверта с
    номером нет вовсе.
    """
    out: dict = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for call in calls_of(row):
            if not isinstance(call, dict):
                continue
            cid = call.get("id") or (call.get("function") or {}).get("id")
            if cid:
                out.setdefault(str(cid), row)
    return out


def write_text(path: Path, text: str) -> None:
    """Запись через временный файл: половина markdown на диске хуже, чем его отсутствие.

    `newline=""` — ВЕРБАТИМ, и это не мелочь. По умолчанию Python на Windows переводит `\\n`
    в `\\r\\n`, то есть выгрузка меняла бы КАЖДУЮ строку сообщения. Файл задуман так, чтобы
    его можно было поправить где угодно и вернуть обратно (`Vision09`), а вернулся бы он
    тогда с чужими концами строк — молчаливая правка всего текста под видом одной.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def seat_width(rows) -> int:
    """Ширина номера позиции: столько цифр, сколько в длине ленты, но не меньше трёх.

    Постоянная ширина внутри одного снимка — единственное, что делает сортировку по имени
    сортировкой по порядку: `9` встало бы после `10`.
    """
    return max(3, len(str(max(0, len(rows or []) - 1))))


def seat_mark(index: int, width: int) -> str:
    """`007` — НОМЕР ПОЗИЦИИ В ЛЕНТЕ, а не порядковый номер внутри выборки.

    Решение владельца 2026-09-23, и разница не косметическая: в снимке выборки номера идут
    с пропусками (`003`, `007`, `012`), и пропуск — это ИНФОРМАЦИЯ. Он говорит, что между
    сохранёнными кусками что-то было и человек его не взял. Сплошная нумерация `001, 002,
    003` эту правду стирает, и выборка начинает выглядеть целой сессией.
    """
    return str(max(0, int(index))).zfill(max(1, int(width)))


def export(session: str | None, rows, addresses, wanted, root: str | None = "",
           snapshot: bool = False, at=None, operations=None) -> dict:
    """Выгрузить названные адреса. Возвращает отчёт, НИЧЕГО не бросает.

    `addresses` — список той же длины, что `rows` (адрес строки или ""), ровно в том виде,
    в каком его выдаёт `/messages`. `wanted` — адреса, которые человек отметил.

    ДВА РЕЖИМА, и у них РАЗНЫЕ ГАРАНТИИ. Это не рассогласование, а два разных предмета, и
    писать это надо вслух, иначе следующий заход начнёт «чинить» то, что верно:

    - обычный (`snapshot=False`) — файл живёт ПО ЛИЧНОСТИ сообщения, лежит прямо в папке
      сессии и при повторном сохранении перезаписывает сам себя. Порядок в папке по времени,
      то есть приблизительный: у дистиллята время свежее, а в ленте он стоит в середине;
    - снимок (`snapshot=True`) — папка С ДАТОЙ СОХРАНЕНИЯ внутри папки сессии, и это МОМЕНТ:
      номер позиции в ленте становится частью имени, порядок внутри снимка точный и больше
      никогда не меняется, ничего не перезаписывается.

    Ненайденный адрес НЕ проглатывается: он уходит в `missing`. «Сохранили ноль» и «такого
    сообщения нет» — разные факты, и второй человек обязан увидеть, иначе он смотрит на
    пустую папку и не знает почему.

    `at` — время снимка числом; None значит «сейчас». Параметр существует ради тестов и
    зондов: без него имя папки нельзя было бы предсказать, и проверять было бы нечего.

    `operations` — записи журнала рода «операция» этой сессии (вызывающий отбирает их
    `_ledger.is_event`: этот модуль грузится по пути и журнал импортировать не может). Только
    у снимка: в папку ложится спутник `ledger.jsonl` — записи, чей `tool_call_id` у
    выгруженного результата, как есть, по строке JSON (`Plan27-Task01`). Импорт по ним
    заводит операции в журнал целевой сессии. В файлы сообщений id вызовов не попадают.
    """
    report = {"version": VERSION, "session": str(session or ""), "root": "",
              "dir": "", "snapshot": bool(snapshot),
              "written": [], "missing": [], "errors": []}

    wanted_list = [str(a or "").strip() for a in (wanted or []) if str(a or "").strip()]
    if not wanted_list:
        report["errors"].append("не названо ни одного адреса")
        return report

    try:
        folder = session_dir(session, root)
        if snapshot:
            folder = folder / stamp(time.time() if at is None else at)
    except Exception as exc:  # noqa: BLE001 - маршруту нужен ответ, не 500
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        return report
    report["root"] = str(export_dir(root))
    report["dir"] = str(folder)

    rows = list(rows or [])
    addresses = list(addresses or [])
    by_address = {}
    for index, row in enumerate(rows):
        addr = str(addresses[index] or "") if index < len(addresses) else ""
        if addr:
            by_address.setdefault(addr, (index, row))
    carriers = carriers_by_call(rows)
    width = seat_width(rows)

    # СНИМОК ВЫБОРКИ достраивается до пар (Plan16-Task06, владелец 2026-09-28): выбранный
    # результат тянет свою безмолвную реплику-носителя (`_addresses.expand`). Обычная выгрузка
    # одного сообщения не достраивается: там человек назвал ровно один файл.
    report["paired"] = []
    report["shells"] = []
    if snapshot:
        wanted_list, report["paired"] = _addresses.expand("export", rows, addresses, wanted_list)

    # Пишем В ПОРЯДКЕ ЛЕНТЫ, а не в порядке, в каком адреса назвали. Отчёт читает человек, и
    # он ждёт увидеть то же, что увидит в папке.
    picked = []
    seen = set()
    for address in wanted_list:
        if address in seen:
            continue
        seen.add(address)
        found = by_address.get(address)
        if found is None:
            report["missing"].append(address)
            continue
        picked.append((found[0], address, found[1]))
    picked.sort(key=lambda item: item[0])
    kept = {item[0] for item in picked}
    written_calls: set = set()

    for index, address, row in picked:
        carrier = carriers.get(str(row.get("tool_call_id") or "")) \
            if str(row.get("role") or "") == "tool" else None
        # В снимке реплика несёт только те вызовы, чьи результаты тоже едут (`_addresses.
        # live_calls`): вызова без пары в выгрузке быть не должно. Правится КОПИЯ строки —
        # лента, пришедшая снаружи, не наша.
        if snapshot and str(row.get("role") or "") == "assistant" and calls_of(row):
            row = dict(row, tool_calls=_addresses.live_calls("export", rows, index, kept))
            # Оболочка жива, пока в ней есть проза или хоть один вызов (правило владения).
            # Сюда попадает безмолвный носитель, отмеченный взводом рельсы (ячейки у него
            # нет, снять его человек не может), когда человек снял все его результаты.
            if not calls_of(row) and not body_of(row).strip():
                report["shells"].append(address)
                continue
        try:
            text = render(row, carrier)
            name = file_name(row, address)
            if snapshot:
                name = f"{seat_mark(index, width)}__{name}"
            path = folder / name
            write_text(path, text)
        except Exception as exc:  # noqa: BLE001 - один битый файл не отменяет остальные
            report["errors"].append(f"{address}: {type(exc).__name__}: {exc}")
            continue
        report["written"].append({"msg": address, "path": str(path),
                                  "seat": index, "chars": len(text)})
        if str(row.get("role") or "") == "tool" and row.get("tool_call_id"):
            written_calls.add(str(row.get("tool_call_id")))

    if snapshot and operations is not None:
        report["ledger"] = _write_ledger(folder, operations, written_calls, report)
    return report


def _write_ledger(folder, operations, call_ids, report) -> int:
    """Спутник снимка `ledger.jsonl`: записи операций выгруженных результатов. Сколько записано.

    Нечего писать — файла нет: папка без спутника импортируется как прежде (старые папки).
    """
    picked = [e for e in operations or []
              if isinstance(e, dict) and str(e.get("tool_call_id") or "") in call_ids]
    if not picked:
        return 0
    try:
        text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in picked)
        write_text(Path(folder) / LEDGER_FILE, text)
    except Exception as exc:  # noqa: BLE001 - без спутника снимок всё равно годен
        report["errors"].append(f"{LEDGER_FILE}: {type(exc).__name__}: {exc}")
        return 0
    return len(picked)
