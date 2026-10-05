"""Mock module - replaces all Hermes infrastructure dependencies.

These mocks make the file tools standalone CLI utilities that don't need
Hermes registry, terminal_tool, task_id management, or security layers."""

import json
import os
from pathlib import Path


# ── Registry + error helpers ───────────────────────────────────────

def tool_error(message: str, **kwargs) -> str:
    """Return a standardized error JSON response (mock of Hermes registry.tool_error)."""
    result = {"error": message}
    for k, v in kwargs.items():
        if v is not None and v != "":
            result[k] = v
    return json.dumps(result, ensure_ascii=False)


# ── Path resolution (mock of _resolve_path_for_task) ───────────────

def resolve_path_for_tool(path: str, base_dir: str = None) -> Path:
    """Resolve a file path relative to base_dir or current working directory.
    
    Mock replacement for Hermes' _resolve_path_for_task which uses task_id,
    session cwd, and terminal environment context."""
    if base_dir is None:
        base_dir = os.getcwd()
    
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = (Path(base_dir) / p).resolve()
    else:
        p = p.resolve()
    return p


# ── File operations mock ───────────────────────────────────────────

class MockFileOpsResult:
    """Mock of Hermes ReadResult/WriteResult/PatchResult."""
    
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
    
    def to_dict(self, densify=False):
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


class MockFileOperations:
    """Mock of Hermes ShellFileOperations - uses direct Python file I/O instead of shell subprocesses."""
    
    def read_file(self, path: str, offset: int = 1, limit: int = 2000) -> MockFileOpsResult:
        """Read a text file with pagination and line numbers."""
        try:
            resolved = resolve_path_for_tool(path)
            if not resolved.exists():
                return self._suggest_similar_files(resolved, path)
            
            content = resolved.read_text(encoding='utf-8', errors='replace')
            lines = content.splitlines()
            total_lines = len(lines)
            
            end_line = min(offset + limit - 1, total_lines)
            page_lines = lines[offset - 1:end_line] if offset <= total_lines else []
            page_text = "\n".join(page_lines)
            
            formatted = self._add_line_numbers(page_text, offset) if page_text else ""
            truncated = (offset + limit - 1) > total_lines
            
            return MockFileOpsResult(
                content=formatted,
                error=None,
                total_lines=total_lines,
                file_size=os.path.getsize(str(resolved)),
                truncated=truncated,
                resolved_path=str(resolved),
            )
        except PermissionError as e:
            return MockFileOpsResult(content="", error=f"Permission denied: {path}")
        except Exception as e:
            return MockFileOpsResult(content="", error=str(e))
    
    def write_file(self, path: str, content: str) -> MockFileOpsResult:
        """Write content to a file (creates parent dirs)."""
        try:
            resolved = resolve_path_for_tool(path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            # newline='' is load-bearing on Windows: without it, Path.write_text
            # silently translates every '\n' in `content` to '\r\n' on write
            # (universal-newline mode), corrupting create_file_new_core.py's
            # own CRLF-preservation logic for insert_line (it already puts the
            # exact bytes it wants in `content` - this must write them as-is).
            resolved.write_text(content, encoding='utf-8', newline='')
            
            return MockFileOpsResult(
                success=True,
                error=None,
                file_size=os.path.getsize(str(resolved)),
                resolved_path=str(resolved),
            )
        except PermissionError as e:
            return MockFileOpsResult(success=False, error=f"Permission denied: {path}")
        except Exception as e:
            return MockFileOpsResult(success=False, error=str(e))
    
    def write_file_bytes(self, path: str, data: bytes) -> MockFileOpsResult:
        """Write binary content to a file."""
        try:
            resolved = resolve_path_for_tool(path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_bytes(data)
            return MockFileOpsResult(
                success=True, error=None,
                file_size=os.path.getsize(str(resolved)),
                resolved_path=str(resolved),
            )
        except Exception as e:
            return MockFileOpsResult(success=False, error=str(e))
    
    def read_file_bytes(self, path: str, max_bytes: int = None) -> MockFileOpsResult:
        """Read file bytes (for document extraction)."""
        import base64
        try:
            resolved = resolve_path_for_tool(path)
            data = resolved.read_bytes()
            if max_bytes and len(data) > max_bytes:
                return MockFileOpsResult(
                    error=f"File too large ({len(data)} bytes, limit {max_bytes})",
                    base64_content=None, file_size=len(data),
                )
            
            b64 = base64.b64encode(data).decode('ascii')
            return MockFileOpsResult(
                error=None,
                base64_content=b64,
                file_size=len(data),
            )
        except Exception as e:
            return MockFileOpsResult(error=str(e), base64_content=None)
    
    def patch_replace(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> MockFileOpsResult:
        """Replace text in a file (exact match by default)."""
        try:
            resolved = resolve_path_for_tool(path)
            if not resolved.exists():
                return MockFileOpsResult(error=f"File not found: {path}")
            
            # newline='' — the other half of the CRLF fix below. Without it
            # Python's universal-newline translation turns the file's CRLFs
            # into "\n" on READ, so an old_string that legitimately contains
            # "\r\n" (anything cut verbatim out of a CRLF file, e.g. by
            # modify_file_new_core) can never be found and EVERY patch of a
            # CRLF file failed in console/mock mode. The real backend reads
            # through `cat`, raw, and has no such problem - so this was a
            # mock-only divergence that made the mock STRICTER than reality.
            # Via open(), not read_text(): the latter only accepts `newline`
            # from Python 3.13, and this runs on 3.10 (write_text got it in
            # 3.10, which is why the write side below can use it directly).
            with resolved.open(encoding='utf-8', errors='replace', newline='') as fh:
                content = fh.read()

            count = content.count(old_string)
            if count == 0 and replace_all:
                # Try fuzzy match suggestion (simple version)
                return MockFileOpsResult(error=f"Could not find exact text to replace in {path}")
            
            if replace_all:
                new_content = content.replace(old_string, new_string)
            else:
                new_content = content.replace(old_string, new_string, 1)
            
            if new_content == content:
                return MockFileOpsResult(error=f"Could not find exact text to replace in {path}")

            # Mirror core's own behaviour (file_operations.py::patch_replace ->
            # _normalize_line_endings): after the substitution it normalizes the
            # WHOLE file to the line ending the file already used. Models send
            # new_string with bare LF, so without this a patch of a CRLF file
            # leaves lone LFs behind on exactly the lines it touched - mixed
            # endings that the real backend would never produce, i.e. another
            # mock-vs-real divergence in console testing.
            if "\r\n" in content:
                new_content = new_content.replace("\r\n", "\n").replace("\n", "\r\n")

            # newline='' — without it Python's text mode translates every \n to
            # the platform ending on Windows, so patching an LF file through the
            # console/mock backend silently rewrote the WHOLE file to CRLF.
            # Console-only bug (the real backend shells out), but it corrupted
            # our own test fixtures.
            resolved.write_text(new_content, encoding='utf-8', newline='')

            # Generate unified diff header (simplified)
            old_lines = content.splitlines(keepends=True)
            new_lines = new_content.splitlines(keepends=True)
            
            return MockFileOpsResult(
                success=True, error=None,
                resolved_path=str(resolved),
                files_modified=[str(resolved)],
                replacements=count if replace_all else 1,
            )
        except Exception as e:
            return MockFileOpsResult(error=str(e))
    
    def patch_v4a(self, patch_content: str) -> MockFileOpsResult:
        """Apply a V4A multi-file patch (simplified parser)."""
        import re
        
        # Parse V4A format: *** Update File: path | *** Add File: path | etc.
        # This is a simplified version - full implementation would parse unified diffs too
        files_modified = []
        errors = []
        
        # Find all file operations in the patch
        pattern = r'^\*\*\*\s*(Update|Add)\s+File:\s*(.+?)$'
        matches = list(re.finditer(pattern, patch_content, re.MULTILINE))
        
        if not matches:
            return MockFileOpsResult(error="No valid V4A file operations found in patch")
        
        for i, match in enumerate(matches):
            op = match.group(1)
            filepath = match.group(2).strip()
            
            # Find the diff content between this header and the next (or end)
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(patch_content)
            block = patch_content[start:end]
            
            try:
                resolved = resolve_path_for_tool(filepath)
                
                if op == "Add":
                    # For Add, the content after header is the file content (simplified)
                    new_content = block.strip()
                    resolved.parent.mkdir(parents=True, exist_ok=True)
                    resolved.write_text(new_content, encoding='utf-8')
                    files_modified.append(str(resolved))
                    
                elif op == "Update":
                    if not resolved.exists():
                        errors.append(f"File not found: {filepath}")
                        continue
                    
                    # Parse unified diff or simple replacement (simplified)
                    content = resolved.read_text(encoding='utf-8', errors='replace')
                    new_content = self._apply_diff_block(content, block)
                    
                    if new_content is None:
                        errors.append(f"Could not apply patch to {filepath}")
                    else:
                        resolved.write_text(new_content, encoding='utf-8')
                        files_modified.append(str(resolved))
                        
            except Exception as e:
                errors.append(f"Error processing {filepath}: {e}")
        
        if errors and not files_modified:
            return MockFileOpsResult(error="; ".join(errors))
        
        result = MockFileOpsResult(
            success=True, error=None,
            files_modified=files_modified,
        )
        if errors:
            result.warnings = errors
        return result
    
    def search(self, pattern: str, path: str = ".", target: str = "content",
               file_glob: str = None, limit: int = 50, offset: int = 0,
               output_mode: str = "content", context: int = 0,
               ignore_case: bool = False) -> MockFileOpsResult:
        """Search files using grep for content, pure Python for file names.

        File-name search used to shell out to `find`, which collided with
        Windows' own find.exe on PATH and got its glob argument re-expanded
        by the MSYS runtime before main() ever saw it (two independent,
        Windows-only failure modes) — replaced with Path.rglob + fnmatch,
        which is deterministic on every OS and drops an external dependency.

        `ignore_case` (search_files "new" path, Vision01__search.md §6) - only
        honored HERE (mock/console backend adds `-i` to the grep command
        below). The real backend gets case-insensitivity a different way -
        sift_files_new_core.py wraps `pattern` in rg's own `(?i)` inline
        flag before calling it - so this kwarg is meaningless there, see
        file_backend.py::RealFileOpsAdapter.search()."""
        import subprocess

        try:
            search_path = resolve_path_for_tool(path)
            if not search_path.exists():
                return MockFileOpsResult(error=f"Path not found: {path}")

            if target == "files":
                return self._search_files_by_name(search_path, pattern, offset, limit)

            # Content search with grep
            cmd = ["grep", "-r", "-n"]
            if file_glob:
                cmd.append("--include=" + file_glob)
            cmd.append("-E")  # Extended regex
            if ignore_case:
                cmd.append("-i")

            if output_mode == "files_only":
                cmd.append("-l")
            elif output_mode == "count":
                cmd.append("-c")

            if context > 0:
                cmd.extend(["-C", str(context)])

            cmd.extend([pattern, str(search_path)])

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            except FileNotFoundError:
                return MockFileOpsResult(
                    error="grep not found on PATH. On Windows install Git for Windows "
                          "(ships grep in usr/bin) or run this from a shell that has it."
                )
            output = proc.stdout.strip()

            if not output and proc.returncode != 0:
                return MockFileOpsResult(matches=[], total_count=0, error=None)

            prefix = str(search_path)

            # -l / -c outputs have a DIFFERENT shape than -n (no line numbers),
            # so they can't go through _parse_grep_output — handle them here.
            if output_mode == "files_only":
                files, seen = [], set()
                for line in output.split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    rel = line[len(prefix):].lstrip('/\\') if line.startswith(prefix) else line
                    if rel not in seen:
                        seen.add(rel)
                        files.append(rel)
                total_count = len(files)
                truncated = total_count > offset + limit
                return MockFileOpsResult(
                    matches=[{"path": f} for f in files[offset:offset + limit]],
                    total_count=total_count,
                    error=None,
                    truncated=truncated,
                )

            if output_mode == "count":
                counts = {}
                for line in output.split('\n'):
                    line = line.strip()
                    if not line or ':' not in line:
                        continue
                    fpart, cpart = line.rsplit(':', 1)
                    try:
                        n = int(cpart)
                    except ValueError:
                        continue
                    if n == 0:  # grep -c prints zeros for non-matching files
                        continue
                    rel = fpart[len(prefix):].lstrip('/\\') if fpart.startswith(prefix) else fpart
                    counts[rel] = n
                total_count = sum(counts.values())
                truncated = len(counts) > offset + limit
                result_counts = dict(list(counts.items())[offset:offset + limit])
                return MockFileOpsResult(
                    matches=result_counts,
                    total_count=total_count,
                    error=None,
                    truncated=truncated,
                )

            # Parse grep -n output into structured matches (default content mode)
            matches = self._parse_grep_output(output, search_path)
            total_count = len(matches)
            truncated = total_count > offset + limit

            # Default content mode with line numbers
            result_matches = matches[offset:offset + limit]
            
            return MockFileOpsResult(
                matches=[{
                    "file": m["file"],
                    "line_number": m["line_number"],
                    "content": m["content"],
                } for m in result_matches],
                total_count=total_count,
                error=None,
                truncated=truncated,
            )
            
        except subprocess.TimeoutExpired:
            return MockFileOpsResult(error="Search timed out after 60 seconds")
        except Exception as e:
            return MockFileOpsResult(error=str(e))
    
    def _parse_grep_output(self, output: str, base_path: Path) -> list:
        """Parse grep -n output into structured matches.

        Can't blindly split on ':' — a Windows absolute path already has one
        (the drive letter, `T:\\...`), which used to swallow the real
        filepath:line separator and silently drop every match. Fixed by
        stripping the exact search-root prefix we gave grep before splitting
        the remainder, so only grep's own separators are left."""
        matches = []
        prefix = str(base_path)
        for line in output.split('\n'):
            if not line:
                continue

            if line.startswith(prefix):
                rest = line[len(prefix):].lstrip('/\\')
            else:
                rest = line

            parts = rest.split(':', 2)
            if len(parts) >= 3:
                file_rel, line_num_str, content = parts[0], parts[1], parts[2]

                try:
                    line_num = int(line_num_str)
                except ValueError:
                    continue

                if not file_rel:
                    file_rel = base_path.name

                matches.append({
                    "file": file_rel,
                    "line_number": line_num,
                    "content": content.rstrip('\n'),
                })
        return matches

    def _search_files_by_name(self, search_path: Path, pattern: str, offset: int, limit: int) -> "MockFileOpsResult":
        """Find files by name glob pattern (pure Python, no `find` dependency)."""
        import fnmatch

        if search_path.is_file():
            candidates = [search_path] if fnmatch.fnmatch(search_path.name, pattern) else []
        else:
            candidates = [f for f in search_path.rglob('*') if f.is_file() and fnmatch.fnmatch(f.name, pattern)]

        files = [str(f) for f in sorted(candidates)]
        total_count = len(files)
        result_files = files[offset:offset + limit]
        truncated = total_count > offset + limit

        return MockFileOpsResult(
            matches=[{"path": f} for f in result_files],
            total_count=total_count,
            error=None,
            truncated=truncated,
        )
    
    def _apply_diff_block(self, original_content: str, diff_block: str) -> str | None:
        """Apply a simplified diff block to content."""
        # For now: if the diff contains @@ lines (unified diff), try basic apply
        # Otherwise treat as replacement content for "Add" operations
        
        import difflib
        
        old_lines = original_content.splitlines(keepends=True)
        
        # Try to parse unified diff hunks from block
        new_lines = []
        i = 0
        lines = diff_block.splitlines(keepends=True)
        
        while i < len(lines):
            line = lines[i]
            
            if line.startswith('@@'):
                # Parse hunk header: @@ -start,count +start,count @@ context
                import re
                m = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
                if not m:
                    i += 1
                    continue
                
                old_start = int(m.group(1)) - 1  # 0-indexed
                old_count = int(m.group(2)) if m.group(2) else 1
                new_start = int(m.group(3)) - 1
                new_count = int(m.group(4)) if m.group(4) else 1
                
                i += 1  # Skip header
                
                hunk_lines = []
                while i < len(lines) and not lines[i].startswith('@@'):
                    hunk_lines.append(lines[i])
                    i += 1
                
                # Apply hunk
                if old_start >= len(old_lines):
                    return None  # Hunk doesn't apply
                
                context_before = new_start - len([l for l in hunk_lines[:new_count] if not l.startswith('+') and not l.startswith('-')])
                
                # Simple unified diff application
                result = []
                old_pos = old_start
                for hl in hunk_lines:
                    if hl.startswith(' '):
                        # Context line
                        if old_pos < len(old_lines) and old_lines[old_pos] == hl:
                            result.append(hl)
                            old_pos += 1
                        else:
                            return None  # Context mismatch
                    elif hl.startswith('+'):
                        result.append(hl[1:])
                    elif hl.startswith('-'):
                        old_pos += 1
                
                new_lines.extend(result)
                
            else:
                i += 1
        
        if not new_lines and diff_block.strip():
            # Fallback: treat entire block as replacement content (for simple patches)
            return diff_block.strip() + '\n'
        
        return ''.join(new_lines) if new_lines else None
    
    def _suggest_similar_files(self, resolved: Path, original_path: str) -> MockFileOpsResult:
        """Suggest similar files when the requested path doesn't exist."""
        parent = resolved.parent
        name = resolved.name
        
        suggestions = []
        try:
            if parent.exists() and parent.is_dir():
                for f in parent.iterdir():
                    if name.lower() in f.name.lower():
                        suggestions.append(str(f.relative_to(parent)))
                        if len(suggestions) >= 5:
                            break
        except (PermissionError, OSError):
            pass
        
        error_msg = f"File not found: {original_path}"
        if suggestions:
            error_msg += f"\nSimilar files in {parent}: {', '.join(suggestions)}"
        
        return MockFileOpsResult(content="", error=error_msg)
    
    def _add_line_numbers(self, text: str, start_offset: int = 1) -> str:
        """Add line numbers to text content."""
        lines = text.split('\n')
        numbered_lines = []
        
        for i, line in enumerate(lines):
            line_num = start_offset + i
            padded = f"{line_num:>6}"
            numbered_lines.append(f"{padded}|{line}")
        
        return '\n'.join(numbered_lines)


# ── File state mock (cross-agent tracking stubs) ───────────────────

class MockFileState:
    """Mock of Hermes file_state - no-op operations for standalone use."""
    
    def record_read(self, task_id: str, path: str, partial=False):
        pass
    
    def note_write(self, task_id: str, path: str):
        pass
    
    def check_stale(self, task_id: str, path: str) -> None:
        return None
    
    def lock_path(self, path: str):
        """Return a no-op context manager."""
        class NoOpLock:
            def __enter__(self): return self
            def __exit__(self, *args): pass
        return NoOpLock()


file_state = MockFileState()


# ── Security layer mocks (disabled for standalone use) ─────────────

def has_binary_extension(path: str) -> bool:
    """Check if file has a known binary extension."""
    binary_exts = {
        '.exe', '.dll', '.so', '.dylib', '.bin', '.o', '.obj', '.a', '.lib',
        '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.tiff', '.webp',
        '.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a',
        '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv',
        '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
        '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar',
        '.pyc', '.pyo', '.class', '.jar', '.war', '.ear',
    }
    
    ext = Path(path).suffix.lower()
    return ext in binary_exts


def get_read_block_error(path: str) -> None:
    """Return error message if path should be blocked from reading.
    
    Mock version: disabled for standalone use (returns None always)."""
    return None


def redact_sensitive_text(text: str, file_read=False) -> str:
    """Redact sensitive patterns from text.
    
    Mock version: returns text unchanged."""
    return text


# ── Pagination helpers ─────────────────────────────────────────────

def normalize_read_pagination(offset: int, limit: int):
    """Normalize read pagination parameters."""
    if offset < 1:
        offset = 1
    if limit < 1:
        limit = 1
    if limit > 2000:
        limit = 2000
    return offset, limit


def normalize_search_pagination(offset: int, limit: int):
    """Normalize search pagination parameters."""
    if offset < 0:
        offset = 0
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500
    return offset, limit


# ── Character budget helpers ───────────────────────────────────────

_MAX_READ_CHARS = 15000

def get_max_read_chars() -> int:
    """Get maximum characters for a read operation."""
    return _MAX_READ_CHARS


def truncate_to_char_budget(content: str, max_chars: int) -> tuple[str, int, bool]:
    """Truncate content to fit within character budget at line boundary.
    
    Returns (trimmed_content, lines_kept, first_line_exceeded)."""
    if len(content) <= max_chars:
        return content, content.count('\n') + 1, False
    
    lines = content.split('\n', max_chars // 20)  # Estimate to avoid huge split
    
    result_lines = []
    current_len = 0
    first_line_exceeded = len(lines[0]) >= max_chars if lines else False
    
    for line in lines:
        added = len(line) + 1  # +1 for newline
        if current_len + added > max_chars and not first_line_exceeded:
            break
        result_lines.append(line)
        current_len += added
    
    trimmed = '\n'.join(result_lines)
    
    if first_line_exceeded:
        # Clamp the first line itself
        trimmed = lines[0][:max_chars]
    
    return trimmed, len(result_lines), first_line_exceeded


# ── Document extraction (structured docs) ──────────────────────────

def _doc_extract_module():
    try:  # plugin mode
        from . import _doc_extract
    except ImportError:  # console mode
        import _doc_extract  # type: ignore
    return _doc_extract


def is_extractable_document(path: str) -> bool:
    """Check if file can be extracted to text (the set lives in _doc_extract)."""
    ext = Path(path).suffix.lower()
    return ext in _doc_extract_module().EXTRACTABLE_EXTENSIONS


def extract_document_bytes(data: bytes, path: str) -> str | None:
    """Extract text from document bytes - delegates to _doc_extract (kept
    separate so openpyxl/anydoc are only imported when a
    matching extension is actually read, not on every mocks.py import).

    Raises _doc_extract.ExtractionError with a model-facing message when a
    document cannot be converted; the callers turn it into an ERROR line."""
    return _doc_extract_module().extract(data, path)


# ── Shared instance ────────────────────────────────────────────────

file_ops = MockFileOperations()
