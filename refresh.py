#!/usr/bin/env python3
"""Publish human titles onto Luvus AGENTS resume rows.

Prefers structured session names (Pi/OMP `session_info.name` first), then a
short title derived from the session. Never logs or forwards the module token.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Iterator

MAX_TITLES = 256
MAX_TITLE_BYTES = 240
MAX_PER_AGENT = 24
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 256 * 1024
SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,256}$")
ALIAS_TITLE = re.compile(r"^=\S+$")
PLACEHOLDER_TITLE = re.compile(
    r"^(new session\b|untitled\b|session\b|$)",
    re.IGNORECASE,
)
CODEX_ENV = re.compile(
    r"<environment_context>.*?</environment_context>",
    re.IGNORECASE | re.DOTALL,
)
WRAPPER_XML = re.compile(r"<[^>]+>.*?</[^>]+>", re.DOTALL)
JUNK_DERIVED = re.compile(
    r"^(#\s*AGENTS\.md\b|#\s*Response annotations\b|<heartbeat>|<recommended_plugins>"
    r"|<external_codex|<INSTRUCTIONS>|Message Type:|Work in /)",
    re.IGNORECASE,
)


def home() -> Path:
    return Path.home()


def env_path(*names: str) -> Path | None:
    for name in names:
        raw = os.environ.get(name)
        if raw:
            return Path(raw)
    return None


def decode_lines(blob: bytes) -> list[str]:
    text = blob.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line.strip()]


def read_head_tail(path: Path) -> tuple[list[str], list[str]]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            head = handle.read(HEAD_BYTES)
            if size <= HEAD_BYTES:
                return decode_lines(head), []
            handle.seek(max(0, size - TAIL_BYTES))
            tail = handle.read(TAIL_BYTES)
        return decode_lines(head), decode_lines(tail)
    except OSError:
        return [], []


def parse_json_line(line: str) -> dict | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def iter_json_objects(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        obj = parse_json_line(line)
        if obj is not None:
            yield obj


def first_string(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return None


def text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
            continue
        if not isinstance(item, dict):
            continue
        piece = item.get("text") or item.get("input_text")
        if isinstance(piece, str):
            chunks.append(piece)
    return "\n".join(chunks)


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def truncate_bytes(text: str, limit: int = MAX_TITLE_BYTES) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    clipped = encoded[:limit]
    while clipped:
        try:
            text = clipped.decode("utf-8")
            break
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    else:
        return ""
    text = text.rstrip()
    if " " in text and len(text) > 24:
        text = text.rsplit(" ", 1)[0].rstrip()
    return text.rstrip(" ,;:.-")


def is_alias_title(title: str) -> bool:
    return bool(ALIAS_TITLE.fullmatch(title.strip()))


def usable_derived(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = collapse_ws(
        re.sub(r"<[^>]+>", " ", WRAPPER_XML.sub(" ", CODEX_ENV.sub(" ", text)))
    )
    if not cleaned or JUNK_DERIVED.match(cleaned):
        return None
    if not re.search(r"[\w\u0080-\uffff]", cleaned):
        return None
    return cleaned


def clean_title(raw: str | None, *, derived: bool = False) -> str | None:
    if not raw:
        return None
    text = collapse_ws(CODEX_ENV.sub(" ", raw))
    if derived:
        text = usable_derived(text) or ""
    if not text or is_alias_title(text):
        return None
    if PLACEHOLDER_TITLE.match(text):
        return None
    return truncate_bytes(text) or None


def prefer(structured: str | None, derived: str | None) -> str | None:
    return clean_title(structured) or clean_title(derived, derived=True)


def emit(agent: str, session_id: str, title: str | None) -> dict | None:
    if not title:
        return None
    if not SAFE_SESSION_ID.fullmatch(session_id):
        return None
    cleaned = clean_title(title)
    if not cleaned:
        return None
    return {"agent": agent, "session_id": session_id, "title": cleaned}


def newest_unique(
    rows: Iterable[tuple[float, object]],
    key_of: Callable[[object], object],
    limit: int = MAX_PER_AGENT,
) -> list[object]:
    ordered = sorted(rows, key=lambda item: item[0], reverse=True)
    out: list[object] = []
    seen: set[object] = set()
    for _, payload in ordered:
        key = key_of(payload)
        if key in seen:
            continue
        seen.add(key)
        out.append(payload)
        if len(out) >= limit:
            break
    return out


def collect_jsonl_files(base: Path, *, depth: int = 2) -> list[tuple[float, Path]]:
    found: list[tuple[float, Path]] = []
    if not base.is_dir():
        return found

    def walk(directory: Path, remaining: int) -> None:
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file() and entry.suffix == ".jsonl":
                    if "/run-" in str(entry):
                        continue
                    found.append((entry.stat().st_mtime, entry))
                elif remaining > 0 and entry.is_dir():
                    walk(entry, remaining - 1)
            except OSError:
                continue

    walk(base, depth)
    return found


def pi_user_text(obj: dict) -> str | None:
    if obj.get("type") != "message":
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    text = collapse_ws(text_from_content(message.get("content")))
    return text or None


def collect_pi_like(agent: str, base: Path) -> list[dict]:
    files = collect_jsonl_files(base, depth=2)
    parsed: list[tuple[float, tuple[str, str, str | None, str | None]]] = []
    for mtime, path in files:
        head, tail = read_head_tail(path)
        session_id = None
        cwd = None
        structured = None
        derived = None
        for obj in iter_json_objects(head + tail):
            if session_id is None:
                candidate = obj.get("id")
                candidate_cwd = obj.get("cwd")
                if isinstance(candidate, str) and isinstance(candidate_cwd, str):
                    session_id = candidate
                    cwd = candidate_cwd
            if obj.get("type") == "session_info" and "name" in obj:
                name = obj.get("name")
                structured = name if isinstance(name, str) else ""
            if derived is None:
                derived = usable_derived(pi_user_text(obj))
        if not session_id or not cwd:
            # Filename fallback: 2026-09-12T11-49-13-894Z_<id>.jsonl
            stem = path.stem
            if "_" in stem:
                session_id = stem.split("_", 1)[1]
                cwd = str(path.parent)
            else:
                continue
        parsed.append((mtime, (cwd, session_id, structured, derived)))

    titles: list[dict] = []
    for cwd, session_id, structured, derived in newest_unique(
        parsed, key_of=lambda row: row[0]
    ):
        item = emit(agent, session_id, prefer(structured, derived))
        if item:
            titles.append(item)
    return titles


def pi_sessions() -> list[dict]:
    base = env_path("PI_CODING_AGENT_SESSION_DIR") or (
        home() / ".pi" / "agent" / "sessions"
    )
    return collect_pi_like("pi", base)


def omp_sessions() -> list[dict]:
    override = env_path("PI_CODING_AGENT_SESSION_DIR")
    if override:
        return collect_pi_like("omp", override)
    xdg = env_path("XDG_DATA_HOME")
    if xdg:
        root = xdg / "omp"
        profile = os.environ.get("OMP_PROFILE") or os.environ.get("PI_PROFILE")
        if profile and profile not in {"", "default"}:
            root = root / "profiles" / profile
        candidate = root / "sessions"
        if candidate.is_dir():
            return collect_pi_like("omp", candidate)
    config = os.environ.get("PI_CONFIG_DIR") or ".omp"
    profile = os.environ.get("OMP_PROFILE") or os.environ.get("PI_PROFILE")
    if profile and profile not in {"", "default"}:
        base = home() / config / "profiles" / profile / "agent" / "sessions"
    else:
        agent_dir = env_path("PI_CODING_AGENT_DIR") or (home() / config / "agent")
        base = agent_dir / "sessions"
    return collect_pi_like("omp", base)


def grok_sessions() -> list[dict]:
    base = env_path("GROK_HOME") or (home() / ".grok")
    root = base / "sessions"
    if not root.is_dir():
        return []
    rows: list[tuple[float, tuple[str, Path]]] = []
    try:
        cwd_dirs = list(root.iterdir())
    except OSError:
        return []
    for cwd_dir in cwd_dirs:
        try:
            if not cwd_dir.is_dir():
                continue
            best: tuple[float, Path] | None = None
            for session_dir in cwd_dir.iterdir():
                if not session_dir.is_dir() or session_dir.name == "subagents":
                    continue
                summary = session_dir / "summary.json"
                try:
                    mtime = summary.stat().st_mtime if summary.is_file() else session_dir.stat().st_mtime
                except OSError:
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, session_dir)
            if best:
                rows.append((best[0], (cwd_dir.name, best[1])))
        except OSError:
            continue
    titles: list[dict] = []
    for _, session_dir in newest_unique(rows, key_of=lambda row: row[0]):
        session_id = session_dir.name
        structured = None
        derived = None
        summary_path = session_dir / "summary.json"
        if summary_path.is_file():
            try:
                data = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            if isinstance(data, dict):
                structured = first_string(
                    data.get("session_summary"),
                    data.get("title"),
                    data.get("name"),
                )
                info = data.get("info")
                if isinstance(info, dict):
                    session_id = first_string(info.get("id"), session_id) or session_id
        history = session_dir.parent / "prompt_history.jsonl"
        if history.is_file() and not structured:
            try:
                with history.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        obj = parse_json_line(line)
                        if not obj:
                            continue
                        if obj.get("session_id") == session_id:
                            derived = first_string(obj.get("prompt"))
                            if derived:
                                break
            except OSError:
                pass
        item = emit("grok", session_id, prefer(structured, derived))
        if item:
            titles.append(item)
    return titles


def opencode_sessions() -> list[dict]:
    candidates = []
    xdg = env_path("XDG_DATA_HOME")
    if xdg:
        candidates.append(xdg / "opencode" / "opencode.db")
    candidates.extend(
        [
            home() / ".local" / "share" / "opencode" / "opencode.db",
            home() / ".opencode" / "opencode.db",
        ]
    )
    db_path = next((path for path in candidates if path.is_file()), None)
    if db_path is None:
        return collect_opencode_files()
    titles: list[dict] = []
    try:
        uri = db_path.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.4) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, directory, title, time_updated
                FROM session
                WHERE id IS NOT NULL
                ORDER BY time_updated DESC
                LIMIT 80
                """
            ).fetchall()
    except sqlite3.Error:
        return collect_opencode_files()
    seen: set[str] = set()
    for row in rows:
        directory = row["directory"] or ""
        if directory in seen:
            continue
        seen.add(directory)
        item = emit("opencode", row["id"], prefer(row["title"], None))
        if item:
            titles.append(item)
        if len(titles) >= MAX_PER_AGENT:
            break
    return titles


def collect_opencode_files() -> list[dict]:
    bases = []
    xdg = env_path("XDG_DATA_HOME")
    if xdg:
        bases.append(xdg / "opencode" / "storage")
    bases.extend(
        [
            home() / ".local" / "share" / "opencode" / "storage",
            home() / ".opencode" / "storage",
        ]
    )
    files: list[tuple[float, Path]] = []
    for base in bases:
        for sub in ("session", "session-metadata"):
            root = base / sub
            if not root.is_dir():
                continue
            try:
                projects = list(root.iterdir())
            except OSError:
                continue
            for project in projects:
                try:
                    if not project.is_dir():
                        continue
                    for file in project.iterdir():
                        if file.suffix == ".json" and file.is_file():
                            files.append((file.stat().st_mtime, file))
                except OSError:
                    continue
    titles: list[dict] = []
    parsed: list[tuple[float, tuple[str, str, str | None]]] = []
    for mtime, path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = first_string(data.get("id"))
        directory = first_string(data.get("directory"), data.get("cwd"))
        if not session_id or not directory:
            continue
        parsed.append((mtime, (directory, session_id, first_string(data.get("title"), data.get("name")))))
    for directory, session_id, title in newest_unique(parsed, key_of=lambda row: row[0]):
        item = emit("opencode", session_id, prefer(title, None))
        if item:
            titles.append(item)
    return titles


def claude_sessions() -> list[dict]:
    base = env_path("CLAUDE_CONFIG_DIR") or (home() / ".claude")
    projects = base / "projects"
    if not projects.is_dir():
        return []
    files: list[tuple[float, Path]] = []
    try:
        for project in projects.iterdir():
            if not project.is_dir():
                continue
            try:
                for file in project.iterdir():
                    if file.suffix == ".jsonl" and file.is_file():
                        files.append((file.stat().st_mtime, file))
            except OSError:
                continue
    except OSError:
        return []
    parsed: list[tuple[float, tuple[str, str, str | None, str | None]]] = []
    for mtime, path in files:
        session_id = path.stem
        structured = None
        derived = None
        head, tail = read_head_tail(path)
        cwd = None
        for obj in iter_json_objects(head + tail):
            if cwd is None:
                cwd = first_string(obj.get("cwd"))
            structured = first_string(
                structured,
                obj.get("customTitle"),
                obj.get("title"),
                obj.get("summary") if obj.get("type") == "summary" else None,
            )
            if derived is None and obj.get("type") in {"user", "message"}:
                message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                if isinstance(message, dict) and message.get("role") in {None, "user"}:
                    derived = usable_derived(
                        collapse_ws(text_from_content(message.get("content")))
                    )
        parsed.append((mtime, (cwd or str(path.parent), session_id, structured, derived)))
    titles: list[dict] = []
    for cwd, session_id, structured, derived in newest_unique(
        parsed, key_of=lambda row: row[0]
    ):
        item = emit("claude", session_id, prefer(structured, derived))
        if item:
            titles.append(item)
    return titles


def codex_user_text(obj: dict) -> str | None:
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
    if not isinstance(payload, dict):
        return None
    if payload.get("type") not in {None, "message"} and obj.get("type") not in {
        "response_item",
        "event_msg",
        "message",
    }:
        return None
    role = payload.get("role")
    if role not in {None, "user"}:
        return None
    text = collapse_ws(CODEX_ENV.sub(" ", text_from_content(payload.get("content"))))
    return text or None


def collect_named_jsonl(
    agent: str,
    files: list[tuple[float, Path]],
    read_id_cwd: Callable[[dict, Path], tuple[str | None, str | None]],
    read_structured: Callable[[dict], str | None],
    read_derived: Callable[[dict], str | None],
) -> list[dict]:
    parsed: list[tuple[float, tuple[str, str, str | None, str | None]]] = []
    for mtime, path in files:
        head, tail = read_head_tail(path)
        session_id = None
        cwd = None
        structured = None
        derived = None
        for obj in iter_json_objects(head + tail):
            found_id, found_cwd = read_id_cwd(obj, path)
            if session_id is None and found_id:
                session_id = found_id
            if cwd is None and found_cwd:
                cwd = found_cwd
            structured = first_string(structured, read_structured(obj))
            if derived is None:
                derived = usable_derived(read_derived(obj))
        if not session_id:
            continue
        parsed.append((mtime, (cwd or str(path.parent), session_id, structured, derived)))
    titles: list[dict] = []
    for cwd, session_id, structured, derived in newest_unique(
        parsed, key_of=lambda row: row[0]
    ):
        item = emit(agent, session_id, prefer(structured, derived))
        if item:
            titles.append(item)
    return titles


def walk_files(root: Path, match: Callable[[Path], bool], depth: int = 4) -> list[tuple[float, Path]]:
    found: list[tuple[float, Path]] = []
    if not root.is_dir():
        return found

    def walk(directory: Path, remaining: int) -> None:
        try:
            entries = list(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file() and match(entry):
                    found.append((entry.stat().st_mtime, entry))
                elif remaining > 0 and entry.is_dir():
                    walk(entry, remaining - 1)
            except OSError:
                continue

    walk(root, depth)
    return found


def codex_sessions() -> list[dict]:
    base = env_path("CODEX_HOME") or (home() / ".codex")

    def match(path: Path) -> bool:
        name = path.name
        return name.startswith("rollout-") and name.endswith(".jsonl")

    def read_id_cwd(obj: dict, path: Path) -> tuple[str | None, str | None]:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        if not isinstance(payload, dict):
            return None, None
        session_id = first_string(
            payload.get("id"),
            payload.get("session_id"),
            payload.get("conversation_id"),
        )
        cwd = first_string(payload.get("cwd"), payload.get("workdir"))
        if session_id is None:
            name = path.name
            if name.startswith("rollout-") and name.endswith(".jsonl"):
                # rollout-<time>-<uuid>.jsonl
                parts = name[:-6].split("-")
                if len(parts) >= 5:
                    session_id = "-".join(parts[-5:])
        return session_id, cwd

    def read_structured(obj: dict) -> str | None:
        if obj.get("type") not in {"session_meta", "session_meta_item"}:
            return None
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        if not isinstance(payload, dict):
            return None
        return first_string(
            payload.get("title"),
            payload.get("session_title"),
            payload.get("custom_title"),
        )

    return collect_named_jsonl(
        "codex",
        walk_files(base / "sessions", match, depth=4),
        read_id_cwd,
        read_structured,
        codex_user_text,
    )


def chat_store_sessions(agent: str, base: Path) -> list[dict]:
    tmp = base / "tmp"
    if not tmp.is_dir():
        return []
    rows: list[tuple[float, tuple[str, Path]]] = []
    try:
        projects = list(tmp.iterdir())
    except OSError:
        return []
    for project in projects:
        chats = project / "chats"
        root = project / ".project_root"
        if not chats.is_dir():
            continue
        try:
            cwd = root.read_text(encoding="utf-8").strip() if root.is_file() else str(project)
        except OSError:
            cwd = str(project)
        best: tuple[float, Path] | None = None
        try:
            for file in chats.iterdir():
                name = file.name
                if not name.startswith("session-"):
                    continue
                if file.suffix not in {".json", ".jsonl"}:
                    continue
                try:
                    mtime = file.stat().st_mtime
                except OSError:
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, file)
        except OSError:
            continue
        if best:
            rows.append((best[0], (cwd, best[1])))
    titles: list[dict] = []
    for cwd, path in newest_unique(rows, key_of=lambda row: row[0]):
        head, tail = read_head_tail(path)
        session_id = None
        structured = None
        derived = None
        for obj in iter_json_objects(head + tail):
            session_id = first_string(session_id, obj.get("sessionId"), obj.get("id"))
            structured = first_string(
                structured,
                obj.get("title"),
                obj.get("name"),
                obj.get("customTitle"),
            )
            if derived is None:
                message = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                if isinstance(message, dict) and message.get("role") in {None, "user"}:
                    derived = usable_derived(
                        collapse_ws(text_from_content(message.get("content")))
                    )
        if not session_id:
            continue
        item = emit(agent, session_id, prefer(structured, derived))
        if item:
            titles.append(item)
    return titles


def gemini_sessions() -> list[dict]:
    base = env_path("GEMINI_CLI_HOME") or (home() / ".gemini")
    return chat_store_sessions("gemini", base)


def qwen_sessions() -> list[dict]:
    base = env_path("QWEN_CODE_HOME") or (home() / ".qwen")
    return chat_store_sessions("qwen", base)


def fx_sessions() -> list[dict]:
    base = env_path("FX_HOME") or (home() / ".fx")
    root = base / "sessions"
    if not root.is_dir():
        return []
    rows: list[tuple[float, Path]] = []
    try:
        for session_dir in root.iterdir():
            path = session_dir / "session.json"
            if path.is_file():
                try:
                    rows.append((path.stat().st_mtime, path))
                except OSError:
                    continue
    except OSError:
        return []
    titles: list[dict] = []
    parsed: list[tuple[float, tuple[str, str, str | None]]] = []
    for mtime, path in rows:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        session_id = first_string(data.get("id"))
        cwd = first_string(data.get("workspace_root"), data.get("origin_workspace_root"))
        if not session_id or not cwd:
            continue
        parsed.append(
            (
                mtime,
                (
                    cwd,
                    session_id,
                    first_string(data.get("title"), data.get("name"), data.get("summary")),
                ),
            )
        )
    for cwd, session_id, title in newest_unique(parsed, key_of=lambda row: row[0]):
        item = emit("fx", session_id, prefer(title, None))
        if item:
            titles.append(item)
    return titles


def kimi_sessions() -> list[dict]:
    base = env_path("KIMI_CODE_HOME") or (home() / ".kimi-code")
    index = base / "session_index.jsonl"
    if not index.is_file():
        return []
    rows: list[tuple[int, dict]] = []
    try:
        with index.open(encoding="utf-8", errors="replace") as handle:
            for i, line in enumerate(handle):
                obj = parse_json_line(line)
                if obj:
                    rows.append((i, obj))
    except OSError:
        return []
    # Index is oldest-first; Luvus reverses it.
    titles: list[dict] = []
    seen: set[str] = set()
    for _, obj in reversed(rows):
        session_id = first_string(obj.get("sessionId"), obj.get("id"))
        work = first_string(obj.get("workDir"), obj.get("cwd"))
        if not session_id or not work or work in seen:
            continue
        seen.add(work)
        structured = first_string(obj.get("title"), obj.get("name"), obj.get("summary"))
        item = emit("kimi", session_id, prefer(structured, None))
        if item:
            titles.append(item)
        if len(titles) >= MAX_PER_AGENT:
            break
    return titles


def muse_sessions() -> list[dict]:
    xdg = env_path("XDG_DATA_HOME") or (home() / ".local" / "share")
    base = xdg / "muse" / "sessions"
    files = walk_files(base, lambda path: path.name == "session.jsonl", depth=4)
    parsed: list[tuple[float, tuple[str, str, str | None, str | None]]] = []
    for mtime, path in files:
        head, _tail = read_head_tail(path)
        session_id = None
        cwd = None
        structured = None
        derived = None
        count = 0
        for obj in iter_json_objects(head):
            count += 1
            if count > 64:
                break
            payload_type = obj.get("payload_type")
            record = obj.get("payload", {})
            if isinstance(record, dict):
                record = record.get("record", record)
            if not isinstance(record, dict):
                continue
            if payload_type == "session.opened.observed":
                session_id = first_string(session_id, record.get("session_id"))
            if payload_type in {"runtime.session.metadata", "runtime.session.route_facts"}:
                cwd = first_string(cwd, record.get("workspace_root"), record.get("cwd"))
                structured = first_string(
                    structured,
                    record.get("title"),
                    record.get("name"),
                    record.get("session_title"),
                )
            if derived is None:
                derived = first_string(record.get("text"), record.get("prompt"))
        if session_id:
            parsed.append((mtime, (cwd or str(path.parent), session_id, structured, derived)))
    titles: list[dict] = []
    for cwd, session_id, structured, derived in newest_unique(
        parsed, key_of=lambda row: row[0]
    ):
        item = emit("muse", session_id, prefer(structured, derived))
        if item:
            titles.append(item)
    return titles


def collect_titles() -> list[dict]:
    collectors = (
        pi_sessions,
        omp_sessions,
        grok_sessions,
        opencode_sessions,
        claude_sessions,
        codex_sessions,
        gemini_sessions,
        qwen_sessions,
        fx_sessions,
        kimi_sessions,
        muse_sessions,
    )
    titles: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for collector in collectors:
        try:
            items = collector()
        except Exception as exc:  # noqa: BLE001 — one agent must not abort the rest
            print(f"{collector.__name__} failed: {exc}", file=sys.stderr)
            continue
        for item in items:
            key = (item["agent"], item["session_id"])
            if key in seen:
                continue
            seen.add(key)
            titles.append(item)
            if len(titles) >= MAX_TITLES:
                return titles
    return titles


def push_titles(titles: list[dict]) -> int:
    if not titles:
        return 0
    luvus = os.environ.get("LUVUS_BIN_PATH") or "luvus"
    payload = json.dumps(titles, ensure_ascii=False, separators=(",", ":"))
    try:
        result = subprocess.run(
            [luvus, "ui", "agent-title", "push", "--titles", payload],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"agent-title push failed: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        if err:
            print(err, file=sys.stderr)
        return result.returncode
    return 0


def main() -> int:
    dump = "--dump" in sys.argv
    titles = collect_titles()
    if dump:
        json.dump(titles, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    if not os.environ.get("LUVUS_BIN_PATH") and not os.environ.get("LUVUS_ENV"):
        # Local invocation without a Luvus session: stay quiet.
        return 0
    return push_titles(titles)


if __name__ == "__main__":
    raise SystemExit(main())
