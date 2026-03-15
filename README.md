# claude-portage

Portable Claude Code workspace archives.

Claude Code stores per-project session history, file snapshots, and metadata in `~/.claude/` using a path-encoding scheme (e.g., `/Users/alice/src/foo` → `-Users-alice-src-foo`). This data is tightly coupled to absolute paths, making it impossible to move a project to another machine or directory and use `claude --continue` or `claude --resume`.

**claude-portage** bundles a project + its Claude metadata into a portable archive that can be unpacked anywhere with automatic path rewriting.

## Installation

```bash
# Install from source
pip install .

# Or run directly (zero dependencies)
python3 claude_portage.py <command>
```

## Usage

### Pack a project

```bash
claude-portage pack /path/to/my-project
# Creates my-project.portage.tar.gz

claude-portage pack /path/to/my-project -o /tmp/backup.portage.tar.gz
claude-portage pack /path/to/my-project --no-project-files  # metadata only
claude-portage pack /path/to/my-project --include-debug -v   # include debug logs
```

### Inspect an archive

```bash
claude-portage inspect my-project.portage.tar.gz
claude-portage inspect my-project.portage.tar.gz -v  # show individual files
```

### Unpack to a new location

```bash
claude-portage unpack my-project.portage.tar.gz /new/path/to/my-project
# Project files extracted to /new/path/to/my-project
# Claude metadata placed in ~/.claude/ with all paths rewritten
```

Then:
```bash
cd /new/path/to/my-project
claude --resume  # Sessions from the original machine appear
```

## How It Works

1. **Pack** discovers all Claude metadata for a project: session JSONL files, subagent logs, tool results, file-history snapshots, session environments, todos, plans, and memory.
2. The archive includes a `manifest.json` recording the source absolute paths.
3. **Unpack** extracts project files and places Claude metadata into `~/.claude/` on the target machine, performing line-by-line string replacement of all source paths with target paths.
4. Path rewriting handles the project path, the Claude config directory path, and the encoded directory name, applied longest-first to avoid partial matches.

## Known Limitations

- Path rewriting is string-based, not JSON-aware. This works because paths appear in many contexts (command strings, tool outputs, file paths) where structured rewriting would miss them.
- File-history snapshots (source code versions) are copied as-is without path rewriting, since they are project source code, not metadata.
- The archive does not include Claude's global config (`settings.json`, API keys, etc.) — only project-specific session data.
- Session UUIDs are preserved; if the same session ID already exists at the target, files will be overwritten.

## License

MIT
