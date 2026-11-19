#!/usr/bin/env python3
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FILES = ROOT / "files"
MEM = ROOT / "memory"
LOGS = ROOT / "logs"
DB = MEM / "memory.db"

FILES.mkdir(parents=True, exist_ok=True)
MEM.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

MAX_READ = 200_000
MAX_WRITE = 2_000_000
MAX_OUTPUT = 200_000
MAX_SCAN_FILE = 1_000_000
SHELL_TIMEOUT = 20
MAX_SHELL_OUTPUT = 200_000

ALLOWED_SHELL = {
    "ls", "cat", "head", "tail", "grep", "wc", "file", "stat",
    "du", "df", "date", "pwd", "which", "uname", "sw_vers", "mdls",
}

METACHARS = set("|&;<>()`$\n\r{}[]")


def audit(tool, status, detail=""):
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "status": status,
            "detail": str(detail)[:400],
        }
        with open(LOGS / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def resolve(path):
    raw = str(path if path not in (None, "") else ".")
    if raw.startswith("~"):
        raise ValueError("tilde paths are not allowed; use paths relative to the ucai root")
    p = Path(raw)
    cand = p if p.is_absolute() else (ROOT / p)
    cand = cand.resolve(strict=False)
    try:
        cand.relative_to(ROOT)
    except ValueError:
        raise ValueError(f"path escapes the sandbox root {ROOT}: {path}")
    return cand


def rel(p):
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def is_binary(data):
    return b"\x00" in data[:8192]


def db():
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
            id UNINDEXED, title, body, tags
        )
        """
    )
    conn.commit()
    conn.close()


def tool_list_files(args):
    target = resolve(args.get("path", "."))
    if not target.is_dir():
        raise ValueError(f"not a directory: {rel(target)}")
    entries = []
    for child in sorted(target.iterdir(), key=lambda x: x.name.lower()):
        try:
            st = child.stat()
            entries.append({
                "name": child.name,
                "path": rel(child),
                "type": "dir" if child.is_dir() else "file",
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            })
        except OSError:
            continue
        if len(entries) >= 500:
            break
    return json.dumps({"root": rel(target) or ".", "entries": entries}, indent=2)


def tool_read_file(args):
    target = resolve(args.get("path"))
    if not target.is_file():
        raise ValueError(f"not a file: {rel(target)}")
    size = target.stat().st_size
    data = target.read_bytes()
    if is_binary(data):
        raise ValueError("binary file; read it with an appropriate tool")
    text = data.decode("utf-8", errors="replace")
    truncated = False
    if len(text) > MAX_READ:
        text = text[:MAX_READ]
        truncated = True
    out = {
        "path": rel(target),
        "size": size,
        "content": text,
        "truncated": truncated,
    }
    return json.dumps(out, indent=2)


def tool_write_file(args):
    target = resolve(args.get("path"))
    content = args.get("content", "")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE:
        raise ValueError(f"content too large ({len(data)} bytes, max {MAX_WRITE})")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / (f".{target.name}.tmp.{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return json.dumps({"path": rel(target), "bytes": len(data)}, indent=2)


def tool_append_file(args):
    target = resolve(args.get("path"))
    content = args.get("content", "")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE:
        raise ValueError(f"content too large ({len(data)} bytes, max {MAX_WRITE})")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "ab") as f:
        f.write(data)
    return json.dumps({"path": rel(target), "appended_bytes": len(data)}, indent=2)


def tool_edit_file(args):
    target = resolve(args.get("path"))
    old = args.get("old")
    new = args.get("new")
    if not isinstance(old, str) or not isinstance(new, str) or old == "":
        raise ValueError("old and new must be strings and old must be non-empty")
    if not target.is_file():
        raise ValueError(f"not a file: {rel(target)}")
    text = target.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        raise ValueError("old text not found")
    replace_all = bool(args.get("all", False))
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    target.write_text(updated, encoding="utf-8")
    return json.dumps({"path": rel(target), "occurrences": count, "replaced_all": replace_all}, indent=2)


def tool_glob(args):
    pattern = args.get("pattern", "**/*")
    if not isinstance(pattern, str) or pattern.startswith("/"):
        raise ValueError("pattern must be a non-empty relative glob")
    matches = []
    for p in ROOT.glob(pattern):
        try:
            rp = p.resolve(strict=False)
            rp.relative_to(ROOT)
        except (ValueError, OSError):
            continue
        matches.append(rel(rp) + ("/" if rp.is_dir() else ""))
        if len(matches) >= 500:
            break
    matches.sort()
    return json.dumps({"pattern": pattern, "matches": matches}, indent=2)


def tool_grep(args):
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or pattern == "":
        raise ValueError("pattern is required")
    rx = re.compile(pattern)
    start = resolve(args.get("path", "."))
    limit = int(args.get("max_results", 200))
    results = []
    files = [start] if start.is_file() else sorted(start.rglob("*"))
    for p in files:
        if len(results) >= limit:
            break
        if not p.is_file() or p == DB:
            continue
        try:
            if p.stat().st_size > MAX_SCAN_FILE:
                continue
            data = p.read_bytes()
            if is_binary(data):
                continue
            text = data.decode("utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                results.append(f"{rel(p)}:{i}:{line[:500]}")
                if len(results) >= limit:
                    break
    return json.dumps({"pattern": pattern, "matches": results, "count": len(results)}, indent=2)


def tool_run_shell(args):
    cmd = args.get("cmd", "")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("cmd is required")
    found = METACHARS.intersection(cmd)
    if found:
        raise ValueError(f"shell metacharacters are not allowed: {''.join(sorted(found))}")
    argv = shlex.split(cmd)
    if not argv:
        raise ValueError("empty command")
    binary = Path(argv[0]).name
    if binary not in ALLOWED_SHELL:
        raise ValueError(f"binary not allowed: {binary}")
    try:
        proc = subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"command timed out after {SHELL_TIMEOUT}s")
    out = proc.stdout or ""
    if proc.stderr:
        out += ("\n[stderr]\n" + proc.stderr)
    if len(out) > MAX_SHELL_OUTPUT:
        out = out[:MAX_SHELL_OUTPUT] + "\n[truncated]"
    return json.dumps({"cmd": cmd, "returncode": proc.returncode, "output": out.strip() or "(empty)"}, indent=2)


def tool_memory_write(args):
    title = str(args.get("title", "")).strip()
    body = str(args.get("body", ""))
    tags = str(args.get("tags", ""))
    if not title:
        raise ValueError("title is required")
    if not body:
        raise ValueError("body is required")
    if len(body.encode("utf-8")) > MAX_WRITE:
        raise ValueError("body too large")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    nid = uuid.uuid4().hex
    conn = db()
    conn.execute(
        "INSERT INTO notes(id,title,body,tags,created_at,updated_at) VALUES (?,?,?,?,?,?)",
        (nid, title, body, tags, now, now),
    )
    conn.execute(
        "INSERT INTO notes_fts(id,title,body,tags) VALUES (?,?,?,?)",
        (nid, title, body, tags),
    )
    conn.commit()
    conn.close()
    return json.dumps({"id": nid, "title": title, "tags": tags}, indent=2)


def tool_memory_search(args):
    query = str(args.get("query", "")).strip()
    limit = max(1, min(int(args.get("limit", 5)), 50))
    if not query:
        raise ValueError("query is required")
    match = " ".join('"' + t.replace('"', "") + '"' for t in query.split())
    conn = db()
    rows = conn.execute(
        """
        SELECT n.id, n.title, n.body, n.tags, n.created_at
        FROM notes_fts f JOIN notes n ON n.id = f.id
        WHERE notes_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (match, limit),
    ).fetchall()
    conn.close()
    results = []
    for r in rows:
        body = r["body"]
        results.append({
            "id": r["id"],
            "title": r["title"],
            "tags": r["tags"],
            "created_at": r["created_at"],
            "body": body[:1200] + ("..." if len(body) > 1200 else ""),
        })
    return json.dumps({"query": query, "results": results}, indent=2)


def tool_memory_read(args):
    nid = str(args.get("id", "")).strip()
    if not nid:
        raise ValueError("id is required")
    conn = db()
    r = conn.execute("SELECT * FROM notes WHERE id = ?", (nid,)).fetchone()
    conn.close()
    if not r:
        raise ValueError(f"note not found: {nid}")
    return json.dumps({
        "id": r["id"],
        "title": r["title"],
        "body": r["body"],
        "tags": r["tags"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }, indent=2)


def tool_memory_list(args):
    limit = max(1, min(int(args.get("limit", 20)), 100))
    tag = str(args.get("tag", "")).strip()
    conn = db()
    if tag:
        rows = conn.execute(
            "SELECT id,title,tags,created_at FROM notes WHERE tags LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"%{tag}%", limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id,title,tags,created_at FROM notes ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return json.dumps({"notes": [dict(r) for r in rows]}, indent=2)


def tool_memory_delete(args):
    nid = str(args.get("id", "")).strip()
    if not nid:
        raise ValueError("id is required")
    conn = db()
    conn.execute("DELETE FROM notes WHERE id = ?", (nid,))
    conn.execute("DELETE FROM notes_fts WHERE id = ?", (nid,))
    conn.commit()
    conn.close()
    return json.dumps({"deleted": nid}, indent=2)


TOOLS = [
    {
        "name": "list_files",
        "description": "List a directory under the user's local ucai root.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory relative to the root. Default '.'."}},
        },
    },
    {
        "name": "read_file",
        "description": "Read a UTF-8 text file under the ucai root for inspection.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path relative to the root."}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file under the ucai root. Use files/ for artifacts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "append_file",
        "description": "Append text to a file under the ucai root, creating it if needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace text in an existing file under the ucai root.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "all": {"type": "boolean", "description": "Replace every occurrence. Default false."},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "glob",
        "description": "Find files and directories under the ucai root by glob pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "Relative glob such as '**/*.md'."}},
            "required": ["pattern"],
        },
    },
    {
        "name": "grep",
        "description": "Regex search text files under the ucai root. Returns path:line:text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "File or directory relative to the root. Default '.'."},
                "max_results": {"type": "integer", "description": "Default 200."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a read-only shell command (ls, cat, head, tail, grep, wc, file, stat, du, df, date, pwd, which, uname, sw_vers, mdls). No pipes or redirection.",
        "inputSchema": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
    {
        "name": "memory_write",
        "description": "Save a durable note to the user's local memory (SQLite FTS5).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "tags": {"type": "string", "description": "Comma-separated tags."},
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "memory_search",
        "description": "Full-text search the user's durable memory. Use before tasks that may depend on prior context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "Default 5."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_read",
        "description": "Read a memory note by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "memory_list",
        "description": "List recent memory notes, optionally filtered by tag.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Default 20."},
                "tag": {"type": "string"},
            },
        },
    },
    {
        "name": "memory_delete",
        "description": "Delete a memory note by id.",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
]

HANDLERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "append_file": tool_append_file,
    "edit_file": tool_edit_file,
    "glob": tool_glob,
    "grep": tool_grep,
    "run_shell": tool_run_shell,
    "memory_write": tool_memory_write,
    "memory_search": tool_memory_search,
    "memory_read": tool_memory_read,
    "memory_list": tool_memory_list,
    "memory_delete": tool_memory_delete,
}


def call_tool(name, args):
    handler = HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"unknown tool: {name}")
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    result = handler(args)
    audit(name, "ok", args.get("path") or args.get("query") or args.get("cmd") or "")
    return result


def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def reply_error(mid, code, message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}) + "\n")
    sys.stdout.flush()


def main():
    init_db()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            reply(mid, {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ucai", "version": "1.0"},
            })
        elif method == "notifications/initialized":
            continue
        elif method == "ping":
            reply(mid, {})
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            try:
                text = call_tool(name, args)
                reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:
                audit(name, "error", str(e))
                reply(mid, {"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True})
        elif isinstance(method, str) and method.startswith("notifications/"):
            continue
        elif mid is not None:
            reply_error(mid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    main()
