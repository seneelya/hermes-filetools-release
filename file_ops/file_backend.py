"""File backend adapter — routes our 4 core tools through Hermes' REAL file operations.

WHY THIS EXISTS (the path bug)
------------------------------
Built-in ``read_file`` resolves and executes via
``tools.file_tools._get_file_ops(task_id)`` -> ``ShellFileOperations``, whose
``_exec`` runs commands in the task's TERMINAL ENVIRONMENT (docker container,
host local, ...). That is why built-in read_file can open ``/project/foo.cs``
— inside the docker backend that path really exists.

Our plugin, however, used to run every op through ``mocks.py`` — plain local
file I/O on whatever OS the Hermes *process* lives on. On a Windows host that
means ``/project/foo.cs`` gets resolved host-side to ``T:\\project\\foo.cs``,
which does not exist. That is exactly the reported failure:

    view_file(/project/ProjectStarter/.../Edge.cs)
      -> {"error": "File not found: T:\\project\\ProjectStarter\\...\\Edge.cs"}

THE FIX
-------
In PLUGIN mode (running inside the Hermes process, ``tools.file_tools`` is
importable), every core function resolves paths with ``_resolve_path_for_task``
and executes through ``_get_file_ops(task_id)`` — i.e. it does EXACTLY what the
built-in tools do, so it sees the same filesystem view (the container).

In CONSOLE mode (standalone CLI on Windows/Linux without Hermes), that import
fails and we transparently fall back to the mock backend + host-side path
resolution, which is all a standalone console utility can sensibly offer.

This module never imports ``mocks`` itself — each core function passes in its
already-imported mock resolver/ops as fallbacks, so this file stays importable
in both modes without duplicating the package/bare dual-import dance.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace as NS
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class RealFileOpsAdapter:
    """Wraps a real ``FileOperations`` (e.g. ShellFileOperations) instance and
    presents the simple attribute-access interface our core functions expect.

    The real backend returns dataclasses (ReadResult / WriteResult / PatchResult /
    SearchResult). We normalize each into a plain namespace with exactly the
    attributes our code reads, so the core logic is identical for mock and real:

      read_file        -> .content .total_lines .file_size .truncated .error .base64_content .hint
      read_file_bytes  -> .error .base64_content .file_size
      write_file       -> .error .file_size
      patch_replace    -> .error .replacements .success
      search           -> .matches(list[dict]) .total_count .truncated .error .files .counts
    """

    def __init__(self, ops: Any):
        self._ops = ops

    # ── read_file ─────────────────────────────────────────────────────
    def read_file(self, path: str, offset: int = 1, limit: int = 2000) -> NS:
        r = self._ops.read_file(path, offset=offset, limit=limit)
        return NS(
            content=r.content or "",
            total_lines=getattr(r, "total_lines", 0),
            file_size=getattr(r, "file_size", 0),
            truncated=bool(getattr(r, "truncated", False)),
            error=getattr(r, "error", None),
            base64_content=getattr(r, "base64_content", None),
            hint=getattr(r, "hint", None),
        )

    # ── read_file_bytes (structured-document extraction) ──────────────
    def read_file_bytes(self, path: str, max_bytes: Optional[int] = None) -> NS:
        r = self._ops.read_file_bytes(path, max_bytes=max_bytes)
        return NS(
            error=getattr(r, "error", None),
            base64_content=getattr(r, "base64_content", None),
            file_size=getattr(r, "file_size", 0),
        )

    # ── write_file ────────────────────────────────────────────────────
    def write_file(self, path: str, content: str) -> NS:
        r = self._ops.write_file(path, content)
        return NS(
            error=getattr(r, "error", None),
            file_size=getattr(r, "bytes_written", 0),
        )

    # ── patch_replace ─────────────────────────────────────────────────
    def patch_replace(self, path: str, old_string: str, new_string: str,
                      replace_all: bool = False) -> NS:
        r = self._ops.patch_replace(path, old_string, new_string, replace_all=replace_all)
        err = getattr(r, "error", None)
        n = len(getattr(r, "files_modified", [])) or (0 if err else 1)
        # no_change/note: core returns success-WITHOUT-a-write when it detects a
        # re-sent, already-applied edit (file_operations.py::patch_replace's
        # is_already_applied branch). The "new" patch path must report that as
        # its own outcome, not as a successful patch - otherwise the model does
        # line arithmetic against a write that never happened
        # (Vision01__patch.md §8). The mock backend has no equivalent and simply
        # never sets these.
        return NS(error=err, replacements=n, success=bool(getattr(r, "success", False)),
                  no_change=bool(getattr(r, "no_change", False)),
                  note=getattr(r, "note", None))

    # ── add_line_numbers (doc-extraction path; both backends expose it) ──
    def add_line_numbers(self, text: str, start_offset: int = 1) -> str:
        fn = getattr(self._ops, "_add_line_numbers", None)
        if callable(fn):
            return fn(text, start_offset)
        # Fallback (shouldn't happen for the two known backends).
        lines = text.split("\n")
        return "\n".join(f"{start_offset + i}|{ln}" for i, ln in enumerate(lines))

    # ── search ────────────────────────────────────────────────────────
    def search(self, pattern: str, path: str = ".", target: str = "content",
               file_glob: Optional[str] = None, limit: int = 50, offset: int = 0,
               output_mode: str = "content", context: int = 0,
               ignore_case: bool = False) -> NS:
        # ignore_case: accepted for signature parity with the mock backend
        # (mocks.py::search) but NOT forwarded - core's own
        # ShellFileOperations.search() has no case-insensitivity kwarg at
        # all. Case-insensitivity against the real backend instead comes
        # from sift_files_new_core.py wrapping `pattern` itself in rg's
        # `(?i)` inline flag before it ever reaches here - this parameter
        # just keeps the call signature uniform across both backends.
        r = self._ops.search(
            pattern=pattern, path=path, target=target, file_glob=file_glob,
            limit=limit, offset=offset, output_mode=output_mode, context=context,
        )
        # target="files" (filename search, not content search) has no line-level
        # matches at all - the real backend puts its results in .files regardless
        # of output_mode. Must be checked BEFORE the output_mode branches below,
        # else the default output_mode="content" falls through to the .matches
        # read further down, which is always empty for a filename search (only
        # .total_count is set there): total_count comes out right, matches is
        # silently empty. Bug found via live-agent testing 2026-08-24 (sift_files
        # target=files returned matches:[] with a correct nonzero total_count).
        if target == "files":
            files = getattr(r, "files", None) or []
            return NS(
                matches=[{"path": f} for f in files],
                total_count=getattr(r, "total_count", len(files)),
                truncated=bool(getattr(r, "truncated", False)),
                error=getattr(r, "error", None),
            )
        # files_only / count modes (content search, alternate presentation)
        # carry their data in .files / .counts.
        if output_mode == "files_only" and getattr(r, "files", None):
            return NS(
                matches=[{"path": f} for f in r.files],
                total_count=len(r.files),
                truncated=bool(getattr(r, "truncated", False)),
                error=getattr(r, "error", None),
            )
        if output_mode == "count" and getattr(r, "counts", None):
            return NS(
                matches=dict(r.counts),
                total_count=sum(r.counts.values()),
                truncated=bool(getattr(r, "truncated", False)),
                error=getattr(r, "error", None),
            )

        # content mode: normalize SearchMatch dataclasses -> dicts our code reads.
        raw = getattr(r, "matches", []) or []
        if raw and not isinstance(raw[0], dict):
            matches = [{"file": m.path, "line_number": m.line_number, "content": m.content}
                       for m in raw]
        else:
            matches = list(raw)
        return NS(
            matches=matches,
            total_count=getattr(r, "total_count", len(matches)),
            truncated=bool(getattr(r, "truncated", False)),
            error=getattr(r, "error", None),
            files=getattr(r, "files", []),
            counts=getattr(r, "counts", {}),
        )


class FileBackend:
    """Decides per-call which backend + path-resolution to use.

    * ``is_real`` True  -> Hermes is importable and a live file-ops exists for the
      task; we resolve with ``_resolve_path_for_task`` and execute in that env.
    * ``is_real`` False -> console/mock mode (or explicit base_dir); host-side
      resolution + mock ops, exactly as before this fix.

    The mock resolver/ops are injected by each core function (already imported
    there with the package/bare dual-import), so this class has no dependency on
    ``mocks`` and imports cleanly in both modes.
    """

    def __init__(self, task_id: str = "default", *,
                 mock_ops: Optional[Any] = None,
                 mock_resolve: Optional[Callable[..., Any]] = None):
        self.task_id = task_id or "default"
        self._mock_ops = mock_ops
        self._mock_resolve = mock_resolve
        self._real: Optional[RealFileOpsAdapter] = None
        try:
            from tools.file_tools import _get_file_ops as _gfo  # type: ignore[import]
            ops = _gfo(self.task_id)
            if ops is not None:
                self._real = RealFileOpsAdapter(ops)
        except Exception as exc:  # console mode / no Hermes -> mock fallback
            logger.debug("file_backend: real backend unavailable (%s); using mock", exc)
            self._real = None

    @property
    def is_real(self) -> bool:
        return self._real is not None

    def resolve(self, path: str, base_dir: Optional[str] = None) -> str:
        """Resolve *path* to an absolute string for the active backend."""
        if self.is_real and base_dir is None:
            # Same resolution as built-in tools: anchor relative paths on the task's
            # real working dir; pass container-absolute paths through untouched.
            from tools.file_tools import _resolve_path_for_task  # type: ignore[import]
            return str(_resolve_path_for_task(path, self.task_id))
        if self._mock_resolve is None:
            raise RuntimeError("file_backend: no mock resolver available")
        return str(self._mock_resolve(path, base_dir))

    @property
    def ops(self) -> Any:
        if self.is_real:
            return self._real
        if self._mock_ops is None:
            raise RuntimeError("file_backend: no mock ops available")
        return self._mock_ops

    def stat_mtime(self, resolved_path: str) -> Optional[float]:
        """Best-effort last-modified time (epoch seconds) of *resolved_path*.

        Mock/console mode: direct ``os.path.getmtime`` on the resolved host path
        (the same filesystem the mock backend itself just wrote/read).

        Real (plugin) mode: NEVER falls back to a host-side ``os.stat()`` - that
        is exactly the path bug this module exists to avoid (see module
        docstring): a real-mode path is only guaranteed to exist inside the
        task's own environment (docker/host), not on the plugin process's own
        filesystem. Instead this shells a ``stat`` probe through the same
        ``_exec`` channel every other real op already uses (confirmed live
        against ``tools.file_tools.ShellFileOperations`` 2026-08-25: it has no
        public stat/mtime method, but does expose ``_exec``/``_escape_shell_arg``
        - reusing them here is the same kind of private-method reach the
        plugin already does for ``_add_line_numbers`` below). GNU stat
        (``-c %Y``, Linux/Git-Bash-on-Windows - this backend's commands always
        run through a POSIX shell, see ``_size_probe_cmd``'s own docstring) is
        tried first, BSD/macOS stat (``-f %m``) second. Returns None - a
        missing mtime anchor, never a wrong one - if the probe methods aren't
        there, the path doesn't exist, or anything about the exec fails.
        """
        if self.is_real:
            ops = self._real._ops
            exec_fn = getattr(ops, "_exec", None)
            escape_fn = getattr(ops, "_escape_shell_arg", None)
            if not callable(exec_fn) or not callable(escape_fn):
                return None
            arg = escape_fn(resolved_path)
            cmd = (f"if [ -e {arg} ]; then stat -c %Y {arg} 2>/dev/null "
                  f"|| stat -f %m {arg} 2>/dev/null; else exit 1; fi")
            try:
                result = exec_fn(cmd)
            except Exception:
                return None
            if getattr(result, "exit_code", 1) != 0:
                return None
            try:
                return float(result.stdout.strip())
            except (ValueError, AttributeError, TypeError):
                return None
        try:
            import os
            return os.path.getmtime(resolved_path)
        except OSError:
            return None


def add_line_numbers(text: str, start_offset: int = 1) -> str:
    """Format text with a compact ``LINE_NUM|CONTENT`` gutter (pure helper).

    Used by the structured-document extraction path where we page the decoded
    text ourselves and need line numbers that match the normal read format.
    """
    lines = text.split("\n")
    return "\n".join(f"{start_offset + i}|{ln}" for i, ln in enumerate(lines))


def get_real_file_ops(task_id: str = "default") -> Optional[RealFileOpsAdapter]:
    """Convenience: adapter for the real backend, or None when not in Hermes."""
    return FileBackend(task_id)._real
