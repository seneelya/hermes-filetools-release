// desktop/src/adapter.js
import * as sdk from "@hermes/plugin-sdk";

// desktop/src/core-format.js
var KIND_TONE = { ok: "good", "no-change": "muted", error: "bad" };
function tokensLabel(tokens) {
  const n2 = typeof tokens === "number" && isFinite(tokens) ? Math.max(0, Math.round(tokens)) : 0;
  if (n2 < 1e3) return String(n2);
  if (n2 < 1e4) return `${(n2 / 1e3).toFixed(1).replace(/\.0$/, "")}k`;
  return `${Math.round(n2 / 1e3)}k`;
}
var READ_STATE_TONE = {
  verbatim: "good",
  // строка всё ещё держит наши байты
  compacted: "warn",
  // строку переписала ИХ компакция
  removed_from_context: "bad",
  // человек убрал операцию из контекста: содержимого НОЛЬ
  evicted: "bad",
  // строки в активном окне больше нет
  unknown: "muted"
  // сопоставить не удалось — и мы этого НЕ скрываем
};
var READ_STATE_LABEL = {
  verbatim: "verbatim",
  compacted: "compacted",
  // Ровно то слово, что стоит в заголовке строки, и ровно в том же виде. Русский перевод
  // рядом с английским токеном давал двуязычную кашу («удалено из контекста — removed from
  // the context by a human») — владелец 2026-09-09: «статус — removed from context! точка».
  // Здесь единственное место во всём словаре, где подпись НЕ по-русски, и это не
  // небрежность: остальные состояния — наши слова о чужом факте, а это слово ЗАПИСАНО в
  // строке, которую читает модель, и расходиться с ней нельзя.
  removed_from_context: "removed from context",
  evicted: "evicted",
  unknown: "unmatched"
};
function formatWhen(ts) {
  const ms = typeof ts === "number" && isFinite(ts) ? ts * 1e3 : NaN;
  if (!isFinite(ms)) return "";
  const d = new Date(ms);
  if (isNaN(d.getTime())) return "";
  const p = (n2) => String(n2).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function formatDuration(sec) {
  if (typeof sec !== "number" || !isFinite(sec) || sec < 0) return "";
  if (sec < 10) return `${sec.toFixed(2)}s`;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  return `${m}m ${Math.round(sec - m * 60)}s`;
}

// desktop/src/format-vocab.js
var FORMAT_DEFAULTS = Object.freeze({
  block_symbol: "\u25A0",
  anchor_symbol: "\u2693",
  separator_symbol: "\u25CB",
  comment_fallback: "\u25C6",
  header_separator: "\u25CB\u25CB\u25CB",
  block_open: "\u25A0BLOCK",
  block_close: "\u25A0END"
});
var TOOL_DEFAULTS = Object.freeze({
  icon: Object.freeze({
    read_file: "\u25E7",
    write_file: "\u271A",
    patch: "\u0394",
    search_files: "\u2315",
    terminal: "\u276F",
    execute_code: "\u25B7",
    process: "\u27F3",
    vision_analyze: "\u25C9"
  }),
  header: Object.freeze({
    read_file: Object.freeze({
      file: "File",
      size: "Size",
      modified: "Modified",
      encoding: "Encoding",
      file_tag: "FileTag"
    }),
    patch: Object.freeze({ lines: "Lines", unchanged: "Unchanged", dry_run: "Dry run" }),
    terminal: Object.freeze({ exit_code: "Exit code" }),
    vision_analyze: Object.freeze({ success: "Success" })
  })
});
var FOREIGN_ICON = "\u25C6";
var vocab = { ...FORMAT_DEFAULTS };
var tools = {};
var cache = null;
function applyTools(src) {
  const next = {};
  const obj = (v) => v && typeof v === "object" && !Array.isArray(v) ? v : null;
  for (const [name, raw] of Object.entries(obj(src) || {})) {
    const t = obj(raw);
    if (!t) continue;
    const header = {};
    for (const [role, word] of Object.entries(obj(t.header) || {})) {
      if (typeof word === "string" && word !== "") header[role] = word;
    }
    const call = obj(t.call) || {};
    next[name] = {
      icon: typeof t.icon === "string" ? t.icon : "",
      header,
      call: {
        params: (Array.isArray(call.params) ? call.params : []).filter((p) => obj(p) && typeof p.name === "string"),
        body: (Array.isArray(call.body) ? call.body : []).filter((k2) => typeof k2 === "string")
      }
    };
  }
  tools = next;
  cache = null;
  return tools;
}
function toolIcon(name) {
  const own = tools[name] && tools[name].icon;
  return own || TOOL_DEFAULTS.icon[name] || FOREIGN_ICON;
}
function headerWord(tool, role) {
  const fromServer = tools[tool] && tools[tool].header[role];
  if (fromServer) return fromServer;
  const fallback = TOOL_DEFAULTS.header[tool];
  return fallback && fallback[role] || "";
}
function toolCall(name) {
  return tools[name] ? tools[name].call : null;
}
function headerWords() {
  const header = tools.read_file && Object.keys(tools.read_file.header).length ? tools.read_file.header : TOOL_DEFAULTS.header.read_file;
  return Object.entries(header).filter(([role]) => role in TOOL_DEFAULTS.header.read_file || !(role in COMMON_ROLES)).map(([, word]) => word + ":");
}
var COMMON_ROLES = Object.freeze({ op: 1, tool: 1, size: 1, state: 1 });
function escapeRe(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
function applyFormatting(formatting) {
  const src = formatting && typeof formatting === "object" ? formatting : {};
  const next = {};
  for (const key of Object.keys(FORMAT_DEFAULTS)) {
    const value = src[key];
    next[key] = typeof value === "string" && value !== "" ? value : FORMAT_DEFAULTS[key];
  }
  vocab = next;
  cache = null;
  return vocab;
}
function formatVocab() {
  return vocab;
}
function blockOpen() {
  return vocab.block_open;
}
function built() {
  if (cache) return cache;
  const block = escapeRe(vocab.block_open);
  const end = escapeRe(vocab.block_close);
  const anchor = escapeRe(vocab.anchor_symbol);
  const sep = escapeRe(vocab.header_separator);
  const hash = escapeRe(vocab.comment_fallback);
  const words = headerWords().map(escapeRe).join("|");
  const word = (tool, role) => escapeRe(headerWord(tool, role));
  cache = {
    // Маркер может быть голым или обёрнутым: в markdown его прячут в HTML-комментарий, в
    // JS/Python — за `//` или `#`, у файлов без известного комментария — за `comment_fallback`.
    // Тег между маркером и телом (`■BLOCK x0a :`) не мешает — матчим только НАЧАЛО строки.
    own: new RegExp(`^\\s*(\u2192\\s*)?(<!--\\s*)?(\\/\\/\\s*|#\\s*|${hash}\\s*)?(${block}(?!\\w)|${end}(?!\\w)|${anchor}|${sep}|${words})`),
    separator: new RegExp(sep),
    blockTag: new RegExp(`${block}\\s*(x[0-9a-fA-F]{2,4})\\s*:`),
    // Разбор значений строк шапки. Слово — из справочника по роли, формат значения — логика
    // панели. Где стоит `^\W*`, матчим НАЧАЛО строки: то же слово в выводе самой команды не
    // должно подменять её статус.
    exitCode: new RegExp(`^\\W*${word("terminal", "exit_code")}:\\s*(-?\\d+)`, "m"),
    success: new RegExp(`^\\W*${word("vision_analyze", "success")}:\\s*(True|False)\\b`, "m"),
    noChange: new RegExp(`${word("patch", "lines")}:\\s*\\d+\\s*\\(unchanged\\)`),
    patchLines: new RegExp(`${word("patch", "lines")}: (\\d+) -> (\\d+)`),
    nothingChanged: new RegExp(`^\\W*(${word("patch", "unchanged")}|${word("patch", "dry_run")}):`, "m")
  };
  return cache;
}
function ownMarkerRe() {
  return built().own;
}
function patchLinesRe() {
  return built().patchLines;
}
function nothingChangedRe() {
  return built().nothingChanged;
}
function separatorRe() {
  return built().separator;
}
function blockTagRe() {
  return built().blockTag;
}

// desktop/src/core.js
var COMMAND_TOOLS = ["terminal", "execute_code"];
function isCommandTool(name) {
  return COMMAND_TOOLS.includes(name);
}
function asRecord(v) {
  return v && typeof v === "object" && !Array.isArray(v) ? v : null;
}
function brickTitles(tool, args) {
  const a = asRecord(args) || {};
  const path = typeof a.path === "string" && a.path ? a.path : "";
  switch (tool) {
    case "read_file": {
      const parts = [];
      if (Number.isFinite(a.line) && a.line > 1) parts.push(`line ${a.line}`);
      if (Number.isFinite(a.count) && a.count !== 2e3) parts.push(`+${a.count}`);
      return { title: path || "read", subtitle: parts.join(", ") };
    }
    case "write_file": {
      const insert = a.insert_line === void 0 ? null : Number.isFinite(a.insert_line) ? a.insert_line : String(a.insert_line);
      let subtitle;
      if (insert == null || insert === 0) subtitle = "overwrite";
      else if (insert === -1) subtitle = "append @ end";
      else subtitle = `insert @ line ${insert}`;
      return { title: path || "write", subtitle };
    }
    case "patch": {
      const subtitle = Number.isFinite(a.line) && a.line > 0 ? `@ line ${a.line}` : "";
      return { title: path || "patch", subtitle };
    }
    case "search_files": {
      const pattern = typeof a.pattern === "string" && a.pattern ? `pattern "${a.pattern}"` : "";
      const glob = typeof a.glob === "string" && a.glob && a.glob !== "*" ? ` \xB7 ${a.glob}` : "";
      return { title: path || "search", subtitle: pattern + glob };
    }
    case "terminal": {
      const cmd = typeof a.command === "string" ? a.command.replace(/\s*\n\s*/g, " \u23CE ").trim() : "";
      const workdir = typeof a.workdir === "string" && a.workdir ? a.workdir : "";
      return { title: cmd || "terminal", subtitle: workdir };
    }
    case "execute_code": {
      const code = typeof a.code === "string" ? a.code : "";
      const lines = code.split("\n").map((l) => l.trim()).filter((l) => l !== "");
      const first = lines.find((l) => !/^(import|from)\s/.test(l)) || lines[0] || "";
      const subtitle = lines.length > 1 ? `${lines.length} lines` : "";
      return { title: first || "execute_code", subtitle };
    }
    case "vision_analyze": {
      const src = typeof a.image_url === "string" ? a.image_url : "";
      const title = src.startsWith("data:") ? "inline image (data: URL)" : src || "vision_analyze";
      const q = typeof a.question === "string" ? a.question.replace(/\s*\n\s*/g, " \u23CE ").trim() : "";
      return { title, subtitle: q };
    }
    default:
      return null;
  }
}
function countLines(text) {
  if (typeof text !== "string" || text === "") return 0;
  const n2 = text.split("\n").length;
  return text.endsWith("\n") ? n2 - 1 : n2;
}
function diffFromArgs(tool, args) {
  const a = args || {};
  if (tool === "patch" && (typeof a.new_string === "string" || typeof a.old_string === "string")) {
    return { added: countLines(a.new_string), removed: countLines(a.old_string) };
  }
  if (tool === "write_file" && typeof a.content === "string") {
    return { added: countLines(a.content), removed: a.insert_line != null ? 0 : null };
  }
  return null;
}
var LIVE_PREFIX = "_live:";
function viewKeyFor(storedId, liveKey) {
  return storedId || liveKey || null;
}
function isPersistableKey(key) {
  return typeof key === "string" && key !== "" && !key.startsWith(LIVE_PREFIX);
}
function opIsGone(fact) {
  if (!fact) return false;
  return fact.missing_row === true || fact.state === ERASED_STATE;
}
var ERASED_STATE = "erased";
function isErasedBrick(brick) {
  return !!(brick && brick.meta && brick.meta.state === ERASED_STATE);
}
function visibleBricks(bricks, showErased) {
  const list = Array.isArray(bricks) ? bricks : [];
  if (showErased) return { bricks: list, hidden: 0 };
  const kept = list.filter((b) => !isErasedBrick(b));
  return { bricks: kept, hidden: list.length - kept.length };
}
function liveContextOps(contextOps2) {
  return (contextOps2 || []).filter((fact) => !opIsGone(fact));
}
var SEGMENT_TOOLS = /* @__PURE__ */ new Set(["read_file", "patch", "write_file"]);
var GONE_STATES = /* @__PURE__ */ new Set(["compacted", "removed_from_context"]);
var REMOVED_STATE = "removed_from_context";
var isRemoved = (f) => f && f.state === REMOVED_STATE;
function segmentKind(op) {
  if (op.tool === "patch" || op.tool === "write_file") return "patch";
  return op.nested ? "read_nested" : "read";
}
var isNestedRead = (f) => !!f.nested && f.tool === "read_file";
function contextParts(f) {
  return isNestedRead(f) ? f.seen || [] : f.parts;
}
function seenParts(op) {
  if (!Array.isArray(op.seen)) return [];
  return op.seen.filter((p) => Array.isArray(p) && p[0] >= 1 && p[1] >= p[0]).map(([from, to]) => [from, to]);
}
function subtractParts(parts, cut) {
  let rest = parts.map(([from, to]) => [from, to]);
  for (const [cfrom, cto] of cut) {
    const next = [];
    for (const [from, to] of rest) {
      if (cto < from || cfrom > to) {
        next.push([from, to]);
        continue;
      }
      if (from < cfrom) next.push([from, cfrom - 1]);
      if (to > cto) next.push([cto + 1, to]);
    }
    rest = next;
  }
  return rest;
}
function shadows(op, total) {
  if (op.tool !== "write_file") return true;
  const first = op.parts[0];
  const lastPart = op.parts[op.parts.length - 1];
  return !!first && first[0] === 1 && lastPart[1] >= total;
}
function hasPosition(op) {
  return (op.from || 0) >= 1 && (op.to || 0) >= (op.from || 0);
}
function readParts(op) {
  const ok = Array.isArray(op.parts) && op.parts.length > 1 && op.parts.every(
    (p) => Array.isArray(p) && p[0] >= 1 && p[1] >= p[0]
  );
  return ok ? op.parts.map(([a, b]) => [a, b]) : [[op.from, op.to]];
}
function foldParts(parts, at, delta) {
  const out = [];
  for (const [from, to] of parts) {
    if (from >= at) {
      out.push([from + delta, to + delta]);
    } else if (to >= at) {
      if (delta > 0) {
        out.push([from, at - 1]);
        out.push([at + delta, to + delta]);
      } else {
        out.push([from, Math.max(from, to + delta)]);
      }
    } else {
      out.push([from, to]);
    }
  }
  return out.filter(([from, to]) => to >= from);
}
function buildContextView(ops) {
  const all = (Array.isArray(ops) ? ops.slice() : []).filter(Boolean).sort((a, b) => (a.seq || 0) - (b.seq || 0));
  const sorted = all.filter((o) => o.path);
  const byPath = /* @__PURE__ */ new Map();
  const desync = [];
  const order = {};
  for (const op of all) {
    if (op.op && !(op.op in order)) order[op.op] = op.seq || 0;
  }
  for (const op of sorted) {
    let file = byPath.get(op.path);
    if (!file) {
      file = { path: op.path, name: basename(op.path), total: 0, folded: [] };
      byPath.set(op.path, file);
    }
    const positioned = hasPosition(op);
    const fullReplace = positioned && !!op.total && op.from === 1 && op.to >= op.total;
    const delta = fullReplace ? 0 : (op.added || 0) - (op.removed || 0);
    if (delta !== 0 && positioned) {
      for (const prev of file.folded) {
        prev.parts = foldParts(prev.parts, op.from, delta);
        if (prev.seen) prev.seen = foldParts(prev.seen, op.from, delta);
      }
    }
    if (op.total) {
      const implied = file.total + delta;
      if (file.total && !fullReplace && implied !== op.total) {
        desync.push({ path: op.path, op: op.op, implied, recorded: op.total });
      }
      file.total = op.total;
    }
    file.folded.push({
      // Диапазон — НАБОР кусков, а не одна пара чисел: вставка внутрь прочитанного разрывает его
      // надвое (см. foldParts). Обычный случай — один кусок. Чтение нескольких кусков разом
      // (`--line 120,450`, Plan30) приносит их в `parts`, а `from`/`to` — их общий охват.
      parts: positioned ? readParts(op) : [],
      pos: positioned,
      op: op.op,
      tool: op.tool,
      state: op.state,
      nested: op.nested,
      // Только у вложенного чтения: что из него дошло до модели (см. `contextParts`).
      seen: positioned && isNestedRead(op) ? seenParts(op) : null
    });
  }
  const files = [];
  const stale = {};
  for (const file of byPath.values()) {
    const drawable = file.folded.filter(
      (f) => f.pos && SEGMENT_TOOLS.has(f.tool) && !GONE_STATES.has(f.state)
    );
    if (!drawable.length && (!file.total || file.folded.every((f) => GONE_STATES.has(f.state)))) continue;
    const total = file.total || Math.max(1, ...file.folded.flatMap((f) => f.parts.map((p) => p[1])), 1);
    const last = drawable.length - 1;
    const pieces = (f) => {
      if (!isNestedRead(f) || !f.seen || !f.seen.length) {
        return f.parts.map(([from, to]) => ({ from, to, kind: segmentKind(f) }));
      }
      return [
        ...f.seen.map(([from, to]) => ({ from, to, kind: "read" })),
        ...subtractParts(f.parts, f.seen).map(([from, to]) => ({ from, to, kind: "read_nested" }))
      ].sort((a, b) => a.from - b.from);
    };
    const segments = drawable.flatMap((f, i) => pieces(f).map(({ from, to, kind }) => ({
      from,
      to,
      kind,
      rank: last - i,
      op: f.op,
      // Зажато в [0, 100]. Без этого сегмент физически вылезал за габариты плитки: при вставке
      // сложенный диапазон может превысить записанную длину (последней операцией по файлу было
      // чтение, и `total` остался от неё), а тогда правый край уходит за край бара.
      pct: [
        round1(clampPct((from - 1) / total * 100)),
        round1(clampPct(to / total * 100))
      ]
    })));
    files.push({
      path: file.path,
      name: file.name,
      total,
      segments,
      // УДАЛЁННЫХ здесь нет — ни в третьей строке карточки, ни в таблице попапа. Требование
      // владельца 2026-09-09: «уже на карточку не влияют, но в списке они есть». Плитка
      // отвечает на вопрос «что модель по этому файлу сейчас видит», а удалённая операция на
      // него не отвечает ничем: ни сегмента, ни данных. Её дом — стакан, где у неё своя
      // карточка со своим состоянием.
      //
      // `compacted` при этом ОСТАЁТСЯ (поправка 1 к шву): её строка всё ещё занимает контекст
      // и тратит токены, просто опереться на неё нельзя. Разница не косметическая — одна
      // операция в контексте есть, другой в нём нет вовсе.
      op_ids: file.folded.filter((f) => f.op && !isRemoved(f)).map((f) => f.op),
      // Строки для таблицы в попапе: тул + адрес + диапазон, по КАЖДОЙ операции, что касалась
      // файла, включая беспозиционные и не попавшие на бар. Поправка 3 к шву (владелец,
      // 2026-09-07): попапу нужно имя тула на операцию, а `segments` знает только `kind`
      // (read/patch/read_nested) и покрывает лишь нарисованное.
      rows: file.folded.filter((f) => f.op && !isRemoved(f)).map((f) => ({
        op: f.op,
        tool: f.tool || "",
        // В таблице показываем ОХВАТ операции — от первой прочитанной строки до последней, даже
        // если посередине образовался разрыв от вставки. Перечислять куски там значило бы менять
        // ширину колонки от того, что случилось ПОЗЖЕ; сами куски видны на баре.
        from: f.parts.length ? f.parts[0][0] : null,
        to: f.parts.length ? f.parts[f.parts.length - 1][1] : null,
        nested: !!f.nested,
        state: f.state || ""
      }))
    });
    for (let i = 0; i < file.folded.length; i++) {
      const own = file.folded[i];
      if (!own.op || !own.pos || !SEGMENT_TOOLS.has(own.tool)) continue;
      const ownParts = contextParts(own);
      const span = ownParts.reduce((n2, [from, to]) => n2 + (to - from + 1), 0);
      if (!span) continue;
      const laterRanges = [];
      for (let j = i + 1; j < file.folded.length; j++) {
        const later = file.folded[j];
        if (!later.pos || !shadows(later, total)) continue;
        for (const [ofrom, oto] of ownParts) {
          for (const [lfrom, lto] of contextParts(later)) {
            const lo = Math.max(ofrom, lfrom);
            const hi = Math.min(oto, lto);
            if (hi >= lo) laterRanges.push([lo, hi]);
          }
        }
      }
      stale[own.op] = Math.min(100, Math.round(mergedLength(laterRanges) / span * 100));
    }
  }
  return { files, stale, desync, order };
}
function mergedLength(ranges) {
  if (!ranges.length) return 0;
  const sortedRanges = ranges.slice().sort((a, b) => a[0] - b[0]);
  let covered = 0;
  let [lo, hi] = sortedRanges[0];
  for (let i = 1; i < sortedRanges.length; i++) {
    const [a, b] = sortedRanges[i];
    if (a > hi + 1) {
      covered += hi - lo + 1;
      lo = a;
      hi = b;
    } else if (b > hi) {
      hi = b;
    }
  }
  return covered + hi - lo + 1;
}
function basename(path) {
  const parts = String(path || "").split(/[\/]/);
  return parts[parts.length - 1] || String(path || "");
}
function round1(n2) {
  return Math.round(n2 * 10) / 10;
}
function clampPct(n2) {
  if (!Number.isFinite(n2)) return 0;
  return Math.min(100, Math.max(0, n2));
}
function contextOpsFrom(res) {
  const r = asRecord(res);
  if (!r) return { ops: [], note: "the route answered with a non-object" };
  const ops = (Array.isArray(r.ops) ? r.ops : []).filter((o) => asRecord(o));
  const err = typeof r.error === "string" ? r.error : "";
  const note = typeof r.note === "string" ? r.note : "";
  if (ops.length > 0) return { ops, note: err };
  return { ops, note: err || note };
}
function layoutMessages(carriages, messages) {
  const facts = /* @__PURE__ */ new Map();
  const order = [];
  (Array.isArray(messages) ? messages : []).forEach((m) => {
    const f = asRecord(m);
    if (!f) return;
    const address = String(f.msg || "");
    order.push({ address, fact: f });
    if (address && !facts.has(address)) facts.set(address, f);
  });
  const seen = /* @__PURE__ */ new Set();
  const rows = [];
  (Array.isArray(carriages) ? carriages : []).forEach((c, ci) => {
    const car = asRecord(c);
    if (!car) return;
    const steps = Array.isArray(car.steps) ? car.steps : [];
    const foreign = new Set((Array.isArray(car.foreign) ? car.foreign : []).map(String));
    const unrecorded = new Set((Array.isArray(car.unrecorded) ? car.unrecorded : []).map(String));
    const opener = car.opener ? String(car.opener) : "";
    rows.push({
      kind: "carriage",
      key: `car-${ci}`,
      index: ci + 1,
      opener,
      count: steps.reduce((n2, s) => n2 + (Array.isArray(s) ? s.length : 0), 0)
    });
    let stepNo = 0;
    steps.forEach((step, si) => {
      const members = (Array.isArray(step) ? step : []).map(String);
      const isOpener = members.length === 1 && members[0] === opener && opener;
      if (!isOpener) {
        stepNo += 1;
        rows.push({
          kind: "step",
          key: `car-${ci}-step-${si}`,
          index: stepNo,
          count: members.length
        });
      }
      members.forEach((address) => {
        seen.add(address);
        rows.push({
          kind: "msg",
          key: `msg-${address || `${ci}-${si}-${rows.length}`}`,
          address,
          opener: address === opener && !!opener,
          foreign: foreign.has(address),
          unrecorded: unrecorded.has(address),
          fact: facts.get(address) || { msg: address }
        });
      });
    });
  });
  const orphans = order.filter((o) => !o.address || !seen.has(o.address));
  orphans.forEach((o, i) => {
    rows.push({
      kind: "orphan",
      key: `orphan-${o.address || i}`,
      address: o.address,
      foreign: false,
      unrecorded: false,
      fact: o.fact
    });
  });
  return { rows, placed: seen.size, orphans: orphans.length };
}
function shouldRefetchOnStatus(event) {
  const e = asRecord(event);
  const p = asRecord(e && e.payload) || {};
  return p.kind === "compacted";
}
function sameMessages(cur, res) {
  const a = asRecord(cur) || {};
  const b = asRecord(res) || {};
  const msgs = (x) => Array.isArray(x.messages) ? x.messages : [];
  const cars = (x) => Array.isArray(x.carriages) ? x.carriages : [];
  if (msgs(a).length !== msgs(b).length || cars(a).length !== cars(b).length) return false;
  return JSON.stringify([msgs(a), cars(a)]) === JSON.stringify([msgs(b), cars(b)]);
}

// desktop/src/adapter.js
function makeAtom(initial) {
  const mk = typeof sdk.atom === "function" ? sdk.atom : null;
  if (mk) return mk(initial);
  let cur = initial;
  return { get: () => cur, set: (x) => {
    cur = x;
  }, listen: () => () => {
  } };
}
var VIEW_INITIAL = { sessionId: null, mode: "mirror", showErased: false };
var $view = makeAtom(VIEW_INITIAL);
function useView() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($view) || VIEW_INITIAL;
}
function setShowErased(on) {
  const cur = $view.get() || VIEW_INITIAL;
  $view.set(Object.assign({}, cur, { showErased: !!on }));
}
var $fileContext = makeAtom({ ops: [] });
function useFileContext() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($fileContext) || { ops: [] };
}
var COMPACT_INITIAL = {
  open: false,
  mode: false,
  picked: {},
  dragging: false,
  dragMode: null,
  confirming: false,
  busy: false,
  note: "",
  ok: null,
  wish: "",
  sets: null,
  menu: {},
  menuTouched: false,
  preview: false
};
var $compact = makeAtom(COMPACT_INITIAL);
function useCompact() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($compact) || COMPACT_INITIAL;
}
var RUN_INITIAL = { running: false, sid: "" };
var $compactRun = makeAtom(RUN_INITIAL);
function useCompactRun() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($compactRun) || RUN_INITIAL;
}
function pollCompactRun(sid) {
  const prev = $compactRun.get() || RUN_INITIAL;
  if (prev.sid !== sid) $compactRun.set(Object.assign({}, RUN_INITIAL, { sid }));
  adapter.rest(`/context-compact-status?session=${encodeURIComponent(sid)}`).then((res) => {
    if (viewKey() !== sid) return;
    const r = res && typeof res === "object" ? res : {};
    const cur = $compactRun.get() || RUN_INITIAL;
    if (!r.running && !cur.running) return;
    const doneAt = !r.running && cur.running ? Date.now() : 0;
    $compactRun.set(Object.assign({}, r, { sid, doneAt }));
  }).catch(() => {
  });
}
var adapter = { hasEvents: false, state: null, storageGet: () => Promise.resolve(void 0), storageSet: () => {
}, onDispose: () => {
} };
function createAdapter(ctx) {
  const h = sdk && typeof sdk === "object" ? sdk.host : void 0;
  return {
    // feature-detect двери (таск п.9): новые двери проверять через typeof host.X === 'function'.
    hasEvents: !!(h && typeof h.onEvent === "function"),
    state: h && h.state && typeof h.state === "object" ? h.state : null,
    storageGet(key) {
      try {
        const s = ctx && ctx.storage;
        if (!s || typeof s.get !== "function") return Promise.resolve(void 0);
        return Promise.resolve(s.get(key));
      } catch {
        return Promise.resolve(void 0);
      }
    },
    storageSet(key, value) {
      try {
        const s = ctx && ctx.storage;
        if (!s || typeof s.set !== "function") return;
        const r = s.set(key, value);
        if (r && typeof r.catch === "function") r.catch(() => {
        });
      } catch {
      }
    },
    hasRest: !!(ctx && typeof ctx.rest === "function"),
    // `opts` прокидывается насквозь: их `ctx.rest(path, {method, body})` это умеет
    // (`api/plugins.ts::PluginRestOptions`), а наша обёртка второй аргумент раньше теряла —
    // и записать что-либо с панели было нечем. Не передан — обычный GET, как и было.
    rest(path, opts) {
      try {
        if (!ctx || typeof ctx.rest !== "function") return Promise.reject(new Error("ctx.rest \u043D\u0435\u0434\u043E\u0441\u0442\u0443\u043F\u0435\u043D"));
        return Promise.resolve(opts === void 0 ? ctx.rest(path) : ctx.rest(path, opts));
      } catch (err) {
        return Promise.reject(err);
      }
    },
    // Живой близнец `rest` (Plan11-Task06, дверь из Recon19): вебсокет в наше же пространство,
    // JSON-фреймы в колбэк, авто-реконнект и disposer — всё на их стороне.
    // ВАЖНО: на OAuth-развёртывании их реализация резолвится в НО-ОП (`api/plugins.ts:106`), и
    // комментарий к типу говорит прямо — «accelerator over your polling, never a replacement».
    // Поэтому ни одна ветка UI не вправе зависеть от того, приехал ли фрейм; несущий путь — HTTP.
    hasSocket: !!(ctx && typeof ctx.socket === "function"),
    socket(path, onMessage) {
      try {
        if (!ctx || typeof ctx.socket !== "function") return () => {
        };
        const off = ctx.socket(path, onMessage);
        return typeof off === "function" ? off : () => {
        };
      } catch {
        return () => {
        };
      }
    },
    onDispose(fn) {
      try {
        ctx && typeof ctx.onDispose === "function" && ctx.onDispose(fn);
      } catch {
      }
    }
  };
}
function initAdapter(ctx) {
  adapter = createAdapter(ctx);
  contextFetched.clear();
  return adapter;
}
function hasRest() {
  return adapter.hasRest;
}
function loadConfig() {
  return adapter.rest("/config").then((data) => {
    if (data && !data.error) {
      applyFormatting(data.formatting);
      applyTools(data.tools);
    }
    return data;
  });
}
function saveConfig(patch) {
  return adapter.rest("/config", { method: "POST", body: patch }).then((data) => {
    if (data && !data.error) {
      applyFormatting(data.formatting);
      applyTools(data.tools);
    }
    return data;
  });
}
function reloadPlugin() {
  return adapter.rest("/config-reload", { method: "POST" });
}
function checkSeams(full) {
  return full ? adapter.rest("/seams-check?tests=1", { timeoutMs: 12e5 }) : adapter.rest("/seams-check");
}
var STORED_SESSION_ATOM = "focusedStoredSessionId";
function storedSessionId() {
  const a = adapter.state && adapter.state[STORED_SESSION_ATOM];
  if (a && typeof a.get === "function") {
    const v = a.get();
    if (typeof v === "string" && v !== "") return v;
  }
  return null;
}
function viewKey() {
  return viewKeyFor(storedSessionId(), null);
}
var contextFetched = /* @__PURE__ */ new Set();
var contextOps = {};
var NO_OPS = [];
var CONTEXT_REFETCH_MS = 300;
var contextTimer = null;
function ensureContextState(sid) {
  if (!isPersistableKey(sid) || contextFetched.has(sid)) return;
  contextFetched.add(sid);
  if (!adapter.hasRest) return;
  adapter.rest(`/context-state?session=${encodeURIComponent(sid)}`).then((res) => {
    const { ops, note } = contextOpsFrom(res);
    contextOps[sid] = ops;
    if (note) console.info("[hermes-filetools] /context-state:", note);
    publishFileContext(sid);
  }).catch((err) => {
    contextOps[sid] = NO_OPS;
    console.warn("[hermes-filetools] /context-state \u043D\u0435\u0434\u043E\u0441\u0442\u0443\u043F\u0435\u043D:", err && err.message || err);
    publishFileContext(sid);
  });
}
var MESSAGES_PREVIEW = 100;
var MESSAGES_INITIAL = {
  sessionId: null,
  carriages: [],
  messages: [],
  note: "",
  loading: false
};
var $messages = makeAtom(MESSAGES_INITIAL);
function useMessages() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($messages) || MESSAGES_INITIAL;
}
var _fullText = /* @__PURE__ */ new Map();
var _fullInflight = /* @__PURE__ */ new Map();
var _fullEpoch = 0;
function forgetFullText(sid) {
  _fullEpoch += 1;
  const prefix = sid + "|";
  for (const key of Array.from(_fullText.keys())) if (key.startsWith(prefix)) _fullText.delete(key);
  for (const key of Array.from(_fullInflight.keys())) if (key.startsWith(prefix)) _fullInflight.delete(key);
}
var EMPTY_FULL = { text: "", call: null };
function loadFullMessage(address) {
  const key = String(address || "");
  const sid = viewKey();
  if (!key || !isPersistableKey(sid) || !adapter.hasRest) return Promise.resolve(EMPTY_FULL);
  const cacheKey = sid + "|" + key;
  if (_fullText.has(cacheKey)) return Promise.resolve(_fullText.get(cacheKey));
  const pending = _fullInflight.get(cacheKey);
  if (pending) return pending;
  const epoch = _fullEpoch;
  const request = adapter.rest(`/messages?session=${encodeURIComponent(sid)}&preview=1&full=${encodeURIComponent(key)}`).then((res) => {
    const r = asRecord(res);
    const list = Array.isArray(r.messages) ? r.messages : [];
    const hit = asRecord(list.find((m) => asRecord(m).msg === key));
    const text = String(hit.text || "");
    const call = hit.call && typeof hit.call === "object" ? hit.call : null;
    const packed = { text, call };
    if (epoch === _fullEpoch && (text || call)) _fullText.set(cacheKey, packed);
    return packed;
  }).catch(() => EMPTY_FULL).finally(() => {
    if (_fullInflight.get(cacheKey) === request) _fullInflight.delete(cacheKey);
  });
  _fullInflight.set(cacheKey, request);
  return request;
}
function loadMessageText(address) {
  return loadFullMessage(address).then((hit) => String(hit && hit.text || ""));
}
function loadMessageCall(address) {
  return loadFullMessage(address).then((hit) => hit && hit.call || null);
}
function exportMessage(address) {
  const key = String(address || "");
  const sid = viewKey();
  if (!key) return Promise.resolve({ ok: false, note: "no address" });
  if (!adapter.hasRest || !isPersistableKey(sid)) {
    return Promise.resolve({ ok: false, note: "this chat is not saved yet" });
  }
  return adapter.rest("/context-export", { method: "POST", body: { session: sid, msgs: [key] } }).then((res) => {
    const r = asRecord(res);
    if (r.ok !== true) return { ok: false, note: String(r.error || "not saved") };
    const written = Array.isArray(r.written) ? r.written : [];
    const path = String(asRecord(written[0]).path || "");
    return { ok: true, note: path ? `saved ${path}` : "saved" };
  }).catch((err) => ({ ok: false, note: `request failed: ${err && err.message || err}` }));
}
function copyMessage(address, fallback) {
  const key = String(address || "");
  return loadMessageText(key).then((text) => {
    const body = text || String(fallback || "");
    if (!body) return { ok: false, note: "nothing to copy" };
    const clip = typeof navigator !== "undefined" && navigator && navigator.clipboard;
    if (!clip || typeof clip.writeText !== "function") {
      return { ok: false, note: "no clipboard in this build" };
    }
    return Promise.resolve(clip.writeText(body)).then(() => ({ ok: true, note: `copied ${body.length} chars` })).catch((err) => ({ ok: false, note: `copy failed: ${err && err.message || err}` }));
  }).catch((err) => ({ ok: false, note: `copy failed: ${err && err.message || err}` }));
}
function readClipboard() {
  const clip = typeof navigator !== "undefined" && navigator && navigator.clipboard;
  if (!clip || typeof clip.readText !== "function") return Promise.resolve("");
  return Promise.resolve(clip.readText()).then((t) => String(t || "")).catch(() => "");
}
function catchNextPaste(onText) {
  if (typeof window === "undefined" || !window || typeof window.addEventListener !== "function") {
    return () => {
    };
  }
  const handler = (e) => {
    const data = e && e.clipboardData;
    const text = data && typeof data.getData === "function" ? String(data.getData("text/plain") || data.getData("text") || "") : "";
    if (e && typeof e.preventDefault === "function") e.preventDefault();
    if (e && typeof e.stopPropagation === "function") e.stopPropagation();
    window.removeEventListener("paste", handler, true);
    onText(text);
  };
  window.addEventListener("paste", handler, true);
  return () => window.removeEventListener("paste", handler, true);
}
var editNotes = /* @__PURE__ */ new Map();
function takeEditNote(address) {
  const key = String(address || "");
  const note = editNotes.get(key) || "";
  editNotes.delete(key);
  return note;
}
function editWarning(lines) {
  const last = (Array.isArray(lines) ? lines : []).map(String).reverse().find((line) => line.startsWith("\u0418\u0422\u041E\u0413: \u043F\u0440\u0438\u043C\u0435\u043D\u0435\u043D\u043E"));
  const tail = last ? last.slice("\u0418\u0422\u041E\u0413: \u043F\u0440\u0438\u043C\u0435\u043D\u0435\u043D\u043E".length).replace(/^\s*—\s*/, "").trim() : "";
  return tail && !tail.startsWith("(\u0431\u0435\u0437 \u043B\u043E\u043A\u0430") ? tail : "";
}
function editMessage(address, text) {
  const key = String(address || "");
  const sid = viewKey();
  if (!key) return Promise.resolve({ ok: false, note: "no address" });
  if (!adapter.hasRest || !isPersistableKey(sid)) {
    return Promise.resolve({ ok: false, note: "this chat is not saved yet" });
  }
  return adapter.rest("/context-edit", { method: "POST", body: { session: sid, msg: key, content: String(text || "") } }).then((res) => {
    const r = asRecord(res) || {};
    if (r.ok !== true) return { ok: false, note: String(r.summary && r.summary !== "not applied" ? r.summary : r.error || "not saved") };
    const now = String(r.now || "");
    const warning = editWarning(r.lines);
    const note = `saved: ${key} \u2192 ${now || "?"}` + (warning ? ` \u2014 ${warning}` : "");
    if (now) editNotes.set(now, note);
    loadMessages();
    return { ok: true, now, note };
  }).catch((err) => ({ ok: false, note: `request failed: ${err && err.message || err}` }));
}
var EXPORT_INITIAL = { mode: false, picked: {}, busy: false, note: "", ok: null, dir: "" };
var $exportRail = makeAtom(EXPORT_INITIAL);
function useExportRail() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($exportRail) || EXPORT_INITIAL;
}
function setExportRail(patch) {
  $exportRail.set(Object.assign({}, $exportRail.get() || EXPORT_INITIAL, patch));
}
function setExportMode(on, initialPicked) {
  if (!on) {
    const prev = $exportRail.get() || EXPORT_INITIAL;
    setExportRail(Object.assign({}, EXPORT_INITIAL, { note: prev.note, ok: prev.ok, dir: prev.dir }));
    return;
  }
  setExportRail({
    mode: true,
    picked: Object.assign({}, initialPicked || {}),
    busy: false,
    note: "",
    ok: null,
    dir: ""
  });
  setCompactMode(false);
  setAddrOpMode(false);
  setLLMOpsMode(false);
}
function setExportPicks(picked) {
  const cur = $exportRail.get() || EXPORT_INITIAL;
  if (!cur.mode) return;
  setExportRail({ picked: Object.assign({}, picked || {}) });
}
function applyExport() {
  const cur = $exportRail.get() || EXPORT_INITIAL;
  if (!cur.mode || cur.busy) return Promise.resolve();
  const msgs = Object.keys(cur.picked || {});
  const sid = viewKey();
  if (!msgs.length) {
    setExportRail({ note: "nothing ticked", ok: false });
    return Promise.resolve();
  }
  if (!adapter.hasRest || !isPersistableKey(sid)) {
    setExportRail({ note: "this chat is not saved yet", ok: false });
    return Promise.resolve();
  }
  setExportRail({ busy: true, note: "", ok: null, dir: "" });
  return adapter.rest("/context-export", { method: "POST", body: { session: sid, msgs, snapshot: true } }).then((res) => {
    const r = asRecord(res);
    const written = Array.isArray(r.written) ? r.written.length : 0;
    const missing = Array.isArray(r.missing) ? r.missing.length : 0;
    if (r.ok !== true) {
      setExportRail({ busy: false, ok: false, note: String(r.error || (r.errors || [])[0] || "not exported") });
      return;
    }
    const paired = Array.isArray(r.paired) ? r.paired.length : 0;
    const note = `exported ${written}` + (paired ? ` (+${paired} calls)` : "") + ` \u2192 ${String(r.dir || "")}` + (missing ? ` \xB7 ${missing} not found` : "");
    setExportRail(Object.assign({}, EXPORT_INITIAL, { note, ok: true, dir: String(r.dir || "") }));
  }).catch((err) => setExportRail({ busy: false, ok: false, note: `request failed: ${err && err.message || err}` }));
}
var LLMOPS_INITIAL = { mode: false, picked: {}, busy: false, note: "", ok: null };
var $llmopsRail = makeAtom(LLMOPS_INITIAL);
var $llmopsWin = makeAtom(null);
var llmopsSeq = 0;
function useLLMOpsRail() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($llmopsRail) || LLMOPS_INITIAL;
}
function useLLMOpsWin() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($llmopsWin) || null;
}
function llmopsWindowOpen() {
  return !!$llmopsWin.get();
}
function setLLMOpsRail(patch) {
  $llmopsRail.set(Object.assign({}, $llmopsRail.get() || LLMOPS_INITIAL, patch));
}
function setLLMOpsMode(on) {
  if (!on) {
    const prev = $llmopsRail.get() || LLMOPS_INITIAL;
    setLLMOpsRail(Object.assign({}, LLMOPS_INITIAL, { note: prev.note, ok: prev.ok }));
    return;
  }
  setLLMOpsRail({ mode: true, picked: {}, busy: false, note: "", ok: null });
  setCompactMode(false);
  setExportMode(false);
  setAddrOpMode(false);
}
function setLLMOpsPicks(picked) {
  const cur = $llmopsRail.get() || LLMOPS_INITIAL;
  if (!cur.mode) return;
  setLLMOpsRail({ picked: Object.assign({}, picked || {}) });
}
function openLLMOpsWindow(sid, addrs, draft) {
  if ($llmopsWin.get()) return false;
  llmopsSeq += 1;
  $llmopsWin.set(Object.assign(
    { sid: String(sid || ""), addrs: (addrs || []).slice(), seq: llmopsSeq },
    draft ? { draft } : {}
  ));
  return true;
}
function closeLLMOpsWindow() {
  $llmopsWin.set(null);
}
function applyLLMOps() {
  const cur = $llmopsRail.get() || LLMOPS_INITIAL;
  if (!cur.mode) return;
  const addrs = Object.keys(cur.picked || {});
  const sid = viewKey();
  if (!addrs.length) {
    setLLMOpsRail({ note: "nothing ticked", ok: false });
    return;
  }
  if (!isPersistableKey(sid)) {
    setLLMOpsRail({ note: "this chat is not saved yet", ok: false });
    return;
  }
  if (!openLLMOpsWindow(sid, addrs)) {
    setLLMOpsRail({ note: "the LLMOps window is already open", ok: false });
    return;
  }
  setLLMOpsMode(false);
}
var LLMOPS_POLL_MS = 1500;
var LLMOPS_WAIT_MS = 15 * 60 * 1e3;
function waitLLMOpsRun(sid) {
  const started = Date.now();
  return new Promise((resolve) => {
    const tick = () => {
      adapter.rest(`/context-compact-status?session=${encodeURIComponent(sid)}`).then((res) => {
        const r = asRecord(res);
        if (r.running) {
          if (Date.now() - started > LLMOPS_WAIT_MS) {
            resolve({ ok: false, error: "no result after 15 min \u2014 see the status line" });
            return;
          }
          setTimeout(tick, LLMOPS_POLL_MS);
          return;
        }
        const lines = Array.isArray(r.lines) ? r.lines.map(String) : [];
        const report = String(r.error || lines[lines.length - 1] || (r.ok ? "replaced" : "not replaced"));
        setLLMOpsRail({ note: `llm ops: ${report}`, ok: r.ok === true });
        if (r.ok) {
          contextFetched.delete(sid);
          refresh();
          loadMessages();
        }
        resolve({ ok: r.ok === true, report, error: r.ok ? "" : report, lines });
      }).catch(() => setTimeout(tick, LLMOPS_POLL_MS));
    };
    setTimeout(tick, LLMOPS_POLL_MS);
  });
}
var LLMOPS_CHAT_TIMEOUT_MS = 30 * 60 * 1e3;
function llmopsTransport(sid, addrs) {
  const s = encodeURIComponent(String(sid || ""));
  const get = (path, what) => adapter.rest(path).then((res) => {
    const r = asRecord(res);
    if (r.ok === false || r.error) throw new Error(String(r.error || what));
    return r;
  });
  return {
    data: () => get(`/llmops-data?session=${s}&addrs=${encodeURIComponent((addrs || []).join(","))}`, "no data"),
    models: () => get(`/llmops-models?session=${s}`, "no model list"),
    presets: () => get(`/llmops-presets?session=${s}`, "no presets"),
    // A big session's prompt takes minutes — their default 30 s cut it (owner, 2026-10-14).
    chat: ({ messages, model, think }) => adapter.rest("/llmops-chat", { method: "POST", body: { session: sid, messages, model, think: !!think }, timeoutMs: LLMOPS_CHAT_TIMEOUT_MS }).then(asRecord),
    stop: () => adapter.rest("/llmops-stop", { method: "POST", body: { session: sid } }).then(asRecord),
    // Что модель уже напечатала, пока `chat` ждёт: новое после `textAt`/`reasoningAt` символов.
    progress: (textAt, reasoningAt) => adapter.rest(`/llmops-progress?session=${s}&text=${Number(textAt) || 0}&reasoning=${Number(reasoningAt) || 0}`).then(asRecord),
    replace: ({ addrs: eat, text, wrap, preset, origin }) => {
      setLLMOpsRail({ note: "llm ops: replacing\u2026", ok: null });
      return adapter.rest("/llmops-replace", { method: "POST", body: Object.assign(
        { session: sid, addrs: eat, text, wrap, preset: preset || "" },
        origin ? { origin } : {}
      ) }).then(asRecord).then((r) => {
        if (r.ok !== true) {
          const why = String(r.error || "not started");
          setLLMOpsRail({ note: `llm ops: ${why}`, ok: false });
          return { ok: false, error: why };
        }
        return waitLLMOpsRun(sid);
      });
    }
  };
}
function copyExportDir() {
  const dir = String(($exportRail.get() || EXPORT_INITIAL).dir || "");
  const clip = typeof navigator !== "undefined" && navigator && navigator.clipboard;
  if (!dir || !clip || typeof clip.writeText !== "function") return Promise.resolve(false);
  return Promise.resolve(clip.writeText(dir)).then(() => true).catch(() => false);
}
var IMPORT_INITIAL = {
  open: false,
  folder: "",
  checking: false,
  check: null,
  busy: false,
  note: "",
  ok: null
};
var $importOp = makeAtom(IMPORT_INITIAL);
function useImportOp() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($importOp) || IMPORT_INITIAL;
}
function setImportOp(patch) {
  $importOp.set(Object.assign({}, $importOp.get() || IMPORT_INITIAL, patch));
}
function clipboardPath(text) {
  const t = String(text || "").trim().replace(/^["']|["']$/g, "");
  if (!t || t.length > 400 || /[\r\n]/.test(t)) return "";
  return /[\\/]/.test(t) ? t : "";
}
function openImport() {
  const cur = $importOp.get() || IMPORT_INITIAL;
  const fallback = cur.folder || String(($exportRail.get() || EXPORT_INITIAL).dir || "");
  setImportOp({ open: true, folder: fallback, check: null, checking: false, busy: false });
  const usePath = (text) => {
    const path = clipboardPath(text);
    const now = $importOp.get() || IMPORT_INITIAL;
    if (!path || !now.open) return void 0;
    setImportOp({ folder: path, check: null });
    return checkImport();
  };
  return readClipboard().then((text) => {
    if (clipboardPath(text)) return usePath(text);
    stopImportPaste();
    importPasteOff = catchNextPaste((pasted) => {
      importPasteOff = null;
      usePath(pasted);
    });
    return void 0;
  });
}
var importPasteOff = null;
function stopImportPaste() {
  if (importPasteOff) {
    importPasteOff();
    importPasteOff = null;
  }
}
function closeImport() {
  stopImportPaste();
  setImportOp({ open: false, checking: false, busy: false });
}
function setImportFolder(folder) {
  setImportOp({ folder: String(folder || ""), check: null });
}
function checkImport() {
  const cur = $importOp.get() || IMPORT_INITIAL;
  const folder = String(cur.folder || "").trim();
  if (!folder || cur.checking || !adapter.hasRest) return Promise.resolve();
  setImportOp({ checking: true, check: null });
  return adapter.rest("/context-import", { method: "POST", body: { session: viewKey() || "", folder } }).then((res) => {
    const r = asRecord(res) || {};
    setImportOp({ checking: false, check: Object.assign({ folder }, r) });
  }).catch((err) => setImportOp({
    checking: false,
    check: { folder, ok: false, error: `request failed: ${err && err.message || err}` }
  }));
}
var IMPORT_MODES = ["replace", "tail", "head"];
function applyImport(mode = "replace") {
  const cur = $importOp.get() || IMPORT_INITIAL;
  const sid = viewKey();
  const check = cur.check || {};
  if (cur.busy || check.ok !== true || check.folder !== cur.folder) return Promise.resolve();
  if (!IMPORT_MODES.includes(mode)) return Promise.resolve();
  if (!adapter.hasRest || !isPersistableKey(sid)) {
    setImportOp({ note: "this chat is not saved yet", ok: false });
    return Promise.resolve();
  }
  setImportOp({ busy: true });
  return adapter.rest("/context-import", { method: "POST", body: { session: sid, folder: cur.folder, apply: true, mode } }).then((res) => {
    const r = asRecord(res) || {};
    const ok = r.ok === true;
    const note = ok ? `imported ${r.rows_after || 0} messages (was ${r.rows_before || 0}, ${mode}) \u2190 ${cur.folder}` : String(r.summary && r.summary !== "not applied" ? r.summary : r.error || r.summary || "not imported");
    if (ok) stopImportPaste();
    setImportOp({ busy: false, ok, note, open: !ok });
    if (ok) {
      loadMessages();
      refresh();
    }
  }).catch((err) => setImportOp({ busy: false, ok: false, note: `request failed: ${err && err.message || err}` }));
}
var ADDR_OP_INITIAL = { mode: false, kind: "content", picked: {}, busy: false, note: "", ok: null };
var $addrOp = makeAtom(ADDR_OP_INITIAL);
function useAddrOp() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($addrOp) || ADDR_OP_INITIAL;
}
function setAddrOp(patch) {
  $addrOp.set(Object.assign({}, $addrOp.get() || ADDR_OP_INITIAL, patch));
}
function setAddrOpMode(on, kind) {
  if (!on) {
    const prev = $addrOp.get() || ADDR_OP_INITIAL;
    setAddrOp(Object.assign({}, ADDR_OP_INITIAL, { note: prev.note, ok: prev.ok }));
    return;
  }
  setAddrOp({
    mode: true,
    kind: kind === "erase" ? "erase" : "content",
    picked: {},
    busy: false,
    note: "",
    ok: null
  });
  setCompactMode(false);
  setExportMode(false);
  setLLMOpsMode(false);
}
function setAddrOpPicks(picked) {
  const cur = $addrOp.get() || ADDR_OP_INITIAL;
  if (!cur.mode) return;
  setAddrOp({ picked: Object.assign({}, picked || {}) });
}
function applyAddrOp() {
  const cur = $addrOp.get() || ADDR_OP_INITIAL;
  if (!cur.mode || cur.busy) return Promise.resolve();
  const msgs = Object.keys(cur.picked || {});
  const sid = viewKey();
  if (!msgs.length) {
    setAddrOp({ note: "nothing ticked", ok: false });
    return Promise.resolve();
  }
  if (!adapter.hasRest || !isPersistableKey(sid)) {
    setAddrOp({ note: "this chat is not saved yet \u2014 nothing to apply to", ok: false });
    return Promise.resolve();
  }
  setAddrOp({ busy: true, note: "", ok: null });
  return adapter.rest("/context-operation", { method: "POST", body: { session: sid, operation: cur.kind, msgs } }).then((res) => {
    const r = asRecord(res);
    const ok = r.ok === true;
    if (!ok) {
      setAddrOp({ busy: false, ok: false, note: String(r.summary || r.error || "not applied") });
      return;
    }
    setAddrOp(Object.assign({}, ADDR_OP_INITIAL, { ok: true, note: String(r.summary || "applied") }));
    contextFetched.delete(sid);
    refresh();
  }).catch((err) => setAddrOp({ busy: false, ok: false, note: `request failed: ${err && err.message || err}` }));
}
var ZONES_KEY = "mirror-zones";
var ZONES_INITIAL = { operations: true, status: true, files: true };
var $zones = makeAtom(ZONES_INITIAL);
function useZones() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($zones) || ZONES_INITIAL;
}
var zonesLoaded = false;
function loadZones() {
  if (zonesLoaded) return Promise.resolve();
  zonesLoaded = true;
  return adapter.storageGet(ZONES_KEY).then((saved) => {
    if (saved && typeof saved === "object") $zones.set(Object.assign({}, ZONES_INITIAL, saved));
  }).catch(() => {
  });
}
function toggleZone(id) {
  const cur = $zones.get() || ZONES_INITIAL;
  const next = Object.assign({}, cur, { [id]: !cur[id] });
  $zones.set(next);
  adapter.storageSet(ZONES_KEY, next);
}
function loadMessages() {
  const sid = viewKey();
  if (!isPersistableKey(sid)) {
    $messages.set(Object.assign({}, MESSAGES_INITIAL, { note: "no stored session yet" }));
    return Promise.resolve();
  }
  if (!adapter.hasRest) {
    $messages.set(Object.assign({}, MESSAGES_INITIAL, { sessionId: sid, note: "this build has no REST door" }));
    return Promise.resolve();
  }
  forgetFullText(sid);
  $messages.set(Object.assign(
    {},
    $messages.get() || MESSAGES_INITIAL,
    { sessionId: sid, loading: true, note: "" }
  ));
  return adapter.rest(`/messages?session=${encodeURIComponent(sid)}&preview=${MESSAGES_PREVIEW}`).then((res) => {
    if (viewKey() !== sid) return;
    const r = asRecord(res);
    $messages.set({
      sessionId: sid,
      carriages: Array.isArray(r.carriages) ? r.carriages : [],
      messages: Array.isArray(r.messages) ? r.messages : [],
      note: r.error ? String(r.error) : "",
      loading: false
    });
    loadTokens({ exact: "auto" });
  }).catch((err) => {
    if (viewKey() !== sid) return;
    $messages.set(Object.assign({}, MESSAGES_INITIAL, {
      sessionId: sid,
      note: `/messages: ${err && err.message || err}`
    }));
  });
}
var MIRROR_RELOAD_MS = 1500;
var mirrorTimer = null;
function scheduleMirrorReload() {
  if (mirrorTimer) clearTimeout(mirrorTimer);
  mirrorTimer = setTimeout(() => {
    mirrorTimer = null;
    try {
      const mode = ($view.get() || VIEW_INITIAL).mode || "mirror";
      if (mode === "mirror") reloadMessagesQuietly();
    } catch (err) {
      console.error("[hermes-filetools] mirror reload:", err);
    }
  }, MIRROR_RELOAD_MS);
  if (mirrorTimer && typeof mirrorTimer.unref === "function") mirrorTimer.unref();
}
function stopMirrorReload() {
  if (mirrorTimer) clearTimeout(mirrorTimer);
  mirrorTimer = null;
}
function reloadMessagesQuietly() {
  const sid = viewKey();
  if (!isPersistableKey(sid) || !adapter.hasRest) return Promise.resolve();
  return adapter.rest(`/messages?session=${encodeURIComponent(sid)}&preview=${MESSAGES_PREVIEW}`).then((res) => {
    if (viewKey() !== sid) return;
    const r = asRecord(res) || {};
    if (r.error) return;
    const cur = $messages.get() || MESSAGES_INITIAL;
    loadTokens({ exact: false });
    if (cur.sessionId === sid && sameMessages(cur, r)) return;
    forgetFullText(sid);
    $messages.set({
      sessionId: sid,
      carriages: Array.isArray(r.carriages) ? r.carriages : [],
      messages: Array.isArray(r.messages) ? r.messages : [],
      note: "",
      loading: false
    });
  }).catch(() => {
  });
}
function publishFileContext(sid) {
  if (sid !== viewKey()) return;
  const cur = $fileContext.get() || {};
  const next = contextOps[sid] || NO_OPS;
  if (cur.ops === next) return;
  $fileContext.set(Object.assign({}, cur, { ops: next }));
}
function scheduleContextRefetch(sid) {
  if (!isPersistableKey(sid)) return;
  if (contextTimer) clearTimeout(contextTimer);
  contextTimer = setTimeout(() => {
    contextTimer = null;
    contextFetched.delete(sid);
    ensureContextState(sid);
  }, CONTEXT_REFETCH_MS);
}
function handleStatusUpdate(event) {
  try {
    if (!shouldRefetchOnStatus(event)) return;
    const sid = viewKey();
    if (!isPersistableKey(sid)) return;
    contextFetched.delete(sid);
    refresh();
    scheduleMirrorReload();
  } catch (err) {
    console.error("[hermes-filetools] status.update handler:", err);
  }
}
var socketSid = null;
var socketOff = null;
function handleSocketFrame(data) {
  const frame = asRecord(data) || {};
  if (frame.kind === "hello") {
    console.info("[hermes-filetools] /events: \u043A\u0430\u043D\u0430\u043B \u043E\u0442\u043A\u0440\u044B\u0442", frame.session || "");
    return;
  }
  if (frame.kind === "context-changed") {
    try {
      const sid = viewKey();
      if (!isPersistableKey(sid)) return;
      if (frame.session && frame.session !== sid) return;
      contextFetched.delete(sid);
      refresh();
      scheduleMirrorReload();
    } catch (err) {
      console.error("[hermes-filetools] context-changed handler:", err);
    }
    return;
  }
}
var focusSid = null;
function ensureFocus(sid) {
  if (!adapter.hasRest || focusSid === sid) return;
  focusSid = sid;
  if (!isPersistableKey(sid)) return;
  adapter.rest("/context-focus", { method: "POST", body: { session: sid } }).catch(() => {
  });
}
function ensureEventSocket(sid) {
  if (!adapter.hasSocket || socketSid === sid) return;
  closeEventSocket();
  socketSid = sid;
  if (!isPersistableKey(sid)) return;
  socketOff = adapter.socket(`/events?session=${encodeURIComponent(sid)}`, handleSocketFrame);
}
var REVISION_POLL_MS = 2e3;
var revisionTimer = null;
var revisionSeen = {};
function pollRevision() {
  const sid = viewKey();
  if (!isPersistableKey(sid) || !adapter.hasRest) return;
  pollCompactRun(sid);
  adapter.rest(`/context-revision?session=${encodeURIComponent(sid)}`).then((res) => {
    const rev = res && Number.isFinite(Number(res.revision)) ? Number(res.revision) : 0;
    if (!(sid in revisionSeen)) {
      revisionSeen[sid] = rev;
      return;
    }
    if (rev === revisionSeen[sid]) return;
    revisionSeen[sid] = rev;
    contextFetched.delete(sid);
    refresh();
    loadMessages();
  }).catch(() => {
  });
}
function startRevisionPoll() {
  if (revisionTimer || !adapter.hasRest) return;
  revisionTimer = setInterval(pollRevision, REVISION_POLL_MS);
  if (revisionTimer && typeof revisionTimer.unref === "function") revisionTimer.unref();
}
function stopRevisionPoll() {
  if (revisionTimer) {
    clearInterval(revisionTimer);
    revisionTimer = null;
  }
}
function closeEventSocket() {
  if (socketOff) {
    try {
      socketOff();
    } catch {
    }
    socketOff = null;
  }
  socketSid = null;
}
function refresh() {
  const sid = viewKey();
  ensureContextState(sid);
  ensureEventSocket(sid);
  ensureFocus(sid);
  startRevisionPoll();
  publishFileContext(sid);
  const cur = $view.get() || {};
  if (cur.sessionId !== sid) $view.set(Object.assign({}, cur, { sessionId: sid }));
}
function setCompact(patch) {
  $compact.set(Object.assign({}, $compact.get() || COMPACT_INITIAL, patch));
}
function armCompactFromServer(share) {
  const sid = viewKey();
  if (!isPersistableKey(sid) || !adapter.hasRest) {
    setCompactMode(true, {});
    setCompact({ note: "this chat is not saved yet \u2014 nothing to pick from", ok: false });
    return Promise.resolve();
  }
  setCompact({ busy: true, note: "picking\u2026" });
  return adapter.rest("/context-compact-pick", { method: "POST", body: { session: sid, share } }).then((res) => {
    const r = asRecord(res) || {};
    const picked = {};
    (Array.isArray(r.picked) ? r.picked : []).forEach((a) => {
      if (a) picked[String(a)] = true;
    });
    setCompactMode(true, picked);
    const why = Array.isArray(r.lines) ? r.lines.join(" \xB7 ") : "";
    if (r.error || !r.ok) setCompact({ note: String(r.error || why || "nothing to pick"), ok: false });
  }).catch((err) => {
    setCompactMode(true, {});
    setCompact({ note: `/context-compact-pick: ${err && err.message || err}`, ok: false });
  });
}
function setCompactMode(on, initialPicked) {
  if (!on) {
    const prev = $compact.get() || COMPACT_INITIAL;
    setCompact(Object.assign({}, COMPACT_INITIAL, { note: prev.note, ok: prev.ok }));
    return;
  }
  setCompact({
    open: true,
    mode: true,
    picked: Object.assign({}, initialPicked || {}),
    dragging: false,
    dragMode: null,
    confirming: false,
    busy: false,
    note: "",
    ok: null
  });
  setExportMode(false);
  setAddrOpMode(false);
  setLLMOpsMode(false);
}
function applyCompactPick(addr, wantPicked) {
  if (!addr) return;
  const cur = $compact.get() || COMPACT_INITIAL;
  if (!cur.mode) return;
  const already = addr in (cur.picked || {});
  if (already === wantPicked) return;
  const picked = Object.assign({}, cur.picked);
  if (wantPicked) picked[addr] = true;
  else delete picked[addr];
  setCompact({ picked });
}
function setCompactPicks(picked) {
  const cur = $compact.get() || COMPACT_INITIAL;
  if (!cur.mode) return;
  setCompact({ picked: Object.assign({}, picked || {}) });
}
function beginCompactDrag(addr) {
  const cur = $compact.get() || COMPACT_INITIAL;
  if (!cur.mode || !addr) return;
  const dragMode = addr in (cur.picked || {}) ? "remove" : "add";
  setCompact({ dragging: true, dragMode });
  applyCompactPick(addr, dragMode === "add");
}
function dragCompactOver(addr) {
  const cur = $compact.get() || COMPACT_INITIAL;
  if (!cur.dragging || !cur.dragMode) return;
  applyCompactPick(addr, cur.dragMode === "add");
}
function endCompactDrag() {
  const cur = $compact.get() || COMPACT_INITIAL;
  if (cur.dragging) setCompact({ dragging: false, dragMode: null });
}
function compactMouseUp() {
  endCompactDrag();
}
function openCompactConfirm() {
  const cur = $compact.get() || COMPACT_INITIAL;
  if (!Object.keys(cur.picked || {}).length) {
    setCompact({ note: "nothing to compact \u2014 no message is ticked", ok: false });
    return;
  }
  setCompact({ confirming: true });
  loadCompactSets().then((r) => {
    if (!r || r.ok !== true || !Array.isArray(r.sets)) return;
    const off = new Set(Array.isArray(r.off) ? r.off : []);
    const menu = Object.fromEntries(r.sets.map((s) => [s.name, s.fallback || !off.has(s.name)]));
    setCompact({ sets: r.sets, menu, menuTouched: false });
  });
}
function loadCompactSets(sid) {
  const key = sid || viewKey();
  if (!adapter.hasRest || !isPersistableKey(key)) {
    return Promise.resolve({ ok: false, error: "open a saved chat \u2014 the sets come from its live engine" });
  }
  return adapter.rest(`/compact-sets?session=${encodeURIComponent(key)}`).then((r) => r && typeof r === "object" ? r : { ok: false, error: "not an answer" }).catch((err) => ({ ok: false, error: err && err.message || String(err) }));
}
function compactSetLabel(s) {
  return String(s && (s.word || s.name) || "");
}
function compactSetTitle(s) {
  const asks = Array.isArray(s && s.asks) ? s.asks : [];
  return [String(s && s.pick || "")].concat(asks.length ? ["", "asks:"].concat(asks.map((a) => `  \u2022 ${a}`)) : []).concat(s && s.fallback ? ["", "fallback \u2014 always shown"] : []).join("\n");
}
function toggleCompactMenu(name) {
  const cur = $compact.get() || COMPACT_INITIAL;
  const set = (cur.sets || []).find((s) => s.name === name);
  if (!set || set.fallback) return;
  setCompact({ menu: Object.assign({}, cur.menu, { [name]: !(cur.menu || {})[name] }), menuTouched: true });
}
function setCompactWish(text) {
  setCompact({ wish: String(text || "") });
}
function toggleCompactPreview() {
  const cur = $compact.get() || COMPACT_INITIAL;
  setCompact({ preview: !cur.preview });
}
function draftOfStatus(r) {
  const d = r && typeof r.draft === "object" && r.draft ? r.draft : null;
  if (!d || !String(d.text || "").trim() || !Array.isArray(d.addrs) || !d.addrs.length) return null;
  return {
    addrs: d.addrs.map(String),
    win: {
      text: String(d.text),
      wrap: String(d.wrap || "compaction"),
      // `sets` — классификация опроса `[{topic, set}]` как есть: строками журнал их выбросит.
      origin: {
        by: "compact",
        wish: String(d.wish || ""),
        sets: Array.isArray(d.sets) ? d.sets.filter((x) => x && typeof x === "object").map((x) => ({ topic: String(x.topic || ""), set: String(x.set || "") })) : []
      }
    }
  };
}
function cancelCompactConfirm() {
  setCompact({ confirming: false });
}
var COMPACT_POLL_MS = 4e3;
var compactPollTimer = null;
function stopCompactPoll() {
  if (compactPollTimer) {
    clearInterval(compactPollTimer);
    compactPollTimer = null;
  }
}
function pollCompactStatus(sid) {
  if (!adapter.hasRest) return;
  adapter.rest(`/context-compact-status?session=${encodeURIComponent(sid)}`).then((res) => {
    const r = res && typeof res === "object" ? res : {};
    if (r.running) return;
    stopCompactPoll();
    const draft = draftOfStatus(r);
    if (draft) {
      const opened = openLLMOpsWindow(sid, draft.addrs, draft.win);
      setCompact({
        busy: false,
        ok: null,
        note: opened ? "preview: the distillate is in the LLMOps window \u2014 Replace puts it into the tape" : "preview ready, but the LLMOps window is already open \u2014 close it and run the preview again"
      });
      return;
    }
    const lines = Array.isArray(r.lines) ? r.lines : [];
    setCompact({
      busy: false,
      ok: r.ok === true,
      note: r.error || lines[lines.length - 1] || (r.ok ? "compacted" : "not compacted")
    });
    if (r.ok) {
      contextFetched.delete(sid);
      refresh();
      loadMessages();
    }
  }).catch(() => {
  });
}
function startCompactPoll(sid) {
  stopCompactPoll();
  compactPollTimer = setInterval(() => pollCompactStatus(sid), COMPACT_POLL_MS);
  if (compactPollTimer && typeof compactPollTimer.unref === "function") compactPollTimer.unref();
}
function confirmCompact() {
  const cur = $compact.get() || COMPACT_INITIAL;
  const addrs = Object.keys(cur.picked || {});
  const sid = viewKey();
  if (!addrs.length) {
    setCompact({ confirming: false, note: "nothing to compact \u2014 no message is ticked", ok: false });
    return Promise.resolve();
  }
  if (!adapter.hasRest || !isPersistableKey(sid)) {
    setCompact({ confirming: false, note: "this chat is not saved yet \u2014 nothing to compact", ok: false });
    return Promise.resolve();
  }
  setCompact({ confirming: false, busy: true, note: "", ok: null });
  return adapter.rest("/context-compact-start", { method: "POST", body: Object.assign(
    { session: sid, addrs },
    String(cur.wish || "").trim() ? { wish: String(cur.wish).trim() } : {},
    // Наборы — только если человек их трогал: нетронутые = настройка, и фильтры работают.
    cur.menuTouched ? { menu: Object.keys(cur.menu || {}).filter((n2) => cur.menu[n2]) } : {},
    // Превью — только `true` и только с галочкой: без неё тело прежнее (`Plan35`).
    cur.preview ? { preview: true } : {}
  ) }).then((res) => {
    const r = res && typeof res === "object" ? res : {};
    if (r.ok !== true) {
      setCompact({ busy: false, ok: false, note: r.error || "not started" });
      return;
    }
    setCompact({ mode: false, open: false, note: "started \u2014 running in the background", ok: null });
    startCompactPoll(sid);
  }).catch((err) => {
    setCompact({ busy: false, ok: false, note: `request failed: ${err && err.message || err}` });
  });
}
function handleTurnEnd() {
  scheduleMirrorReload();
  loadTokens({ exact: "auto" });
}
var TOKENS_INITIAL = { sessionId: null, rep: null, busy: false, note: "" };
var TOKENS_RETRIES = 4;
var TOKENS_RETRY_MS = 1500;
var $tokens = makeAtom(TOKENS_INITIAL);
function useTokens() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($tokens) || TOKENS_INITIAL;
}
function loadTokens(opts) {
  const o = opts || {};
  const sid = viewKey();
  if (!isPersistableKey(sid) || !adapter.hasRest) return Promise.resolve();
  const cur = $tokens.get() || TOKENS_INITIAL;
  const loud = o.exact === true || o.load === true;
  if (loud) $tokens.set(Object.assign({}, cur, { busy: true, note: o.load ? "loading model\u2026" : "counting\u2026" }));
  return adapter.rest("/tokens", { method: "POST", body: { session: sid, exact: o.exact || false, load: !!o.load } }).then((res) => {
    if (viewKey() !== sid) return;
    const r = asRecord(res) || {};
    const prev = $tokens.get() || TOKENS_INITIAL;
    const tries = o.tries || 0;
    if (r.error && /not among the live/.test(String(r.error)) && tries < TOKENS_RETRIES) {
      setTimeout(() => {
        loadTokens(Object.assign({}, o, { tries: tries + 1 }));
      }, TOKENS_RETRY_MS);
    }
    $tokens.set({
      sessionId: sid,
      rep: r.error && prev.sessionId === sid && prev.rep ? prev.rep : r,
      busy: false,
      note: r.error ? String(r.error) : ""
    });
  }).catch((err) => {
    if (viewKey() !== sid) return;
    $tokens.set(Object.assign(
      {},
      $tokens.get() || TOKENS_INITIAL,
      { busy: false, note: `/tokens: ${err && err.message || err}` }
    ));
  });
}
var LIVE_DURATIONS_MAX = 200;
var $liveDurations = makeAtom({});
function useLiveDurations() {
  const hook = typeof sdk.useValue === "function" ? sdk.useValue : (a) => a && typeof a.get === "function" ? a.get() : void 0;
  return hook($liveDurations) || {};
}
function handleToolComplete(event) {
  try {
    scheduleMirrorReload();
    const p = asRecord(event && event.payload || {}) || {};
    const id = typeof p.tool_id === "string" ? p.tool_id : "";
    if (id && Number.isFinite(p.duration_s)) {
      const cur = $liveDurations.get() || {};
      const keys = Object.keys(cur);
      const next = {};
      keys.slice(Math.max(0, keys.length - LIVE_DURATIONS_MAX + 1)).forEach((k2) => {
        next[k2] = cur[k2];
      });
      next[id] = p.duration_s;
      $liveDurations.set(next);
    }
    scheduleContextRefetch(viewKey());
  } catch (err) {
    console.error("[hermes-filetools] tool.complete handler:", err);
  }
}

// desktop/src/file-ops-pane.js
import { jsx as jsx42, jsxs as jsxs37, Fragment } from "react/jsx-runtime";
import { useRef as useRef12, useLayoutEffect as useLayoutEffect7, useMemo as useMemo5, useEffect as useEffect10, useState as useState18, memo } from "react";

// desktop/src/brick.js
import { jsx, jsxs } from "react/jsx-runtime";
import * as sdk2 from "@hermes/plugin-sdk";
var SELECTABLE = { userSelect: "text", WebkitUserSelect: "text" };
var CAPPED = { width: "100%", maxWidth: "100%", minWidth: 0 };
var SCROLLER = { overflowX: "auto", overflowY: "hidden", maxWidth: "100%", minWidth: 0 };
var PRE = { width: "max-content", minWidth: "100%" };
var SELECTABLE_SMALL = { ...SELECTABLE, fontSize: "0.7rem" };
function brickCounts(brick) {
  const m = brick.meta || {};
  const bits = [];
  if (Number.isFinite(m.added) && m.added > 0) bits.push(`+${m.added}`);
  if (Number.isFinite(m.removed) && m.removed > 0) bits.push(`-${m.removed}`);
  if (Number.isFinite(m.replacements) && m.replacements > 0) bits.push(`\xD7${m.replacements}`);
  return bits.length ? bits.join(" ") : brick.subtitle || "";
}
function sdkC(Comp, fallbackTag, props) {
  if (typeof Comp !== "function" && typeof Comp !== "object") return jsx(fallbackTag, props);
  return jsx(Comp, props);
}
function BrickBody({ body }) {
  return jsxs("div", {
    className: "rounded-md border border-(--ui-stroke-secondary) p-2",
    style: CAPPED,
    children: [
      // Горизонтальный скролл — на отдельном контейнере, НЕ на самом <pre>: по CSS-спеке у элемента
      // с overflow-x:auto и высоким контентом overflow-y вычисляется в auto — внутри тела появился бы
      // ещё и вертикальный скроллбар. Вертикаль — за панелью (ScrollArea), горизонталь — здесь.
      jsx("div", {
        className: "",
        style: SCROLLER,
        children: jsx("pre", {
          // Выделение — ИНЛАЙН-стилем (SELECTABLE_SMALL): оболочка наследует user-select:none, а
          // класса `select-text` в их собранном CSS нет вовсе. whitespace-pre = без переносов.
          className: "m-0 whitespace-pre leading-snug font-mono text-(--ui-text-primary)",
          style: Object.assign({}, SELECTABLE_SMALL, PRE),
          children: body || "(empty answer)"
        })
      }),
      typeof sdk2.CopyButton === "function" && body !== "" ? jsx("span", {
        className: (
          // ВСЕГДА видима, приглушённо — а не по наведению. Показ по hover требует
          // `group-hover:*`, которых в собранном CSS приложения нет: базовое `opacity-0`
          // применялось, а раскрытие нет, и кнопка была постоянно невидимой И некликабельной.
          // `:hover` инлайн-стилем не выражается, поэтому выбор между «всегда видно» и
          // «не видно никогда» — и первое честнее.
          "ml-auto shrink-0 flex justify-end pb-1"
        ),
        style: { opacity: 0.55 },
        children: jsx(sdk2.CopyButton, { appearance: "icon", buttonSize: "icon", stopPropagation: true, text: body })
      }) : null
    ]
  });
}
var KIND_LABEL_CLASS = {
  error: "text-(--ui-red) font-semibold",
  ok: "text-(--ui-green)",
  "no-change": "text-(--ui-text-tertiary)"
};
function Brick({ brick }) {
  const tone = KIND_TONE[brick.kind] || "muted";
  return jsxs("details", {
    className: "group rounded-md border border-(--ui-stroke-secondary) mb-1 overflow-hidden bg-(--ui-bg-elevated)",
    style: CAPPED,
    children: [
      // Заголовок тоже выделяется мышью — тем же инлайн-стилем, по той же причине:
      // Tailwind-класса в собранном CSS приложения не существует.
      jsxs("summary", {
        className: "flex items-center gap-2 cursor-pointer list-none px-2 py-1.5 [&::-webkit-details-marker]:hidden",
        style: SELECTABLE,
        children: [
          sdkC(sdk2.StatusDot, "span", { tone }),
          jsx("code", { className: "text-(--ui-text-quaternary) shrink-0", style: { fontSize: "0.65rem" }, children: brick.tool }),
          // min-w-0 — позволяет flex-элементу сжиматься ниже ширины контента: длинный текст
          // уезжает «под» правые элементы (ellipsize), а те всегда остаются на экране.
          jsx("span", {
            className: "min-w-0 truncate text-xs font-medium text-(--ui-text-primary)",
            title: brick.title,
            children: brick.title || "(no path)"
          }),
          brickCounts(brick) ? jsx("span", { className: "min-w-0 truncate text-(--ui-text-secondary)", style: { fontSize: "0.65rem" }, children: brickCounts(brick) }) : null,
          brick.meta && brick.meta.duration_s != null ? jsx("span", { className: "shrink-0 tabular-nums text-(--ui-text-quaternary)", style: { fontSize: "0.625rem" }, children: formatDuration(brick.meta.duration_s) }) : null,
          // Состояние прочитанного — вторая метка НА карточке. Появляется только если чтение
          // сопоставлено со строкой в базе; причину даёт маршрут, своих формулировок не пишем.
          brick.read ? jsxs("span", {
            className: "ml-auto shrink-0 inline-flex items-center gap-1",
            style: { fontSize: "0.65rem" },
            title: brick.read.reason || "",
            children: [
              sdkC(sdk2.StatusDot, "span", { tone: READ_STATE_TONE[brick.read.state] || "muted" }),
              jsx("span", { className: "text-(--ui-text-quaternary)", children: READ_STATE_LABEL[brick.read.state] || brick.read.state })
            ]
          }) : null,
          jsx("span", {
            className: (brick.read ? "shrink-0 " : "ml-auto shrink-0 ") + (KIND_LABEL_CLASS[brick.kind] || ""),
            style: { fontSize: "0.65rem" },
            children: brick.kind
          })
        ]
      }),
      // min-w-0/max-w-full — блок не расширяет карточку шире панели: длинная строка тела
      // скроллится внутри <pre>, а не уезжает за правый край (срез).
      jsx("div", { className: "px-2 py-1.5 border-t border-(--ui-stroke-secondary)", style: CAPPED, children: jsx(BrickBody, { body: brick.body }) })
    ]
  });
}

// desktop/src/operation-card.js
import { jsx as jsx5, jsxs as jsxs5 } from "react/jsx-runtime";
import { useState as useState4 } from "react";

// desktop/src/operation-card-header.js
import { jsx as jsx3, jsxs as jsxs3 } from "react/jsx-runtime";
import { useState as useState2, useRef as useRef2, useLayoutEffect } from "react";

// desktop/src/operation-card-primitives.js
import { jsx as jsx2, jsxs as jsxs2 } from "react/jsx-runtime";
import { useState, useRef, useEffect } from "react";
var TONE_COLOR = {
  good: "var(--ui-green)",
  warn: "var(--ui-yellow)",
  bad: "var(--ui-red)",
  muted: "var(--ui-text-tertiary)"
};
var ROW_HEIGHT = "34px";
var _hoverTipSet = null;
function GlobalHoverTip() {
  const [tip, setTip] = useState(null);
  useEffect(() => {
    _hoverTipSet = setTip;
    return () => {
      _hoverTipSet = null;
    };
  }, []);
  if (!tip) return null;
  return jsx2("div", {
    className: "px-2 py-1 rounded-md text-[0.6rem] whitespace-nowrap",
    style: {
      position: "fixed",
      left: `${tip.x}px`,
      top: `${tip.y}px`,
      transform: "translate(-50%, calc(-100% - 4px))",
      zIndex: 999,
      backgroundColor: "var(--ui-bg-elevated)",
      border: "1px solid var(--ui-stroke-secondary)",
      color: "var(--ui-text-primary)"
    },
    children: tip.hint
  });
}
function HoverTip({ hint, children }) {
  const showingRef = useRef(false);
  useEffect(() => () => {
    if (showingRef.current && _hoverTipSet) _hoverTipSet(null);
  }, []);
  return jsx2("span", {
    style: { display: "inline-flex" },
    onMouseEnter: (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      showingRef.current = true;
      if (_hoverTipSet) _hoverTipSet({ x: r.left + r.width / 2, y: r.top, hint });
    },
    onMouseLeave: () => {
      showingRef.current = false;
      if (_hoverTipSet) _hoverTipSet(null);
    },
    children
  });
}
function StatusMark({ tone, hint, glyph, offset }) {
  return jsx2(HoverTip, {
    hint,
    children: glyph ? jsx2("span", { style: { fontSize: "0.6rem", fontWeight: 700, lineHeight: 1, cursor: "help", color: TONE_COLOR[tone], transform: offset }, children: glyph }) : jsx2("span", { style: { width: "6px", height: "6px", borderRadius: "50%", backgroundColor: TONE_COLOR[tone], cursor: "help", flexShrink: 0, transform: offset } })
  });
}
function StatusColumn({ statuses }) {
  return jsx2("div", {
    style: { width: "8px", display: "flex", flexDirection: "column", alignSelf: "stretch", flexShrink: 0 },
    children: statuses.slice(0, 2).map((s, i) => jsx2("div", {
      key: i,
      style: { flex: 1, display: "flex", alignItems: "center", justifyContent: "center" },
      children: jsx2(StatusMark, Object.assign({}, s, { offset: i === 0 ? "translateY(-2px)" : "translateY(2px)" }))
    }))
  });
}
function CloseButton({ onClick }) {
  return jsx2("button", {
    onClick,
    title: "collapse",
    className: "cursor-pointer leading-none",
    style: { position: "absolute", top: "2px", right: "3px", zIndex: 11, color: "var(--ui-text-quaternary)", fontSize: "0.7rem", background: "transparent", border: "none", padding: "2px" },
    children: "\xD7"
  });
}
function StaleStack({ items }) {
  return jsx2("div", {
    style: { display: "flex", flexDirection: "column", height: ROW_HEIGHT, boxSizing: "border-box" },
    children: items.map((it, i) => jsx2("div", {
      key: i,
      style: { flex: 1, display: "flex", alignItems: "center", minHeight: 0 },
      children: jsx2(HoverTip, {
        hint: "covered by later operations on this file (buildContextView().stale)",
        children: jsxs2("span", { className: "flex items-center gap-1", children: [
          jsx2("span", {
            className: "flex items-center justify-center",
            style: { width: "6px", height: "6px", flexShrink: 0 },
            children: it.glyph ? jsx2("span", { style: { fontSize: "0.5rem", lineHeight: 1, color: "var(--ui-orange)" }, children: it.glyph }) : jsx2("span", { style: { width: "6px", height: "6px", borderRadius: "50%", backgroundColor: "var(--ui-orange)" } })
          }),
          jsx2("span", { style: { color: "var(--ui-orange)", fontStyle: "italic", fontSize: "0.65rem", lineHeight: 1.1 }, children: it.label })
        ] })
      })
    }))
  });
}
function StaleBadge({ items }) {
  const first = (items || [])[0];
  if (!first) return null;
  const pct = typeof first.pct === "number" ? `${first.pct}%` : "";
  return jsx2(HoverTip, {
    hint: `${first.label} \u2014 covered by later operations on this file`,
    children: jsxs2("span", { className: "flex items-center gap-1", style: { flexShrink: 0 }, children: [
      jsx2("span", {
        style: { width: "6px", height: "6px", borderRadius: "50%", backgroundColor: "var(--ui-orange)", flexShrink: 0 }
      }),
      pct ? jsx2("span", { style: { color: "var(--ui-orange)", fontSize: "0.55rem", fontStyle: "italic" }, children: pct }) : null
    ] })
  });
}

// desktop/src/operation-card-header.js
var DRAWER_BG = "color-mix(in srgb, var(--dt-secondary) 6%, var(--ui-bg-elevated))";
var DRAWER_EDGE_HINT = `linear-gradient(to right, transparent, ${DRAWER_BG} 16px)`;
var TOOL_FONT = "0.68rem";
var DIFF_FONT = "0.6rem";
var TOOL_ICON_SLOT = 14;
var TOOL_GAP = 4;
var DIFF_CH = 4;
var TOOL_MIN_CH = "patch".length;
var TOOL_NOISE_WORDS = ["file", "files"];
function toolWords(name) {
  const all = String(name || "").split(/[_-]+/).filter(Boolean);
  const kept = all.filter((w) => !TOOL_NOISE_WORDS.includes(w.toLowerCase()));
  return kept.length ? kept : all;
}
function toolColWidth(toolNames) {
  let longest = TOOL_MIN_CH;
  for (const t of toolNames || []) {
    for (const w of toolWords(t)) longest = Math.max(longest, w.length);
  }
  return `calc(${longest}ch + ${DIFF_CH}ch + ${TOOL_ICON_SLOT + TOOL_GAP * 2}px)`;
}
function ToolTag({ icon, name }) {
  return jsxs3("div", { className: "flex items-center gap-1 min-w-0", style: { flex: 1 }, children: [
    jsx3("span", { className: "shrink-0 text-center", style: { width: `${TOOL_ICON_SLOT}px`, fontSize: "0.7rem", color: "var(--ui-text-tertiary)" }, children: icon }),
    // items-start — слова прижаты к ЛЕВОМУ краю блока, сразу за значком. Правый край пробовался
    // (слова у вертикали дифа) и отвергнут вживую: между значком и именем возникала дырка на всю
    // ширину колонки. После вычистки шумовых слов имена стали 4-6 знаков при колонке в 6, так что
    // слака справа почти нет — а лесенка многословных имён (execute/code) читается от левого края.
    jsx3("div", {
      className: "flex flex-col items-start min-w-0",
      style: { flex: 1, lineHeight: 1.05 },
      children: toolWords(name).map((w, i) => jsx3("code", {
        key: i,
        className: "truncate min-w-0 block",
        style: Object.assign({ color: "var(--ui-text-quaternary)" }, SELECTABLE),
        children: w
      }))
    })
  ] });
}
function Diff({ added, removed }) {
  if (!added && !removed) return null;
  return jsxs3("div", {
    // items-end — цифры прижаты к ПРАВОМУ краю колонки, то есть к той самой вертикали, от которой
    // начинаются имена файлов. Иначе +1 и −12 разной длины давали рваный правый край у самой
    // границы с файловым блоком.
    className: "flex flex-col items-end shrink-0 leading-tight",
    style: Object.assign({ width: `${DIFF_CH}ch`, fontSize: DIFF_FONT }, SELECTABLE),
    children: [
      added ? jsx3("span", { style: { color: "var(--ui-green)" }, children: `+${added}` }) : null,
      removed ? jsx3("span", { style: { color: "var(--ui-red)" }, children: `-${removed}` }) : null
    ]
  });
}
function ToolColumn({ icon, tool, added, removed, width }) {
  return jsxs3("div", {
    className: "flex items-center shrink-0 min-w-0",
    // fontSize здесь, а не только на самом имени: `ch` в width считается от шрифта ЭТОГО
    // элемента, и без него единица оказалась бы от базового размера, то есть не той.
    style: { width: width || toolColWidth([tool]), fontSize: TOOL_FONT, gap: `${TOOL_GAP}px` },
    children: [
      jsx3(ToolTag, { icon, name: tool }),
      jsx3(Diff, { added, removed })
    ]
  });
}
var FADED_PRIMARY = "color-mix(in srgb, var(--ui-text-primary) 40%, transparent)";
var FADED_QUATERNARY = "color-mix(in srgb, var(--ui-text-quaternary) 50%, transparent)";
function FileTarget({ fileName, filePath, strike, faded = false }) {
  return jsx3("div", {
    style: { display: "flex", flexDirection: "column", minWidth: "64px", flexShrink: 1, maxWidth: "46%", gap: "1px" },
    children: [
      jsx3("span", { className: "truncate font-medium", style: Object.assign({ fontSize: "0.82rem", color: faded ? FADED_PRIMARY : "var(--ui-text-primary)", textDecoration: strike ? "line-through" : "none" }, SELECTABLE), children: fileName }),
      filePath ? jsx3("span", { className: "truncate", style: Object.assign({ fontSize: "0.64rem", color: faded ? FADED_QUATERNARY : "var(--ui-text-quaternary)", marginTop: "-4px" }, SELECTABLE), children: filePath }) : null
    ]
  });
}
function OpAddress({ text, onSelectOp, fontSize, color, weight }) {
  const [hover, setHover] = useState2(false);
  const clickable = !!onSelectOp;
  return jsx3("span", {
    onClick: clickable ? (e) => {
      e.stopPropagation();
      onSelectOp();
    } : void 0,
    onMouseEnter: clickable ? () => setHover(true) : void 0,
    onMouseLeave: clickable ? () => setHover(false) : void 0,
    title: clickable ? "show the context as of this operation" : void 0,
    style: Object.assign({
      fontSize,
      color: clickable && hover ? "var(--ui-text-primary)" : color,
      whiteSpace: "nowrap",
      cursor: clickable ? "pointer" : void 0
    }, weight ? { fontWeight: weight } : null, SELECTABLE),
    children: text
  });
}
function CollapsedMeta({ opNumber, tag, onSelectOp }) {
  const items = [opNumber || null, tag ? `tag: ${tag}` : null].filter(Boolean);
  if (!items.length) return null;
  return jsx3("div", {
    style: { display: "flex", flexDirection: "column", gap: "2px", lineHeight: 1.05 },
    children: items.map((t, i) => {
      const isOp = t === opNumber && onSelectOp;
      return jsx3("div", {
        key: i,
        children: jsx3(OpAddress, {
          text: t,
          onSelectOp: isOp ? onSelectOp : void 0,
          fontSize: "0.6rem",
          color: "var(--ui-text-quaternary)"
        })
      });
    })
  });
}
function PickSlot({ pick }) {
  if (!pick || !pick.enabled) return null;
  if (!pick.selectable) {
    return jsx3("span", {
      title: pick.why || "nothing to select here",
      style: { color: "var(--ui-text-quaternary)" },
      children: "\u2014"
    });
  }
  return jsxs3("label", {
    className: "inline-flex items-center gap-1 cursor-pointer select-none",
    title: pick.why || void 0,
    // Клик по галочке НЕ должен всплывать: выше на карточке сидит раскрытие тела, а на
    // document — снятие режима затенения. Оба сработали бы заодно с постановкой галочки.
    onClick: (e) => {
      e.stopPropagation();
    },
    // Галочка слева, а справа от неё ДВЕ СТРОКИ — имя действия над ценой (владелец,
    // 2026-09-09):
    //
    //         remove
    //     []  120 tk
    //
    // Компактнее строки в один ряд, и — главное — цена стоит ПОД своим действием. Когда рядом
    // встанет `delete` со своей цифрой, разные числа будут объяснены тем, над чем они написаны:
    // у `remove` уходит содержимое, у `delete` — весь шаг, и весить это может по-разному.
    //
    // Цена НЕ так жирно, как имя действия: решение принимают по действию, цифра — основание, а
    // не заголовок.
    children: [
      jsx3("input", {
        type: "checkbox",
        checked: !!pick.checked,
        onChange: pick.onPick,
        style: { accentColor: "var(--ui-red)", width: "10px", height: "10px", flexShrink: 0 }
      }),
      jsxs3("span", {
        style: { display: "flex", flexDirection: "column", gap: "1px", lineHeight: 1.05 },
        children: [
          // Имя действия приходит извне (`pick.label`) — карточка не выбирает слов для
          // операции, которую сама не выполняет. Следующее действие подставит сюда своё.
          jsx3("span", {
            style: { color: "var(--ui-text-secondary)", fontWeight: 500 },
            children: pick.label || ""
          }),
          pick.tokensText ? jsx3("span", {
            title: pick.exact ? "operation weight in tokens: answer plus call arguments" : "approximate: answer only, the call arguments have not loaded yet",
            style: {
              color: "var(--ui-text-quaternary)",
              fontSize: "0.5rem",
              fontVariantNumeric: "tabular-nums"
            },
            children: `${pick.exact ? "" : "~"}${pick.tokensText}`
          }) : null
        ]
      })
    ]
  });
}
function Drawer({ drawerRef, open, onToggle, durationText, opNumber, tag, pick, staleItems, onSelectOp }) {
  if (!open) {
    return jsx3("button", {
      ref: drawerRef,
      onClick: onToggle,
      title: "expand context actions",
      className: "flex items-center justify-center gap-1 cursor-pointer",
      style: {
        position: "absolute",
        top: 0,
        bottom: 0,
        right: 0,
        zIndex: 10,
        background: DRAWER_EDGE_HINT,
        borderTopRightRadius: "6px",
        borderBottomRightRadius: "6px",
        padding: "0 8px 0 6px",
        fontSize: "0.55rem",
        whiteSpace: "nowrap",
        color: "var(--ui-text-quaternary)"
      },
      children: [
        jsx3(StaleBadge, { items: staleItems }),
        jsx3(CollapsedMeta, { opNumber, tag, onSelectOp }),
        jsx3("span", { children: durationText || "\u2014" })
      ]
    });
  }
  return jsxs3("div", {
    ref: drawerRef,
    className: "flex items-center gap-2",
    style: {
      position: "absolute",
      top: 0,
      bottom: 0,
      right: 0,
      zIndex: 10,
      background: DRAWER_EDGE_HINT,
      borderTopRightRadius: "6px",
      borderBottomRightRadius: "6px",
      padding: "0 18px 0 16px",
      fontSize: "0.55rem",
      whiteSpace: "nowrap"
    },
    children: [
      jsx3(StaleStack, { items: staleItems }),
      jsx3(PickSlot, { pick }),
      tag ? jsx3("span", { style: Object.assign({ color: "var(--ui-text-tertiary)", fontSize: "0.68rem", fontWeight: 500 }, SELECTABLE), children: `tag: ${tag}` }) : null,
      jsx3(OpAddress, {
        text: opNumber,
        onSelectOp,
        fontSize: "0.68rem",
        color: "var(--ui-text-tertiary)",
        weight: 500
      }),
      jsx3("span", { style: { color: "var(--ui-text-quaternary)" }, children: durationText })
    ]
  });
}
var DIMMED_BG = "color-mix(in srgb, var(--ui-bg-elevated) 20%, transparent)";
function OperationCardHeader({
  statuses,
  icon,
  tool,
  added,
  removed,
  fileName,
  filePath,
  command,
  drawerOpen: initialOpen,
  opNumber,
  durationText,
  tag,
  pick = null,
  staleItems = [],
  attachedBelow = false,
  disabled = false,
  dimmed = false,
  onHeaderClick,
  toolColumnWidth,
  onSelectOp
}) {
  const [localOpen, setOpen] = useState2(initialOpen);
  const open = pick && pick.enabled ? true : localOpen;
  const drawerRef = useRef2(null);
  const [reserve, setReserve] = useState2(40);
  useLayoutEffect(() => {
    if (drawerRef.current) setReserve(drawerRef.current.offsetWidth);
  });
  return jsxs3("div", {
    className: "rounded-md border overflow-hidden",
    // CAPPED — не «на всякий случай»: в боевой панели карточка лежит внутри SDK ScrollArea,
    // который даёт внутреннему контейнеру ширину ПО СОДЕРЖИМОМУ. Тогда width:100% считать не от
    // чего (процент от родителя, чья ширина зависит от содержимого), и карточка растёт под самую
    // длинную строку, уезжая за правый край панели. Ширину приходится прижимать к 100% на КАЖДОМ
    // уровне цепочки, а не только у корня списка — см. brick.js, там та же причина.
    style: Object.assign(
      {
        position: "relative",
        borderColor: "var(--ui-stroke-secondary)",
        backgroundColor: dimmed ? DIMMED_BG : "var(--ui-bg-elevated)"
      },
      CAPPED,
      attachedBelow ? { borderBottomLeftRadius: 0, borderBottomRightRadius: 0 } : null,
      disabled ? { opacity: 0.45, filter: "grayscale(70%)", pointerEvents: "none" } : null
    ),
    children: [
      jsx3("div", {
        className: "flex items-center gap-1.5",
        onClick: onHeaderClick,
        style: {
          height: ROW_HEIGHT,
          boxSizing: "border-box",
          paddingLeft: "4px",
          paddingRight: `${Math.round(reserve * 0.95)}px`,
          cursor: onHeaderClick ? "pointer" : "default"
        },
        children: jsxs3("div", { className: "flex items-center gap-1.5", style: CAPPED, children: [
          jsx3(StatusColumn, { statuses }),
          // toolColumnWidth задаёт список (он один знает про соседей); без него карточка
          // самодостаточна — считает ширину по своему собственному имени тула.
          jsx3(ToolColumn, { icon, tool, added, removed, width: toolColumnWidth }),
          // Удаление помечается ПРИГЛУШЕНИЕМ, а не зачёркиванием (владелец, 2026-09-09:
          // «зачёркивание лишнее, лучше цвет текста прозрачнее»). Зачёркивание осталось за
          // `disabled` — это другое состояние и другой смысл.
          fileName ? jsx3(FileTarget, { fileName, filePath, strike: disabled, faded: dimmed }) : null,
          // flexShrink выше, чем у имени файла — команда менее важна и отдаёт место первой при
          // сужении панели, файл держит размер дольше.
          command ? jsx3("code", {
            className: "truncate",
            style: Object.assign({ fontSize: "0.65rem", color: "var(--ui-text-secondary)", minWidth: 0, flexShrink: 4 }, SELECTABLE),
            children: command
          }) : null,
          jsx3("div", { className: "flex-1 min-w-0" })
        ] })
      }),
      jsx3(Drawer, { drawerRef, open, onToggle: () => setOpen((o) => !o), durationText, opNumber, tag, pick, staleItems, onSelectOp }),
      // Крестика в режиме выбора нет: закрыть шторку он бы всё равно не смог (режим её
      // перекрывает), а кнопка, которая на вид закрывает и не закрывает, хуже её отсутствия.
      // Выход из режима — кнопкой режима, там же, где вход.
      open && !(pick && pick.enabled) ? jsx3(CloseButton, { onClick: () => setOpen(false) }) : null
    ]
  });
}

// desktop/src/operation-card-body.js
import { jsx as jsx4, jsxs as jsxs4 } from "react/jsx-runtime";
import { useState as useState3 } from "react";
var COMMENT_RE = /^\s*(\/\/|#|--|;|<!--|\*(?!\/))/;
var LEADING_NUMBER_RE = /^(→\s*)?(\s*\d+)([:.|]?\s*)/;
function headerEndIndex(lines) {
  const re = separatorRe();
  return lines.findIndex((l) => re.test(l));
}
function BodyLine({ text, header }) {
  if (header) {
    return jsx4("div", { style: { color: "var(--body-marker)" }, children: text || " " });
  }
  if (ownMarkerRe().test(text)) {
    return jsx4("div", { style: { color: "var(--body-marker)" }, children: text });
  }
  if (COMMENT_RE.test(text)) {
    return jsx4("div", { style: { color: "var(--body-comment)" }, children: text });
  }
  const m = text.match(LEADING_NUMBER_RE);
  if (m) {
    return jsxs4("div", { children: [
      jsx4("span", { style: { color: "var(--body-mark)" }, children: m[0] }),
      jsx4("span", { style: { color: "var(--body-text)" }, children: text.slice(m[0].length) })
    ] });
  }
  return jsx4("div", { style: { color: "var(--body-text)" }, children: text || " " });
}
function BodyPreview({ text }) {
  const lines = text.split("\n");
  const headerEnd = headerEndIndex(lines);
  return jsx4("div", {
    className: "rounded-md border",
    style: Object.assign({ maxHeight: "27rem", overflowX: "auto", overflowY: "auto", borderColor: "var(--ui-stroke-secondary)", backgroundColor: "var(--ui-bg-elevated)" }, CAPPED),
    children: jsx4("div", {
      className: "whitespace-pre font-mono p-2",
      style: Object.assign({
        fontSize: "0.65rem",
        lineHeight: 1.4,
        "--body-text": "var(--ui-text-primary)",
        "--body-comment": "var(--ui-text-tertiary)",
        "--body-marker": "var(--dt-secondary)",
        "--body-mark": "var(--ui-orange)"
      }, PRE, SELECTABLE),
      children: lines.map((line, i) => jsx4(BodyLine, { key: i, text: line, header: headerEnd >= 0 && i <= headerEnd }))
    })
  });
}
function CommandBlock({ command }) {
  const [open, setOpen] = useState3(false);
  const preview = command.split("\n").slice(0, 2).join("\n");
  const truncated = preview.length < command.length;
  return jsxs4("div", {
    className: "rounded-md border cursor-pointer",
    style: { borderColor: "var(--ui-stroke-secondary)", backgroundColor: "var(--ui-bg-elevated)" },
    onClick: () => setOpen((o) => !o),
    children: [
      jsxs4("div", {
        className: "flex items-center gap-1 px-2 pt-1",
        style: { fontSize: "0.55rem", color: "var(--ui-text-quaternary)" },
        children: [
          jsx4("span", { children: open ? "\u25BE" : "\u25B8" }),
          jsx4("span", { children: "command" })
        ]
      }),
      open ? jsx4("div", {
        className: "whitespace-pre font-mono px-2 pb-1.5",
        style: Object.assign({ fontSize: "0.6rem", color: "var(--ui-text-secondary)", maxHeight: "12rem", overflowX: "auto", overflowY: "auto" }, SELECTABLE),
        children: command
      }) : jsxs4("div", {
        className: "whitespace-pre font-mono px-2 pb-1.5 overflow-hidden",
        style: Object.assign({ fontSize: "0.6rem", color: "var(--ui-text-secondary)", maxHeight: "2.2em" }, SELECTABLE),
        children: [preview, truncated ? " \u2026" : ""]
      })
    ]
  });
}
function CardTray({ children }) {
  return jsx4("div", {
    className: "flex flex-col gap-1",
    style: Object.assign({}, CAPPED, {
      backgroundColor: "color-mix(in srgb, var(--ui-bg-elevated) 60%, var(--dt-background))",
      border: "1px solid var(--ui-stroke-secondary)",
      borderTop: "none",
      borderBottomLeftRadius: "6px",
      borderBottomRightRadius: "6px",
      padding: "4px 8px",
      boxSizing: "border-box"
    }),
    children
  });
}

// desktop/src/operation-card.js
function OperationCard({ headerProps, commandText, result }) {
  const [bodyOpen, setBodyOpen] = useState4(false);
  const canToggle = !headerProps.disabled && (commandText || result);
  return jsxs5("div", {
    style: CAPPED,
    children: [
      jsx5(OperationCardHeader, Object.assign({}, headerProps, {
        attachedBelow: bodyOpen,
        onHeaderClick: canToggle ? () => setBodyOpen((o) => !o) : void 0
      })),
      bodyOpen ? jsx5(CardTray, {
        children: [
          commandText ? jsx5(CommandBlock, { command: commandText }) : null,
          result ? jsx5(BodyPreview, { text: result }) : null
        ]
      }) : null
    ]
  });
}
function mockStatuses(tool) {
  const kindGuess = tool === "terminal" || tool === "execute_code" ? "warn" : "good";
  return [
    { tone: kindGuess, hint: `kind: MOCK (${kindGuess})` },
    { tone: "muted", hint: "read: MOCK (unknown)" }
  ];
}

// desktop/src/color-mixer.js
var THEME_ACCENT = "var(--dt-primary)";
function themeHue({ l, c, h }, share) {
  return `oklch(from color-mix(in oklch, oklch(${l} ${c} ${h}), ${THEME_ACCENT} ${share}%) ${l} ${c} h / 1)`;
}
var COMPACT_TONE = `color-mix(in oklch, var(--ui-purple), ${THEME_ACCENT} 45%)`;
var EXPORT_BASE = { l: 0.8, c: 0.17, h: 125 };
var EXPORT_HUE_SHARE = 25;
var EXPORT_TONE = themeHue(EXPORT_BASE, EXPORT_HUE_SHARE);
var IMPORT_BASE = { l: 0.75, c: 0.13, h: 215 };
var IMPORT_TONE = themeHue(IMPORT_BASE, EXPORT_HUE_SHARE);
var LLMOPS_BASE = { l: 0.78, c: 0.09, h: 75 };
var LLMOPS_TONE = themeHue(LLMOPS_BASE, EXPORT_HUE_SHARE);
var ACTION_COLOR = {
  content: "var(--ui-orange)",
  tool: "var(--ui-purple)",
  turn: "var(--ui-red)",
  compact: COMPACT_TONE,
  export: EXPORT_TONE,
  erase: "var(--ui-red)",
  import: IMPORT_TONE,
  llmops: LLMOPS_TONE
};
function railFill(tone) {
  return `color-mix(in oklch, ${tone}, transparent 45%)`;
}
var TOKEN_COLOR = {
  fixed: `color-mix(in oklch, var(--ui-yellow), ${THEME_ACCENT} 30%)`,
  msgs: THEME_ACCENT,
  growth: `color-mix(in oklch, ${THEME_ACCENT}, transparent 55%)`,
  ticked: COMPACT_TONE,
  threshold: `oklch(from ${THEME_ACCENT} l c calc(h - 160) / 1)`,
  over: "var(--ui-red)",
  track: "color-mix(in oklch, var(--ui-text-quaternary), transparent 75%)"
};

// desktop/src/card-adapter.js
var KIND_LABEL = { ok: "done", error: "error", "no-change": "no change" };
var FILE_TAG_RE = /FileTag:\s*(x[0-9a-fA-F]{2,4})/;
function extractFileTag(body) {
  if (typeof body !== "string" || body === "") return null;
  const open = blockOpen();
  for (const line of body.split("\n")) {
    if (line.includes(open)) {
      const b = line.match(blockTagRe());
      return b ? b[1] : null;
    }
    const m = line.match(FILE_TAG_RE);
    if (m) return m[1];
  }
  return null;
}
function splitPath(title) {
  const t = typeof title === "string" ? title : "";
  if (!t) return { fileName: "(no path)", filePath: null };
  const i = Math.max(t.lastIndexOf("/"), t.lastIndexOf("\\"));
  if (i < 0) return { fileName: t, filePath: null };
  return { fileName: t.slice(i + 1), filePath: t.slice(0, i) };
}
function staleItemsFor(pct) {
  const n2 = typeof pct === "number" && isFinite(pct) ? Math.round(pct) : 0;
  if (n2 <= 0) return [];
  return [{ label: `stale ${n2}%`, pct: n2 }];
}
function statusesFor(brick) {
  if (isErasedBrick(brick)) {
    const meta = brick.meta || {};
    const when = formatWhen(meta.erased_at);
    const by = typeof meta.erased_by === "string" ? meta.erased_by : "";
    return [{
      tone: "muted",
      hint: ["erased", when, by ? `by ${by}` : ""].filter(Boolean).join(" \xB7 ")
    }];
  }
  const out = [{
    tone: KIND_TONE[brick.kind] || "muted",
    hint: `result: ${KIND_LABEL[brick.kind] || brick.kind}`
  }];
  if (brick.read) {
    const label = READ_STATE_LABEL[brick.read.state] || brick.read.state;
    out.push({
      tone: READ_STATE_TONE[brick.read.state] || "muted",
      // БЕЗ ПРЕФИКСА. `в контексте: removed from context` — двуязычная бессмыслица, и
      // владелец требовала это убрать несколько раз (2026-09-09). Подпись состояния
      // самодостаточна: `дословно`, `сжато`, `removed from context` читаются без вводного
      // слова, а с ним последняя превращается в «в контексте: убрано из контекста».
      // Причину даёт маршрут (/reads), своих формулировок не сочиняем.
      hint: `${label}${brick.read.reason ? ` \u2014 ${brick.read.reason}` : ""}`
    });
  }
  return out;
}
function brickToCardProps(brick, toolColumnWidth, stalePct) {
  const meta = brick.meta || {};
  const isCommand = isCommandTool(brick.tool);
  const { fileName, filePath } = isCommand ? { fileName: null, filePath: null } : splitPath(brick.title);
  return {
    headerProps: {
      statuses: statusesFor(brick),
      staleItems: staleItemsFor(stalePct),
      // Приглушить фон карточки, если операция удалена из контекста. Просьба владельца
      // 2026-09-09: «когда их много, они сливаются» — одна точка состояния на карточке
      // теряется в списке из тридцати, а удаление необратимо и должно быть заметно.
      dimmed: !!(brick.read && brick.read.state === "removed_from_context"),
      // `#L7` — формат надгробия, заведённый в палитре задолго до механизма: всё содержимое на
      // месте, но карточка приглушена целиком, имя зачёркнуто и клики выключены. Владелец
      // указала на него сама: «графика есть, и там имя зачёркнуто».
      //
      // Отличие от `dimmed` существенное и оба состояния нужны рядом: `dimmed` — операция есть,
      // содержимого нет (после `drop content`), с ней можно работать. `disabled` — операции нет
      // вовсе, работать не с чем.
      disabled: isErasedBrick(brick),
      icon: toolIcon(brick.tool),
      tool: brick.tool,
      added: meta.added,
      removed: meta.removed,
      fileName,
      filePath,
      // Файловые тулы: subtitle — «что именно сделано» из аргументов вызова (`line 42`,
      // `@ line 7`, `pattern "..."`, `overwrite`). Командные: сама команда из title, а subtitle
      // (там лежит явно заданный workdir) приписываем к ней — иначе он не показался бы нигде.
      command: isCommand ? [brick.title, brick.subtitle].filter(Boolean).join("  \xB7 ") || null : brick.subtitle || null,
      durationText: formatDuration(meta.duration_s),
      tag: extractFileTag(brick.body),
      // Адрес операции (Plan09). Слот в карточке существовал с самого начала —
      // «формат придуман, генератор не существует»; генератор теперь есть, это
      // сервис леджера (file_ops/_ledger.py::next_op), и адрес приходит в
      // meta.op из шапки нашего же ответа.
      // В карточке метка СОКРАЩЁННАЯ (`op:`), в тексте для модели — полная
      // (`Operation ID:`, _tool_text.OP_LABEL). Расхождение намеренное: полное
      // слово нужно модели, которой сокращение приходится угадывать, а слот в
      // шапке карточки узкий и читает его человек, у которого адрес перед
      // глазами каждый день. Значение при этом одно и то же.
      opNumber: typeof meta.op === "string" && meta.op ? `op: ${meta.op}` : null,
      pick: null,
      toolColumnWidth
    },
    commandText: null,
    result: brick.body
  };
}
var CHIP_STATUS = {
  error: { glyph: "\u25CF", tone: "var(--ui-red)", title: "result: error" },
  noChange: { glyph: "=", tone: "var(--ui-text-quaternary)", title: "result: no change" },
  dropped: { glyph: "\u2298", tone: ACTION_COLOR.content, title: "removed from context (drop content)" },
  compacted: { glyph: "\u25D0", tone: ACTION_COLOR.compact, title: "compacted" },
  evicted: { glyph: "\u2205", tone: "var(--ui-red)", title: "evicted: no longer in the active window" },
  unknown: { glyph: "?", tone: "var(--ui-text-quaternary)", title: "read state: could not be matched" },
  // НЕ ромб (владелец, 2026-09-29: «похож на точку»). Пунктирная рамка — «не наш». На чипе
  // встаёт НА МЕСТО иконки тула, как и точка ошибки: иконка чужому тулу не нужна.
  foreign: { glyph: "\u2B1A", tone: "var(--ui-orange)", title: "foreign tool: no operation of ours carries this tool_call_id" },
  unrecorded: { glyph: "\u25CB", tone: "var(--ui-text-quaternary)", title: "ours, no ledger record" },
  // Результат привезён импортом и его текст правили руками до импорта (Plan27, владелец
  // 2026-10-05: `≠` оранжевым). Сам факт импорта значка не получает — только строку в ховере.
  edited: { glyph: "\u2260", tone: "var(--ui-orange)", title: "edited by hand before import" }
};
function chipState(ledger, fact) {
  return fact && fact.state || ledger && ledger.state || "";
}
function chipStatuses({ ledger, fact, foreign, unrecorded }) {
  const out = [];
  const kind = fact && fact.kind || "ok";
  if (kind === "error") out.push(CHIP_STATUS.error);
  else if (kind === "no-change") out.push(CHIP_STATUS.noChange);
  const state = chipState(ledger, fact);
  if (state === "removed_from_context") out.push(CHIP_STATUS.dropped);
  else if (state === "compacted") out.push(CHIP_STATUS.compacted);
  else if (state === "evicted") out.push(CHIP_STATUS.evicted);
  else if (state === "unknown") out.push(CHIP_STATUS.unknown);
  if (fact && fact.edited) out.push(CHIP_STATUS.edited);
  if (foreign) out.push(CHIP_STATUS.foreign);
  if (unrecorded) out.push(CHIP_STATUS.unrecorded);
  return out;
}
function callTitles(fact) {
  const call = fact && fact.call;
  if (!call || !call.args) return { title: "", subtitle: "", command: false };
  const name = String(call.name || fact.name || "");
  const t = brickTitles(name, call.args) || { title: "", subtitle: "" };
  const blank2 = !t.title || t.title === name || ["read", "write", "patch", "search"].includes(t.title);
  return { title: blank2 ? "" : t.title, subtitle: t.subtitle || "", command: isCommandTool(name) };
}
function chipTarget({ fact, target }) {
  if (fact && fact.file) return fact.file;
  const t = callTitles(fact);
  if (t.title) return t.command ? t.title : splitPath(t.title).fileName;
  const raw = String(target || "").replace(/\\/g, "/");
  return raw ? raw.split("/").filter(Boolean).pop() || raw : "";
}
function chipDiff(ledger, fact) {
  const added = ledger ? Number(ledger.added) || 0 : 0;
  const removed = ledger ? Number(ledger.removed) || 0 : 0;
  if (added || removed) return { added, removed };
  return diffFromRow(fact);
}
var WRITE_TEXT_RES = [
  /(?:Created new file|Overwritten) \((\d+) lines?\)/,
  /Inserted (\d+) lines? at/,
  /Appended (\d+) lines? \(/
];
function diffFromRow(fact) {
  const text = String(fact && fact.text || "");
  const call = fact && fact.call || null;
  const name = call && call.name;
  if (!name || !text || /^\s*ERROR/.test(text) || nothingChangedRe().test(text)) return null;
  if (name === "write_file") {
    for (const re of WRITE_TEXT_RES) {
      const m = text.match(re);
      if (m) return { added: Number(m[1]), removed: 0 };
    }
    return null;
  }
  if (name === "patch") {
    const guess = call.cut ? null : diffFromArgs(name, call.args);
    if (guess && (guess.added || guess.removed)) return guess;
    const m = text.match(patchLinesRe());
    if (m) {
      const net = Number(m[2]) - Number(m[1]);
      return net ? { added: Math.max(net, 0), removed: Math.max(-net, 0) } : null;
    }
  }
  return null;
}
function chipTipLines({ name, op, ledger, fact, target, statuses, tokens, freshness, duration }) {
  const lines = [];
  lines.push([name, op ? `op ${op}` : ""].filter(Boolean).join(" \xB7 "));
  const t = callTitles(fact);
  const command = t.command ? t.title : "";
  const path = fact && fact.path || (command ? "" : t.title) || String(target || "");
  if (command) lines.push(`command: ${command}`);
  else if (path) lines.push(path);
  if (t.subtitle) lines.push(t.subtitle);
  const diff = chipDiff(ledger, fact);
  if (diff) lines.push(`lines: +${diff.added} -${diff.removed}`);
  (statuses || []).forEach((s) => lines.push(`${s.glyph}  \u2014  ${s.title}`));
  if (ledger && ledger.reason && !(fact && fact.state)) lines.push(String(ledger.reason));
  const from = fact && fact.imported_from;
  if (from && typeof from === "object") {
    lines.push(`imported: was op ${from.op || "?"} in ${from.session || "?"}`);
  }
  const dur = formatDuration(duration);
  lines.push([tokens ? `${tokens} tk` : "", `freshness: ${freshness}`, dur ? `took ${dur}` : ""].filter(Boolean).join(" \xB7 "));
  return lines.filter(Boolean);
}

// desktop/src/stack-mock.js
var T0 = Date.UTC(2026, 9, 6, 12, 0, 0);
function mock(i, tool, title, subtitle, body, meta = {}, extra = {}) {
  return Object.assign({
    tool,
    ts: T0 + i * 7e3,
    session_id: "mock",
    tool_id: `mock_${i}`,
    title,
    subtitle,
    body,
    kind: "ok",
    meta: Object.assign({ op: (64 + i).toString(16).padStart(4, "0"), duration_s: 0.1 + i / 10 }, meta)
  }, extra);
}
var MOCK_BRICKS = [
  mock(
    1,
    "read_file",
    "read_file /workspace/src/app.py",
    "line 1 \xB7 count 40",
    "# Operation ID: 0041\n# File: /workspace/src/app.py\n# Lines: 1-40 of 212 \xB7 LF\n\u25CB\u25CB\u25CB\n\u25A0BLOCK\n   1 | import os\n   2 | import sys\n   3 |\n   4 | def main():\n   5 |     return run(sys.argv)\n\u25A0END",
    {},
    { note: "\u0421\u043D\u0430\u0447\u0430\u043B\u0430 \u043F\u043E\u0441\u043C\u043E\u0442\u0440\u044E, \u043A\u0430\u043A \u0443\u0441\u0442\u0440\u043E\u0435\u043D\u0430 \u0442\u043E\u0447\u043A\u0430 \u0432\u0445\u043E\u0434\u0430." }
  ),
  mock(
    2,
    "patch",
    "patch /workspace/src/app.py",
    "line 5",
    "# Operation ID: 0042\n# File: /workspace/src/app.py\n# Lines: 212 -> 214\n\u25CB\u25CB\u25CB\nReplaced 1 occurrence.",
    { added: 3, removed: 1 }
  ),
  mock(
    3,
    "terminal",
    "terminal pytest -q tests/test_app.py",
    "",
    "\u25C6 Operation ID: 0043\n\u25C6 Exit code: 0\n\u25CB\u25CB\u25CB\n12 passed in 0.84s",
    {}
  ),
  mock(
    4,
    "search_files",
    "search_files /workspace/src",
    "pattern 'run('",
    "# Operation ID: 0044\n\u25CB\u25CB\u25CB\n/workspace/src/app.py:5:    return run(sys.argv)\n/workspace/src/cli.py:18:def run(argv):",
    {}
  ),
  mock(
    5,
    "write_file",
    "write_file /workspace/notes/todo.md",
    "",
    "<!-- Operation ID: 0045 -->\n<!-- File: /workspace/notes/todo.md -->\n\u25CB\u25CB\u25CB\nCreated new file (6 lines).",
    { added: 6, removed: null, state: "removed_from_context" },
    { read: { state: "removed_from_context", reason: "" } }
  ),
  mock(
    6,
    "read_file",
    "read_file /workspace/src/cli.py",
    "",
    "",
    { state: "erased", erased_at: (T0 + 6e4) / 1e3, erased_by: "erase" }
  )
];

// desktop/src/core-window.js
var ROW_PX = 34;
var NOTE_ESTIMATE_PX = 30;
var OVERSCAN_PX = 400;
var TAIL_GUARANTEE = 20;
function estimateBrickHeight(b) {
  return ROW_PX + (b && b.note ? NOTE_ESTIMATE_PX : 0);
}
function renderWindow(bricks, scrollTop, viewportHeight) {
  const list = Array.isArray(bricks) ? bricks : [];
  const heights = list.map(estimateBrickHeight);
  const total = heights.reduce((sum, h) => sum + h, 0);
  if (!list.length) return { start: 0, end: 0, topSpacer: 0, bottomSpacer: 0, total: 0 };
  const lo = Math.max(0, (scrollTop || 0) - OVERSCAN_PX);
  const hi = (scrollTop || 0) + (viewportHeight || 0) + OVERSCAN_PX;
  let offset = 0;
  let start = 0;
  let end = list.length;
  for (let i = 0; i < list.length; i++) {
    if (offset >= hi) {
      end = i;
      break;
    }
    if (offset + heights[i] <= lo) start = i + 1;
    offset += heights[i];
  }
  end = Math.max(end, list.length - TAIL_GUARANTEE);
  if (start > end) start = end;
  let topSpacer = 0;
  for (let i = 0; i < start; i++) topSpacer += heights[i];
  let bottomSpacer = 0;
  for (let i = end; i < list.length; i++) bottomSpacer += heights[i];
  return { start, end, topSpacer, bottomSpacer, total };
}

// desktop/src/palette/index.js
import { jsx as jsx18, jsxs as jsxs18 } from "react/jsx-runtime";
import * as sdk6 from "@hermes/plugin-sdk";

// desktop/src/palette/buttons.js
import { jsx as jsx6, jsxs as jsxs6 } from "react/jsx-runtime";
function jsxSemanticSoft(label, colorHex) {
  return jsx6("button", {
    className: "px-3 py-1 rounded-md text-xs font-medium cursor-pointer active:scale-95 transition-colors duration-150 ease-out select-none",
    style: Object.assign(
      { backgroundColor: `color-mix(in oklch, ${colorHex} 8%, transparent)`, color: colorHex },
      SELECTABLE
    ),
    onMouseEnter: (e) => {
      e.currentTarget.style.backgroundColor = `color-mix(in oklch, ${colorHex} 15%, transparent)`;
    },
    onMouseLeave: (e) => {
      e.currentTarget.style.backgroundColor = `color-mix(in oklch, ${colorHex} 8%, transparent)`;
    },
    children: label
  });
}
function ButtonsSection() {
  return paletteTile("Buttons", jsxs6("div", {
    className: "flex flex-col gap-1.5",
    style: {
      fontSize: "0.7rem",
      // Токены от var(--dt-primary), определены ОДИН РАЗ здесь и переиспользуются ниже
      // (#A2, #A7) — правило C2, не дублировать формулу/значение.
      // bg: ~85% непрозрачности — чуть снижает контраст заливки в обеих темах.
      "--btn-primary-bg": "color-mix(in oklch, var(--dt-primary) 85%, transparent)",
      // hover-bg: lightness тянется К СЕРЕДИНЕ (0.5) — тот же приём, что в #G8.
      "--btn-primary-hover-bg": "oklch(from var(--dt-primary) calc(l + (0.5 - l) * 0.55) c h)",
      // contrast: НЕ математика от собственной светлоты --dt-primary (проверено 2026-09-02 —
      // не инвертируется со сменой темы, если у бренд-цвета светлота похожа в обеих темах).
      // Вместо этого — цвет фона приложения (--dt-background): он ПО ОПРЕДЕЛЕНИЮ инвертируется
      // с темой, и весь текст приложения и так рассчитан быть читаемым на нём.
      "--btn-primary-contrast": "var(--dt-background)"
    },
    children: [
      // Primary variants — visible buttons, not hidden links
      jsx6("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#A1 Primary" }),
      jsxs6("div", { className: "flex flex-wrap gap-1", children: [
        // #A2 — БЫЛ на bg-primary/90: этот opacity-slash класс не докомпилен (найдено 2026-09-02,
        // визуально — почти невидимая кнопка, только текст). Голый bg-primary РАБОТАЕТ (см. #A6/#A7),
        // поэтому фон и текст переведены на инлайн var(--dt-primary). Текст и hover-фон — через
        // --btn-primary-contrast/--btn-primary-hover-bg (определены один раз на обёртке выше).
        jsx6("button", {
          className: "px-3 py-1 rounded-md text-xs font-medium cursor-pointer active:scale-95 transition-colors duration-150 ease-out select-none",
          style: Object.assign({ backgroundColor: "var(--btn-primary-bg)", color: "var(--btn-primary-contrast)" }, SELECTABLE),
          onMouseEnter: (e) => {
            e.currentTarget.style.backgroundColor = "var(--btn-primary-hover-bg)";
          },
          onMouseLeave: (e) => {
            e.currentTarget.style.backgroundColor = "var(--btn-primary-bg)";
          },
          children: "#A2 Primary"
        }),
        // #A4 — soft primary (tinted background, primary text)
        jsx6("button", { className: "px-3 py-1 rounded-md bg-primary/20 text-primary text-xs font-medium cursor-pointer hover:bg-primary/30 active:scale-95 transition-colors duration-150 ease-out select-none", children: "#A4 Soft" }),
        // #A5 — ghost ✓
        jsx6("button", { className: "px-3 py-1 rounded-full border border-(--ui-stroke-secondary) bg-transparent text-muted-foreground text-xs font-medium cursor-pointer hover:bg-(--ui-bg-tertiary) hover:text-foreground active:scale-95 transition-colors duration-150 ease-out select-none", children: "#A5 Ghost" }),
        // #A6 — outline ✓
        jsx6("button", { className: "px-3 py-1 rounded-full border border-primary/40 bg-transparent text-primary text-xs font-medium cursor-pointer hover:bg-primary/10 active:scale-95 transition-colors duration-150 ease-out select-none", children: "#A6 Outline" }),
        // Accent text on primary background — обычное состояние var(--dt-accent), hover
        // переключает на --btn-primary-contrast (ту же вычисленную переменную, что и текст
        // #A2 — не отдельная константа, JS-обработчик вместо hover: в className, потому что
        // Tailwind вырезает произвольные значения).
        jsx6("button", { className: "px-3 py-1 rounded-full text-xs font-medium cursor-pointer active:scale-95 transition-all duration-150 ease-out select-none", style: Object.assign({ backgroundColor: "var(--btn-primary-bg)", color: "var(--dt-accent)" }, SELECTABLE), onMouseEnter: (e) => {
          e.currentTarget.style.color = "var(--btn-primary-contrast)";
        }, onMouseLeave: (e) => {
          e.currentTarget.style.color = "var(--dt-accent)";
        }, children: "#A7 accent on primary" })
      ] }),
      // Secondary / neutral — bodies must be visible, not just text on background
      jsx6("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#A8 Neutral" }),
      jsxs6("div", { className: "flex flex-wrap gap-1", children: [
        // #A9 Secondary — БЫЛ на bg-background/90 (не opacity-класс темы, а почти непрозрачный
        // background) — визуально читался ТЯЖЕЛЕЕ primary, хотя должен быть легче. Заменено на
        // var(--ui-bg-secondary) (M18, живая, буквально "вторичная поверхность" по смыслу темы).
        jsx6("button", { className: "px-3 py-1 rounded-md border border-(--ui-stroke-quaternary) text-xs font-medium cursor-pointer hover:bg-(--ui-bg-tertiary) active:scale-95 transition-colors duration-150 ease-out select-none", style: Object.assign({ backgroundColor: "var(--ui-bg-secondary)" }, SELECTABLE), children: "#A9 Secondary" }),
        // #A10 Muted — БЫЛ на bg-muted/20 (слишком бледный/не докомпилен, фона почти не видно).
        // Заменено на var(--ui-bg-tertiary) (M19) — видимый, но заведомо более тихий, чем Secondary.
        jsx6("button", { className: "px-3 py-1 rounded-md text-muted-foreground text-xs font-medium cursor-pointer hover:bg-primary/5 active:scale-95 transition-colors duration-150 ease-out select-none", style: Object.assign({ backgroundColor: "var(--ui-bg-tertiary)" }, SELECTABLE), children: "#A10 Muted" }),
        // #A11 Chip — pill shape with faint accent border (primary tint instead of stroke)
        jsx6("button", { className: "px-2.5 py-1 rounded-full bg-primary/5 text-xs font-medium cursor-pointer hover:bg-primary/10 active:scale-95 transition-colors duration-100 ease-out select-none", children: "#A11 Chip" }),
        // #A12 Tag → — pill shape, scale UP on hover (not down), accent border distinct from chip
        jsx6("button", { className: "px-2.5 py-1 rounded-full border border-(--ui-stroke-secondary)/40 text-xs font-medium cursor-pointer hover:bg-(--ui-row-hover-background) hover:scale-105 active:scale-95 transition-all duration-100 ease-out select-none", children: "#A12 Tag \u2192" })
      ] }),
      // Destructive / semantic — INLINE STYLES ONLY (Tailwind CSS vars AND hex arbitrary values are tree-shaken!)
      jsx6("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#A13 Semantic" }),
      jsxs6("div", { className: "flex flex-wrap gap-1", children: [
        // БЫЛ на hsl(var(--destructive))/hsl(var(--destructive-foreground)) — голых переменных
        // без dt- нет в теме вовсе (в F12-дампе есть только --dt-destructive), фон был невидим,
        // оставался только текст. Заменено на var(--ui-red) (M10, живая) + var(--dt-background)
        // для текста — тот же приём, что и у #A2/#A7 (инвертируется с темой).
        jsx6("button", {
          className: "px-3 py-1 rounded-md text-xs font-medium cursor-pointer active:scale-95 transition-colors duration-150 ease-out select-none",
          style: Object.assign({ backgroundColor: "color-mix(in oklch, var(--ui-red) 85%, transparent)", color: "var(--dt-background)" }, SELECTABLE),
          children: "#A14 Destructive"
        }),
        // Живые переменные из M10-16, а не захардкоженный hex — тот же цвет, но не константа.
        jsxSemanticSoft("#A15 Error soft", "var(--ui-red)"),
        jsxSemanticSoft("#A16 Success", "var(--ui-green)"),
        jsxSemanticSoft("#A17 Warning", "var(--ui-yellow)"),
        jsxSemanticSoft("#A18 Purple", "var(--ui-purple)")
      ] }),
      jsx6("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#A19 Disabled (\u0441\u043E\u0441\u0442\u043E\u044F\u043D\u0438\u0435, \u043D\u0435 \u043E\u0442\u0434\u0435\u043B\u044C\u043D\u044B\u0439 \u0432\u0438\u0434 \u2014 \u043D\u0430 \u043B\u044E\u0431\u043E\u043C \u0432\u0438\u0434\u0435 \u0432\u044B\u0448\u0435)" }),
      jsxs6("div", { className: "flex flex-wrap gap-1", children: [
        // #A20 — та же проблема с bg-primary/90, что и была у #A2; тот же фикс.
        jsx6("button", {
          disabled: true,
          className: "px-3 py-1 rounded-md text-xs font-medium opacity-40 pointer-events-none cursor-not-allowed select-none",
          style: Object.assign({ backgroundColor: "var(--dt-primary)", color: "var(--dt-primary-foreground, #fcfcfc)" }, SELECTABLE),
          children: "#A20 Disabled primary"
        }),
        jsx6("button", { disabled: true, className: "px-3 py-1 rounded-md border border-(--ui-stroke-secondary) bg-transparent text-xs font-medium opacity-50 pointer-events-none cursor-not-allowed select-none", children: "#A21 Disabled ghost" })
      ] }),
      // Sizes
      jsx6("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#A22 Sizes" }),
      jsxs6("div", { className: "flex flex-wrap items-center gap-1", children: [
        jsx6("button", { className: "px-2 py-0.5 rounded text-[0.6rem] font-medium bg-primary/20 text-primary cursor-pointer hover:bg-primary/30 select-none", children: "#A23 XS" }),
        jsx6("button", { className: "px-2.5 py-1 rounded-md text-xs font-medium bg-primary/20 text-primary cursor-pointer hover:bg-primary/30 select-none", children: "#A24 SM" }),
        jsx6("button", { className: "px-3 py-1.5 rounded-md text-sm font-medium bg-primary/20 text-primary cursor-pointer hover:bg-primary/30 select-none", children: "#A25 MD" })
      ] }),
      // Reveal on hover — buttons that appear when hovering their container (Hermes pattern)
      jsx6("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#A26 Reveal on hover (group-hover)" }),
      jsxs6("div", { className: "flex flex-col gap-1 select-none", style: SELECTABLE, children: [
        // Row with hidden action buttons — matches Hermes session list / file row pattern
        jsxs6("div", {
          className: "group relative flex items-center justify-between px-2 py-1 rounded-md border border-(--ui-stroke-quaternary) cursor-default hover:bg-(--ui-row-hover-background) transition-colors duration-100 ease-out",
          children: [
            jsx6("span", { className: "text-[0.6rem] truncate flex-1", children: "#A27 Hover this row to reveal actions \u2192" }),
            jsxs6("div", { className: "flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity duration-100 ease-out", children: [
              jsx6("button", { className: "px-2 py-0.5 rounded text-[0.55rem] bg-transparent cursor-pointer hover:bg-(--ui-bg-tertiary) select-none", children: "#A28 edit" }),
              jsx6("button", { className: "px-2 py-0.5 rounded text-[0.55rem] bg-destructive/10 text-destructive cursor-pointer hover:bg-destructive/20 select-none", children: "#A29 delete" })
            ] })
          ]
        }),
        // Icon-style reveal buttons (common in Hermes toolbars)
        jsxs6("div", {
          className: "group relative flex items-center justify-between px-2 py-1 rounded-md border border-(--ui-stroke-quaternary) cursor-default hover:bg-(--ui-row-hover-background) transition-colors duration-100 ease-out",
          children: [
            jsx6("span", { className: "text-[0.6rem] truncate flex-1", children: "#A30 Hover for icons \u2192" }),
            jsxs6("div", { className: "flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity duration-100 ease-out", children: [
              jsx6("button", { className: "w-5 h-5 rounded flex items-center justify-center text-[0.6rem] bg-transparent cursor-pointer hover:bg-(--ui-bg-tertiary) select-none", title: "#A31 copy", children: "\u29C9" }),
              jsx6("button", { className: "w-5 h-5 rounded flex items-center justify-center text-[0.6rem] bg-transparent cursor-pointer hover:bg-(--ui-bg-tertiary) select-none", title: "#A32 more", children: "\u22EF" })
            ] })
          ]
        })
      ] })
    ]
  }));
}

// desktop/src/palette/cards.js
import { jsx as jsx7, jsxs as jsxs7 } from "react/jsx-runtime";
function CardsSection() {
  return paletteTile("Cards & Panels", jsxs7("div", {
    className: "flex flex-col gap-1.5",
    style: {
      fontSize: "0.7rem",
      // ЕДИНСТВЕННОЕ место с значениями: 7 токенов shiki (светлый / тёмный) через light-dark().
      // На уровне ВСЕЙ секции (не только #B15-контейнера), чтобы #B16 мог быть отдельным
      // сиблингом и всё равно унаследовать эти же переменные — без дублирования (правило C2).
      "--cc-bg": "light-dark(rgb(255 255 255 / 0.7), rgb(34 39 46 / 0.7))",
      // фон карточки (прозрачность ~30%)
      "--cc-text": "light-dark(rgb(31, 35, 40), rgb(173, 186, 199))",
      // обычный текст
      "--cc-comment": "light-dark(rgb(87, 96, 106), rgb(118, 131, 144))",
      // комментарий
      "--cc-string": "light-dark(rgb(10, 48, 105), rgb(150, 208, 255))",
      // строки
      "--cc-fn": "light-dark(rgb(130, 80, 223), rgb(220, 189, 251))",
      // функции/ключевые слова
      "--cc-op": "light-dark(rgb(207, 34, 46), rgb(244, 112, 103))",
      // операторы
      "--cc-num": "light-dark(rgb(5, 80, 174), rgb(108, 182, 255))",
      // числа/голубое слово
      "--cc-mark": "light-dark(rgb(149, 56, 0), rgb(246, 157, 80))"
      // маркеры строк (оранжевый, из #B16)
    },
    children: [
      jsx7("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#B1 Surfaces" }),
      jsxs7("div", { className: "grid grid-cols-2 gap-1", children: [
        jsx7("div", { className: "px-2 py-1.5 rounded-md border border-(--ui-stroke-secondary) bg-card text-[0.6rem] truncate", title: "#B2 bg-card + border", children: "#B2 bg-card" }),
        jsx7("div", { className: "px-2 py-1.5 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) text-[0.6rem] truncate", title: "#B3 elevated + border", children: "#B3 elevated" }),
        // #B4 — БЫЛ на bg-background/80 (opacity-slash, ненадёжен — та же болезнь, что у кнопок).
        // Инлайн color-mix + backdrop-blur-sm класс оставлен (сам blur — не opacity-класс).
        jsx7("div", { className: "px-2 py-1.5 rounded-lg border border-(--ui-stroke-tertiary) backdrop-blur-sm text-[0.6rem] truncate", style: { backgroundColor: "color-mix(in oklch, var(--dt-background), transparent 20%)" }, title: "#B4 glass + blur", children: "#B4 glass" }),
        // #B5 — БЫЛ на bg-(--ui-bg-secondary) (paren-var класс, не докомпилен для bg- с этим
        // именем — border-(--ui-stroke-*) с тем же именем работает, bg- нет; непредсказуемо
        // без проверки каждого класса, поэтому инлайн — надёжнее). var(--ui-bg-secondary)
        // оказался слишком ярким — заменено на var(--ui-bg-card), тише (2026-09-02).
        jsx7("div", { className: "px-2 py-1.5 rounded-md border border-transparent text-[0.6rem] truncate", style: { backgroundColor: "var(--ui-bg-card)" }, title: "#B5 secondary no border", children: "#B5 secondary" })
      ] }),
      // Radius variants
      jsx7("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#B6 Radius" }),
      jsxs7("div", { className: "flex flex-wrap gap-1", children: [
        jsx7("div", { className: "px-2 py-1 rounded-sm border border-(--ui-stroke-secondary) bg-muted/40 text-[0.6rem]", children: "#B7 sm" }),
        jsx7("div", { className: "px-2 py-1 rounded-md border border-(--ui-stroke-secondary) bg-muted/40 text-[0.6rem]", children: "#B8 md" }),
        jsx7("div", { className: "px-2 py-1 rounded-lg border border-(--ui-stroke-secondary) bg-muted/40 text-[0.6rem]", children: "#B9 lg" }),
        jsx7("div", { className: "px-2 py-1 rounded-xl border border-(--ui-stroke-secondary) bg-muted/40 text-[0.6rem]", children: "#B10 xl" }),
        jsx7("div", { className: "px-3 py-1 rounded-full border border-(--ui-stroke-secondary) bg-muted/40 text-[0.6rem]", children: "#B11 full" })
      ] }),
      // Shadow variants
      jsx7("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#B12 Shadows" }),
      jsxs7("div", { className: "flex flex-wrap gap-1", children: [
        jsx7("div", { className: "px-2 py-1 rounded-md border border-(--ui-stroke-secondary) bg-card shadow-sm text-[0.6rem]", children: "#B13 sm" }),
        jsx7("div", { className: "px-2 py-1 rounded-md border border-(--ui-stroke-secondary) bg-card shadow-md text-[0.6rem]", children: "#B14 md" })
      ] }),
      // ── CODE CARD (shiki github-dark-dimmed/light-default) ────────────────
      // Токены определены ОДИН РАЗ ниже как CSS-переменные (--cc-*), все спаны — только var().
      jsx7("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#B15 Code card (shiki themes, tokens = vars)" }),
      jsxs7("div", {
        className: "rounded-md border border-(--ui-stroke-secondary) overflow-hidden select-none",
        style: Object.assign({ backgroundColor: "var(--cc-bg)" }, CAPPED),
        children: [
          jsx7("pre", {
            className: "m-0 overflow-x-auto font-mono leading-relaxed whitespace-pre",
            style: Object.assign({ padding: "0.5rem 0.75rem", fontSize: "0.6rem" }, SELECTABLE_SMALL, SCROLLER),
            children: [
              // Comment (gray) — var only, zero literals
              jsx7("span", { style: { color: "var(--cc-comment)" }, children: "// #B17 comment token\n" }),
              // String (blue)
              jsx7("span", { style: { color: "var(--cc-string)" }, children: "'#B18 mix 25%'" }),
              // Plain text (dark gray)
              jsx7("span", { style: { color: "var(--cc-text)" }, children: " \u2192 color-" }),
              // Function (purple)
              jsx7("span", { style: { color: "var(--cc-fn)" }, children: "mix" }),
              // Operator (red)
              jsx7("span", { style: { color: "var(--cc-op)" }, children: "(" }),
              // Keyword (blue)
              jsx7("span", { style: { color: "var(--cc-num)" }, children: "in" }),
              // Plain text
              jsx7("span", { style: { color: "var(--cc-text)" }, children: " oklch, #e75e78, var(--dt-primary) " }),
              // Number (blue)
              jsx7("span", { style: { color: "var(--cc-num)" }, children: "25" }),
              // Operator (red)
              jsx7("span", { style: { color: "var(--cc-op)" }, children: "%)\n\n" }),
              // Second line for visual density
              jsx7("span", { style: { color: "var(--cc-comment)" }, children: "// #B19 second comment\n" }),
              jsx7("span", { style: { color: "var(--cc-text)" }, children: "#B20 label + " }),
              jsx7("span", { style: { color: "var(--cc-string)" }, children: "'#B21 mix 50%'" }),
              jsx7("span", { style: { color: "var(--cc-text)" }, children: ' \u2192 "color-mix(in oklch)";\n\n' }),
              // Third line: pure operators / keywords
              jsx7("span", { style: { color: "var(--cc-text)" }, children: "#B22 tokens: " }),
              jsx7("span", { style: { color: "var(--cc-op)" }, children: "(operator)" }),
              jsx7("span", { style: { color: "var(--cc-text)" }, children: " + " }),
              jsx7("span", { style: { color: "var(--cc-fn)" }, children: "(keyword)" })
            ]
          })
        ]
      }),
      // ── #B16 FILE-DIFF TOOL-BLOCK (сиблинг #B15, не вложен — наследует --cc-* с уровня
      // ВСЕЙ секции, см. её style выше; было вложено в #B15, вынесено по просьбе 2026-09-01) ──
      // Счётчики и строка: дословные precompiled-классы из исходников приложения.
      // Live-переменные (--ui-diff-*, --conversation-scaffold-text) — ссылки, значения НЕ копируем.
      jsx7("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#B16 File-diff tool-block (live vars + shared cc tokens)" }),
      jsxs7("div", {
        className: "rounded-md border border-(--ui-stroke-tertiary) overflow-hidden select-none",
        // Тот же --cc-bg, что у #B15 (та же секция, наследуется с общего style выше) — по
        // просьбе 2026-09-02, раньше у #B16 фона не было задано вообще.
        style: Object.assign({ backgroundColor: "var(--cc-bg)" }, CAPPED),
        children: [
          // Шапка: live var для имени файла; счётчики — precompiled Tailwind emerald/rose (как в приложении)
          jsxs7("div", { className: "flex items-center gap-1 px-2 py-1 border-b border-(--ui-stroke-tertiary)", title: "F12: --conversation-scaffold-text?", children: [
            jsx7("span", { style: Object.assign({ color: "var(--cc-comment)", fontSize: "0.65rem" }, SELECTABLE), children: "\u2263" }),
            jsx7("span", { className: "min-w-0 truncate font-mono text-[0.6rem]", style: { color: "var(--conversation-scaffold-text)" }, title: "F12: --conversation-scaffold-text?", children: "implementation_plan.md" }),
            jsx7("span", { className: "ml-auto shrink-0 font-mono text-[0.55rem] tabular-nums text-emerald-600 dark:text-emerald-400", title: "precompiled class \u0438\u0437 \u0438\u0441\u0445\u043E\u0434\u043D\u0438\u043A\u043E\u0432 \u043F\u0440\u0438\u043B\u043E\u0436\u0435\u043D\u0438\u044F (\u043A\u0430\u043A \u0432 \u0447\u0430\u0442\u0435)", children: "+1" }),
            jsx7("span", { className: "shrink-0 font-mono text-[0.55rem] tabular-nums text-rose-600 dark:text-rose-400", title: "precompiled class \u0438\u0437 \u0438\u0441\u0445\u043E\u0434\u043D\u0438\u043A\u043E\u0432 \u043F\u0440\u0438\u043B\u043E\u0436\u0435\u043D\u0438\u044F (\u043A\u0430\u043A \u0432 \u0447\u0430\u0442\u0435)", children: "\u22121" })
          ] }),
          // Тело diff: токены текста — var(--cc-*), live vars только для подсветки строк
          jsx7("pre", {
            className: "m-0 overflow-x-auto font-mono leading-relaxed whitespace-pre",
            style: Object.assign({ padding: "0.5rem 0" }, SELECTABLE_SMALL, SCROLLER),
            title: "F12: --ui-diff-add/remove-background/border?",
            children: [
              jsxs7("div", { className: "whitespace-pre pl-3 pr-2 py-px border-l-2 border-transparent", style: Object.assign({ fontSize: "0.6rem" }, SELECTABLE), title: "#B23 normal line: --cc-mark + --cc-text/--cc-num", children: [
                jsx7("span", { style: { color: "var(--cc-mark)" }, children: "4." }),
                jsx7("span", { style: { color: "var(--cc-text)" }, children: " [ ] V1\u2013V3 (\xA70) \u2014 \u0437\u0430\u0444\u0438\u043A\u0441\u0438\u0440\u043E\u0432\u0430\u0442\u044C \u0432 impl-log" })
              ] }),
              jsxs7("div", { className: "whitespace-pre pl-3 pr-2 py-px border-l-2", style: Object.assign({ fontSize: "0.6rem", borderColor: "var(--ui-diff-remove-border)", backgroundColor: "var(--ui-diff-remove-background)" }, SELECTABLE), title: "#B24 removed line: live vars --ui-diff-remove-*", children: [
                jsx7("span", { style: { color: "var(--cc-mark)" }, children: "5." }),
                jsx7("span", { style: { color: "var(--cc-text)" }, children: " [ ] Handler \u0432 tools.py (\u0432\u044B\u0434\u0430\u0447\u0430 \u0438\u0437 " }),
                jsx7("span", { style: { color: "var(--cc-num)" }, children: "`_PENDING`" }),
                jsx7("span", { style: { color: "var(--cc-text)" }, children: ")" })
              ] }),
              jsxs7("div", { className: "whitespace-pre pl-3 pr-2 py-px border-l-2", style: Object.assign({ fontSize: "0.6rem", borderColor: "var(--ui-diff-add-border)", backgroundColor: "var(--ui-diff-add-background)" }, SELECTABLE), title: "#B25 added line: live vars --ui-diff-add-*", children: [
                jsx7("span", { style: { color: "var(--cc-mark)" }, children: "6." }),
                jsx7("span", { style: { color: "var(--cc-text)" }, children: " [ ] Handler \u0432 " }),
                jsx7("span", { style: { color: "var(--cc-num)" }, children: "`__init__.py`" }),
                jsx7("span", { style: { color: "var(--cc-text)" }, children: " (\u0432\u044B\u0434\u0430\u0447\u0430 \u0438\u0437 " }),
                jsx7("span", { style: { color: "var(--cc-num)" }, children: "`_PENDING`" }),
                jsx7("span", { style: { color: "var(--cc-text)" }, children: ")" })
              ] })
            ]
          })
        ]
      })
    ]
  }));
}

// desktop/src/palette/badges.js
import { jsx as jsx8, jsxs as jsxs8 } from "react/jsx-runtime";
import * as sdk3 from "@hermes/plugin-sdk";
function BadgesSection() {
  return paletteTile("Badges & Chips", jsxs8("div", {
    className: "flex flex-col gap-1.5",
    style: { fontSize: "0.7rem" },
    children: [
      jsx8("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#C1 Badges" }),
      jsxs8("div", { className: "flex flex-wrap gap-1", children: [
        jsx8("span", { className: "inline-flex items-center px-2 py-0.5 rounded-full text-[0.6rem] font-medium bg-primary/10 text-primary select-none", style: SELECTABLE, children: "#C2 primary" }),
        jsx8("span", { className: "inline-flex items-center px-2 py-0.5 rounded-full text-[0.6rem] font-medium bg-(--ui-green)/10 text-(--ui-green) select-none", style: SELECTABLE, children: "#C3 success" }),
        // БЫЛ на bg-(--ui-red)/10 text-(--ui-red) — фон почти не виден (красный на 10% темнее/
        // тусклее воспринимается, чем зелёный/жёлтый на той же непрозрачности). Инлайн, 18%.
        jsx8("span", { className: "inline-flex items-center px-2 py-0.5 rounded-full text-[0.6rem] font-medium select-none", style: Object.assign({ backgroundColor: "color-mix(in oklch, var(--ui-red), transparent 82%)", color: "var(--ui-red)" }, SELECTABLE), children: "#C4 error" }),
        jsx8("span", { className: "inline-flex items-center px-2 py-0.5 rounded-full text-[0.6rem] font-medium bg-(--ui-yellow)/10 text-(--ui-yellow) select-none", style: SELECTABLE, children: "#C5 warning" }),
        jsx8("span", { className: "inline-flex items-center px-2 py-0.5 rounded-full text-[0.6rem] font-medium bg-muted/40 text-muted-foreground select-none", style: SELECTABLE, children: "#C6 neutral" })
      ] }),
      // Dot + label combos. БЫЛО tone:'error'/'warning' — невалидные значения для SDK
      // StatusDot (по коду проекта валидны 'good'/'muted'/'bad'/'warn' — см. KIND_TONE в
      // core.js, READ_STATE_TONE в brick.js), точка молча не рисовалась. Исправлено на
      // 'bad'/'warn'. Заодно текст рядом с точкой тоже покрашен в тон — не только точка
      // несёт смысл (по просьбе 2026-09-02, "точки мало").
      jsx8("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#C7 Status badges (dot + \u0446\u0432\u0435\u0442\u043D\u043E\u0439 \u0442\u0435\u043A\u0441\u0442)" }),
      jsxs8("div", { className: "flex flex-wrap gap-2", children: [
        jsxs8("span", { className: "inline-flex items-center gap-1 text-[0.6rem] font-medium select-none", style: Object.assign({ color: "var(--ui-green)" }, SELECTABLE), children: [sdkC(sdk3.StatusDot, "span", { tone: "good" }), jsx8("span", { children: "#C8 live" })] }),
        jsxs8("span", { className: "inline-flex items-center gap-1 text-[0.6rem] font-medium text-(--ui-text-tertiary) select-none", style: SELECTABLE, children: [sdkC(sdk3.StatusDot, "span", { tone: "muted" }), jsx8("span", { children: "#C9 idle" })] }),
        jsxs8("span", { className: "inline-flex items-center gap-1 text-[0.6rem] font-medium select-none", style: Object.assign({ color: "var(--ui-red)" }, SELECTABLE), children: [sdkC(sdk3.StatusDot, "span", { tone: "bad" }), jsx8("span", { children: "#C10 failed" })] }),
        jsxs8("span", { className: "inline-flex items-center gap-1 text-[0.6rem] font-medium select-none", style: Object.assign({ color: "var(--ui-yellow)" }, SELECTABLE), children: [sdkC(sdk3.StatusDot, "span", { tone: "warn" }), jsx8("span", { children: "#C11 pending" })] })
      ] }),
      // #C12 убран 2026-09-02 — дублировал #C3/#C4 (та же заливка-пилюля, ничего нового).
      // Пульсация осталась единственным реально ДРУГИМ способом показать статус (не точка,
      // не заливка) — для "активно сейчас". opacity-анимация, GPU-composited (приём #G8).
      jsx8("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: '#C12 Status \u0441 \u043F\u0443\u043B\u044C\u0441\u0430\u0446\u0438\u0435\u0439 (\u0442\u043E\u043B\u044C\u043A\u043E \u0434\u043B\u044F "\u0430\u043A\u0442\u0438\u0432\u043D\u043E \u0441\u0435\u0439\u0447\u0430\u0441")' }),
      jsxs8("span", { className: "inline-flex items-center gap-1 text-[0.6rem] font-medium select-none", style: Object.assign({ color: "var(--ui-green)" }, SELECTABLE), children: [
        jsx8("span", { className: "w-1.5 h-1.5 rounded-full animate-pulse", style: { backgroundColor: "var(--ui-green)" } }),
        jsx8("span", { children: "#C12 processing\u2026" })
      ] }),
      // Строка символов — это просто ТЕКСТ (обычные юникод-глифы, не иконочный шрифт), а не
      // формы из CSS: выделяется/копируется как обычная строка. Поэтому не по одному элементу
      // с номером, а один выделяемый набор — бери нужный символ прямо отсюда. Крупнее обычной
      // буквы (1rem), для маркеров "перед тулом/именем/статусом".
      jsx8("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#C13 \u2014 \u0441\u0442\u0440\u043E\u043A\u0430 \u0441\u0438\u043C\u0432\u043E\u043B\u043E\u0432 (\u043A\u043E\u043F\u0438\u0440\u0443\u0435\u043C\u0430\u044F)" }),
      jsx8("div", {
        className: "text-[1rem] leading-relaxed tracking-widest",
        style: Object.assign({ color: "var(--ui-text-secondary)" }, SELECTABLE),
        // Убраны 2026-09-02: ⏺▶⏸⏹ рендерятся цветными (emoji-стиль шрифта, не моно-глиф);
        // •‣ слишком мелкие на глаз, бесполезны.
        children: "\u2713 \u2717 \u25CF \u25CB \u25A0 \u25A1 \u25C6 \u25C7 \u25B2 \u25BC \u2605 \u2606 \u26A0 \u24D8 \u26A1 \u21BB"
      })
    ]
  }));
}

// desktop/src/palette/typography.js
import { jsx as jsx9, jsxs as jsxs9 } from "react/jsx-runtime";
function TypographySection() {
  return paletteTile("Typography", jsxs9("div", {
    className: "flex flex-col gap-1",
    children: [
      jsx9("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#D1 Color hierarchy" }),
      jsx9("p", { className: "text-xs font-medium text-foreground leading-tight select-none", style: SELECTABLE, children: "#D2 Primary \u2014 main text (font-medium)" }),
      jsx9("p", { className: "text-[0.7rem] text-muted-foreground leading-tight select-none", style: SELECTABLE, children: "#D3 Secondary / muted \u2014 labels & subtitles" }),
      jsx9("p", { className: "text-[0.65rem] text-(--ui-text-secondary) leading-tight select-none", style: SELECTABLE, children: "#D4 Tertiary UI text \u2014 metadata hints" }),
      jsx9("p", { className: "text-[0.6rem] text-(--ui-text-tertiary) leading-tight select-none", style: SELECTABLE, children: "#D5 Quaternary \u2014 very faint captions & timestamps" }),
      jsx9("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1.5", children: "#D6 Sizes & styles" }),
      jsxs9("p", { className: "flex items-baseline gap-2 leading-tight select-none", style: SELECTABLE, children: [
        jsx9("span", { className: "text-[length:var(--conversation-text-font-size)] font-normal text-foreground", children: "#D7 Chat" }),
        jsx9("span", { className: "text-xs font-medium text-muted-foreground", children: "#D8 xs/med" }),
        jsx9("span", { className: "text-[0.6875rem] font-mono text-(--ui-text-secondary)", children: "#D9 tool mono" })
      ] }),
      jsx9("span", { className: "text-sm font-semibold text-primary select-none", style: SELECTABLE, children: "#D10 Primary strong" }),
      // #D12 — код-чип из markdown-рендера. НЕГАТИВНАЯ НАХОДКА (2026-09-01): составные
      // Tailwind Typography-модификаторы (`prose-code:rounded-[...]` и т.п.) до плагина
      // НЕ доезжают — в отличие от простых precompiled-классов, эта комбинированная запись
      // в собранном CSS плагину недоступна (текст рендерился чёрным, без темы). Взято
      // напрямую через F12 (Computed): цвет/фон — живые переменные `--ui-inline-code-*`
      // (фон — тот же цвет текста на ~7% непрозрачности, задан отдельной переменной, не
      // color-mix нашими руками); border-radius 0.25rem — константа (не цвет, не по C1).
      jsx9("code", {
        dir: "ltr",
        style: Object.assign({
          color: "var(--ui-inline-code-foreground)",
          backgroundColor: "var(--ui-inline-code-background)",
          borderRadius: "0.25rem",
          padding: "1px 0.1875rem",
          fontFamily: "monospace",
          fontSize: "0.9em",
          // Прямой flex-child в flex-col родителе растягивается на всю ширину строки
          // (align-items:stretch по умолчанию) — без этого фон полз бы под весь ряд,
          // а не только под текст.
          alignSelf: "flex-start"
        }, SELECTABLE),
        children: "#D12 desktop/Recon__UI-Decisions.md"
      }),
      // #D13 — заголовок сворачиваемого tool-блока («Saved to memory», F12, 2026-09-01).
      // Шеврон-кнопка (codicon codicon-chevron-right) из оригинала сюда сознательно НЕ взята —
      // она подразумевает hover/expand-поведение, которого у нас тут нет.
      // ПОДТВЕРЖДЕНО ЖИВЬЁМ (2026-09-02): классы `tool-memory-legendary-*` — не Tailwind, а
      // собственные классы приложения — реально доезжают до плагина. `tool-memory-legendary-
      // title` красит текст ГРАДИЕНТОМ (background + background-clip:text, --tool-memory-
      // legendary-from/-mid/-to), поэтому в оригинале title был text-transparent — цвет шёл не
      // через color. --ui-text-tertiary — тоже подтверждена.
      jsxs9("div", { className: "group/disclosure-row relative flex w-full max-w-full min-w-0 text-(--ui-text-tertiary) mt-1.5", style: SELECTABLE, children: [
        jsxs9("span", { className: "flex min-w-0 max-w-fit items-start gap-1.5", children: [
          jsx9("span", {
            className: "grid size-3.5 shrink-0 place-items-center self-center tool-memory-legendary-glyph",
            title: "F12: \u043A\u043B\u0430\u0441\u0441 tool-memory-legendary-glyph \u2014 \u043D\u0435 Tailwind, \u0441\u0432\u043E\u0439 \u043B\u0438 CSS \u0434\u043E\u0439\u0434\u0451\u0442 \u0434\u043E \u043F\u043B\u0430\u0433\u0438\u043D\u0430?",
            children: "\u25C6"
          }),
          jsx9("span", {
            className: "block min-w-0 max-w-full overflow-hidden whitespace-nowrap text-[length:var(--conversation-tool-font-size)] leading-(--conversation-line-height) tool-memory-legendary-title",
            title: "\u043F\u043E\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043D\u043E: \u043A\u043B\u0430\u0441\u0441 \u0441\u0430\u043C \u043A\u0440\u0430\u0441\u0438\u0442 \u0442\u0435\u043A\u0441\u0442 \u0433\u0440\u0430\u0434\u0438\u0435\u043D\u0442\u043E\u043C (background-clip:text), \u0438\u043D\u043B\u0430\u0439\u043D-\u0446\u0432\u0435\u0442 \u043D\u0435 \u0437\u0430\u0434\u0430\u0432\u0430\u043B\u0441\u044F",
            children: "#D13 Saved to memory"
          })
        ] })
      ] })
    ]
  }));
}

// desktop/src/palette/interactive.js
import { jsx as jsx10, jsxs as jsxs10 } from "react/jsx-runtime";
function InteractiveSection() {
  return paletteTile("Interactive States", jsxs10("div", {
    className: "flex flex-col gap-1.5",
    style: { fontSize: "0.7rem" },
    children: [
      jsx10("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#F1 Hover states (mouse over)" }),
      jsxs10("div", { className: "flex flex-col gap-1", children: [
        jsx10("button", { className: "w-full px-2 py-1 rounded-md text-xs text-left cursor-pointer select-none transition-colors duration-100 ease-out hover:bg-(--ui-row-hover-background) active:bg-(--ui-bg-tertiary)", children: "#F2 Row hover \u2192" }),
        // БЫЛ на hover:bg-accent/40 — голое --accent (не --dt-accent) мертвое в этой теме
        // (та же болезнь, что была у #A14/--destructive), hover визуально не отличался от #F2.
        // Заменено на var(--dt-accent) через JS-обработчик (Tailwind hover: с произвольными
        // значениями/переменными вырезается — тот же паттерн, что и в кнопках Buttons).
        jsx10("button", {
          className: "w-full px-2 py-1 rounded-md text-xs text-left cursor-pointer select-none transition-colors duration-100 ease-out",
          onMouseEnter: (e) => {
            e.currentTarget.style.backgroundColor = "color-mix(in oklch, var(--dt-accent), transparent 60%)";
          },
          onMouseLeave: (e) => {
            e.currentTarget.style.backgroundColor = "transparent";
          },
          children: "#F3 Accent hover \u2192"
        }),
        jsx10("button", { className: "w-full px-2 py-1 rounded-md border border-(--ui-stroke-secondary) text-xs text-left cursor-pointer select-none transition-colors duration-100 ease-out hover:border-(--ui-stroke-primary)", children: "#F4 Border highlight \u2192" })
      ] }),
      jsx10("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#F5 Disabled states" }),
      jsxs10("div", { className: "flex flex-col gap-1", children: [
        jsx10("button", { disabled: true, className: "w-full px-2 py-1 rounded-md text-xs text-left opacity-40 pointer-events-none cursor-not-allowed select-none bg-muted/40", children: "#F6 Disabled row" })
        // #F7 убрана 2026-09-02 — дублировала #A20 Disabled primary (Buttons), ничего нового
        // не показывала. Opacity Test Stand убран туда же — вопрос закрыт (М36), тут был
        // хардкод-константа (#f97316) для проверки самого механизма, ценности не несёт.
      ] })
    ]
  }));
}

// desktop/src/palette/loaders.js
import { jsx as jsx11, jsxs as jsxs11 } from "react/jsx-runtime";
import { useState as useState5 } from "react";
import * as sdk4 from "@hermes/plugin-sdk";
function LoadersSection() {
  return paletteTile("Loaders & Animations", jsxs11("div", {
    className: "flex flex-col gap-1.5",
    style: { fontSize: "0.7rem" },
    children: [
      // #G1 — единственный тестовый элемент анимации: composited sweep через
      // transform (не background-position, не JS-таймер). Полоса-подсветка
      // едет через translateX внутри overflow:hidden — GPU-слой, без repaint.
      jsx11("style", {
        dangerouslySetInnerHTML: { __html: `
          @keyframes sweep-composited {
            from { transform: translateX(-120%); }
            to   { transform: translateX(220%); }
          }
          .animate-sweep-composited {
            animation: sweep-composited 1.8s linear infinite;
            will-change: transform;
          }
          @keyframes bounce-custom {
            0%, 100% { transform: translateY(0); animation-timing-function: cubic-bezier(0.8, 0, 1, 1); }
            50% { transform: translateY(-4px); animation-timing-function: cubic-bezier(0, 0, 0.2, 1); }
          }
          .animate-bounce-custom {
            animation: bounce-custom 1s infinite;
          }
          @keyframes crossfade-derived {
            0%, 100% { opacity: 1; }
            50% { opacity: 0; }
          }
          .animate-crossfade-derived {
            animation: crossfade-derived 3s ease-in-out infinite;
            will-change: opacity;
          }
          @keyframes appear-once {
            from { opacity: 0; transform: translateY(4px) scale(0.98); }
            to   { opacity: 1; transform: translateY(0) scale(1); }
          }
          .animate-appear-once {
            /* both \u2014 \u0434\u0435\u0440\u0436\u0438\u0442 \u043A\u043E\u043D\u0435\u0447\u043D\u043E\u0435 \u0441\u043E\u0441\u0442\u043E\u044F\u043D\u0438\u0435 \u043F\u043E\u0441\u043B\u0435 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043D\u0438\u044F; \u0418\u0413\u0420\u0410\u0415\u0422 \u041E\u0414\u0418\u041D \u0420\u0410\u0417 (\u043D\u0435 infinite) \u2014
               \u0441\u0440\u0430\u0431\u0430\u0442\u044B\u0432\u0430\u0435\u0442 \u043F\u0440\u0438 \u043C\u043E\u043D\u0442\u0438\u0440\u043E\u0432\u0430\u043D\u0438\u0438 \u0443\u0437\u043B\u0430, \u0442.\u0435. \u043F\u0440\u0438 \u043F\u0435\u0440\u0435\u043A\u043B\u044E\u0447\u0435\u043D\u0438\u0438 FileOps \u2192 Palette. */
            animation: appear-once 220ms ease-out both;
            will-change: opacity, transform;
          }
          @keyframes spin-custom {
            to { transform: rotate(360deg); }
          }
          .animate-spin-custom {
            /* \u0428\u0442\u0430\u0442\u043D\u044B\u0439 Tailwind animate-spin \u043E\u043A\u0430\u0437\u0430\u043B\u0441\u044F \u043D\u0435 \u0434\u043E\u043A\u043E\u043C\u043F\u0438\u043B\u0435\u043D \u0432 \u044D\u0442\u043E\u0439 \u0441\u0431\u043E\u0440\u043A\u0435 (2026-09-02,
               #G2 \u043D\u0435 \u043A\u0440\u0443\u0442\u0438\u043B\u0441\u044F \u0432\u043E\u043E\u0431\u0449\u0435) \u2014 \u0441\u0432\u043E\u0439 keyframe, \u043A\u0430\u043A \u0438 \u043E\u0441\u0442\u0430\u043B\u044C\u043D\u044B\u0435 \u0432 \u044D\u0442\u043E\u043C \u0431\u043B\u043E\u043A\u0435, \u043D\u0430\u0434\u0451\u0436\u043D\u0435\u0435. */
            animation: spin-custom 1s linear infinite;
            will-change: transform;
          }
          @keyframes blink-custom {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.15; }
          }
          .animate-blink-slow   { animation: blink-custom 1.6s ease-in-out infinite; will-change: opacity; }
          .animate-blink-medium { animation: blink-custom 0.9s ease-in-out infinite; will-change: opacity; }
          .animate-blink-fast   { animation: blink-custom 0.45s ease-in-out infinite; will-change: opacity; }
        ` }
      }),
      jsx11("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#G1 Animation test (composited transform, not background-position)" }),
      jsx11("div", {
        className: "h-3 w-full rounded-full overflow-hidden relative select-none",
        style: { backgroundColor: "var(--dt-primary)" },
        children: jsx11("div", {
          className: "animate-sweep-composited absolute inset-y-0 w-1/3",
          style: { background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.5), transparent)" }
        })
      }),
      jsx11("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#G2-4 Spinners / pulse / bounce (\u0442\u043E\u0436\u0435 composited: transform, opacity)" }),
      jsxs11("div", { className: "flex items-center gap-3", children: [
        jsxs11("span", { className: "inline-flex items-center gap-1.5 text-[0.6rem] select-none", style: SELECTABLE, children: [
          // border-t-primary тоже не докомпилен — кольцо было одноцветным, вращение
          // физически шло, но не было видно. Инлайн top-цвет делает "засечку" заметной.
          jsx11("div", { className: "w-3 h-3 rounded-full animate-spin-custom", style: { border: "2px solid var(--ui-stroke-secondary)", borderTopColor: "var(--dt-primary)" } }),
          jsx11("span", { children: "#G2 spin" })
        ] }),
        jsxs11("span", { className: "inline-flex items-center gap-1.5 text-[0.6rem] select-none", style: SELECTABLE, children: [
          sdkC(sdk4.StatusDot, "span", { tone: "muted" }),
          jsx11("div", { className: "w-2 h-2 rounded-full bg-(--ui-text-tertiary) animate-pulse" }),
          jsx11("span", { children: "#G3 pulse" })
        ] }),
        jsxs11("span", { className: "inline-flex items-center gap-1.5 text-[0.6rem] select-none", style: SELECTABLE, children: [
          jsxs11("div", { className: "flex gap-0.5 items-center", children: [
            jsx11("div", { className: "w-1 h-1 rounded-full bg-foreground animate-bounce-custom", style: { animationDelay: "0ms" } }),
            jsx11("div", { className: "w-1 h-1 rounded-full bg-foreground animate-bounce-custom", style: { animationDelay: "200ms" } }),
            jsx11("div", { className: "w-1 h-1 rounded-full bg-foreground animate-bounce-custom", style: { animationDelay: "400ms" } })
          ] }),
          jsx11("span", { children: "#G4 bounce (css)" })
        ] })
      ] }),
      // Progress bars (CSS-only)
      jsx11("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#G5 Progress bars" }),
      jsxs11("div", { className: "flex flex-col gap-1", children: [
        jsx11("span", { className: "text-[0.5rem] text-(--ui-text-quaternary)", children: "#G6 primary bar (bg-primary/80)" }),
        jsx11("div", { className: "w-full h-3 rounded-full bg-muted/40 overflow-hidden select-none", children: jsx11("div", { className: "h-full w-3/4 rounded-full bg-primary/80" }) }),
        jsx11("span", { className: "text-[0.5rem] text-(--ui-text-quaternary)", children: "#G7 green bar (bg-green/70)" }),
        jsx11("div", { className: "w-full h-2 rounded-full bg-(--ui-stroke-quaternary) overflow-hidden select-none", children: jsx11("div", { className: "h-full w-1/2 rounded-full bg-(--ui-green)/70" }) })
      ] }),
      // #G8 — анимация МЕЖДУ базовым цветом и его производным (OKLCH RCS), не двумя
      // константами. Производный тон: lightness сдвигается К СЕРЕДИНЕ (0.5) — светлый темнеет,
      // тёмный светлеет, без if/else, чистой формулой `l + (0.5 - l) * k`. Анимируется НЕ
      // background-color (это перекраска/repaint каждый кадр — то, из-за чего убрали shimmer
      // в #G-секции раньше), а opacity верхнего слоя — GPU-композит, дёшево.
      jsx11("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-1 mt-3", children: "#G8 Crossfade base \u2194 derived (OKLCH RCS, opacity-composited)" }),
      jsx11("div", {
        className: "h-4 w-full rounded-md relative overflow-hidden",
        style: {
          // Нижний слой — производный тон (сдвиг к середине lightness).
          backgroundColor: "oklch(from var(--dt-primary) calc(l + (0.5 - l) * 0.55) c h)",
          border: "1px solid rgba(255,255,255,0.1)"
        },
        children: jsx11("div", {
          className: "absolute inset-0 animate-crossfade-derived",
          // Верхний слой — сам var(--dt-primary); анимируется его opacity, не цвет.
          style: { backgroundColor: "oklch(from var(--dt-primary) l c h)" }
        })
      }),
      // #G9 — появление при монтировании, группой + кнопка replay. Выход из палитры и обратно
      // для повтора неудобен (панель неизвестно где), поэтому реиграем через смену `key` —
      // React снимает и заново монтирует поддерево, анимация монтирования срабатывает опять,
      // без выхода из палитры вообще.
      jsx11(AppearGroupDemo, {}),
      // #G10 — срочность/важность через СКОРОСТЬ мигания, та же opacity-анимация
      // (composited), разница только в длительности. Оранжевый — "важная штука мигает".
      jsx11("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-3", children: "#G10 Blink \u2014 \u0441\u0440\u043E\u0447\u043D\u043E\u0441\u0442\u044C \u0447\u0435\u0440\u0435\u0437 \u0441\u043A\u043E\u0440\u043E\u0441\u0442\u044C (composited opacity)" }),
      jsxs11("div", { className: "flex items-center gap-3", children: [
        jsxs11("span", { className: "inline-flex items-center gap-1.5 text-[0.6rem] select-none", style: SELECTABLE, children: [
          jsx11("div", { className: "w-2.5 h-2.5 rounded-full animate-blink-slow", style: { backgroundColor: "var(--ui-orange)" } }),
          jsx11("span", { children: "#G10 slow" })
        ] }),
        jsxs11("span", { className: "inline-flex items-center gap-1.5 text-[0.6rem] select-none", style: SELECTABLE, children: [
          jsx11("div", { className: "w-2.5 h-2.5 rounded-full animate-blink-medium", style: { backgroundColor: "var(--ui-orange)" } }),
          jsx11("span", { children: "medium" })
        ] }),
        jsxs11("span", { className: "inline-flex items-center gap-1.5 text-[0.6rem] select-none", style: SELECTABLE, children: [
          jsx11("div", { className: "w-2.5 h-2.5 rounded-full animate-blink-fast", style: { backgroundColor: "var(--ui-orange)" } }),
          jsx11("span", { children: "fast" })
        ] })
      ] })
    ]
  }));
}
function AppearGroupDemo() {
  const [gen, setGen] = useState5(0);
  return jsxs11("div", {
    children: [
      jsxs11("div", { className: "flex items-center justify-between mb-1 mt-3", children: [
        jsx11("span", { className: "text-[0.6rem] text-(--ui-text-quaternary)", children: "#G9 \u041F\u043E\u044F\u0432\u043B\u0435\u043D\u0438\u0435 (\u0433\u0440\u0443\u043F\u043F\u0430, replay \u043D\u0438\u0436\u0435)" }),
        jsx11("button", {
          className: "px-2 py-0.5 rounded-full text-[0.55rem] font-medium cursor-pointer select-none hover:bg-(--ui-bg-tertiary)",
          style: { border: "1px solid var(--ui-stroke-secondary)", color: "var(--ui-text-secondary)" },
          onClick: () => setGen((g) => g + 1),
          children: "\u21BB replay"
        })
      ] }),
      jsx11("div", {
        key: gen,
        className: "flex flex-col gap-1",
        children: [0, 1, 2].map((i) => jsx11("div", {
          key: i,
          className: "animate-appear-once px-2 py-1.5 rounded-md text-[0.6rem] font-medium",
          style: Object.assign(
            { backgroundColor: "var(--ui-bg-tertiary)", border: "1px solid var(--ui-stroke-secondary)", color: "var(--ui-text-primary)", animationDelay: `${i * 70}ms` },
            SELECTABLE
          ),
          children: `#G9.${i + 1} card`
        }))
      })
    ]
  });
}

// desktop/src/palette/lists.js
import { jsx as jsx12, jsxs as jsxs12 } from "react/jsx-runtime";
import * as sdk5 from "@hermes/plugin-sdk";
function ListsSection() {
  return paletteTile("Lists & Rows", jsxs12("div", {
    className: "flex flex-col gap-1.5",
    style: { fontSize: "0.7rem" },
    children: [
      jsx12("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#H1 Interactive list (scrollable)" }),
      // Test A: overflow-y-auto with inline maxHeight — max-h-* Tailwind classes are tree-shaken!
      jsxs12("div", {
        className: "rounded-md border border-(--ui-stroke-secondary) overflow-y-auto select-none scrollbar-dt scrollbar-fade",
        style: Object.assign({}, CAPPED, { maxHeight: "7rem" }),
        // 28 * 0.25 = 7rem (inline, bypasses tree-shaking)
        children: [
          // Номер адресации убран у чистого филлера (2026-09-02) — ссылаться на "возьми стиль
          // с #H10" бессмысленно, это просто строка-наполнитель для скролла. #H4/#H5 номер
          // оставлен — там есть содержательная разница (точка-статус, конкретный вид строки).
          jsxListRow("Item one \u2014 hover me"),
          jsxListRow("Second item with detail \u2192"),
          jsxListRow("#H4 Third item"),
          jsxs12(ListRowWithDot, { label: "#H5 Active item", tone: "good" }),
          jsxListRow("Fifth item"),
          jsxListRow("Sixth item \u2014 long text that truncates"),
          jsxListRow("Seventh row for scroll testing"),
          jsxListRow("Eighth item \u2014 more content here"),
          jsxListRow("Ninth row to push height further"),
          jsxListRow("Tenth item \u2014 should be visible only with scrolling"),
          jsxListRow("Eleventh row \u2014 definitely needs scrollbar now")
        ]
      }),
      // Test B: pure inline styles for scroll container (no Tailwind classes at all)
      jsx12("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1.5", children: "#H8 Inline style scroll test" }),
      jsxs12("div", {
        className: "",
        // no Tailwind classes — pure inline styles to isolate what works
        style: Object.assign(
          { maxHeight: "6rem", overflowY: "auto", borderRadius: "0.375rem", border: "1px solid var(--ui-stroke-secondary)" },
          CAPPED
        ),
        children: [
          jsx12("div", { className: "", style: Object.assign({ padding: "4px 8px", fontSize: "0.6rem" }, SELECTABLE), children: "First inline scroll row" }),
          jsx12("div", { className: "", style: Object.assign({ padding: "4px 8px", fontSize: "0.6rem" }, SELECTABLE), children: "Second row with some text" }),
          jsx12("div", { className: "", style: Object.assign({ padding: "4px 8px", fontSize: "0.6rem" }, SELECTABLE), children: "Third item here" }),
          jsx12("div", { className: "", style: Object.assign({ padding: "4px 8px", fontSize: "0.6rem" }, SELECTABLE), children: "Fourth row \u2014 more content" }),
          jsx12("div", { className: "", style: Object.assign({ padding: "4px 8px", fontSize: "0.6rem" }, SELECTABLE), children: "Fifth row for testing" }),
          jsx12("div", { className: "", style: Object.assign({ padding: "4px 8px", fontSize: "0.6rem" }, SELECTABLE), children: "Sixth row should force scroll" }),
          jsx12("div", { className: "", style: Object.assign({ padding: "4px 8px", fontSize: "0.6rem" }, SELECTABLE), children: "Seventh row definitely needs scrollbar" }),
          jsx12("div", { className: "", style: Object.assign({ padding: "4px 8px", fontSize: "0.6rem" }, SELECTABLE), children: "Eighth and final test row" })
        ]
      }),
      // Test C: horizontal scroll — long content that overflows width
      jsx12("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1.5", children: "#H17 Horizontal scroll test" }),
      jsxs12("div", {
        className: "",
        // no Tailwind classes — pure inline styles
        style: Object.assign(
          { maxHeight: "4rem", overflowX: "auto", overflowY: "hidden", borderRadius: "0.375rem", border: "1px solid var(--ui-stroke-secondary)" },
          CAPPED
        ),
        children: jsx12("div", {
          className: "",
          // inner wrapper wider than container to force horizontal scroll
          style: Object.assign({ minWidth: "600px", padding: "4px 8px", fontSize: "0.6rem", whiteSpace: "nowrap" }, SELECTABLE),
          children: "This is a very long row that should force horizontal scrolling because the content width exceeds the container width significantly so we can test if overflow-x-auto works properly in this plugin context \u2713 scroll right \u2192"
        })
      })
    ]
  }));
}
function jsxListRow(text) {
  return jsx12("button", {
    className: "w-full flex items-center gap-1.5 px-2 py-1 row-hover rounded-md cursor-pointer text-[0.6rem] text-left select-none transition-colors duration-100 ease-out hover:bg-(--ui-row-hover-background) active:bg-(--ui-bg-tertiary)",
    children: text
  });
}
function ListRowWithDot({ label, tone }) {
  return jsxs12("div", {
    className: "flex items-center gap-1.5 px-2 py-1 rounded-md bg-(--ui-row-active-background) cursor-pointer text-[0.6rem] select-none",
    children: [
      sdkC(sdk5.StatusDot, "span", { tone }),
      jsx12("span", { className: "truncate font-medium", children: label })
    ]
  });
}

// desktop/src/palette/dividers.js
import { jsx as jsx13, jsxs as jsxs13 } from "react/jsx-runtime";
function DividersSection() {
  return paletteTile("Borders & Dividers", jsxs13("div", {
    className: "flex flex-col gap-2",
    style: { fontSize: "0.7rem" },
    children: [
      jsx13("div", { className: "text-[0.6rem] text-(--ui-text-quaternary)", children: "#J1 Stroke levels (top to bottom)" }),
      jsxs13("div", { className: "flex flex-col gap-1 select-none", style: SELECTABLE, children: [
        // БЫЛ на border-(--ui-stroke-primary) — этот класс не докомпилен (--ui-stroke-primary
        // самая сильная по теме, 24%, но рендерилась слабее всех остальных). border-(--ui-
        // stroke-secondary/tertiary/quaternary) ниже используются в проекте намного чаще и
        // докомпилены нормально — инлайн для primary надёжнее.
        jsx13("div", { children: "#J2 Primary" }),
        jsx13("hr", { className: "m-0", style: { borderTop: "1px solid var(--ui-stroke-primary)" } }),
        jsx13("div", { children: "#J3 Secondary (most common)" }),
        jsx13("hr", { className: "border-t border-(--ui-stroke-secondary) m-0" }),
        jsx13("div", { children: "#J4 Tertiary" }),
        jsx13("hr", { className: "border-t border-(--ui-stroke-tertiary) m-0" }),
        jsx13("div", { children: "#J5 Quaternary (faintest)" }),
        jsx13("hr", { className: "border-t border-(--ui-stroke-quaternary) m-0" })
      ] })
    ]
  }));
}

// desktop/src/palette/sdk.js
import { jsx as jsx14, jsxs as jsxs14 } from "react/jsx-runtime";
function SdkSection() {
  return paletteTile("SDK Components", jsxs14("div", {
    className: "flex flex-col gap-1.5",
    style: { fontSize: "0.7rem" },
    children: [
      // StatusDot-точки убраны 2026-09-02 — дублировали #C7-C11 (тот же sdk.StatusDot, та же
      // демонстрация тонов). Оставлен только справочник API — единственное, чего нет больше
      // нигде в палитре (что вообще доступно из @hermes/plugin-sdk).
      jsx14("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#K1 Available SDK APIs:" }),
      jsxs14("code", { className: "block px-2 py-1 rounded-md bg-muted/40 text-[0.55rem] font-mono overflow-x-auto select-none whitespace-pre", style: SELECTABLE, children: [
        "#K2 atom, useValue, host.onEvent,",
        "\n",
        "StatusDot, ScrollArea, CopyButton,",
        "\n",
        "EmptyState"
      ] })
    ]
  }));
}

// desktop/src/palette/layout.js
import { jsx as jsx15, jsxs as jsxs15 } from "react/jsx-runtime";
var SAMPLE_BODY_TEXT = [
  "\u25CB\u25CB\u25CB",
  "plugin.js: 6",
  "1026|    style: { fontSize: '0.7rem' },",
  "1027|    children: [",
  "1028|      jsx('div', { className: 'text-[0.6rem] text-(--ui-text-quaternary) mb-0.5', children: '#60 Semantic tokens (from theme)' }),",
  "1029|      jsxs('div', { className: 'grid grid-cols-3 gap-1', children: [",
  "\u21921030|        jsxColorSwatch('#61 primary', 'bg-primary'),",
  "",
  "//\u2693x52 Line: 997",
  "#\u2693x9 Line: 42",
  "",
  "<!-- File: /workspace/SRC/hermes-filetools/desktop/Recon__Ui-hermes-plugin.md -->",
  "<!-- Size: 220 lines, 11963 bytes, 11845 chars, LF, max/avg line 274/52.8 -->",
  "<!-- Modified: 2026-08-30 18:44 -->",
  "<!-- Encoding: utf-8 -->",
  "<!-- FileTag: x0a -->",
  "<!-- \u25CB\u25CB\u25CB -->",
  "<!-- \u25A0BLOCK x0a : 1-220 -->",
  "# Hermes Desktop Plugin \u2014 UI & Tailwind Investigation Notes",
  "",
  "Date: 2026-08-28  ",
  "Plugin: hermes-filetools/desktop/plugin.js ",
  "",
  "<!-- \u2693x0a Line: 111 -->",
  "<!-- \u25A0END x0a : 220 -->"
].join("\n");
function LayoutSection() {
  return paletteTile("Layout Compositions", jsxs15("div", {
    className: "flex flex-col gap-1.5",
    style: { fontSize: "0.7rem" },
    children: [
      jsx15("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#L1 \u041F\u0440\u043E\u0442\u043E\u0442\u0438\u043F \u0441\u0442\u0440\u043E\u043A\u0438 \u043E\u043F\u0435\u0440\u0430\u0446\u0438\u0438 \u2014 \u043F\u043E\u0442\u044F\u043D\u0438 \u0437\u0430 \u043F\u0440\u0430\u0432\u044B\u0439 \u043D\u0438\u0436\u043D\u0438\u0439 \u0443\u0433\u043E\u043B \u0431\u043B\u043E\u043A\u0430 \u043D\u0438\u0436\u0435" }),
      jsx15("div", {
        style: {
          resize: "horizontal",
          overflow: "hidden",
          width: "380px",
          minWidth: "150px",
          maxWidth: "100%",
          border: "1px dashed var(--ui-stroke-tertiary)",
          padding: "6px",
          display: "flex",
          flexDirection: "column",
          gap: "4px"
        },
        children: [
          jsx15("div", { className: "text-[0.55rem] mb-0.5", style: { color: "var(--ui-text-quaternary)" }, children: "#L2 StaleStack: 3 \u044D\u043B\u0435\u043C\u0435\u043D\u0442\u0430, \u0437\u043D\u0430\u0447\u043A\u0438 \u0432\u043C\u0435\u0441\u0442\u043E \u0442\u043E\u0447\u0435\u043A (\u2713/\u26A0/\u21BB)" }),
          jsx15(OperationCardHeader, {
            statuses: mockStatuses("patch"),
            icon: "\u0394",
            tool: "patch",
            added: 8,
            removed: 120,
            fileName: "core_reader_and_writer_utils.js",
            filePath: "desktop/src/palette/components/deeply/nested/example/core.js",
            command: "npm run build -- --watch --config production.settings.json",
            drawerOpen: true,
            opNumber: "op:56AB",
            durationText: "2.0s",
            tag: "xAB",
            showCheckboxes: false,
            staleItems: [{ label: "\u0443\u0441\u0442\u0430\u0440\u0435\u043B\u043E", glyph: "\u2713" }, { label: "\u0443\u0441\u0442\u0430\u0440\u0435\u043B\u043E 2", glyph: "\u26A0" }, { label: "\u0443\u0441\u0442\u0430\u0440\u0435\u043B\u043E 3", glyph: "\u21BB" }]
          }),
          jsx15("div", { className: "text-[0.55rem] mb-0.5 mt-1.5", style: { color: "var(--ui-text-quaternary)" }, children: "#L3 StaleStack: 1 \u044D\u043B\u0435\u043C\u0435\u043D\u0442 (\u0442\u043E\u0447\u043A\u0430, \u043A\u0430\u043A \u0440\u0430\u043D\u044C\u0448\u0435)" }),
          jsx15(OperationCardHeader, {
            statuses: mockStatuses("execute_code"),
            icon: "\u25A4",
            tool: "execute_code",
            added: 0,
            removed: 0,
            fileName: null,
            filePath: null,
            command: "npm run build -- --watch --config prod.settings.local.json",
            drawerOpen: true,
            opNumber: "op:12F0",
            durationText: "0.9s",
            staleItems: [{ label: "\u0443\u0441\u0442\u0430\u0440\u0435\u043B\u043E" }]
          }),
          jsx15("div", { className: "text-[0.55rem] mb-0.5 mt-1.5", style: { color: "var(--ui-text-quaternary)" }, children: "#L4 StaleStack: 2 \u044D\u043B\u0435\u043C\u0435\u043D\u0442\u0430 (\u0442\u043E\u0447\u043A\u0438)" }),
          jsx15(OperationCardHeader, {
            statuses: mockStatuses("write_file"),
            icon: "\u271A",
            tool: "write_file",
            added: 150,
            removed: 0,
            fileName: "core.js",
            filePath: "desktop/src/palette/components/deeply/nested/example/core.js",
            command: "buildBrick",
            drawerOpen: true,
            opNumber: "op:9D3E",
            durationText: "0.2s",
            staleItems: [{ label: "\u0443\u0441\u0442\u0430\u0440\u0435\u043B\u043E" }, { label: "\u0443\u0441\u0442\u0430\u0440\u0435\u043B\u043E 2" }]
          }),
          jsx15("div", { className: "text-[0.55rem] mb-0.5 mt-1.5", style: { color: "var(--ui-text-quaternary)" }, children: "#L7 \u0412\u044B\u043A\u0438\u043D\u0443\u0442\u043E \u0438\u0437 \u043A\u043E\u043D\u0442\u0435\u043A\u0441\u0442\u0430 \u2014 \u0432\u0441\u0451 \u043A\u0430\u043A \u0431\u044B\u043B\u043E, \u043F\u0440\u0438\u0442\u0443\u0448\u0435\u043D\u043E, \u043D\u0435 \u043A\u043B\u0438\u043A\u0430\u0435\u0442\u0441\u044F" }),
          jsx15(OperationCardHeader, {
            statuses: mockStatuses("patch"),
            icon: "\u0394",
            tool: "patch",
            added: 8,
            removed: 120,
            fileName: "core.js",
            filePath: "desktop/src/palette/components/deeply/nested/example/core.js",
            command: null,
            drawerOpen: false,
            opNumber: "op:56AB",
            durationText: "2.0s",
            disabled: true
          })
        ]
      }),
      jsx15("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-2 pt-2", style: { borderTop: "1px solid var(--ui-stroke-secondary)" }, children: "#L5 \u0421\u0432\u043E\u0451 \u0442\u0435\u043B\u043E: \u043D\u0430\u0448\u0438 \u043C\u0430\u0440\u043A\u0435\u0440\u044B / \u043A\u043E\u043C\u043C\u0435\u043D\u0442\u0430\u0440\u0438\u0439 / \u043D\u043E\u043C\u0435\u0440 \u0441\u0442\u0440\u043E\u043A\u0438 \u2014 \u0431\u0435\u0437 \u0431\u0438\u0431\u043B\u0438\u043E\u0442\u0435\u043A" }),
      jsx15(BodyPreview, { text: SAMPLE_BODY_TEXT }),
      jsx15("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-2 pt-2", style: { borderTop: "1px solid var(--ui-stroke-secondary)" }, children: "#L6 \u0421\u043E\u0431\u0440\u0430\u043D\u043D\u0430\u044F \u043A\u0430\u0440\u0442\u043E\u0447\u043A\u0430 (OperationCard) \u2014 \u043A\u043B\u0438\u043A \u043F\u043E \u0448\u0430\u043F\u043A\u0435 \u0440\u0430\u0441\u043A\u0440\u044B\u0432\u0430\u0435\u0442 \u043A\u043E\u043C\u0430\u043D\u0434\u0443+\u0440\u0435\u0437\u0443\u043B\u044C\u0442\u0430\u0442" }),
      jsx15(OperationCard, {
        headerProps: {
          statuses: mockStatuses("patch"),
          icon: "\u25E7",
          tool: "patch",
          added: 8,
          removed: 120,
          fileName: "core_reader_and_writer_utils.js",
          filePath: "desktop/src/palette/components/deeply/nested/example/core.js",
          command: null,
          drawerOpen: false,
          opNumber: "op:56AB",
          durationText: "2.0s",
          tag: "a1"
        },
        commandText: "npm run build -- --watch --config production.settings.json",
        result: SAMPLE_BODY_TEXT
      })
    ]
  }));
}

// desktop/src/palette/theme-vars.js
import { jsx as jsx16, jsxs as jsxs16 } from "react/jsx-runtime";
function swatchBg(num, varName, expect) {
  return jsx16("div", {
    className: "h-5 rounded flex items-center justify-center px-1 text-[0.5rem] font-bold truncate",
    style: Object.assign({ backgroundColor: `var(${varName})`, color: "#fcfcfc80" }, SELECTABLE),
    title: `${varName} \u2014 \u043E\u0436\u0438\u0434\u0430\u043D\u0438\u0435: ${expect}`,
    children: `#M${num} ${varName}`
  });
}
function swatchText(num, varName, expect) {
  return jsx16("div", {
    className: "text-[0.6rem] font-bold truncate select-none",
    style: Object.assign({ color: `var(${varName})` }, SELECTABLE),
    title: `${varName} \u2014 \u043E\u0436\u0438\u0434\u0430\u043D\u0438\u0435: ${expect}`,
    children: `#M${num} ${varName}`
  });
}
function groupLabel(text) {
  return jsx16("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: text });
}
function ThemeVarsSection() {
  return paletteTile("Theme Vars (audit)", jsxs16("div", {
    className: "flex flex-col gap-1",
    style: { fontSize: "0.7rem" },
    children: [
      groupLabel("#M1-5 \u0417\u0430\u043B\u0438\u0432\u043A\u0438 \u2014 \u0441\u0435\u043C\u0430\u043D\u0442\u0438\u043A\u0430"),
      jsxs16("div", { className: "grid grid-cols-5 gap-1", children: [
        swatchBg(1, "--dt-primary", "#a7c080"),
        swatchBg(2, "--dt-secondary", "#4f5c4e"),
        swatchBg(3, "--dt-accent", "#434e47"),
        swatchBg(4, "--dt-destructive", "#da6362"),
        swatchBg(5, "--ui-success", "#41b770")
      ] }),
      groupLabel("#M17-21 \u0417\u0430\u043B\u0438\u0432\u043A\u0438 \u2014 \u0444\u043E\u043D\u044B \u043F\u0430\u043D\u0435\u043B\u0435\u0439"),
      jsxs16("div", { className: "grid grid-cols-5 gap-1", children: [
        swatchBg(17, "--ui-bg-primary", "accent 16%"),
        swatchBg(18, "--ui-bg-secondary", "accent 11%"),
        swatchBg(19, "--ui-bg-tertiary", "accent 8%"),
        swatchBg(20, "--ui-bg-card", "accent 4%"),
        swatchBg(21, "--ui-bg-elevated", "elevated-seed")
      ] }),
      groupLabel("#M10-16 UI-\u043F\u0430\u043B\u0438\u0442\u0440\u0430 (\u0444\u0438\u043A\u0441\u0438\u0440\u043E\u0432\u0430\u043D\u043D\u044B\u0435 hex-\u043F\u0435\u0440\u0435\u043C\u0435\u043D\u043D\u044B\u0435)"),
      jsxs16("div", { className: "grid grid-cols-7 gap-1", children: [
        swatchBg(10, "--ui-red", "#e75e78"),
        swatchBg(11, "--ui-green", "#55a583"),
        swatchBg(12, "--ui-cyan", "#6f9ba6"),
        swatchBg(13, "--ui-orange", "#db704b"),
        swatchBg(14, "--ui-yellow", "#c08532"),
        swatchBg(15, "--ui-blue", "#0053fd"),
        swatchBg(16, "--ui-purple", "#9e94d5")
      ] }),
      groupLabel("#M6-9 Foreground-\u043F\u0430\u0440\u044B (\u0446\u0432\u0435\u0442 \u0422\u0415\u041A\u0421\u0422\u0410 \u0434\u043B\u044F \u0437\u0430\u043B\u0438\u0432\u043E\u043A \u0432\u044B\u0448\u0435)"),
      jsxs16("div", { className: "grid grid-cols-2 gap-x-2", children: [
        swatchText(6, "--dt-primary-foreground", "#ffffff"),
        swatchText(7, "--dt-secondary-foreground", "#d3c6aa"),
        swatchText(8, "--dt-accent-foreground", "#d3c6aa"),
        swatchText(9, "--dt-destructive-foreground", "#ffffff")
      ] }),
      // Градиент по смыслу (94% → 36% непрозрачности base) — в один ряд, чтобы затухание было видно сразу.
      groupLabel("#M22-25 \u0422\u0435\u043A\u0441\u0442 \u2014 \u0438\u0435\u0440\u0430\u0440\u0445\u0438\u044F (\u0433\u0440\u0430\u0434\u0438\u0435\u043D\u0442 \u043F\u0440\u043E\u0437\u0440\u0430\u0447\u043D\u043E\u0441\u0442\u0438 base)"),
      jsxs16("div", { className: "flex items-baseline gap-2", children: [
        swatchText(22, "--ui-text-primary", "94% base"),
        swatchText(23, "--ui-text-secondary", "74% base"),
        swatchText(24, "--ui-text-tertiary", "54% base"),
        swatchText(25, "--ui-text-quaternary", "36% base")
      ] }),
      // Diff — border+background СМЫСЛОВАЯ пара, одна плашка на пару (как в #B16), не 4 отдельных.
      groupLabel("#M26-27 Diff (\u0440\u0430\u043C\u043A\u0430+\u0444\u043E\u043D \u043E\u0434\u043D\u043E\u0439 \u043F\u043B\u0430\u0448\u043A\u043E\u0439)"),
      jsxs16("div", { className: "grid grid-cols-2 gap-1", children: [
        jsx16("div", {
          className: "h-5 rounded border text-[0.5rem] flex items-center justify-center font-bold",
          style: { borderColor: "var(--ui-diff-add-border)", backgroundColor: "var(--ui-diff-add-background)", color: "var(--ui-diff-add-border)" },
          title: "--ui-diff-add-border/-background \u2014 \u043E\u0436\u0438\u0434\u0430\u043D\u0438\u0435: green / green 12%",
          children: "M26 add"
        }),
        jsx16("div", {
          className: "h-5 rounded border text-[0.5rem] flex items-center justify-center font-bold",
          style: { borderColor: "var(--ui-diff-remove-border)", backgroundColor: "var(--ui-diff-remove-background)", color: "var(--ui-diff-remove-border)" },
          title: "--ui-diff-remove-border/-background \u2014 \u043E\u0436\u0438\u0434\u0430\u043D\u0438\u0435: red / red 12%",
          children: "M27 remove"
        })
      ] }),
      // Inline code — та же пара bg+fg, тоже одной плашкой (см. #D12).
      groupLabel("#M28 Inline code (\u0444\u043E\u043D+\u0442\u0435\u043A\u0441\u0442 \u043E\u0434\u043D\u043E\u0439 \u043F\u043B\u0430\u0448\u043A\u043E\u0439)"),
      jsx16("div", {
        className: "inline-flex px-1 rounded text-[0.6rem] font-bold",
        style: { backgroundColor: "var(--ui-inline-code-background)", color: "var(--ui-inline-code-foreground)" },
        title: "--ui-inline-code-background/-foreground \u2014 \u043E\u0436\u0438\u0434\u0430\u043D\u0438\u0435: #ffffff12 / #ffffffe0",
        children: "M28 --ui-inline-code-*"
      }),
      groupLabel("#M29 \u041B\u0435\u0433\u0435\u043D\u0434\u0430\u0440\u043D\u0430\u044F \u043F\u0430\u043C\u044F\u0442\u044C tool"),
      swatchBg(29, "--tool-memory-legendary-icon", "yellow\xD7purple mix"),
      // Перенесено сюда 2026-09-02 из #G8-10 (RCS-сдвиги) и #E13-15 (color-mix) — оба метода
      // получения ПРОИЗВОДНОГО цвета от var(--dt-primary), тематически одно и то же умение.
      groupLabel("#M30-32 OKLCH Relative Color Syntax \u2014 \u043F\u0440\u043E\u0438\u0437\u0432\u043E\u0434\u043D\u044B\u0435 \u043E\u0442 var(--dt-primary)"),
      jsxs16("div", { className: "grid grid-cols-3 gap-1", children: [
        jsx16("div", {
          className: "h-5 rounded flex items-center justify-center text-[0.45rem] font-bold truncate",
          style: { backgroundColor: "oklch(from var(--dt-primary) l c h)", color: "#fcfcfc80" },
          title: "oklch(from var(--dt-primary) l c h) \u2014 \u0442\u043E\u0447\u043D\u043E\u0435 \u0437\u0435\u0440\u043D\u043E \u043F\u0435\u0440\u0435\u043C\u0435\u043D\u043D\u043E\u0439",
          children: "#M30 base"
        }),
        jsx16("div", {
          className: "h-5 rounded flex items-center justify-center text-[0.45rem] font-bold truncate",
          style: { backgroundColor: "oklch(from var(--dt-primary) l c calc(h + 20))", color: "#fcfcfc80" },
          title: "oklch(from var(--dt-primary) l c calc(h + 20)) \u2014 hue +20",
          children: "#M31 hue+20"
        }),
        jsx16("div", {
          className: "h-5 rounded flex items-center justify-center text-[0.45rem] font-bold truncate",
          style: { backgroundColor: "oklch(from var(--dt-primary) calc(l - 0.3) c h)", color: "#fcfcfc80" },
          title: "oklch(from var(--dt-primary) calc(l - 0.3) c h) \u2014 lightness -0.3",
          children: "#M32 light-0.3"
        })
      ] }),
      groupLabel("#M33-35 color-mix(in oklch, #e75e78, var(--dt-primary) N%)"),
      jsxs16("div", { className: "grid grid-cols-3 gap-1", children: [
        jsx16("div", {
          className: "h-5 rounded flex items-center justify-center text-[0.45rem] font-bold truncate",
          style: { backgroundColor: "color-mix(in oklch, #e75e78, var(--dt-primary) 25%)", color: "#fcfcfc80" },
          title: "color-mix(in oklch, #e75e78, var(--dt-primary) 25%)",
          children: "#M33 25%"
        }),
        jsx16("div", {
          className: "h-5 rounded flex items-center justify-center text-[0.45rem] font-bold truncate",
          style: { backgroundColor: "color-mix(in oklch, #e75e78, var(--dt-primary) 50%)", color: "#fcfcfc80" },
          title: "color-mix(in oklch, #e75e78, var(--dt-primary) 50%)",
          children: "#M34 50%"
        }),
        jsx16("div", {
          className: "h-5 rounded flex items-center justify-center text-[0.45rem] font-bold truncate",
          style: { backgroundColor: "color-mix(in oklch, #e75e78, var(--dt-primary) 75%)", color: "#fcfcfc80" },
          title: "color-mix(in oklch, #e75e78, var(--dt-primary) 75%)",
          children: "#M35 75%"
        })
      ] }),
      // Перенесено сюда 2026-09-02 из #F (Interactive Opacity Test Stand) — сам вопрос
      // "работает ли прозрачность фона" закрыт, оставлен один пример на ЖИВОЙ переменной
      // (var(--ui-orange), не хардкод-hex, каким был тест).
      groupLabel("#M36 Opacity via color-mix (\u043D\u0430 \u0436\u0438\u0432\u043E\u0439 var(--ui-orange), \u043D\u0435 \u043A\u043E\u043D\u0441\u0442\u0430\u043D\u0442\u0435)"),
      jsxs16("div", { className: "grid grid-cols-4 gap-1", children: [
        jsx16("div", { className: "h-5 rounded border text-[0.45rem] flex items-center justify-center", style: { backgroundColor: "color-mix(in oklch, var(--ui-orange), transparent 90%)" }, children: "10%" }),
        jsx16("div", { className: "h-5 rounded border text-[0.45rem] flex items-center justify-center", style: { backgroundColor: "color-mix(in oklch, var(--ui-orange), transparent 70%)" }, children: "30%" }),
        jsx16("div", { className: "h-5 rounded border text-[0.45rem] flex items-center justify-center", style: { backgroundColor: "color-mix(in oklch, var(--ui-orange), transparent 50%)" }, children: "50%" }),
        jsx16("div", { className: "h-5 rounded border text-[0.45rem] flex items-center justify-center", style: { backgroundColor: "color-mix(in oklch, var(--ui-orange), transparent 20%)" }, children: "80%" })
      ] })
    ]
  }));
}

// desktop/src/palette/forms.js
import { jsx as jsx17, jsxs as jsxs17 } from "react/jsx-runtime";
import { useState as useState6 } from "react";
function TextInputDemo() {
  return jsx17("input", {
    type: "text",
    defaultValue: "",
    placeholder: "#N1 text input",
    className: "w-full px-2 py-1 rounded-md text-[0.65rem] outline-none transition-colors duration-100 ease-out",
    // БЫЛ на var(--ui-bg-input) — константа #fcfcfc в самой теме, без тёмной пары, не
    // инвертируется со сменой темы. var(--ui-bg-elevated) — то же, что уже работает в #B3.
    style: { backgroundColor: "var(--ui-bg-elevated)", border: "1px solid var(--ui-stroke-secondary)", color: "var(--ui-text-primary)" },
    onFocus: (e) => {
      e.currentTarget.style.borderColor = "var(--dt-ring)";
    },
    onBlur: (e) => {
      e.currentTarget.style.borderColor = "var(--ui-stroke-secondary)";
    }
  });
}
function TextAreaDemo() {
  return jsx17("textarea", {
    rows: 3,
    placeholder: "#N5 multiline input",
    className: "w-full px-2 py-1 rounded-md text-[0.65rem] outline-none resize-none transition-colors duration-100 ease-out",
    style: { backgroundColor: "var(--ui-bg-elevated)", border: "1px solid var(--ui-stroke-secondary)", color: "var(--ui-text-primary)" },
    onFocus: (e) => {
      e.currentTarget.style.borderColor = "var(--dt-ring)";
    },
    onBlur: (e) => {
      e.currentTarget.style.borderColor = "var(--ui-stroke-secondary)";
    }
  });
}
function CheckboxDemo() {
  return jsxs17("label", { className: "inline-flex items-center gap-1.5 text-[0.65rem] cursor-pointer select-none", style: SELECTABLE, children: [
    jsx17("input", { type: "checkbox", defaultChecked: true, style: { accentColor: "var(--dt-primary)" } }),
    jsx17("span", { children: "#N2 checkbox" })
  ] });
}
function RadioDemo() {
  return jsxs17("div", { className: "inline-flex items-center gap-3 text-[0.65rem] select-none", style: SELECTABLE, children: [
    jsxs17("label", { className: "inline-flex items-center gap-1 cursor-pointer", children: [
      jsx17("input", { type: "radio", name: "palette-n3-demo", defaultChecked: true, style: { accentColor: "var(--dt-primary)" } }),
      jsx17("span", { children: "a" })
    ] }),
    jsxs17("label", { className: "inline-flex items-center gap-1 cursor-pointer", children: [
      jsx17("input", { type: "radio", name: "palette-n3-demo", style: { accentColor: "var(--dt-primary)" } }),
      jsx17("span", { children: "b" })
    ] }),
    jsx17("span", { className: "text-(--ui-text-quaternary)", children: "#N3 radio" })
  ] });
}
function TooltipDemo() {
  const [open, setOpen] = useState6(false);
  return jsx17("span", {
    className: "relative inline-block",
    onMouseEnter: () => setOpen(true),
    onMouseLeave: () => setOpen(false),
    children: jsxs17("span", { style: { position: "relative" }, children: [
      jsx17("span", {
        key: "trigger",
        className: "text-[0.65rem] cursor-help select-none",
        style: { borderBottom: "1px dotted var(--ui-text-tertiary)", color: "var(--ui-text-secondary)" },
        children: "#N4 hover me (tooltip)"
      }),
      open ? jsx17("div", {
        key: "tooltip",
        className: "absolute z-10 px-2 py-1 rounded-md text-[0.6rem] whitespace-nowrap",
        style: {
          bottom: "100%",
          left: "0",
          marginBottom: "4px",
          backgroundColor: "var(--ui-bg-elevated)",
          border: "1px solid var(--ui-stroke-secondary)",
          color: "var(--ui-text-primary)"
        },
        children: "controlled tooltip \u2014 \u044D\u0442\u043E\u0442 \u0442\u0435\u043A\u0441\u0442 \u0442\u043E\u0447\u043D\u043E \u0438\u0437 \u0441\u043E\u0441\u0442\u043E\u044F\u043D\u0438\u044F, \u043D\u0435 \u0438\u0437 title="
      }) : null
    ] })
  });
}
function FormsSection() {
  return paletteTile("Forms & Tooltip", jsxs17("div", {
    className: "flex flex-col gap-2",
    style: { fontSize: "0.7rem" },
    children: [
      jsx17("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5", children: "#N1 Text input" }),
      jsx17(TextInputDemo, {}),
      jsx17("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#N2-3 Checkbox / radio" }),
      jsxs17("div", { className: "flex items-center gap-3", children: [jsx17(CheckboxDemo, {}), jsx17(RadioDemo, {})] }),
      jsx17("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#N4 Tooltip (controlled)" }),
      jsx17(TooltipDemo, {}),
      jsx17("div", { className: "text-[0.6rem] text-(--ui-text-quaternary) mb-0.5 mt-1", children: "#N5 Multiline input" }),
      jsx17(TextAreaDemo, {})
    ]
  }));
}

// desktop/src/palette/index.js
function paletteTile(title, children) {
  return jsxs18("div", {
    className: "rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-bg-card)",
    style: CAPPED,
    children: [
      jsx18("div", {
        className: "px-2 py-1 border-b border-(--ui-stroke-quaternary) text-[0.6rem] font-semibold uppercase tracking-wide text-(--ui-text-tertiary)",
        style: SELECTABLE,
        children: title
      }),
      jsx18("div", { className: "p-2", children })
    ]
  });
}
function PalettePane() {
  return sdkC(sdk6.ScrollArea, "div", {
    className: "h-full w-full overflow-hidden p-2 gap-1.5",
    style: CAPPED,
    children: jsxs18("div", {
      className: "grid grid-cols-2 gap-1.5",
      style: CAPPED,
      children: [
        jsx18(ButtonsSection, {}),
        jsx18(CardsSection, {}),
        jsx18(BadgesSection, {}),
        jsx18(TypographySection, {}),
        jsx18(ListsSection, {}),
        jsx18(LoadersSection, {}),
        jsx18(InteractiveSection, {}),
        jsx18(DividersSection, {}),
        jsx18(SdkSection, {}),
        jsx18(FormsSection, {}),
        // Layout и Theme Vars на всю ширину
        jsxs18("div", { className: "col-span-2", children: [jsx18(LayoutSection, {})] }),
        jsxs18("div", { className: "col-span-2", children: [jsx18(ThemeVarsSection, {})] })
      ]
    })
  });
}

// desktop/src/prompt-mirror.js
import * as sdk8 from "@hermes/plugin-sdk";
import { jsx as jsx34, jsxs as jsxs31 } from "react/jsx-runtime";
import { useEffect as useEffect5, useLayoutEffect as useLayoutEffect5, useState as useState12, useMemo as useMemo2, useRef as useRef7 } from "react";

// desktop/src/mirror-rail.js
import { jsx as jsx19 } from "react/jsx-runtime";
var ROW_HEIGHT2 = {
  header: "1rem",
  // `opener` = верхний паддинг обёртки (`pt-1`, 0.25rem) + содержимое `brick` (ниже) —
  // ТОЧНО, не с запасом (владелец, 2026-09-15: карточка реплики юзера «обрезалась снизу» —
  // запас в 1.5rem маскировал СИМПТОМ несовпадения, не был его причиной, точный расчёт
  // безопаснее произвольного запаса).
  opener: "1.3rem",
  // `brick` = `lineHeight:1 * fontSize 0.6rem` (0.6rem) + `Chip`'s `py-0.5` (0.25rem) +
  // 1px-бордер сверху/снизу (0.125rem) = 0.975rem, плюс небольшой запас на округление.
  // Раньше это было то же число, но БЕЗ `lineHeight:1` на `Chip` — реальная высота чипа
  // зависела от УНАСЛЕДОВАННОГО межстрочного интервала хоста (не наш контроль), оказывалась
  // БОЛЬШЕ этого числа, и `BrickRow`'s `overflow:hidden` молча резал чип снизу. `lineHeight:
  // 1` на `Chip` (`prompt-mirror.js`) убирает саму неопределённость, а не прячет её запасом.
  brick: "1.05rem",
  step: "0.4rem",
  // Строка, которая слева рендерит НИЧЕГО (владелец, 2026-09-15: «есть состояния когда эта
  // сетка съезжает» — реплика ассистента, которая только зовёт тулы без своего текста,
  // `ReplyBrick` возвращает `null` для неё; рельса раньше всё равно резервировала под неё
  // `ROW_HEIGHT.brick`, реальный след слева был 0 — отсюда и разъезд). `0rem`, а не
  // пропуск строки в списке: список остаётся 1-в-1 с проходом по строкам вагона, а не
  // превращается во ВТОРОЙ условный проход, который рискует разойтись с первым.
  empty: "0rem"
};
var ROW_GAP = "0.125rem";
var RAIL_COL_WIDTH = "1.4rem";
var RAIL_WIDTH = RAIL_COL_WIDTH;
var RAIL_BODY_WIDTH = "2rem";
function buildRailRows(entries) {
  return entries.map((e, i) => {
    const height = ROW_HEIGHT2[e.kind] || ROW_HEIGHT2.brick;
    const gap = e.gapBefore ? ROW_GAP : "0rem";
    const base = {
      rowIndex: i,
      height,
      gap,
      pickable: !!e.pickable,
      tone: e.tone || null,
      msgAddr: e.msgAddr || "",
      result: !!e.result
    };
    return e.pickable ? Object.assign(base, { op: e.op, toolId: e.toolId || "", stepKey: e.stepKey }) : base;
  });
}
var SEGMENT_GUIDE = "color-mix(in oklch, var(--ui-stroke-secondary), transparent 92%)";
function selectionRailChrome({ key, column, rowSpan, fill, headHeight, onClose, capChildren, markedCount }) {
  const body = jsx19("div", {
    // Метка для замера: крестик в шапке ставится над ИЗМЕРЕННЫМ телом (`prompt-mirror.js`).
    "data-rail-body": key,
    className: "rounded-md border border-(--ui-stroke-secondary)",
    style: {
      gridColumn: column,
      gridRow: `1 / ${rowSpan + 1}`,
      alignSelf: "stretch",
      justifySelf: "stretch",
      width: "100%",
      boxSizing: "border-box",
      position: "relative",
      zIndex: 0,
      pointerEvents: "none",
      backgroundColor: fill
    }
  }, `${key}-body`);
  const cap = jsx19("div", {
    style: {
      gridColumn: column,
      gridRow: `1 / ${rowSpan + 1}`,
      alignSelf: "stretch",
      justifySelf: "stretch",
      position: "relative",
      zIndex: 3,
      minHeight: 0,
      pointerEvents: "none"
    },
    children: jsx19("div", {
      style: {
        position: "sticky",
        top: 0,
        pointerEvents: "auto",
        display: "flex",
        gap: "2px",
        width: "100%",
        height: ROW_HEIGHT2.header,
        marginBottom: `-${ROW_HEIGHT2.header}`
      },
      children: capChildren
    })
  }, `${key}-cap`);
  const close = jsx19("button", {
    type: "button",
    className: "mirror-action-btn rounded-md border border-(--ui-stroke-secondary)",
    title: "remove operation",
    onClick: (e) => {
      e.stopPropagation();
      onClose();
    },
    style: {
      gridColumn: column,
      gridRow: 1,
      zIndex: 4,
      alignSelf: "stretch",
      justifySelf: "stretch",
      width: "100%",
      height: headHeight,
      boxSizing: "border-box",
      padding: 0,
      cursor: "pointer",
      display: "flex",
      flexDirection: "column",
      justifyContent: "space-between",
      paddingInline: "3px",
      lineHeight: 1,
      color: "var(--ui-text-secondary)",
      backgroundColor: fill
    },
    // КРЕСТИК — ГЛАВНОЕ, число — справка (владелец, 2026-09-29: «информация — это крестик: эту
    // штуку можно закрыть; число — так, для справки»). Высота прежняя, две строки, но ПО
    // ДИАГОНАЛИ, как дробь: крестик слева вверху, число справа внизу — столбиком по центру они
    // читались одним значком. Кнопка — вся зона, ховер на всей зоне: целиться в крестик не надо.
    children: [
      // `lineHeight: 0.7`: у глифа × свои пустоты сверху и снизу, строку можно ужать без обрезки.
      // Иначе 0.8rem + 0.55rem = ровно высота кнопки, и зазору под числом взяться неоткуда.
      jsx19("span", { style: {
        alignSelf: "flex-start",
        fontSize: "0.8rem",
        lineHeight: 0.7,
        marginTop: "2px",
        color: "var(--ui-text-primary)"
      }, children: "\xD7" }, `${key}-x`),
      jsx19("span", {
        // 2px над нижней рамкой (владелец, 2026-09-29): вплотную число ложилось на границу.
        style: {
          alignSelf: "flex-end",
          fontSize: "0.55rem",
          lineHeight: 1,
          marginBottom: "2px",
          color: "var(--ui-text-quaternary)"
        },
        children: String(markedCount || 0)
      }, `${key}-n`)
    ]
  }, `${key}-close`);
  return { body, cap, close };
}
function Rail({ railRows, cells: cells2, guide, gridRow, gridColumn }) {
  const rowsTemplate = railRows.map((r) => `calc(${r.height} + ${r.gap})`).join(" ");
  return jsx19("div", {
    style: {
      display: "grid",
      gridTemplateRows: rowsTemplate,
      width: RAIL_WIDTH,
      flexShrink: 0,
      boxSizing: "border-box",
      gridRow,
      gridColumn,
      position: guide ? "relative" : void 0,
      zIndex: guide ? 1 : void 0,
      justifySelf: guide ? "center" : void 0,
      outline: guide ? `1px solid ${SEGMENT_GUIDE}` : void 0,
      outlineOffset: guide ? "4px" : void 0
    },
    children: cells2
  });
}
var OPENER_CELL = "1.18rem";
function cellSide(r) {
  return r.height === ROW_HEIGHT2.opener ? OPENER_CELL : r.height;
}
function activeAddresses(railRows, isActive) {
  const on = isActive || ((r) => !!r.msgAddr);
  const ids = [];
  railRows.forEach((r) => {
    if (r.height === ROW_HEIGHT2.empty) return;
    if (r.height === ROW_HEIGHT2.header && !r.msgAddr) return;
    if (!r.msgAddr || !on(r)) return;
    if (ids.indexOf(r.msgAddr) < 0) ids.push(r.msgAddr);
  });
  return ids;
}
function markedAddresses(picked) {
  return Object.keys(picked || {});
}
function baseCells(railRows, picked, drag, paint) {
  const marks = picked || {};
  const onDown = drag && drag.onDown;
  const onEnter = drag && drag.onEnter;
  const border = paint && paint.border || "var(--ui-stroke-secondary)";
  const fill = paint && paint.fill || border;
  const isActive = paint && paint.active || ((r) => !!r.msgAddr);
  return railRows.filter((r) => r.height !== ROW_HEIGHT2.empty && !(r.height === ROW_HEIGHT2.header && !r.msgAddr)).map((r) => {
    const active = !!(r.msgAddr && isActive(r));
    const selected = active && Object.prototype.hasOwnProperty.call(marks, r.msgAddr);
    const wash = r.tone ? `color-mix(in oklch, ${r.tone}, transparent 85%)` : "transparent";
    return jsx19("span", {
      onMouseDown: active ? (e) => {
        e.stopPropagation();
        onDown(r.msgAddr);
      } : void 0,
      // КЛИК тоже гасится, не только нажатие: на документе висят слушатели «клик мимо —
      // выйти из режима» (`file-ops-pane.js` для `drop`), и без этого первая же отметка на
      // рельсе `drop content` снимала режим (владелец, 2026-09-29).
      onClick: (e) => e.stopPropagation(),
      onMouseEnter: active ? () => onEnter(r.msgAddr) : void 0,
      title: active ? `${selected ? "selected" : "empty"} ${r.msgAddr}` : "not selectable",
      style: {
        gridRow: r.rowIndex + 1,
        alignSelf: "end",
        justifySelf: "center",
        width: cellSide(r),
        height: cellSide(r),
        boxSizing: "border-box",
        borderRadius: "0.1rem",
        cursor: active ? "pointer" : "default",
        border: active ? `1px solid ${border}` : "1px solid transparent",
        backgroundColor: selected ? fill : wash
      }
    }, `${r.rowIndex}`);
  });
}

// desktop/src/mirror-primitives.js
import { jsx as jsx21, jsxs as jsxs20 } from "react/jsx-runtime";
import { useEffect as useEffect2, useState as useState7, useRef as useRef3, useLayoutEffect as useLayoutEffect2 } from "react";

// desktop/src/mirror-op-chip.js
import { jsx as jsx20, jsxs as jsxs19 } from "react/jsx-runtime";
var CHIP_PAD_LEFT = "0.5rem";
var ICON_COL_WIDTH = "1.2rem";
var NAME_COL_WIDTH = "13ch";
var LEAD_GAP = "0.3rem";
var MARK_SLOT_WIDTH = `calc(${CHIP_PAD_LEFT} + ${ICON_COL_WIDTH})`;
var LEAD_WIDTH = `calc(${MARK_SLOT_WIDTH} + ${NAME_COL_WIDTH} + ${LEAD_GAP})`;
var OP_COL_WIDTH = "4ch";
var UNASSIGNED_OP = "0000";
var UNASSIGNED_OP_OPACITY = 0.4;
var OVERLAP_COL_WIDTH = "4.2ch";
var CLIP = { overflow: "hidden", whiteSpace: "nowrap" };
var OP_CHIP_GAP = "0.25rem";
var DIFF_FIELD_ON = false;
var DIFF_PAD = "0.1rem";
var DIFF_PLUS_W = "4.2ch";
var DIFF_MINUS_W = "3.8ch";
function diffWidth(diff) {
  return `calc(${diff && diff.added ? DIFF_PLUS_W : "0px"} + ${diff && diff.removed ? DIFF_MINUS_W : "0px"})`;
}
function DiffPill({ tone, text, width }) {
  return jsx20("span", {
    style: {
      display: "flex",
      justifyContent: "center",
      overflow: "hidden",
      boxSizing: "border-box",
      width,
      padding: `0 ${DIFF_PAD}`,
      backgroundColor: DIFF_FIELD_ON ? `color-mix(in oklch, ${tone}, transparent 85%)` : "transparent",
      // Цвет темы может нести альфу (`--ui-green` полупрозрачный) — это и был «прозрачный шрифт»:
      // `/ 1` снимает её; насыщенность (`c`) чуть выше, яркость и толщину не трогаем.
      color: `oklch(from ${tone} l calc(c * 1.15) h / 1)`
    },
    children: jsx20("span", { style: { flexShrink: 0, whiteSpace: "nowrap" }, children: text })
  });
}
function OpChipBody({ marks, name, data, diff, op, overlapText, overlapTone, overlapTitle }) {
  const list = Array.isArray(marks) ? marks : [];
  const leadGroup = jsxs19("span", {
    // Диф встаёт в ту же ширину: группа ужимается на его ширину, имя файла остаётся на месте.
    style: Object.assign({
      display: "flex",
      alignItems: "center",
      flexShrink: 0,
      columnGap: "0.15rem",
      width: diff && (diff.added || diff.removed) ? `calc(${LEAD_WIDTH} - ${diffWidth(diff)})` : LEAD_WIDTH
    }, CLIP),
    children: [
      list.length ? jsx20("span", {
        title: list.map((m) => `${m.glyph}  \u2014  ${m.title}`).join("\n"),
        style: Object.assign({ flexShrink: 0, width: MARK_SLOT_WIDTH, display: "flex", justifyContent: "flex-end" }, CLIP),
        children: list.map((m, i) => jsx20("span", { style: { color: m.tone || "inherit" }, children: m.glyph }, i))
      }, "marks") : jsx20("span", {
        title: name,
        style: { flexShrink: 0, width: MARK_SLOT_WIDTH, paddingLeft: CHIP_PAD_LEFT, boxSizing: "border-box", textAlign: "center" },
        children: toolIcon(name)
      }, "icon"),
      // Имя — остаток группы (`flex:1`). Колонка значков постоянная, поэтому имя от статусов
      // больше не сжимается и не едет; `data` снаружи группы тоже стоит на месте.
      jsx20("span", {
        style: Object.assign({ flex: "1 1 auto", minWidth: 0 }, CLIP),
        children: name
      })
    ]
  });
  return jsxs19("span", {
    style: Object.assign({ display: "flex", alignItems: "center", width: "100%", columnGap: OP_CHIP_GAP }, CLIP),
    children: [
      leadGroup,
      // Диф `+N -M` — ПЕРЕД именем файла, имя файла НЕ двигает (владелец, 2026-10-05): ширина
      // дифа вычтена из группы слева (имя операции ужимается, не налезает), а отрицательный margin
      // гасит лишний columnGap — у операций с дифом и без него имя файла стоит в одной точке.
      // Только у операций, которые диф знают; у прочих узла нет вовсе.
      diff && (diff.added || diff.removed) ? jsxs19("span", {
        style: {
          display: "flex",
          flexShrink: 0,
          marginLeft: `calc(-1 * ${OP_CHIP_GAP})`,
          borderRadius: "0.2rem",
          overflow: "hidden",
          width: diffWidth(diff),
          fontVariantNumeric: "tabular-nums"
        },
        children: [
          diff.added ? jsx20(DiffPill, { tone: "var(--ui-green)", text: `+${diff.added}`, width: DIFF_PLUS_W }) : null,
          diff.removed ? jsx20(DiffPill, { tone: "var(--ui-red)", text: `-${diff.removed}`, width: DIFF_MINUS_W }) : null
        ]
      }) : null,
      jsx20("span", {
        style: Object.assign({ flex: "1 1 auto", minWidth: 0, opacity: 0.75 }, CLIP),
        children: data || ""
      }),
      jsx20("span", {
        style: Object.assign({
          flexShrink: 0,
          width: OP_COL_WIDTH,
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
          opacity: op === UNASSIGNED_OP ? UNASSIGNED_OP_OPACITY : 1
        }, CLIP),
        children: op || ""
      }),
      jsx20("span", {
        title: overlapTitle,
        style: Object.assign({
          flexShrink: 0,
          width: OVERLAP_COL_WIDTH,
          textAlign: "right",
          fontVariantNumeric: "tabular-nums",
          color: overlapTone || "inherit"
        }, CLIP),
        children: overlapText || ""
      })
    ]
  });
}

// desktop/src/mirror-primitives.js
function mixTone(baseVar, pct) {
  return `color-mix(in oklch, var(${baseVar}), var(--dt-primary) ${pct}%)`;
}
function opaque(varName) {
  return `oklch(from var(${varName}) l c h / 1)`;
}
var SURFACE_BASE = "--ui-editor-surface-background";
var WORKAREA_BG = `color-mix(in oklch, ${opaque(SURFACE_BASE)}, ${opaque("--ui-bg-elevated")} 50%)`;
var VAGON_BG = `color-mix(in oklch, ${opaque(SURFACE_BASE)}, ${opaque("--tool-memory-legendary-icon")} 3%)`;
var ROLE_TONE = {
  user: mixTone("--ui-blue", 30),
  assistant: mixTone("--ui-green", 30),
  tool: mixTone("--ui-cyan", 30),
  system: mixTone("--ui-yellow", 30)
};
function toneOf(role) {
  return ROLE_TONE[String(role || "")] || "var(--ui-text-quaternary)";
}
var DISTILL_RE = /FT-DISTILL(-END)?(?:\/1|_v1)/;
var DISTILL_HEAD_CHARS = 120;
var BRIDGE_MARK = "\u25C6bridge-message\u25C6";
var OLD_BRIDGE_HEADS = ["[system \u2014 context compaction:"];
function firstBracket(head) {
  const close = head.indexOf("]");
  return /^<?\[/.test(head) && close > 0 ? head.slice(0, close + 1) : head.slice(0, DISTILL_HEAD_CHARS);
}
var SUMMARY_MARK = "\u25C6FT-SUMMARY_v1\u25C6";
var SUMMARY_VOICES = ["[CONTEXT SUMMARISATION:", "[context summarisation:", "[LLM OPS:"];
function distillKind(text) {
  const head = String(text || "").trimStart();
  if (SUMMARY_VOICES.some((v) => head.startsWith(v))) return "summary";
  if (firstBracket(head).includes(SUMMARY_MARK)) return "summary";
  if (OLD_BRIDGE_HEADS.some((h) => head.startsWith(h))) return "bridge";
  const first = firstBracket(head);
  if (first.includes(BRIDGE_MARK)) return "bridge";
  const m = first.match(DISTILL_RE);
  if (!m) return "";
  return m[1] ? "close" : "open";
}
function rowTone(fact) {
  const f = fact || {};
  if (f.distill === "summary" || distillKind(f.text) === "summary") return ACTION_COLOR.llmops;
  return f.distill || distillKind(f.text) ? ACTION_COLOR.compact : toneOf(f.role);
}
function roughTokens(chars) {
  return Math.ceil(Number(chars || 0) / 4);
}
function factTokens(fact) {
  const f = fact || {};
  return typeof f.tokens === "number" && isFinite(f.tokens) ? f.tokens : roughTokens(f.chars);
}
function chipTokens(fact) {
  const f = fact || {};
  const own = factTokens(f);
  if (typeof f.call_tokens === "number" && isFinite(f.call_tokens)) return own + f.call_tokens;
  if (typeof f.prose_tokens === "number" && isFinite(f.prose_tokens)) return f.prose_tokens;
  return own;
}
function briefPreview(text, maxChars) {
  const s = String(text || "");
  return s.length > maxChars ? s.slice(0, maxChars) + "\u2026" : s;
}
var TOKEN_COL_WIDTH = "1.1rem";
var CHIP_WIDTH = "17rem";
var CHIP_HOVER_BRIGHTNESS = 1.3;
var CHIP_OPEN_FILL_BRIGHTNESS = CHIP_HOVER_BRIGHTNESS;
var CHIP_OPEN_TEXT_BRIGHTNESS = CHIP_HOVER_BRIGHTNESS;
var CHIP_OPEN_RING_TRANSPARENT_PCT = 30;
function brighten(color, factor) {
  return `oklch(from ${color} calc(l * ${factor}) c h)`;
}
function Chip({ tone, border, open, flushLeft, onClick, onMouseEnter, onMouseLeave, children }) {
  return jsx21("span", {
    className: "mirror-chip relative inline-flex items-center px-2 py-0.5 rounded-xl font-medium cursor-pointer" + (open ? " mirror-chip-open" : ""),
    style: Object.assign({
      width: CHIP_WIDTH,
      fontSize: "0.6rem",
      // `lineHeight: 1` (Таск 10a, живая правка) — без него высота чипа зависит от
      // УНАСЛЕДОВАННОГО межстрочного интервала хоста, который мы не знаем заранее и не
      // контролируем; `ROW_HEIGHT.brick` (mirror-rail.js) посчитан ИМЕННО под `lineHeight:
      // 1`, и `BrickRow` теперь режет строку по этой высоте (`overflow:hidden`) — если
      // реальная высота чипа окажется больше расчётной (как было до этой правки, с
      // унаследованным интервалом), чип обрезается снизу молча. Владелец поймала это на
      // карточке-реплике юзера («обрезалась снизу») сразу после первого внедрения трека.
      lineHeight: 1,
      backgroundColor: `color-mix(in oklch, ${open ? brighten(tone, CHIP_OPEN_FILL_BRIGHTNESS) : tone}, transparent 85%)`,
      color: open ? brighten(tone, CHIP_OPEN_TEXT_BRIGHTNESS) : tone,
      boxShadow: open ? `inset 0 0 0 1px color-mix(in oklch, ${tone}, transparent ${CHIP_OPEN_RING_TRANSPARENT_PCT}%)` : "none",
      border: border ? "1px solid var(--ui-stroke-secondary)" : "1px solid transparent",
      whiteSpace: "nowrap",
      overflow: "hidden",
      textOverflow: "ellipsis"
    }, flushLeft ? { paddingLeft: 0 } : null, SELECTABLE),
    onClick,
    onMouseEnter,
    onMouseLeave,
    children: jsx21("span", { style: { overflow: "hidden", textOverflow: "ellipsis", width: "100%" }, children }, "label")
  });
}
var CHIP_FONT = "0.6rem";
function BrickHead({ left, opLabel, addrLabel, addrTitle, height }) {
  return jsxs20("div", {
    style: {
      display: "grid",
      gridTemplateColumns: `${TOKEN_COL_WIDTH} ${CHIP_WIDTH} minmax(0, auto)`,
      alignItems: "center",
      columnGap: "0.375rem",
      minWidth: 0,
      overflow: "hidden",
      height,
      boxSizing: "border-box",
      fontSize: "0.5rem",
      lineHeight: 1
    },
    children: [
      jsx21("span", {
        className: "text-(--ui-text-quaternary)",
        style: Object.assign({ gridColumn: "1 / 3", gridRow: 1, whiteSpace: "nowrap", overflow: "hidden" }, SELECTABLE),
        children: left
      }, "left"),
      jsx21("span", {
        style: {
          gridColumn: 2,
          gridRow: 1,
          display: "flex",
          justifyContent: "flex-end",
          fontSize: CHIP_FONT,
          // `+ 0.4rem` — живая подгонка владельца (2026-09-29: «левее на 1.5 буквы»).
          padding: `0 calc(0.5rem + 1px + ${OVERLAP_COL_WIDTH} + ${OP_CHIP_GAP} + 0.4rem) 0 0`,
          pointerEvents: "none"
        },
        children: jsx21("span", {
          className: "text-(--ui-text-quaternary)",
          style: { width: OP_COL_WIDTH, textAlign: "right", whiteSpace: "nowrap", fontSize: "0.5rem", opacity: 0.7 },
          title: "the number of our operation \u2014 what the model sees as Operation ID",
          children: opLabel
        })
      }, "op"),
      jsxs20("span", {
        style: { gridColumn: 3, gridRow: 1, display: "flex", flexWrap: "wrap", height: "1em", overflow: "hidden", minWidth: 0 },
        children: [
          jsx21("span", { style: { width: 0 } }, "pad"),
          jsx21("span", {
            className: "text-(--ui-text-quaternary)",
            style: Object.assign({ whiteSpace: "nowrap", opacity: 0.7 }, SELECTABLE),
            title: addrTitle,
            children: addrLabel
          }, "addr")
        ]
      }, "addr")
    ]
  });
}
function BrickRow({ tokens, chip, rest }) {
  return jsxs20("div", {
    style: {
      display: "grid",
      gridTemplateColumns: `${TOKEN_COL_WIDTH} ${CHIP_WIDTH} auto`,
      alignItems: "center",
      columnGap: "0.375rem",
      minWidth: 0,
      overflow: "hidden",
      height: ROW_HEIGHT2.brick,
      boxSizing: "border-box"
    },
    children: [
      jsx21("span", {
        className: "text-(--ui-text-quaternary)",
        // `lineHeight: 1` — та же причина, что у `Chip`: строка вырезана по авторитетной
        // высоте (`overflow:hidden` выше), унаследованный межстрочный интервал не должен
        // решать, влезет ли содержимое.
        style: Object.assign({ fontVariantNumeric: "tabular-nums", fontSize: "0.55rem", lineHeight: 1, textAlign: "right", overflow: "hidden" }, SELECTABLE),
        children: tokens
      }),
      chip,
      jsxs20("span", { className: "flex items-center gap-1 min-w-0", style: { lineHeight: 1 }, children: rest || [] })
    ]
  });
}
var PANE_MIN_LINES = 5;
var PANE_WHOLE_MAX_SHARE = 60;
var PANE_LINE_REM = 0.94;
var PANE_PAD_REM = 0.5;
var PANE_CHARS_PER_LINE = 70;
function paneMinRem(chars) {
  const size = Math.max(0, Number(chars) || 0);
  const lines = Math.max(1, Math.ceil(size / PANE_CHARS_PER_LINE));
  return Math.min(PANE_MIN_LINES, lines) * PANE_LINE_REM + PANE_PAD_REM;
}
function paneSplit(topChars, bottomChars) {
  const top = Math.max(0, Number(topChars) || 0);
  return {
    top: top === 0 ? null : { minRem: paneMinRem(top) },
    // Пустой результат секцию НЕ отменяет: «ответ пуст» — это факт, а не нечего показать.
    bottom: { minRem: paneMinRem(bottomChars) }
  };
}
function Pane({ minRem, whole, children }) {
  return jsx21("div", {
    className: "mirror-pane-scroll overflow-auto px-2 py-1",
    style: whole ? {
      flexGrow: 0,
      flexShrink: 1,
      flexBasis: "auto",
      minHeight: `${minRem}rem`,
      maxHeight: `${PANE_WHOLE_MAX_SHARE}%`
    } : { flexGrow: 0, flexShrink: 1, flexBasis: "auto", minHeight: `${minRem}rem` },
    children
  });
}
function TwoPanes({ top, bottom, topChars, bottomChars, topWhole }) {
  const split = paneSplit(top ? topChars === void 0 ? 1 : topChars : 0, bottomChars);
  const parts = [];
  if (split.top) {
    parts.push(jsx21(Pane, { minRem: split.top.minRem, whole: topWhole, children: top }, "call"));
    parts.push(jsx21("div", {
      className: "shrink-0",
      style: { height: "4px", backgroundColor: "var(--ui-stroke-secondary)", opacity: 0.9 }
    }, "split"));
  }
  parts.push(topWhole ? jsx21("div", {
    className: "mirror-pane-scroll overflow-auto px-2 py-1 grow",
    style: { minHeight: `${split.bottom.minRem}rem` },
    children: bottom
  }, "result") : jsx21(Pane, { minRem: split.bottom.minRem, children: bottom }, "result"));
  return jsxs20("div", { className: "flex flex-col min-h-0 grow", children: parts }, "panes");
}
var HOVER_DELAY_MS = 200;
var FLYOUT_GAP_PX = 6;
var FLYOUT_BOTTOM_REM = 5;
var FLYOUT_LOWER_QUARTER = 0.3;
var EDGE_PAD_PX = 4;
var TOP_PAD_FACTOR = 5;
var TALL_WAIT_MS = 500;
var TALL_TRIES = 3;
function tallBox(cur, short, viewH, pad2) {
  const box = cur && typeof cur === "object" ? cur : {};
  const top0 = Math.max(0, Number(box.top) || 0);
  const bottom0 = Math.max(top0, Number(box.bottom) || 0);
  const height0 = bottom0 - top0;
  const missing = Math.max(0, Number(short) || 0);
  if (missing <= 0 || height0 <= 0) return null;
  const topPad = pad2 * TOP_PAD_FACTOR;
  const want = height0 + missing;
  const top = Math.min(top0, Math.max(topPad, bottom0 - want));
  const bottom = Math.min(viewH - pad2, Math.max(bottom0, top + want));
  return bottom - top > height0 ? { top: Math.round(top), height: Math.round(bottom - top) } : null;
}
function flyoutBox(rect, widthRem, fill, tall, viewH, viewW, capPx) {
  if (!rect) return { display: "none" };
  const right = Math.max(EDGE_PAD_PX, viewW - rect.left + FLYOUT_GAP_PX);
  if (tall && tall.height > 0) {
    return { position: "fixed", top: tall.top, right, width: widthRem, maxHeight: `${tall.height}px` };
  }
  const top = Math.max(EDGE_PAD_PX, rect.top);
  const chipBottom = rect.bottom == null ? top : rect.bottom;
  const mid = rect.bottom == null ? top : (Number(rect.top) + Number(rect.bottom)) / 2;
  const inLowerQuarter = mid >= viewH * (1 - FLYOUT_LOWER_QUARTER);
  if (fill && inLowerQuarter) {
    const anchor = Math.min(Math.max(chipBottom, EDGE_PAD_PX), viewH - EDGE_PAD_PX);
    const room = Math.round(anchor - EDGE_PAD_PX);
    const maxH = capPx > 0 ? Math.min(room, Math.round(capPx)) : room;
    return {
      position: "fixed",
      bottom: viewH - anchor,
      right,
      width: widthRem,
      maxHeight: `${maxH}px`
    };
  }
  return {
    position: "fixed",
    top,
    right,
    width: widthRem,
    // Считается ОТ `top`, а не задаётся процентом вроде `90vh`: фиксированный процент у чипа
    // внизу ленты увёл бы нижний край за пределы экрана — молча и необратимо.
    maxHeight: fill ? `calc(100vh - ${top}px - ${FLYOUT_BOTTOM_REM}rem)` : void 0
  };
}
function leftFlyoutStyle(rect, widthRem, fill, tall, capPx) {
  return flyoutBox(rect, widthRem, fill, tall, window.innerHeight, window.innerWidth, capPx);
}
function safePadPx() {
  const root = typeof document !== "undefined" && document.documentElement;
  const size = root ? parseFloat(getComputedStyle(root).fontSize) : 16;
  return FLYOUT_BOTTOM_REM * (size > 0 ? size : 16);
}
function hiddenBelow(el) {
  if (!el || typeof el.querySelectorAll !== "function") return 0;
  let short = 0;
  for (const node of el.querySelectorAll("*")) {
    const over = (node.scrollHeight || 0) - (node.clientHeight || 0);
    if (over > 1) short += over;
  }
  return short;
}
var MESSAGE_DETAIL = "message_detail";
function ChipTrigger({ id, popup, setPopup, tone, label, tip, border, flushLeft, renderPopup }) {
  const open = !!popup && popup.id === id;
  const tier = open ? popup.tier : "summary";
  const [hover, setHover] = useState7(false);
  const hoverTimer = useRef3(null);
  const triggerRef = useRef3(null);
  const [hoverRect, setHoverRect] = useState7(null);
  const [popupRect, setPopupRect] = useState7(null);
  useLayoutEffect2(() => {
    if (hover && triggerRef.current) setHoverRect(triggerRef.current.getBoundingClientRect());
  }, [hover]);
  const [raw, setRaw] = useState7(false);
  const [tall, setTall] = useState7(null);
  const [gripped, setGripped] = useState7(false);
  const [restHeight, setRestHeight] = useState7(null);
  const popupRef = useRef3(null);
  const tallTimer = useRef3(null);
  useLayoutEffect2(() => {
    if (open && triggerRef.current) setPopupRect(triggerRef.current.getBoundingClientRect());
    if (open) {
      setRaw(false);
      setTall(null);
      setGripped(false);
      setRestHeight(null);
    }
    if (!open && tallTimer.current) {
      clearTimeout(tallTimer.current);
      tallTimer.current = null;
    }
  }, [open]);
  const expandLater = (tries) => {
    if (tallTimer.current) clearTimeout(tallTimer.current);
    tallTimer.current = setTimeout(() => {
      tallTimer.current = null;
      const el = popupRef.current;
      if (!el) return;
      const next = tallBox(
        el.getBoundingClientRect(),
        hiddenBelow(el),
        window.innerHeight,
        safePadPx()
      );
      if (next) setTall(next);
      else if (tries > 1) expandLater(tries - 1);
    }, TALL_WAIT_MS);
  };
  useLayoutEffect2(() => {
    const root = popupRef.current;
    if (!root || typeof root.querySelectorAll !== "function") return;
    const kick = () => {
      for (const node of root.querySelectorAll(".mirror-pane-scroll")) {
        const y = node.scrollTop || 0;
        node.scrollTop = y + 1;
        node.scrollTop = y;
      }
    };
    kick();
    const id2 = requestAnimationFrame(kick);
    return () => cancelAnimationFrame(id2);
  }, [tall, gripped, tier]);
  useEffect2(() => () => {
    if (tallTimer.current) clearTimeout(tallTimer.current);
  }, []);
  useEffect2(() => () => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
  }, []);
  return jsxs20("span", {
    ref: triggerRef,
    // Якорь для перехода к чипу снаружи (список операций файла, 2026-10-14): `prompt-mirror.js::goToOp`.
    "data-chip-id": id,
    className: "inline-flex",
    style: { minWidth: 0 },
    children: [
      jsx21(Chip, {
        tone,
        border,
        open,
        flushLeft,
        onMouseEnter: () => {
          hoverTimer.current = setTimeout(() => setHover(true), HOVER_DELAY_MS);
        },
        onMouseLeave: () => {
          if (hoverTimer.current) clearTimeout(hoverTimer.current);
          setHover(false);
        },
        onClick: (e) => {
          e.stopPropagation();
          setPopup(open ? null : { id, tier: "summary" });
        },
        children: label
      }),
      hover && !open ? jsx21("div", {
        className: "rounded-md p-1.5 whitespace-pre-wrap pointer-events-none",
        style: Object.assign({
          maxWidth: "70vw",
          maxHeight: "50vh",
          overflow: "auto",
          fontSize: "0.6rem",
          whiteSpace: "pre-wrap",
          backgroundColor: "var(--ui-bg-elevated)",
          border: "1px solid var(--ui-stroke-secondary)",
          boxShadow: "0 4px 12px rgba(0,0,0,0.35)",
          zIndex: 50
        }, leftFlyoutStyle(hoverRect, "20rem"), SELECTABLE),
        children: tip
      }) : null,
      open ? jsx21("div", {
        // ОДИНАРНЫЙ клик разворачивает до полного текста (владелец, 2026-09-23).
        // Был двойной — жест, который в списке никто не находит: он не подсказывается
        // ничем, кроме строчки внизу, и её ещё надо прочитать.
        //
        // Выделение текста при этом не отнимаем: если в окне что-то выделено, клик
        // считается концом выделения, а не запросом развернуть. Промоушен всё равно
        // одноразовый (`summary` → `full`), так что цена ошибки — один лишний разворот.
        onClick: (e) => {
          e.stopPropagation();
          if (tier !== "summary") return;
          const sel = typeof window !== "undefined" && window.getSelection ? String(window.getSelection() || "") : "";
          if (sel) return;
          setPopup({ id, tier: "full" });
        },
        // ДВА БЛОКА, А НЕ ОДИН СКРОЛЛЕР (владелец, 2026-09-23): шапка — заголовок,
        // кнопки и строка состояния — прижата к верху и не едет, текст проскроливается
        // ПОД ней. Поэтому `overflow` снят с самого окна и отдан телу, а `renderPopup`
        // отдаёт теперь `{head, body}` вместо одного дерева: разделить их снаружи
        // невозможно, а `position: sticky` внутри общего скроллера пришлось бы
        // подпирать отрицательным `top` из-за паддинга окна — тот же результат, но
        // фокусом вместо устройства.
        ref: popupRef,
        className: "rounded-md cursor-pointer flex flex-col",
        style: Object.assign({
          maxWidth: "80vw",
          maxHeight: "50vh",
          overflow: "hidden",
          fontSize: "0.65rem",
          backgroundColor: "var(--ui-bg-elevated)",
          border: "1px solid var(--ui-stroke-secondary)",
          boxShadow: "0 4px 12px rgba(0,0,0,0.35)",
          zIndex: 51
        }, leftFlyoutStyle(popupRect, "30rem", true, tall, gripped ? 0 : restHeight), SELECTABLE),
        children: (() => {
          const parts = renderPopup(tier, {
            raw,
            toggleRaw: () => setRaw((on) => !on),
            tall: !!tall
          }) || {};
          const grown = !!(tall || gripped);
          const onGrip = (e) => {
            e.stopPropagation();
            if (grown) {
              setTall(null);
              setGripped(false);
              return;
            }
            const h = popupRef.current ? popupRef.current.getBoundingClientRect().height : 0;
            if (h > 0) setRestHeight(Math.round(h));
            setGripped(true);
            if (tier === "summary") setPopup({ id, tier: "full" });
            expandLater(TALL_TRIES);
          };
          const grip = (edge) => {
            const named = edge === "top" && grown;
            return jsx21("div", {
              className: "shrink-0",
              title: grown ? "collapse back to the chip" : "expand upward",
              // Курсор — ИНЛАЙНОМ. Классом `cursor-n-resize` он не работает: наш бандл в
              // сканирование Tailwind не попадает, и класс, которого нет в их исходниках,
              // молча выбрасывается (правило в шапке `core.js`). Без курсора кромка
              // кликабельна, но об этом никто не узнает.
              style: {
                height: "6px",
                cursor: edge === "bottom" ? "s-resize" : "n-resize",
                backgroundColor: grown ? "var(--ui-stroke-secondary)" : "transparent",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                overflow: "hidden",
                fontSize: "0.45rem",
                fontWeight: 300,
                letterSpacing: "0.06em",
                lineHeight: 1,
                color: "var(--ui-text-primary)",
                userSelect: "none"
              },
              onClick: onGrip,
              children: named ? jsx21("span", { style: { transform: "translateY(-1px)" }, children: MESSAGE_DETAIL }, "name") : null
            }, edge);
          };
          return [
            // ВЕРХНЯЯ КРОМКА — ручка разворота (владелец, 2026-09-23: «можно на верхней
            // границе окна невидимую, просто утолщив границу чуток»). Кнопкой в шапке это
            // отняло бы место у заголовка ради жеста, которым пользуются раз в сессию.
            // `stopPropagation` обязателен: клик по окну иначе промотирует его в `full`.
            grip("top"),
            jsx21("div", {
              className: "shrink-0 px-2 pt-2 pb-1",
              style: { borderBottom: "1px solid var(--ui-stroke-quaternary)" },
              children: parts.head
            }, "head"),
            // Прокрутка НЕ здесь: её держат сами секции (`TwoPanes`), иначе, читая
            // длинный результат, ты увозил бы вызов за верхний край окна.
            jsx21("div", {
              className: "min-h-0 grow flex flex-col whitespace-pre-wrap pt-1",
              children: parts.body
            }, "body"),
            // ПОДВАЛ — вне скролла, как и шапка (владелец, 2026-09-23: подсказка «прибита
            // к тексту и уезжает под срез вместе со скроллом»). Она про ОКНО, а не про
            // текст в нём, и место ей у края окна.
            parts.foot ? jsx21("div", {
              className: "shrink-0 px-2 pt-1 pb-1 text-center text-(--ui-text-quaternary)",
              style: {
                fontSize: "0.55rem",
                borderTop: "1px solid var(--ui-stroke-quaternary)"
              },
              children: parts.foot
            }, "foot") : null,
            grip("bottom")
          ];
        })()
      }) : null
    ]
  });
}

// desktop/src/mirror-bricks.js
import { jsx as jsx26, jsxs as jsxs23 } from "react/jsx-runtime";
import { useEffect as useEffect3, useRef as useRef4, useState as useState8 } from "react";

// desktop/src/call-render.js
import { jsx as jsx22, jsxs as jsxs21 } from "react/jsx-runtime";
var GRID_MAX = 21;
var BODY_KEYS = ["content", "code", "text", "body", "patch", "old_string", "new_string", "pattern"];
function orderedKeys(args, call) {
  const keys = Object.keys(args);
  if (!call || !call.params.length) return keys;
  const known = call.params.map((p) => p.name).filter((k2) => keys.includes(k2));
  return [...known, ...keys.filter((k2) => !known.includes(k2))];
}
var KEY_CH = 11;
var CELL_CH = 32;
var CELL_LINE = "color-mix(in oklch, var(--ui-stroke-secondary), transparent 65%)";
function callValue(value) {
  if (typeof value === "string") return value;
  if (value === null || value === void 0) return "";
  try {
    if (typeof value === "object") return realNewlines(JSON.stringify(value, null, 2));
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
function realNewlines(text) {
  return String(text).replace(/(\\+)n/g, (m, slashes) => slashes.length % 2 === 1 ? slashes.slice(1) + "\n" : m);
}
function unfoldValue(value) {
  if (typeof value !== "string") return value;
  const t = value.trim();
  if (t.startsWith("{") && t.endsWith("}") || t.startsWith("[") && t.endsWith("]")) {
    try {
      const parsed = JSON.parse(t);
      if (parsed && typeof parsed === "object") return parsed;
    } catch {
    }
  }
  if (!value.includes("\n") && (value.match(/\\n/g) || []).length >= 2) {
    return value.replace(/\\r\\n/g, "\n").replace(/\\n/g, "\n").replace(/\\t/g, "	");
  }
  return value;
}
var NEST_MAX = 3;
function isPlainObject(v) {
  return !!v && typeof v === "object" && !Array.isArray(v);
}
function nestedOf(value, depth, limit) {
  if (depth >= NEST_MAX) return null;
  if (isPlainObject(value) && Object.keys(value).length > 0) {
    return [{ label: "", rows: callRows(value, limit, depth + 1) }];
  }
  if (Array.isArray(value) && value.length > 0 && value.every(isPlainObject)) {
    return value.map((item, i) => ({
      label: value.length > 1 ? `[${i}]` : "",
      rows: callRows(item, limit, depth + 1)
    }));
  }
  return null;
}
function callRows(args, limit, depth = 0, tool = "") {
  const cut = (text) => limit > 0 && text.length > limit ? text.slice(0, limit) + " \u2026" : text;
  if (typeof args === "string") {
    return args ? [{ key: "", value: cut(args), block: true }] : [];
  }
  if (!args || typeof args !== "object") return [];
  const call = depth === 0 && tool ? toolCall(tool) : null;
  const bodyKeys = call ? call.body : BODY_KEYS;
  return orderedKeys(args, call).map((key) => {
    const raw = unfoldValue(args[key]);
    const nested = nestedOf(raw, depth, limit);
    if (nested) return { key, value: "", block: true, nested };
    const value = cut(callValue(raw));
    const many = value.includes("\n");
    const long = value.length > GRID_MAX || bodyKeys.includes(key.toLowerCase());
    return { key, value, block: many, wide: !many && long };
  });
}
function splitFields(rows) {
  const list = Array.isArray(rows) ? rows : [];
  return {
    grid: list.filter((row) => !row.block && !row.wide && row.key),
    // Средний ярус: длинное, но однострочное. Идёт той же сеткой, но во всю её ширину — пара
    // остаётся парой, просто занимает строку целиком.
    wide: list.filter((row) => row.wide && row.key),
    // Низ — ВСЁ многострочное, а не одно самое большое: у чужого тула таких полей запросто два,
    // и второе утонуло бы выше. Сюда же безымянное (битый JSON): ключа нет, в сетку такое
    // ставить не на что.
    body: list.filter((row) => row.block || !row.key)
  };
}
function Key({ text, width }) {
  return jsx22("span", {
    className: "shrink-0 text-(--ui-text-quaternary)",
    style: Object.assign(
      { fontSize: "0.6rem" },
      // Ширина задаётся только в сетке: там ключи обязаны стоять столбцом. У блока ключ один на
      // всю ширину, и резервировать под него полосу незачем.
      width ? { width: `${KEY_CH}ch`, overflow: "hidden", textOverflow: "ellipsis" } : null,
      SELECTABLE
    ),
    children: text
  });
}
function Value({ text, tone }) {
  return jsx22("div", {
    style: Object.assign(
      // `lineHeight` здесь свой, а не наследуемый: у многострочного значения общий межстрочный
      // интервал панели даёт разрежённую простыню, которую неудобно читать (владелец, 2026-09-23).
      { whiteSpace: "pre-wrap", overflowWrap: "anywhere", lineHeight: 1.3, minWidth: 0 },
      tone ? { color: tone } : null,
      SELECTABLE
    ),
    children: text
  });
}
function FieldGrid({ rows, wide }) {
  const list = Array.isArray(rows) ? rows : [];
  const wideList = Array.isArray(wide) ? wide : [];
  if (list.length === 0 && wideList.length === 0) return null;
  return jsx22("div", {
    style: { overflow: "hidden" },
    children: jsx22("div", {
      style: {
        display: "grid",
        marginLeft: "-1px",
        marginBottom: "-1px",
        // `auto-fill` + фиксированная ячейка + `justifyContent: start`: колонок столько, сколько
        // влезло, и они прижаты влево. `1fr` растягивал бы их по всей ширине — при двух полях
        // второе уезжало к правому краю.
        gridTemplateColumns: `repeat(auto-fill, ${CELL_CH}ch)`,
        justifyContent: "start",
        // ЗАЗОРОВ НЕТ — «лист в клеточку» (владелец, 2026-09-23). Линия между двумя соседями
        // должна быть ОДНА, поэтому ячейка рисует только два ребра из четырёх: рисуй она все,
        // на каждом стыке было бы по две линии, и сетка выглядела бы вдвое жирнее внутри.
        columnGap: 0,
        rowGap: 0
      },
      children: list.map((row, i) => jsxs21("div", {
        className: "flex items-baseline gap-1.5 min-w-0",
        // Еле заметная линия сетки (владелец, 2026-09-23). Смысл не в украшении: без неё глаз не
        // видит, где кончается одно значение и начинается соседний ключ — колонки держатся только
        // на выравнивании, а оно ломается, едва значение перенеслось на вторую строку.
        style: {
          borderLeft: `1px solid ${CELL_LINE}`,
          borderBottom: `1px solid ${CELL_LINE}`,
          padding: "0.05rem 0.25rem"
        },
        children: [jsx22(Key, { text: row.key, width: true }, "k"), jsx22(Value, { text: row.value }, "v")]
      }, `g${i}`)).concat(wideList.map((row, i) => jsxs21("div", {
        className: "flex items-baseline gap-1.5 min-w-0",
        // `1 / -1` — во всю ширину сетки. Ряд той же сетки, а не отдельный блок: ключи остаются в
        // одном столбце с ячейками выше, и таблица читается как одна таблица.
        style: {
          gridColumn: "1 / -1",
          borderLeft: `1px solid ${CELL_LINE}`,
          borderBottom: `1px solid ${CELL_LINE}`,
          padding: "0.05rem 0.25rem"
        },
        children: [jsx22(Key, { text: row.key, width: true }, "k"), jsx22(Value, { text: row.value }, "v")]
      }, `w${i}`)))
    }, "cells")
  });
}
function FieldBlocks({ rows }) {
  const list = Array.isArray(rows) ? rows : [];
  return list.map((row, i) => jsxs21("div", {
    children: [
      row.key ? jsx22(Key, { text: row.key }, "k") : null,
      row.nested ? jsx22("div", { className: "pl-2", style: { paddingLeft: "0.5rem" }, children: row.nested.map((part, j) => jsx22(NestedTable, { part }, `n${j}`)) }, "nested") : jsx22("div", { className: row.key ? "pl-2" : "", children: jsx22(Value, { text: row.value, tone: row.tone }) }, "body")
    ]
  }, `b${i}`));
}
function NestedTable({ part }) {
  const { grid, wide, body } = splitFields(part.rows);
  return jsxs21("div", {
    className: "flex flex-col gap-1",
    style: { borderLeft: `1px solid ${CELL_LINE}`, paddingLeft: "0.4rem", marginBottom: "0.25rem" },
    children: [
      part.label ? jsx22(Key, { text: part.label }, "label") : null,
      jsx22(FieldGrid, { rows: grid, wide }, "grid"),
      ...FieldBlocks({ rows: body })
    ]
  });
}
var NAME_CH = 14;
var DIFF_MAX_CELLS = 4e6;
function lineDiff(oldText, newText) {
  const a = String(oldText ?? "") === "" ? [] : String(oldText).split("\n");
  const b = String(newText ?? "") === "" ? [] : String(newText).split("\n");
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (tail < a.length - head && tail < b.length - head && a[a.length - 1 - tail] === b[b.length - 1 - tail]) tail += 1;
  const x = a.slice(head, a.length - tail);
  const y = b.slice(head, b.length - tail);
  if ((x.length + 1) * (y.length + 1) > DIFF_MAX_CELLS) return null;
  const w = y.length + 1;
  const dp = new Uint32Array((x.length + 1) * w);
  for (let i2 = x.length - 1; i2 >= 0; i2 -= 1) {
    for (let j2 = y.length - 1; j2 >= 0; j2 -= 1) {
      dp[i2 * w + j2] = x[i2] === y[j2] ? dp[(i2 + 1) * w + j2 + 1] + 1 : Math.max(dp[(i2 + 1) * w + j2], dp[i2 * w + j2 + 1]);
    }
  }
  const out = a.slice(0, head).map((text) => ({ op: " ", text }));
  let i = 0;
  let j = 0;
  while (i < x.length || j < y.length) {
    if (i < x.length && j < y.length && x[i] === y[j]) {
      out.push({ op: " ", text: x[i] });
      i += 1;
      j += 1;
    } else if (j >= y.length || i < x.length && dp[(i + 1) * w + j] >= dp[i * w + j + 1]) {
      out.push({ op: "-", text: x[i] });
      i += 1;
    } else {
      out.push({ op: "+", text: y[j] });
      j += 1;
    }
  }
  return out.concat(a.slice(a.length - tail).map((text) => ({ op: " ", text })));
}
var DIFF_TONE = {
  "-": "color-mix(in oklch, var(--ui-red), transparent 82%)",
  "+": "color-mix(in oklch, var(--ui-green), transparent 82%)"
};
function DiffView({ lines }) {
  const minus = lines.filter((l) => l.op === "-").length;
  const plus = lines.filter((l) => l.op === "+").length;
  return jsxs21("div", {
    children: [
      jsxs21("div", { className: "flex items-baseline gap-2", children: [
        jsx22(Key, { text: "old_string \u2192 new_string" }, "k"),
        jsx22("span", { style: { fontSize: "0.55rem", color: "var(--ui-text-quaternary)" }, children: `\u2212${minus} +${plus}` }, "n")
      ] }, "head"),
      jsx22("div", {
        className: "font-mono",
        style: Object.assign({ lineHeight: 1.3, minWidth: 0 }, SELECTABLE),
        children: lines.map((l, k2) => jsxs21("div", {
          style: { display: "flex", background: DIFF_TONE[l.op] || "transparent" },
          children: [
            // Знак — отдельной колонкой и невыделяемым: скопированный текст остаётся кодом.
            jsx22("span", { style: {
              width: "1.2em",
              flexShrink: 0,
              textAlign: "center",
              userSelect: "none",
              color: "var(--ui-text-quaternary)"
            }, children: l.op === " " ? "" : l.op === "-" ? "\u2212" : "+" }, "s"),
            jsx22("span", { style: { whiteSpace: "pre-wrap", overflowWrap: "anywhere", minWidth: 0 }, children: l.text || " " }, "t")
          ]
        }, k2))
      }, "lines")
    ]
  });
}
function patchDiff(name, args, limit, cut) {
  if (cut || name !== "patch" || !args || typeof args !== "object") return null;
  const oldText = args.old_string;
  const newText = args.new_string;
  if (typeof oldText !== "string" || typeof newText !== "string") return null;
  if (limit > 0 && (oldText.length > limit || newText.length > limit)) return null;
  return lineDiff(oldText, newText);
}
function CallView({ name, args, limit, cut }) {
  const diff = patchDiff(name, args, limit, cut);
  const rows = callRows(args, limit, 0, name);
  const { grid, wide, body } = splitFields(diff ? rows.filter((r) => r.key !== "old_string" && r.key !== "new_string") : rows);
  const head = name ? jsx22("div", {
    style: Object.assign({ fontWeight: 500 }, SELECTABLE),
    children: name
  }, "name") : jsx22("div", {
    className: "text-(--ui-text-quaternary)",
    style: { fontSize: "0.6rem" },
    children: "call with no name"
  }, "noname");
  return jsxs21("div", {
    className: "flex flex-col gap-1",
    children: [
      jsxs21("div", {
        style: { display: "grid", gridTemplateColumns: `${NAME_CH}ch minmax(0, 1fr)`, columnGap: "0.5rem" },
        children: [
          head,
          jsx22("div", { className: "min-w-0", children: jsx22(FieldGrid, { rows: grid, wide }) }, "table")
        ]
      }, "top"),
      ...FieldBlocks({ rows: body }),
      diff ? jsx22(DiffView, { lines: diff }, "diff") : null
    ]
  });
}

// desktop/src/result-render.js
import { jsx as jsx23, jsxs as jsxs22 } from "react/jsx-runtime";
var HEADER_PAIR_RE = /^([A-Za-z][A-Za-z0-9_]*(?: [A-Za-z][A-Za-z0-9_]*)?):\s*(.*)$/;
function unwrap(line) {
  const fallback = String(formatVocab().comment_fallback || "");
  let text = String(line).replace(/^\s*(?:<!--\s*)?(?:\/\/\s*|#\s*)?/, "");
  if (fallback && text.startsWith(fallback)) text = text.slice(fallback.length).replace(/^\s*/, "");
  return text.replace(/\s*-->\s*$/, "").trimEnd();
}
function parsePreamble(line) {
  const text = String(line || "").trim();
  if (!text.startsWith("{") || !text.endsWith("}")) return null;
  try {
    const value = JSON.parse(text);
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}
function splitResult(text) {
  const whole = typeof text === "string" ? text : "";
  if (whole === "") return { fields: [], notes: [], body: "" };
  const lines = whole.split("\n");
  let at = 0;
  const preamble = parsePreamble(lines[0]);
  const fields = [];
  if (preamble) {
    at = 1;
    for (const key of Object.keys(preamble)) {
      const value = preamble[key];
      fields.push({ key, value: typeof value === "string" ? value : JSON.stringify(value) });
    }
  }
  const sep = separatorRe();
  let end = -1;
  for (let i = at; i < lines.length; i += 1) {
    if (sep.test(lines[i])) {
      end = i;
      break;
    }
  }
  const notes = [];
  if (end >= 0) {
    for (let i = at; i < end; i += 1) {
      const bare = unwrap(lines[i]);
      if (bare === "") continue;
      const m = bare.match(HEADER_PAIR_RE);
      if (m && m[2] !== "") fields.push({ key: m[1], value: m[2] });
      else notes.push(bare);
    }
    at = end + 1;
  }
  return { fields, notes, body: lines.slice(at).join("\n") };
}
function toRows(fields) {
  return fields.map(({ key, value }) => {
    const text = String(value);
    const many = text.includes("\n");
    return { key, value: text, block: many, wide: !many && text.length > GRID_MAX };
  });
}
function patchAddedLines(body, oldText, newText) {
  const marks = /* @__PURE__ */ new Set();
  const neu = String(newText || "");
  if (!neu || !body) return marks;
  const diff = lineDiff(oldText, neu);
  if (!diff) return marks;
  const plus = [];
  let k2 = 0;
  for (const l of diff) {
    if (l.op === "+") plus.push(k2);
    if (l.op !== "-") k2 += 1;
  }
  if (!plus.length) return marks;
  let at = body.indexOf(neu);
  while (at !== -1) {
    const first = body.slice(0, at).split("\n").length - 1;
    for (const p of plus) marks.add(first + p);
    at = body.indexOf(neu, at + neu.length);
  }
  return marks;
}
var ADDED_TONE = "color-mix(in oklch, var(--ui-green), transparent 82%)";
function ResultView({ text, added }) {
  const { fields, notes, body } = splitResult(text);
  const marks = added ? patchAddedLines(body, added.oldText, added.newText) : null;
  const rows = toRows(fields);
  const grid = rows.filter((r) => !r.block && !r.wide);
  const wide = rows.filter((r) => r.wide);
  const blocks2 = rows.filter((r) => r.block);
  const parts = [];
  if (grid.length || wide.length) parts.push(jsx23(FieldGrid, { rows: grid, wide }, "grid"));
  if (blocks2.length) parts.push(...FieldBlocks({ rows: blocks2 }));
  if (notes.length) {
    parts.push(jsx23("div", {
      className: "text-(--ui-text-tertiary)",
      style: Object.assign({ lineHeight: 1.3 }, SELECTABLE),
      children: notes.map((line, i) => jsx23("div", { children: line }, `n${i}`))
    }, "notes"));
  }
  if (body !== "") {
    parts.push(jsx23("div", {
      className: "font-mono",
      style: Object.assign({
        // `pre-wrap`, а НЕ `pre`. Оба сохраняют текст дословно — пробелы, отступы, пустые
        // строки, — но `pre` запрещает перенос, и длинная строка уезжала вправо в
        // горизонтальный скролл (владелец, 2026-09-23). Перенос длинной строки содержимое не
        // меняет: это то, как её показывают, а не то, что в ней написано.
        whiteSpace: "pre-wrap",
        overflowWrap: "anywhere",
        fontSize: "0.65rem",
        lineHeight: 1.35,
        // Воздух между таблицей и содержимым (владелец, 2026-09-23). Это граница двух разных
        // вещей — наших служебных фактов и дословного текста, — и она должна читаться без линии.
        marginTop: "0.35rem",
        "--body-text": "var(--ui-text-primary)",
        "--body-comment": "var(--ui-text-tertiary)",
        "--body-marker": "var(--dt-secondary)",
        "--body-mark": "var(--ui-orange)"
        // Без `PRE`: он задаёт `width: max-content`, то есть растягивает блок по самой длинной
        // строке — с переносом это несовместимо по смыслу.
      }, SELECTABLE),
      children: body.split("\n").map((line, i) => marks && marks.has(i) ? jsx23("div", { style: { background: ADDED_TONE }, children: jsx23(BodyLine, { text: line }) }, `b${i}`) : jsx23(BodyLine, { text: line }, `b${i}`))
    }, "body"));
  }
  if (parts.length === 0) {
    return jsx23("div", {
      className: "text-(--ui-text-quaternary)",
      style: Object.assign({ fontSize: "0.6rem" }, SELECTABLE),
      children: "the tool answered nothing"
    });
  }
  return jsxs22("div", { className: "flex flex-col gap-1", children: parts });
}

// desktop/src/markdown.js
import { jsx as jsx25 } from "react/jsx-runtime";
import * as sdk7 from "@hermes/plugin-sdk";

// desktop/src/md-lite.js
import { jsx as jsx24 } from "react/jsx-runtime";
var FENCE = /^\s*(`{3,}|~{3,})\s*([\w+-]*)/;
var HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
var RULE = /^\s{0,3}([-*_])(\s*\1){2,}\s*$/;
var QUOTE = /^\s{0,3}>\s?/;
var ITEM = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
var TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
var blank = (line) => !line.trim();
function cells(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}
function startsBlock(lines, i) {
  const line = lines[i];
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || ITEM.test(line) || line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]);
}
function parseMd(text) {
  const lines = String(text || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (blank(line)) {
      i += 1;
      continue;
    }
    const fence = line.match(FENCE);
    if (fence) {
      const mark = fence[1];
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(mark)) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      out.push({ type: "code", lang: fence[2] || "", text: body.join("\n") });
      continue;
    }
    const heading = line.match(HEADING);
    if (heading) {
      out.push({ type: "heading", level: heading[1].length, text: heading[2] });
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      out.push({ type: "rule" });
      i += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      const body = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        body.push(lines[i].replace(QUOTE, ""));
        i += 1;
      }
      out.push({ type: "quote", children: parseMd(body.join("\n")) });
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = cells(line);
      const align = cells(lines[i + 1]).map((c) => c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : "left");
      const rows = [];
      i += 2;
      while (i < lines.length && !blank(lines[i]) && lines[i].includes("|")) {
        rows.push(cells(lines[i]));
        i += 1;
      }
      out.push({ type: "table", head, align, rows });
      continue;
    }
    const item = line.match(ITEM);
    if (item) {
      const indent = item[1].length;
      const ordered = /\d/.test(item[2]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(ITEM);
        if (!m || m[1].length !== indent || /\d/.test(m[2]) !== ordered) break;
        const body = [m[3]];
        i += 1;
        while (i < lines.length) {
          const l = lines[i];
          if (blank(l)) {
            const next = lines[i + 1];
            if (next !== void 0 && !blank(next) && next.match(/^\s*/)[0].length > indent) {
              body.push("");
              i += 1;
              continue;
            }
            break;
          }
          const lead = l.match(/^\s*/)[0].length;
          if (lead <= indent) break;
          body.push(l.slice(Math.min(lead, indent + 2)));
          i += 1;
        }
        let first = body[0];
        let checked = null;
        const task = first.match(/^\[([ xX])\]\s+(.*)$/);
        if (task) {
          checked = task[1] !== " ";
          first = task[2];
        }
        items.push({ checked, children: parseMd([first, ...body.slice(1)].join("\n")) });
      }
      out.push({ type: "list", ordered, start: ordered ? parseInt(item[2], 10) : 1, items });
      continue;
    }
    const para = [];
    while (i < lines.length && !blank(lines[i]) && (para.length === 0 || !startsBlock(lines, i))) {
      para.push(lines[i].trim());
      i += 1;
    }
    out.push({ type: "para", text: para.join("\n") });
  }
  return out;
}
var INLINE = /(`+)([\s\S]*?[^`])\1(?!`)|\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|~~([\s\S]+?)~~|\*([^*\s][\s\S]*?)\*|(?<![\w])_([^_\s][\s\S]*?)_(?![\w])/;
function parseInline(text) {
  const out = [];
  let rest = String(text || "");
  while (rest) {
    const m = rest.match(INLINE);
    if (!m) {
      out.push({ type: "text", text: rest });
      break;
    }
    if (m.index) out.push({ type: "text", text: rest.slice(0, m.index) });
    if (m[1]) out.push({ type: "code", text: m[2].replace(/^ (.*) $/, "$1") });
    else if (m[3]) out.push({ type: "link", href: m[4], children: parseInline(m[3]) });
    else if (m[5] || m[6]) out.push({ type: "strong", children: parseInline(m[5] || m[6]) });
    else if (m[7]) out.push({ type: "del", children: parseInline(m[7]) });
    else out.push({ type: "em", children: parseInline(m[8] || m[9]) });
    rest = rest.slice(m.index + m[0].length);
  }
  return out;
}
var MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
var CODE_BG = "color-mix(in oklch, var(--ui-text-primary, currentColor), transparent 92%)";
var LINE = "1px solid var(--ui-stroke-secondary, rgba(128,128,128,0.35))";
var SAFE_HREF = /^(https?:|mailto:)/i;
function inline(nodes) {
  return nodes.map((n2, k2) => {
    if (n2.type === "text") {
      const parts = n2.text.split("\n");
      return parts.length === 1 ? n2.text : parts.flatMap((p, j) => j ? [jsx24("br", {}, `b${k2}-${j}`), p] : [p]);
    }
    if (n2.type === "code") {
      return jsx24("code", { style: { fontFamily: MONO, fontSize: "0.9em", padding: "0 0.25em", borderRadius: "0.25em", background: CODE_BG }, children: n2.text }, k2);
    }
    if (n2.type === "strong") return jsx24("strong", { children: inline(n2.children) }, k2);
    if (n2.type === "em") return jsx24("em", { children: inline(n2.children) }, k2);
    if (n2.type === "del") return jsx24("del", { children: inline(n2.children) }, k2);
    if (n2.type === "link") {
      return SAFE_HREF.test(n2.href) ? jsx24("a", { href: n2.href, target: "_blank", rel: "noopener noreferrer", style: { color: "var(--dt-primary, inherit)", textDecoration: "underline" }, children: inline(n2.children) }, k2) : jsx24("span", { children: inline(n2.children) }, k2);
    }
    return null;
  });
}
var H_SIZE = [1.35, 1.2, 1.1, 1, 0.95, 0.9];
function blocks(list) {
  return list.map((b, k2) => {
    if (b.type === "heading") {
      return jsx24(`h${b.level}`, { style: { fontSize: `${H_SIZE[b.level - 1]}em`, fontWeight: 650, margin: "0.7em 0 0.35em", lineHeight: 1.25 }, children: inline(parseInline(b.text)) }, k2);
    }
    if (b.type === "para") return jsx24("p", { style: { margin: "0 0 0.6em" }, children: inline(parseInline(b.text)) }, k2);
    if (b.type === "rule") return jsx24("hr", { style: { border: "none", borderTop: LINE, margin: "0.8em 0" } }, k2);
    if (b.type === "code") {
      return jsx24("pre", {
        title: b.lang || void 0,
        style: {
          fontFamily: MONO,
          fontSize: "0.88em",
          lineHeight: 1.4,
          margin: "0 0 0.6em",
          padding: "0.5em 0.65em",
          borderRadius: "0.4em",
          background: CODE_BG,
          whiteSpace: "pre-wrap",
          overflowWrap: "anywhere"
        },
        children: b.text
      }, k2);
    }
    if (b.type === "quote") {
      return jsx24("blockquote", { style: { margin: "0 0 0.6em", padding: "0 0 0 0.75em", borderLeft: "3px solid var(--ui-stroke-secondary, rgba(128,128,128,0.5))", opacity: 0.9 }, children: blocks(b.children) }, k2);
    }
    if (b.type === "list") {
      return jsx24(b.ordered ? "ol" : "ul", {
        start: b.ordered && b.start !== 1 ? b.start : void 0,
        style: { margin: "0 0 0.6em", paddingLeft: "1.4em", listStyleType: b.ordered ? "decimal" : "disc" },
        children: b.items.map((it, j) => jsx24("li", {
          style: { margin: "0.15em 0", listStyleType: it.checked === null ? void 0 : "none" },
          children: [
            it.checked === null ? null : jsx24("span", { style: { marginRight: "0.35em", marginLeft: "-1.2em" }, children: it.checked ? "\u2611" : "\u2610" }, "box"),
            // A one-paragraph item stays on the bullet's line (no paragraph margin).
            ...it.children.length === 1 && it.children[0].type === "para" ? [jsx24("span", { children: inline(parseInline(it.children[0].text)) }, "p")] : blocks(it.children)
          ]
        }, j))
      }, k2);
    }
    if (b.type === "table") {
      const cell = (tag, text, j) => jsx24(tag, { style: { border: LINE, padding: "0.2em 0.5em", textAlign: b.align[j] || "left", verticalAlign: "top" }, children: inline(parseInline(text)) }, j);
      return jsx24("div", { style: { overflowX: "auto", margin: "0 0 0.6em" }, children: jsx24("table", {
        style: { borderCollapse: "collapse", fontSize: "0.95em" },
        children: [
          jsx24("thead", { children: jsx24("tr", { style: { fontWeight: 650 }, children: b.head.map((c, j) => cell("th", c, j)) }) }, "h"),
          jsx24("tbody", { children: b.rows.map((r, ri) => jsx24("tr", { children: b.head.map((_, j) => cell("td", r[j] || "", j)) }, ri)) }, "b")
        ]
      }) }, k2);
    }
    return null;
  });
}
function MdLite({ text }) {
  return jsx24("div", { style: { fontSize: "0.875rem", lineHeight: 1.55, overflowWrap: "anywhere" }, children: blocks(parseMd(text)) });
}

// desktop/src/markdown.js
var FORCE_LITE = false;
var MessageText = sdk7.MessageTextContent;
var Streamdown2 = sdk7.Streamdown;
var markdown = FORCE_LITE ? (text) => jsx25(MdLite, { text }) : MessageText ? (text) => jsx25(MessageText, { text: String(text || ""), media: false }) : Streamdown2 ? (text) => jsx25(Streamdown2, { children: String(text || "") }) : (text) => jsx25(MdLite, { text });

// desktop/src/mirror-bricks.js
var MD_ZOOM = 0.85;
function FullText({ address, fallback, cut, raw, md, patch }) {
  const [text, setText] = useState8("");
  const [added, setAdded] = useState8(null);
  useEffect3(() => {
    let alive = true;
    loadMessageText(address).then((value) => {
      if (alive) setText(value);
    });
    if (patch) {
      loadMessageCall(address).then((call) => {
        const a = call && call.name === "patch" && !call.cut ? call.args : null;
        if (alive && a && typeof a.old_string === "string" && typeof a.new_string === "string") {
          setAdded({ oldText: a.old_string, newText: a.new_string });
        }
      });
    }
    return () => {
      alive = false;
    };
  }, [address, patch]);
  const shown = text || (cut ? fallback + " \u2026" : fallback);
  if (raw) return shown;
  if (md) return jsx26("div", { "data-selectable-text": "true", style: Object.assign({ zoom: MD_ZOOM }, SELECTABLE), children: md(shown) });
  return jsx26(ResultView, { text: shown, added });
}
function FullCall({ address, fallback, raw }) {
  const [call, setCall] = useState8(null);
  useEffect3(() => {
    let alive = true;
    loadMessageCall(address).then((value) => {
      if (alive && value) setCall(value);
    });
    return () => {
      alive = false;
    };
  }, [address]);
  const shown = call || fallback;
  if (!shown) return null;
  const callText = String(shown.raw || "");
  return raw ? jsx26("div", {
    style: Object.assign({ overflowWrap: "anywhere" }, SELECTABLE),
    children: callText
  }) : jsx26(CallView, { name: shown.name, args: shown.args, limit: 0, cut: !!shown.cut });
}
var REPLY_PREVIEW_CHARS = 64;
var POPUP_PREVIEW_CHARS = 240;
function isEmptyLeadIn(row) {
  const fact = row.fact || {};
  const role = String(fact.role || "");
  const text = String(fact.text || "");
  const calls = Array.isArray(fact.calls) ? fact.calls : [];
  return role === "assistant" && calls.length > 0 && !text;
}
function EyeButton({ raw, onToggle }) {
  return jsx26("button", {
    onClick: (e) => {
      e.stopPropagation();
      onToggle();
    },
    className: "mirror-action-btn rounded px-1 shrink-0 cursor-pointer select-none transition-colors duration-150 ease-out",
    style: {
      fontSize: "0.7rem",
      lineHeight: 1,
      border: "1px solid transparent",
      background: "transparent",
      color: raw ? "var(--ui-orange)" : "var(--ui-text-quaternary)",
      opacity: raw ? 1 : 0.75
    },
    title: raw ? "showing RAW \u2014 exactly what goes into an exported file; click to render" : "rendered; click to see the RAW text",
    // 📖 — rendered (owner, 2026-10-11: the same book as in the LLMOps window); ◉ orange — raw.
    children: raw ? "\u25C9" : "\u{1F4D6}"
  }, "eye");
}
function PopupHead({ tone, title, address, fallback, raw, onToggleRaw, edit }) {
  const [note, setNote] = useState8(() => takeEditNote(address));
  const [busy, setBusy] = useState8(false);
  const run = (work) => {
    setBusy(true);
    setNote("\u2026");
    Promise.resolve(work()).then((res) => setNote(String(res && res.note || ""))).catch((err) => setNote(String(err && err.message || err))).then(() => setBusy(false));
  };
  const btn2 = (label, { disabled, tone: tone2, hint, onPress }) => jsx26("button", {
    disabled: !!disabled,
    // `stopPropagation` ОБЯЗАТЕЛЕН на каждой (см. выше): без него нажатие ещё и
    // разворачивало бы всплывашку до полного текста.
    onClick: (e) => {
      e.stopPropagation();
      if (onPress && !disabled) onPress();
    },
    // `mirror-action-btn` — тот же ховер, что у кнопок полос действий: правило живёт в
    // `<style>` внутри `prompt-mirror.js` и красит через `box-shadow` от `currentColor`,
    // поэтому подстраивается под цвет КАЖДОЙ кнопки само. Tailwind `hover:bg-*` тут не
    // сработал бы — инлайновый `background` всегда обгоняет класс на ТОМ ЖЕ свойстве.
    //
    // `cursor-pointer` вместо прежнего `cursor-default`: курсор-стрелка и отсутствие отклика
    // на наведение и были причиной, по которой рабочие кнопки читались как неактивные
    // (владелец, 2026-09-23). У выключенной `paste` стрелка остаётся — она и правда не жмётся.
    className: "mirror-action-btn rounded px-2 py-1 select-none transition-colors duration-150 ease-out" + (disabled ? " cursor-default" : " cursor-pointer active:scale-95"),
    style: {
      fontSize: "0.6rem",
      lineHeight: 1,
      border: `1px solid ${tone2 || "var(--ui-stroke-secondary)"}`,
      color: tone2 || "var(--ui-text-quaternary)",
      background: "transparent",
      opacity: disabled ? 0.55 : 0.85
    },
    title: hint,
    children: label
  }, label);
  const reading = jsx26("span", { className: "flex items-center gap-1", children: [
    btn2("copy", {
      disabled: busy,
      hint: "copy the FULL text of this message to the clipboard",
      onPress: () => run(() => copyMessage(address, fallback))
    }),
    btn2("save", {
      disabled: busy,
      hint: "save this message to a file under the export path from Config",
      onPress: () => run(() => exportMessage(address))
    })
  ] }, "read");
  const editing = !!(edit && edit.editing);
  const writing = !edit ? null : jsx26("span", { className: "flex items-center gap-1", children: editing ? [
    btn2("save", {
      disabled: busy || !edit.dirty,
      tone: "var(--ui-orange)",
      hint: edit.dirty ? "REPLACE this message with the edited text (irreversible; role and tool calls stay)" : "nothing changed yet",
      onPress: () => run(edit.onSave)
    }),
    btn2("discard", {
      disabled: busy,
      hint: "leave the editor, keep the message as it is",
      onPress: () => {
        edit.onDiscard();
        setNote("");
      }
    })
  ] : [
    btn2("edit", {
      disabled: busy,
      tone: "var(--ui-orange)",
      hint: "edit the text of this message in place \u2014 Save writes it, Discard drops it",
      onPress: () => run(edit.onEdit)
    })
  ] }, "write");
  const buttons = jsxs23("span", {
    className: "flex items-center gap-4 shrink-0",
    children: editing ? [writing] : [reading, writing]
  }, "buttons");
  const line = jsxs23("div", {
    className: "flex items-baseline gap-2",
    style: Object.assign({ fontSize: "0.6rem", color: tone }, SELECTABLE),
    children: [
      // В правке глазика нет (владелец, 2026-10-02): поле ввода показывает сырой текст всегда,
      // переключатель «rendered / raw» ни на что бы не влиял.
      onToggleRaw && !editing ? jsx26(EyeButton, { raw, onToggle: onToggleRaw }, "eye") : null,
      // Подсказка с ПОЛНЫМ заголовком — по той же причине, что у строки сохранения: имя
      // тула с именем файла и адресом в узкое окно не влезает, а `truncate` съедает как раз
      // конец, где стоят адрес и размер (владелец, 2026-09-23).
      jsx26("span", { className: "truncate", style: SELECTABLE, title, children: title }),
      jsx26("span", { className: "flex-1" }),
      buttons
    ]
  }, "line");
  const said = note ? jsx26("div", {
    className: "truncate text-right",
    style: Object.assign({
      fontSize: "0.55rem",
      opacity: 0.75,
      color: "var(--ui-text-quaternary)"
    }, SELECTABLE),
    title: note,
    children: note
  }, "note") : null;
  return jsxs23("div", { className: "flex flex-col gap-1", children: [line, said] }, "head");
}
function addrMark(address) {
  return jsx26("span", {
    className: "shrink-0 text-(--ui-text-quaternary)",
    style: Object.assign({ fontSize: "0.5rem", opacity: 0.7 }, SELECTABLE),
    title: address ? `message address ${address}` : "no address \u2014 the indexer did not mark this row",
    children: address || "----"
  });
}
function ReplyBrick({ row, popup, setPopup }) {
  const fact = row.fact || {};
  const role = String(fact.role || "");
  const text = String(fact.text || "");
  const chars = Number(fact.chars || 0);
  const calls = Array.isArray(fact.calls) ? fact.calls : [];
  const cut = chars > text.length;
  const emptyLabel = calls.length ? "no text \u2014 this row only calls tools" : "empty";
  const tone = rowTone(fact);
  const preview = briefPreview(text, REPLY_PREVIEW_CHARS);
  const open = !!popup && popup.id === row.key;
  const [edit, setEdit] = useState8(null);
  useEffect3(() => {
    if (!open) setEdit(null);
  }, [open]);
  const editControl = {
    editing: !!edit,
    dirty: !!edit && edit.text !== edit.base,
    // Править можно только ПОЛНЫЙ текст: в ленте он обрезан, и запись обрезка вместо
    // сообщения молча укоротила бы его. Не приехал полный — отказ, а не правка превью.
    onEdit: () => {
      setPopup({ id: row.key, tier: "full" });
      return loadMessageText(row.address).then((full) => {
        const base = full ? String(full) : cut ? null : text;
        if (base === null) return { note: "could not load the full text \u2014 editing refused" };
        setEdit({ text: base, base });
        return { note: "editing \u2014 save writes it, discard drops it" };
      });
    },
    onSave: () => editMessage(row.address, edit ? edit.text : "").then((res) => {
      if (res && res.ok) {
        setEdit(null);
        if (res.now) setPopup({ id: `msg-${res.now}`, tier: "full" });
      }
      return res;
    }),
    onDiscard: () => setEdit(null)
  };
  const keep = (e) => e.stopPropagation();
  const editRows = edit ? Math.min(40, Math.max(8, edit.text.split("\n").length + 2)) : 0;
  const editor = edit ? jsx26("textarea", {
    value: edit.text,
    rows: editRows,
    autoFocus: true,
    spellCheck: false,
    onChange: (e) => {
      const value = e.target.value;
      setEdit((cur) => cur && { ...cur, text: value });
    },
    onClick: keep,
    onMouseDown: keep,
    onKeyDown: keep,
    onKeyUp: keep,
    onPaste: keep,
    className: "w-full rounded",
    style: {
      display: "block",
      boxSizing: "border-box",
      resize: "none",
      padding: "4px",
      fontSize: "0.65rem",
      fontFamily: "inherit",
      lineHeight: 1.4,
      whiteSpace: "pre-wrap",
      cursor: "text",
      color: "var(--ui-text-primary)",
      background: "transparent",
      border: "1px solid var(--ui-orange)",
      outline: "none"
    }
  }, "editor") : null;
  if (isEmptyLeadIn(row)) return null;
  return jsx26(BrickRow, {
    tokens: tokensLabel(chipTokens(fact)),
    chip: jsx26(ChipTrigger, {
      id: row.key,
      popup,
      setPopup,
      tone,
      border: true,
      label: preview || emptyLabel,
      tip: text ? briefPreview(text, 200) : emptyLabel,
      renderPopup: (tier, eye) => ({
        head: jsx26(PopupHead, {
          tone,
          address: row.address,
          fallback: text,
          edit: editControl,
          raw: eye.raw,
          onToggleRaw: eye.toggleRaw,
          // Токены рядом с символами (владелец, 2026-09-23): цена сообщения читается в
          // токенах, а не в символах, и это то же число, что стоит слева от чипа в ленте
          // (`factTokens`) — одна оценка на все поверхности, не вторая рядом с первой.
          title: `${(role || "no role").toUpperCase()} \xB7 ${row.address || "----"} \xB7 ${chars} chars \xB7 ~${tokensLabel(chipTokens(fact))} tk` + (typeof fact.prose_tokens === "number" ? " (prose; calls on their own chips)" : "")
        }),
        // Верхней секции у реплики НЕТ (решение владельца 2026-09-23): её вызовы уже
        // нарисованы своими кирпичиками, и повторять их здесь значит показать одно и то же
        // дважды. `TwoPanes` с пустым верхом схлопывается сам — своей ветки не нужно.
        body: jsx26(TwoPanes, {
          bottomChars: tier === "full" ? chars : Math.min(chars, POPUP_PREVIEW_CHARS),
          bottom: editor || jsx26("div", {
            style: Object.assign({ overflowWrap: "anywhere" }, SELECTABLE),
            children: !text ? emptyLabel : tier === "full" ? jsx26(FullText, {
              address: row.address,
              fallback: text,
              cut,
              raw: eye.raw,
              md: role === "assistant" ? markdown : null
            }) : briefPreview(text, POPUP_PREVIEW_CHARS)
          })
        }),
        foot: tier === "summary" ? "click for full text" : null
      })
    }),
    rest: [addrMark(row.address)]
  });
}
function MessageTile({ row }) {
  const fact = row.fact || {};
  const role = String(fact.role || "");
  const chars = Number(fact.chars || 0);
  const text = String(fact.text || "");
  const calls = Array.isArray(fact.calls) ? fact.calls : [];
  const cut = chars > text.length;
  const marks = [];
  if (fact.name) marks.push(String(fact.name));
  if (calls.length) marks.push("\u2192 " + calls.join(", "));
  if (row.opener) marks.push("opens the carriage");
  if (row.unrecorded) marks.push("ours, no ledger record");
  if (fact.tool_call_id) marks.push("answers " + String(fact.tool_call_id).slice(0, 12));
  if (fact.sent_as === "api_content") {
    marks.push(fact.db_chars !== void 0 ? `sent as api_content (${fact.db_chars} stored)` : "sent as api_content");
  }
  if (fact.their_repair) marks.push(String(fact.their_repair));
  return jsxs23("div", {
    className: "px-2 py-1 rounded-md border border-(--ui-stroke-secondary) bg-card",
    style: Object.assign({ fontSize: "0.65rem" }, CAPPED, SELECTABLE),
    children: [
      jsxs23("div", {
        className: "flex items-center justify-between gap-2",
        style: Object.assign({ fontSize: "0.55rem" }, SELECTABLE),
        children: [
          // Роль ПЕРЕД номером и КАПСОМ (владелец, 2026-09-10): сначала «что это за
          // строка», потом её адрес.
          jsxs23("span", {
            className: "font-medium",
            style: Object.assign({ color: rowTone(fact), overflowWrap: "anywhere" }, SELECTABLE),
            children: [(role || "no role").toUpperCase(), " \xB7 ", row.address || "\u2014"]
          }),
          // ПИМПОЧКА чужого вызова — в шапке плитки, а не строкой среди прочих
          // пометок: «наше или не наше» находят взглядом по списку, а текст в
          // общей строке для этого надо читать. Оранжевый, а не красный: чужой
          // тул — это норма (десятая часть тул-строк живой базы), а не поломка.
          jsxs23("span", {
            className: "flex items-center gap-1.5 shrink-0",
            style: SELECTABLE,
            children: [
              row.foreign ? jsx26("span", {
                className: "px-1.5 rounded-full",
                style: Object.assign({
                  border: "1px solid var(--ui-orange)",
                  color: "var(--ui-orange)",
                  background: "color-mix(in srgb, var(--ui-orange) 12%, transparent)",
                  whiteSpace: "nowrap"
                }, SELECTABLE),
                title: "no operation of ours carries this tool_call_id",
                children: `${CHIP_STATUS.foreign.glyph} foreign`
              }) : null,
              jsx26("span", {
                className: "text-(--ui-text-quaternary)",
                style: SELECTABLE,
                children: cut ? `${text.length}/${chars} chars` : `${chars} chars`
              })
            ]
          })
        ]
      }),
      // НЕ `truncate`: обрезанное имя тула нельзя скопировать, а копировать нужно всё
      // (владелец, 2026-09-10). Переносим строкой.
      marks.length ? jsx26("div", {
        className: "text-(--ui-text-quaternary)",
        style: Object.assign({ fontSize: "0.55rem", overflowWrap: "anywhere" }, SELECTABLE),
        children: marks.join(" \xB7 ")
      }) : null,
      // Тело — с переносом, а не `truncate`: `truncate` это nowrap, то есть ширина
      // max-content, и одна длинная строка растянула бы весь список (та же грабля, что
      // уже оплачена в file-ops-pane.js).
      text ? jsx26("div", {
        className: "mt-0.5 text-(--ui-text-secondary)",
        style: Object.assign(
          { whiteSpace: "pre-wrap", overflowWrap: "anywhere" },
          CAPPED,
          SELECTABLE
        ),
        children: cut ? text + " \u2026" : text
      }) : jsx26("div", {
        className: "mt-0.5 text-(--ui-text-quaternary) italic",
        style: Object.assign({ fontSize: "0.55rem" }, SELECTABLE),
        children: calls.length ? "no text \u2014 this row only calls tools" : "empty"
      })
    ]
  });
}
function StepBar({ row }) {
  return jsx26("div", {
    style: { height: ROW_HEIGHT2.step, boxSizing: "border-box", display: "flex", alignItems: "center" },
    title: `step ${row.index} \xB7 ${row.count} calls`,
    children: jsx26("div", { className: "w-full border-t border-(--ui-stroke-tertiary)" })
  });
}
function MessageNode({ row, popup, setPopup }) {
  const role = String(row.fact && row.fact.role || "");
  return role === "user" || role === "assistant" ? jsx26(ReplyBrick, { row, popup, setPopup }) : jsx26(MessageTile, { row });
}
var MOCK_OP = UNASSIGNED_OP;
function ToolPairBrick({ row, popup, setPopup, stale, ledger, duration }) {
  const answer = row.answer;
  const afact = answer && answer.fact || {};
  const chars = Number(afact.chars || 0);
  const text = String(afact.text || "");
  const cut = chars > text.length;
  const tone = toneOf("tool");
  const name = row.name || "\u0442\u0443\u043B";
  const realOp = afact.op || "";
  const displayData = chipTarget({ fact: afact, target: row.target });
  const displayOp = realOp || MOCK_OP;
  const plainLabel = [name, displayData, displayOp].filter(Boolean).join(" \xB7 ");
  const addr = answer ? answer.address || "" : "";
  const foreign = !!(answer && answer.foreign);
  const unrecorded = !!(answer && answer.unrecorded);
  const statusMark = foreign ? `${CHIP_STATUS.foreign.glyph} foreign` : unrecorded ? "no ledger record" : "";
  const call = afact.call || null;
  const callText = call ? String(call.raw || "") : "";
  const realPct = realOp && stale && typeof stale[realOp] === "number" ? stale[realOp] : null;
  const pct = answer ? realPct : null;
  const statuses = chipStatuses({ ledger, fact: afact, foreign, unrecorded });
  const diff = chipDiff(ledger, afact);
  const tip = chipTipLines({
    name,
    op: realOp,
    ledger,
    fact: afact,
    target: row.target,
    statuses,
    duration,
    tokens: tokensLabel(chipTokens(afact)),
    freshness: pct === null ? "\u2014" : pct + "%"
  }).join("\n") + (text ? "\n\n" + briefPreview(text, 200) : "");
  const chipBody = jsx26(OpChipBody, {
    marks: statuses,
    name,
    data: displayData,
    diff,
    op: displayOp,
    overlapText: pct === null ? "" : `${pct}%`,
    overlapTone: pct !== null && pct > 60 ? "var(--ui-orange)" : "var(--ui-text-quaternary)",
    overlapTitle: pct === null ? void 0 : "covered by later operations on this file (buildContextView().stale)"
  });
  const rest = [addrMark(addr)];
  return jsx26(BrickRow, {
    tokens: tokensLabel(chipTokens(afact)),
    chip: jsx26(ChipTrigger, {
      id: row.key,
      popup,
      setPopup,
      tone,
      flushLeft: true,
      label: chipBody,
      tip,
      renderPopup: (tier, eye) => {
        const callLimit = 0;
        return {
          // Без `paste`: результат инструмента правке не подлежит.
          head: jsx26(PopupHead, {
            tone,
            address: addr,
            fallback: text,
            raw: eye.raw,
            onToggleRaw: eye.toggleRaw,
            title: `${plainLabel} \xB7 ${addr || "----"} \xB7 ${chars} chars \xB7 ~${tokensLabel(chipTokens(afact))} tk` + (typeof afact.call_tokens === "number" ? ` (call ${tokensLabel(afact.call_tokens)} + answer ${tokensLabel(factTokens(afact))})` : "") + (statusMark ? " \xB7 " + statusMark : "")
          }),
          // ДВЕ СЕКЦИИ: сверху вызов, снизу результат (Plan22-Task02). Пока в верхней тот же
          // текст, что уходит в файл выгрузки (`call.raw`) — рендер таблицей это Task04, и слот
          // под него здесь уже стоит.
          body: jsx26(TwoPanes, {
            topChars: callText.length,
            bottomChars: tier === "full" ? chars : Math.min(chars, POPUP_PREVIEW_CHARS),
            // В развёрнутом окне приоритет у ВЫЗОВА: он показывается целиком и не скроллится,
            // результат забирает остаток (решение владельца 2026-09-23). Вызов читают один раз и
            // целиком, результат листают — место, выигранное разворотом, честнее отдать первому.
            topWhole: !!eye.tall,
            // СЫРЬЁ — это `call.raw`, то есть БУКВАЛЬНО то, что уходит в файл выгрузки
            // (`context_ctl/_export.py::render_call`). Обещание глазика «покажи как есть»
            // держится тем, что сравнивать не с чем: строка одна и приехала с сервера.
            top: !call ? null : tier === "full" ? jsx26(FullCall, { address: addr, fallback: call, raw: eye.raw }) : eye.raw ? jsx26("div", {
              style: Object.assign({ overflowWrap: "anywhere" }, SELECTABLE),
              children: callText
            }) : jsx26(CallView, { name: call.name, args: call.args, limit: callLimit, cut: !!call.cut }),
            bottom: jsx26("div", {
              style: Object.assign({ overflowWrap: "anywhere" }, SELECTABLE),
              children: !answer ? "no answer row recorded for this call" : !text ? "empty" : tier === "full" ? jsx26(FullText, { address: addr, fallback: text, cut, raw: eye.raw, patch: !!call && call.name === "patch" }) : eye.raw ? briefPreview(text, POPUP_PREVIEW_CHARS) : jsx26(ResultView, { text: briefPreview(text, POPUP_PREVIEW_CHARS) })
            })
          }),
          foot: tier === "summary" ? "click for full text" : null
        };
      }
    }),
    rest
  });
}

// desktop/src/mirror-action-zone.js
import { jsx as jsx27, jsxs as jsxs24 } from "react/jsx-runtime";
var ACTION_BTN = {
  fontSize: "0.625rem",
  lineHeight: 1,
  padding: "0.125rem 0.5625rem",
  boxSizing: "border-box"
};
var ACTION_BTN_THIN = Object.assign({}, ACTION_BTN, { padding: "0 0.5rem", lineHeight: 1.25 });
var ADDR_ACTIONS = [
  {
    kind: "content",
    label: "drop content",
    color: ACTION_COLOR.content,
    hint: 'arm "drop content": tick tool results on the rail, then press Apply operation'
  },
  {
    kind: "erase",
    label: "erase",
    color: ACTION_COLOR.erase,
    hint: 'arm "erase": tick any messages on the rail, then press Apply operation'
  }
];
function ActionZone({ op, thin }) {
  const buttons = ADDR_ACTIONS.map((action) => {
    const armed = !!op.mode && op.kind === action.kind;
    return jsx27("button", {
      key: action.kind,
      onClick: (e) => {
        e.stopPropagation();
        if (op.busy) return;
        if (armed) setAddrOpMode(false);
        else setAddrOpMode(true, action.kind);
      },
      className: "mirror-action-btn rounded-full font-medium cursor-pointer select-none transition-colors duration-150 ease-out active:scale-95",
      disabled: !!op.busy,
      style: Object.assign({
        border: `1px solid ${armed ? action.color : "var(--ui-stroke-secondary)"}`,
        background: armed ? `color-mix(in srgb, ${action.color} 14%, transparent)` : "transparent",
        color: armed ? action.color : "var(--ui-text-secondary)",
        opacity: op.busy ? 0.5 : 1,
        whiteSpace: "nowrap"
      }, thin ? ACTION_BTN_THIN : ACTION_BTN),
      title: armed ? `close "${action.label}" mode` : action.hint,
      children: action.label
    });
  });
  return jsxs24("div", {
    className: thin ? "flex items-center gap-1.5 px-1" : "flex items-center gap-2 px-2 py-1",
    style: Object.assign({ fontSize: "0.55rem" }, SELECTABLE),
    children: buttons
  });
}
function ExportZone({ exp, addresses, thin }) {
  const armed = !!exp.mode;
  const color = ACTION_COLOR.export;
  return jsx27("div", {
    className: thin ? "flex items-center px-1" : "flex items-center gap-2 px-2 py-1",
    style: Object.assign({ fontSize: "0.55rem" }, SELECTABLE),
    children: jsx27("button", {
      onClick: (e) => {
        e.stopPropagation();
        if (exp.busy) return;
        if (armed) {
          setExportMode(false);
          return;
        }
        const picked = {};
        (addresses || []).forEach((a) => {
          if (a) picked[a] = true;
        });
        setExportMode(true, picked);
      },
      className: "mirror-action-btn rounded-full font-medium cursor-pointer select-none transition-colors duration-150 ease-out active:scale-95",
      disabled: !!exp.busy,
      style: Object.assign({
        border: `1px solid ${armed ? color : "var(--ui-stroke-secondary)"}`,
        background: armed ? `color-mix(in srgb, ${color} 14%, transparent)` : "transparent",
        color: armed ? color : "var(--ui-text-secondary)",
        opacity: exp.busy ? 0.5 : 1,
        whiteSpace: "nowrap"
      }, thin ? ACTION_BTN_THIN : ACTION_BTN),
      title: armed ? "close export mode" : "arm export: tick messages on the rail, then press Apply operation",
      children: "export"
    })
  });
}
function ImportZone({ imp, thin, onOpen }) {
  const open = !!imp.open;
  const color = ACTION_COLOR.import;
  return jsx27("div", {
    className: thin ? "flex items-center px-1" : "flex items-center gap-2 px-2 py-1",
    style: Object.assign({ fontSize: "0.55rem" }, SELECTABLE),
    children: jsx27("button", {
      onClick: (e) => {
        e.stopPropagation();
        if (imp.busy) return;
        if (open) {
          closeImport();
          return;
        }
        if (llmopsWindowOpen()) return;
        const box = e.currentTarget && e.currentTarget.getBoundingClientRect ? e.currentTarget.getBoundingClientRect() : null;
        if (onOpen) onOpen(box ? { x: box.left + box.width / 2, y: box.bottom } : null);
        openImport();
      },
      className: "mirror-action-btn rounded-full font-medium cursor-pointer select-none transition-colors duration-150 ease-out active:scale-95",
      disabled: !!imp.busy,
      style: Object.assign({
        border: `1px solid ${open ? color : "var(--ui-stroke-secondary)"}`,
        background: open ? `color-mix(in srgb, ${color} 14%, transparent)` : "transparent",
        color: open ? color : "var(--ui-text-secondary)",
        opacity: imp.busy ? 0.5 : 1,
        whiteSpace: "nowrap"
      }, thin ? ACTION_BTN_THIN : ACTION_BTN),
      title: "import a session from an export folder \u2014 REPLACES this chat's whole context",
      children: "import"
    })
  });
}
function LLMOpsZone({ thin }) {
  const rail = useLLMOpsRail();
  const armed = !!rail.mode;
  const color = ACTION_COLOR.llmops;
  return jsx27("div", {
    className: thin ? "flex items-center px-1" : "flex items-center gap-2 px-2 py-1",
    style: Object.assign({ fontSize: "0.55rem" }, SELECTABLE),
    children: jsx27("button", {
      onClick: (e) => {
        e.stopPropagation();
        setLLMOpsMode(!armed);
      },
      className: "mirror-action-btn rounded-full font-medium cursor-pointer select-none transition-colors duration-150 ease-out active:scale-95",
      style: Object.assign({
        border: `1px solid ${armed ? color : "var(--ui-stroke-secondary)"}`,
        background: armed ? `color-mix(in srgb, ${color} 14%, transparent)` : "transparent",
        color: armed ? color : "var(--ui-text-secondary)",
        whiteSpace: "nowrap"
      }, thin ? ACTION_BTN_THIN : ACTION_BTN),
      title: armed ? 'close "llm ops" mode' : 'arm "llm ops": tick messages on the rail, then press Apply operation \u2014 opens the LLMOps window',
      children: "llm ops"
    })
  });
}

// desktop/src/mirror-zones.js
import { jsx as jsx29, jsxs as jsxs26 } from "react/jsx-runtime";

// desktop/src/compact-progress.js
import { jsx as jsx28, jsxs as jsxs25 } from "react/jsx-runtime";
var STAGE_TEXT = {
  export: "exporting the session",
  prepare: "selecting",
  warmup: "warming up the model",
  questions: "asking",
  assemble: "assembling the tape",
  checks: "checking the tape",
  write: "writing the base",
  handover: "handing the tape to Hermes",
  wait_turn: "waiting for the turn to end"
};
var STAGE_PCT = { export: 1, prepare: 2, warmup: 4, assemble: 95, checks: 97, write: 99, handover: 99, wait_turn: 98 };
var Q_FROM = 5;
var Q_SPAN = 88;
function fmtClock(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function runLine(run) {
  const r = run || {};
  if (!r.running) return null;
  const now = Number(r.now) || 0;
  const elapsed = now && r.started_at ? now - Number(r.started_at) : 0;
  const silentFor = now && r.beat ? now - Number(r.beat) : 0;
  const silent = Number(r.silence_s) > 0 && silentFor > Number(r.silence_s);
  const parts = [r.who === "auto" ? "\u27F3 auto" : r.who === "llmops" ? "\u27F3 llm ops" : "\u27F3 compact"];
  if (r.span) parts.push(r.span);
  let pct = STAGE_PCT[r.stage] || 0;
  if (r.stage === "questions") {
    const step = Number(r.step) || 0;
    const total = Number(r.total) || 0;
    parts.push(total ? `question ${step}/${total}` : `question ${step}`);
    const what = [r.topic ? `topic ${r.topic}` : "", r.label || ""].filter(Boolean).join(": ");
    if (what) parts.push(what);
    pct = total ? Q_FROM + Q_SPAN * Math.min(1, Math.max(0, (step - 1) / total)) : Q_FROM;
  } else {
    parts.push(STAGE_TEXT[r.stage] || r.stage || "starting");
  }
  parts.push(fmtClock(elapsed));
  let text = parts.join(" \xB7 ");
  if (silent) text += ` \xB7 no step for ${fmtClock(silentFor)} \u2014 may be hung`;
  return { text, pct: Math.round(pct), silent, silentFor };
}
function CompactProgress({ run, font }) {
  const line = runLine(run);
  if (!line) return null;
  const tone = line.silent ? "var(--ui-yellow)" : run && run.who === "llmops" ? ACTION_COLOR.llmops : ACTION_COLOR.compact;
  return jsxs25("div", {
    className: "flex flex-col min-w-0 w-full",
    "data-compact-progress": "",
    children: [
      jsx28("span", {
        className: "truncate",
        title: line.text,
        style: { color: tone, fontSize: font, userSelect: "text", minWidth: 0 },
        children: line.text
      }),
      jsx28("div", {
        style: { height: 2, width: "100%", background: "var(--ui-stroke-quaternary)", borderRadius: 1 },
        children: jsx28("div", {
          style: { height: "100%", width: `${line.pct}%`, background: tone, borderRadius: 1, transition: "width 0.4s" }
        })
      })
    ]
  });
}

// desktop/src/mirror-zones.js
var HEAD_FONT = "0.55rem";
var TOGGLE_FONT = "0.8rem";
function ZoneToggle({ title, open, onToggle }) {
  return jsx29("button", {
    type: "button",
    onClick: (e) => {
      e.stopPropagation();
      onToggle();
    },
    className: "cursor-pointer shrink-0",
    style: {
      color: "var(--ui-text-quaternary)",
      fontSize: TOGGLE_FONT,
      lineHeight: 1,
      border: "none",
      background: "transparent",
      padding: "0 2px"
    },
    title: open ? `collapse ${title.toLowerCase()}` : `expand ${title.toLowerCase()}`,
    children: open ? "\u25BE" : "\u25B8"
  });
}
function OpGroup({ label, lead, children }) {
  return jsxs26("div", {
    className: "flex items-center flex-wrap",
    children: [
      jsx29("span", {
        className: "flex items-center shrink-0",
        style: { width: "3.6rem", paddingLeft: "0.375rem", gap: "2px" },
        children: [
          lead || jsx29("span", { style: { display: "inline-block", width: "0.95rem" } }, "pad"),
          jsx29("span", { className: "text-(--ui-text-quaternary)", style: { fontSize: HEAD_FONT }, children: label }, "label")
        ]
      }),
      ...[].concat(children)
    ]
  });
}
function noteTone(ok) {
  return ok === true ? "var(--ui-green)" : ok === false ? "var(--ui-red)" : "var(--ui-text-quaternary)";
}
var AUTO_RESULT_MS = 6e4;
function StatusLine({ report, run }) {
  if (run && run.running) {
    return jsx29("div", {
      className: "shrink-0 flex items-center px-2 min-w-0 border-b border-(--ui-stroke-quaternary)",
      style: { fontSize: HEAD_FONT, lineHeight: 1.6, paddingBottom: 2 },
      children: jsx29(CompactProgress, { run, font: HEAD_FONT })
    });
  }
  if (run && run.who === "auto" && run.doneAt && Date.now() - run.doneAt < AUTO_RESULT_MS) {
    const lines = Array.isArray(run.lines) ? run.lines : [];
    report = {
      note: "auto-compaction: " + (run.error || lines[lines.length - 1] || (run.ok ? "done" : "not done")),
      ok: run.ok === true
    };
  }
  if (!report || !report.note) {
    return jsx29("div", {
      className: "shrink-0 px-2 border-b border-(--ui-stroke-quaternary) text-(--ui-text-quaternary)",
      style: { fontSize: HEAD_FONT, lineHeight: 1.6 },
      children: "no reports yet"
    });
  }
  return jsxs26("div", {
    className: "shrink-0 flex items-center gap-2 px-2 min-w-0 border-b border-(--ui-stroke-quaternary)",
    style: { fontSize: HEAD_FONT, lineHeight: 1.6 },
    children: [
      report.dir ? jsx29("button", {
        type: "button",
        className: "mirror-action-btn rounded-full cursor-pointer select-none shrink-0",
        title: "copy the folder path to the clipboard",
        onClick: (e) => {
          e.stopPropagation();
          copyExportDir();
        },
        style: Object.assign({
          border: "1px solid var(--ui-stroke-secondary)",
          color: "var(--ui-text-secondary)",
          background: "transparent",
          whiteSpace: "nowrap"
        }, ACTION_BTN),
        children: "copy path"
      }) : null,
      jsx29("span", {
        className: "truncate",
        title: report.note,
        style: Object.assign({ color: noteTone(report.ok), minWidth: 0 }, SELECTABLE),
        children: report.note
      })
    ]
  });
}

// desktop/src/file-context-panel.js
import { jsx as jsx30, jsxs as jsxs27 } from "react/jsx-runtime";
import { useMemo, useRef as useRef5, useState as useState9, useLayoutEffect as useLayoutEffect3 } from "react";
var MIN_H = 56;
var MAX_H = 420;
var DEFAULT_H = 132;
var AUTO_ROWS = 3.5;
function shortLength(n2) {
  const v = Number(n2);
  if (!Number.isFinite(v) || v < 0) return "";
  if (v < 1e3) return String(Math.round(v));
  if (v < 1e4) return `${(v / 1e3).toFixed(1).replace(/\.0$/, "")}k`;
  return `${Math.round(v / 1e3)}k`;
}
function truncateName(name, max = 20) {
  const s = typeof name === "string" ? name : "";
  if (s.length <= max) return s;
  if (max <= 1) return s.slice(0, Math.max(0, max));
  const headLen = Math.ceil((max - 1) / 2);
  const tailLen = max - 1 - headLen;
  return tailLen > 0 ? `${s.slice(0, headLen)}\u2026${s.slice(s.length - tailLen)}` : `${s.slice(0, headLen)}\u2026`;
}
function graySegments(files, order, selectedOp) {
  if (!Array.isArray(files) || !selectedOp || !order || !(selectedOp in order)) return files;
  if (!files.some((f) => Array.isArray(f.segments) && f.segments.some((s) => s.op === selectedOp))) {
    return files;
  }
  const cutoff = order[selectedOp];
  return files.map((f) => Object.assign({}, f, {
    segments: f.segments.map((s) => Object.assign({}, s, {
      gray: s.op && s.op in order ? order[s.op] > cutoff : false
    }))
  }));
}
var BAR_BASE = "var(--ui-bg-elevated)";
var FADE_STEP = 0.75;
function mixWithBase(color, pct) {
  return `color-mix(in srgb, ${color} ${pct}%, ${BAR_BASE})`;
}
function kindBase(kind) {
  if (kind === "patch") return "var(--ui-orange)";
  if (kind === "read_nested") return "var(--ui-cyan)";
  return "var(--ui-green)";
}
function segFill(kind, rank, gray) {
  if (gray) return mixWithBase("var(--ui-text-quaternary)", 30);
  const pct = Math.pow(FADE_STEP, Math.max(0, rank || 0)) * 100;
  return mixWithBase(kindBase(kind), Math.round(pct * 10) / 10);
}
function segZ(rank) {
  return -Math.max(0, rank || 0);
}
var POPUP_MAX_W = 280;
var POPUP_EDGE = 8;
function popupStyle(x, y, extra) {
  const vw = typeof window !== "undefined" ? window.innerWidth : Infinity;
  const flip = x + 10 + POPUP_MAX_W > vw - POPUP_EDGE;
  return Object.assign({
    position: "fixed",
    top: `${y}px`,
    left: flip ? "auto" : `${x}px`,
    right: flip ? `${vw - x + 10}px` : "auto",
    transform: flip ? "translate(0, 12px)" : "translate(10px, 12px)",
    zIndex: 999,
    maxWidth: `${POPUP_MAX_W}px`,
    backgroundColor: "var(--ui-bg-elevated)",
    border: "1px solid var(--ui-stroke-secondary)",
    color: "var(--ui-text-primary)",
    borderRadius: "6px",
    padding: "4px 6px",
    fontSize: "0.62rem",
    lineHeight: 1.35,
    whiteSpace: "pre-line"
  }, extra);
}
function dirOf(path) {
  const s = String(path || "");
  const cut = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
  return cut > 0 ? s.slice(0, cut) : cut === 0 ? "/" : "";
}
function NameHover({ file, children }) {
  const [pos, setPos] = useState9(null);
  const dir = dirOf(file.path);
  return jsxs27("div", {
    style: { minWidth: 0, cursor: "default" },
    onMouseMove: (e) => setPos({ x: e.clientX, y: e.clientY }),
    onMouseLeave: () => setPos(null),
    children: [
      children,
      pos ? jsxs27("div", {
        style: Object.assign(popupStyle(pos.x, pos.y), { pointerEvents: "none", whiteSpace: "normal" }),
        children: [
          jsx30("div", { style: { fontSize: "0.68rem", color: "var(--ui-text-primary)" }, children: file.name }),
          dir ? jsx30("div", {
            className: "font-mono",
            style: { fontSize: "0.55rem", color: "var(--ui-text-quaternary)", wordBreak: "break-all" },
            children: dir
          }) : null
        ]
      }) : null
    ]
  });
}
function toolLabel(tool) {
  return String(tool || "").replace(/_files?$/, "");
}
function opIdsBadge(ids) {
  const list = (ids || []).filter(Boolean).map(String);
  if (!list.length) return "";
  return list.length > 1 ? `${list[list.length - 1]} \xD7${list.length}` : list[0];
}
function opIdsLine(ids) {
  const list = (ids || []).filter(Boolean).map(String);
  if (!list.length) return "";
  const rest = list.slice(1).map((id) => id.replace(/^0+(?=.)/, ""));
  return [list[0]].concat(rest).join("  ");
}
function rangeText(from, to) {
  if (from == null || to == null) return "\u2014";
  return from === to ? String(from) : `${from}-${to}`;
}
function OpIdsPopup({ file, pos, onClose, onPick }) {
  const [hot, setHot] = useState9(null);
  if (!pos) return null;
  const rows = file.rows || [];
  return jsx30("div", {
    // Текст выделяется мышью (владелец, 2026-09-29: «выделяешь и копируешь, когда нужно сказать
    // агенту — вот ты читал 100-123»). Панель целиком `select-none`, поэтому разрешаем явно.
    style: Object.assign(popupStyle(pos.x, pos.y), { whiteSpace: "normal", cursor: "text" }, SELECTABLE),
    onMouseDown: (e) => e.stopPropagation(),
    onMouseLeave: onClose,
    children: jsx30("div", {
      className: "font-mono",
      style: {
        display: "grid",
        gridTemplateColumns: "auto auto auto",
        columnGap: "6px",
        rowGap: "1px",
        fontSize: "0.58rem"
      },
      children: rows.flatMap((r, i) => {
        const pick = onPick && r.op ? (e) => {
          e.stopPropagation();
          onPick(r.op);
          onClose();
        } : null;
        const cell = pick ? { onClick: pick, onMouseEnter: () => setHot(i), onMouseLeave: () => setHot(null) } : {};
        const hotBg = hot === i ? { backgroundColor: "var(--ui-bg-tertiary)", cursor: "pointer" } : {};
        return [
          jsxs27("div", Object.assign({ key: `t${i}`, style: Object.assign({ color: "var(--ui-text-secondary)" }, hotBg), children: [
            toolLabel(r.tool),
            // Метка вложенности. Говорит РОВНО одно: операция выполнена внутри `execute_code` —
            // именно это и означает флаг `nested` в леджере. «Из терминала» показать нельзя: правка
            // файла шелл-командой нашими тулами не проходит вовсе и в леджер не попадает, так что
            // такого факта у нас просто нет, и выдумывать его метка не станет.
            r.nested ? jsx30("span", {
              // Тот же плотный оттенок, что у вложенного чтения на баре, и БЕЗ `opacity`: правило
              // «только оттенки, не прозрачности» держим по всей панели, а не только на слоях.
              style: { color: segFill("read_nested", 0, false) },
              children: " exec"
            }) : null
          ] }, cell)),
          jsx30("div", Object.assign({ key: `o${i}`, style: Object.assign({ color: "var(--ui-text-primary)", textAlign: "right" }, hotBg), children: `${r.op}:` }, cell)),
          jsx30("div", Object.assign({
            key: `r${i}`,
            style: Object.assign({ color: r.from == null ? "var(--ui-text-quaternary)" : "var(--ui-text-primary)" }, hotBg),
            children: rangeText(r.from, r.to)
          }, cell))
        ];
      })
    })
  });
}
function Tile({ file, popupPos, onTogglePopup, onClosePopup, onPickOp }) {
  const segs = file.segments;
  return jsxs27("div", {
    className: "rounded-sm border",
    // Над именем почти без воздуха (владелец, 2026-09-29): 1px и строка без наследуемого
    // межстрочного интервала.
    style: Object.assign({ borderColor: "var(--ui-stroke-tertiary)", padding: "1px 4px 2px", minWidth: 0 }, CAPPED),
    children: [
      jsxs27("div", {
        className: "flex items-baseline gap-1",
        style: { lineHeight: 1.2 },
        children: [
          jsx30("div", {
            className: "flex-1 min-w-0",
            children: jsx30(NameHover, {
              file,
              children: jsx30("div", {
                className: "truncate",
                style: { fontSize: "0.6rem", color: "var(--ui-text-secondary)" },
                children: truncateName(file.name)
              })
            })
          }),
          // Номера операций: клик — список целиком (повторный закрывает; другая плитка —
          // закрывает эту и открывает ту, решает панель). Усекаются первыми: полный список
          // всегда в попапе.
          jsx30("div", {
            className: "truncate shrink",
            style: {
              fontSize: "0.5rem",
              maxWidth: "45%",
              color: "var(--ui-text-quaternary)",
              cursor: file.op_ids.length ? "pointer" : "default"
            },
            onClick: (e) => {
              if (!file.op_ids.length) return;
              onTogglePopup(file.path, { x: e.clientX, y: e.clientY });
            },
            title: file.op_ids.length > 1 ? opIdsLine(file.op_ids) : void 0,
            children: (opIdsBadge(file.op_ids) || "") + (file.desync ? " \u26A0" : "")
          })
        ]
      }),
      jsxs27("div", {
        className: "flex items-center gap-1",
        style: { marginTop: "2px" },
        children: [
          // Бар: базовый слой чтений (непрозрачный, свежее НАД старым — DOM-порядок делает это
          // бесплатно), поверх — патчи (полупрозрачные, multiply, копятся сами). `isolation:isolate`
          // держит блендинг внутри этой плитки, чтобы он не утёк на соседей/страницу.
          jsxs27("div", {
            style: { position: "relative", height: "4px", flex: "1 1 auto", minWidth: 0, isolation: "isolate" },
            children: [
              // Обрезающая обёртка ВОКРУГ СЛОЁВ, а не вокруг всего бара: подпись длины висит выше
              // бара (`top:-7px`), и `overflow:hidden` на внешнем контейнере срезал бы её.
              // Сама обрезка — вторая линия защиты к зажиму процентов в модели: если сегмент однажды
              // снова придёт шире длины файла, он обрежется по бару, а не полезет за габариты плитки
              // (живой симптом 2026-09-07).
              jsx30("div", {
                style: { position: "absolute", inset: 0, overflow: "hidden" },
                children: segs.map((s, i) => jsx30("div", {
                  key: `${s.op}-${i}`,
                  style: {
                    position: "absolute",
                    top: 0,
                    bottom: 0,
                    left: `${s.pct[0]}%`,
                    width: `${Math.max(s.pct[1] - s.pct[0], 0)}%`,
                    minWidth: "1px",
                    // даже однострочное чтение — видимый штрих (спека §4)
                    // Плотный оттенок, посчитанный заранее. Ни `opacity`, ни `mix-blend-mode`: см.
                    // segFill — смешанный цвет читался бы как состояние, которого нет.
                    backgroundColor: segFill(s.kind, s.rank, s.gray),
                    // Свежее — выше, без исключений для видов (см. segZ).
                    zIndex: segZ(s.rank)
                  }
                }))
              })
            ]
          }),
          // Длина — цветом имени: такая же характеристика файла. Не в кликабельной зоне.
          jsx30("div", {
            className: "shrink-0",
            style: { fontSize: "0.5rem", lineHeight: 1, color: "var(--ui-text-secondary)" },
            title: `${file.total} lines`,
            children: shortLength(file.total)
          })
        ]
      }),
      jsx30(OpIdsPopup, { file, pos: popupPos, onClose: onClosePopup, onPick: onPickOp })
    ]
  });
}
function FileContextPanel({ view, selectedOp, open, onToggle, onPickOp }) {
  const [height, setHeight] = useState9(DEFAULT_H);
  const [manual, setManual] = useState9(false);
  const [autoH, setAutoH] = useState9(DEFAULT_H);
  const gridRef = useRef5(null);
  const [localCollapsed, setLocalCollapsed] = useState9(false);
  const driven = typeof open === "boolean";
  const collapsed = driven ? !open : localCollapsed;
  const setCollapsed = driven ? () => onToggle && onToggle() : setLocalCollapsed;
  const dragState = useRef5(null);
  const [openOps, setOpenOps] = useState9(null);
  const { files, desync, order } = view || { files: [], desync: [], order: {} };
  const desyncPaths = useMemo(() => new Set((desync || []).map((d) => d.path)), [desync]);
  const grayed = useMemo(() => graySegments(files, order, selectedOp), [files, order, selectedOp]);
  function togglePopup(path, pos) {
    setOpenOps((cur) => cur && cur.path === path ? null : { path, pos });
  }
  useLayoutEffect3(() => {
    const el = gridRef.current;
    if (!el || manual) return;
    const measure = () => {
      const tile = el.firstElementChild;
      const row = tile ? tile.getBoundingClientRect().height + 2 : 30;
      const want = Math.ceil(el.getBoundingClientRect().height);
      const next = Math.min(want, Math.ceil(row * AUTO_ROWS + 4));
      setAutoH((prev) => Math.abs(prev - next) < 1 ? prev : next);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [manual, collapsed, files.length]);
  function onDragStart(e) {
    e.preventDefault();
    const startH = manual ? height : autoH;
    setHeight(startH);
    setManual(true);
    dragState.current = { startY: e.clientY, startH };
    const onMove = (ev) => {
      const st = dragState.current;
      if (!st) return;
      setHeight(Math.min(MAX_H, Math.max(MIN_H, st.startH + (ev.clientY - st.startY))));
    };
    const onUp = () => {
      dragState.current = null;
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }
  return jsxs27("div", {
    className: "flex items-stretch border-b border-(--ui-stroke-quaternary) shrink-0 select-none",
    style: CAPPED,
    children: [
      jsxs27("div", { className: "flex flex-col flex-1 min-w-0", style: CAPPED, children: [
        // Треугольник ПЕРВЫМ, то есть слева (владелец, 2026-09-09): обе панели — файловая и
        // полоса работы с контекстом — обязаны раскрываться с одной стороны, иначе жест
        // приходится искать заново у каждой. Отсюда и `gap` вместо `justify-between`: прижимать
        // подпись к правому краю больше незачем, она идёт следом за своим органом управления.
        jsxs27("div", { className: "flex items-center gap-1 px-1.5", style: { fontSize: "0.55rem", lineHeight: 1.2 }, children: [
          jsx30("button", {
            onClick: () => setCollapsed((c) => !c),
            type: "button",
            className: "cursor-pointer shrink-0",
            // Вдвое крупнее прежнего (владелец: «сейчас они очень очень маленькие»). Размер
            // задан отдельно от строки: подпись рядом остаётся мелкой, а орган управления
            // должен попадаться под курсор без прицеливания.
            // 0.8rem, не 1.2rem: заголовок зоны на треть ниже (Plan23-Task01, владелец 2026-09-29).
            style: { color: "var(--ui-text-quaternary)", fontSize: "0.8rem", lineHeight: 1, border: "none", background: "transparent", padding: "0 2px" },
            title: collapsed ? "expand files" : "collapse files",
            children: collapsed ? "\u25B8" : "\u25BE"
          }),
          // Легенда прямо в этой строке (владелец, 2026-09-08): цвет — единственный носитель
          // смысла на баре, и он обязан быть подписан там же, где виден. Слово окрашено ТЕМ ЖЕ
          // цветом, что даёт `segFill` свежайшей операции, — иначе легенда объясняла бы не то,
          // что нарисовано.
          jsxs27("span", { className: "text-(--ui-text-quaternary) truncate min-w-0 flex-1", children: [
            `context \xB7 ${files.length} files \xB7 `,
            jsx30("span", { style: { color: segFill("read", 0, false) }, children: "read" }),
            " ",
            jsx30("span", { style: { color: segFill("read_nested", 0, false) }, children: "exec" }),
            " ",
            jsx30("span", { style: { color: segFill("patch", 0, false) }, children: "patch" }),
            // Режим НАЗЫВАЕТ себя и говорит, как из него выйти (владелец, 2026-09-08).
            // «grey от 0012» не объясняло ни что произошло, ни почему всё посерело: человек не
            // включал никакого «grey» и слова этого нигде не видел. Теперь строка отвечает на оба
            // вопроса — что показано и что сделать, чтобы вернуть как было. С 2026-10-06 выбор —
            // открытое окно чипа в зеркале (`prompt-mirror.js::selectedOpOf`).
            selectedOp ? jsx30("span", {
              style: { color: "var(--ui-text-secondary)" },
              children: ` \xB7 shadowed at operation ${selectedOp} \u2014 close the popup to clear`
            }) : ""
          ] })
        ] }),
        // Бегунок — планка как у ленты (`.fops-scroll`: 8px, без скругления), только на 1px ближе
        // к правому краю (владелец, 2026-09-29). Трек УЖЕ, чем у ленты (13px, слева 1px): у ленты
        // слева от трека своё поле, а здесь сразу плитки со своим отступом сетки, и прежние 3px
        // прозрачного края плюс он давали широкую щель между плитками и планкой. Правило живёт
        // ЗДЕСЬ: панель стоит и над стаканом, и в зеркале, и должна выглядеть одинаково.
        collapsed ? null : jsx30("style", { children: `
          .fctx-scroll::-webkit-scrollbar { width: 13px; }
          .fctx-scroll::-webkit-scrollbar-track { background: transparent; }
          .fctx-scroll::-webkit-scrollbar-thumb {
            background-color: var(--ui-stroke-secondary);
            border-radius: 0;
            border-style: solid;
            border-color: transparent;
            border-width: 0 4px 0 1px;
            background-clip: padding-box;
          }
          .fctx-scroll::-webkit-scrollbar-thumb:hover { background-color: var(--ui-stroke-primary); }
        ` }),
        collapsed ? null : jsx30("div", {
          className: "fctx-scroll",
          style: Object.assign({ height: `${manual ? height : autoH}px`, overflowY: "auto", overflowX: "hidden" }, CAPPED),
          children: jsx30("div", {
            ref: gridRef,
            // Плитка шире на ~30% (74 → 96px, владелец 2026-09-07): имена файлов усекались слишком
            // рано. Зазор между плитками и внутренний отступ снизу — меньше: вертикаль в этой
            // панели дороже горизонтали, её отдают стакану.
            className: "grid gap-0.5 p-1",
            style: { gridTemplateColumns: "repeat(auto-fill, minmax(96px, 1fr))", alignContent: "start" },
            children: grayed.map((f) => jsx30(Tile, {
              key: f.path,
              file: Object.assign({}, f, { desync: desyncPaths.has(f.path) }),
              popupPos: openOps && openOps.path === f.path ? openOps.pos : null,
              onTogglePopup: togglePopup,
              onPickOp,
              onClosePopup: () => setOpenOps(null)
            }))
          })
        }),
        collapsed ? null : jsx30("div", {
          onMouseDown: onDragStart,
          title: "drag to resize",
          style: { height: "5px", cursor: "ns-resize", backgroundColor: "var(--ui-stroke-tertiary)", opacity: 0.35 }
        })
      ] }),
      // Правая зона кнопок (спека §3): режим один, кнопок в нём нет — зона схлопнута до нуля,
      // ширины не тратит. Зарезервирована под compress/compact/summarize следующим планом.
      jsx30("div", { style: { width: 0, flexShrink: 0 } })
    ]
  });
}

// desktop/src/mirror-compact-zone.js
import { jsx as jsx31, jsxs as jsxs28 } from "react/jsx-runtime";
import { useState as useState10, useEffect as useEffect4 } from "react";
var DEFAULT_CUT = { headKeep: 6, cutPercent: 30, tailKeep: 20 };
var SHARE_STEP = 5;
function CompactZone({ compact, addresses, thin }) {
  const [cutCfg, setCutCfg] = useState10(DEFAULT_CUT);
  const [share, setShare] = useState10(String(DEFAULT_CUT.cutPercent));
  useEffect4(() => {
    let cancelled = false;
    loadConfig().then((data) => {
      if (cancelled || !data || data.error) return;
      const c = data && data.compaction || {};
      const cfg = {
        headKeep: Number(c.head_keep),
        cutPercent: Number(c.cut_percent),
        tailKeep: Number(c.tail_keep)
      };
      const clean = {
        headKeep: Number.isFinite(cfg.headKeep) ? cfg.headKeep : DEFAULT_CUT.headKeep,
        cutPercent: Number.isFinite(cfg.cutPercent) ? cfg.cutPercent : DEFAULT_CUT.cutPercent,
        tailKeep: Number.isFinite(cfg.tailKeep) ? cfg.tailKeep : DEFAULT_CUT.tailKeep
      };
      setCutCfg(clean);
      setShare(String(clean.cutPercent));
    }).catch(() => {
    });
    return () => {
      cancelled = true;
    };
  }, []);
  function stepShare(dir) {
    const cur = Number(share);
    const base = Number.isFinite(cur) ? cur : cutCfg.cutPercent;
    const next = dir > 0 ? Math.floor(base / SHARE_STEP) * SHARE_STEP + SHARE_STEP : Math.ceil(base / SHARE_STEP) * SHARE_STEP - SHARE_STEP;
    setShare(String(Math.min(100, Math.max(0, next))));
  }
  function stepBtn(dir) {
    return jsx31("button", {
      type: "button",
      disabled: compact.mode,
      title: dir > 0 ? `+${SHARE_STEP}%` : `-${SHARE_STEP}%`,
      onClick: (e) => {
        e.stopPropagation();
        stepShare(dir);
      },
      className: "mirror-action-btn select-none",
      style: {
        flex: "1 1 0",
        minHeight: 0,
        padding: "0 0.2rem",
        lineHeight: 1,
        fontSize: "0.45rem",
        border: "1px solid var(--ui-stroke-secondary)",
        borderRadius: dir > 0 ? "0.2rem 0.2rem 0 0" : "0 0 0.2rem 0.2rem",
        background: "transparent",
        color: "var(--ui-text-secondary)",
        cursor: compact.mode ? "default" : "pointer",
        opacity: compact.mode ? 0.45 : 1
      },
      children: dir > 0 ? "\u25B2" : "\u25BC"
    });
  }
  function handleClick() {
    if (!compact.mode) {
      const pct = Number(share);
      armCompactFromServer(Number.isFinite(pct) && String(share).trim() !== "" ? pct : void 0);
      return;
    }
    setCompactMode(false);
  }
  const armed = !!compact.mode;
  const color = ACTION_COLOR.compact;
  return jsxs28("div", {
    className: thin ? "flex items-center px-1" : "flex items-center gap-2 px-2 py-1",
    style: Object.assign({ fontSize: "0.55rem" }, SELECTABLE),
    children: [
      jsx31("button", {
        onClick: (e) => {
          e.stopPropagation();
          if (!compact.busy) handleClick();
        },
        className: "mirror-action-btn rounded-full font-medium cursor-pointer select-none transition-colors duration-150 ease-out active:scale-95",
        disabled: !!compact.busy,
        style: Object.assign({
          minWidth: thin ? 0 : "5.5rem",
          textAlign: "center",
          border: `1px solid ${armed ? color : "var(--ui-stroke-secondary)"}`,
          background: armed ? `color-mix(in srgb, ${color} 14%, transparent)` : "transparent",
          color: armed ? color : "var(--ui-text-secondary)",
          opacity: compact.busy ? 0.5 : 1,
          whiteSpace: "nowrap"
        }, thin ? ACTION_BTN_THIN : ACTION_BTN),
        title: armed ? "close compaction mode" : "arm compaction: ticks the rail from the config numbers, then press Apply operation",
        // `compact GO` снят (Plan16-Task07): он был нужен, пока второе нажатие ПРИМЕНЯЛО, а
        // кнопка, называющаяся одинаково в двух смыслах, это «непонятно» (владелец,
        // 2026-09-23). Теперь применяет `Apply operation`, у кнопки снова один смысл — вход и
        // выход из режима, — и имя возвращается к правилу 2026-09-10: не меняется при взводе.
        children: "compact"
      }),
      thin ? null : jsxs28("label", {
        className: "flex items-center gap-1",
        style: SELECTABLE,
        onClick: (e) => e.stopPropagation(),
        children: [
          "cut %:",
          jsx31("input", {
            type: "text",
            inputMode: "numeric",
            value: share,
            disabled: compact.mode,
            onChange: (e) => setShare(e.currentTarget.value),
            className: "w-10 px-1 rounded",
            style: { border: "1px solid var(--ui-stroke-secondary)", backgroundColor: "var(--ui-bg-tertiary)" }
          }),
          jsxs28("span", {
            style: { display: "flex", flexDirection: "column", alignSelf: "stretch", gap: 0 },
            children: [stepBtn(1), stepBtn(-1)]
          })
        ]
      }),
      // Счётчик «N ticked» снят (владелец, 2026-09-28): число отмеченных стоит под крестиком
      // рельсы.
      thin ? null : jsx31("div", { className: "flex-1 min-w-0" })
      // Модалка подтверждения переехала в `prompt-mirror.js`: она встаёт рядом с `Apply
      // operation`, а координаты этой кнопки знает только шапка зеркала (Plan16-Task08).
    ]
  });
}

// desktop/src/confirm-modal.js
import { jsx as jsx32, jsxs as jsxs29 } from "react/jsx-runtime";
var KEYFRAMES = `
  @keyframes confirm-modal-appear {
    from { opacity: 0; transform: translateY(4px) scale(0.98); }
    to   { opacity: 1; transform: translateY(0) scale(1); }
  }
  .confirm-modal-appear { animation: confirm-modal-appear 220ms ease-out both; will-change: opacity, transform; }
  @keyframes confirm-modal-spin { to { transform: rotate(360deg); } }
  .confirm-modal-spin { animation: confirm-modal-spin 1s linear infinite; will-change: transform; }
  @keyframes confirm-modal-sweep {
    from { transform: translateX(-120%); }
    to   { transform: translateX(220%); }
  }
  .confirm-modal-sweep { animation: confirm-modal-sweep 1.8s linear infinite; will-change: transform; }
`;
var ANCHOR_GAP_PX = 14;
var ANCHOR_WIDTH_REM = 20;
var EDGE_PX = 8;
function ConfirmModal({ open, title, body, busy, progress, onConfirm, onCancel, anchor, canConfirm = true, actions, extra, widthRem }) {
  if (!open) return null;
  const anchored = anchor && Number.isFinite(anchor.x) && Number.isFinite(anchor.y);
  const w = Number(widthRem) > 0 ? Number(widthRem) : ANCHOR_WIDTH_REM;
  const place = anchored ? {
    position: "absolute",
    width: `${w}rem`,
    minWidth: 0,
    left: `clamp(${EDGE_PX}px, calc(${anchor.x}px - ${w / 2}rem), calc(100vw - ${w}rem - ${EDGE_PX}px))`,
    top: `min(${anchor.y + ANCHOR_GAP_PX}px, calc(100vh - 12rem))`
  } : {};
  return jsxs29("div", {
    // stopPropagation on the whole overlay: every other click-to-cancel listener in this
    // panel (drop-mode, compact-mode, popups) reacts to ANY document click, and a click on
    // the modal's own backdrop must not fall through to them and cancel the arm underneath.
    onClick: (e) => e.stopPropagation(),
    style: {
      position: "fixed",
      inset: 0,
      // ВЫШЕ ВСЕГО, что панель умеет поднимать (правка 2026-09-23). Было 50 — ровно столько
      // же, сколько у раскрытой всплывашки сообщения (`mirror-primitives.js`, 50/51), и на
      // живой проверке она накрывала модалку: вопрос виден, а нажать нельзя. Подсказки и
      // оверлеи в панели живут на 999, поэтому модалка садится над ними.
      zIndex: 1e3,
      display: anchored ? "block" : "flex",
      alignItems: "center",
      justifyContent: "center",
      // Затемнение как было: жалоба владельца была НЕ про него (2026-09-23, поправила меня —
      // «я ничего не говорила про затемнение»). Просвечивала сама КАРТОЧКА, см. ниже.
      backgroundColor: "rgba(0,0,0,0.45)"
    },
    children: [
      jsx32("style", { dangerouslySetInnerHTML: { __html: KEYFRAMES } }),
      jsxs29("div", {
        className: "confirm-modal-appear rounded-md p-4 flex flex-col gap-2",
        style: {
          // АЛЬФА С ФОНА СНЯТА ПРИНУДИТЕЛЬНО (владелец, 2026-09-23: «модалка должна иметь
          // прозрачность 0, сейчас сквозь неё видно текст»). `var(--ui-bg-secondary)` — токен
          // ХОСТА, и он несёт собственную альфу < 1; карточка наследовала её и просвечивала
          // разговор под собой.
          //
          // Ровно та же грабля и ровно то же лекарство, что у подложек зеркала:
          // `oklch(from var(...) l c h)` — relative-color-syntax без альфа-канала
          // (`mirror-primitives.js::opaque`, заведён по этому же поводу 2026-09-15, её слова
          // тогда: «нужно прозрачности обоих цветов присвоить 0»). Помощник импортируется, а
          // не переписывается: второй экземпляр этого знания однажды разойдётся с первым.
          // ОБЫЧНЫЙ ФОН, А НЕ АКЦЕНТ (владелец, 2026-09-23). `--ui-bg-secondary` оказался
          // ярким акцентным цветом, который хост и глушил той самой альфой — сняв её, мы
          // получили честно непрозрачное, но кричащее пятно. Берём ту же базу, на которой
          // сидит вся наша панель (`SURFACE_BASE`, он же `--ui-editor-surface-background`),
          // и делаем непрозрачной её.
          backgroundColor: opaque(SURFACE_BASE),
          border: "1px solid var(--ui-stroke-secondary)",
          minWidth: "18rem",
          maxWidth: "26rem",
          ...place
        },
        children: [
          jsx32("div", { className: "text-sm font-semibold", style: SELECTABLE, children: title }),
          jsx32("div", { className: "text-xs text-(--ui-text-secondary)", style: SELECTABLE, children: body }),
          extra || null,
          busy ? jsxs29("div", { className: "flex flex-col gap-1 mt-1", children: [
            jsx32("div", {
              className: "h-2 w-full rounded-full overflow-hidden relative",
              style: { backgroundColor: "var(--ui-stroke-quaternary)" },
              children: jsx32("div", {
                className: "confirm-modal-sweep absolute inset-y-0 w-1/3",
                style: { background: "linear-gradient(90deg, transparent, var(--dt-primary), transparent)" }
              })
            }),
            // Строка сообщений/процент — место под индикатор хода (задел, п.7 задачи):
            // данных для него ещё нет, значит и текста конкретнее "running…" тоже нет.
            jsx32("div", {
              className: "text-[0.65rem] text-(--ui-text-quaternary)",
              style: SELECTABLE,
              children: progress || "running in the background\u2026"
            })
          ] }) : null,
          // РАСКЛАДКА КНОПОК — НАРОЧНО НЕ ПРИВЫЧНАЯ (владелец, 2026-09-22/23).
          //
          // Подтверждение — В ЦЕНТРЕ, закрытие — в правом НИЖНЕМ углу. Причина не в
          // эстетике: правка необратима, а кнопка на привычном месте (справа внизу, где у
          // всех «ОК») нажимается рефлекторно, как «ладно, закрыть». Пусть подтверждение
          // требует секунды внимания, а рефлекс уводит в безопасное.
          //
          // Крестика наверху НЕТ намеренно: у модалки нет титульной полосы, и крестик был бы
          // цитатой из окна, которого здесь не существует (владелец: «этож модалка»).
          //
          // Сетка из трёх колонок, а не `justify-between`: центр должен быть центром ОКНА, а
          // не серединой между двумя кнопками разной ширины.
          Array.isArray(actions) && actions.length ? jsx32("div", {
            className: "mt-3 flex items-center justify-center gap-2 flex-wrap",
            children: actions.map((a) => jsx32("button", {
              disabled: !!busy || !canConfirm,
              onClick: a.onClick,
              className: `${a.main ? "px-4 py-1.5 text-sm font-semibold" : "px-2.5 py-1 text-xs font-medium"} rounded-md cursor-pointer select-none`,
              style: {
                border: "1px solid var(--ui-red)",
                color: "var(--ui-red)",
                opacity: canConfirm ? 1 : 0.35,
                whiteSpace: "nowrap"
              },
              children: a.label
            }, a.key || a.label))
          }, "actions") : null,
          jsxs29("div", {
            className: "mt-3 grid items-center gap-2",
            style: { gridTemplateColumns: "1fr auto 1fr" },
            children: [
              jsx32("div", {}),
              Array.isArray(actions) && actions.length ? jsx32("div", {}) : jsxs29("button", {
                disabled: !!busy || !canConfirm,
                onClick: onConfirm,
                className: "px-4 py-1 rounded-md text-xs font-medium cursor-pointer select-none flex items-center gap-1.5 justify-center",
                style: { border: "1px solid var(--ui-red)", color: "var(--ui-red)", opacity: canConfirm ? 1 : 0.35 },
                children: [
                  busy ? jsx32("div", {
                    className: "w-2.5 h-2.5 rounded-full confirm-modal-spin",
                    style: { border: "2px solid var(--ui-stroke-secondary)", borderTopColor: "var(--ui-red)" }
                  }) : null,
                  "confirm"
                ]
              }),
              jsx32("div", { className: "flex justify-end", children: jsx32("button", {
                disabled: !!busy,
                onClick: onCancel,
                className: "px-3 py-1 rounded-md text-xs cursor-pointer select-none",
                style: { border: "1px solid var(--ui-stroke-secondary)" },
                children: "close"
              }) })
            ]
          })
        ]
      })
    ]
  });
}

// desktop/src/config-tools-table.js
import { jsx as jsx33, jsxs as jsxs30 } from "react/jsx-runtime";
import { useState as useState11, useLayoutEffect as useLayoutEffect4, useRef as useRef6 } from "react";
function InfoLabel({ id, text, info, openInfo, setOpenInfo, className }) {
  const open = openInfo === id;
  const popRef = useRef6(null);
  const [flipLeft, setFlipLeft] = useState11(false);
  useLayoutEffect4(() => {
    if (!open) {
      setFlipLeft(false);
      return;
    }
    const el = popRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    if (rect.right > window.innerWidth) setFlipLeft(true);
  }, [open]);
  return jsxs30("span", { className: "relative inline-flex min-w-0 flex-1", children: [
    jsx33("span", {
      key: "label",
      onClick: (e) => {
        e.stopPropagation();
        setOpenInfo(open ? null : id);
      },
      className: (className || "") + " cursor-pointer",
      style: SELECTABLE,
      children: text
    }),
    open ? jsx33("div", {
      key: "pop",
      ref: popRef,
      onClick: (e) => e.stopPropagation(),
      className: "absolute z-20 rounded-md p-2 text-[0.65rem] leading-snug whitespace-pre-wrap",
      style: Object.assign(
        { top: "100%", marginTop: "3px", width: "220px", maxWidth: "80vw", backgroundColor: "var(--ui-bg-elevated)", border: "1px solid var(--ui-stroke-secondary)", boxShadow: "0 4px 12px rgba(0,0,0,0.35)" },
        flipLeft ? { right: 0 } : { left: 0 },
        SELECTABLE
      ),
      children: info || "\u2014"
    }) : null
  ] });
}
function sectionHead(text, infoId, info, openInfo, setOpenInfo) {
  return jsx33("div", { className: "px-1 pt-1 pb-0.5", children: jsx33(InfoLabel, {
    id: infoId,
    info,
    openInfo,
    setOpenInfo,
    className: "text-[0.65rem] font-semibold uppercase tracking-wide text-(--ui-text-quaternary)",
    text
  }) });
}
var SELECT_STYLE = { backgroundColor: "var(--ui-bg-elevated)", border: "1px solid var(--ui-stroke-secondary)", color: "var(--ui-text-primary)", height: "18px", margin: 0 };
var CHECKBOX_STYLE = { accentColor: "var(--dt-primary)", width: "14px", height: "14px" };
function disableRow(name, info, checked, onChange, openInfo, setOpenInfo) {
  return jsxs30("div", {
    className: "flex items-center justify-between gap-2 pl-1 pr-1 py-0 text-[10px] transition-colors duration-100 ease-out hover:bg-(--ui-row-hover-background)",
    style: { borderRight: "1px solid var(--ui-stroke-quaternary)" },
    children: [
      // Hard clip, no ellipsis — the owner explicitly wants letters cut off, not "...": that
      // reads as "still complete, just tight" instead of announcing a truncation.
      jsx33(InfoLabel, { id: "dis-" + name, text: name, info, openInfo, setOpenInfo, className: "block w-full overflow-hidden whitespace-nowrap" }),
      jsxs30("label", { className: "flex items-center gap-1 shrink-0 text-(--ui-text-quaternary) cursor-pointer", style: SELECTABLE, children: [
        jsx33("span", { style: SELECTABLE, children: "disable" }),
        jsx33("input", { type: "checkbox", checked, onChange: (e) => onChange(e.currentTarget.checked), style: CHECKBOX_STYLE })
      ] })
    ]
  }, name);
}
function paramControl(toolKey, param, onChange, openInfo, setOpenInfo) {
  const id = "param-" + toolKey + "-" + param.key;
  let control;
  if (param.type === "bool") {
    control = jsx33("input", { type: "checkbox", checked: param.value, onChange: (e) => onChange(e.currentTarget.checked), style: CHECKBOX_STYLE });
  } else if (param.type === "number") {
    control = jsx33("input", {
      type: "text",
      inputMode: "numeric",
      maxLength: 3,
      value: String(param.value),
      onChange: (e) => {
        const digits = e.currentTarget.value.replace(/\D/g, "").slice(0, 3);
        onChange(digits === "" ? 0 : Number(digits));
      },
      className: "w-8 px-1 rounded text-[10px] text-center outline-none",
      style: SELECT_STYLE
    });
  } else if (param.type === "enum") {
    control = jsx33("select", {
      value: param.value,
      onChange: (e) => onChange(e.currentTarget.value),
      className: "rounded text-[10px] outline-none",
      style: SELECT_STYLE,
      children: (param.options || []).map((o) => jsx33("option", { value: o, children: o }, o))
    });
  } else {
    control = jsx33("span", { className: "text-[0.7rem] text-(--ui-text-quaternary)", style: SELECTABLE, children: String(param.value) });
  }
  return jsxs30("div", { className: "flex items-center justify-between gap-2 w-full", children: [
    jsx33(InfoLabel, { id, text: param.key, info: param.help, openInfo, setOpenInfo, className: "text-[10px]" }),
    control
  ] }, param.key);
}
function paramsEditor(toolKey, params, onChange, openInfo, setOpenInfo) {
  if (!params.length) return jsx33("span", { className: "text-[0.7rem] text-(--ui-text-quaternary)", style: SELECTABLE, children: "\u2014" });
  return jsx33("div", {
    className: "flex flex-col gap-0.5 w-full",
    children: params.map((p) => paramControl(toolKey, p, (value) => onChange(p.key, value), openInfo, setOpenInfo))
  });
}
function pathSelect(key, value, onChange) {
  return jsx33("select", {
    value,
    onChange: (e) => onChange(e.currentTarget.value),
    className: "rounded text-[10px] outline-none",
    style: SELECT_STYLE,
    children: ["old", "new"].map((o) => jsx33("option", { value: o, children: o }, o))
  });
}
var COL_PX = { tool: 100, params: 120, enabled: 60, path: 70 };
function toolTableRow(key, state, onChange, showParams, showPath, hoverKey, setHoverKey) {
  const onEnter = () => setHoverKey(key);
  const onLeave = () => setHoverKey(null);
  const cellStyle = (extra) => Object.assign(
    { display: "flex", alignItems: "flex-start", borderBottom: "1px solid var(--ui-stroke-quaternary)", backgroundColor: hoverKey === key ? "var(--ui-row-hover-background)" : "transparent" },
    extra
  );
  const cells2 = [
    jsx33("div", {
      className: "px-1 py-0.5 min-w-0 text-[10px]",
      style: cellStyle(),
      onMouseEnter: onEnter,
      onMouseLeave: onLeave,
      // text-[0.8rem]: text-sm (0.875rem) was competing with the section header's own text-xs
      // for visual weight; dialed back a touch while keeping the added font-semibold.
      children: jsx33(InfoLabel, { id: "slot-" + key, text: key, info: state.help, openInfo: state.openInfo, setOpenInfo: state.setOpenInfo, className: "text-[10px] font-semibold truncate" })
    }, key + "-name")
  ];
  if (showParams) {
    cells2.push(jsx33("div", {
      className: "px-1 py-0.5",
      onMouseEnter: onEnter,
      onMouseLeave: onLeave,
      style: cellStyle({ borderRight: "1px solid var(--ui-stroke-quaternary)" }),
      children: paramsEditor(
        key,
        state.params,
        (paramKey, value) => onChange({ params: state.params.map((p) => p.key === paramKey ? Object.assign({}, p, { value }) : p) }),
        state.openInfo,
        state.setOpenInfo
      )
    }, key + "-params"));
  }
  cells2.push(jsx33("div", {
    className: "px-1 py-0.5",
    style: cellStyle({ justifyContent: "center" }),
    onMouseEnter: onEnter,
    onMouseLeave: onLeave,
    children: jsx33("span", { className: "inline-flex items-center", children: jsx33("input", { type: "checkbox", checked: state.enabled, onChange: (e) => onChange({ enabled: e.currentTarget.checked }), style: CHECKBOX_STYLE }) })
  }, key + "-enabled"));
  if (showPath) {
    cells2.push(jsx33("div", {
      className: "px-1 py-0.5 text-[10px]",
      style: cellStyle(),
      onMouseEnter: onEnter,
      onMouseLeave: onLeave,
      children: pathSelect(key, state.path, (v) => onChange({ path: v }))
    }, key + "-path"));
  }
  return cells2;
}

// desktop/src/prompt-mirror.js
var CONFIRM_CHECKBOX = Object.assign({}, CHECKBOX_STYLE, { width: "12px", height: "12px", cursor: "pointer" });
var CONFIRM_HINT = "text-[0.65rem] leading-snug text-(--ui-text-quaternary)";
var TAIL_SPACE = "40%";
var FOLLOW_PAUSE_MS = 6e4;
var FOLLOW_BOTTOM_PX = 40;
var EXPORT_PAINT = {
  border: ACTION_COLOR.export,
  fill: railFill(ACTION_COLOR.export),
  active: (row) => !!row.msgAddr
};
var ADDR_PAINT = {
  content: {
    border: ACTION_COLOR.content,
    fill: railFill(ACTION_COLOR.content),
    active: (row) => !!row.result && !!row.msgAddr
  },
  erase: {
    border: ACTION_COLOR.erase,
    fill: railFill(ACTION_COLOR.erase),
    active: (row) => !!row.msgAddr
  }
};
var LLMOPS_PAINT = {
  border: ACTION_COLOR.llmops,
  fill: railFill(ACTION_COLOR.llmops),
  active: (row) => !!row.msgAddr
};
var COMPACT_PAINT = {
  border: ACTION_COLOR.compact,
  fill: railFill(ACTION_COLOR.compact),
  active: (row) => !!row.msgAddr
};
function pairToolCalls(items) {
  const consumed = /* @__PURE__ */ new Set();
  const pairsByIdx = /* @__PURE__ */ new Map();
  items.forEach((row, idx) => {
    if (row.kind !== "msg") return;
    const fact = row.fact || {};
    if (String(fact.role) !== "assistant") return;
    const info = Array.isArray(fact.call_info) ? fact.call_info : [];
    if (!info.length) return;
    const pairs = info.map((call, c) => {
      const id = call && call.id || "";
      let answer = null;
      if (id) {
        for (let j = idx + 1; j < items.length; j++) {
          if (items[j].kind !== "msg" || consumed.has(j)) continue;
          const cf = items[j].fact || {};
          if (String(cf.role) === "tool" && String(cf.tool_call_id || "") === id) {
            answer = items[j];
            consumed.add(j);
            break;
          }
        }
      }
      return {
        kind: "toolpair",
        key: `pair-${row.key}-${c}`,
        stepKey: row.key,
        name: call && call.name || "\u0442\u0443\u043B",
        target: call && call.target || "",
        answer
      };
    });
    pairsByIdx.set(idx, pairs);
  });
  const out = [];
  items.forEach((row, idx) => {
    if (consumed.has(idx)) return;
    const fact = row.fact || {};
    if (row.kind === "msg" && String(fact.role) === "tool") {
      out.push({
        kind: "toolpair",
        key: `lone-${row.key}`,
        stepKey: `lone-${row.key}`,
        name: fact.name || "\u0442\u0443\u043B",
        target: "",
        answer: row
      });
      return;
    }
    out.push(row);
    const pairs = pairsByIdx.get(idx);
    if (pairs) pairs.forEach((p) => out.push(p));
  });
  return out;
}
function chipKeyOfOp(op, groups, ops) {
  if (!op) return [null, false];
  const find = (test) => {
    for (const g of groups || []) {
      const hit = pairToolCalls(g.items).find((r) => r.kind === "toolpair" && r.answer && test(r.answer));
      if (hit) return hit.key;
    }
    return null;
  };
  const own = find((a) => a.fact && a.fact.op === op);
  if (own) return [own, false];
  const fact = (ops || []).find((o) => o && o.op === op && o.host_msg);
  const host3 = fact ? find((a) => a.address === fact.host_msg) : null;
  return host3 ? [host3, true] : [null, false];
}
function selectedOpOf(popup, groups) {
  const id = popup && popup.id;
  if (!id) return null;
  for (const g of groups || []) {
    const hit = pairToolCalls(g.items).find((r) => r.kind === "toolpair" && r.key === id);
    if (hit) return hit.answer && hit.answer.fact && hit.answer.fact.op || null;
  }
  return null;
}
function buildCarriageGroups(rows) {
  const groups = [];
  let current = null;
  rows.forEach((row) => {
    if (row.kind === "carriage") {
      current = { header: row, items: [] };
      groups.push(current);
    } else if (row.kind === "orphan") {
      current = null;
    } else if (current) {
      current.items.push(row);
    }
  });
  return groups;
}
function buildCarriage(group, popup, setPopup, stale, compact, row, ledgerByMsg, durations) {
  const header = group.header;
  const opener = group.items.find((r) => r.kind === "msg" && r.opener);
  const rest = opener ? group.items.filter((r) => r !== opener) : group.items;
  const railEntries = [{ kind: "header", pickable: false }];
  if (opener) {
    railEntries.push({
      kind: "opener",
      pickable: false,
      tone: opener.fact ? rowTone(opener.fact) : toneOf("user"),
      msgAddr: opener.address || ""
    });
  }
  let prevKind = null;
  const restNodes = pairToolCalls(rest).map((row2) => {
    const gapBefore = row2.kind === "toolpair" && prevKind === "msg";
    prevKind = row2.kind;
    let node;
    if (row2.kind === "step") {
      node = jsx34(StepBar, { row: row2 });
      railEntries.push({ kind: "step", pickable: false });
    } else if (row2.kind === "toolpair") {
      const callId = String(row2.answer && row2.answer.fact && row2.answer.fact.tool_call_id || "");
      const answerAddr = row2.answer && row2.answer.address || "";
      node = jsx34(ToolPairBrick, {
        row: row2,
        popup,
        setPopup,
        stale,
        ledger: answerAddr && ledgerByMsg && ledgerByMsg.get(answerAddr) || null,
        duration: callId && durations && durations[callId]
      });
      const afact = row2.answer && row2.answer.fact || {};
      const realOp = afact.op || "";
      railEntries.push({
        kind: "brick",
        pickable: !!realOp,
        op: realOp,
        result: true,
        toolId: afact.tool_call_id || "",
        stepKey: row2.stepKey,
        gapBefore,
        tone: toneOf("tool"),
        // Адрес ОТВЕТА, не вызова — вызов уже свой lead-in-строкой выше (см. `isEmptyLeadIn`
        // ниже), и оба даются компакции независимыми ячейками.
        msgAddr: row2.answer && row2.answer.address || ""
      });
    } else if (isEmptyLeadIn(row2)) {
      node = null;
      railEntries.push({ kind: "empty", pickable: false, gapBefore, msgAddr: row2.address || "" });
    } else {
      node = jsx34(MessageNode, { row: row2, popup, setPopup });
      railEntries.push({
        kind: "brick",
        pickable: false,
        gapBefore,
        tone: rowTone(row2.fact),
        msgAddr: row2.address || ""
      });
    }
    return jsx34("div", { style: gapBefore ? { marginTop: "0.125rem" } : void 0, children: node }, row2.key);
  });
  const railRows = buildRailRows(railEntries);
  const vagon = jsxs31("div", {
    // Ширина — ПО СОДЕРЖИМОМУ (`fit-content`), не фиксированный процент (владелец,
    // 2026-09-15, второй заход: «вот как раз после этого номера [адреса] и должна сразу
    // заканчиваться ширина этого прямоугольника... но он продолжается направо дальше пока не
    // займёт всю ширину» — колонки строки (`BrickRow`) и так фиксированы, `fit-content`
    // просто обводит их по факту, не гадая с процентом). `maxWidth` — потолок на случай
    // непредвиденно длинного адреса/бейджа. Раньше здесь стоял тот же `width` в объекте, что
    // и в `CAPPED` (`width: '100%'`) — `Object.assign` идёт по порядку, и `CAPPED` шёл
    // ВТОРЫМ, поэтому его `100%` тихо перезаписывал явную ширину до неё — оттого вагон и
    // расползался на всю ширину стакана, несмотря на явно заданный процент.
    //
    // БЕЗ `maxWidth` (был потолок `70%`) и С `overflow: 'hidden'` — живая правка: при сужении
    // панели ниже потолка колонки-таблицы (`BrickRow`, фиксированная сетка) сжаться не могут,
    // контейнер оказывался ýже своего содержимого, и рамка визуально резалась поверх
    // вылезающих кирпичиков. `fit-content` без верхнего предела просто отдаёт контейнеру
    // естественную ширину его строк; если панель у́же этого — обрезает СВОЙ собственный край
    // чисто (`overflow: hidden`), а не рисует границу поверх чужого содержимого.
    className: "rounded-md border border-(--ui-stroke-secondary)",
    "data-mirror-wagon": "",
    style: Object.assign({}, CAPPED, {
      width: "fit-content",
      overflow: "hidden",
      backgroundColor: VAGON_BG,
      gridColumn: 1,
      gridRow: row
    }),
    children: [
      // Шапка вагона — сетка строк (`BrickHead`): `Turn …` слева, `op ID` над номерами операций,
      // `message ID` над адресами (владелец, 2026-09-23 и 2026-09-29). Адрес — наша система
      // координат, но по одному виду `m001f` этого не угадать.
      jsx34("div", {
        className: "px-2 pt-1",
        style: { height: ROW_HEIGHT2.header, boxSizing: "border-box" },
        // «carriage» → «Turn» (владелец, 2026-09-15).
        children: jsx34(BrickHead, {
          left: `Turn ${header.index} \xB7 ${header.count} msg`,
          opLabel: "op ID",
          addrLabel: "message ID",
          addrTitle: "the address of each message \u2014 what you name in /ctx-compact and see in the run report",
          height: "100%"
        })
      }),
      opener ? jsx34("div", {
        className: "flex justify-end items-center px-2 pt-1",
        style: { height: ROW_HEIGHT2.opener, boxSizing: "border-box" },
        children: jsx34(MessageNode, { row: opener, popup, setPopup })
      }) : null,
      // `pb-1`, БЕЗ `pt-1` (Таск 10a) — верхний паддинг этой обёртки не был бы учтён в
      // `railEntries` (там нет строки под «просто отступ»), и первая строка внутри
      // съехала бы вниз относительно своей ячейки на рельсе на эту величину. Нижний
      // паддинг безопасен — он ПОСЛЕ последней строки, ничьей ячейки не сдвигает.
      jsx34("div", { className: "flex flex-col px-2 pb-1 gap-0", children: restNodes })
    ]
  }, `${group.header.key}-vagon`);
  return { vagon, railRows };
}
function ImportBody({ imp }) {
  const check = imp.check;
  const fresh = check && check.folder === imp.folder;
  const roles = fresh && check.roles ? Object.keys(check.roles).map((k2) => `${k2} ${check.roles[k2]}`).join(" \xB7 ") : "";
  const problems = fresh && Array.isArray(check.problems) ? check.problems : [];
  return jsxs31("div", {
    className: "flex flex-col gap-2",
    children: [
      jsx34("div", { children: "Adds the folder's messages before (head) or after (tail) this chat's context, or replaces the WHOLE context with them. Order is the file number. Irreversible." }),
      jsxs31("div", {
        className: "flex items-center gap-1",
        children: [
          jsx34("input", {
            type: "text",
            value: imp.folder || "",
            placeholder: "export snapshot folder \u2014 or press Ctrl+V",
            onChange: (e) => setImportFolder(e.target.value),
            onKeyDown: (e) => {
              if (e.key === "Enter") checkImport();
            },
            style: {
              flex: "1 1 auto",
              minWidth: 0,
              fontSize: "0.65rem",
              padding: "0.15rem 0.3rem",
              border: "1px solid var(--ui-stroke-secondary)",
              borderRadius: "0.25rem",
              background: "transparent",
              color: "var(--ui-text-primary)"
            }
          }, "folder"),
          jsx34("button", {
            type: "button",
            disabled: !!imp.checking || !String(imp.folder || "").trim(),
            onClick: (e) => {
              e.stopPropagation();
              checkImport();
            },
            className: "rounded-md cursor-pointer select-none",
            style: {
              border: "1px solid var(--ui-stroke-secondary)",
              padding: "0.15rem 0.5rem",
              fontSize: "0.65rem",
              whiteSpace: "nowrap",
              opacity: imp.checking ? 0.5 : 1
            },
            children: imp.checking ? "checking\u2026" : "check"
          }, "check")
        ]
      }, "row"),
      fresh ? jsx34("div", {
        style: { color: check.ok ? "var(--ui-green)" : "var(--ui-red)", whiteSpace: "pre-wrap" },
        children: check.error ? String(check.error) : `${check.files || 0} files \u2192 ${check.messages || 0} messages` + (roles ? ` (${roles})` : "") + (problems.length ? `
${problems.length} problem(s):
` + problems.slice(0, 8).join("\n") + (problems.length > 8 ? `
\u2026 and ${problems.length - 8} more` : "") : `
You gonna import: ${check.messages || 0} messages`)
      }, "result") : null,
      imp.note && imp.ok === false ? jsx34("div", { style: { color: "var(--ui-red)" }, children: imp.note }, "note") : null
    ]
  });
}
function PromptMirror() {
  const view = useView();
  const st = useMessages();
  const tok = useTokens();
  useEffect5(() => {
    loadMessages();
  }, [view.sessionId]);
  const [popup, setPopup] = useState12(null);
  const exp = useExportRail();
  const addrOp = useAddrOp();
  const llmops = useLLMOpsRail();
  const llmopsWin = useLLMOpsWin();
  const [applyAt, setApplyAt] = useState12(null);
  const [dropConfirm, setDropConfirm] = useState12(false);
  const imp = useImportOp();
  const [importAt, setImportAt] = useState12(null);
  const dragRef = useRef7(null);
  const tapeRef = useRef7(null);
  const headRef = useRef7(null);
  const [railBoxes, setRailBoxes] = useState12({});
  function railDrag(key, read, write) {
    function mark(id, add) {
      const marks = read();
      if (!!marks[id] === add) return;
      const next = Object.assign({}, marks);
      if (add) next[id] = true;
      else delete next[id];
      write(next);
    }
    return {
      onDown: (id) => {
        const add = !read()[id];
        dragRef.current = { key, mode: add ? "add" : "remove" };
        mark(id, add);
      },
      onEnter: (id) => {
        const drag = dragRef.current;
        if (!drag || drag.key !== key) return;
        mark(id, drag.mode === "add");
      }
    };
  }
  const readExport = () => ($exportRail.get() || {}).picked || {};
  const readAddrOp = () => ($addrOp.get() || {}).picked || {};
  const readLLMOps = () => ($llmopsRail.get() || {}).picked || {};
  useEffect5(() => {
    const up = () => {
      dragRef.current = null;
    };
    document.addEventListener("mouseup", up);
    return () => document.removeEventListener("mouseup", up);
  }, []);
  useEffect5(() => {
    if (!popup) return;
    const clear = () => setPopup(null);
    document.addEventListener("click", clear);
    return () => document.removeEventListener("click", clear);
  }, [popup]);
  const fileContext = useFileContext();
  const ctxView = useMemo2(() => buildContextView(liveContextOps(fileContext.ops)), [fileContext.ops]);
  const ledgerByMsg = useMemo2(() => {
    const map = /* @__PURE__ */ new Map();
    (fileContext.ops || []).forEach((f) => {
      if (f && f.msg) map.set(String(f.msg), f);
    });
    return map;
  }, [fileContext.ops]);
  const durations = useLiveDurations();
  const compact = useCompact();
  const compactRun = useCompactRun();
  useEffect5(() => {
    if (!compact.mode) return;
    document.addEventListener("mouseup", compactMouseUp);
    const onKey = (e) => {
      if (e.key === "Escape") setCompactMode(false);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mouseup", compactMouseUp);
      document.removeEventListener("keydown", onKey);
    };
  }, [compact.mode]);
  useEffect5(() => {
    if (!exp.mode) return;
    const onKey = (e) => {
      if (e.key === "Escape") setExportMode(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [exp.mode]);
  useEffect5(() => {
    if (!addrOp.mode) return;
    const onKey = (e) => {
      if (e.key === "Escape") setAddrOpMode(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [addrOp.mode]);
  useEffect5(() => {
    if (!llmops.mode) return;
    const onKey = (e) => {
      if (e.key === "Escape") setLLMOpsMode(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [llmops.mode]);
  useEffect5(() => {
    if (compact.confirming) setPopup(null);
  }, [compact.confirming]);
  const zones = useZones();
  useEffect5(() => {
    loadZones();
  }, []);
  const [lastReport, setLastReport] = useState12("");
  const [jump, setJump] = useState12(null);
  useEffect5(() => {
    if (addrOp.note) setLastReport("addr");
  }, [addrOp.note]);
  useEffect5(() => {
    if (compact.note) setLastReport("compact");
  }, [compact.note]);
  useEffect5(() => {
    if (exp.note) setLastReport("export");
  }, [exp.note]);
  useEffect5(() => {
    if (imp.note) setLastReport("import");
  }, [imp.note]);
  useEffect5(() => {
    if (llmops.note) setLastReport("llmops");
  }, [llmops.note]);
  const { rows, placed, orphans } = layoutMessages(st.carriages, st.messages);
  const total = Array.isArray(st.messages) ? st.messages.length : 0;
  const foreign = rows.filter((r) => r.foreign).length;
  useLayoutEffect5(() => {
    const tape = tapeRef.current;
    if (!tape) return;
    const measure = () => {
      const head2 = headRef.current;
      const origin = head2 ? head2.getBoundingClientRect().left : 0;
      const boxes = {};
      tape.querySelectorAll("[data-rail-body]").forEach((el) => {
        const r = el.getBoundingClientRect();
        boxes[el.getAttribute("data-rail-body")] = { left: Math.round(r.left - origin), width: Math.round(r.width) };
      });
      const sig = JSON.stringify(boxes);
      setRailBoxes((prev) => JSON.stringify(prev) === sig ? prev : boxes);
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(tape);
    if (headRef.current) ro.observe(headRef.current);
    return () => ro.disconnect();
  }, [exp.mode, compact.mode, addrOp.mode, llmops.mode, rows.length]);
  const scrollRef = useRef7(null);
  const follow = useRef7({ sid: null, n: 0, top: 0, pausedAt: 0 });
  useEffect5(() => {
    const f = follow.current;
    const n2 = rows.length;
    if (f.sid !== view.sessionId || f.n === 0) {
      f.sid = view.sessionId;
      f.n = n2;
      return;
    }
    const grew = n2 > f.n;
    f.n = n2;
    const el = scrollRef.current;
    if (!grew || !el || Date.now() - f.pausedAt < FOLLOW_PAUSE_MS) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [rows.length, view.sessionId]);
  function onTapeScroll(e) {
    const f = follow.current;
    const el = e.currentTarget;
    const top = el.scrollTop;
    if (top < f.top - 2) f.pausedAt = Date.now();
    else if (el.scrollHeight - top - el.clientHeight < FOLLOW_BOTTOM_PX) f.pausedAt = 0;
    f.top = top;
  }
  const armedOp = exp.mode ? "export" : compact.mode ? "compact" : addrOp.mode ? addrOp.kind : llmops.mode ? "llmops" : null;
  const armedPicked = armedOp === "export" ? exp.picked : armedOp === "compact" ? compact.picked : addrOp.mode ? addrOp.picked : armedOp === "llmops" ? llmops.picked : {};
  const armedBusy = armedOp === "export" ? !!exp.busy : armedOp === "compact" ? !!compact.busy : addrOp.mode ? !!addrOp.busy : false;
  const armedCount = markedAddresses(armedPicked).length;
  const blockedByWindow = !!llmopsWin && (armedOp === "compact" || armedOp === "erase" || armedOp === "llmops");
  const canApply = !!armedOp && !armedBusy && armedCount > 0 && !blockedByWindow;
  const applyColor = armedOp ? ACTION_COLOR[armedOp] : null;
  const armedLabel = { export: "export", compact: "compact", content: "drop content", erase: "erase", llmops: "llm ops" }[armedOp] || "";
  const report = lastReport === "addr" ? addrOp : lastReport === "compact" ? compact : lastReport === "export" ? exp : lastReport === "import" ? imp : lastReport === "llmops" ? llmops : lastReport === "jump" ? jump : null;
  function applyArmed(button) {
    const box = button && button.getBoundingClientRect ? button.getBoundingClientRect() : null;
    setApplyAt(box ? { x: box.left + box.width / 2, y: box.bottom } : null);
    if (armedOp === "export") applyExport();
    else if (armedOp === "compact") openCompactConfirm();
    else if (armedOp === "content" || armedOp === "erase") setDropConfirm(true);
    else if (armedOp === "llmops") applyLLMOps();
  }
  const applyBtn = jsx34("button", {
    type: "button",
    disabled: !canApply,
    title: canApply ? armedOp === "export" ? `export ${armedCount} ticked message(s) to files` : armedOp === "content" ? `drop the content of ${armedCount} ticked tool result(s) \u2014 asks to confirm, IRREVERSIBLE` : armedOp === "erase" ? `erase ${armedCount} ticked message(s) \u2014 asks to confirm, IRREVERSIBLE` : armedOp === "llmops" ? `open the LLMOps window with ${armedCount} ticked message(s)` : `compact ${armedCount} ticked message(s) \u2014 asks to confirm, IRREVERSIBLE` : blockedByWindow ? "the LLMOps window is open \u2014 close it first" : armedOp ? "nothing ticked" : "arm an operation first",
    onClick: (e) => {
      e.stopPropagation();
      if (canApply) applyArmed(e.currentTarget);
    },
    className: canApply ? "mirror-action-btn rounded-full font-medium cursor-pointer select-none active:scale-95" : "rounded-full font-medium select-none",
    style: Object.assign({
      marginLeft: "auto",
      flexShrink: 0,
      border: `1px solid ${canApply ? applyColor : "var(--ui-stroke-secondary)"}`,
      color: canApply ? applyColor : void 0,
      userSelect: "none",
      cursor: canApply ? "pointer" : "default",
      opacity: canApply ? 1 : 0.45
    }, ACTION_BTN),
    children: armedBusy ? "applying\u2026" : "Apply operation"
  });
  const head = jsxs31("div", {
    className: "items-center gap-2 px-2 py-1 border-b border-(--ui-stroke-quaternary) shrink-0",
    style: Object.assign({
      fontSize: "0.55rem",
      display: "grid",
      gridTemplateColumns: "1fr auto 1fr",
      alignItems: "center"
    }, SELECTABLE),
    children: [
      jsx34("span", {}, "apply-gap"),
      jsx34("span", {
        className: orphans ? "text-(--ui-text-primary)" : "text-(--ui-text-quaternary)",
        style: Object.assign({ justifySelf: "center", minWidth: 0, textAlign: "center" }, SELECTABLE),
        // Число, которое и есть вердикт прогона: сколько сообщений легло в структуру
        // и сколько осталось за бортом.
        children: st.loading ? "loading\u2026" : `${placed}/${total} placed` + (orphans ? ` \xB7 ${orphans} not placed` : "") + (foreign ? ` \xB7 ${foreign} foreign` : "")
      }),
      jsx34("button", {
        onClick: () => loadMessages(),
        className: "rounded-full font-medium cursor-pointer select-none transition-colors duration-150 ease-out hover:bg-(--ui-bg-tertiary) active:scale-95",
        style: Object.assign({
          justifySelf: "end",
          border: "1px solid var(--ui-stroke-secondary)",
          userSelect: "none"
        }, ACTION_BTN),
        children: "\u21BB Reload"
      })
    ]
  });
  if (st.note) {
    return jsxs31("div", {
      className: "flex flex-col h-full w-full overflow-hidden",
      style: CAPPED,
      children: [head, sdkC(sdk8.EmptyState, "div", { title: "No messages", description: st.note })]
    });
  }
  if (!rows.length) {
    return jsxs31("div", {
      className: "flex flex-col h-full w-full overflow-hidden",
      style: CAPPED,
      children: [head, sdkC(sdk8.EmptyState, "div", {
        title: st.loading ? "Loading\u2026" : "Nothing addressed yet",
        description: "Messages appear once the session has history and the indexer has run."
      })]
    });
  }
  const msgTokens2 = (Array.isArray(st.messages) ? st.messages : []).reduce((sum, m) => sum + factTokens(m), 0);
  const tokRep = tok && tok.sessionId === view.sessionId && tok.rep && tok.rep.ours || null;
  const fixedTokens = tokRep ? (tokRep.system || 0) + (tokRep.tools || 0) : 0;
  const totalTokens = msgTokens2 + fixedTokens;
  const pickedTokens = rows.reduce((sum, r) => r.kind === "msg" && r.address && (compact.picked || {})[r.address] ? sum + factTokens(r.fact) : sum, 0);
  const RAIL_HEAD = "1.35rem";
  const statsBar = jsxs31("div", {
    className: "flex items-center gap-2 min-w-0",
    style: Object.assign({ fontSize: "0.55rem", width: "100%" }, SELECTABLE),
    children: [
      jsx34("span", {
        className: "text-(--ui-text-quaternary)",
        style: SELECTABLE,
        title: tokRep ? `messages ${msgTokens2} + system ${tokRep.system || 0} + tools ${tokRep.tools || 0} \u2014 our estimator` : "messages only \u2014 the token counter has not answered yet",
        children: `~${tokensLabel(totalTokens)} tk total${tokRep ? "" : " (msgs)"}`
      }),
      // `PRESS ESC` снят вместе с приходом крестика на рельсу компакции (Plan16-Task07,
      // CARRY Task05: «убрать вместе с крестиком компакта»). Esc остался клавиатурным близнецом.
      // СКОЛЬКО ТОКЕНОВ ОТМЕЧЕНО (владелец, 2026-09-23). Считается ТЕМ ЖЕ `factTokens`, что
      // уже стоит числом перед каждым сообщением в ленте — не вторая оценка рядом с первой, а
      // сумма ровно тех чисел, которые человек видит слева. Иначе сумма не сошлась бы с
      // слагаемыми, и доверия к ней не было бы никакого.
      //
      // Источник — `rows`, а не `st.messages`: отметка стоит на АДРЕСЕ, и только в `rows`
      // адрес лежит рядом с длиной. `~` не для красоты: это оценка по символам, а не счёт
      // токенизатором (см. `factTokens`).
      compact.mode ? jsx34("span", {
        className: "font-semibold",
        style: Object.assign({ fontSize: "0.7rem", color: ACTION_COLOR.compact }, SELECTABLE),
        title: "rough token estimate of the ticked messages \u2014 same per-message numbers as in the tape, summed",
        children: `~${tokensLabel(pickedTokens)} tk ticked`
      }) : null,
      applyBtn
    ]
  });
  const groups = buildCarriageGroups(rows);
  const railSpecs = [];
  if (exp.mode) {
    railSpecs.push({
      key: "export",
      picked: exp.picked,
      paint: EXPORT_PAINT,
      setPicks: setExportPicks,
      drag: railDrag("export", readExport, setExportPicks),
      onClose: () => setExportMode(false)
    });
  }
  if (addrOp.mode) {
    railSpecs.push({
      key: addrOp.kind,
      picked: addrOp.picked,
      paint: ADDR_PAINT[addrOp.kind],
      setPicks: setAddrOpPicks,
      drag: railDrag(addrOp.kind, readAddrOp, setAddrOpPicks),
      onClose: () => setAddrOpMode(false)
    });
  }
  if (llmops.mode) {
    railSpecs.push({
      key: "llmops",
      picked: llmops.picked,
      paint: LLMOPS_PAINT,
      setPicks: setLLMOpsPicks,
      drag: railDrag("llmops", readLLMOps, setLLMOpsPicks),
      onClose: () => setLLMOpsMode(false)
    });
  }
  if (compact.mode) {
    railSpecs.push({
      key: "compact",
      picked: compact.picked,
      paint: COMPACT_PAINT,
      setPicks: setCompactPicks,
      drag: { onDown: beginCompactDrag, onEnter: dragCompactOver },
      onClose: () => setCompactMode(false)
    });
  }
  const openRails = railSpecs;
  const items = [];
  const idsBySpec = railSpecs.map(() => []);
  groups.forEach((g, i) => {
    const built2 = buildCarriage(
      g,
      popup,
      setPopup,
      ctxView.stale,
      compact,
      i + 1,
      ledgerByMsg,
      durations
    );
    items.push(built2.vagon);
    railSpecs.forEach((spec, slot) => {
      activeAddresses(built2.railRows, spec.paint.active).forEach((id) => {
        if (idsBySpec[slot].indexOf(id) < 0) idsBySpec[slot].push(id);
      });
      items.push(jsx34(Rail, {
        railRows: built2.railRows,
        cells: baseCells(built2.railRows, spec.picked, spec.drag, spec.paint),
        guide: true,
        gridRow: i + 1,
        gridColumn: 2 + slot
      }, `${g.header.key}-rail-${spec.key}`));
    });
  });
  function goToOp(op) {
    const [key, viaHost] = chipKeyOfOp(op, groups, fileContext.ops);
    const tape = tapeRef.current;
    const el = key && tape && tape.querySelector(`[data-chip-id="${String(key).replace(/"/g, '\\"')}"]`);
    setLastReport("jump");
    if (!key) {
      setJump({ note: `op ${op}: no chip in the tape`, ok: false });
      return;
    }
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "center", behavior: "auto" });
    setJump({ note: `op ${op} \u2192 ${key}` + (viaHost ? " (the execute_code it ran inside)" : "") + (el ? "" : " (chip not on screen)"), ok: !!el });
    setPopup({ id: key, tier: "summary" });
  }
  function pickAll(slot, clear) {
    const next = {};
    if (!clear) idsBySpec[slot].forEach((id) => {
      next[id] = true;
    });
    railSpecs[slot].setPicks(next);
  }
  function invertRail(slot) {
    const next = Object.assign({}, railSpecs[slot].picked || {});
    idsBySpec[slot].forEach((id) => {
      if (next[id]) delete next[id];
      else next[id] = true;
    });
    railSpecs[slot].setPicks(next);
  }
  function railPickBtn(title, glyph, onClick, disabled) {
    return jsx34("button", {
      type: "button",
      className: "mirror-action-btn rounded-md border border-(--ui-stroke-secondary)",
      title,
      disabled,
      onClick: (e) => {
        e.stopPropagation();
        onClick();
      },
      style: {
        flex: "1 1 0",
        minWidth: 0,
        boxSizing: "border-box",
        height: "100%",
        padding: 0,
        cursor: "pointer",
        lineHeight: 1,
        fontSize: "0.65rem",
        color: "var(--ui-text-secondary)",
        backgroundColor: VAGON_BG
      },
      children: glyph
    }, title);
  }
  const chromes = groups.length ? railSpecs.map((spec, slot) => {
    const marked = markedAddresses(spec.picked);
    const ids = idsBySpec[slot];
    const allOn = ids.length > 0 && ids.every((id) => marked.indexOf(id) !== -1);
    return selectionRailChrome({
      key: spec.key,
      column: 2 + slot,
      rowSpan: groups.length,
      fill: VAGON_BG,
      headHeight: RAIL_HEAD,
      markedCount: marked.length,
      onClose: spec.onClose,
      capChildren: [
        railPickBtn(allOn ? "clear all" : "select all", allOn ? "\u25CB" : "\u2713", () => pickAll(slot, allOn), ids.length === 0),
        railPickBtn("invert", "\u21C4", () => invertRail(slot), ids.length === 0)
      ]
    });
  }) : [];
  const railLefts = Object.keys(railBoxes).map((k2) => railBoxes[k2].left);
  const firstRail = railLefts.length ? Math.min.apply(null, railLefts) : null;
  const tapeHead = jsx34("div", {
    style: {
      position: "relative",
      zIndex: 4,
      height: RAIL_HEAD,
      width: firstRail !== null ? `${Math.max(0, firstRail - 6)}px` : "100%",
      boxSizing: "border-box",
      display: "flex",
      alignItems: "center",
      padding: "0 0.5rem",
      backgroundColor: WORKAREA_BG,
      borderBottom: "1px solid var(--ui-stroke-quaternary)"
    },
    children: statsBar
  }, "tape-head");
  const orphanRows = rows.filter((r) => r.kind === "orphan");
  if (orphanRows.length) {
    items.push(jsxs31("div", {
      className: "flex items-center gap-2 mt-3 mb-1",
      style: Object.assign({ fontSize: "0.55rem", width: "60%", gridColumn: "1 / -1" }, SELECTABLE),
      children: [
        jsx34("span", {
          className: "px-1.5 py-0.5 rounded-full border border-(--ui-stroke-secondary) text-(--ui-text-primary) shrink-0",
          children: `not placed \xB7 ${orphans}`
        }),
        jsx34("div", { className: "grow", style: { borderTop: "1px solid var(--ui-stroke-primary)" } })
      ]
    }, "orphan-bar"));
    orphanRows.forEach((row) => items.push(jsx34("div", {
      style: { gridColumn: "1 / -1" },
      children: jsx34(MessageNode, { row, popup, setPopup })
    }, row.key)));
  }
  const msgAddresses = rows.filter((r) => r.kind === "msg").map((r) => r.address).filter(Boolean);
  const railTerms = [];
  openRails.forEach(() => railTerms.push(RAIL_BODY_WIDTH));
  const railCols = railTerms.length ? ` ${railTerms.join(" ")}` : "";
  const railColumnGap = "0.375rem";
  return jsxs31("div", {
    className: "flex flex-col h-full w-full overflow-hidden",
    style: CAPPED,
    children: [
      head,
      // ЗОНА КОМАНД — НАВЕРХУ контейнера, сразу под шапкой, и ВСЕ кнопки в одной полосе
      // (владелец, 2026-09-23: «так будет удобнее», «забирай их все наверх и ложи покачто в
      // одну строку»). Были внизу, за скроллером. Разница не в привычке: рельсы тянутся
      // вдоль ВСЕЙ ленты, и кнопка, которая их взводит, должна быть видна там, где человек
      // начинает выбирать, а не там, куда он доскроллит.
      //
      // `flex-wrap` — кнопок пять и поле доли; на узкой панели полоса переносится, а не
      // выдавливает содержимое за срез (ровно та беда, что была у строки состояния).
      // Рамку несёт ЭТА обёртка, а не каждая зона: иначе между половинами одной полосы
      // появилась бы линия.
      // ЗОНЫ (Plan23-Task01, владелец 2026-09-29: «сейчас очень хаотично»): Operations — кнопки
      // группами, Status — одна строка отчёта, Files — FileOps. Каждая сворачивается, свёрнутая
      // показывает сводку. `Apply` — в шапке ленты у крестиков рельс (2026-10-14): виден и при свёрнутой Operations.
      // `Operations` БЕЗ заголовка (владелец, 2026-09-29: «мы теряем высоту на заголовке»):
      // развёрнутая — группы кнопок, треугольник в начале первой; свёрнутая — одна строка
      // тонких кнопок без опций. `Status` — просто одна строка: разворачивать там пока нечего.
      zones.operations ? jsxs31("div", { className: "shrink-0 flex flex-col border-b border-(--ui-stroke-quaternary)", children: [
        jsx34(OpGroup, {
          label: "context",
          lead: jsx34(ZoneToggle, { title: "Operations", open: true, onToggle: () => toggleZone("operations") }),
          children: [
            jsx34(ActionZone, { op: addrOp }, "addr"),
            jsx34(CompactZone, { compact, addresses: msgAddresses }, "compact"),
            jsx34(LLMOpsZone, {}, "llmops")
          ]
        }),
        jsx34(OpGroup, { label: "save", children: [
          jsx34(ExportZone, { exp, addresses: msgAddresses }, "export"),
          jsx34(ImportZone, { imp, onOpen: setImportAt }, "import")
        ] })
      ] }, "zone-operations") : jsxs31("div", {
        className: "shrink-0 flex items-center flex-wrap border-b border-(--ui-stroke-quaternary)",
        style: { paddingLeft: "0.375rem", minHeight: "1.25rem" },
        children: [
          jsx34(ZoneToggle, { title: "Operations", open: false, onToggle: () => toggleZone("operations") }, "toggle"),
          jsx34(ActionZone, { op: addrOp, thin: true }, "addr"),
          jsx34(CompactZone, { compact, addresses: msgAddresses, thin: true }, "compact"),
          jsx34(LLMOpsZone, { thin: true }, "llmops"),
          jsx34(ExportZone, { exp, addresses: msgAddresses, thin: true }, "export"),
          jsx34(ImportZone, { imp, thin: true, onOpen: setImportAt }, "import")
        ]
      }, "zone-operations"),
      jsx34(StatusLine, { report, run: compactRun }, "zone-status"),
      // `Files` пропадает целиком, пока файлов в контексте нет (владелец: «сжимать в ноль»).
      ctxView.files && ctxView.files.length ? jsx34(FileContextPanel, {
        view: ctxView,
        selectedOp: selectedOpOf(popup, groups),
        onPickOp: goToOp,
        open: zones.files,
        onToggle: () => toggleZone("files")
      }, "zone-files") : null,
      jsx34("style", { children: `
        .fops-scroll::-webkit-scrollbar { width: 16px; }
        .fops-scroll::-webkit-scrollbar-track { background: transparent; }
        .fops-scroll::-webkit-scrollbar-thumb {
          background-color: var(--ui-stroke-secondary);
          border-radius: 0;
          border-style: solid;
          border-color: transparent;
          border-width: 0 5px 0 3px;
          background-clip: padding-box;
        }
        .fops-scroll::-webkit-scrollbar-thumb:hover { background-color: var(--ui-stroke-primary); }
        /* \u0421\u043A\u0440\u043E\u043B\u043B \u0421\u0415\u041A\u0426\u0418\u0419 \u043E\u043A\u043D\u0430, \u043D\u0435 \u043B\u0435\u043D\u0442\u044B. \u041B\u0435\u043D\u0442\u043E\u0447\u043D\u044B\u0439 \u0431\u0435\u0433\u0443\u043D\u043E\u043A \u0448\u0438\u0440\u043E\u043A\u0438\u0439 \u0438 \u0441\u0438\u043B\u044C\u043D\u043E \u0441\u043A\u0440\u0443\u0433\u043B\u0451\u043D
           (\u0445\u0438\u0442-\u0437\u043E\u043D\u0430 \u0441\u043A\u0440\u043E\u043B\u043B\u0431\u0430\u0440\u0430). \u0417\u0434\u0435\u0441\u044C \u0431\u0435\u0433\u0443\u043D\u043E\u043A \u0442\u043E\u043D\u044C\u0448\u0435 \u0438 \u043F\u043E\u0447\u0442\u0438 \u043F\u0440\u044F\u043C\u043E\u0439. \u041A\u043B\u0430\u0441\u0441\u0438\u0447\u0435\u0441\u043A\u0438\u0439, \u043D\u0435
           \u043E\u0432\u0435\u0440\u043B\u0435\u0439\u043D\u044B\u0439: \u043E\u0432\u0435\u0440\u043B\u0435\u0439\u043D\u044B\u0439 \u043F\u0440\u043E\u043F\u0430\u0434\u0430\u0435\u0442 \u043F\u043E\u0441\u043B\u0435 \u0440\u0430\u0437\u0432\u043E\u0440\u043E\u0442\u0430, \u043F\u043E\u043A\u0430 \u043F\u043E \u043F\u043E\u043B\u043E\u0441\u0435 \u043D\u0435 \u043A\u043B\u0438\u043A\u043D\u0435\u0448\u044C. */
        .mirror-pane-scroll::-webkit-scrollbar { width: 6px; }
        .mirror-pane-scroll::-webkit-scrollbar-track { background: transparent; }
        .mirror-pane-scroll::-webkit-scrollbar-thumb {
          background-color: var(--ui-stroke-secondary);
          border-radius: 2px;
        }
        .mirror-pane-scroll::-webkit-scrollbar-thumb:hover { background-color: var(--ui-stroke-primary); }
        /* \u0425\u043E\u0432\u0435\u0440-\u043F\u043E\u0434\u0441\u0432\u0435\u0442\u043A\u0430 \u0447\u0438\u043F\u0430 (Plan19, \u0432\u043B\u0430\u0434\u0435\u043B\u0435\u0446 2026-09-15: \xAB\u0445\u043E\u0432\u0435\u0440 \u0438\u0437\u043C\u0435\u043D\u0435\u043D\u0438\u044F \u0446\u0432\u0435\u0442\u0430... \u0442\u043E\u0436\u0435
           \u043D\u0435\u0442, \u043D\u0443\u0436\u043D\u043E \u0434\u043E\u0431\u0430\u0432\u0438\u0442\u044C\xBB). filter \u0432\u043C\u0435\u0441\u0442\u043E \u0441\u043C\u0435\u043D\u044B \u0444\u043E\u043D\u0430 \u043A\u043B\u0430\u0441\u0441\u043E\u043C: \u0438\u043D\u043B\u0430\u0439\u043D-\u0444\u043E\u043D \u0447\u0438\u043F\u0430 (color-mix
           \u043F\u043E\u0434 \u0442\u043E\u043D \u0440\u043E\u043B\u0438/\u0442\u0443\u043B\u0430) \u0432\u0441\u0435\u0433\u0434\u0430 \u043E\u0431\u0433\u043E\u043D\u044F\u0435\u0442 Tailwind hover:bg-* \u043F\u043E \u043F\u0440\u0438\u043E\u0440\u0438\u0442\u0435\u0442\u0443, \u0430 filter
           \u043F\u0440\u0438\u043C\u0435\u043D\u044F\u0435\u0442\u0441\u044F \u043F\u043E\u0432\u0435\u0440\u0445 \u0433\u043E\u0442\u043E\u0432\u043E\u0439 \u043A\u0430\u0440\u0442\u0438\u043D\u043A\u0438 \u043D\u0435\u0437\u0430\u0432\u0438\u0441\u0438\u043C\u043E \u043E\u0442 \u0442\u043E\u0433\u043E, \u043A\u0430\u043A \u0437\u0430\u0434\u0430\u043D \u0444\u043E\u043D. */
        .mirror-chip:hover:not(.mirror-chip-open) { filter: brightness(${CHIP_HOVER_BRIGHTNESS}); }
        /* \u0425\u043E\u0432\u0435\u0440 \u043A\u043D\u043E\u043F\u043E\u043A \u043D\u0438\u0437\u0430 (Palette \u2192 Buttons #A5-A6, \u0432\u043B\u0430\u0434\u0435\u043B\u0435\u0446 2026-09-15: \xAB\u0434\u043E\u0431\u0430\u0432\u044C \u0445\u043E\u0432\u0435\u0440,
           \u0438\u0437\u043C\u0435\u043D\u0435\u043D\u0438\u0435 \u0446\u0432\u0435\u0442\u0430, \u0447\u0442\u043E\u0431\u044B \u043E\u043D\u0438 \u0432\u044B\u0433\u043B\u044F\u0434\u0435\u043B\u0438 \u0436\u0438\u0432\u0435\u0435\xBB). box-shadow, \u043D\u0435 background \u2014 \u0438\u043D\u043B\u0430\u0439\u043D-
           \u0444\u043E\u043D \u043A\u043D\u043E\u043F\u043A\u0438 (\u043F\u0440\u043E\u0437\u0440\u0430\u0447\u043D\u044B\u0439/color-mix \u043F\u043E \u0441\u043E\u0441\u0442\u043E\u044F\u043D\u0438\u044E armed) \u043D\u0435 \u0442\u0440\u043E\u0433\u0430\u0435\u0442\u0441\u044F, \u0430 currentColor
           \u043F\u043E\u0434\u0445\u0432\u0430\u0442\u044B\u0432\u0430\u0435\u0442 \u0446\u0432\u0435\u0442 \u041A\u0410\u0416\u0414\u041E\u0419 \u043A\u043D\u043E\u043F\u043A\u0438 \u0431\u0435\u0437 \u043F\u0435\u0440\u0435\u0447\u0438\u0441\u043B\u0435\u043D\u0438\u044F. */
        .mirror-action-btn { transition: box-shadow 150ms ease-out, filter 150ms ease-out; }
        .mirror-action-btn:hover { box-shadow: inset 0 0 0 999px color-mix(in srgb, currentColor 12%, transparent); }
        .mirror-action-btn:disabled:hover { box-shadow: none; }
      ` }),
      jsxs31("div", {
        className: "min-h-0 w-full",
        style: {
          flex: "1 1 auto",
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
          padding: "0 0 0 0.5rem",
          backgroundColor: WORKAREA_BG
        },
        children: [
          // Вагоны и рельса слева, по содержимому. Бегунок — на правом крае окна,
          // потому что скроллер на всю ширину, а лента внутри него не растянута.
          jsxs31("div", {
            ref: headRef,
            style: {
              flex: "0 0 auto",
              position: "relative",
              width: "100%",
              height: RAIL_HEAD,
              backgroundColor: WORKAREA_BG
            },
            children: [
              tapeHead,
              ...railSpecs.map((spec, slot) => {
                const box = railBoxes[spec.key];
                if (!box || !chromes[slot]) return null;
                return jsx34("div", {
                  style: {
                    position: "absolute",
                    top: 0,
                    left: `${box.left}px`,
                    width: `${box.width}px`,
                    height: RAIL_HEAD,
                    zIndex: 5
                  },
                  children: chromes[slot].close
                }, `close-${spec.key}`);
              })
            ]
          }),
          jsx34("div", {
            ref: scrollRef,
            onScroll: onTapeScroll,
            className: "w-full fops-scroll",
            style: {
              flex: "1 1 auto",
              width: "100%",
              minHeight: 0,
              overflowY: "auto",
              overflowX: "hidden",
              backgroundColor: WORKAREA_BG
            },
            children: [
              jsxs31("div", {
                ref: tapeRef,
                style: {
                  display: "grid",
                  width: "fit-content",
                  maxWidth: "100%",
                  // 2px справа ВНУТРИ ширины (владелец, 2026-09-29): на минимальной ширине рамка
                  // правой рельсы срезалась окном. 1px на рамку, 1px зазора до среза.
                  paddingRight: "2px",
                  boxSizing: "border-box",
                  gridTemplateColumns: "minmax(0, max-content)" + railCols,
                  columnGap: railColumnGap,
                  rowGap: "0.5rem",
                  alignItems: "start",
                  justifyItems: "start"
                },
                children: [
                  ...chromes.reduce((acc, c) => acc.concat(c.body, c.cap), []),
                  ...items
                ]
              }, "tape"),
              // Хвост — СОСЕДОМ сетки, прямым ребёнком скроллера (2026-10-02: он жил внутри сетки,
              // 40% от её auto-высоты схлопнулись в ноль, и последний вагон лёг на нижний край).
              jsx34("div", { style: { height: TAIL_SPACE, pointerEvents: "none" } }, "tail-space")
            ]
          })
        ]
      }),
      jsx34(ConfirmModal, {
        open: !!compact.confirming,
        anchor: applyAt,
        title: "Compact messages?",
        // На 10% шире прочих: имена наборов в 4 колонки вылезали за край (владелица, 2026-10-16).
        widthRem: 22,
        body: `This summarizes ${markedAddresses(compact.picked).length} ticked message(s) into one summary and removes the rest from the model's context. Irreversible.`,
        busy: !!compact.busy,
        // Пожелание агенту к этой компакции (`Plan34` Task 6) — уходит в опрос директивой.
        extra: jsxs31("div", { className: "flex flex-col gap-1.5", children: [
          jsx34("textarea", {
            value: compact.wish || "",
            rows: 3,
            disabled: !!compact.busy,
            placeholder: "wish for the agent (optional): what to focus on, what to drop",
            onChange: (e) => setCompactWish(e.target.value),
            onKeyDown: (e) => e.stopPropagation(),
            style: {
              width: "100%",
              boxSizing: "border-box",
              resize: "vertical",
              minHeight: "3.2rem",
              maxHeight: "12rem",
              fontSize: "0.72rem",
              lineHeight: 1.4,
              padding: "0.35rem 0.45rem",
              borderRadius: "0.375rem",
              border: "1px solid var(--ui-stroke-secondary)",
              background: "transparent",
              color: "inherit"
            }
          }, "wish"),
          // Превью (`Plan35`): дистиллят — в окно LLMOps, в ленту — только по Replace. Первым, над
          // наборами, отделено чертой (владелица, 2026-10-16). Галочки — стилем Config.
          jsxs31("label", {
            className: "inline-flex items-center gap-1 cursor-pointer select-none text-[0.65rem]",
            title: "the distillate opens in the LLMOps window; the tape changes only when you press Replace there",
            children: [
              jsx34("input", {
                type: "checkbox",
                checked: !!compact.preview,
                disabled: !!compact.busy,
                style: CONFIRM_CHECKBOX,
                onChange: () => toggleCompactPreview()
              }),
              "preview"
            ]
          }, "preview"),
          // Подсказки — тем же неприметным шрифтом, что подпись наборов (владелица, 2026-10-16).
          jsx34("span", {
            className: CONFIRM_HINT,
            style: { marginTop: "-0.3rem" },
            children: "The result goes to the LLMOps window instead of straight into the tape: read it, edit it, and insert it into the tape from there with Replace."
          }, "preview-hint"),
          Array.isArray(compact.sets) ? jsx34("div", { style: { borderTop: "1px solid var(--ui-stroke-secondary)" } }, "rule") : null,
          // Какие наборы опроса показать модели — на этот прогон (`Plan34` Task 8). Из настройки;
          // тронул — уходит в прогон, фильтры молчат. Запасной (`general`) не снимается.
          // Колонками, а не в строку (владелица, 2026-10-16): подпись сверху, наборы — сеткой по 4.
          Array.isArray(compact.sets) ? jsx34("span", { className: CONFIRM_HINT, children: "question sets:" }, "sets-head") : null,
          Array.isArray(compact.sets) ? jsx34("div", {
            className: "text-[0.65rem]",
            style: { display: "grid", gridTemplateColumns: "repeat(4, max-content)", columnGap: "1.25rem", rowGap: "0.15rem" },
            children: compact.sets.map((s) => jsxs31("label", {
              className: "inline-flex items-center gap-1 cursor-pointer select-none",
              title: compactSetTitle(s),
              children: [
                jsx34("input", {
                  type: "checkbox",
                  checked: !!(compact.menu || {})[s.name],
                  disabled: !!s.fallback || !!compact.busy,
                  style: CONFIRM_CHECKBOX,
                  onChange: () => toggleCompactMenu(s.name)
                }),
                compactSetLabel(s)
              ]
            }, s.name))
          }, "sets") : null,
          // Подсказка — ПОД галочками наборов (владелица, 2026-10-16).
          Array.isArray(compact.sets) ? jsx34("span", {
            className: CONFIRM_HINT,
            children: "Sets of questions the agent is asked about the conversation. For each topic it picks on its own which set fits; untick a set to keep it out of this run. Hover a set to see its questions."
          }, "sets-hint") : null
        ] }),
        onConfirm: () => confirmCompact(),
        onCancel: () => cancelCompactConfirm()
      }, "confirm-compact"),
      jsx34(ConfirmModal, {
        open: dropConfirm,
        anchor: applyAt,
        title: addrOp.kind === "erase" ? "Erase messages?" : "Drop content?",
        body: addrOp.kind === "erase" ? `This removes ${markedAddresses(addrOp.picked).length} ticked message(s) from the model's context. A tool result takes its call with it. Irreversible.` : `This replaces the body of ${markedAddresses(addrOp.picked).length} ticked tool result(s) with a stub; our header stays, a foreign result keeps only its tool name and size. Irreversible.`,
        busy: !!addrOp.busy,
        onConfirm: () => {
          applyAddrOp().then(() => {
            setDropConfirm(false);
            loadMessages();
          });
        },
        onCancel: () => setDropConfirm(false)
      }, "confirm-drop"),
      jsx34(ConfirmModal, {
        open: !!imp.open,
        anchor: importAt,
        title: "Import session from a folder?",
        body: jsx34(ImportBody, { imp }),
        busy: !!imp.busy,
        canConfirm: !!(imp.check && imp.check.ok === true && imp.check.folder === imp.folder),
        // Три режима (Plan27, владелец): главная — `Replace session`, крупнее.
        actions: [
          { key: "head", label: "Add to head", onClick: () => {
            applyImport("head");
          } },
          { key: "tail", label: "Add to tail", onClick: () => {
            applyImport("tail");
          } },
          { key: "replace", label: "Replace session", main: true, onClick: () => {
            applyImport("replace");
          } }
        ],
        onCancel: () => closeImport()
      }, "confirm-import")
      // НИЖНЕЙ ПОЛОСЫ ДЕЙСТВИЙ БОЛЬШЕ НЕТ (владелец, 2026-09-23: «эту панель можно удалить»).
      // Она осталась дублем после переезда команд наверх: те же три кнопки, тот же атом, тот
      // же обработчик — два входа в одно и то же действие на одном экране.
    ]
  });
}

// desktop/src/token-strip.js
import { jsx as jsx36, jsxs as jsxs33 } from "react/jsx-runtime";
import { useEffect as useEffect6, useState as useState14 } from "react";

// desktop/src/token-bar.js
import { jsx as jsx35, jsxs as jsxs32 } from "react/jsx-runtime";
import { useState as useState13, useRef as useRef8 } from "react";
var k = (x) => typeof x === "number" && isFinite(x) ? x >= 1e4 ? Math.round(x / 1e3) + "k" : (x / 1e3).toFixed(1) + "k" : "\u2014";
var n = (x) => typeof x === "number" && isFinite(x) ? x.toLocaleString("en-US").replace(/,/g, " ") : "\u2014";
var pad = (s, w) => (String(s) + " ".repeat(w)).slice(0, Math.max(w, String(s).length));
var rpad = (s, w) => (" ".repeat(w) + String(s)).slice(-Math.max(w, String(s).length));
function mainNumber(r) {
  if (r && typeof r.next === "number" && r.anchor) return { value: r.next, mark: "\u2693", label: "server number + growth after it" };
  if (r && r.exact && typeof r.exact.total === "number") return { value: r.exact.total, mark: "\u2713", label: "exact count by the model server" };
  if (r && r.ours && typeof r.ours.total === "number") return { value: r.ours.total, mark: "\u2248", label: "our estimate only" };
  return null;
}
function tipText(r, main, win, auto, autoPercent, ticked) {
  const o = r.ours || {};
  const fx = o.fixed_exact || {};
  const e = r.exact;
  const a = r.anchor;
  const w = r.window || {};
  const tk = r.tokenizer || {};
  const s = r.server || {};
  const L = (label, value, note) => "  " + pad(label, 26) + rpad(value, 9) + (note ? "  " + note : "");
  const lines = [];
  lines.push(`CONTEXT  ${n(main.value)} / ${n(win)}  (${win ? (100 * main.value / win).toFixed(1) : "\u2014"}%)  ${main.mark} ${main.label}`);
  lines.push(L("system prompt", n(o.system), fx.system ? "\u2713 exact" : "\u2248"));
  lines.push(L("tool schemas", n(o.tools), (fx.tools ? "\u2713 exact" : "\u2248") + ` \xB7 ${n(o.tools_count)} tools`));
  lines.push(L("messages", n(o.msgs), `\u2248 \xB7 ${n((r.rows || {}).total)} rows \xB7 markup ${n(o.markup)}`));
  if (a) lines.push(L("growth after last reply", "+" + n(main.value - a.prompt), `${n((r.rows || {}).after_anchor)} rows`));
  lines.push("");
  lines.push("AUTO-COMPACTION");
  lines.push(L(
    `threshold ${autoPercent != null ? autoPercent + "%" : "\u2014"}`,
    n(auto),
    auto != null ? main.value >= auto ? `passed by ${n(main.value - auto)}` : `${n(auto - main.value)} left` : ""
  ));
  const d = r.decision;
  if (d) lines.push(L(
    "decision",
    d.fire ? "FIRE" : "not yet",
    d.enabled ? "automaton ON" : "automaton off \u2014 compact_auto_enabled: false"
  ));
  const ask = r.last_ask;
  lines.push(L(
    "their automaton asked",
    ask ? "@" + ask.at : "never",
    ask ? `they counted ${n(ask.their_tokens)} \xB7 we answered ${ask.answer ? "yes" : "no"}${ask.why ? " \u2014 " + ask.why : ""}` : "compression.enabled is off in config.yaml"
  ));
  const au = r.auto || {};
  if (au.last || au.fails) {
    lines.push(L(
      "last automatic run",
      au.last ? au.last.ok ? "ok" : "failed" : "\u2014",
      (au.last ? "@" + au.last.at + " " + (au.last.why || "") : "") + (au.fails ? ` \xB7 ${au.fails} failures in a row` : "")
    ));
  }
  if (au.pause_until && au.pause_until * 1e3 > Date.now()) {
    lines.push(L("paused until", new Date(au.pause_until * 1e3).toLocaleTimeString(), "after failures in a row"));
  }
  if (ticked) lines.push(L("ticked for compaction", n(ticked), `\u2192 \u2248 ${n(main.value - ticked)} after (distillate not counted)`));
  lines.push("");
  lines.push("SOURCES");
  lines.push(L("ServerPrompt (last reply)", a ? n(a.prompt) : "\u2014", a ? `+${n(a.completion)} out` : "no reply yet"));
  lines.push(L("Next (anchor + ours)", n(r.next)));
  lines.push(L(
    "Exact (model server)",
    e && typeof e.total === "number" ? n(e.total) : "\u2014",
    e && e.error ? e.error : e && e.at ? "@" + e.at : ""
  ));
  lines.push(L("Ours (estimate)", n(o.total)));
  lines.push(L("Hermes (their estimate)", n(r.hermes)));
  lines.push("");
  lines.push("MODEL");
  lines.push(L("window: Hermes / server", `${n(w.hermes)} / ${n(w.server)}`));
  lines.push(L("tokenizer", `${tk.fp || "\u2014"} ${tk.fp ? tk.exact_model ? "\u2713" : "\u2248" : ""}`, `\xD7${tk.mult != null ? tk.mult : "\u2014"}`));
  lines.push(L("server", `${s.kind || "\u2014"}${s.status ? "/" + s.status : ""}`));
  if (s.via) lines.push(`  counted via ${s.via}`);
  lines.push(`  model ${r.model || "\u2014"}`);
  lines.push(`  session ${r.session || "\u2014"}`);
  return lines.join("\n");
}
function seg(key, width, color, extra) {
  return width > 0 ? jsx35("div", { style: Object.assign({ width: width + "%", height: "100%", backgroundColor: color, flex: "none" }, extra || {}) }, key) : null;
}
function TokenBar({ rep, autoPercent, ticked }) {
  const [open, setOpen] = useState13(null);
  const rootRef = useRef8(null);
  const toggle = () => {
    if (open) {
      setOpen(null);
      return;
    }
    const strip = rootRef.current && rootRef.current.closest("[data-token-strip]");
    const box = (strip || rootRef.current).getBoundingClientRect();
    const own = rootRef.current.getBoundingClientRect();
    setOpen({
      right: Math.max(0, window.innerWidth - box.right),
      bottom: Math.max(0, window.innerHeight - own.top),
      maxWidth: Math.max(160, box.right - 8)
    });
  };
  const r = rep || {};
  const main = mainNumber(r);
  const w = r.window || {};
  const win = w.server || w.hermes || 0;
  if (!main || !win) return null;
  const o = r.ours || {};
  const auto = autoPercent != null ? Math.round(win * autoPercent / 100) : null;
  const pct = (x) => Math.max(0, Math.min(100, 100 * (x || 0) / win));
  const fixed = (o.system || 0) + (o.tools || 0);
  const growth = r.anchor && main.mark === "\u2693" ? Math.max(0, main.value - r.anchor.prompt) : 0;
  const pick = Math.min(ticked || 0, Math.max(0, main.value - fixed - growth));
  const msgs = Math.max(0, main.value - fixed - growth - pick);
  const over = auto != null && main.value >= auto;
  const bar = jsxs32("div", {
    style: { position: "relative", flex: "1 1 auto", minWidth: "4rem", height: "0.4rem" },
    children: [
      jsxs32("div", {
        style: {
          position: "absolute",
          inset: 0,
          borderRadius: "0.2rem",
          overflow: "hidden",
          display: "flex",
          backgroundColor: TOKEN_COLOR.track
        },
        children: [
          seg("fixed", pct(fixed), TOKEN_COLOR.fixed),
          seg("msgs", pct(msgs), TOKEN_COLOR.msgs),
          seg("ticked", pct(pick), TOKEN_COLOR.ticked),
          seg("growth", pct(growth), TOKEN_COLOR.growth)
        ]
      }),
      auto != null ? jsx35("div", { style: {
        position: "absolute",
        left: `${pct(auto)}%`,
        top: "-0.15rem",
        bottom: "-0.15rem",
        width: "1px",
        backgroundColor: over ? TOKEN_COLOR.over : TOKEN_COLOR.threshold
      } }, "auto") : null
    ]
  });
  const text = jsxs32("span", {
    style: { whiteSpace: "nowrap", flex: "none" },
    children: [
      jsx35("span", {
        className: "font-semibold",
        style: { color: "var(--ui-text-secondary)" },
        children: `${k(main.value)} / ${k(win)}`
      }),
      ` \xB7 ${Math.round(100 * main.value / win)}% ${main.mark}`,
      auto != null ? jsx35("span", {
        style: { color: over ? TOKEN_COLOR.over : void 0 },
        children: `   auto ${k(auto)} \xB7 ${over ? "over by " + k(main.value - auto) : k(auto - main.value) + " left"}`
      }) : null,
      ticked ? jsx35("span", { style: { color: TOKEN_COLOR.ticked }, children: `   \u2192 \u2248${k(main.value - ticked)}` }) : null
    ]
  });
  return jsxs32("div", {
    // Без `position: relative`: окошко встаёт относительно всей мини-панели и получает её
    // ширину целиком, вместе с колонкой кнопок (владелец: «окно узковатое»).
    style: { display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer" },
    title: open ? void 0 : "click \u2014 all numbers",
    ref: rootRef,
    onMouseLeave: () => setOpen(null),
    children: [
      jsxs32("div", {
        style: { display: "flex", alignItems: "center", gap: "0.5rem", flex: "1 1 auto", minWidth: 0 },
        onClick: toggle,
        children: [bar, text]
      }),
      open ? jsx35("div", {
        // Прозрачная подложка до самой полосы — мост для мыши; окошко сидит на её верхнем краю.
        style: {
          position: "fixed",
          right: open.right + "px",
          bottom: open.bottom + "px",
          zIndex: 1e3,
          maxWidth: open.maxWidth + "px",
          cursor: "text"
        },
        children: jsx35("pre", {
          style: {
            margin: 0,
            padding: "0.4rem 0.5rem",
            fontSize: "0.5rem",
            lineHeight: "0.75rem",
            whiteSpace: "pre",
            overflowX: "auto",
            width: "max-content",
            maxWidth: "100%",
            fontFamily: "var(--font-mono, ui-monospace, monospace)",
            userSelect: "text",
            WebkitUserSelect: "text",
            background: "var(--ui-editor-surface-background, var(--background))",
            border: "1px solid var(--ui-stroke-secondary)",
            borderRadius: "0.3rem",
            color: "var(--ui-text-secondary)"
          },
          children: tipText(r, main, win, auto, autoPercent, ticked)
        })
      }) : null
    ]
  });
}

// desktop/src/token-strip.js
function btn(label, onClick, lit, title, disabled) {
  return jsx36("button", {
    onClick,
    disabled,
    title,
    className: "px-1.5 rounded cursor-pointer select-none hover:bg-(--ui-bg-tertiary) active:scale-95",
    style: {
      border: "1px solid " + (lit ? "hsl(var(--primary))" : "var(--ui-stroke-secondary)"),
      color: lit ? "hsl(var(--primary))" : void 0,
      opacity: disabled ? 0.5 : 1,
      fontSize: "0.55rem",
      lineHeight: "0.7rem",
      whiteSpace: "nowrap",
      width: "100%"
    },
    children: label
  }, label);
}
function TokenStrip() {
  const t = useTokens();
  const compact = useCompact();
  const st = useMessages();
  const [autoPercent, setAutoPercent] = useState14(null);
  useEffect6(() => {
    loadConfig().then((c) => {
      const p = c && c.compaction && c.compaction.auto_percent;
      if (typeof p === "number") setAutoPercent(p);
    }).catch(() => {
    });
  }, []);
  const r = t.rep || {};
  const s = r.server || {};
  const notLoaded = s.kind === "router" && (s.status === "unloaded" || s.status === "loading");
  const canCount = s.kind !== "none" && (s.status === "loaded" || s.status == null || !!s.via);
  const ticked = compact.mode ? (Array.isArray(st.messages) ? st.messages : []).reduce((sum, m) => sum + ((compact.picked || {})[m && m.msg] ? factTokens(m) : 0), 0) : 0;
  const cell = (child) => jsx36("div", { className: "min-w-0", children: child });
  return jsxs33("div", {
    "data-token-strip": "1",
    // Зазоры ужаты (владелец, 2026-09-29: высота ~0.7 от прежней). `relative` — окошко полосы
    // встаёт относительно всей панели.
    className: "shrink-0 border-t border-(--ui-stroke-quaternary) px-2 text-(--ui-text-tertiary)",
    style: {
      position: "relative",
      fontSize: "0.55rem",
      lineHeight: "0.75rem",
      fontVariantNumeric: "tabular-nums",
      display: "grid",
      gridTemplateColumns: "minmax(0, 1fr) max-content",
      columnGap: "0.5rem",
      rowGap: "0.1rem",
      alignItems: "center",
      paddingTop: "0.15rem",
      paddingBottom: "0.15rem"
    },
    children: [
      cell(t.rep ? jsx36(TokenBar, { rep: t.rep, autoPercent, ticked }) : jsx36("span", { children: t.note || "no token report yet" })),
      btn(
        "count",
        () => loadTokens({ exact: true }),
        false,
        s.via ? `exact count now via ${s.via} (same tokenizer)` : "exact count by the model server now",
        t.busy || !canCount
      ),
      cell(jsx36("div", {
        style: { whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", userSelect: "text" },
        title: t.note || r.model || "",
        // Ошибка — ПЕРВОЙ (владелец: «ушла под срез»): иначе её не видно за длинным именем модели.
        children: (t.note ? `\u26A0 ${t.note}   \xB7 ` : "") + `sess ${r.session ? String(r.session).slice(-6) : "\u2014"}   model ${r.model || "\u2014"}`
      })),
      btn(
        "load",
        () => loadTokens({ exact: true, load: true }),
        notLoaded,
        notLoaded ? "the model is not loaded \u2014 load it on the server and count (nothing is sent to it)" : "load is only for an unloaded model on a llama.cpp router",
        t.busy || !notLoaded
      )
    ]
  });
}

// desktop/src/config-pane.js
import { jsx as jsx38, jsxs as jsxs34 } from "react/jsx-runtime";
import { useState as useState16, useEffect as useEffect8, useLayoutEffect as useLayoutEffect6, useRef as useRef10 } from "react";

// desktop/src/config-widgets.js
import { jsx as jsx37 } from "react/jsx-runtime";
import { useState as useState15, useEffect as useEffect7, useRef as useRef9 } from "react";
function zone(children) {
  return jsx37("div", { className: "flex flex-col border-b border-(--ui-stroke-quaternary) pb-1 mb-1", children });
}
function textInput(value, onChange, placeholder, extraClassName) {
  return jsx37("input", {
    type: "text",
    value,
    onChange,
    placeholder,
    className: (extraClassName || "w-full") + " px-1.5 py-0.5 rounded text-[10px] outline-none transition-colors duration-100 ease-out",
    style: { backgroundColor: "var(--ui-bg-elevated)", border: "1px solid var(--ui-stroke-secondary)", color: "var(--ui-text-primary)" },
    onFocus: (e) => {
      e.currentTarget.style.borderColor = "var(--dt-ring)";
    },
    onBlur: (e) => {
      e.currentTarget.style.borderColor = "var(--ui-stroke-secondary)";
    }
  });
}
function textArea(value, rows, small) {
  return jsx37("textarea", {
    readOnly: true,
    value,
    rows: rows || 5,
    className: "w-full px-2 py-1 rounded-md outline-none resize-none font-mono" + (small ? "" : " text-xs"),
    style: Object.assign(
      { backgroundColor: "var(--ui-bg-elevated)", border: "1px solid var(--ui-stroke-secondary)", color: "var(--ui-text-primary)" },
      small ? { fontSize: "8.5px", lineHeight: "1.25" } : {}
    )
  });
}
function smallNumberField(value, onChange) {
  return jsx37("input", {
    type: "text",
    inputMode: "numeric",
    value,
    onChange,
    className: "w-full px-1 rounded outline-none text-right transition-colors duration-100 ease-out",
    style: {
      fontSize: "0.65rem",
      lineHeight: 1.5,
      backgroundColor: "var(--ui-bg-elevated)",
      border: "1px solid var(--ui-stroke-secondary)",
      color: "var(--ui-text-primary)"
    },
    onFocus: (e) => {
      e.currentTarget.style.borderColor = "var(--dt-ring)";
    },
    onBlur: (e) => {
      e.currentTarget.style.borderColor = "var(--ui-stroke-secondary)";
    }
  });
}
function compactCell(label, title, control) {
  return [
    jsx37("span", { title, className: "text-right text-(--ui-text-secondary)", style: SELECTABLE, children: label }, label + ":l"),
    jsx37("div", { title, className: "flex items-center", children: control }, label + ":c")
  ];
}
function actionButton(label, onClick) {
  return jsx37("button", {
    onClick,
    className: "px-2 py-0.5 rounded text-[10px] font-medium cursor-pointer select-none transition-colors duration-100 ease-out hover:bg-(--ui-bg-tertiary) active:scale-95",
    style: { border: "1px solid var(--ui-stroke-secondary)" },
    children: label
  });
}
function useElementWidth() {
  const ref = useRef9(null);
  const [width, setWidth] = useState15(9999);
  useEffect7(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0] && entries[0].contentRect && entries[0].contentRect.width;
      if (typeof w === "number") setWidth(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

// desktop/src/config-pane.js
function seamsElapsedText(ms) {
  const sec = Math.max(0, Math.floor((Number(ms) || 0) / 1e3));
  return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0") + " / ~3:00";
}
function seamsTallyText(tally) {
  if (!tally || typeof tally !== "object") return "";
  return Object.keys(tally).sort().map((k2) => k2 + ": " + tally[k2]).join(" \xB7 ");
}
function hermesVerifiedText(verified) {
  const commit = String(verified && verified.commit || "");
  const date = String(verified && verified.date || "");
  return "Verified on Hermes " + (commit || "\u2014") + (date ? " (" + date + ")" : "") + ". Much of this plugin hooks into Hermes code that is not a public API \u2014 after a Hermes update, run Full check.";
}
function splitDisabledTools(rawDisabled, help) {
  const canonical = Object.keys(help);
  return {
    legacy: rawDisabled.filter((n2) => !canonical.includes(n2)),
    disabled: Object.fromEntries(canonical.map((n2) => [n2, rawDisabled.includes(n2)]))
  };
}
function ConfigPane() {
  const [status, setStatus] = useState16("loading");
  const [errorMsg, setErrorMsg] = useState16("");
  const [zoneHelp, setZoneHelp] = useState16({ disabled: "" });
  const [disabledHelp, setDisabledHelp] = useState16({});
  const [legacyDisabled, setLegacyDisabled] = useState16([]);
  const [disabled, setDisabled] = useState16({});
  const [slots, setSlots] = useState16({});
  const [auxModel, setAuxModel] = useState16("");
  const [ctxEnabled, setCtxEnabled] = useState16(true);
  const [compactHeadKeep, setCompactHeadKeep] = useState16("6");
  const [compactAutoPercent, setCompactAutoPercent] = useState16("70");
  const [compactCutPercent, setCompactCutPercent] = useState16("30");
  const [compactTailKeep, setCompactTailKeep] = useState16("20");
  const [compactAutoOn, setCompactAutoOn] = useState16(false);
  const [compactPauseMin, setCompactPauseMin] = useState16("3");
  const [compactSilenceMin, setCompactSilenceMin] = useState16("5");
  const [compactWaitTurnMin, setCompactWaitTurnMin] = useState16("10");
  const [setsOff, setSetsOff] = useState16([]);
  const [setsCatalog, setSetsCatalog] = useState16(null);
  const [setsWhy, setSetsWhy] = useState16("");
  const [dataRoot, setDataRoot] = useState16("");
  const [settingsFile, setSettingsFile] = useState16("");
  const [exportRoot, setExportRoot] = useState16("");
  const [seamsState, setSeamsState] = useState16("idle");
  const [seamsOutput, setSeamsOutput] = useState16("Check - quick seam check (search). Full check - plus the behaviour tests (minutes).");
  const [seamsTally, setSeamsTally] = useState16("");
  const [seamsFull, setSeamsFull] = useState16(false);
  const [seamsStarted, setSeamsStarted] = useState16(0);
  const [seamsNow, setSeamsNow] = useState16(0);
  const [hermesVerified, setHermesVerified] = useState16({ commit: "", date: "" });
  useEffect8(() => {
    if (seamsState !== "pending" || !seamsFull) return void 0;
    const id = setInterval(() => setSeamsNow(Date.now()), 1e3);
    return () => clearInterval(id);
  }, [seamsState, seamsFull]);
  const [openInfo, setOpenInfo] = useState16(null);
  const [hoverKey, setHoverKey] = useState16(null);
  const [tableRef, tableWidth] = useElementWidth();
  const [saveState, setSaveState] = useState16("idle");
  const [reloadState, setReloadState] = useState16("idle");
  const [reloadMsg, setReloadMsg] = useState16("");
  function applyConfigData(data) {
    if (!data || data.error) {
      setErrorMsg(data && data.error || "empty response");
      setStatus("error");
      return;
    }
    const { legacy, disabled: canonicalDisabled } = splitDisabledTools(
      Array.isArray(data.disabled_tools) ? data.disabled_tools : [],
      data.disabled_tools_help || {}
    );
    setDisabledHelp(data.disabled_tools_help || {});
    setLegacyDisabled(legacy);
    setDisabled(canonicalDisabled);
    setZoneHelp({ disabled: data.disabled_tools_zone_help || "" });
    setSlots(Object.fromEntries(Object.entries(data.tools || {}).map(([key, t]) => [key, {
      enabled: !!t.enabled,
      path: t.path || "new",
      help: t.help || "",
      params: (t.params || []).map((p) => Object.assign({}, p))
    }])));
    setCtxEnabled(!!data.context_ctl_enabled);
    setDataRoot(String(data.data_root || ""));
    setSettingsFile(String(data.settings_file || ""));
    const verified = data.hermes_verified || {};
    setHermesVerified({ commit: String(verified.commit || ""), date: String(verified.date || "") });
    const compaction = data.compaction || {};
    setCompactHeadKeep(String(compaction.head_keep ?? 6));
    setCompactAutoPercent(String(compaction.auto_percent ?? 70));
    setCompactCutPercent(String(compaction.cut_percent ?? 30));
    setCompactTailKeep(String(compaction.tail_keep ?? 20));
    setCompactAutoOn(compaction.auto_enabled === true);
    setCompactPauseMin(String(compaction.pause_minutes ?? 3));
    setCompactSilenceMin(String(compaction.silence_minutes ?? 5));
    setCompactWaitTurnMin(String(compaction.wait_turn_minutes ?? 10));
    setExportRoot(String(compaction.export_root ?? ""));
    setSetsOff(Array.isArray(compaction.sets_off) ? compaction.sets_off.map(String) : []);
    setStatus("ready");
  }
  useEffect8(() => {
    if (!hasRest()) {
      setStatus("unavailable");
      return;
    }
    let cancelled = false;
    loadConfig().then((data) => {
      if (!cancelled) applyConfigData(data);
    }).catch((err) => {
      if (cancelled) return;
      setErrorMsg(err && err.message || String(err));
      setStatus("error");
    });
    loadCompactSets().then((r) => {
      if (cancelled) return;
      if (r && r.ok === true && Array.isArray(r.sets)) setSetsCatalog(r.sets);
      else setSetsWhy(r && r.error || "no answer");
    });
    return () => {
      cancelled = true;
    };
  }, []);
  useEffect8(() => {
    if (!openInfo) return;
    const clear = () => setOpenInfo(null);
    document.addEventListener("click", clear);
    return () => document.removeEventListener("click", clear);
  }, [openInfo]);
  if (status === "loading") {
    return jsx38("div", { className: "flex items-center justify-center h-full text-xs text-(--ui-text-quaternary)", style: SELECTABLE, children: "loading config\u2026" });
  }
  if (status === "unavailable") {
    return jsx38("div", { className: "flex items-center justify-center h-full text-xs text-(--ui-text-quaternary) text-center px-4", style: SELECTABLE, children: "REST is not available in this build \u2014 Config needs the live plugin API." });
  }
  if (status === "error") {
    return jsx38("div", { className: "flex items-center justify-center h-full text-xs text-center px-4", style: Object.assign({ color: "var(--dt-danger, #e05252)" }, SELECTABLE), children: "/config failed: " + errorMsg });
  }
  const setDisabledOne = (name, v) => setDisabled((cur) => Object.assign({}, cur, { [name]: v }));
  const setSlot = (key, patch) => setSlots((cur) => Object.assign({}, cur, { [key]: Object.assign({}, cur[key], patch) }));
  function handleSave() {
    setSaveState("saving");
    const patch = {
      disabled_tools: legacyDisabled.concat(Object.keys(disabled).filter((n2) => disabled[n2])),
      tools: Object.fromEntries(Object.entries(slots).map(([key, s]) => [key, {
        enabled: s.enabled,
        path: s.path,
        params: s.params.map((p) => ({ key: p.key, value: p.value }))
      }])),
      context_ctl_enabled: ctxEnabled,
      // Битое/пустое поле не уходит в патч вовсе (сервер держит своё прежнее значение) — та же
      // осторожность, что и у остальных числовых полей этого файла (`anchor_gap` и т.п.):
      // отправить NaN в конфиг компакции значило бы молча сломать расстановку галочек кнопки.
      compaction: Object.fromEntries(
        [
          ["head_keep", compactHeadKeep],
          ["auto_percent", compactAutoPercent],
          ["cut_percent", compactCutPercent],
          ["tail_keep", compactTailKeep],
          ["pause_minutes", compactPauseMin],
          ["silence_minutes", compactSilenceMin],
          ["wait_turn_minutes", compactWaitTurnMin]
        ].filter(([, v]) => Number.isFinite(Number(v)) && String(v).trim() !== "").map(([k2, v]) => [k2, Number(v)]).concat([["export_root", exportRoot], ["auto_enabled", compactAutoOn], ["sets_off", setsOff]])
      )
    };
    saveConfig(patch).then((data) => {
      applyConfigData(data);
      setSaveState("saved");
    }).catch((err) => {
      setErrorMsg(err && err.message || String(err));
      setSaveState("error");
    });
  }
  function handleReload() {
    setReloadState("pending");
    reloadPlugin().then((data) => {
      if (data && data.ok) {
        setReloadState("done");
        return;
      }
      setReloadMsg(data && data.error || "unknown error");
      setReloadState("error");
    }).catch((err) => {
      setReloadMsg(err && err.message || String(err));
      setReloadState("error");
    });
  }
  function handleCheckSeams(full) {
    if (seamsState === "pending") return;
    setSeamsState("pending");
    setSeamsFull(!!full);
    setSeamsTally("");
    setSeamsStarted(Date.now());
    setSeamsNow(Date.now());
    if (full) setSeamsOutput("running the seam tests on the server\u2026 (about 3 minutes - the timer next to the buttons shows it is alive)");
    checkSeams(full).then((data) => {
      if (!data || data.error) {
        setSeamsOutput(data && data.error || "no answer");
        setSeamsTally("");
        setSeamsState("error");
        return;
      }
      setSeamsOutput(data.text || "");
      setSeamsTally(seamsTallyText(data.tally));
      setSeamsState(data.ok ? "done" : "error");
    }).catch((err) => {
      setSeamsOutput(err && err.message || String(err));
      setSeamsTally("");
      setSeamsState("error");
    });
  }
  const head = jsxs34("div", {
    className: "flex items-center justify-between gap-2 px-2 py-1 border-b border-(--ui-stroke-quaternary) shrink-0",
    style: { fontSize: "0.55rem" },
    children: [
      jsx38("span", { className: "text-(--ui-text-quaternary)", style: SELECTABLE, children: "tool settings need a new agent session to apply \u2014 compaction settings (ours) may apply live" }),
      jsxs34("div", { className: "flex items-center gap-2 shrink-0", children: [
        saveState === "saved" ? jsx38("span", { className: "text-(--dt-primary)", style: SELECTABLE, children: "saved" }) : null,
        saveState === "error" ? jsx38("span", { style: Object.assign({ color: "var(--dt-danger, #e05252)" }, SELECTABLE), children: "save failed: " + errorMsg }) : null
      ] })
    ]
  });
  const disabledZone = zone([
    sectionHead("Disabled Hermes tools", "zone-disabled", zoneHelp.disabled, openInfo, setOpenInfo),
    // minmax(170px, 1fr) + auto-fill: a column NEVER shrinks below 170px (truncation still
    // kicks in if it somehow had to), but extra panel width is now shared out evenly across
    // every existing column instead of being wasted — the earlier "compress unevenly" bug was
    // actually the missing name-truncation fix (now in InfoLabel), not this stretch by itself:
    // auto-fill keeps one shared track size for the whole grid, not a per-row thing.
    jsx38("div", {
      className: "grid",
      style: { gridTemplateColumns: "repeat(auto-fill, minmax(170px, 1fr))" },
      children: Object.keys(disabledHelp).map((name) => disableRow(name, disabledHelp[name], !!disabled[name], (v) => setDisabledOne(name, v), openInfo, setOpenInfo))
    })
  ]);
  const showParams = tableWidth >= COL_PX.tool + COL_PX.params + COL_PX.enabled;
  const showPath = tableWidth >= COL_PX.tool + COL_PX.params + COL_PX.enabled + COL_PX.path;
  const gridCols = [COL_PX.tool + "px"];
  if (showParams) gridCols.push(COL_PX.params + "px");
  gridCols.push(COL_PX.enabled + "px");
  if (showPath) gridCols.push(COL_PX.path + "px");
  const headerCell = (text, center) => jsx38("div", {
    className: "text-[0.6rem] font-semibold uppercase tracking-wide text-(--ui-text-quaternary) opacity-50 px-1 pb-0.5" + (center ? " text-center" : ""),
    style: SELECTABLE,
    children: text
  });
  const slotsZone = zone([
    sectionHead("Our tools", "zone-slots", "", openInfo, setOpenInfo),
    // No alignItems override: grid's default `stretch` makes every cell in a row match the
    // tallest one (Params, when it stacks several controls) — needed so hover paints one even
    // rectangle instead of a staircase.
    jsx38("div", { ref: tableRef, className: "grid", style: { gridTemplateColumns: gridCols.join(" "), rowGap: 0 }, children: [
      headerCell("Tool Name"),
      showParams ? headerCell("Params") : null,
      headerCell("Enable", true),
      showPath ? headerCell("Path") : null,
      ...Object.keys(slots).flatMap((key) => toolTableRow(key, Object.assign({}, slots[key], { openInfo, setOpenInfo }), (patch) => setSlot(key, patch), showParams, showPath, hoverKey, setHoverKey))
    ].filter(Boolean) })
  ]);
  const auxZone = zone([
    sectionHead("Aux model \u2014 compression (not wired)", "zone-aux", "", openInfo, setOpenInfo),
    jsx38("div", { className: "px-1 pb-0.5", children: textInput(auxModel, (e) => setAuxModel(e.currentTarget.value), "(empty \u2014 falls back to their default)") })
  ]);
  const compactionZone = zone([
    sectionHead("Compaction", "zone-compaction", "", openInfo, setOpenInfo),
    jsx38("div", { className: "px-1 pb-0.5", children: jsxs34("label", { className: "inline-flex items-center gap-2 cursor-pointer text-[10px]", children: [
      jsx38("input", { type: "checkbox", checked: ctxEnabled, onChange: (e) => setCtxEnabled(e.currentTarget.checked), style: CHECKBOX_STYLE }),
      jsx38("span", { style: SELECTABLE, children: "context_ctl.enabled \u2014 our context engine kill switch" })
    ] }) }),
    // Числа и выключатель компакции (Plan16-Task03; автомат и ход прогона — 2026-09-30) — одной
    // СЕТКОЙ в четыре колонки «подпись · поле», мелко (владелец: «раскидано хрен пойми как»).
    // Сохраняются общей кнопкой Save выше, не своей.
    jsx38("div", {
      className: "px-1 pb-0.5 grid items-center text-[10px]",
      style: {
        gridTemplateColumns: "repeat(4, max-content 2.5rem)",
        columnGap: "0.4rem",
        rowGap: "0.25rem",
        justifyContent: "start"
      },
      children: [
        compactCell(
          "head keep",
          "compact_head_keep \u2014 messages at the start never pre-ticked",
          smallNumberField(compactHeadKeep, (e) => setCompactHeadKeep(e.currentTarget.value))
        ),
        compactCell(
          "auto %",
          "compact_auto_percent \u2014 context fill at which the automaton compacts",
          smallNumberField(compactAutoPercent, (e) => setCompactAutoPercent(e.currentTarget.value))
        ),
        compactCell(
          "cut %",
          "compact_cut_percent \u2014 OUR share of the middle to pre-tick",
          smallNumberField(compactCutPercent, (e) => setCompactCutPercent(e.currentTarget.value))
        ),
        compactCell(
          "tail keep",
          "compact_tail_keep \u2014 messages at the end never pre-ticked",
          smallNumberField(compactTailKeep, (e) => setCompactTailKeep(e.currentTarget.value))
        ),
        compactCell(
          "auto on",
          'compact_auto_enabled \u2014 the automaton compacts at "auto %"; off = the decision is only shown',
          jsx38("input", {
            type: "checkbox",
            checked: compactAutoOn,
            onChange: (e) => setCompactAutoOn(e.currentTarget.checked),
            style: Object.assign({}, CHECKBOX_STYLE, { width: "12px", height: "12px", cursor: "pointer" })
          })
        ),
        compactCell(
          "pause min",
          "compact_auto_pause_minutes \u2014 after two automaton failures in a row, no new attempt for this long",
          smallNumberField(compactPauseMin, (e) => setCompactPauseMin(e.currentTarget.value))
        ),
        compactCell(
          "silence min",
          "compact_silence_minutes \u2014 no step of a running compaction this long: the progress line turns yellow (the run is not killed)",
          smallNumberField(compactSilenceMin, (e) => setCompactSilenceMin(e.currentTarget.value))
        ),
        compactCell(
          "wait turn min",
          "compact_wait_turn_minutes \u2014 manual compaction done while a turn runs waits this long for the turn to end, then writes; 0 = do not wait",
          smallNumberField(compactWaitTurnMin, (e) => setCompactWaitTurnMin(e.currentTarget.value))
        )
      ].flat()
    }),
    // Наборы опроса, которые видит модель (`Plan34` Task 8): снятая галочка — набор выключен для
    // всех компакций (кнопка, команды, автомат); разово — в окне подтверждения. `general` — запасной.
    // Как в окне подтверждения (владелица, 2026-10-16: «вывалил в кучу»): подпись, наборы сеткой по 4,
    // под ними неприметная подсказка.
    jsx38("div", { className: "px-1 pb-0.5 flex flex-col gap-1 text-[10px]", children: [
      jsx38("span", {
        style: SELECTABLE,
        title: "compact_sets_off \u2014 question sets the interrogation model is NOT offered",
        children: "question sets:"
      }, "h"),
      setsCatalog ? jsx38("div", {
        style: { display: "grid", gridTemplateColumns: "repeat(4, max-content)", columnGap: "1.25rem", rowGap: "0.15rem" },
        children: setsCatalog.map((s) => jsxs34("label", {
          className: "inline-flex items-center gap-1 cursor-pointer select-none",
          title: compactSetTitle(s),
          children: [
            jsx38("input", {
              type: "checkbox",
              checked: !!s.fallback || !setsOff.includes(s.name),
              disabled: !!s.fallback,
              style: Object.assign({}, CHECKBOX_STYLE, { width: "12px", height: "12px", cursor: "pointer" }),
              onChange: (e) => {
                const on = e.currentTarget.checked;
                setSetsOff((cur) => on ? cur.filter((n2) => n2 !== s.name) : cur.concat([s.name]));
              }
            }),
            compactSetLabel(s)
          ]
        }, s.name))
      }, "grid") : jsx38("span", {
        className: "text-(--ui-text-quaternary)",
        style: SELECTABLE,
        children: `off: ${setsOff.length ? setsOff.join(", ") : "none"} \u2014 ${setsWhy || "loading\u2026"}`
      }, "why"),
      jsx38("span", {
        className: "leading-snug text-(--ui-text-quaternary)",
        style: SELECTABLE,
        children: "Sets of questions the agent is asked about the conversation. For each topic it picks on its own which set fits; an unticked set is never offered, in any compaction. For one run only \u2014 the ticks in the compaction confirm window. Hover a set to see its questions."
      }, "hint")
    ] }),
    // Предупреждение про направление третьего числа (владелец, 2026-09-22) — ставится тут же,
    // рядом с полем, а не только в комментарии кода: экран этот числа и меняет.
    jsx38("div", {
      className: "px-1 pb-0.5 text-[0.6rem] text-(--ui-text-quaternary)",
      style: SELECTABLE,
      children: '"cut %" is OUR share to cut \u2014 not their "shrink to" target. Same shape of number, opposite meaning.'
    })
  ]);
  const referenceZone = zone([
    sectionHead("Reference (read-only, from their config)", "zone-reference", "", openInfo, setOpenInfo),
    jsx38("div", { className: "px-1 pb-0.5 flex flex-col text-[10px]", children: [
      ["auxiliary.compression.provider", "auto"],
      ["auxiliary.compression.model", "(not set)"],
      ["auxiliary.compression.timeout", "60000"]
    ].map(([k2, v]) => jsxs34("div", { style: SELECTABLE, children: [k2 + ": ", jsx38("span", { className: "text-(--ui-text-quaternary)", children: v })] }, k2)) })
  ]);
  const dataZone = zone([
    sectionHead("Paths", "zone-data", "", openInfo, setOpenInfo),
    // Только чтение: дом плагина и файл, куда Save пишет ваши настройки (умолчания — plugin.yaml).
    jsxs34("div", { className: "px-1 pb-0.5 grid gap-x-2 text-[10px]", style: { gridTemplateColumns: "max-content 1fr" }, children: [
      jsx38("span", { className: "text-(--ui-text-quaternary)", style: SELECTABLE, children: "data root" }),
      jsx38("span", { className: "font-mono break-all", style: SELECTABLE, children: dataRoot || "\u2014" }),
      jsx38("span", { className: "text-(--ui-text-quaternary)", style: SELECTABLE, children: "your settings" }),
      jsx38("span", { className: "font-mono break-all", style: SELECTABLE, children: settingsFile || "\u2014" })
    ] }),
    // НАСТОЯЩАЯ настройка, в отличие от строки выше: едет в `plugin.yaml` и обратно. Само
    // сохранение сообщений ещё не написано (`Vision09`) — ключ заведён заранее, чтобы путь
    // выбирался один раз и на виду, а не изобретался первым кодом, которому он понадобится.
    jsx38("div", {
      className: "px-1 pb-0.5 text-[0.6rem] text-(--ui-text-quaternary)",
      style: SELECTABLE,
      children: "export root \u2014 where messages and sessions are saved as .md (empty = plugin home)"
    }),
    // Подсказка косыми ВПЕРЁД, хотя система Windows: обратный слэш в конце строкового
    // литерала экранирует закрывающую кавычку, и это уже стоило одной сломанной сборки.
    // Путь всё равно разбирает `pathlib`, которому направление косой безразлично.
    jsx38("div", { className: "px-1 pb-0.5", children: textInput(exportRoot, (e) => setExportRoot(e.currentTarget.value), "<hermes home>/filetools/export/", "w-full font-mono") })
  ]);
  const seamsZone = zone([
    sectionHead("Check Seams", "zone-seams", "", openInfo, setOpenInfo),
    // Строка из двух колонок (владелец): [Check] [вердикт], под ней — поле отчёта во всю ширину.
    jsxs34("div", { className: "px-1 pb-0.5 flex flex-col gap-1", children: [
      jsxs34("div", { className: "grid gap-2 items-center", style: { gridTemplateColumns: "max-content 1fr" }, children: [
        jsxs34("div", { className: "flex items-center gap-1", children: [
          actionButton(seamsState === "pending" && !seamsFull ? "Checking\u2026" : "Check", () => handleCheckSeams(false)),
          actionButton(seamsState === "pending" && seamsFull ? "Running tests\u2026" : "Full check", () => handleCheckSeams(true))
        ] }),
        jsx38("span", {
          className: "font-medium text-[10px]" + (seamsState === "error" ? "" : " text-(--dt-primary)"),
          style: seamsState === "error" ? Object.assign({ color: "var(--dt-danger, #e05252)" }, SELECTABLE) : SELECTABLE,
          children: seamsState === "pending" && seamsFull ? "running\u2026 " + seamsElapsedText(seamsNow - seamsStarted) : seamsTally
        })
      ] }),
      // ЗАЧЕМ ЭТА ЗОНА, одной строкой (владелец, 2026-10-02): многое у нас держится за
      // НЕПУБЛИЧНЫЙ код Hermes — другого способа достать это нет, — и их обновление может это
      // сломать. Поэтому каждая версия называет коммит Hermes, на котором её проверили.
      jsx38("div", {
        className: "text-[10px] text-(--ui-text-quaternary)",
        style: SELECTABLE,
        children: hermesVerifiedText(hermesVerified)
      }),
      textArea(seamsOutput, seamsState === "idle" ? 2 : 28, true)
    ] })
  ]);
  const reloadZone = jsx38("div", { className: "shrink-0 px-1 py-1 flex items-center gap-2 border-t border-(--ui-stroke-quaternary)", children: [
    actionButton(saveState === "saving" ? "Saving\u2026" : "Save", handleSave),
    actionButton(reloadState === "pending" ? "Reloading\u2026" : "\u21BB Reload plugin", handleReload),
    reloadState === "done" ? jsx38("span", { className: "text-(--dt-primary) text-xs", style: SELECTABLE, children: "reloaded" }) : null,
    reloadState === "error" ? jsx38("span", { className: "text-xs", style: Object.assign({ color: "var(--dt-danger, #e05252)" }, SELECTABLE), children: "reload failed: " + reloadMsg }) : null
  ] });
  return jsxs34("div", {
    className: "flex flex-col h-full w-full overflow-hidden",
    style: CAPPED,
    children: [
      head,
      jsx38("div", {
        className: "flex-1 overflow-y-auto py-0.5 px-1",
        // Порядок (владелец, 2026-09-30): рабочие зоны сверху, справка ниже,
        // неподключённое (Aux model) — в самом низу.
        children: [
          disabledZone,
          slotsZone,
          compactionZone,
          seamsZone,
          referenceZone,
          dataZone,
          auxZone
        ]
      }),
      reloadZone
    ]
  });
}

// desktop/src/file-ops-strip.js
import { jsx as jsx39, jsxs as jsxs35 } from "react/jsx-runtime";
var FILTER_LABEL = "show erased";
function FilterBar({ showErased, hidden }) {
  return jsxs35("div", {
    className: "flex items-center gap-2 px-2 py-1 shrink-0 border-t border-(--ui-stroke-secondary)",
    style: { fontSize: "0.55rem" },
    children: [
      jsxs35("label", {
        className: "inline-flex items-center gap-1 cursor-pointer select-none",
        // Клик не всплывает: выше на document висят снятие режима выбора и снятие затенения.
        onClick: (e) => {
          e.stopPropagation();
        },
        title: "show operations that were erased from the context",
        children: [
          jsx39("input", {
            type: "checkbox",
            checked: !!showErased,
            onChange: (e) => setShowErased(e && e.target ? e.target.checked : !showErased),
            style: { accentColor: "var(--dt-primary)", width: "10px", height: "10px" }
          }),
          jsx39("span", { style: { color: "var(--ui-text-secondary)" }, children: FILTER_LABEL })
        ]
      }),
      // Сколько спрятано. Молчаливое сокрытие — ложь того же рода, что молчаливое удаление:
      // без этого числа человек не поймёт, влияет ли фильтр на что-нибудь вообще.
      hidden > 0 && !showErased ? jsx39("span", {
        style: { color: "var(--ui-text-quaternary)" },
        children: `${hidden} hidden`
      }) : null
    ]
  });
}

// desktop/src/llmops/llmops-host.js
import { jsx as jsx41 } from "react/jsx-runtime";
import { useMemo as useMemo4 } from "react";

// desktop/src/llmops/llmops-window.js
import { jsx as jsx40, jsxs as jsxs36 } from "react/jsx-runtime";
import { useEffect as useEffect9, useMemo as useMemo3, useRef as useRef11, useState as useState17 } from "react";

// desktop/src/llmops/llmops-core.js
function roughTokens2(text) {
  return Math.ceil(String(text || "").length / 4);
}
function msgTokens(m) {
  const t = m && m.tokens;
  return typeof t === "number" && isFinite(t) ? t : roughTokens2(m && m.text);
}
function sumTokens(list) {
  return (list || []).reduce((n2, m) => n2 + msgTokens(m), 0);
}
function kLabel(n2) {
  const v = Number(n2 || 0);
  return v >= 1e3 ? `${(v / 1e3).toFixed(1)}k` : String(v);
}
function messageBlock(m) {
  const who = m.tool ? `${m.role} ${m.tool}` : m.role;
  return `[${m.addr} ${who}]
${String(m.text || "")}`;
}
function pickedPart(list, side, pick) {
  const all = list || [];
  if (pick === true) return all;
  const k2 = Math.max(0, Math.min(all.length, Math.floor(Number(pick) || 0)));
  if (!k2) return [];
  return side === "prefix" ? all.slice(all.length - k2) : all.slice(0, k2);
}
function dataText(data, picks) {
  const p = picks || {};
  const parts = [];
  const before = pickedPart(data.prefix, "prefix", p.prefix);
  if (before.length) {
    parts.push("<context-before>\n" + before.map(messageBlock).join("\n\n") + "\n</context-before>");
  }
  parts.push("<messages>\n" + (data.target || []).map(messageBlock).join("\n\n") + "\n</messages>");
  const after = pickedPart(data.suffix, "suffix", p.suffix);
  if (after.length) {
    parts.push("<context-after>\n" + after.map(messageBlock).join("\n\n") + "\n</context-after>");
  }
  return parts.join("\n\n");
}
function presetTurns(preset, data, picks, values) {
  const pr = preset || {};
  const body = dataText(data, picks);
  const tpl = String(pr.user || "{{data}}");
  const user = tpl.includes("{{data}}") ? tpl.split("{{data}}").join(body) : `${tpl}

${body}`;
  const lines = (pr.settings || []).map((s) => {
    const v = (values || {})[s.key] || (s.options || [])[0];
    return v ? `${s.label}: ${v}` : "";
  }).filter(Boolean);
  const system = [String(pr.system || ""), lines.join("\n")].filter(Boolean).join("\n\n");
  return [
    { role: "system", content: system },
    { role: "user", content: user }
  ];
}
function conversationTokens(turns) {
  return (turns || []).reduce((n2, t) => n2 + roughTokens2(t.content), 0);
}
var WRAPS = ["as-it-was", "summarisation", "compaction", "none"];
function asItWas(frames) {
  return (frames || []).some((f) => f && f.kind === "compaction") ? "compaction" : "none";
}
function resolveWrap(wrap, frames) {
  return wrap === "as-it-was" ? asItWas(frames) : wrap;
}
function wrapResult(body, wrap, frames, frameTexts) {
  const kind = resolveWrap(wrap, frames);
  if (kind === "none") return String(body || "");
  const stripped = (frames || []).find((f) => f && f.kind === kind);
  const fr = stripped || (frameTexts || {})[kind];
  if (!fr) return String(body || "");
  return `${fr.head}
${String(body || "")}
${fr.tail}`;
}
function dropReply(result, text) {
  return result.cleared ? result : { body: String(text || ""), cleared: false };
}
function clearResult() {
  return { body: "", cleared: true };
}
function moveToResult(text) {
  return { body: String(text || ""), cleared: true };
}
function unwrapMessages(body) {
  const lines = String(body || "").trim().split("\n");
  let from = 0;
  let to = lines.length;
  if (lines[0] && lines[0].trim() === "<messages>") from = 1;
  if (to > from && lines[to - 1].trim() === "</messages>") to -= 1;
  if (from === 0 && to === lines.length) return null;
  return lines.slice(from, to).join("\n").trim();
}
function stripHeaders(body, data) {
  const d = data || {};
  const heads = new Set([...d.prefix || [], ...d.target || [], ...d.suffix || []].map((m) => messageBlock({ ...m, text: "" }).trim()));
  const lines = String(body || "").split("\n");
  const kept = lines.filter((l) => !heads.has(l.trim()));
  if (kept.length === lines.length) return null;
  return kept.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}
function trainRows(data) {
  const t = (data && data.target || []).map((m) => ({ ...m, bubble: false }));
  const b = (data && data.bubbles || []).map((m) => ({ ...m, bubble: true }));
  return t.concat(b).sort((x, y) => String(x.addr).localeCompare(String(y.addr)));
}
function targetLabel(data) {
  const t = data && data.target || [];
  if (!t.length) return "nothing selected";
  const span = t.length === 1 ? t[0].addr : `${t[0].addr}\u2026${t[t.length - 1].addr}`;
  return `${span} \xB7 ${t.length} msg`;
}
function dragPos(x, y, width, viewW, viewH, keep = 120, headPx = 32) {
  const nx = Math.min(Math.max(x, keep - width), viewW - keep);
  const ny = Math.min(Math.max(y, 0), viewH - headPx);
  return { x: Math.round(nx), y: Math.round(ny) };
}
var MIN_W = 480;
var MIN_H2 = 280;
function placeOf(saved, viewW, viewH) {
  const s = saved || {};
  if (![s.x, s.y, s.w, s.h].every((v) => typeof v === "number" && Number.isFinite(v))) return null;
  const w = Math.round(Math.min(Math.max(s.w, MIN_W), viewW));
  const h = Math.round(Math.min(Math.max(s.h, MIN_H2), viewH));
  return { ...dragPos(s.x, s.y, w, viewW, viewH), w, h };
}
function frameFor(kind, frameTexts, kept) {
  const fr = (frameTexts || {})[kind];
  if (!fr || kind === "none") return null;
  const apart = kind === "compaction" && !!kept;
  return {
    head: String(apart && fr.head_kept || fr.head || ""),
    tail: String(apart && fr.tail_kept || fr.tail || ""),
    tailApart: apart
  };
}

// desktop/src/llmops/llmops-window.js
var PLACE_KEY = "hermes-filetools.llmops.place";
function loadPlace() {
  try {
    return JSON.parse(window.localStorage.getItem(PLACE_KEY) || "null");
  } catch (_) {
    return null;
  }
}
function savePlace(place) {
  try {
    window.localStorage.setItem(PLACE_KEY, JSON.stringify(place));
  } catch (_) {
  }
}
var SURFACE = "oklch(from var(--ui-editor-surface-background) l c h / 1)";
var ELEVATED = `color-mix(in oklch, ${SURFACE}, oklch(from var(--ui-bg-elevated) l c h / 1) 50%)`;
var STROKE = "1px solid var(--ui-stroke-secondary)";
var STROKE_SOFT = "1px solid var(--ui-stroke-quaternary)";
var TEXT_DIM = "var(--ui-text-quaternary)";
var TEXT_2 = "var(--ui-text-secondary)";
var ACCENT = "var(--dt-primary)";
var SEL = { userSelect: "text", WebkitUserSelect: "text" };
function mixTone2(baseVar) {
  return `color-mix(in oklch, var(${baseVar}), var(--dt-primary) 30%)`;
}
var ROLE_TONE2 = {
  user: mixTone2("--ui-blue"),
  assistant: mixTone2("--ui-green"),
  tool: mixTone2("--ui-cyan"),
  system: mixTone2("--ui-yellow")
};
var toneOf2 = (role) => ROLE_TONE2[role] || TEXT_DIM;
var WIN_W = "min(72rem, calc(100vw - 2rem))";
var WIN_H = "min(46rem, calc(100vh - 5rem))";
var WRAP_LABEL = { "as-it-was": "as it was", summarisation: "summarisation", compaction: "compaction", none: "no wrap" };
function Btn({ children, onClick, disabled, title, strong, tone }) {
  return jsx40("button", {
    onClick,
    disabled,
    title,
    className: "cursor-pointer select-none",
    style: {
      padding: strong ? "0.25rem 0.9rem" : "0.15rem 0.5rem",
      borderRadius: "0.375rem",
      fontSize: strong ? "0.75rem" : "0.65rem",
      fontWeight: strong ? 600 : 500,
      border: `1px solid ${tone || "var(--ui-stroke-secondary)"}`,
      color: tone || "inherit",
      background: "transparent",
      opacity: disabled ? 0.35 : 1,
      whiteSpace: "nowrap"
    },
    children
  });
}
function Select({ value, options, onChange, title }) {
  return jsx40("select", {
    value,
    title,
    onChange: (e) => onChange(e.target.value),
    style: {
      fontSize: "0.65rem",
      padding: "0.1rem 0.25rem",
      borderRadius: "0.3rem",
      border: STROKE,
      background: SURFACE,
      color: "inherit",
      maxWidth: "11rem"
    },
    children: options.map((o) => jsx40("option", { value: o.value, children: o.label }, o.value))
  });
}
function Label({ children }) {
  return jsx40("div", { style: { fontSize: "0.55rem", letterSpacing: "0.06em", color: TEXT_DIM, textTransform: "uppercase", padding: "0.25rem 0" }, children });
}
function ModeSwitch({ raw, onRaw, md, editLabel }) {
  if (!md) return null;
  const side = (on, label, title, value) => jsx40("button", {
    onClick: (e) => {
      e.stopPropagation();
      onRaw(value);
    },
    title,
    className: "cursor-pointer select-none",
    style: {
      padding: "0.05rem 0.35rem",
      fontSize: "0.65rem",
      border: "none",
      color: on ? ACCENT : TEXT_DIM,
      background: on ? `color-mix(in oklch, ${ACCENT}, transparent 85%)` : "transparent"
    },
    children: label
  });
  return jsxs36("span", {
    style: { display: "inline-flex", border: STROKE, borderRadius: "0.375rem", overflow: "hidden", flexShrink: 0 },
    children: [
      side(!raw, "\u{1F4D6}", "view \u2014 rendered markdown", false),
      side(raw, "\u270E", editLabel || "edit \u2014 the raw text", true)
    ]
  });
}
function Md({ text, md, style, className }) {
  return jsx40("div", {
    "data-selectable-text": "true",
    className,
    style: {
      fontSize: "0.75rem",
      lineHeight: 1.5,
      wordBreak: "break-word",
      minWidth: 0,
      ...md ? {} : { whiteSpace: "pre-wrap" },
      ...SEL,
      ...style
    },
    children: md ? md(text) : String(text || "")
  });
}
function Chip2({ m, open, onOpen }) {
  const tone = toneOf2(m.role);
  const what = m.tool || String(m.text || "").slice(0, 40) || (m.calls ? `\u2192 ${m.calls.join(", ")}` : "");
  return jsxs36("div", {
    onClick: (e) => onOpen(open ? null : { addr: m.addr, rect: e.currentTarget.getBoundingClientRect() }),
    title: m.bubble ? "bubble: floats out, stays in the tape, not sent" : void 0,
    style: {
      display: "flex",
      alignItems: "center",
      gap: "0.3rem",
      minWidth: 0,
      cursor: "pointer",
      padding: "0.15rem 0.45rem",
      borderRadius: "0.75rem",
      fontSize: "0.6rem",
      lineHeight: 1.2,
      backgroundColor: `color-mix(in oklch, ${tone}, transparent ${open ? 70 : 85}%)`,
      color: tone,
      border: m.bubble ? `1px dashed ${tone}` : "1px solid transparent",
      opacity: m.bubble ? 0.75 : 1,
      boxShadow: open ? `inset 0 0 0 1px color-mix(in oklch, ${tone}, transparent 30%)` : "none"
    },
    children: [
      m.bubble ? jsx40("span", { children: "\u25E7" }) : null,
      jsx40("span", { style: { fontFamily: "monospace", flexShrink: 0 }, children: m.addr }),
      jsx40("span", { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0, flex: 1 }, children: what }),
      jsx40("span", { style: { color: TEXT_DIM, flexShrink: 0 }, children: kLabel(msgTokens(m)) })
    ]
  });
}
var POPUP_W_REM = 26;
function ChipPopup({ m, rect, onClose, onCopy, md }) {
  const tone = toneOf2(m.role);
  const [raw, setRaw] = useState17(false);
  const rem = 16;
  const w = POPUP_W_REM * rem;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const left = rect.right + 8 + w <= vw ? rect.right + 8 : Math.max(8, rect.left - 8 - w);
  const top = Math.min(Math.max(8, rect.top), Math.max(8, vh - 0.6 * vh - 8));
  return jsxs36("div", {
    onClick: (e) => e.stopPropagation(),
    style: {
      position: "fixed",
      left: `${left}px`,
      top: `${top}px`,
      width: `${POPUP_W_REM}rem`,
      maxHeight: "60vh",
      zIndex: 950,
      display: "flex",
      flexDirection: "column",
      background: SURFACE,
      border: STROKE,
      borderRadius: "0.5rem",
      boxShadow: "0 8px 28px rgba(0,0,0,0.45)"
    },
    children: [
      jsxs36("div", {
        style: { display: "flex", alignItems: "center", gap: "0.4rem", padding: "0.3rem 0.5rem", borderBottom: STROKE_SOFT, fontSize: "0.62rem", userSelect: "none" },
        children: [
          jsx40("span", { style: { fontFamily: "monospace", color: tone, fontWeight: 600 }, children: m.addr }),
          jsx40("span", { style: { color: tone }, children: m.tool ? `${m.role} \xB7 ${m.tool}` : m.role }),
          m.bubble ? jsx40("span", { style: { color: TEXT_DIM }, children: "\u25E7 bubble \u2014 stays in the tape, not sent" }) : null,
          jsx40("span", { style: { color: TEXT_DIM, marginLeft: "auto" }, children: kLabel(msgTokens(m)) }),
          jsx40(ModeSwitch, { raw, onRaw: setRaw, md, editLabel: "raw \u2014 the text as the model gets it" }),
          jsx40(Btn, { onClick: onCopy, title: "copy the text", children: "\u29C9" }),
          jsx40(Btn, { onClick: onClose, title: "close", children: "\xD7" })
        ]
      }),
      md && !raw ? jsx40(Md, { text: m.text, md, style: { overflowY: "auto", padding: "0.45rem 0.55rem" } }) : jsx40("div", {
        style: { overflowY: "auto", padding: "0.45rem 0.55rem", fontSize: "0.7rem", lineHeight: 1.45, whiteSpace: "pre-wrap", wordBreak: "break-word", ...SEL },
        children: String(m.text || "")
      })
    ]
  });
}
function Wagon({ name, list, count, onCount }) {
  const n2 = (list || []).length;
  const part = pickedPart(list, name, count);
  const on = part.length > 0;
  const span = (l) => l.length ? `${l[0].addr}\u2026${l[l.length - 1].addr}` : "nothing";
  return jsxs36("div", {
    title: n2 ? `${name}: ${span(list)} \xB7 sent: ${span(part)}` : `${name}: empty`,
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "0.25rem",
      padding: "0.4rem 0.45rem",
      borderRadius: "0.5rem",
      fontSize: "0.6rem",
      background: ELEVATED,
      border: STROKE_SOFT,
      borderLeft: `3px solid ${on ? ACCENT : "var(--ui-stroke-secondary)"}`,
      opacity: n2 ? 1 : 0.4,
      userSelect: "none"
    },
    children: [
      jsxs36("label", {
        style: { display: "flex", alignItems: "center", gap: "0.35rem", cursor: n2 ? "pointer" : "default" },
        children: [
          jsx40("input", { type: "checkbox", checked: on, disabled: !n2, onChange: () => onCount(on ? 0 : n2), style: { margin: 0 } }),
          jsx40("span", { style: { fontWeight: 600 }, children: name }),
          jsx40("span", {
            style: { color: TEXT_DIM, marginLeft: "auto" },
            children: `${part.length} of ${n2} \xB7 ${kLabel(sumTokens(part))}`
          })
        ]
      }),
      // The prefix counts from its end — its slider grows toward the target from the right; the
      // suffix counts from its start.
      n2 > 1 ? jsx40("input", {
        type: "range",
        min: 0,
        max: n2,
        step: 1,
        value: part.length,
        title: name === "prefix" ? "messages right before the selection" : "messages right after the selection",
        onChange: (e) => onCount(Number(e.target.value)),
        style: { width: "100%", margin: 0, accentColor: ACCENT, direction: name === "prefix" ? "rtl" : "ltr" }
      }) : null
    ]
  });
}
function Grow({ value, onChange, readOnly, minRem, maxRem, placeholder, onKeyDown, mono }) {
  const ref = useRef11(null);
  useEffect9(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }, [value]);
  return jsx40("textarea", {
    ref,
    value,
    readOnly,
    placeholder,
    onKeyDown,
    onChange: (e) => onChange && onChange(e.target.value),
    style: {
      width: "100%",
      resize: "none",
      boxSizing: "border-box",
      minHeight: `${minRem || 1.6}rem`,
      maxHeight: `${maxRem || 12}rem`,
      overflowY: "auto",
      fontSize: "0.7rem",
      lineHeight: 1.4,
      padding: "0.35rem 0.45rem",
      borderRadius: "0.375rem",
      border: STROKE,
      background: readOnly ? "transparent" : SURFACE,
      color: "inherit",
      fontFamily: mono ? "monospace" : "inherit",
      ...SEL
    }
  });
}
function copyText(text) {
  const clip = typeof navigator !== "undefined" && navigator && navigator.clipboard;
  if (!clip || typeof clip.writeText !== "function") return Promise.resolve(false);
  return Promise.resolve(clip.writeText(String(text || ""))).then(() => true, () => false);
}
function Turn({ turn, index, editable, folded, onFold, onEdit, onCopy, onMove, md, raw, onRaw }) {
  const tone = toneOf2(turn.role);
  const reply = turn.role === "assistant";
  const headRef = useRef11(null);
  const fold = () => {
    onFold();
    requestAnimationFrame(() => {
      const el = headRef.current;
      if (el) el.scrollIntoView({ block: "nearest" });
    });
  };
  return jsxs36("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "0.3rem",
      padding: "0.5rem 0 0.4rem",
      marginTop: index ? "0.35rem" : 0,
      borderTop: index ? `2px solid color-mix(in oklch, ${tone}, transparent 35%)` : "none"
    },
    children: [
      // The head sticks to the top of the column while its turn scrolls by (owner, 2026-10-14: the
      // role chip ran off screen, a huge turn could not be folded) - opaque, so text slides under it.
      jsxs36("div", {
        ref: headRef,
        style: {
          display: "flex",
          alignItems: "center",
          gap: "0.4rem",
          fontSize: "0.62rem",
          userSelect: "none",
          padding: "0.2rem 0.4rem",
          borderRadius: "0.35rem",
          position: "sticky",
          top: 0,
          zIndex: 1,
          background: `color-mix(in oklch, ${tone} 15%, ${SURFACE})`
        },
        children: [
          jsx40("span", {
            onClick: fold,
            style: { cursor: "pointer", color: tone, fontWeight: 700, fontSize: "0.8rem", letterSpacing: "0.02em" },
            children: `${folded ? "\u25B8" : "\u25BE"} ${turn.role}${index < 2 && editable ? " (editable)" : ""}`
          }),
          folded ? jsx40("span", {
            style: { color: TEXT_DIM, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0, flex: 1 },
            children: String(turn.content).slice(0, 80)
          }) : jsx40("span", { style: { flex: 1 } }),
          jsx40("span", { style: { color: TEXT_DIM }, children: kLabel(roughTokens2(turn.content)) }),
          folded ? null : jsx40(ModeSwitch, { raw, onRaw, md, editLabel: editable ? "edit this turn" : "raw \u2014 the text as it came" }),
          reply ? jsx40(Btn, { onClick: onCopy, title: "copy", children: "\u29C9" }) : null,
          reply ? jsx40(Btn, { onClick: onMove, title: "to the result", children: "\u2192" }) : null
        ]
      }),
      folded ? null : md && !raw ? jsx40(Md, { text: turn.content, md }) : editable ? jsx40(Grow, { value: turn.content, onChange: onEdit, maxRem: 16, mono: index === 1 }) : jsx40("div", {
        style: { fontSize: "0.7rem", lineHeight: 1.45, whiteSpace: "pre-wrap", wordBreak: "break-word", ...SEL },
        children: turn.content
      })
    ]
  });
}
var LIVE_POLL_MS = 400;
function LiveTurn({ live }) {
  const tone = toneOf2("assistant");
  const waiting = !live.text && !live.reasoning;
  const thoughtRef = useRef11(null);
  useEffect9(() => {
    const el = thoughtRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [live.reasoning]);
  const label = live.stopped ? "assistant \u2014 stopped, not in the conversation" : waiting ? "assistant \u2014 waiting for the first words\u2026" : "assistant \u2014 typing\u2026";
  return jsxs36("div", {
    style: {
      display: "flex",
      flexDirection: "column",
      gap: "0.3rem",
      padding: "0.5rem 0 0.4rem",
      marginTop: "0.35rem",
      borderTop: `2px dashed color-mix(in oklch, ${tone}, transparent 35%)`,
      opacity: live.stopped ? 0.6 : 1
    },
    children: [
      jsx40("div", {
        style: {
          fontSize: "0.62rem",
          padding: "0.2rem 0.4rem",
          borderRadius: "0.35rem",
          userSelect: "none",
          color: tone,
          fontWeight: 700,
          background: `color-mix(in oklch, ${tone} 15%, ${SURFACE})`
        },
        children: label
      }),
      live.reasoning ? jsx40("div", {
        ref: thoughtRef,
        className: "llmops-scroll",
        style: {
          fontSize: "0.65rem",
          lineHeight: 1.4,
          color: TEXT_DIM,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          maxHeight: "10rem",
          overflowY: "auto",
          borderLeft: STROKE,
          paddingLeft: "0.45rem",
          ...SEL
        },
        children: live.reasoning
      }) : null,
      live.text ? jsx40("div", {
        style: { fontSize: "0.7rem", lineHeight: 1.45, whiteSpace: "pre-wrap", wordBreak: "break-word", ...SEL },
        children: live.text
      }) : null
    ]
  });
}
function FramePiece({ text, open, onToggle, note }) {
  const first = String(text || "").split("\n")[0];
  const long = String(text || "").includes("\n");
  return jsxs36("div", {
    style: {
      fontSize: "0.62rem",
      lineHeight: 1.4,
      color: TEXT_DIM,
      padding: "0.25rem 0.45rem",
      borderRadius: "0.375rem",
      border: "1px dashed var(--ui-stroke-secondary)",
      whiteSpace: "pre-wrap",
      wordBreak: "break-word",
      maxHeight: open ? "14rem" : "none",
      overflowY: open ? "auto" : "visible",
      flexShrink: 0,
      ...SEL
    },
    children: [
      long ? jsx40("span", { onClick: onToggle, style: { cursor: "pointer", userSelect: "none" }, children: open ? "\u25BE " : "\u25B8 " }) : null,
      open || !long ? text : first,
      note ? jsx40("div", { style: { fontStyle: "italic", marginTop: "0.15rem" }, children: `\u21B3 ${note}` }) : null
    ]
  });
}
function LLMOpsWindow({ transport, presets: presetsProp, frameTexts: framesProp, markdown: md, onClose, draft }) {
  const [presets, setPresets] = useState17(presetsProp || []);
  const [frameTexts, setFrameTexts] = useState17(framesProp || {});
  const [data, setData] = useState17(null);
  const [models, setModels] = useState17({ session: "", list: [] });
  const [model, setModel] = useState17("");
  const [think, setThink] = useState17(false);
  const [presetKey, setPresetKey] = useState17((presets[0] || {}).key);
  const [picks, setPicks] = useState17({ prefix: 0, suffix: 0 });
  const [values, setValues] = useState17({});
  const [turns, setTurns] = useState17([]);
  const [sent, setSent] = useState17(false);
  const [folded, setFolded] = useState17({ 0: true });
  const [input, setInput] = useState17("");
  const [busy, setBusy] = useState17(false);
  const [status, setStatus] = useState17({ kind: "idle", note: "loading\u2026" });
  const [result, setResult] = useState17({ body: "", cleared: false });
  const [fromDraft, setFromDraft] = useState17(!!(draft && draft.text));
  const [undoBody, setUndoBody] = useState17(null);
  const [rawTurns, setRawTurns] = useState17({});
  const [resultRaw, setResultRaw] = useState17(false);
  const [wrap, setWrap] = useState17((presets[0] || {}).wrap || "as-it-was");
  const tapeRef = useRef11(null);
  const chatRun = useRef11(0);
  const [live, setLive] = useState17(null);
  const livePoll = useRef11(0);
  const liveTimer = useRef11(null);
  const liveAt = useRef11({ t: 0, r: 0 });
  const stuck = useRef11(true);
  const [chipPop, setChipPop] = useState17(null);
  const [headOpen, setHeadOpen] = useState17(false);
  const saved = useMemo3(() => placeOf(loadPlace(), window.innerWidth, window.innerHeight), []);
  const [pos, setPos] = useState17(saved ? { x: saved.x, y: saved.y } : null);
  const [size, setSize] = useState17(saved ? { w: saved.w, h: saved.h } : null);
  const boxRef = useRef11(null);
  useEffect9(() => {
    const el = boxRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let timer = null;
    const ro = new ResizeObserver(() => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        const r = el.getBoundingClientRect();
        const w = Math.round(r.width);
        const h = Math.round(r.height);
        setSize((s) => s && s.w === w && s.h === h ? s : { w, h });
        savePlace({ x: Math.round(r.left), y: Math.round(r.top), w, h });
      }, 250);
    });
    ro.observe(el);
    return () => {
      clearTimeout(timer);
      ro.disconnect();
    };
  }, []);
  const startDrag = (e) => {
    if (e.button !== 0) return;
    const tag = String(e.target && e.target.tagName || "").toLowerCase();
    if (["select", "button", "input", "option", "textarea"].includes(tag)) return;
    const box = boxRef.current && boxRef.current.getBoundingClientRect();
    if (!box) return;
    e.preventDefault();
    const dx = e.clientX - box.left;
    const dy = e.clientY - box.top;
    const move = (ev) => setPos(dragPos(ev.clientX - dx, ev.clientY - dy, box.width, window.innerWidth, window.innerHeight));
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      const r = boxRef.current && boxRef.current.getBoundingClientRect();
      if (r) savePlace({ x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) });
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };
  const preset = presets.find((p) => p.key === presetKey) || presets[0] || {};
  useEffect9(() => {
    let dead = false;
    const pack = transport.presets ? transport.presets() : Promise.resolve(null);
    Promise.all([transport.data(), transport.models(), pack]).then(([d, m, p]) => {
      if (dead) return;
      if (p && Array.isArray(p.presets) && p.presets.length) {
        setPresets(p.presets);
        setFrameTexts(p.frames || {});
        setPresetKey(p.presets[0].key);
        setWrap(p.presets[0].wrap || "as-it-was");
      }
      if (draft && draft.text) {
        setResult({ body: String(draft.text), cleared: false });
        setWrap(draft.wrap || "compaction");
      }
      setData(d);
      setModels(m);
      setModel(m.session || (m.list || [])[0] || "");
      setStatus({ kind: "idle", note: draft && draft.text ? "preview of compaction \u2014 nothing is in the tape until Replace" : "ready" });
    }, (err) => {
      if (!dead) setStatus({ kind: "error", note: `${err && err.message || err}` });
    });
    return () => {
      dead = true;
    };
  }, [transport]);
  useEffect9(() => {
    if (!data || sent) return;
    setTurns(presetTurns(preset, data, picks, values));
  }, [data, presetKey, picks, values, sent]);
  useEffect9(() => {
    const el = tapeRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns.length]);
  useEffect9(() => {
    const el = tapeRef.current;
    if (el && live && stuck.current) el.scrollTop = el.scrollHeight;
  }, [live]);
  useEffect9(() => () => {
    livePoll.current = 0;
    clearTimeout(liveTimer.current);
  }, []);
  const pollLive = (run) => {
    if (livePoll.current !== run || !transport.progress) return;
    transport.progress(liveAt.current.t, liveAt.current.r).then((p) => {
      if (livePoll.current !== run || !p || !p.ok || !p.running) return;
      if (!p.text && !p.reasoning) return;
      liveAt.current = { t: p.text_len, r: p.reasoning_len };
      setLive((l) => l ? { ...l, text: l.text + p.text, reasoning: l.reasoning + p.reasoning } : l);
    }, () => {
    }).then(() => {
      if (livePoll.current === run) liveTimer.current = setTimeout(() => pollLive(run), LIVE_POLL_MS);
    });
  };
  const endLive = () => {
    livePoll.current = 0;
    clearTimeout(liveTimer.current);
  };
  const choosePreset = (key) => {
    setPresetKey(key);
    setValues({});
    setWrap((presets.find((p) => p.key === key) || {}).wrap || "as-it-was");
    if (sent) {
      setSent(false);
      setLive((l) => l && l.stopped ? null : l);
      setStatus({ kind: "idle", note: "preset changed \u2014 conversation restarted" });
    }
  };
  const send = () => {
    if (busy || !data) return;
    let conv = turns;
    const extra = input.trim();
    if (extra) conv = conv.concat({ role: "user", content: input });
    const last = conv[conv.length - 1];
    if (!last || last.role !== "user") {
      setStatus({ kind: "idle", note: "write a message first" });
      return;
    }
    setTurns(conv);
    setInput("");
    setSent(true);
    setBusy(true);
    setStatus({ kind: "busy", note: `sending ${kLabel(conversationTokens(conv))} to ${model}\u2026` });
    const run = ++chatRun.current;
    const mine = (f) => (x) => {
      if (run === chatRun.current) f(x);
    };
    setLive({ text: "", reasoning: "", stopped: false });
    liveAt.current = { t: 0, r: 0 };
    stuck.current = true;
    livePoll.current = run;
    liveTimer.current = setTimeout(() => pollLive(run), LIVE_POLL_MS);
    transport.chat({ messages: conv.map((t) => ({ role: t.role, content: t.content })), model, think }).then(mine((r) => {
      endLive();
      if (!r || !r.ok) {
        setLive(null);
        setStatus({ kind: "error", note: r && r.error || "no answer" });
        return;
      }
      setLive(null);
      setTurns((c) => c.concat({ role: "assistant", content: r.text }));
      setResult((res) => dropReply(res, r.text));
      setFromDraft(false);
      const tk = r.tokens || {};
      setStatus({ kind: "idle", note: `${r.model || model} \xB7 ${kLabel(tk.prompt)} \u2192 ${kLabel(tk.completion)}` });
    }), mine((err) => {
      endLive();
      setLive(null);
      setStatus({ kind: "error", note: String(err && err.message || err) });
    })).then(mine(() => setBusy(false)));
  };
  const stop = () => {
    if (!busy) return;
    chatRun.current++;
    endLive();
    setLive((l) => l && (l.text || l.reasoning) ? { ...l, stopped: true } : null);
    setBusy(false);
    setStatus({ kind: "idle", note: "stopped" });
    if (transport.stop) {
      transport.stop().then((r) => {
        if (r && r.ok === false) setStatus({ kind: "error", note: `stopped here; server: ${r.error || "no stop"}` });
      }, () => {
      });
    }
  };
  const frames = data && data.frames || [];
  const frame = !data ? null : wrap === "as-it-was" ? frames[0] ? { head: frames[0].head, tail: frames[0].tail || "", tailApart: false } : null : frameFor(wrap, frameTexts, (data.bubbles || []).length > 0);
  const wrapped = data ? wrapResult(result.body, wrap, frames, frameTexts) : "";
  const targetTok = data ? sumTokens(data.target) : 0;
  const replace = () => {
    if (busy || !data || !result.body.trim()) return;
    setBusy(true);
    setStatus({ kind: "busy", note: "replacing\u2026" });
    transport.replace({
      addrs: data.addrs || data.target.map((m) => m.addr),
      text: result.body,
      wrap,
      preset: preset.key || "",
      ...fromDraft && draft && draft.origin ? { origin: draft.origin } : {}
    }).then(
      (r) => setStatus(r && r.ok ? { kind: "done", note: `${r.report || "replaced"} \u2014 the tape is reloaded` } : { kind: "error", note: r && r.error || "replace failed" }),
      (err) => setStatus({ kind: "error", note: String(err && err.message || err) })
    ).then(() => setBusy(false));
  };
  const rows = useMemo3(() => data ? trainRows(data) : [], [data]);
  const popRow = chipPop ? rows.find((r) => r.addr === chipPop.addr) : null;
  useEffect9(() => {
    if (!chipPop) return;
    const onKey = (e) => {
      if (e.key === "Escape") setChipPop(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [chipPop]);
  const stateColor = status.kind === "error" ? "var(--ui-red)" : status.kind === "done" ? "var(--ui-green)" : TEXT_2;
  const head = jsxs36("div", {
    onMouseDown: startDrag,
    title: "drag to move",
    style: { display: "flex", alignItems: "center", gap: "0.5rem", padding: "0.35rem 0.6rem", borderBottom: STROKE, userSelect: "none", fontSize: "0.65rem", cursor: "move" },
    children: [
      jsx40("span", { style: { fontWeight: 700, color: ACCENT, fontSize: "0.75rem" }, children: "LLMOps" }),
      fromDraft ? jsx40("span", {
        style: { fontWeight: 600, color: "var(--ui-yellow)", whiteSpace: "nowrap" },
        title: "the result holds the compaction distillate \u2014 the tape is untouched until Replace",
        children: "preview of compaction"
      }) : null,
      jsx40(Select, {
        value: presetKey,
        title: "preset",
        onChange: choosePreset,
        options: presets.map((p) => ({ value: p.key, label: `preset: ${p.label}` }))
      }),
      jsx40(Select, {
        value: model,
        title: "model of the route server",
        onChange: setModel,
        options: (models.list || []).map((m) => ({ value: m, label: m === models.session ? `${m} (session)` : m }))
      }),
      jsx40("span", {
        style: { fontWeight: 600, color: TEXT_2, whiteSpace: "nowrap" },
        title: "the call goes to this model",
        children: model ? `\u2192 ${model}` : "\u2192 \u2026"
      }),
      jsxs36("label", { style: { display: "flex", alignItems: "center", gap: "0.2rem", cursor: "pointer" }, children: [
        jsx40("input", { type: "checkbox", checked: think, onChange: () => setThink(!think), style: { margin: 0 } }),
        "think"
      ] }),
      jsxs36("span", {
        style: { flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", color: stateColor, ...SEL },
        title: status.note,
        children: [busy ? "\u25CC " : status.kind === "error" ? "\u26A0 " : "", status.note]
      }),
      jsx40("span", { style: { color: TEXT_DIM }, title: "the whole conversation goes every time", children: `request ${kLabel(conversationTokens(turns))}` }),
      jsx40(Btn, { onClick: onClose, title: "close (the conversation is not stored)", children: "\xD7" })
    ]
  });
  const left = jsxs36("div", {
    style: { display: "flex", flexDirection: "column", gap: "0.3rem", padding: "0 0.45rem 0.45rem", borderRight: STROKE, minHeight: 0, overflow: "hidden" },
    children: [
      jsx40(Label, { children: "data" }),
      data ? jsx40(Wagon, { name: "prefix", list: data.prefix, count: picks.prefix, onCount: (k2) => setPicks({ ...picks, prefix: k2 }) }) : null,
      jsx40("div", {
        style: { display: "flex", flexDirection: "column", gap: "0.2rem", overflowY: "auto", minHeight: 0, flex: "0 1 auto", padding: "0.1rem 0" },
        children: rows.map((m) => jsx40(Chip2, { m, open: !!chipPop && chipPop.addr === m.addr, onOpen: setChipPop }, m.addr))
      }),
      data ? jsx40(Wagon, { name: "suffix", list: data.suffix, count: picks.suffix, onCount: (k2) => setPicks({ ...picks, suffix: k2 }) }) : null,
      jsx40("div", { style: { flex: 1 } }),
      frames.length ? jsx40("div", {
        style: { fontSize: "0.55rem", color: TEXT_DIM },
        title: frames.map((f) => f.head).join("\n"),
        children: `frames stripped: ${frames.map((f) => f.kind).join(", ")}`
      }) : null,
      jsxs36("div", {
        style: { borderTop: STROKE_SOFT, paddingTop: "0.2rem", display: "flex", flexDirection: "column", gap: "0.25rem" },
        children: [
          jsx40(Label, { children: `preset \xB7 ${preset.label || ""}` }),
          (preset.settings || []).length ? (preset.settings || []).map((s) => jsxs36("label", {
            style: { display: "flex", alignItems: "center", gap: "0.3rem", fontSize: "0.6rem" },
            children: [s.label, jsx40(Select, {
              value: values[s.key] || s.options[0],
              onChange: (v) => setValues({ ...values, [s.key]: v }),
              options: s.options.map((o) => ({ value: o, label: o }))
            })]
          }, s.key)) : jsx40("div", { style: { fontSize: "0.6rem", color: TEXT_DIM }, children: "no settings" })
        ]
      })
    ]
  });
  const centre = jsxs36("div", {
    style: { display: "flex", flexDirection: "column", minHeight: 0, borderRight: STROKE },
    children: [
      jsx40("div", { style: { padding: "0 0.6rem" }, children: jsx40(Label, { children: "conversation" }) }),
      jsx40("div", {
        ref: tapeRef,
        className: "llmops-scroll",
        onScroll: (e) => {
          const el = e.currentTarget;
          stuck.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        },
        style: { flex: 1, minHeight: 0, overflowY: "auto", padding: "0 0.6rem" },
        children: turns.map((t, i) => jsx40(Turn, {
          turn: t,
          index: i,
          editable: !sent && i < 2,
          folded: !!folded[i],
          onFold: () => setFolded({ ...folded, [i]: !folded[i] }),
          onEdit: (v) => setTurns(turns.map((x, j) => j === i ? { ...x, content: v } : x)),
          onCopy: () => copyText(t.content).then((ok) => setStatus({ kind: "idle", note: ok ? "copied" : "copy failed" })),
          onMove: () => {
            setUndoBody(null);
            setResult(moveToResult(t.content));
            setFromDraft(false);
          },
          md,
          raw: !!rawTurns[i],
          onRaw: (v) => setRawTurns({ ...rawTurns, [i]: v })
        }, i)).concat(live ? [jsx40(LiveTurn, { live }, "live")] : [])
      }),
      jsxs36("div", {
        style: { display: "flex", alignItems: "flex-end", gap: "0.4rem", padding: "0.45rem 0.6rem", borderTop: STROKE },
        children: [
          jsx40("div", { style: { flex: 1, minWidth: 0 }, children: jsx40(Grow, {
            value: input,
            onChange: setInput,
            maxRem: 10,
            placeholder: sent ? "next message\u2026 (Ctrl+Enter \u2014 Send)" : "optional: add to the preset turns\u2026 (Ctrl+Enter \u2014 Send)",
            onKeyDown: (e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                send();
              }
            }
          }) }),
          busy ? jsx40(Btn, { onClick: stop, strong: true, tone: "var(--ui-red)", title: "stop waiting and cut the model request", children: "\u25A0 Stop" }) : null,
          jsx40(Btn, { onClick: send, disabled: busy || !data, strong: true, tone: ACCENT, children: busy ? "\u2026" : "Send" })
        ]
      })
    ]
  });
  const right = jsxs36("div", {
    style: { display: "flex", flexDirection: "column", gap: "0.4rem", minHeight: 0, padding: "0 0.6rem 0.5rem" },
    children: [
      jsxs36("div", { style: { display: "flex", alignItems: "center", gap: "0.3rem", flexWrap: "wrap" }, children: [
        jsx40(Label, { children: result.cleared ? "result \u2014 only \u2192 fills it now" : "result \u2014 the reply drops in" }),
        jsx40("span", { style: { marginLeft: "auto" } }),
        // Clean-up by hand (owner, 2026-10-11): the model may echo our wrapping; the human decides.
        jsx40(Btn, {
          onClick: () => {
            setUndoBody(result.body);
            setResult({ ...result, body: unwrapMessages(result.body) });
          },
          disabled: unwrapMessages(result.body) === null,
          title: "take off a <messages> line at the start and a </messages> line at the end of the body",
          children: "strip <messages>"
        }),
        jsx40(Btn, {
          onClick: () => {
            setUndoBody(result.body);
            setResult({ ...result, body: stripHeaders(result.body, data) });
          },
          disabled: stripHeaders(result.body, data) === null,
          title: "take off lines like [m0033 assistant] \u2014 headers of the messages that were sent",
          children: "strip [m\u2026] headers"
        }),
        jsx40(Btn, {
          onClick: () => {
            setResult({ ...result, body: undoBody });
            setUndoBody(null);
          },
          disabled: undoBody === null,
          title: "put back the body as it was before the last strip",
          children: "\u21B6"
        }),
        jsx40(ModeSwitch, { raw: resultRaw || !result.body.trim(), onRaw: setResultRaw, md, editLabel: "edit the body" })
      ] }),
      frame ? jsx40(FramePiece, { text: frame.head, open: headOpen, onToggle: () => setHeadOpen(!headOpen) }, "frame-head") : null,
      // 📖 shows the body rendered; an empty body is always the field (there is nothing to read).
      md && !resultRaw && result.body.trim() ? jsx40(Md, { text: result.body, md, className: "llmops-scroll", style: {
        flex: 1,
        minHeight: 0,
        overflowY: "auto",
        padding: "0.45rem 0.55rem",
        borderRadius: "0.375rem",
        border: STROKE
      } }) : jsx40("div", { style: { flex: 1, minHeight: 0, display: "flex" }, children: jsx40("textarea", {
        value: result.body,
        onChange: (e) => setResult({ ...result, body: e.target.value }),
        placeholder: "body only \u2014 the wrap is shown around it",
        style: {
          flex: 1,
          resize: "none",
          fontSize: "0.72rem",
          lineHeight: 1.45,
          padding: "0.45rem 0.55rem",
          borderRadius: "0.375rem",
          border: STROKE,
          background: SURFACE,
          color: "inherit",
          ...SEL
        }
      }) }),
      frame && frame.tail ? jsx40(FramePiece, { text: frame.tail, note: frame.tailApart ? "separate message, after the bubbles" : "" }, "frame-tail") : null,
      jsxs36("div", { style: { display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.62rem", flexWrap: "wrap" }, children: [
        "wrap",
        jsx40(Select, {
          value: wrap,
          onChange: setWrap,
          title: "frame around the body",
          options: WRAPS.map((w) => ({ value: w, label: w === "as-it-was" ? `as it was (${WRAP_LABEL[asItWas(frames)]})` : WRAP_LABEL[w] }))
        }),
        jsx40("span", {
          style: { color: TEXT_DIM },
          title: "always the compaction path: one message in place of the span, bubbles float out",
          children: "insert: compaction path"
        })
      ] }),
      jsxs36("div", { style: { display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "0.62rem", color: TEXT_2 }, children: [
        jsx40("span", { children: `target ${data ? targetLabel(data) : "\u2014"}` }),
        jsx40("span", {
          style: { marginLeft: "auto", fontFamily: "monospace" },
          title: "target tokens \u2192 wrapped result tokens",
          children: `${kLabel(targetTok)} \u2192 ${kLabel(roughTokens2(wrapped))}`
        })
      ] }),
      jsxs36("div", { style: { display: "flex", alignItems: "center", gap: "0.4rem", justifyContent: "flex-end" }, children: [
        jsx40(Btn, { onClick: () => {
          setUndoBody(null);
          setResult(clearResult());
          setFromDraft(false);
        }, title: "clear \u2014 replies stop dropping in by themselves", children: "\xD7" }),
        jsx40(Btn, {
          onClick: () => copyText(wrapped).then((ok) => setStatus({ kind: "idle", note: ok ? "copied (wrapped)" : "copy failed" })),
          title: "copy the wrapped text",
          children: "\u29C9"
        }),
        jsx40(Btn, {
          onClick: replace,
          disabled: busy || !result.body.trim(),
          strong: true,
          tone: "var(--ui-red)",
          title: "replace the target span \u2014 runs at once, the field is the preview",
          children: "Replace"
        })
      ] })
    ]
  });
  return jsxs36("div", {
    ref: boxRef,
    // Clicks must not fall through to the panel's document-level «click outside» listeners.
    onClick: (e) => e.stopPropagation(),
    onMouseDown: (e) => e.stopPropagation(),
    style: {
      position: "fixed",
      zIndex: 900,
      // Spawns in the middle of the app window; once dragged — where it was left. No `transform`
      // for centring: it would make the chip popup's `fixed` count from this box, not the screen.
      top: pos ? `${pos.y}px` : `calc((100vh - ${WIN_H}) / 2)`,
      left: pos ? `${pos.x}px` : `calc((100vw - ${WIN_W}) / 2)`,
      width: size ? `${size.w}px` : WIN_W,
      height: size ? `${size.h}px` : WIN_H,
      // Native corner resize (bottom-right); the observer above remembers the result.
      resize: "both",
      minWidth: `${MIN_W}px`,
      minHeight: `${MIN_H2}px`,
      maxWidth: "100vw",
      maxHeight: "100vh",
      display: "flex",
      flexDirection: "column",
      overflow: "hidden",
      background: SURFACE,
      border: STROKE,
      borderRadius: "0.6rem",
      // All round, the TOP too (owner, 2026-10-11: the top edge merged with the chat text under it): a
      // thin dark ring, an upward glow, the old drop below.
      boxShadow: "0 0 0 1px rgba(0,0,0,0.25), 0 -10px 40px rgba(0,0,0,0.35), 0 20px 64px rgba(0,0,0,0.4)",
      color: "var(--ui-text-primary)"
    },
    children: [
      // A visible scrollbar on the long columns (owner, 2026-10-14: only the wheel scrolled them).
      jsx40("style", { children: `
        .llmops-scroll::-webkit-scrollbar { width: 13px; }
        .llmops-scroll::-webkit-scrollbar-track { background: transparent; }
        .llmops-scroll::-webkit-scrollbar-thumb {
          background-color: var(--ui-stroke-secondary);
          border-radius: 0; border-style: solid; border-color: transparent;
          border-width: 0 4px 0 1px; background-clip: padding-box;
        }
        .llmops-scroll::-webkit-scrollbar-thumb:hover { background-color: var(--ui-stroke-primary); }
      ` }),
      head,
      popRow ? jsx40(ChipPopup, {
        m: popRow,
        rect: chipPop.rect,
        onClose: () => setChipPop(null),
        md,
        onCopy: () => copyText(popRow.text).then((ok) => setStatus({ kind: "idle", note: ok ? `copied ${popRow.addr}` : "copy failed" }))
      }, "chip-pop") : null,
      jsxs36("div", {
        style: { flex: 1, minHeight: 0, display: "grid", gridTemplateColumns: "11rem minmax(0, 1fr) minmax(0, 1fr)" },
        children: [left, centre, right]
      })
    ]
  });
}

// desktop/src/llmops/llmops-host.js
function LLMOpsHost() {
  const win = useLLMOpsWin();
  const seq = win ? win.seq : 0;
  const transport = useMemo4(() => win ? llmopsTransport(win.sid, win.addrs) : null, [seq]);
  if (!win || !transport) return null;
  return jsx41(LLMOpsWindow, { transport, markdown, onClose: closeLLMOpsWindow, draft: win.draft || null }, seq);
}

// desktop/src/file-ops-pane.js
var USE_CARD_V2 = true;
var TAIL_SPACE2 = "40%";
function renderCard(brick, key, toolColumnWidth) {
  if (!USE_CARD_V2) return jsx42(Brick, { key, brick });
  const props = brickToCardProps(brick, toolColumnWidth);
  return jsx42("div", {
    key,
    className: "mb-1 animate-appear-once",
    style: CAPPED,
    children: jsx42(OperationCard, props)
  });
}
var DEV_MODES = ["operations", "palette"];
function effectiveMode(saved, dev) {
  const mode = String(saved || "mirror");
  return !dev && DEV_MODES.includes(mode) ? "mirror" : mode;
}
function FileOpsPaneImpl() {
  const view = useView();
  const bricks = MOCK_BRICKS;
  const scrollerRef = useRef12(null);
  const shownSessionRef = useRef12(null);
  const shownListRef = useRef12("");
  const [version, setVersion] = useState18("");
  const [dev, setDev] = useState18(false);
  useEffect10(() => {
    let dead = false;
    loadConfig().then((data) => {
      if (dead || !data || data.error) return;
      setVersion(String(data.version || ""));
      setDev(data.dev === true);
    }).catch(() => {
    });
    return () => {
      dead = true;
    };
  }, []);
  const [viewport, setViewport] = useState18({ top: 0, height: 600 });
  const bricksForWindowRef = useRef12([]);
  const winBoundsRef = useRef12({ start: 0, end: 0 });
  const scrollRaf = useRef12(null);
  const onScroll = () => {
    if (scrollRaf.current) return;
    scrollRaf.current = requestAnimationFrame(() => {
      scrollRaf.current = null;
      const el = scrollerRef.current;
      if (!el) return;
      const probe = renderWindow(bricksForWindowRef.current, el.scrollTop, el.clientHeight);
      if (probe.start === winBoundsRef.current.start && probe.end === winBoundsRef.current.end) return;
      setViewport({ top: el.scrollTop, height: el.clientHeight });
    });
  };
  useLayoutEffect7(() => {
    const el = scrollerRef.current;
    if (el) setViewport({ top: el.scrollTop, height: el.clientHeight });
  }, []);
  useLayoutEffect7(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const last = bricks[bricks.length - 1];
    const signature = `${bricks.length}:${last && (last.tool_id || last.ts) || ""}`;
    const listChanged = shownListRef.current !== signature;
    shownListRef.current = signature;
    const switched = shownSessionRef.current !== view.sessionId;
    shownSessionRef.current = view.sessionId;
    if (!switched && !listChanged) return;
    const fromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (switched || fromBottom <= el.clientHeight) el.scrollTop = el.scrollHeight;
    if (switched) {
      setViewport({ top: el.scrollTop, height: el.clientHeight });
    }
  });
  const mode = effectiveMode(view.mode, dev);
  const mirrorOn = mode === "mirror";
  const paletteOn = mode === "palette";
  const configOn = mode === "config";
  function modeBtn(label, key, on, muted) {
    return jsx42("button", {
      onClick: () => {
        $view.set(Object.assign({}, view, { mode: key }));
      },
      className: "px-2 py-0.5 rounded-full text-[0.55rem] font-medium cursor-pointer select-none transition-colors duration-150 ease-out hover:bg-(--ui-bg-tertiary) active:scale-95",
      // `nowrap` (владелец, 2026-09-29): без него при сужении панели «◉ Operations» ломалось
      // на две строки, и вся шапка становилась двухстрочной. Сжимается название слева.
      style: Object.assign(
        { border: "1px solid var(--ui-stroke-secondary)", userSelect: "none", whiteSpace: "nowrap" },
        muted ? { color: "var(--ui-text-quaternary)" } : {}
      ),
      children: label
    }, key);
  }
  function brandLabel() {
    return jsxs37("span", { className: "flex items-baseline gap-1 min-w-0", children: [
      jsx42("span", {
        className: "font-semibold text-[color:hsl(var(--primary))] truncate",
        title: "hermes-filetools plugin",
        children: "File Ops"
      }),
      jsx42("span", {
        className: "text-(--ui-text-quaternary) shrink-0",
        style: { fontSize: "0.55rem" },
        title: version ? "plugin version (both halves ship from one manifest)" : "version unknown \u2014 /config did not answer",
        children: version ? `v${version}` : "v\u2014"
      })
    ] }, "brand");
  }
  function modeBtns() {
    return jsxs37("div", { className: "flex items-center gap-1 shrink-0", children: [
      modeBtn((mirrorOn ? "\u25C9" : "\u25CB") + " Mirror", "mirror", mirrorOn),
      modeBtn((configOn ? "\u25C9" : "\u25CB") + " Config", "config", configOn),
      // Operations — за Config и приглушённым текстом (владелец, 2026-09-30). Только в dev.
      dev ? modeBtn((mode === "operations" ? "\u25C9" : "\u25CB") + " Operations", "operations", mode === "operations", true) : null,
      // Palette последней — инструмент разработки, временный (см. adapter.js: "плагин при запуске
      // должен показывать то, ради чего он существует", не палитру). Только в dev.
      dev ? modeBtn((paletteOn ? "\u25C9" : "\u25CB") + " Palette", "palette", paletteOn) : null
    ] }, "modes");
  }
  if (mirrorOn) {
    return jsxs37("div", {
      className: "flex flex-col h-full w-full overflow-hidden bg-background",
      style: CAPPED,
      children: [
        jsxs37("div", {
          className: "flex items-center justify-between px-2 py-1 border-b border-(--ui-stroke-quaternary) shrink-0 select-none",
          style: { fontSize: "0.6rem", userSelect: "none" },
          children: [
            brandLabel(),
            modeBtns()
          ]
        }),
        // Зеркало в своей ячейке `flex-1`: его корень `h-full`, и без обёртки он вытеснил бы
        // мини-панель токенов под край.
        jsx42("div", { className: "flex-1 min-h-0", children: jsx42(PromptMirror, {}) }),
        jsx42(TokenStrip, {})
      ]
    });
  }
  if (paletteOn) {
    return jsxs37("div", {
      className: "flex flex-col h-full w-full overflow-hidden bg-background",
      style: CAPPED,
      children: [
        jsxs37("div", {
          className: "flex items-center justify-between px-2 py-1 border-b border-(--ui-stroke-quaternary) shrink-0 select-none",
          style: { fontSize: "0.6rem", userSelect: "none" },
          children: [
            brandLabel(),
            modeBtns()
          ]
        }),
        jsx42(PalettePane, {})
      ]
    });
  }
  if (configOn) {
    return jsxs37("div", {
      className: "flex flex-col h-full w-full overflow-hidden bg-background",
      style: CAPPED,
      children: [
        jsxs37("div", {
          className: "flex items-center justify-between px-2 py-1 border-b border-(--ui-stroke-quaternary) shrink-0 select-none",
          style: { fontSize: "0.6rem", userSelect: "none" },
          children: [
            brandLabel(),
            modeBtns()
          ]
        }),
        jsx42(ConfigPane, {})
      ]
    });
  }
  const shown = visibleBricks(bricks, view.showErased);
  const toolCol = toolColWidth(shown.bricks.map((b) => b.tool));
  const win = renderWindow(shown.bricks, viewport.top, viewport.height);
  bricksForWindowRef.current = shown.bricks;
  winBoundsRef.current = { start: win.start, end: win.end };
  const rows = [];
  for (let i = win.start; i < win.end; i++) {
    const b = shown.bricks[i];
    if (b.note) {
      rows.push(jsx42("div", {
        key: `note-${b.tool_id || i}`,
        // Появляется вместе со своей карточкой — той же анимацией, иначе подпись выскакивала бы
        // мгновенно, а операция под ней всплывала следом.
        //
        // ПЕРЕНОС, а не truncate. `truncate` — это white-space:nowrap, то есть max-content ширина
        // подписи = вся строка целиком (до CAPTION_MAX_CHARS=180). Список лежит внутри SDK
        // ScrollArea, который сайзится ПО СОДЕРЖИМОМУ, — и эта одна длинная строка задавала ширину
        // всему списку. Карточки, честно занимая 100%, растягивались вместе с ней и уезжали под
        // правый срез панели. Ширину диктовать должна панель, а не текст модели.
        className: "px-1 pt-2 pb-1 text-(--ui-text-secondary) animate-appear-once",
        style: Object.assign({ fontSize: "0.7rem", whiteSpace: "normal", overflowWrap: "anywhere" }, CAPPED, SELECTABLE),
        title: b.note,
        children: b.note
      }));
    }
    rows.push(renderCard(b, b.tool_id || `${b.ts}-${i}`, toolCol));
  }
  const list = jsxs37("div", {
    className: "flex flex-col items-stretch p-2 gap-0",
    style: CAPPED,
    children: rows
  });
  return jsxs37("div", {
    className: "flex flex-col h-full w-full overflow-hidden bg-background",
    style: CAPPED,
    children: [
      jsxs37("div", {
        className: "flex items-center justify-between px-2 py-1 border-b border-(--ui-stroke-quaternary) shrink-0 select-none",
        style: { fontSize: "0.6rem", userSelect: "none" },
        children: [
          brandLabel(),
          jsxs37("div", { className: "flex items-center gap-1.5 shrink-0", children: [
            jsx42("span", { className: "text-(--ui-text-quaternary)", children: "mock \xB7 " + bricks.length + " ops" }),
            modeBtns()
          ] })
        ]
      }),
      // СВОЙ скроллер, а НЕ sdk.ScrollArea — и это не вкусовщина. SDK-шный ScrollArea (Radix)
      // оборачивает содержимое во внутренний контейнер с `display: table`, а таблица сайзится ПО
      // СОДЕРЖИМОМУ. Этот контейнер не наш: ни className, ни style до него не доходят, поэтому
      // цепочка width:100% обрывается там, сколько её ни прижимай снизу — карточки растягивались
      // под самую длинную строку подписи модели и уезжали под правый срез панели (проверено
      // ядерно-зелёным маркером: корень панели ширину имеет, разъезжается именно внутри).
      // Обычный div с overflowY:auto полностью под нашим контролем; overflowX:hidden — гарантия,
      // что горизонтали здесь не будет вообще: она живёт ВНУТРИ тела карточки (BodyPreview), где
      // ей и место.
      //
      // `contain:'paint'` (гейт GPU-нагрузки, 2026-09-14) — изолирует перерисовку списка карточек
      // от остального окна: без этого браузер перекомпоновывал наш неизменный список каждый раз,
      // когда где-то ЕЩЁ на экране идёт анимация (индикатор набора в чате), хотя у нас самих
      // ничего не менялось (подтверждено счётчиком рендеров — он стоял на месте). Именно поэтому
      // тултипы карточек (`operation-card.js::HoverTip`) больше НЕ рисуют fixed-блок локально —
      // contain делает этот div новым containing block для fixed-потомков, и локальный тултип
      // улетел бы в его координаты вместо окна. `GlobalHoverTip` ниже — СНАРУЖИ этого div нарочно.
      // Шире хит-зона скроллбара (владелец, 2026-09-15): у главного окна хоста есть своя полоса
      // ресайза у самого края — она нам не подчиняется, только у неё же под мышкой. Chromium
      // ловит клик/протаскивание в пределах ВСЕЙ рамки `::-webkit-scrollbar-thumb`, включая
      // `border` — прозрачная рамка тут даёт бегунку узкий вид, но широкую (влево от него) зону
      // попадания, не трогая при этом положение самого правого края контента.
      // `.animate-appear-once` — REAL rule, not borrowed (owner, 2026-09-15): this class is used
      // on every card (`renderCard` below) and the model's note caption, but its only CSS
      // definition anywhere in the app lived inside the Palette demo page's own local `<style>`
      // (`palette/loaders.js`, "#G1 Animation test") - not mounted during normal use, so cards
      // have been appearing with NO animation at all in real usage, silently, since the day this
      // className was added. Defined here instead, where it actually needs to work: `FileOpsPane`
      // is mounted for the plugin's whole lifetime, so this rule is always present. Same
      // keyframe/timing as the demo (`both` keeps the end state after the animation finishes;
      // plays once on mount, not `infinite` - re-triggers per card because each card's `key` is
      // its stable `tool_id`, so the DOM node the animation is attached to genuinely is a new
      // mount, not a re-render of an old one).
      jsx42("style", { children: `
        @keyframes appear-once {
          from { opacity: 0; transform: translateY(4px) scale(0.98); }
          to   { opacity: 1; transform: translateY(0) scale(1); }
        }
        .animate-appear-once {
          animation: appear-once 220ms ease-out both;
          will-change: opacity, transform;
        }
        .fops-scroll::-webkit-scrollbar { width: 14px; }
        .fops-scroll::-webkit-scrollbar-track { background: transparent; }
        .fops-scroll::-webkit-scrollbar-thumb {
          background-color: var(--ui-stroke-secondary);
          border-radius: 6px;
          border-style: solid;
          border-color: transparent;
          border-width: 6px 6px 6px 0px;
          background-clip: padding-box;
        }
        .fops-scroll::-webkit-scrollbar-thumb:hover { background-color: var(--ui-stroke-primary); }
      ` }),
      jsxs37("div", {
        ref: scrollerRef,
        className: "h-full w-full fops-scroll",
        // `overflowAnchor:'none'` (2026-09-14, найдено по трейсу) — гасит браузерный scroll
        // anchoring. Без него: высота распорок — ОЦЕНКА (ROW_PX), реальная высота карточки чуть
        // отличается, якорь сам подправляет scrollTop компенсируя разницу, это стреляет 'scroll',
        // наш onScroll пересчитывает окно, граница окна сдвигается на бричик — снова маленькое
        // расхождение оценка/реальность — снова якорь. Цикл длиной в кадр, сам не останавливается
        // (пойман в Performance-трейсе: RequestAnimationFrame из onScroll ровно раз в 33мс, без
        // единого разрыва, 3+ секунды подряд). У нас уже есть СВОЙ явный «прокрутка к хвосту»
        // (useLayoutEffect ниже) — эвристика браузера тут не нужна и только мешает.
        // A/B 2026-09-14 (владелец сняла contain на живых данных): рост нагрузки НЕ исчез без
        // contain — тот же порядок Layerize-стоимости что и с ним. Значит contain не виноват,
        // возвращён; настоящая причина — ре-рендер на каждый кадр скролла (см. onScroll выше).
        style: Object.assign({ overflowY: "auto", overflowX: "hidden", contain: "paint", overflowAnchor: "none" }, CAPPED),
        onScroll,
        children: [
          // Распорки окна рендера — ПУСТЫЕ div'ы нужной оценочной высоты, замещающие то, что не
          // смонтировано (см. renderWindow/core.js). Высота 0 — не рендерить вовсе, лишний узел
          // в DOM ради нуля ничего не стоит, но и не даёт ничего измерить.
          jsx42("div", { key: "top-spacer", style: { height: win.topSpacer, pointerEvents: "none" } }),
          list,
          jsx42("div", { key: "bottom-spacer", style: { height: win.bottomSpacer, pointerEvents: "none" } }),
          // Распорка — ПРЯМОЙ ребёнок скроллера, не padding у списка: процент высоты считается от
          // родителя с определённой высотой, а у списка она auto (по содержимому) и процент бы
          // просто схлопнулся в ноль. У скроллера высота определённая (h-full), поэтому здесь он
          // работает. flexShrink не нужен — блочная раскладка, не флекс.
          jsx42("div", { key: "tail-space", style: { height: TAIL_SPACE2, pointerEvents: "none" } })
        ]
      }),
      jsx42(GlobalHoverTip, {}),
      // Фильтры — ПОД скроллером, последней строкой панели: они про то, что показывать, а не
      // про то, что делать (действия живут в полосе сверху).
      jsx42(FilterBar, { showErased: !!view.showErased, hidden: shown.hidden })
    ]
  });
}
function FileOpsPaneRoot() {
  return jsxs37(Fragment, { children: [jsx42(FileOpsPaneImpl, {}, "pane"), jsx42(LLMOpsHost, {}, "llmops")] });
}
var FileOpsPane = memo(FileOpsPaneRoot);

// desktop/src/plugin.entry.js
import { jsx as jsx43 } from "react/jsx-runtime";
import * as sdk9 from "@hermes/plugin-sdk";
var plugin_entry_default = {
  id: "hermes-filetools",
  name: "File Tools \u2014 file operations panel",
  description: "Live stream of read_file / write_file / patch / search_files operations.",
  register(ctx) {
    const adapter2 = initAdapter(ctx);
    const a = adapter2.state && adapter2.state[STORED_SESSION_ATOM];
    if (a && typeof a.listen === "function") {
      const off = a.listen(() => refresh());
      adapter2.onDispose(typeof off === "function" ? off : () => {
      });
    }
    refresh();
    if (adapter2.hasEvents) {
      const off = sdk9.host.onEvent("tool.complete", handleToolComplete);
      adapter2.onDispose(typeof off === "function" ? off : () => {
      });
      const offEnd = sdk9.host.onEvent("message.complete", handleTurnEnd);
      adapter2.onDispose(typeof offEnd === "function" ? offEnd : () => {
      });
      const offStatus = sdk9.host.onEvent("status.update", handleStatusUpdate);
      adapter2.onDispose(typeof offStatus === "function" ? offStatus : () => {
      });
    }
    adapter2.onDispose(closeEventSocket);
    adapter2.onDispose(stopRevisionPoll);
    adapter2.onDispose(stopMirrorReload);
    if (typeof ctx.register !== "function") {
      console.warn("[hermes-filetools] ctx.register \u043D\u0435\u0434\u043E\u0441\u0442\u0443\u043F\u0435\u043D \u2014 \u043F\u0430\u043D\u0435\u043B\u044C \u043D\u0435 \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043E\u0432\u0430\u043D\u0430");
      return;
    }
    ctx.register({
      id: "pane",
      area: "panes",
      title: "File Ops",
      data: {
        placement: "main",
        dock: { pane: "workspace", pos: "right" },
        width: "480px",
        minWidth: "360px",
        collapsible: true,
        revealAliases: ["filetools"]
      },
      render: () => jsx43(FileOpsPane, {})
    });
    refresh();
  }
};
export {
  plugin_entry_default as default
};
