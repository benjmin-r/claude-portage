#!/usr/bin/env python3
"""claude-portage: Portable Claude Code workspace archives.

Bundles a project + its Claude Code metadata (~/.claude/) into a portable
archive that can be unpacked anywhere with automatic path rewriting.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

__version__ = "0.2.4"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class PortageConfig:
    """Shared configuration for pack/unpack/rename operations."""

    project_path: Path
    claude_dir: Path
    verbose: bool = False

    @property
    def encoded_path(self) -> str:
        return encode_path(self.project_path)

    @property
    def project_meta_dir(self) -> Path:
        return self.claude_dir / "projects" / self.encoded_path


@dataclass
class PackConfig(PortageConfig):
    """Configuration for the pack command."""

    output: Optional[Path] = None
    include_project_files: bool = True
    include_debug: bool = False


@dataclass
class UnpackConfig:
    """Configuration for the unpack command."""

    archive_path: Path
    target_dir: Path
    claude_dir: Path
    verbose: bool = False


@dataclass
class RenameConfig:
    """Configuration for the rename command."""

    old_path: Path
    new_path: Path
    claude_dir: Path
    verbose: bool = False


# ---------------------------------------------------------------------------
# Path encoding / decoding
# ---------------------------------------------------------------------------

def encode_path(path: Path | str) -> str:
    """Encode an absolute path into Claude's directory-name scheme.

    Claude Code replaces each ``/`` and ``.`` with ``-`` in the resolved
    absolute path.
    e.g. ``/Users/alice.name/src/foo`` → ``-Users-alice-name-src-foo``
    """
    resolved = os.path.realpath(os.path.expanduser(str(path)))
    return resolved.replace(os.sep, "-").replace(".", "-")


def default_claude_dir() -> Path:
    """Return the default ``~/.claude`` directory."""
    return Path.home() / ".claude"


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def discover_session_ids(project_meta_dir: Path) -> List[str]:
    """Discover session IDs from ``<uuid>.jsonl`` files in the project dir."""
    if not project_meta_dir.is_dir():
        return []
    return sorted(
        item.stem
        for item in project_meta_dir.iterdir()
        if item.suffix == ".jsonl" and item.stem != "sessions-index"
    )


def _collect_files_recursively(directory: Path) -> List[Path]:
    """Collect all files under a directory tree."""
    if not directory.is_dir():
        return []
    return [
        Path(root) / fn
        for root, _, filenames in os.walk(directory)
        for fn in filenames
    ]


def _discover_plan_slugs(project_meta_dir: Path, session_ids: List[str]) -> Set[str]:
    """Extract plan file slugs referenced in session JSONL files."""
    slugs: Set[str] = set()
    for sid in session_ids:
        jsonl_file = project_meta_dir / f"{sid}.jsonl"
        if not jsonl_file.is_file():
            continue
        try:
            text = jsonl_file.read_text(encoding="utf-8", errors="replace")
            slugs.update(m.group(1) for m in re.finditer(r'plans/([a-zA-Z0-9_-]+\.md)', text))
        except OSError:
            pass
    return slugs


def discover_session_files(
    claude_dir: Path,
    project_meta_dir: Path,
    session_ids: List[str],
    include_debug: bool = False,
) -> Dict[str, List[Path]]:
    """Discover all files related to sessions.

    Returns a dict mapping category names to lists of absolute paths.
    """
    files: Dict[str, List[Path]] = {
        "project-meta": _collect_files_recursively(project_meta_dir),
        "file-history": [],
        "session-env": [],
        "todos": [],
        "plans": [],
        "debug": [],
    }

    for sid in session_ids:
        files["file-history"].extend(_collect_files_recursively(claude_dir / "file-history" / sid))
        files["session-env"].extend(_collect_files_recursively(claude_dir / "session-env" / sid))

        if include_debug:
            debug_file = claude_dir / "debug" / f"{sid}.txt"
            if debug_file.is_file():
                files["debug"].append(debug_file)

        todos_dir = claude_dir / "todos"
        if todos_dir.is_dir():
            files["todos"].extend(todos_dir.glob(f"{sid}-*.json"))

    plans_dir = claude_dir / "plans"
    if plans_dir.is_dir():
        for slug in _discover_plan_slugs(project_meta_dir, session_ids):
            plan_file = plans_dir / slug
            if plan_file.is_file():
                files["plans"].append(plan_file)

    return files


def collect_project_files(project_dir: Path) -> List[Path]:
    """Collect project files, preferring git ls-files if available."""
    project_dir = project_dir.resolve()
    git_files = _collect_git_tracked_files(project_dir)
    if git_files is not None:
        return git_files
    return _collect_files_by_walk(project_dir)


def _collect_git_tracked_files(project_dir: Path) -> Optional[List[Path]]:
    """Try to collect files via git ls-files. Returns None if git unavailable."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(project_dir),
            capture_output=True,
            text=False,
        )
    except FileNotFoundError:
        return None

    if result.returncode != 0 or not result.stdout:
        return None

    return sorted(
        project_dir / entry.decode("utf-8", errors="replace")
        for entry in result.stdout.split(b"\x00")
        if entry
    )


def _collect_files_by_walk(project_dir: Path) -> List[Path]:
    """Fallback file collection via os.walk, skipping hidden dirs and junk."""
    skip_dirs = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".tox", ".venv", "venv"}
    paths: List[Path] = []
    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        paths.extend(Path(root) / fn for fn in filenames)
    return sorted(paths)


# ---------------------------------------------------------------------------
# Path rewriting
# ---------------------------------------------------------------------------

def build_replacement_map(
    source_project_path: str,
    target_project_path: str,
    source_claude_dir: str,
    target_claude_dir: str,
) -> List[Tuple[str, str]]:
    """Build a replacement map sorted longest-first to avoid partial matches.

    Includes both resolved (realpath) and unresolved variants to handle
    symlinks (e.g., macOS /var → /private/var).
    """
    source_encoded = encode_path(source_project_path)
    target_encoded = encode_path(target_project_path)

    replacements = [
        (source_project_path, target_project_path),
        (source_claude_dir, target_claude_dir),
        (source_encoded, target_encoded),
    ]

    # Also add unresolved variants if they differ from resolved
    source_project_real = os.path.realpath(source_project_path)
    source_claude_real = os.path.realpath(source_claude_dir)
    if source_project_real != source_project_path:
        replacements.append((source_project_real, target_project_path))
        replacements.append((encode_path(source_project_real), target_encoded))
    if source_claude_real != source_claude_dir:
        replacements.append((source_claude_real, target_claude_dir))

    return _dedupe_and_sort(replacements)


def _dedupe_and_sort(replacements: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Deduplicate replacements and sort longest-first."""
    seen: Set[str] = set()
    unique: List[Tuple[str, str]] = []
    for old, new in replacements:
        if old != new and old not in seen:
            seen.add(old)
            unique.append((old, new))
    unique.sort(key=lambda x: len(x[0]), reverse=True)
    return unique


def rewrite_line(line: str, replacements: List[Tuple[str, str]]) -> str:
    """Apply all replacements to a single line (longest-first)."""
    for old, new in replacements:
        line = line.replace(old, new)
    return line


def is_text_file(path: Path) -> bool:
    """Heuristic check if a file is text (for path rewriting)."""
    text_suffixes = {
        ".json", ".jsonl", ".txt", ".md", ".yaml", ".yml",
        ".toml", ".cfg", ".ini", ".log", ".csv",
    }
    if path.suffix.lower() in text_suffixes:
        return True
    try:
        chunk = path.read_bytes()[:8192]
        return b"\x00" not in chunk
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def create_manifest(
    source_project_path: str,
    source_claude_dir: str,
    session_ids: List[str],
    include_project_files: bool,
    include_debug: bool,
    source_project_path_unresolved: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the archive manifest."""
    m: Dict[str, Any] = {
        "version": 1,
        "portage_version": __version__,
        "source_project_path": source_project_path,
        "source_claude_dir": source_claude_dir,
        "source_encoded_path": encode_path(source_project_path),
        "session_ids": session_ids,
        "includes_project_files": include_project_files,
        "includes_debug": include_debug,
    }
    if source_project_path_unresolved and source_project_path_unresolved != source_project_path:
        m["source_project_path_unresolved"] = source_project_path_unresolved
    return m


# ---------------------------------------------------------------------------
# Pack
# ---------------------------------------------------------------------------

def _add_bytes_to_tar(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    """Add raw bytes as a file to a tar archive."""
    import io
    import time

    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def _add_files_to_tar(
    tar: tarfile.TarFile,
    files: List[Path],
    base_dir: Path,
    archive_prefix: str,
    verbose: bool,
) -> int:
    """Add files to a tar archive relative to base_dir. Returns count added."""
    count = 0
    for fp in files:
        try:
            rel = fp.relative_to(base_dir)
            tar.add(str(fp), arcname=f"{archive_prefix}/{rel}")
            count += 1
        except (ValueError, OSError) as e:
            if verbose:
                print(f"  Skipping {fp}: {e}", file=sys.stderr)
    return count


def cmd_pack(args: argparse.Namespace) -> int:
    """Pack a project and its Claude metadata into a portable archive."""
    project_dir_raw = Path(args.project_dir).expanduser().absolute()
    project_dir = project_dir_raw.resolve()
    if not project_dir.is_dir():
        print(f"Error: project directory not found: {project_dir}", file=sys.stderr)
        return 1

    config = PackConfig(
        project_path=project_dir,
        claude_dir=default_claude_dir(),
        verbose=args.verbose,
        output=Path(args.output) if args.output else None,
        include_project_files=not getattr(args, "no_project_files", False),
        include_debug=getattr(args, "include_debug", False),
    )

    if not config.project_meta_dir.is_dir():
        print(
            f"Error: no Claude metadata found at {config.project_meta_dir}\n"
            f"  (encoded path: {config.encoded_path})",
            file=sys.stderr,
        )
        return 1

    source_project_path = str(project_dir)
    source_unresolved = str(project_dir_raw) if str(project_dir_raw) != source_project_path else None

    if config.verbose:
        print(f"Project dir:   {project_dir}")
        print(f"Claude dir:    {config.claude_dir}")
        print(f"Encoded path:  {config.encoded_path}")
        print(f"Metadata dir:  {config.project_meta_dir}")

    session_ids = discover_session_ids(config.project_meta_dir)
    if config.verbose:
        print(f"Sessions:      {len(session_ids)}")

    session_files = discover_session_files(
        config.claude_dir, config.project_meta_dir, session_ids,
        include_debug=config.include_debug,
    )

    project_files = collect_project_files(project_dir) if config.include_project_files else []

    manifest = create_manifest(
        source_project_path=source_project_path,
        source_claude_dir=str(config.claude_dir),
        session_ids=session_ids,
        include_project_files=config.include_project_files,
        include_debug=config.include_debug,
        source_project_path_unresolved=source_unresolved,
    )

    output_path = config.output or (Path.cwd() / f"{project_dir.name}.portage.tar.gz")
    if config.verbose:
        print(f"Output:        {output_path}")

    archive_prefix = f"{project_dir.name}.portage"

    with tarfile.open(str(output_path), "w:gz") as tar:
        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        _add_bytes_to_tar(tar, f"{archive_prefix}/manifest.json", manifest_bytes)

        file_count = _add_files_to_tar(
            tar, project_files, project_dir,
            f"{archive_prefix}/project", config.verbose,
        ) if config.include_project_files else 0

        meta_files_flat = [fp for paths in session_files.values() for fp in paths]
        meta_count = _add_files_to_tar(
            tar, meta_files_flat, config.claude_dir,
            f"{archive_prefix}/claude-meta", config.verbose,
        )

    print(f"Packed {output_path}")
    print(f"  Sessions:       {len(session_ids)}")
    print(f"  Project files:  {file_count}")
    print(f"  Metadata files: {meta_count}")
    return 0


# ---------------------------------------------------------------------------
# Unpack
# ---------------------------------------------------------------------------

def _read_manifest(archive_root: Path) -> Optional[Dict[str, Any]]:
    """Read and return the manifest from an extracted archive."""
    manifest_path = archive_root / "manifest.json"
    if not manifest_path.is_file():
        return None
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _extract_archive(archive_path: Path, dest: Path) -> Optional[Path]:
    """Extract a tar.gz archive and return the single root directory."""
    with tarfile.open(str(archive_path), "r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(path=str(dest), filter="data")
        else:
            tar.extractall(path=str(dest))

    roots = [d for d in dest.iterdir() if d.is_dir()]
    if len(roots) != 1:
        print(f"Error: expected one root directory in archive, found {len(roots)}", file=sys.stderr)
        return None
    return roots[0]


def _copy_project_files(source: Path, target: Path) -> int:
    """Copy project files from extracted archive to target directory."""
    if not source.is_dir():
        return 0
    target.mkdir(parents=True, exist_ok=True)
    count = 0
    for src_file in _collect_files_recursively(source):
        rel = src_file.relative_to(source)
        dst_file = target / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_file), str(dst_file))
        count += 1
    return count


def _copy_with_rewrite(
    src: Path, dst: Path, replacements: List[Tuple[str, str]]
) -> bool:
    """Copy a text file, rewriting paths line by line. Returns True if any changes made."""
    try:
        original = src.read_text(encoding="utf-8", errors="replace")
        rewritten = "\n".join(
            rewrite_line(line, replacements)
            for line in original.split("\n")
        )
        # Preserve original line endings — split/join normalizes, so use line-by-line instead
        changed = False
        with open(src, "r", encoding="utf-8", errors="replace") as fin, \
             open(dst, "w", encoding="utf-8") as fout:
            for line in fin:
                new_line = rewrite_line(line, replacements)
                if new_line != line:
                    changed = True
                fout.write(new_line)
        # Preserve original modification time
        st = os.stat(str(src))
        os.utime(str(dst), (st.st_atime, st.st_mtime))
        return changed
    except OSError:
        shutil.copy2(str(src), str(dst))
        return False


def _copy_metadata_with_rewriting(
    meta_src: Path,
    claude_dir: Path,
    source_encoded: str,
    target_encoded: str,
    replacements: List[Tuple[str, str]],
) -> Tuple[int, int]:
    """Copy Claude metadata files, rewriting paths in text files.

    Returns (total_count, rewritten_count).
    """
    if not meta_src.is_dir():
        return 0, 0

    meta_count = 0
    rewritten_count = 0
    for src_file in _collect_files_recursively(meta_src):
        rel_str = str(src_file.relative_to(meta_src))
        if source_encoded in rel_str:
            rel_str = rel_str.replace(source_encoded, target_encoded)

        dst_file = claude_dir / rel_str
        dst_file.parent.mkdir(parents=True, exist_ok=True)

        if replacements and is_text_file(src_file):
            if _copy_with_rewrite(src_file, dst_file, replacements):
                rewritten_count += 1
        else:
            shutil.copy2(str(src_file), str(dst_file))

        meta_count += 1

    return meta_count, rewritten_count


def _build_unpack_replacements(
    manifest: Dict[str, Any],
    target_project_path: str,
    target_encoded: str,
    target_claude_dir: str,
) -> List[Tuple[str, str]]:
    """Build the full replacement map for unpacking, including unresolved path variants."""
    replacements = build_replacement_map(
        source_project_path=manifest["source_project_path"],
        target_project_path=target_project_path,
        source_claude_dir=manifest["source_claude_dir"],
        target_claude_dir=target_claude_dir,
    )

    unresolved = manifest.get("source_project_path_unresolved")
    if not unresolved or unresolved == manifest["source_project_path"]:
        return replacements

    extra = [
        (unresolved, target_project_path),
        (encode_path(unresolved), target_encoded),
    ]
    seen = {old for old, _ in replacements}
    for old, new in extra:
        if old != new and old not in seen:
            replacements.append((old, new))
            seen.add(old)
    replacements.sort(key=lambda x: len(x[0]), reverse=True)
    return replacements


def cmd_unpack(args: argparse.Namespace) -> int:
    """Unpack a portage archive to a target directory with path rewriting."""
    config = UnpackConfig(
        archive_path=Path(args.archive).resolve(),
        target_dir=Path(args.target_dir).resolve(),
        claude_dir=(Path(args.claude_dir).resolve() if args.claude_dir else default_claude_dir()),
        verbose=args.verbose,
    )

    if not config.archive_path.is_file():
        print(f"Error: archive not found: {config.archive_path}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmpdir:
        if config.verbose:
            print("Extracting to temp dir...")

        archive_root = _extract_archive(config.archive_path, Path(tmpdir))
        if archive_root is None:
            return 1

        manifest = _read_manifest(archive_root)
        if manifest is None:
            print("Error: manifest.json not found in archive", file=sys.stderr)
            return 1

        target_project_path = str(config.target_dir)
        target_encoded = encode_path(target_project_path)
        source_encoded = manifest["source_encoded_path"]

        if config.verbose:
            print(f"Source path:   {manifest['source_project_path']}")
            print(f"Target path:   {target_project_path}")
            print(f"Source encoded: {source_encoded}")
            print(f"Target encoded: {target_encoded}")

        replacements = _build_unpack_replacements(
            manifest, target_project_path, target_encoded, str(config.claude_dir),
        )

        if config.verbose:
            print(f"Replacements ({len(replacements)}):")
            for old, new in replacements:
                print(f"  {old} → {new}")

        file_count = _copy_project_files(archive_root / "project", config.target_dir)

        meta_count, rewritten_count = _copy_metadata_with_rewriting(
            archive_root / "claude-meta", config.claude_dir,
            source_encoded, target_encoded, replacements,
        )

    session_ids = manifest.get("session_ids", [])
    history_count = _register_sessions_in_history(
        config.claude_dir, target_encoded, target_project_path, session_ids, config.verbose,
    )

    print(f"Unpacked to {config.target_dir}")
    print(f"  Project files:  {file_count}")
    print(f"  Metadata files: {meta_count}")
    print(f"  Files rewritten: {rewritten_count}")
    print(f"  History entries: {history_count}")
    print(f"  Claude dir:     {config.claude_dir}")
    return 0


# ---------------------------------------------------------------------------
# History registration
# ---------------------------------------------------------------------------

def _read_existing_session_ids(history_path: Path) -> Set[str]:
    """Read session IDs already present in history.jsonl."""
    if not history_path.is_file():
        return set()
    ids: Set[str] = set()
    try:
        for line in history_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ids.add(json.loads(line).get("sessionId", ""))
            except (json.JSONDecodeError, KeyError):
                pass
    except OSError:
        pass
    return ids


def _extract_session_display_info(session_file: Path) -> Tuple[str, int]:
    """Extract display text and timestamp from the first user message in a session.

    Returns (display_text, timestamp_ms).
    """
    try:
        for line in session_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "user":
                continue

            timestamp_ms = _parse_timestamp_ms(record.get("timestamp", ""))
            display = _extract_display_text(record.get("message"))
            return display, timestamp_ms
    except OSError:
        pass
    return "(migrated session)", 0


def _parse_timestamp_ms(ts: str) -> int:
    """Parse an ISO timestamp string to epoch milliseconds."""
    if not ts:
        return 0
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, OSError):
        return 0


def _extract_display_text(message: Any) -> str:
    """Extract display text from a Claude message field."""
    if isinstance(message, list):
        for block in message:
            if isinstance(block, dict) and block.get("type") == "text":
                return block["text"][:100]
    elif isinstance(message, str):
        return message[:100]
    return "(migrated session)"


def _register_sessions_in_history(
    claude_dir: Path,
    target_encoded: str,
    target_project_path: str,
    session_ids: List[str],
    verbose: bool,
) -> int:
    """Append entries to ~/.claude/history.jsonl for migrated sessions."""
    if not session_ids:
        return 0

    history_path = claude_dir / "history.jsonl"
    project_dir = claude_dir / "projects" / target_encoded
    existing = _read_existing_session_ids(history_path)

    entries: List[str] = []
    for sid in session_ids:
        if sid in existing:
            continue
        session_file = project_dir / f"{sid}.jsonl"
        if not session_file.is_file():
            continue

        display, timestamp_ms = _extract_session_display_info(session_file)
        entries.append(json.dumps({
            "display": display,
            "pastedContents": {},
            "timestamp": timestamp_ms,
            "project": target_project_path,
            "sessionId": sid,
        }, ensure_ascii=False))

    if entries:
        try:
            with open(history_path, "a", encoding="utf-8") as f:
                for entry in entries:
                    f.write(entry + "\n")
            if verbose:
                print(f"Added {len(entries)} entries to {history_path}")
        except OSError as e:
            print(f"Warning: could not update history: {e}", file=sys.stderr)

    return len(entries)


# ---------------------------------------------------------------------------
# Inspect
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def cmd_inspect(args: argparse.Namespace) -> int:
    """Print manifest and file listing of a portage archive."""
    archive_path = Path(args.archive).resolve()
    if not archive_path.is_file():
        print(f"Error: archive not found: {archive_path}", file=sys.stderr)
        return 1

    with tarfile.open(str(archive_path), "r:gz") as tar:
        manifest = _find_manifest_in_tar(tar)
        if manifest is None:
            print("Error: manifest.json not found in archive", file=sys.stderr)
            return 1

        _print_manifest(manifest)
        _print_archive_contents(tar, args.verbose)

    print(f"  Archive size: {_format_size(archive_path.stat().st_size)}")
    return 0


def _find_manifest_in_tar(tar: tarfile.TarFile) -> Optional[Dict[str, Any]]:
    """Find and parse manifest.json from a tar archive."""
    for member in tar.getmembers():
        if member.name.endswith("/manifest.json"):
            f = tar.extractfile(member)
            if f:
                return json.loads(f.read().decode("utf-8"))
    return None


def _print_manifest(manifest: Dict[str, Any]) -> None:
    """Print manifest summary to stdout."""
    print("=== Manifest ===")
    print(f"  Portage version:  {manifest.get('portage_version', 'unknown')}")
    print(f"  Source path:      {manifest['source_project_path']}")
    print(f"  Claude dir:       {manifest['source_claude_dir']}")
    print(f"  Encoded path:     {manifest['source_encoded_path']}")
    print(f"  Sessions:         {len(manifest.get('session_ids', []))}")
    for sid in manifest.get("session_ids", []):
        print(f"    - {sid}")
    print(f"  Project files:    {'yes' if manifest.get('includes_project_files') else 'no'}")
    print(f"  Debug logs:       {'yes' if manifest.get('includes_debug') else 'no'}")


def _print_archive_contents(tar: tarfile.TarFile, verbose: bool) -> None:
    """Print archive content summary to stdout."""
    print()
    print("=== Archive Contents ===")
    categories: Dict[str, List[str]] = {}
    for member in tar.getmembers():
        if not member.isfile():
            continue
        parts = member.name.split("/", 2)
        cat = parts[1] if len(parts) >= 2 else "(root)"
        categories.setdefault(cat, []).append(member.name)

    total = 0
    for cat in sorted(categories):
        files = categories[cat]
        total += len(files)
        print(f"  {cat}: {len(files)} file(s)")
        if verbose:
            for fn in sorted(files)[:20]:
                print(f"    {fn}")
            if len(files) > 20:
                print(f"    ... and {len(files) - 20} more")

    print(f"  Total: {total} file(s)")


# ---------------------------------------------------------------------------
# Rename
# ---------------------------------------------------------------------------

def _rewrite_file_in_place(
    path: Path, replacements: List[Tuple[str, str]]
) -> bool:
    """Rewrite a text file in place, applying path replacements.

    Returns True if any changes were made.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return False

    changed = False
    new_lines: List[str] = []
    for line in lines:
        new_line = rewrite_line(line, replacements)
        if new_line != line:
            changed = True
        new_lines.append(new_line)

    if changed:
        path.write_text("".join(new_lines), encoding="utf-8")

    return changed


def cmd_rename(args: argparse.Namespace) -> int:
    """Rewrite Claude metadata after moving/renaming a project directory."""
    config = RenameConfig(
        old_path=Path(args.old_path).expanduser().resolve(),
        new_path=Path(args.new_path).expanduser().resolve(),
        claude_dir=default_claude_dir(),
        verbose=args.verbose,
    )

    if config.old_path == config.new_path:
        print("Error: old and new paths are the same", file=sys.stderr)
        return 1

    old_encoded = encode_path(config.old_path)
    new_encoded = encode_path(config.new_path)
    old_meta = config.claude_dir / "projects" / old_encoded
    new_meta = config.claude_dir / "projects" / new_encoded

    if not old_meta.is_dir():
        print(
            f"Error: no Claude metadata found for {config.old_path}\n"
            f"  (looked for {old_meta})",
            file=sys.stderr,
        )
        return 1

    if new_meta.exists():
        print(
            f"Error: metadata already exists for target path\n"
            f"  {new_meta}\n"
            f"  Remove it first or choose a different target.",
            file=sys.stderr,
        )
        return 1

    if config.verbose:
        print(f"Old path:     {config.old_path}")
        print(f"New path:     {config.new_path}")
        print(f"Old encoded:  {old_encoded}")
        print(f"New encoded:  {new_encoded}")

    replacements = build_replacement_map(
        source_project_path=str(config.old_path),
        target_project_path=str(config.new_path),
        source_claude_dir=str(config.claude_dir),
        target_claude_dir=str(config.claude_dir),
    )

    if config.verbose:
        print(f"Replacements ({len(replacements)}):")
        for old, new in replacements:
            print(f"  {old} → {new}")

    session_ids = discover_session_ids(old_meta)
    session_files = discover_session_files(
        config.claude_dir, old_meta, session_ids, include_debug=True,
    )

    if config.verbose:
        print(f"Sessions:     {len(session_ids)}")

    rewritten = sum(
        1
        for paths in session_files.values()
        for fp in paths
        if is_text_file(fp) and _rewrite_file_in_place(fp, replacements)
    )

    shutil.move(str(old_meta), str(new_meta))

    total_files = sum(len(v) for v in session_files.values())
    print(f"Renamed {config.old_path} → {config.new_path}")
    print(f"  Sessions:        {len(session_ids)}")
    print(f"  Files processed: {total_files}")
    print(f"  Files rewritten: {rewritten}")
    print(f"  Metadata moved:  {old_meta.name} → {new_meta.name}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-portage",
        description="Portable Claude Code workspace archives",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    # pack
    p_pack = sub.add_parser("pack", help="Pack project + Claude metadata into archive")
    p_pack.add_argument("project_dir", help="Path to the project directory")
    p_pack.add_argument("-o", "--output", help="Output archive path (default: <project>.portage.tar.gz)")
    p_pack.add_argument("--no-project-files", action="store_true", help="Exclude project files (metadata only)")
    p_pack.add_argument("--include-debug", action="store_true", help="Include debug logs")
    p_pack.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # unpack
    p_unpack = sub.add_parser("unpack", help="Unpack archive to target directory")
    p_unpack.add_argument("archive", help="Path to .portage.tar.gz archive")
    p_unpack.add_argument("target_dir", help="Target directory for project files")
    p_unpack.add_argument("--claude-dir", help="Claude config dir (default: ~/.claude)")
    p_unpack.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    # inspect
    p_inspect = sub.add_parser("inspect", help="Inspect archive contents")
    p_inspect.add_argument("archive", help="Path to .portage.tar.gz archive")
    p_inspect.add_argument("-v", "--verbose", action="store_true", help="Show individual files")

    # rename
    p_rename = sub.add_parser("rename", help="Rewrite Claude metadata after moving/renaming a project")
    p_rename.add_argument("old_path", help="Original project directory path")
    p_rename.add_argument("new_path", help="New project directory path")
    p_rename.add_argument("-v", "--verbose", action="store_true", help="Verbose output")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1

    commands = {
        "pack": cmd_pack,
        "unpack": cmd_unpack,
        "inspect": cmd_inspect,
        "rename": cmd_rename,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
