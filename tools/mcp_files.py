#!/usr/bin/env python3
"""A read-only filesystem MCP server: browse, read and grep local files.

WHY A SEPARATE PROCESS SPEAKING MCP, RATHER THAN FOUR MORE BUILTINS
    The bridge already has a tool registry, so `grep` could be a function call
    in-process.  It is a server anyway because the *authority* is the point:
    a child process that starts with an explicit list of roots cannot read
    anything outside them, and the operator can see that list in `ps`.  A
    builtin inherits the bridge's whole filesystem view, which is the whole
    machine.  Same reasoning as mcp_stack_status.py, applied to files.

    (The official `@modelcontextprotocol/server-filesystem` does the same job,
    but this deployment has no node and no npm on the host, and the repo is
    stdlib-only so it stays deployable offline.  Swapping to it is a config
    line, not a code change.)

WHAT IT CAN SEE: NOTHING BY DEFAULT
    A root must be named with --root, one per directory.  No roots, no server:
    it refuses to start rather than defaulting to "/" or the cwd, because a
    voice assistant whose default is "the whole disk" is an incident.

    The model never supplies an absolute path.  It names a root (optional when
    there is exactly one) and a path *relative* to it.  Absolute paths, `~`,
    drive letters and `..` are refused at the argument layer, and containment
    is then re-checked against os.path.realpath() *after* resolution -- so a
    symlink planted inside a root that points at /etc/shadow resolves outside
    and is refused.  The two layers catch different mistakes: the argument
    check catches a confused model, the realpath check catches an adversary.

READ-ONLY IS ABSOLUTE
    There is no write, delete, move, chmod or mkdir tool, and there will not
    be one.  O_RDONLY|O_NOFOLLOW is the only open flag in this file.

WHAT IT STILL LEAKS
    Anything inside a root.  Exposing a repo is not neutral: it publishes
    every .env, key and credential in it to whatever the model decides to
    read, and the transcript goes to a browser.  Roots are opt-in per
    directory for that reason, --ignore prunes .git and friends by default,
    and `--doctor` prints exactly what is exposed so it can be reviewed
    before it is ever reachable.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import stat
import sys

PROTOCOL = "2025-03-26"
SERVER_INFO = {"name": "local-files", "version": "1.0"}
DEFAULT_IGNORE = (".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
                  "build", "dist", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea")
BINARY_SNIFF = 4096
MAX_PATTERN_CHARS = 512


class Refused(Exception):
    """A request this server will not carry out.  Becomes isError, never a crash."""


def _clean_relative(raw, field="path") -> str:
    """Accept only a plain relative path.  Anything absolute or escaping is refused.

    This is the argument layer.  It is deliberately dumb and boring -- it does
    not resolve anything, it just refuses the shapes that mean "somewhere
    else".  The realpath check in Roots.contain is the one that actually
    decides, because it runs after the filesystem has had its say.
    """
    text = str(raw if raw is not None else "").strip()
    if text in ("", ".", "./", "."):
        return ""
    if text.startswith(("/", "~", "\\")):
        raise Refused(f"{field} must be relative to a configured root; absolute paths are not accepted")
    if len(text) > 1 and text[1] == ":":            # windows drive letter
        raise Refused(f"{field} must be relative to a configured root; drive letters are not accepted")
    kept = []
    for part in text.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise Refused(f"{field} may not contain '..'")
        if "\x00" in part:
            raise Refused(f"{field} may not contain NUL")
        kept.append(part)
    return "/".join(kept)


class Roots:
    """The allow-list.  Every path this server touches resolves under one of these."""

    def __init__(self, entries, ignore=DEFAULT_IGNORE, max_read_bytes=65536, max_lines=2000,
                 max_matches=200, max_walk=20000, max_entries=400, max_output_chars=24000):
        self.roots = []                              # (name, realpath)
        for entry in entries:
            real = os.path.realpath(os.path.abspath(os.path.expanduser(entry)))
            if not os.path.isdir(real):
                raise Refused(f"--root {entry!r} is not a directory")
            name = os.path.basename(real) or "root"
            if any(name == existing for existing, _ in self.roots):
                name = f"{name}{len(self.roots) + 1}"
            self.roots.append((name, real))
        if not self.roots:
            raise Refused("no --root given: refusing to expose the filesystem")
        self.by_name = dict(self.roots)
        self.ignore = set(ignore)
        self.max_read_bytes = max_read_bytes
        self.max_lines = max_lines
        self.max_matches = max_matches
        self.max_walk = max_walk
        self.max_entries = max_entries
        self.max_output_chars = max_output_chars

    def pick(self, name):
        """Resolve a root by name, or the single root when there is only one."""
        if name in (None, "", "auto"):
            if len(self.roots) > 1:
                raise Refused("several roots are configured; name which one with 'root': "
                              + ", ".join(sorted(name for name, _ in self.roots)))
            return self.roots[0]
        wanted = str(name)
        for candidate, real in self.roots:
            if candidate == wanted:
                return candidate, real
        raise Refused(f"unknown root {wanted!r}; configured: "
                      + ", ".join(sorted(name for name, _ in self.roots)))

    def contain(self, root_real, candidate) -> None:
        """Raise unless `candidate` (already realpath'd) is root_real or beneath it."""
        if candidate != root_real and not candidate.startswith(root_real.rstrip(os.sep) + os.sep):
            raise Refused("that path resolves outside the configured root (symlink or race?)")

    def resolve(self, root_name=None, relative=""):
        """Return (root_name, real_path) for a relative path, or raise Refused."""
        name, real = self.pick(root_name)
        rel = _clean_relative(relative)
        if not rel:
            return name, real
        probe = os.path.realpath(os.path.join(real, rel))
        self.contain(real, probe)
        return name, probe

    def walk(self, top, glob=None):
        """Yield (dirpath, filename, fullpath) under top.  Never follows symlinks."""
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in self.ignore and not d.endswith(".egg-info"))
            for filename in sorted(filenames):
                scanned += 1
                if scanned > self.max_walk:
                    raise Refused(f"stopped after scanning {self.max_walk} files; narrow the path")
                full = os.path.join(dirpath, filename)
                if glob and not fnmatch.fnmatch(filename, glob):
                    continue
                if os.path.islink(full):
                    continue                          # a link out of the root is not ours to read
                yield dirpath, filename, full


# --------------------------------------------------------------------------- tools

def _size(path) -> int:
    try:
        return os.stat(path, follow_symlinks=False).st_size
    except OSError:
        return -1


def tool_list_dir(roots, args):
    _, target = roots.resolve(args.get("root"), args.get("path", ""))
    if not os.path.isdir(target):
        raise Refused("not a directory: " + str(args.get("path") or "."))
    show_hidden = bool(args.get("include_hidden"))
    entries = []
    for name in sorted(os.listdir(target)):
        if not show_hidden and name.startswith("."):
            continue
        full = os.path.join(target, name)
        try:
            is_link = os.path.islink(full)
            is_dir = os.path.isdir(full)
        except OSError:
            continue
        entries.append({"name": name + ("/" if is_dir else ""),
                        "type": ("link" if is_link else "dir" if is_dir else "file"),
                        "bytes": _size(full)})
        if len(entries) >= roots.max_entries:
            entries.append({"name": f"[…truncated at {roots.max_entries} entries]", "type": "dir", "bytes": 0})
            break
    return {"root": os.path.basename(target), "path": _relative_to_root(roots, target) or ".",
            "entries": entries}


def _relative_to_root(roots, target):
    for _, real in roots.roots:
        if target == real:
            return ""
        if target.startswith(real.rstrip(os.sep) + os.sep):
            return target[len(real):].lstrip(os.sep)
    return None


def _open_read(roots, path):
    """Open O_RDONLY|O_NOFOLLOW, then verify the fd really points inside a root.

    realpath() and open() are two syscalls, and in between a component can be
    swapped for a symlink.  /proc/self/fd says where the descriptor actually
    went, so the check runs on the thing we are about to read rather than on a
    guess about it.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise Refused(f"cannot open: {error.strerror}") from error
    try:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            raise Refused("that path is a directory; use list_dir")
        if not stat.S_ISREG(info.st_mode):
            raise Refused("not a regular file")
        try:
            landed = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            landed = None
        if landed:
            for _, real in roots.roots:
                if real == landed or landed.startswith(real.rstrip(os.sep) + os.sep):
                    break
            else:
                raise Refused("descriptor resolved outside the configured root")
        return fd, info.st_size
    except BaseException:
        os.close(fd)
        raise


def tool_read_file(roots, args):
    _, target = roots.resolve(args.get("root"), args.get("path"))
    if not target:
        raise Refused("read_file needs a path")
    fd, size = _open_read(roots, target)
    try:
        head = os.read(fd, BINARY_SNIFF)
        if b"\x00" in head:
            return {"path": _relative_to_root(roots, target), "binary": True,
                    "bytes": size, "text": "",
                    "note": "binary file: this server only returns text"}
        os.lseek(fd, 0, os.SEEK_SET)
        budget = min(roots.max_read_bytes, max(size, 0) + 1)
        raw = os.read(fd, budget)
    finally:
        os.close(fd)
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines()
    offset = max(1, int(args.get("offset") or 1))
    limit = int(args.get("limit") or roots.max_lines)
    limit = max(1, min(limit, roots.max_lines))
    window = lines[offset - 1:offset - 1 + limit]
    shown = "\n".join(f"{number}\t{line}" for number, line in enumerate(window, start=offset))
    # Two different ceilings can stop this call and the model needs to know
    # which: a byte cap means re-read with a smaller window, more lines means
    # page forward with offset.  One shared "truncated" flag invites it to
    # retry the same call that just failed.
    byte_capped = bool(size and len(raw) >= budget and len(raw) < size)
    more_lines = offset - 1 + len(window) < len(lines)
    return {"path": _relative_to_root(roots, target), "offset": offset,
            "lines_total": len(lines), "lines_returned": len(window),
            "truncated": byte_capped or more_lines, "more_lines": more_lines,
            "byte_capped": byte_capped, "text": shown}


def _scan(roots, start, glob):
    for _, filename, full in roots.walk(start, glob):
        yield filename, full


def tool_grep(roots, args):
    pattern = str(args.get("pattern") or "")
    if not pattern.strip():
        raise Refused("grep needs a pattern")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise Refused(f"pattern longer than {MAX_PATTERN_CHARS} characters")
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        matcher = re.compile(pattern, flags)
    except re.error as error:
        raise Refused(f"invalid regular expression: {error}") from error
    _, start = roots.resolve(args.get("root"), args.get("path", ""))
    glob = args.get("glob")
    limit = max(1, min(int(args.get("max_matches") or roots.max_matches), roots.max_matches))
    context = max(0, min(int(args.get("context") or 0), 3))
    where = start if os.path.isdir(start) else None
    files = [(os.path.basename(start), start)] if where is None else list(_scan(roots, start, glob))
    matches, files_searched, skipped = [], 0, 0
    for filename, full in files:
        if len(matches) >= limit:
            break
        try:
            fd, size = _open_read(roots, full)
        except Refused:
            skipped += 1
            continue
        try:
            raw = os.read(fd, roots.max_read_bytes)
        finally:
            os.close(fd)
        if b"\x00" in raw:
            skipped += 1
            continue
        files_searched += 1
        lines = raw.decode("utf-8", "replace").splitlines()
        for index, line in enumerate(lines, start=1):
            if not matcher.search(line):
                continue
            row = {"file": _relative_to_root(roots, full) or filename, "line": index,
                   "text": line[:1000]}
            if context:
                row["before"] = lines[max(0, index - 1 - context):index - 1]
                row["after"] = lines[index:index + context]
            matches.append(row)
            if len(matches) >= limit:
                break
    return {"pattern": pattern, "matches": matches, "count": len(matches),
            "files_searched": files_searched, "binary_or_unreadable_skipped": skipped,
            "hit_limit": len(matches) >= limit}


def tool_find(roots, args):
    glob = str(args.get("glob") or args.get("name") or "")
    if not glob.strip():
        raise Refused("find needs a glob or name")
    if len(glob) > MAX_PATTERN_CHARS:
        raise Refused("pattern too long")
    _, start = roots.resolve(args.get("root"), args.get("path", ""))
    limit = max(1, min(int(args.get("max_results") or 100), 500))
    want_type = args.get("type")
    found = []
    for dirpath, filename, full in roots.walk(start, None):
        if want_type == "dir":
            raise Refused("internal")
        if glob and not (fnmatch.fnmatch(filename, glob)
                         or fnmatch.fnmatch(_relative_to_root(roots, full) or filename, glob)):
            continue
        found.append({"path": _relative_to_root(roots, full) or filename, "bytes": _size(full)})
        if len(found) >= limit:
            break
    return {"glob": glob, "results": found, "count": len(found), "hit_limit": len(found) >= limit}


def tool_roots(roots, args):
    return {"roots": [{"name": name, "path": real} for name, real in roots.roots],
            "ignore": sorted(roots.ignore),
            "caps": {"max_read_bytes": roots.max_read_bytes, "max_lines": roots.max_lines,
                     "max_matches": roots.max_matches, "max_walk": roots.max_walk,
                     "max_output_chars": roots.max_output_chars}}


TOOLS = [
    {"name": "list_dir",
     "description": ("List a directory inside the allowed roots. Returns names, type and size. "
                     "Read-only. Pass no path for the root itself."),
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Relative to the root. No '..' or absolute paths."},
         "root": {"type": "string", "description": "Which root, when several are configured."},
         "include_hidden": {"type": "boolean"}}}},
    {"name": "read_file",
     "description": ("Read a text file inside the allowed roots, with line numbers. Use offset/limit "
                     "to page through a long file. Read-only; binary files are refused."),
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string"}, "root": {"type": "string"},
         "offset": {"type": "integer", "description": "1-based first line"},
         "limit": {"type": "integer", "description": "Lines to return"}}}},
    {"name": "grep",
     "description": ("Search file contents for a regular expression inside the allowed roots. "
                     "Returns file, line number and matching line. Read-only."),
     "inputSchema": {"type": "object", "properties": {
         "pattern": {"type": "string", "description": "Python regular expression"},
         "path": {"type": "string", "description": "Subdirectory to search; defaults to the whole root"},
         "root": {"type": "string"},
         "glob": {"type": "string", "description": "Filename filter, e.g. '*.py'"},
         "ignore_case": {"type": "boolean"},
         "context": {"type": "integer", "description": "Up to 3 lines either side"},
         "max_matches": {"type": "integer"}}}},
    {"name": "find",
     "description": ("Find files by name or glob inside the allowed roots. Read-only."),
     "inputSchema": {"type": "object", "properties": {
         "glob": {"type": "string", "description": "e.g. '*.md' or 'mcp_*.py'"},
         "path": {"type": "string"}, "root": {"type": "string"},
         "max_results": {"type": "integer"}}}},
    {"name": "roots",
     "description": ("Report which directories this server is allowed to read and what its limits are. "
                     "Call this first if unsure what is available."),
     "inputSchema": {"type": "object", "properties": {}}},
]

HANDLERS = {"list_dir": tool_list_dir, "read_file": tool_read_file, "grep": tool_grep,
            "find": tool_find, "roots": tool_roots}


# ----------------------------------------------------------------------- protocol

def _write(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _answer(identifier, result) -> None:
    _write({"jsonrpc": "2.0", "id": identifier, "result": result})


def _tool_result(identifier, text, is_error=False) -> None:
    _answer(identifier, {"content": [{"type": "text", "text": text}], "isError": bool(is_error)})


TRIMMABLE = ("matches", "results", "entries")


def _fit(roots: Roots, payload) -> str:
    """Serialize under the output cap, always as valid JSON.

    Slicing the serialized string is the obvious way to enforce a character
    budget and it is broken: it cuts mid-token and hands the model a document
    that no longer parses, so a "too long" guard turns a large answer into a
    corrupt one.  Trim the payload instead -- drop rows, shorten the text
    field -- and say which happened, so the model narrows the query or pages
    with offset rather than retrying the call that just overflowed.
    """
    text = json.dumps(payload, indent=1, ensure_ascii=False)
    if len(text) <= roots.max_output_chars:
        return text
    trimmed = dict(payload)
    dropped = 0
    for key in TRIMMABLE:
        rows = trimmed.get(key)
        if isinstance(rows, list) and rows:
            trimmed[key] = []
            dropped += len(rows)
    if isinstance(trimmed.get("text"), str):
        trimmed["text"] = trimmed["text"][:max(0, roots.max_output_chars // 2)]
    if dropped or "text" in trimmed:
        trimmed["truncated"] = True
    if dropped:
        trimmed["rows_dropped"] = dropped
    trimmed["note"] = ("output exceeded this server's cap: narrow the path, lower max_matches, "
                       "or page read_file with offset/limit")
    text = json.dumps(trimmed, indent=1, ensure_ascii=False)
    if len(text) <= roots.max_output_chars:
        return text
    # Still too big: some field we do not know how to trim is enormous.  A
    # small valid refusal beats a large invalid one.
    return json.dumps({"too_large": True, "cap_chars": roots.max_output_chars,
                       "note": "the result is larger than this server may return; narrow the "
                               "path or page read_file with offset/limit"}, indent=1)


def serve(roots: Roots) -> int:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        method, identifier = message.get("method"), message.get("id")
        if identifier is None:                       # notifications get no answer
            continue
        if method == "initialize":
            _answer(identifier, {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                                 "serverInfo": SERVER_INFO})
        elif method == "ping":
            _answer(identifier, {})
        elif method == "tools/list":
            _answer(identifier, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                _tool_result(identifier, "tool error: arguments must be an object", True)
                continue
            handler = HANDLERS.get(name)
            if handler is None:
                _tool_result(identifier, f"tool error: unknown tool {name!r}", True)
                continue
            try:
                payload = handler(roots, arguments)
                _tool_result(identifier, _fit(roots, payload))
            except Refused as error:
                _tool_result(identifier, f"tool error: {error}", True)
            except (OSError, ValueError, TypeError) as error:
                _tool_result(identifier, f"tool error: {type(error).__name__}: {error}", True)
        else:
            _write({"jsonrpc": "2.0", "id": identifier,
                    "error": {"code": -32601, "message": f"unknown {method}"}})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only filesystem MCP server.")
    parser.add_argument("--root", action="append", default=[], metavar="DIR",
                        help="Directory to expose. Repeatable. Required: no roots means no server.")
    parser.add_argument("--ignore", action="append", default=[],
                        help="Directory name to skip while walking (adds to the defaults).")
    parser.add_argument("--max-read-bytes", type=int, default=65536)
    parser.add_argument("--max-lines", type=int, default=2000)
    parser.add_argument("--max-matches", type=int, default=200)
    parser.add_argument("--max-walk", type=int, default=20000)
    parser.add_argument("--max-output-chars", type=int, default=24000)
    parser.add_argument("--doctor", action="store_true",
                        help="Print exactly what is exposed, then exit without serving.")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.root:
        print("mcp_files: no --root given. Refusing to start: a file server with no configured "
              "scope would expose the whole filesystem to the model.\n"
              "  e.g. mcp_files.py --root /srv/voice-assistant", file=sys.stderr)
        return 2
    try:
        roots = Roots(args.root, ignore=set(DEFAULT_IGNORE) | set(args.ignore),
                      max_read_bytes=args.max_read_bytes, max_lines=args.max_lines,
                      max_matches=args.max_matches, max_walk=args.max_walk,
                      max_output_chars=args.max_output_chars)
    except Refused as error:
        print(f"mcp_files: {error}", file=sys.stderr)
        return 2
    if args.doctor:
        print("Read-only filesystem MCP server.  These directories are readable by the model:")
        for name, real in roots.roots:
            files = sum(1 for _ in roots.walk(real))
            print(f"  {name}: {real}  ({files} files walkable)")
        print(f"  skipped directory names: {', '.join(sorted(roots.ignore))}")
        print(f"  caps: read={roots.max_read_bytes}B lines={roots.max_lines} "
              f"matches={roots.max_matches} walk={roots.max_walk} "
              f"output={roots.max_output_chars} chars")
        print("  write/delete: not implemented, by design.")
        return 0
    return serve(roots)


if __name__ == "__main__":
    raise SystemExit(main())
