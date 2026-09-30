"""Свой счёт токенов без сети (`Plan24`): наш оценщик, тестовая строка, файл моделей.

ЗАЧЕМ. Их оценка — `ceil(UTF-8 байт / 4)` (`Recon32` T2), для кириллицы и кода она врёт в разы, а
токенизатора в их венвах нет вовсе (T9). Точный счёт умеет сервер модели (`_tokserver.py`), но он
есть не всегда: облако его не даёт, выгруженную модель ради счёта грузить нельзя. Здесь — то, что
под рукой ВСЕГДА: наш счёт по словам и множитель, который калибруется точными числами сервера.

НАШ СЧЁТ (схема владельца, 2026-09-29): слово → 1, длинное слово → 2–3 (сколько букв на токен —
своё у каждой письменности), каждый знак → 1, одиночный пробел перед словом бесплатный, прочие
пробелы и переводы строк — токены, CJK — символ за токен. Это НЕ токены модели, а «наши единицы»:
в токены модели их переводит множитель токенизатора (`mult`), и только он. Параметры счёта
менять можно, но тогда все множители в файле моделей устаревают — отсюда `ESTIMATOR_VERSION`.

ТЕСТОВАЯ СТРОКА. Одна строка на две работы: хеш её номеров токенов — отпечаток токенизатора
(у двух сборок одной модели он совпадёт, у другой семьи — нет); её точное число против нашего
счёта — стартовый множитель, пока реальных ответов ещё не было. Меняешь строку — поднимай
`PROBE_VERSION`: старые отпечатки тогда перемеряются, а не тихо сравниваются с новыми.

ФАЙЛ МОДЕЛЕЙ (`<hermes home>/filetools/tokens/models.json`, общий на все сессии):

    {"probe": 1, "estimator": 1,
     "names": {"<имя модели>": "a1b2c3"},
     "tok":   {"a1b2c3": {"mult": 1.12, "probe_tokens": 812, "probe_ours": 725,
                          "markup": {...}, "names": [...], "at": "2026-09-29T12:00:00"}}}

Имя модели — просто текст из конфига роутера, поэтому ключ коэффициентов — отпечаток, а имя лишь
указывает на него: имя уже видели — строку не шлём. Отпечаток короткий (6 знаков) нарочно: файл
читают глазами.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path


def _home_module():
    """`<корень плагина>/_home.py` — ОДНО правило дома плагина. Пакетом — относительный импорт;
    панель грузит этот файл ПО ПУТИ (пакета нет) — тогда тот же `_home.py` по пути, один на процесс."""
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


PROBE_VERSION = 1
ESTIMATOR_VERSION = 2

# Смесь всего, что бывает в ленте: проза на двух языках, код, JSON, разметка вызова тула, числа,
# пути, отступы, CJK. Размер — пара тысяч символов: достаточно, чтобы множитель не был шумом, и
# дёшево для сервера.
PROBE_TEXT = (
    "The quick brown fox jumps over the lazy dog. Tokenizers split rare words like "
    "internationalization, antidisestablishmentarianism and configuration into pieces.\n"
    "Съешь же ещё этих мягких французских булок, да выпей чаю. Проверка токенизатора: "
    "длинные слова вроде «переконфигурирование» и «достопримечательность» режутся на части.\n"
    "\n"
    "def estimate(text: str, limit: int = 4096) -> dict:\n"
    "    \"\"\"Return counts.\"\"\"\n"
    "    for i, ch in enumerate(text[:limit]):\n"
    "        if ch in '{}[]()<>':\n"
    "            yield i, ord(ch) * 31 % 1000003\n"
    "\n"
    "{\"path\": \"C:/Users/User/project/src/main.py\", \"offset\": 120, \"limit\": 2000, "
    "\"ok\": true, \"ratio\": 0.75, \"tags\": [\"a\", \"b\"]}\n"
    "<tool_call>\n<function=read_file>\n<parameter=path>\nsrc/app.py\n</parameter>\n"
    "</function>\n</tool_call>\n"
    "Numbers: 2026-09-29 12:34:56, 262144, 3.14159, 0x1F4A9, v0.35.0, +7 (495) 123-45-67.\n"
    "Markdown: **bold**, `code`, - item one\n  - nested item\n| a | b |\n|---|---|\n"
    "日本語のテキストと中文文本混合。\n"
    "    indented line\n\t\ttabbed line\n\n\n"
    "Exit code: 0 · → ← ✓ ✗ — «кавычки» “quotes” ...\n"
)

# Сколько букв одной письменности в среднем уходит в один токен — отсюда цена длинного слова:
# `1 + (len - 1) // span`. Кириллица у Qwen/Gemma режется мельче латиницы. Знаки подряд (`": `,
# `"],`, `**`, `---`) модель склеивает — прогон знаков стоит `1 + (len - 1) // _SYM_SPAN`.
# Подобрано 2026-09-29 перебором на 601 сообщении двенадцати сессий против токенизатора Ornith
# (аргументы вызовов РАСКОДИРОВАННЫЕ, как на проводе — первая подгонка шла по экранированному JSON
# и завышала кириллицу): разброс «сервер / наш счёт» по сессии 2.6 %, по сообщению 7.9 % (у
# `bytes/4` — 43 % по сообщению), множитель ≈ 1.0. Английские слова почти всегда один токен —
# отсюда длинный `latin`.
_SPAN = {"latin": 12, "cyr": 4, "other": 3}

_SYM_SPAN = 3

_CJK = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af"
_PIECE = re.compile(
    r"(?P<latin>[A-Za-z]+)"
    r"|(?P<cyr>[\u0400-\u04FF]+)"
    rf"|(?P<cjk>[{_CJK}])"
    rf"|(?P<other>[^\W\d_{_CJK}]+)"
    r"|(?P<digits>\d+)"
    r"|(?P<space>\s+)"
    r"|(?P<sym>[^\w\s]+|_+)",
    re.S,
)


def _space_cost(run: str) -> int:
    if "\n" not in run:
        # Одиночный пробел садится на следующее слово; длинный прогон — отступ.
        return 0 if len(run) == 1 else math.ceil(len(run) / 4)
    # Переводы строк подряд модель обычно берёт одним токеном; отступ после — отдельно.
    indent = run.rsplit("\n", 1)[1]
    return 1 + (math.ceil(len(indent) / 4) if len(indent) > 1 else 0)


def count_ours(text: str) -> int:
    """Наши единицы для текста. Не токены модели — их даёт `count_ours * mult`."""
    total = 0
    for m in _PIECE.finditer(text or ""):
        kind = m.lastgroup
        piece = m.group()
        if kind in _SPAN:
            total += 1 + (len(piece) - 1) // _SPAN[kind]
        elif kind == "digits":
            total += len(piece)  # Qwen режет числа по цифре; другим семьям поправит множитель
        elif kind == "space":
            total += _space_cost(piece)
        elif kind == "sym":
            total += 1 + (len(piece) - 1) // _SYM_SPAN
        else:  # cjk
            total += 1
    return total


def fingerprint(token_ids) -> str:
    """Короткий отпечаток токенизатора по номерам токенов тестовой строки."""
    raw = ",".join(str(int(i)) for i in token_ids)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()[:6]


# --------------------------------------------------------------------------- строка ленты

def wire_text(row: dict) -> str:
    content = row.get("api_content") if isinstance(row.get("api_content"), str) else row.get("content")
    return content if isinstance(content, str) else ("" if content is None else json.dumps(content, ensure_ascii=False))


def row_calls(row: dict) -> list:
    calls = row.get("tool_calls")
    if isinstance(calls, str):
        try:
            calls = json.loads(calls)
        except ValueError:
            return []
    return calls if isinstance(calls, list) else []


def call_args(call: dict) -> dict:
    fn = call.get("function") or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {"_raw": args}
    return args if isinstance(args, dict) else {}


def row_units(row: dict, markup: dict) -> tuple[int, int]:
    """`(наши единицы строки, её разметка в ТОКЕНАХ модели)`: текст как на проводе (`api_content`
    поверх `content`), имена/ключи/значения вызовов раскодированными; разметка — по цене из пробы."""
    role = row.get("role")
    units = count_ours(wire_text(row))
    for call in row_calls(row):
        args = call_args(call)
        units += count_ours((call.get("function") or {}).get("name") or "")
        for key, val in args.items():
            units += count_ours(key)
            units += count_ours(val if isinstance(val, str) else json.dumps(val, ensure_ascii=False))
    return units, row_markup(role, row, markup)


def row_markup(role, row, markup) -> int:
    if not markup:
        return 0
    if role == "user":
        return markup.get("user", 0)
    if role == "assistant":
        extra = 0
        for call in row_calls(row):
            extra += markup.get("call", 0) + max(len(call_args(call)) - 1, 0) * markup.get("param", 0)
        return markup.get("assistant", 0) + extra
    return 0  # tool: обёртка результата посчитана в `call` у вызова


def row_tokens(row: dict, mult: float, markup: dict) -> int:
    """Строка в токенах модели: наш счёт × множитель токенизатора + разметка шаблона."""
    units, mk = row_units(row, markup or {})
    return round(units * (mult or 1.0)) + mk


# --------------------------------------------------------------------------- файл моделей

def tokens_dir() -> Path:
    """`<hermes home>/filetools/tokens` — рядом с леджером, по той же причине не кэшируется.
    Дом — `_home.py` (без зашитых путей: у клиента своего `Y:\\` нет, 2026-09-30)."""
    return _home_module().plugin_home() / "tokens"


def models_path() -> Path:
    return tokens_dir() / "models.json"


def _empty() -> dict:
    return {"probe": PROBE_VERSION, "estimator": ESTIMATOR_VERSION, "names": {}, "tok": {}}


def load_models(path: Path | None = None) -> dict:
    """Файл моделей. Другая версия СТРОКИ — отпечатки устарели, начинаем с пустого. Другая версия
    СЧЁТА — отпечатки и «имя → токенизатор» верны (они от строки, не от счёта), устарели только
    множители: пересчитываем стартовый из сохранённого точного числа строки."""
    path = path or models_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - нет файла / битый — то же, что пустой
        return _empty()
    if data.get("probe") != PROBE_VERSION:
        return _empty()
    data.setdefault("names", {})
    data.setdefault("tok", {})
    for entry in data["tok"].values():  # до 2026-09-29 разметка называлась `wrap`
        if "wrap" in entry and "markup" not in entry:
            entry["markup"] = entry.pop("wrap")
    if data.get("estimator") != ESTIMATOR_VERSION:
        ours = count_ours(PROBE_TEXT)
        for entry in data["tok"].values():
            entry["probe_ours"] = ours
            entry["mult"] = round(entry.get("probe_tokens", ours) / ours, 4) if ours else 1.0
        data["estimator"] = ESTIMATOR_VERSION
    return data


def save_models(data: dict, path: Path | None = None) -> Path:
    path = path or models_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def remember_probe(data: dict, name: str, fp: str, probe_tokens: int, markup: dict | None = None) -> dict:
    """Записать замер тестовой строки: имя → отпечаток, стартовый множитель, обёртки."""
    ours = count_ours(PROBE_TEXT)
    entry = data["tok"].setdefault(fp, {})
    entry["probe_tokens"] = int(probe_tokens)
    entry["probe_ours"] = ours
    entry.setdefault("mult", round(probe_tokens / ours, 4) if ours else 1.0)
    if markup:
        entry["markup"] = markup
    names = entry.setdefault("names", [])
    if name not in names:
        names.append(name)
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["names"][name] = fp
    return entry


def entry_for(data: dict, name: str) -> tuple[str | None, dict | None, bool]:
    """Коэффициенты для модели: `(отпечаток, запись, точно_эта_модель)`.

    Модель не мерили — последний известный токенизатор (любой точнее `bytes/4`), с флагом
    `False`, чтобы панель подписала «≈ по чужому токенизатору».
    """
    fp = data["names"].get(name)
    if fp and fp in data["tok"]:
        return fp, data["tok"][fp], True
    if data["tok"]:
        fp, entry = max(data["tok"].items(), key=lambda kv: kv[1].get("at", ""))
        return fp, entry, False
    return None, None, False
