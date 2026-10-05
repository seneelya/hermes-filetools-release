"""ACHTUNG — ядро проверки наших швов в чужом (Hermes) коде. Одно на двоих: командная строка
(`__HQ/ACHTUNG/check_seams.py`) и панель (`GET /seams-check`, зона Check Seams в Config).

Читает реестр `ACHTUNG__foreign_seams.yaml` (рядом) и считает вхождения в файлах их checkout.
Только ЧТЕНИЕ файлов: ничего не импортирует из их кода, ничего не запускает и не пишет — поэтому
живёт в процессе панели. Без внутренних импортов плагина: грузится по пути.

Три вида проверки (подробно — шапка реестра):

  symbol      — имя ещё есть (>= min_count вхождений); счёт не равен baseline — DRIFT, не FAIL
  our_literal — строка, прочитанная из НАШЕГО модуля, должна стоять у них дословно
  test_only   — зависимость от ПОВЕДЕНИЯ, грепом не доказать: печатается с тестом, который её
                проверяет.

ПОЛНАЯ ПРОВЕРКА (2026-09-30, `run_tests`): тесты `asserted_by` едут с плагином и запускаются
pytest отдельным процессом — упавший тест делает шов FAIL, TEST-ONLY с прошедшим тестом — TEST-OK.
"""
from __future__ import annotations

import importlib.util
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(HERE)
REGISTRY = os.path.join(HERE, "ACHTUNG__foreign_seams.yaml")

OK, DRIFT, FAIL, MANUAL, GONE_GOOD = "OK", "DRIFT", "FAIL", "TEST-ONLY", "FIXED?"
TEST_OK = "TEST-OK"   # строка TEST-ONLY, чей тест прошёл в полной проверке
MAX_FAILURES = 10     # сколько упавших тестов с причиной показывать в отчёте
SHOW_BREAKS = (FAIL, DRIFT, GONE_GOOD)


def load(path: str = REGISTRY) -> dict:
    """Реестр словарём. YAML разбирает их `hermes_yaml` (PyYAML в их венве нет).

    Вне их венва (агент в контейнере над голыми исходниками, `HowTo__PrepareForUpdate.md`) —
    `ruamel.yaml` напрямую, затем PyYAML. Ничего нет — ImportError, как и раньше."""
    try:
        from hermes_yaml import safe_load
    except ImportError:
        try:
            from ruamel.yaml import YAML

            def safe_load(fh):
                return YAML(typ="safe").load(fh)
        except ImportError:
            from yaml import safe_load
    with open(path, "r", encoding="utf-8") as fh:
        return safe_load(fh) or {}


def _our_attr(module_relpath: str, attr: str):
    """Один атрибут из НАШЕГО модуля, без импорта пакета плагина (он потянул бы Hermes)."""
    full = os.path.join(PLUGIN_ROOT, module_relpath)
    spec = importlib.util.spec_from_file_location("_achtung_probe", full)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, attr, None)


def _count(root: str, relpath: str, needle: str):
    """Вхождений `needle` в файле; None — нет самого ФАЙЛА (переезд и переименование чинятся
    по-разному)."""
    full = os.path.join(root, relpath)
    if not os.path.isfile(full):
        return None
    with open(full, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read().count(needle)


def check_one(seam: dict, root: str) -> tuple:
    """(verdict, observed, note)"""
    kind = seam.get("kind", "symbol")
    if kind == "test_only":
        return MANUAL, None, seam.get("asserted_by") or "no test named - WRITE ONE"

    if kind == "our_literal":
        needle = _our_attr(seam["our_module"], seam["our_attr"])
        if not isinstance(needle, str):
            return FAIL, None, (f"could not read {seam['our_attr']} out of "
                                f"{seam['our_module']} - OUR side moved")
    else:
        needle = seam["symbol"]

    observed = _count(root, seam["file"], needle)
    if observed is None:
        return FAIL, None, f"file missing: {seam['file']}"

    minimum = seam.get("min_count", 1)
    if observed < minimum:
        # Перевёрнутая запись: отсутствие — хороший исход (они починили то, что мы обходим).
        if seam.get("good_when_missing"):
            return GONE_GOOD, observed, "gone upstream - check whether OUR code can be deleted"
        return FAIL, observed, f"expected >= {minimum}"

    baseline = seam.get("baseline_count")
    if baseline is not None and observed != baseline:
        return DRIFT, observed, f"baseline was {baseline} - shape may have changed, read it"
    return OK, observed, ""


def run(root: str, registry: dict, tests: dict | None = None) -> dict:
    """Все швы против checkout `root`: строки данными, итог, число FAIL.

    `tests` — итог `run_tests` (полная проверка): у шва с `asserted_by` появляется `test`
    (passed / failed / skipped / not run); упавший тест делает шов FAIL, прошедший переводит
    TEST-ONLY в TEST-OK. Без `tests` — только поиск, как раньше."""
    rows = []
    for seam in sorted(registry.get("seams") or [], key=lambda s: (s.get("level", 9), s["id"])):
        try:
            verdict, observed, note = check_one(seam, root)
        except Exception as exc:  # noqa: BLE001 — битая запись реестра не валит весь прогон
            verdict, observed, note = FAIL, None, f"check crashed: {type(exc).__name__}: {exc}"
        row = {"id": seam["id"], "level": seam.get("level"), "verdict": verdict,
               "observed": observed, "note": note,
               "breaks": " ".join(str(seam.get("breaks") or "").split())}
        asserted = str(seam.get("asserted_by") or "").strip()
        if tests is not None and asserted:
            status = test_status(asserted, tests)
            row["test"] = status
            if status == "failed":
                row["verdict"] = FAIL
                row["note"] = (note + " · " if note else "") + f"test FAILED: {asserted}"
            elif status == "passed" and verdict == MANUAL:
                row["verdict"] = TEST_OK
                row["note"] = f"test passed: {asserted}"
            elif status != "passed":
                row["note"] = (note + " · " if note else "") + f"test {status}"
        rows.append(row)
    tally: dict = {}
    for row in rows:
        tally[row["verdict"]] = tally.get(row["verdict"], 0) + 1
    out = {"rows": rows, "tally": tally, "failed": tally.get(FAIL, 0)}
    if tests is not None:
        out["tests"] = {k: tests.get(k) for k in ("ran", "error", "seconds", "summary", "failures")}
    return out


# ------------------------------------------------------------------- полная проверка: тесты
#
# У шва в реестре `asserted_by` — тест, который доказывает поведение, недоступное поиску (или весь
# файл тестов). Эти тесты едут с плагином (`deployFiles.txt`, секция tests/) и запускаются здесь
# отдельным процессом — тем же питоном, что работает сам (у шлюза — их venv, где pytest объявлен
# в `pip_dependencies`). `tests/conftest.py` уводит HERMES_HOME во временную папку: данные
# пользователя тесты не трогают.

def asserted_ids(registry: dict) -> list:
    """Уникальные `asserted_by` реестра в порядке появления (узел `файл::тест` или целый файл)."""
    seen, out = set(), []
    for seam in registry.get("seams") or []:
        node = str(seam.get("asserted_by") or "").strip()
        if node and node not in seen:
            seen.add(node)
            out.append(node)
    return out


def dependency_python(paths=None) -> str:
    """Python of the venv whose `site-packages` this process runs with, or `""`.

    WHY (2026-10-06, Hermes `b79c45bd3e`): the gateway now runs on the BARE store python and
    its loader adds the dependency venv's `site-packages` at runtime. A child started with
    `sys.executable` therefore is not that venv: importing `run_agent` there (our tests do)
    re-runs their loader with the tests' temporary `HERMES_HOME`, which tries to build a
    dependency environment for that empty home and exits — pytest reports `internal error`.
    The venv's own python is what `__HQ/tools/hermes_python.py` uses, and there the same tests
    pass. Standard venv layout only (`Lib/site-packages` → `Scripts/python.exe`,
    `lib/pythonX/site-packages` → `bin/python`); nothing of their installer is read.
    """
    import sys
    for entry in (paths if paths is not None else sys.path):
        p = os.path.normpath(str(entry or ""))
        if os.path.basename(p).lower() != "site-packages":
            continue
        lib = os.path.dirname(p)
        for venv in (os.path.dirname(lib), os.path.dirname(os.path.dirname(lib))):
            if not os.path.isfile(os.path.join(venv, "pyvenv.cfg")):
                continue
            for exe in (os.path.join(venv, "Scripts", "python.exe"),
                        os.path.join(venv, "bin", "python")):
                if os.path.isfile(exe):
                    return exe
    return ""


def run_tests(plugin_root: str, ids: list, *, python: str | None = None,
              timeout: float = 1200) -> dict:
    """pytest по `ids` в корне плагина. `{ran, error, seconds, summary, by_node}`;
    `by_node` — `tests/x.py::test_y` → passed / failed / skipped / error."""
    import subprocess
    import sys
    import tempfile
    import time
    import xml.etree.ElementTree as ET

    missing = sorted({i.split("::")[0] for i in ids
                      if not os.path.isfile(os.path.join(plugin_root, i.split("::")[0]))})
    if missing:
        return {"ran": False, "error": "tests not shipped: " + ", ".join(missing), "by_node": {},
                "seconds": 0.0, "summary": {}}
    # Loaded by path (no package). Same rule as `_home.scratch_dir`: RAM disk, else
    # <plugin>/scratch — never AppData Temp.
    ram = os.path.join("c:\\RAM_Cache", "Temp")
    scratch = os.path.join(ram, "hermes-filetools") if os.path.isdir(ram) else os.path.join(plugin_root, "scratch")
    os.makedirs(scratch, exist_ok=True)
    fd, junit = tempfile.mkstemp(prefix="ft-seams-", suffix=".xml", dir=scratch)
    os.close(fd)
    started = time.time()
    cmd = [python or dependency_python() or sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           f"--junitxml={junit}", *ids]
    try:
        proc = subprocess.run(cmd, cwd=plugin_root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ran": False, "error": f"tests timed out after {int(timeout)} s", "by_node": {},
                "seconds": time.time() - started, "summary": {}}
    except OSError as exc:
        return {"ran": False, "error": f"cannot start pytest: {exc}", "by_node": {},
                "seconds": time.time() - started, "summary": {}}
    seconds = time.time() - started
    by_node, summary = {}, {"passed": 0, "failed": 0, "skipped": 0, "error": 0}
    failures: list = []
    try:
        tree = ET.parse(junit)
        for case in tree.iter("testcase"):
            cls = str(case.get("classname") or "")          # tests.test_x  (или tests.test_x.Class)
            name = str(case.get("name") or "").split("[")[0]
            parts = cls.split(".")
            path = "/".join(parts[:2]) + ".py" if len(parts) >= 2 else cls + ".py"
            node = f"{path}::{name}"
            bad = case.find("failure")
            if bad is None:
                bad = case.find("error")
            if case.find("failure") is not None:
                status = "failed"
            elif case.find("error") is not None:
                status = "error"
            elif case.find("skipped") is not None:
                status = "skipped"
            else:
                status = "passed"
            # ПОЧЕМУ упал — первая строка сообщения; без неё FAIL у клиента ничего не объясняет.
            if bad is not None and len(failures) < MAX_FAILURES:
                message = " ".join(str(bad.get("message") or (bad.text or "")).split())
                failures.append({"node": node, "message": message[:300]})
            # Параметризованный тест — несколько случаев на один узел: берём худший.
            if node not in by_node or _WORST[status] > _WORST[by_node[node]]:
                by_node[node] = status
            summary[status] += 1
    except (ET.ParseError, OSError):
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
        return {"ran": False, "error": "pytest gave no report: " + " | ".join(tail),
                "by_node": {}, "seconds": seconds, "summary": {}}
    finally:
        try:
            os.remove(junit)
        except OSError:
            pass
    return {"ran": True, "error": "", "seconds": seconds, "summary": summary, "by_node": by_node,
            "failures": failures}


_WORST = {"passed": 0, "skipped": 1, "failed": 2, "error": 3}


def test_status(asserted: str, tests: dict) -> str:
    """Статус узла `asserted_by`: целый файл — худший из его тестов; не нашёлся — `not run`."""
    if not tests.get("ran"):
        return "not run"
    by_node = tests.get("by_node") or {}
    if "::" in asserted:
        status = by_node.get(asserted, "not run")
        return "failed" if status == "error" else status
    found = [s for n, s in by_node.items() if n.split("::")[0] == asserted]
    if not found:
        return "not run"
    if any(s in ("failed", "error") for s in found):
        return "failed"
    return "passed" if "passed" in found else "skipped"


def render(result: dict, quiet_ok: bool = False) -> list:
    """Строки отчёта — тот же вид, что печатала командная строка до 2026-09-30."""
    rows = result["rows"]
    width = max((len(r["id"]) for r in rows), default=10)
    out, level = [], None
    for r in rows:
        if quiet_ok and r["verdict"] == OK:
            continue
        if r["level"] != level:
            level = r["level"]
            out += ["", f"-- level {level} " + "-" * 52]
        count = "-" if r["observed"] is None else str(r["observed"])
        out.append(f"  {r['verdict']:<9} {r['id']:<{width}} {count:>4}  {r['note']}".rstrip())
        # Что сломается — под строкой FAIL / DRIFT / FIXED? (поле `breaks` реестра). У OK и
        # TEST-ONLY не печатаем: отчёт разросся бы в простыню там, где всё цело.
        if r["verdict"] in SHOW_BREAKS and r.get("breaks"):
            # ОДНОЙ строкой с начала, без отступа и без своих переносов (владелец): поле панели и
            # терминал переносят сами, а выравнивающие пробелы делали текст нечитаемым.
            out += ["breaks: " + r["breaks"], ""]  # пустая строка — отделить от следующего шва
    out += ["", ", ".join(f"{k}: {v}" for k, v in sorted(result["tally"].items()))]
    tests = result.get("tests")
    if tests is not None:
        if tests.get("ran"):
            sm = tests.get("summary") or {}
            out.append(f"full check: {sum(sm.values())} tests in {int(tests.get('seconds') or 0)} s - "
                       + ", ".join(f"{k} {v}" for k, v in sm.items() if v))
            for bad in tests.get("failures") or []:
                out += ["", f"FAILED {bad['node']}", bad["message"]]
        else:
            out.append("full check: tests NOT run - " + str(tests.get("error") or "unknown reason"))
    if result["failed"]:
        out += ["", f"*** {result['failed']} SEAM(S) BROKEN - do NOT update the live Hermes yet.",
                "    Fix order and what each break means: __HQ/HowTo__Verify_after_hermes_update.md"]
    elif tests is not None and tests.get("ran"):
        out += ["", "No broken seams: every seam found AND its behaviour tests passed."]
    else:
        out += ["", "No broken seams. This is necessary, NOT sufficient - the TEST-ONLY rows above",
                "are behaviour no grep can prove. Full check (with tests): Config -> Full check."]
    return out


def running_root() -> tuple:
    """(корень checkout, откуда взят) для ЭТОГО процесса; ("", причина), если не найден.

    `HERMES_AGENT_ROOT`, иначе папка, из которой процесс реально импортирует их пакет `agent`
    (проверяем тот код, что работает, а не угаданный путь), иначе `<HERMES_HOME>/hermes-agent`."""
    env = os.environ.get("HERMES_AGENT_ROOT", "").strip()
    if env and os.path.isdir(env):
        return env, "HERMES_AGENT_ROOT"
    try:
        spec = importlib.util.find_spec("agent")
    except (ImportError, ValueError):
        spec = None
    origin = getattr(spec, "origin", None) or ""
    if origin:
        root = os.path.dirname(os.path.dirname(origin))
        if os.path.isdir(root):
            return root, "imported agent package"
    home = os.environ.get("HERMES_HOME", "").strip()
    if home and os.path.isdir(os.path.join(home, "hermes-agent")):
        return os.path.join(home, "hermes-agent"), r"HERMES_HOME\hermes-agent"
    return "", "Hermes checkout not found (no HERMES_AGENT_ROOT, no importable agent package)"


def source_head(root: str) -> tuple:
    """(коммит, откуда). Питон ПОКОЛЕНИЯ окружения (их venv) импортирует `agent` из КОПИИ кода
    `installs/<key>/environments/<gen>/workspace` — без `.git`. Тогда коммит берём у исходного
    checkout `<HERMES_HOME>/hermes-agent`, с пометкой, что он оттуда. Шлюз живьём импортирует из
    самого checkout (2026-09-30) — там `.git` есть."""
    head = head_of(root)
    if head:
        return head, ""
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        parts = os.path.normpath(root).split(os.sep)
        if "installs" in parts:
            home = os.sep.join(parts[:parts.index("installs")])
    if home:
        head = head_of(os.path.join(home, "hermes-agent"))
        if head:
            return head, "source checkout"
    return "", ""


def head_of(root: str) -> str:
    """Короткий коммит checkout по файлам `.git`, без запуска git; "" — не узнать."""
    git = os.path.join(root, ".git")
    try:
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as fh:
            head = fh.read().strip()
        if not head.startswith("ref:"):
            return head[:10]
        ref = head[4:].strip()
        path = os.path.join(git, *ref.split("/"))
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()[:10]
        with open(os.path.join(git, "packed-refs"), encoding="utf-8") as fh:
            for line in fh:
                if line.rstrip().endswith(" " + ref):
                    return line.split()[0][:10]
    except OSError:
        pass
    return ""
