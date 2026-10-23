#!/usr/bin/env python3
"""Owns the default MPS HTTP port and lets the user pick which running MPS instance receives a request.

Requests such as ``/node?ref=...`` are answered with a page listing the running MPS instances. Choosing
one makes the daemon send the original path+query to that instance; the page stays open and shows the
result in a side panel. Handled links are kept in a SQLite history so they can be opened again later.
"""

from __future__ import annotations

import argparse
import errno
import html
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

HOST = "127.0.0.1"
DEFAULT_PORT = 63320
MPS_MAIN_CLASS = "jetbrains.mps.Launcher"
PROBE_TIMEOUT = 0.3
FORWARD_TIMEOUT = 10.0
DISCOVERY_TTL = 2.0
MAX_BODY = 64 * 1024
SWITCHBOARD_HEADER = "X-MPS-Switchboard"
ACTIVITY_LIMIT = 20

# Never route loopback probes through an HTTP proxy configured in the environment
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_UNSAFE_TARGET = re.compile(r"[^\x21-\x7e]")


# --- discovery ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Instance:
    pid: int
    port: int
    version: str
    projects: tuple[str, ...]
    config_id: str | None = None


def find_mps_processes(proc_root: Path = Path("/proc")) -> dict[int, list[str]]:
    """Return argv of all MPS processes owned by the current user, keyed by pid.

    Processes started by the native launcher (`bin/mps`, MPS 2026.1+) run the JVM in-process, so their
    argv carries no JVM options; for those the `-D` options the launcher passes on are appended.
    """
    uid = os.getuid()
    result: dict[int, list[str]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [arg.decode(errors="replace") for arg in raw.split(b"\0") if arg]
        if MPS_MAIN_CLASS in argv:
            result[int(entry.name)] = argv
        elif (launcher_args := native_launcher_args(entry)) is not None:
            result[int(entry.name)] = argv + launcher_args
    return result


def native_launcher_args(pid_dir: Path) -> list[str] | None:
    """JVM `-D` options of a process running an MPS native launcher, None for any other process.

    Mirrors what the launcher reads: `<BASE>_VM_OPTIONS` and `<BASE>_PROPERTIES` from the environment,
    the paths selector from `product-info.json` of the installation.
    """
    try:
        exe = Path(os.readlink(pid_dir / "exe"))
    except OSError:
        return None
    install = exe.parent.parent
    try:
        info = json.loads((install / "product-info.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("productCode") != "MPS":
        return None
    launchers = {install / launch["launcherPath"] for launch in info.get("launch", [])
                 if isinstance(launch, dict) and isinstance(launch.get("launcherPath"), str)}
    if exe not in launchers:
        return None
    try:
        raw = (pid_dir / "environ").read_bytes()
    except OSError:
        raw = b""
    environ = dict(var.decode(errors="replace").split("=", 1) for var in raw.split(b"\0") if b"=" in var)
    base = info.get("envVarBaseName") or "MPS"
    args = []
    if vm_options := environ.get(f"{base}_VM_OPTIONS"):
        try:
            lines = Path(vm_options).read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        args.extend(line.strip() for line in lines if line.strip().startswith("-D"))
    if properties := environ.get(f"{base}_PROPERTIES"):
        args.append(f"-Didea.properties.file={properties}")
    if selector := info.get("dataDirectoryName"):
        args.append(f"-Didea.paths.selector={selector}")
    return args


def parse_proc_net_tcp(text: str) -> dict[int, int]:
    """Map socket inode -> local port for all LISTEN sockets in /proc/net/tcp{,6} content."""
    result: dict[int, int] = {}
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10 or parts[3] != "0A":
            continue
        result[int(parts[9])] = int(parts[1].rsplit(":", 1)[1], 16)
    return result


def listening_ports(pid: int, proc_root: Path = Path("/proc")) -> list[int]:
    pid_dir = proc_root / str(pid)
    inodes: set[int] = set()
    try:
        fds = os.listdir(pid_dir / "fd")
    except OSError:
        return []
    for fd in fds:
        try:
            link = os.readlink(pid_dir / "fd" / fd)
        except OSError:
            continue
        if match := re.fullmatch(r"socket:\[(\d+)\]", link):
            inodes.add(int(match.group(1)))
    listening: dict[int, int] = {}
    for name in ("tcp", "tcp6"):
        try:
            listening.update(parse_proc_net_tcp((pid_dir / "net" / name).read_text()))
        except OSError:
            continue
    return sorted({port for inode, port in listening.items() if inode in inodes})


def parse_server_header(value: str) -> str | None:
    """`JetBrains MPS 2025.1.4` -> `MPS 2025.1.4`; None for anything that is not MPS."""
    match = re.fullmatch(r"JetBrains MPS\s+(\S+)", value.strip())
    return f"MPS {match.group(1)}" if match else None


def probe_mps(port: int, timeout: float = PROBE_TIMEOUT) -> str | None:
    """Return the MPS version if the port serves the MPS built-in server, else None.

    Uses the `Server` header of `/` (a plain 404): the `/api/*` REST services are rate limited
    and answer 429 when polled.
    """
    try:
        with _OPENER.open(f"http://{HOST}:{port}/", timeout=timeout) as resp:
            server = resp.headers.get("Server", "")
    except urllib.error.HTTPError as exc:
        server = exc.headers.get("Server", "")
        exc.close()
    except (OSError, http.client.HTTPException):
        return None
    return parse_server_header(server)


def parse_properties(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        if match := re.match(r"([^=:\s]+)\s*[=:]\s*(.*)", line):
            props[match.group(1)] = match.group(2)
    return props


def _expand_ide_path(value: str, home: Path) -> Path:
    return Path(value.replace("${user.home}", str(home)).replace("$USER_HOME$", str(home))).expanduser()


def resolve_config_dir(argv: list[str], home: Path) -> Path | None:
    """Locate the IDE config dir the same way the IntelliJ platform does."""
    jvm_props = dict(arg[2:].split("=", 1) for arg in argv if arg.startswith("-D") and "=" in arg)
    if value := jvm_props.get("idea.config.path"):
        return _expand_ide_path(value, home)
    if prop_file := jvm_props.get("idea.properties.file"):
        try:
            file_props = parse_properties(_expand_ide_path(prop_file, home).read_text(errors="replace"))
        except OSError:
            file_props = {}
        if value := file_props.get("idea.config.path"):
            return _expand_ide_path(value, home)
    if selector := jvm_props.get("idea.paths.selector"):
        return home / ".config" / "JetBrains" / selector
    return None


def mpsconfig_id(config_dir: Path | None) -> str | None:
    """Name of the `.mpsconfig/<id>/...` folder the config dir lives in, if any."""
    parts = config_dir.parts if config_dir else ()
    if ".mpsconfig" in parts:
        index = parts.index(".mpsconfig")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _project_from_title(title: str) -> str:
    """Frame titles look like `<project> – <open file>`."""
    return title.split(" – ", 1)[0].strip()


def parse_recent_projects(text: str) -> list[str]:
    """Names of projects marked as currently opened in recentProjects.xml."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    names = []
    for entry in root.iter("entry"):
        meta = entry.find("value/RecentProjectMetaInfo")
        if meta is None or meta.get("opened") != "true":
            continue
        key = entry.get("key", "")
        names.append(_project_from_title(meta.get("frameTitle") or "") or PurePosixPath(key).name or key)
    return names


def open_projects(config_dir: Path) -> list[str]:
    try:
        return parse_recent_projects((config_dir / "options" / "recentProjects.xml").read_text(errors="replace"))
    except OSError:
        return []


def parse_wmctrl_windows(text: str) -> list[tuple[str, int, str]]:
    """(window id, pid, title) for each line of `wmctrl -lp` output."""
    windows = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[2].isdigit():
            windows.append((parts[0], int(parts[2]), parts[4]))
    return windows


def parse_wmctrl(text: str) -> dict[int, list[str]]:
    """Map pid -> project names from `wmctrl -lp` output (title format: `<project> – <file>`)."""
    result: dict[int, list[str]] = {}
    for _, pid, title in parse_wmctrl_windows(text):
        if project := _project_from_title(title):
            result.setdefault(pid, []).append(project)
    return result


def _run(cmd: list[str], timeout: float = 2) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def window_titles() -> dict[int, list[str]]:
    result = _run(["wmctrl", "-lp"], timeout=1)
    return parse_wmctrl(result.stdout) if result else {}


def _sway_socket() -> str | None:
    if sock := os.environ.get("SWAYSOCK"):
        return sock
    # Services started outside the sway session don't inherit SWAYSOCK
    uid = os.getuid()
    sockets = []
    for path in Path(f"/run/user/{uid}").glob(f"sway-ipc.{uid}.*.sock"):
        try:
            sockets.append((path.stat().st_mtime, str(path)))
        except OSError:
            continue
    return max(sockets)[1] if sockets else None


def raise_window(pid: int, project: str) -> tuple[bool, str]:
    """Focus the window of an MPS process, preferring the one showing `project`."""
    if sock := _sway_socket():
        title = re.escape(project).replace('"', '\\"')
        for criteria in (f'[pid={pid} title="^{title}"]', f"[pid={pid}]"):
            result = _run(["swaymsg", "-s", sock, f"{criteria} focus"])
            if result and result.returncode == 0:
                return True, "window focused (sway)"
    listing = _run(["wmctrl", "-lp"])
    if listing and listing.returncode == 0:
        windows = [w for w in parse_wmctrl_windows(listing.stdout) if w[1] == pid]
        windows.sort(key=lambda w: _project_from_title(w[2]) != project)
        if windows:
            result = _run(["wmctrl", "-i", "-a", windows[0][0]])
            if result and result.returncode == 0:
                return True, "window activated (wmctrl)"
    return False, "could not focus the window (needs sway, or wmctrl with DISPLAY set)"


# pid -> (port, version) of already identified instances; only touched by discover_instances,
# which the server calls under Discovery's lock
_identified: dict[int, tuple[int, str]] = {}


def discover_instances(exclude_ports: frozenset[int] = frozenset(), proc_root: Path = Path("/proc")) -> list[Instance]:
    processes = find_mps_processes(proc_root)
    for pid in set(_identified) - set(processes):
        del _identified[pid]
    if not processes:
        return []
    chosen: dict[int, tuple[int, str]] = {}
    candidates = []
    for pid in processes:
        ports = [port for port in listening_ports(pid, proc_root) if port not in exclude_ports]
        known = _identified.get(pid)
        if known and known[0] in ports:
            chosen[pid] = known
        else:
            candidates.extend((pid, port) for port in ports)
    if candidates:
        with ThreadPoolExecutor(max_workers=min(32, len(candidates))) as pool:
            versions = list(pool.map(lambda c: probe_mps(c[1]), candidates))
        # Lowest MPS-answering port per process is the forward target
        for (pid, port), version in zip(candidates, versions):
            if version is not None and (pid not in chosen or port < chosen[pid][0]):
                chosen[pid] = (port, version)
    _identified.update(chosen)
    titles = window_titles() if chosen else {}
    home = Path.home()
    instances = []
    for pid, (port, version) in chosen.items():
        config_dir = resolve_config_dir(processes[pid], home)
        projects = (open_projects(config_dir) if config_dir else []) or titles.get(pid) or [f"pid {pid}"]
        instances.append(Instance(pid, port, version, tuple(dict.fromkeys(projects)), mpsconfig_id(config_dir)))
    return sorted(instances, key=lambda i: i.port)


class Discovery:
    """Discovery results cached briefly, so bursts of requests don't rescan /proc and reprobe ports."""

    def __init__(
        self,
        exclude_ports: frozenset[int] = frozenset(),
        ttl: float = DISCOVERY_TTL,
        discover: Callable[[frozenset[int]], list[Instance]] = discover_instances,
    ) -> None:
        self._exclude = exclude_ports
        self._ttl = ttl
        self._discover = discover
        self._lock = threading.Lock()
        self._cached: list[Instance] | None = None
        self._at = 0.0

    def instances(self, fresh: bool = False) -> list[Instance]:
        with self._lock:
            now = time.monotonic()
            if fresh or self._cached is None or now - self._at > self._ttl:
                self._cached = self._discover(self._exclude)
                self._at = now
            return self._cached


# --- history -----------------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    target       TEXT PRIMARY KEY,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0,
    last_port    INTEGER,
    last_pid     INTEGER,
    last_project TEXT,
    last_version TEXT,
    forwards     INTEGER NOT NULL DEFAULT 0
)
"""

# Results of forwarded links, shared by all open pages; `data` is the JSON sent to the page
_ACTIVITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    at   TEXT NOT NULL,
    data TEXT NOT NULL
)
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class History:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(_SCHEMA)
            con.execute(_ACTIVITY_SCHEMA)
            # Databases created before last_version / forwards existed
            columns = {row["name"] for row in con.execute("PRAGMA table_info(links)")}
            if "last_version" not in columns:
                con.execute("ALTER TABLE links ADD COLUMN last_version TEXT")
            if "forwards" not in columns:
                con.execute("ALTER TABLE links ADD COLUMN forwards INTEGER NOT NULL DEFAULT 0")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self._path, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._connect() as con:
            return con.execute(sql, params).fetchall()

    def record_request(self, target: str) -> None:
        """The link was opened on the switchboard (counted as a hit)."""
        now = _utc_now()
        self._execute(
            "INSERT INTO links (target, first_seen, last_seen, hits) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(target) DO UPDATE SET last_seen = excluded.last_seen, hits = hits + 1",
            (target, now, now),
        )

    def record_forward(self, target: str, instance: Instance, project: str) -> None:
        """The link is being sent to `instance` (counted as a forward)."""
        now = _utc_now()
        self._execute(
            "INSERT INTO links (target, first_seen, last_seen, hits, forwards, last_port, last_pid, last_project, "
            "last_version) VALUES (?, ?, ?, 0, 1, ?, ?, ?, ?) "
            "ON CONFLICT(target) DO UPDATE SET last_seen = excluded.last_seen, forwards = forwards + 1, "
            "last_port = excluded.last_port, last_pid = excluded.last_pid, last_project = excluded.last_project, "
            "last_version = excluded.last_version",
            (target, now, now, instance.port, instance.pid, project, instance.version),
        )

    def get(self, target: str) -> sqlite3.Row | None:
        rows = self._execute("SELECT * FROM links WHERE target = ?", (target,))
        return rows[0] if rows else None

    def entries(self) -> list[sqlite3.Row]:
        return self._execute("SELECT * FROM links ORDER BY last_seen DESC")

    def delete(self, target: str) -> None:
        self._execute("DELETE FROM links WHERE target = ?", (target,))

    def clear(self) -> None:
        self._execute("DELETE FROM links")

    def add_activity(self, data: dict) -> dict:
        """Store a forward result, keep the newest ACTIVITY_LIMIT and return it with `id` and `at`."""
        at = _utc_now()
        with self._connect() as con:
            cur = con.execute("INSERT INTO activity (at, data) VALUES (?, ?)", (at, json.dumps(data)))
            con.execute(
                "DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT ?)",
                (ACTIVITY_LIMIT,),
            )
        return {**data, "id": cur.lastrowid, "at": at}

    def activity(self) -> tuple[str, list[dict]]:
        """Revision and entries (newest first).

        The revision changes with every add (ids are never reused) and every delete (count drops),
        so pages can poll it cheaply.
        """
        rows = self._execute("SELECT * FROM activity ORDER BY id DESC")
        revision = f"{rows[0]['id'] if rows else 0}-{len(rows)}"
        return revision, [{**json.loads(r["data"]), "id": r["id"], "at": r["at"]} for r in rows]

    def delete_activity(self, entry_id: int) -> None:
        self._execute("DELETE FROM activity WHERE id = ?", (entry_id,))

    def clear_activity(self) -> None:
        self._execute("DELETE FROM activity")


# --- web app -----------------------------------------------------------------------------------


@dataclass
class Response:
    status: HTTPStatus
    body: bytes = b""
    content_type: str = "text/html; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def normalize_target(raw: str) -> str | None:
    """Return path+query of a request target, or None if it is not a safe origin-relative target."""
    if raw.startswith(("http://", "https://")):
        parts = urlsplit(raw)
        raw = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if not raw.startswith("/") or _UNSAFE_TARGET.search(raw):
        return None
    return raw


def _first(params: Mapping[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    return values[0] if values else None


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _version_number(version: str) -> str:
    """`MPS 2025.1.4` -> `2025.1.4`; the product name is rendered separately."""
    return version.removeprefix("MPS").strip() or version


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


# Inline SVG icons (stroke = currentColor), so the page needs no external resources
_ICONS = {
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "trash": '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
    "copy": '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
}


_REF_NAME = re.compile(r"\(([^()/]+)\)")


def _link_html(display: str) -> str:
    """The link with the first parenthesized name (the model of an MPS node ref) highlighted."""
    match = _REF_NAME.search(display)
    if match is None:
        return _esc(display)
    start, end = match.span(1)
    return f'{_esc(display[:start])}<span class="ref-name">{_esc(display[start:end])}</span>{_esc(display[end:])}'


_SVG_STROKE = 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"'


def _icon(name: str) -> str:
    return f'<svg viewBox="0 0 24 24" width="18" height="18" {_SVG_STROKE}>{_ICONS[name]}</svg>'


def _sort_header(key: str, label: str, title: str = "", numeric: bool = False) -> str:
    """History column header; the page sorts the rows client-side and sets aria-sort."""
    attrs = f' title="{_esc(title)}"' if title else ""
    return (
        f'<th{" class=\"num\"" if numeric else ""} aria-sort="none"{attrs}>'
        f'<button class="sort" data-sort="{key}">{_esc(label)}</button></th>'
    )


def _icon_button(icon: str, label: str, attrs: str = "", danger: bool = False, extra_class: str = "") -> str:
    """Icon-only button; `attrs` go last so `id="..."` stays adjacent to `hidden`/`>`."""
    cls = " ".join(filter(None, ["icon-button", "danger" if danger else "", extra_class]))
    return f'<button class="{cls}" title="{_esc(label)}" aria-label="{_esc(label)}" {attrs}>{_icon(icon)}</button>'


@dataclass(frozen=True)
class MpsReply:
    status: int | None
    # opened | not_found | no_project | sent (other 2xx) | error (non-2xx) | unreachable
    outcome: str
    # MPS's own text, verbatim: reply lines (plugin header dropped) and, for error pages, the stack trace
    lines: tuple[str, ...] = ()
    detail: str = ""
    # Our own text when MPS did not answer at all
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome in ("opened", "sent")


def _html_lines(text: str) -> list[str]:
    parts = re.split(r"<br\s*/?>", text, flags=re.IGNORECASE)
    lines = (html.unescape(re.sub(r"<[^>]+>", "", part)).strip() for part in parts)
    return [line for line in lines if line]


def interpret_mps_reply(status: int, body: bytes) -> MpsReply:
    """Classify a reply of the MPS HTTP-support plugin.

    Node lookups answer 200 whether or not the node exists, so the outcome comes from the text, e.g.
    `<b>MPS - HTTP Support Plugin</b><br>The requested node has been opened in MPS<br><b>Node</b>: ...`.
    """
    if not 200 <= status < 300:
        headline, detail = split_error_page(body)
        return MpsReply(status, "error", (headline,) if headline else (), detail)
    lines = tuple(line for line in _html_lines(body.decode(errors="replace")) if "HTTP Support Plugin" not in line)
    text = " ".join(lines).lower()
    if "has been opened" in text:
        outcome = "opened"
    elif "has not been found" in text:
        outcome = "not_found"
    elif "no project is available" in text:
        outcome = "no_project"
    else:
        outcome = "sent"
    return MpsReply(status, outcome, lines)


def send_to_mps(url: str, timeout: float = FORWARD_TIMEOUT) -> MpsReply:
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            return interpret_mps_reply(resp.status, resp.read(MAX_BODY))
    except urllib.error.HTTPError as exc:
        with exc:
            return interpret_mps_reply(exc.code, exc.read(MAX_BODY))
    except (OSError, http.client.HTTPException) as exc:
        return MpsReply(None, "unreachable", message=f"request failed: {exc}")


def split_error_page(body: bytes) -> tuple[str, str]:
    """Exception line and stack trace of an MPS error page; both sit in the first <p>."""
    text = body.decode(errors="replace")
    if match := re.search(r"<p>(.*?)(?:</p>|$)", text, re.S):
        text = match.group(1)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "", ""
    return lines[0].strip(), "\n".join(lines[1:])


_CSS = """
:root {
  color-scheme: light dark;
  --board: #e9edf2; --panel: #ffffff; --ink: #18212e; --quiet: #5d6b7e; --rule: #d6dce4;
  --line: #1f5fd1; --line-soft: #e4ecfa; --signal: #c98a00;
  --ok: #1e8a57; --err: #c23b2a;
  --sans: "Fira Sans", "Noto Sans", system-ui, sans-serif;
  --narrow: "Fira Sans Condensed", "Fira Sans", system-ui, sans-serif;
  --mono: "Fira Code", "Fira Mono", ui-monospace, monospace;
  --r-s: 4px; --r-m: 8px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --board: #1b2029; --panel: #242a35; --ink: #dce3ec; --quiet: #8e9aab; --rule: #343c4a;
    --line: #6e9bff; --line-soft: #2a3550; --signal: #f2b630;
    --ok: #4cc38a; --err: #f07062;
  }
}
* { box-sizing: border-box; }
/* Everything is sized in rem, so this scales the whole page */
html { font-size: 110%; }
body { margin: 0; background: var(--board); color: var(--ink); font: 1rem/1.5 var(--sans); }
a { color: var(--line); }
code, kbd { font-family: var(--mono); font-size: .88em; }
/* Every button here is a styled control, never the native look */
button { font: inherit; color: inherit; cursor: pointer; border: 0; background: none; }
:focus-visible { outline: 2px solid var(--line); outline-offset: 2px; }
.muted { color: var(--quiet); }
[hidden] { display: none !important; }

/* Page frame */
.top { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem;
       padding: 1rem 2rem; background: var(--panel); border-bottom: 1px solid var(--rule); }
.top h1 { margin: 0; font: 600 1.35rem/1.2 var(--narrow); }
.top h1 a { color: inherit; text-decoration: none; }
.top h1 a:hover { color: var(--line); }
.listen { margin: 0; color: var(--quiet); font-size: .9rem; }
.listen code { color: var(--ink); }
.layout { display: grid; gap: 2rem; padding: 1.5rem 2rem 3rem; }
/* Route: the link (from) flows into an instance (to); history and activity below */
.route { display: grid; grid-template-columns: 3.6rem minmax(0, 1fr); column-gap: 1rem; min-width: 0; }
.route > section { min-width: 0; }
/* Rail: FROM tag, stem along the link, arrowhead, TO tag on one center line */
.rail { display: flex; flex-direction: column; align-items: center; color: var(--rule); }
.stem { flex: 1; width: 2px; min-height: .5rem; background: currentColor; border-radius: 1px; }
.rail .step + .stem { margin-top: .5rem; }
.rail svg { flex: none; margin-top: -2px; }
.rail .head-right { display: none; }
body.armed .rail { color: var(--line); }
/* While a link waits: dashes flow along the stems toward the arrowhead, which pulses in place */
body.armed .stem { background: repeating-linear-gradient(to bottom, currentColor 0 6px, transparent 6px 10px);
                   animation: flow .6s linear infinite; }
body.armed .rail svg { transform-origin: 50% 50%; animation: pulse 1.2s ease-in-out infinite; }
@keyframes flow { to { background-position: 0 10px; } }
@keyframes flow-x { to { background-position: 10px 0; } }
@keyframes pulse { 50% { transform: scale(1.35); } }
.hint { margin: 0; padding: .6rem 0; align-self: center; color: var(--quiet); font-size: .92rem; }
body.armed .hint { color: var(--line); font-weight: 500; }
.hint > span { display: block; }
.hint .armed-only, body.armed .hint .idle-only { display: none; }
body.armed .hint .armed-only { display: block; }
/* History and activity boxes: one fixed height from the start (no growing as entries arrive), then they scroll */
.lower { --panel-h: max(20rem, 70vh); display: grid; grid-template-columns: minmax(0, 1fr) clamp(23rem, 32vw, 40rem);
         gap: 2rem; align-items: start; }
.lower .section-head { min-height: 2.25rem; }
/* Wide content (the history table) scrolls inside its section instead of widening the column */
.lower > * { min-width: 0; }
/* Same height as .section-head, so the tags line up with the headings */
.step { display: grid; place-items: center; width: 100%; height: 1.5rem; margin-block: .25rem; flex: none;
        font-size: .72rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--quiet);
        border: 1px solid var(--rule); border-radius: var(--r-s); }
body.armed .step { color: var(--line); border-color: currentColor; }
body.armed .ticket, body.armed #instances .lines { box-shadow: 0 0 0 2px var(--line); }
.section-head { display: flex; align-items: center; gap: 1rem; min-height: 2rem; margin-bottom: .5rem; }
.section-head h2 { margin: 0; font: 600 1.05rem/1.3 var(--sans); }
.section-head > :last-child:not(h2) { margin-left: auto; }
.text-button { color: var(--line); font-weight: 500; font-size: .9rem;
               padding: .25rem .5rem; border-radius: var(--r-s); }
.text-button:hover { background: var(--line-soft); }
.icon-button { display: inline-grid; place-items: center; width: 2rem; height: 2rem; padding: 0;
               color: var(--quiet); border-radius: var(--r-s); vertical-align: middle; }
.icon-button:hover { background: var(--line-soft); color: var(--line); }
.icon-button.danger:hover { color: var(--err); }
/* Removing needs a second click: the armed button turns red and says so, without moving anything */
.icon-button.confirming, .icon-button.confirming:hover { position: relative; z-index: 3; background: var(--err); color: #fff; }
.icon-button.confirming::after { content: attr(data-confirm); position: absolute; right: calc(100% + .35rem); top: 50%;
  translate: 0 -50%; padding: .15rem .5rem; border-radius: var(--r-s); background: var(--err); color: #fff;
  font: 600 .75rem/1.4 var(--sans); white-space: nowrap; pointer-events: none; }
.empty { margin: 0; padding: 1rem 1.25rem; background: var(--panel); border-radius: var(--r-m); color: var(--quiet); }

/* The link waiting to be sent */
.ticket { padding: .8rem 1.25rem; background: var(--panel); border-radius: var(--r-m);
          border-left: 6px solid var(--rule); }
.ticket:has(#current:not(.muted)) { border-left-color: var(--signal); }
#current { display: block; font-size: 1rem; line-height: 1.5; min-height: 3em; overflow-wrap: anywhere; }
#current.muted { font-family: var(--sans); }
/* Link text: dimmed URL with the model name stressed, same in the From box and the history */
#current:not(.muted), .history td.link code { color: var(--quiet); }
.ref-name { color: var(--ink); font-weight: 600; }

/* Running instances */
.lines { list-style: none; margin: 0; padding: 0; background: var(--panel); border-radius: var(--r-m); overflow: hidden; }
.line + .line { border-top: 1px solid var(--rule); }
button.instance { width: 100%; display: grid; grid-template-columns: 1.8rem 8.5rem minmax(0, 1fr) auto;
                  gap: 1.25rem; align-items: center; padding: .9rem 1.25rem; text-align: left;
                  border-left: 6px solid transparent; }
button.instance:hover, button.instance:focus-visible { background: var(--line-soft); }
button.instance:focus-visible { outline-offset: -2px; }
/* Result of sending to this instance: failed = shake + red flash, succeeded = slide on + green flash */
button.instance.failed { --tone: var(--err); animation: shake .45s ease-in-out, result-flash 1.6s ease-out; }
button.instance.succeeded { --tone: var(--ok); animation: sent .45s ease-out, result-flash 1.6s ease-out; }
@keyframes shake { 15%, 55% { transform: translateX(-7px); } 35%, 75% { transform: translateX(7px); } }
@keyframes sent { 35% { transform: translateX(10px); } }
@keyframes result-flash { 0%, 25% { background: color-mix(in srgb, var(--tone) 26%, var(--panel)); border-left-color: var(--tone); } }
.line.last button.instance { border-left-color: var(--signal); }
kbd { display: inline-grid; place-items: center; width: 1.8rem; height: 1.8rem; color: var(--quiet);
      border: 1px solid var(--rule); border-bottom-width: 3px; border-radius: var(--r-s); font-size: .8rem; }
kbd:empty { visibility: hidden; }
.ver { display: grid; gap: .1rem; }
.ver-label { font-size: .75rem; font-weight: 500; color: var(--quiet); line-height: 1; }
.ver-num { font: 700 2rem/1 var(--narrow); color: var(--line); font-variant-numeric: tabular-nums; }
.ident { display: grid; gap: .15rem; min-width: 0; }
/* Badge on the project line: marking the last used instance must not change the row height */
.ident .title { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 .75rem; }
.project { font-size: 1.05rem; font-weight: 600; overflow-wrap: anywhere; }
.config { font-family: var(--mono); font-size: .8rem; color: var(--quiet); overflow-wrap: anywhere; }
.config.none { font-family: var(--sans); font-style: italic; }
.badge { font-size: .8rem; font-weight: 600; color: var(--signal); }
.where { display: grid; justify-items: end; font-size: .85rem; color: var(--quiet); font-variant-numeric: tabular-nums; }
.where b { color: var(--ink); font-weight: 600; }

/* History */
#filter + .icon-button { margin-left: 0; }
#filter { margin-left: auto; width: min(18rem, 100%); padding: .35rem .7rem; font: inherit; font-size: .9rem; color: inherit;
          background: var(--panel); border: 1px solid var(--rule); border-radius: var(--r-s); }
.table-wrap { overflow-x: auto; background: var(--panel); border-radius: var(--r-m); }
/* History scrolls on its own, so an opened link can be scrolled into view without moving the page */
#history > *, #results { height: var(--panel-h); }
#history .table-wrap { overflow-y: auto; }
#history thead th { position: sticky; top: 0; z-index: 1; background: var(--panel); box-shadow: inset 0 -1px var(--rule); }
.history { width: 100%; border-collapse: collapse; font-size: .92rem; }
.history th { text-align: left; font-size: .82rem; font-weight: 500; color: var(--quiet); padding: .6rem 1rem; white-space: nowrap; }
.history td { padding: .7rem .75rem; vertical-align: top; border-top: 1px solid var(--rule); }
.history th[aria-sort] { padding: .35rem .25rem; }
.history .sort { padding: .25rem .5rem;
  border-radius: var(--r-s); display: inline-flex; align-items: center; gap: .3rem; }
.history .sort:hover { background: var(--line-soft); color: var(--line); }
.history th.num .sort { flex-direction: row-reverse; }
.history .sort::after { content: "\\2195"; opacity: .35; font-size: .9em; }
.history th[aria-sort="ascending"] .sort, .history th[aria-sort="descending"] .sort { color: var(--ink); font-weight: 600; }
.history th[aria-sort="ascending"] .sort::after { content: "\\2191"; opacity: 1; }
.history th[aria-sort="descending"] .sort::after { content: "\\2193"; opacity: 1; }
.history td.link code { overflow-wrap: anywhere; }
.history td.link { min-width: 14rem; }
/* The whole row selects the link ("send to another MPS") */
.history tr.select { cursor: pointer; }
.history tr.select:hover, .history tr.select:focus-visible { background: var(--line-soft); }
.history tr.select:focus-visible { outline: 2px solid var(--line); outline-offset: -2px; }
.history tr.select:hover .ref-name { color: var(--line); }
/* Row of the current link (amber = selected link) */
.history tr.current { background: color-mix(in srgb, var(--signal) 10%, transparent); }
.history tr.current td:first-child { box-shadow: inset 4px 0 var(--signal); }
.history tr.spotlight { animation: spotlight 1.8s ease-out; }
@keyframes spotlight { 0%, 35% { background: color-mix(in srgb, var(--signal) 35%, transparent); } }
.history .num { text-align: right; font-variant-numeric: tabular-nums; }
.history td.when { white-space: nowrap; color: var(--quiet); font-variant-numeric: tabular-nums; }
.history td.actions { white-space: nowrap; }
.history .actions-row { display: flex; align-items: center; justify-content: flex-end; gap: .25rem; }
.history .actions-row .text-button { font-size: .85rem; padding: .1rem .5rem; }
/* Row hover already uses --line-soft, so buttons in a row need a stronger hover */
.history .actions-row .text-button:hover, .history .actions-row .text-button:focus-visible { background: var(--line); color: var(--panel); }
.history .actions-row .icon-button:hover { background: var(--panel); }
/* Version tag: neutral label styling so it does not read as a link */
.ver-chip { display: inline-block; padding: 0 .4rem; margin-right: .45rem; font: 600 .9rem/1.45 var(--narrow);
            color: var(--ink); background: var(--board); border: 1px solid var(--rule); border-radius: var(--r-s);
            font-variant-numeric: tabular-nums; vertical-align: .05em; }
.port-note { display: block; font-size: .8rem; color: var(--quiet); }
.gone .ver-chip, .gone .project-name { color: var(--quiet); text-decoration: line-through; }

/* Activity log */
#results { list-style: none; margin: 0; padding: .6rem .6rem 0; overflow-y: auto;
           background: color-mix(in srgb, var(--panel) 40%, transparent); border-radius: var(--r-m); }
/* minmax(0, 1fr): long unbreakable stack trace lines scroll inside <pre> instead of widening the entry */
#results li { display: grid; grid-template-columns: minmax(0, 1fr); gap: .3rem; padding: .7rem .9rem .8rem; margin-bottom: .6rem;
              font-size: .88rem; background: var(--panel); border-radius: var(--r-m); border-left: 4px solid var(--tone, var(--rule)); }
#results li.placeholder { color: var(--quiet); animation: fade-in .3s ease-out; }
#results li.arrived { animation: arrived 1.6s ease-out; }
@keyframes fade-in { from { opacity: 0; } }
@keyframes arrived { from { background: var(--line-soft); } }
#results li.ok { --tone: var(--ok); }
#results li.err { --tone: var(--err); }
#results li.pending { --tone: var(--line); }
#results .status { display: flex; align-items: baseline; gap: .6rem; }
#results .status b { font-weight: 600; padding: .05rem .55rem; border-radius: 999px;
                     color: var(--tone); background: color-mix(in srgb, var(--tone) 16%, transparent); }
#results .http { color: var(--quiet); font-size: .8rem; }
#results time { color: var(--quiet); font-variant-numeric: tabular-nums; }
#results .remove-result { margin-left: auto; width: 1.6rem; height: 1.6rem; }
#results .who { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 .4rem; }
#results .who .project { font-size: .92rem; }
#results .who .config { flex-basis: 100%; }
#results a code, #results > li > code { overflow-wrap: anywhere; }
#results .msg { color: var(--quiet); overflow-wrap: anywhere; }
#results .mps-text { display: grid; grid-template-columns: minmax(0, 1fr); gap: .1rem; overflow-wrap: anywhere; }
#results .mps-line b { font-weight: 600; }
#results details summary { cursor: pointer; color: var(--quiet); font-size: .8rem; }
#results details pre { margin: .3rem 0 0; max-height: 16rem; overflow: auto; padding: .5rem .6rem; font: .72rem/1.45 var(--mono);
                       background: var(--board); border-radius: var(--r-s); white-space: pre; }
#results li.err .msg { color: var(--ink); }

/* Lost daemon connection */
#connection { position: sticky; top: 0; z-index: 5; padding: .7rem 2rem; font-weight: 500;
              background: var(--err); color: #fff; }

/* Content may be stale while disconnected */
body.disconnected .layout { opacity: .5; }

/* Focus confirmation */
#toast { position: fixed; left: 50%; bottom: 1.5rem; translate: -50% 0; padding: .65rem 1.1rem;
         background: var(--ink); color: var(--panel); font-weight: 500; border-radius: var(--r-m);
         border-left: 4px solid var(--ok); box-shadow: 0 6px 24px rgb(0 0 0 / .25); animation: toast-in .18s ease-out; }
#toast.err { border-left-color: var(--err); }
@keyframes toast-in { from { opacity: 0; translate: -50% .5rem; } }

/* Enough room: link and instances side by side, the arrow pointing from left to right */
@media (min-width: 1760px) {
  .route { grid-template-columns: minmax(24rem, 1fr) 12rem minmax(0, 1.8fr); grid-template-rows: auto auto minmax(0, 1fr) auto minmax(0, 1fr);
           column-gap: 1.25rem; align-items: start;
           grid-template-areas: "from . to" "lhead . ihead" "card . inst" "card mid inst" "card hint inst"; }
  .rail-from { grid-area: from; }
  .rail-to { grid-area: to; }
  .rail-from, .rail-to { align-items: flex-start; }
  .rail-from .step, .rail-to .step { width: 3.6rem; }
  .rail-from .stem { display: none; }
  /* Headings and boxes as separate grid items: the arrow shares the link card's row and centers on it */
  .route-link, .route-inst { display: contents; }
  .route-link > .section-head { grid-area: lhead; }
  /* The instance list is usually taller: the equal flexible rows put the arrow at its middle, the card centers on it */
  .route-link > .ticket { grid-area: card; align-self: center; }
  /* The narrower column wraps a typical node URL into 3 lines; reserve them so selecting a link moves nothing */
  #current { min-height: 4.5em; }
  .route-inst > .section-head { grid-area: ihead; }
  .route-inst > #instances { grid-area: inst; min-width: 0; align-self: stretch; }
  .rail-mid { grid-area: mid; flex-direction: row; align-self: center; }
  .rail-mid .stem { width: auto; height: 2px; min-height: 0; min-width: .5rem; }
  .rail-mid .head-down { display: none; }
  .rail-mid .head-right { display: block; margin: 0 0 0 -2px; }
  body.armed .rail-mid .stem { background: repeating-linear-gradient(to right, currentColor 0 6px, transparent 6px 10px);
                               animation-name: flow-x; }
  /* Zero height: the hint hangs below the arrow without sizing the rows, so its text can change freely */
  .hint { grid-area: hint; align-self: start; height: 0; padding: 0; text-align: center; font-size: .85rem; }
  .hint > span { padding-top: .5rem; }
}

@media (max-width: 1210px) {
  .lower { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 704px) {
  .top, .layout { padding-inline: 1rem; }
  .top { flex-wrap: wrap; }
  button.instance { grid-template-columns: 1.8rem minmax(0, 1fr); gap: .3rem 1rem; }
  .ver, .ident, .where { grid-column: 2; }
  .ver { display: flex; align-items: baseline; gap: .4rem; }
  .where { justify-items: start; grid-auto-flow: column; justify-content: start; gap: 1rem; }
}
/* Reduced motion: no movement; the result colors on the instances stay since they carry information */
@media (prefers-reduced-motion: reduce) {
  #toast, body.armed .rail svg, body.armed .stem, body.armed .rail-mid .stem, .history tr.spotlight, #results li.placeholder, #results li.arrived {
    animation: none;
  }
  button.instance.failed, button.instance.succeeded { animation: result-flash 1.6s ease-out; }
}
"""

# Single-page client: forwards via POST /__forward and logs results in the side panel.
# The activity panel mirrors the server's activity log: other windows of this browser are told via a
# BroadcastChannel, any other page picks changes up with the regular poll.
# Keys: 1-9 forward to the n-th instance, arrows move between instances, "/" focuses the history filter.
_JS = """
(() => {
  const $ = id => document.getElementById(id);
  let target = document.body.dataset.target || '';
  const lastHtml = {};
  // Connection state: every request goes through call(), so any failure shows the banner
  let connected = true;
  function setConnection(state) {
    const banner = $('connection');
    if (state === 'ok') {
      if (!connected) {
        connected = true;
        banner.hidden = true;
        document.body.classList.remove('disconnected');
        toast('Reconnected to the switchboard', true);
        refreshHistory();
        refreshActivity();
      }
      return;
    }
    connected = false;
    document.body.classList.add('disconnected');
    banner.textContent = state === 'foreign'
      ? `Port ${location.port} is no longer answered by the switchboard; another program (e.g. an MPS instance) took it. Restart the switchboard, then this page reconnects.`
      : `Lost connection to the switchboard on ${location.host}. Retrying every 2 seconds; sending links does not work until it is back.`;
    banner.hidden = false;
  }
  async function call(url, options) {
    let resp;
    try { resp = await fetch(url, options); }
    catch (e) { setConnection('down'); throw new Error('switchboard not reachable'); }
    if (!resp.headers.get('X-MPS-Switchboard')) { setConnection('foreign'); throw new Error('port answered by another program'); }
    setConnection('ok');
    return resp;
  }
  const post = (url, params) => call(url, { method: 'POST', body: new URLSearchParams(params) });
  const fuzzy = (term, text) => { let i = 0; for (const c of text) if (c === term[i]) i++; return i === term.length; };
  const instanceButtons = () => [...document.querySelectorAll('#instances button.instance')];
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  };

  const renumber = () => instanceButtons().forEach((b, i) => {
    const k = b.parentElement.querySelector('kbd'); if (k) k.textContent = i < 9 ? String(i + 1) : '';
  });
  const applyFilter = () => {
    const filter = $('filter'); if (!filter) return;
    const terms = filter.value.toLowerCase().split(/\\s+/).filter(Boolean);
    document.querySelectorAll('[data-entry]').forEach(e => { e.hidden = !terms.every(t => fuzzy(t, e.dataset.search)); });
  };

  const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
  // (Re)start the CSS animation behind `cls`; the class goes once `animationName` (default: any) has ended
  function play(e, cls, animationName) {
    e.classList.remove(cls);
    void e.offsetWidth;  // restart even if the class was already set
    e.classList.add(cls);
    const done = ev => {
      if (ev.target !== e || (animationName && ev.animationName !== animationName)) return;
      e.classList.remove(cls);
      e.removeEventListener('animationend', done);
    };
    e.addEventListener('animationend', done);
  }

  // History reordering (sort change, refresh): rows slide from their old to their new place (FLIP),
  // keyed by link since a refresh replaces the row elements
  const historyRows = () => [...document.querySelectorAll('#history tr[data-target]')].filter(r => !r.hidden);
  const rowPositions = () => new Map(historyRows().map(r => [r.dataset.target, r.getBoundingClientRect().top]));
  function slideRows(before) {
    if (reducedMotion.matches || !before.size) return;
    for (const row of historyRows()) {
      const old = before.get(row.dataset.target);
      if (old === undefined) {
        row.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 300, easing: 'ease-out' });
        continue;
      }
      const dy = old - row.getBoundingClientRect().top;
      if (Math.abs(dy) < 1) continue;
      row.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }],
                  { duration: 380, easing: 'cubic-bezier(.2, .7, .3, 1)' });
    }
  }

  // History sort: column + direction, shared by all windows of this browser; ties keep the server order (last seen)
  const SORT_TEXT = new Set(['link', 'instance']);
  let sort = { key: 'seen', dir: 'desc' };
  try { sort = { ...sort, ...JSON.parse(localStorage.getItem('historySort') || '{}') }; } catch {}
  function applySort() {
    const body = document.querySelector('#history tbody'); if (!body) return;
    const prop = 'sort' + sort.key[0].toUpperCase() + sort.key.slice(1);
    const text = SORT_TEXT.has(sort.key);
    const sign = sort.dir === 'asc' ? 1 : -1;
    const rows = [...body.rows];
    rows.sort((a, b) => {
      const x = a.dataset[prop] ?? '', y = b.dataset[prop] ?? '';
      // Empty values (e.g. never sent) go last in both directions
      if (!x || !y) return !x - !y;
      return sign * (text ? x.localeCompare(y, undefined, { numeric: true }) : (sort.key === 'seen' ? (x > y) - (x < y) : x - y));
    });
    body.append(...rows);
    document.querySelectorAll('#history th[aria-sort]').forEach(th => {
      th.setAttribute('aria-sort', th.querySelector('.sort').dataset.sort === sort.key
        ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none');
    });
  }
  function resort() {
    const before = rowPositions();
    applySort();
    slideRows(before);
  }
  function setSort(key) {
    // Same column flips; a new one starts with newest / largest / A first
    sort = key === sort.key ? { key, dir: sort.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: SORT_TEXT.has(key) ? 'asc' : 'desc' };
    try { localStorage.setItem('historySort', JSON.stringify(sort)); } catch {}
    resort();
  }
  addEventListener('storage', ev => {
    if (ev.key !== 'historySort') return;
    try { sort = JSON.parse(ev.newValue) || sort; } catch {}
    resort();
  });

  async function refresh(id, url) {
    let html;
    try { const resp = await call(url); if (!resp.ok) return; html = await resp.text(); } catch { return; }
    if (lastHtml[id] === html) return;
    lastHtml[id] = html;
    const before = id === 'history' ? rowPositions() : null;
    const scrolled = document.querySelector(`#${id} .table-wrap`)?.scrollTop;
    $(id).innerHTML = html;
    applyFilter();
    if (before) {
      // The replaced table would otherwise jump back to its top
      const wrap = document.querySelector('#history .table-wrap');
      if (wrap && scrolled) wrap.scrollTop = scrolled;
      applySort();
      markCurrent();
      slideRows(before);
    }
    renumber();
  }
  const refreshInstances = () => refresh('instances', '/__instances?' + new URLSearchParams({ target }));
  const refreshHistory = () => refresh('history', '/__history');

  // History row of the current link: highlighted; `reveal` scrolls the history box to it and pulses it
  function markCurrent(reveal = false) {
    let row = null;
    for (const r of document.querySelectorAll('#history tr[data-target]')) {
      const isCurrent = !!target && r.dataset.target === target;
      r.classList.toggle('current', isCurrent);
      if (isCurrent) row = r;
    }
    if (!row || !reveal) return;
    const wrap = row.closest('.table-wrap');
    const head = wrap.querySelector('thead')?.offsetHeight || 0;
    const box = wrap.getBoundingClientRect();
    const offset = row.getBoundingClientRect().top - box.top + wrap.scrollTop - head;
    // Center within the part of the box that is on screen; the box may extend below the window
    let visible = Math.min(wrap.clientHeight, innerHeight - box.top) - head;
    if (visible < row.offsetHeight * 1.5) {
      wrap.scrollIntoView({ block: 'nearest' });
      visible = wrap.clientHeight - head;
    }
    wrap.scrollTo({ top: offset - Math.max(0, (visible - row.offsetHeight) / 2), behavior: reducedMotion.matches ? 'auto' : 'smooth' });
    play(row, 'spotlight');
  }

  function setTarget(t, display) {
    target = t;
    const current = $('current');
    // The link's history row renders it with the model name stressed (_link_html); reuse that markup
    const rendered = [...document.querySelectorAll('#history tr[data-target]')].find(r => r.dataset.target === t)
      ?.querySelector('td.link code');
    if (rendered) current.replaceChildren(...[...rendered.childNodes].map(n => n.cloneNode(true)));
    else current.textContent = display;
    current.classList.remove('muted');
    $('clear-link').hidden = false;
    document.body.classList.add('armed');
    markCurrent();
    refreshInstances().then(() => instanceButtons()[0]?.focus({ preventScroll: true }));
  }
  function clearTarget() {
    target = '';
    const current = $('current');
    current.textContent = current.dataset.empty;
    current.classList.add('muted');
    $('clear-link').hidden = true;
    document.body.classList.remove('armed');
    markCurrent();
    // Keep a reload from re-recording the link
    if (location.pathname !== '/' || location.search) history.replaceState(null, '', '/');
    return refreshInstances();
  }

  const clock = (date = new Date()) =>
    [date.getHours(), date.getMinutes(), date.getSeconds()].map(n => String(n).padStart(2, '0')).join(':');
  const emptyResults = $('results').innerHTML;

  const removeButton = () => {
    const b = el('button', 'icon-button remove-result');
    b.append($('icon-x').content.firstElementChild.cloneNode(true));
    b.title = 'Remove entry';
    b.setAttribute('aria-label', 'Remove entry');
    return b;
  };
  const OUTCOMES = {
    opened: 'Node opened', not_found: 'Node not found', no_project: 'No project open in MPS',
    sent: 'Sent', error: 'MPS error', unreachable: 'MPS unreachable',
  };
  const versionNumber = v => (v || '').replace(/^MPS\\s*/, '') || v;
  function resetResultsIfEmpty() {
    if (!$('results').children.length) $('results').innerHTML = emptyResults;
  }

  // Entries grow in / collapse out so the others visibly make room
  const collapsed = { height: '0px', paddingTop: '0px', paddingBottom: '0px', marginBottom: '0px', opacity: 0 };
  function animateIn(li, arrived = false) {
    if (reducedMotion.matches) return;
    const height = `${li.offsetHeight}px`;
    li.style.overflow = 'hidden';
    const reset = () => { li.style.overflow = ''; };
    li.animate([{ ...collapsed, transform: 'translateY(-.5rem)' }, { height, opacity: 1, transform: 'none' }],
               { duration: 260, easing: 'cubic-bezier(.2, .7, .3, 1)' })
      .finished.then(reset, reset);
    if (arrived) play(li, 'arrived');
  }
  function animateOut(li, delay = 0) {
    li.dataset.leaving = '';
    const done = () => { li.remove(); resetResultsIfEmpty(); };
    if (reducedMotion.matches) { done(); return; }
    li.style.overflow = 'hidden';
    li.animate([{ height: `${li.offsetHeight}px`, opacity: 1 }, { ...collapsed, transform: 'translateX(2rem)' }],
               { duration: 220, delay, easing: 'cubic-bezier(.5, 0, .75, 0)', fill: 'forwards' })
      .finished.then(done, done);
  }

  // Server entries carry data-id; pending and purely local entries (switchboard unreachable) don't.
  // Updated by id, so only entries that really came or went are animated.
  let activityRev = '';
  function renderActivity(entries, animate = true) {
    const results = $('results');
    const wanted = new Set(entries.map(e => String(e.id)));
    const present = new Set();
    results.querySelectorAll('li[data-id]:not([data-leaving])').forEach(li => {
      if (wanted.has(li.dataset.id)) present.add(li.dataset.id);
      else if (animate) animateOut(li);
      else li.remove();
    });
    const added = entries.filter(e => !present.has(String(e.id)));
    if (added.length) results.querySelector('.placeholder')?.remove();
    for (const data of added) {
      const li = el('li');
      fillResult(li, data);
      // Newest first: in front of the first older entry
      const older = [...results.querySelectorAll('li[data-id]')].find(x => Number(x.dataset.id) < data.id);
      results.insertBefore(li, older || null);
      if (animate) animateIn(li, true);
    }
    resetResultsIfEmpty();
  }
  async function refreshActivity() {
    let data;
    try {
      const resp = await call('/__activity?' + new URLSearchParams({ rev: activityRev }));
      if (!resp.ok) return;
      data = await resp.json();
    } catch { return; }
    if (!data.entries || data.rev === activityRev) return;
    activityRev = data.rev;
    renderActivity(data.entries);
  }
  const channel = 'BroadcastChannel' in window ? new BroadcastChannel('mps-node-url-switchboard') : null;
  if (channel) channel.onmessage = () => { refreshActivity(); refreshHistory(); };
  // The activity log changed on the server: tell the other windows of this browser and catch up here
  function activityChanged() {
    channel?.postMessage('changed');
    refreshActivity();
  }
  {
    const initial = JSON.parse($('activity-data').textContent);
    activityRev = initial.rev;
    if (initial.entries.length) renderActivity(initial.entries, false);
  }

  function addResult(display, label = 'Sending…') {
    const results = $('results');
    results.querySelector('.placeholder')?.remove();
    const li = el('li', 'pending');
    const status = el('div', 'status');
    status.append(el('b', '', label), removeButton());
    li.append(status);
    if (display) li.append(el('code', '', display));
    results.prepend(li);
    animateIn(li);
    return li;
  }

  function fillResult(li, data) {
    li.className = data.ok ? 'ok' : 'err';
    li.replaceChildren();
    if (data.id) li.dataset.id = data.id;
    const status = el('div', 'status');
    const label = OUTCOMES[data.outcome] || (data.ok ? 'Sent' : 'Failed');
    status.append(el('b', '', label), el('time', '', clock(data.at ? new Date(data.at) : undefined)));
    if (data.status) status.append(el('span', 'http', `HTTP ${data.status}`));
    status.append(removeButton());
    li.append(status);
    if (data.version) {
      const who = el('div', 'who');
      who.append(el('span', 'ver-chip', versionNumber(data.version)), el('span', 'project', data.project));
      if (data.config_id) who.append(el('span', 'config', data.config_id));
      li.append(who);
    }
    if (data.mps_lines?.length) appendMpsText(li, data);
    else if (data.message) li.append(el('div', 'msg', data.message));
    if (data.url) {
      const a = el('a');
      a.href = data.url; a.target = '_blank'; a.rel = 'noopener';
      a.append(el('code', '', data.url));
      li.append(a);
    } else if (data.display) {
      li.append(el('code', '', data.display));
    }
  }

  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); }
    catch {
      // Clipboard API unavailable or denied: fall back to a temporary selection
      const area = el('textarea');
      area.value = text;
      area.style.position = 'fixed'; area.style.opacity = '0';
      document.body.append(area);
      area.select();
      const ok = document.execCommand('copy');
      area.remove();
      if (!ok) { toast('Could not copy to the clipboard', false); return; }
    }
    toast(`Copied ${text}`, true);
  }

  // MPS's reply as MPS wrote it: "Key: value" lines with the key in bold (as in MPS's HTML), stack trace folded
  function appendMpsText(li, data) {
    const box = el('div', 'mps-text');
    for (const line of data.mps_lines) {
      const row = el('div', 'mps-line');
      const at = line.indexOf(': ');
      if (at > 0 && at < 40) row.append(el('b', '', line.slice(0, at)), line.slice(at));
      else row.textContent = line;
      box.append(row);
    }
    if (data.mps_detail) {
      const details = el('details');
      details.append(el('summary', '', 'Stack trace'), el('pre', '', data.mps_detail));
      box.append(details);
    }
    li.append(box);
  }

  let toastTimer;
  function toast(text, ok) {
    const t = $('toast');
    t.textContent = text;
    t.className = ok ? 'ok' : 'err';
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, ok ? 2500 : 6000);
  }

  async function raiseInstance(params) {
    let data;
    try { data = await (await post('/__raise', params)).json(); }
    catch (e) { data = { ok: false, message: String(e) }; }
    toast(data.ok ? `Focused ${data.project} (MPS ${versionNumber(data.version)})` : (data.message || 'Could not focus the window'), data.ok);
  }

  // Success / failure animation on the instance the link went to
  function flagResult(port, project, ok) {
    const b = instanceButtons().find(x => x.dataset.port === String(port) && x.dataset.project === project);
    if (!b) return;
    b.classList.remove('succeeded', 'failed');
    play(b, ok ? 'succeeded' : 'failed', 'result-flash');
  }

  async function forward(params, display) {
    const li = addResult(display);
    let data;
    try { data = await (await post('/__forward', params)).json(); }
    catch (e) { data = { ok: false, message: String(e) }; }
    // setTarget/clearTarget refresh the instance list themselves
    let instancesRefreshed;
    if (data.needs_choice) setTarget(data.target, data.display);
    else if (data.ok && params.target === target) instancesRefreshed = clearTarget();
    fillResult(li, data);
    // After the instance list refresh, which may replace the buttons
    (instancesRefreshed || refreshInstances()).then(() => { if (data.port) flagResult(data.port, data.project, data.ok); });
    // A poll may have rendered this entry already (and animated it in)
    if (data.id && $('results').querySelectorAll(`li[data-id="${data.id}"]`).length > 1) li.remove();
    else play(li, 'arrived');
    if (data.id) activityChanged();
    refreshHistory();
  }

  async function removeResult(li) {
    if ('leaving' in li.dataset) return;
    const id = li.dataset.id;
    animateOut(li);
    if (!id) return;
    try { await post('/__activity/delete', { id }); } catch { return; }
    activityChanged();
  }
  async function clearResults() {
    [...$('results').querySelectorAll('li:not(.placeholder):not([data-leaving])')]
      .forEach((li, i) => animateOut(li, Math.min(i, 10) * 35));
    try { await post('/__activity/clear', {}); } catch { return; }
    activityChanged();
  }

  const selectEntry = d => {
    setTarget(d.target, d.display);
    // Only scroll when the link box is out of view
    const box = document.querySelector('.ticket').getBoundingClientRect();
    if (box.top < 0 || box.bottom > innerHeight) window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  // Remove/clear buttons act on the second click within a few seconds
  const CONFIRM = [
    ['.delete', 'Click again to delete'],
    ['#clear, #clear-results', 'Click again to clear all'],
  ];
  let armed = null, armTimer;
  function disarm() {
    clearTimeout(armTimer);
    armed?.classList.remove('confirming');
    armed = null;
  }
  // True when this click should only arm the button
  function needsConfirm(b) {
    const text = CONFIRM.find(([selector]) => b.matches(selector))?.[1];
    if (!text) return false;
    if (armed === b) { disarm(); return false; }
    disarm();
    armed = b;
    b.dataset.confirm = text;
    b.classList.add('confirming');
    armTimer = setTimeout(disarm, 3000);
    return true;
  }

  document.addEventListener('click', ev => {
    const b = ev.target.closest('button');
    if (armed && b !== armed) disarm();
    if (b && needsConfirm(b)) return;
    if (!b) {
      // History row; not when the click ended a text selection (e.g. copying the link)
      const row = ev.target.closest('tr.select');
      if (row && !getSelection().toString()) selectEntry(row.dataset);
      return;
    }
    const d = b.dataset;
    if (b.classList.contains('instance')) {
      if (target) forward({ target, port: d.port, project: d.project }, $('current').textContent);
      else raiseInstance({ port: d.port, project: d.project });
    }
    else if (b.id === 'clear-link') clearTarget();
    else if (b.classList.contains('remove-result')) removeResult(b.closest('li'));
    else if (b.id === 'clear-results') clearResults();
    else if (b.classList.contains('reopen')) forward({ target: d.target, mode: 'reopen' }, d.display);
    else if (b.classList.contains('sort')) setSort(d.sort);
    else if (b.classList.contains('copy')) copyText(d.url);
    else if (b.classList.contains('delete')) post('/__history/delete', { target: d.target }).then(refreshHistory);
    else if (b.id === 'clear') post('/__history/clear', {}).then(refreshHistory);
    else if (b.id === 'refresh') refreshInstances();
  });

  document.addEventListener('input', ev => { if (ev.target.id === 'filter') applyFilter(); });

  document.addEventListener('keydown', ev => {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key === 'Escape' && armed) { disarm(); return; }
    const active = document.activeElement;
    if (active?.id === 'filter') {
      if (ev.key === 'Escape') { active.value = ''; applyFilter(); active.blur(); }
      return;
    }
    if (ev.key === '/') { ev.preventDefault(); $('filter')?.focus(); return; }
    if ((ev.key === 'Enter' || ev.key === ' ') && active?.matches('tr.select')) {
      ev.preventDefault();
      selectEntry(active.dataset);
      return;
    }
    if (/^[1-9]$/.test(ev.key)) {
      const b = instanceButtons()[Number(ev.key) - 1];
      if (b) { ev.preventDefault(); b.click(); }
      return;
    }
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      const list = instanceButtons();
      const i = list.indexOf(active);
      if (!list.length || (i < 0 && active !== document.body)) return;
      ev.preventDefault();
      list[ev.key === 'ArrowDown' ? Math.min(i + 1, list.length - 1) : Math.max(i - 1, 0)].focus();
    }
  });

  // Poll faster while disconnected so the banner clears soon after the daemon is back
  (function poll() {
    setTimeout(async () => {
      if (document.visibilityState === 'visible') {
        await refreshInstances();
        if (connected) await refreshActivity();
      }
      poll();
    }, connected ? 5000 : 2000);
  })();
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') { refreshInstances(); refreshActivity(); }
  });
  applySort();
  renumber();
  if (target) {
    instanceButtons()[0]?.focus({ preventScroll: true });
    // Opened link already in the history: show where
    markCurrent(true);
  }
})();
"""

_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
    "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
)


_NO_LINK = "No link selected. Open a node URL or pick one from the history."
# Down for the stacked route, right for the side-by-side one; CSS shows one of them
_ARROW_HEADS = (
    f'<svg class="head-down" viewBox="0 0 16 10" width="16" height="10" {_SVG_STROKE}><path d="M2 2l6 6 6-6"/></svg>'
    f'<svg class="head-right" viewBox="0 0 10 16" width="10" height="16" {_SVG_STROKE}><path d="M2 2l6 6-6 6"/></svg>'
)


class App:
    def __init__(
        self,
        port: int,
        discovery: Discovery,
        history: History,
        send: Callable[[str], MpsReply] = send_to_mps,
        focus: Callable[[int, str], tuple[bool, str]] = raise_window,
    ) -> None:
        self.port = port
        self.discovery = discovery
        self.history = history
        self._send = send
        self._focus = focus
        self._hosts = {f"{HOST}:{port}", f"localhost:{port}"}
        self._origins = {f"http://{host}" for host in self._hosts}

    def handle(self, method: str, raw_path: str, headers: Mapping[str, str], body: bytes = b"") -> Response:
        # Host check blocks DNS-rebinding pages from reading the history
        if headers.get("Host") not in self._hosts:
            return self._text(HTTPStatus.FORBIDDEN, "unexpected Host header")
        target = normalize_target(raw_path)
        if target is None:
            return self._text(HTTPStatus.BAD_REQUEST, "invalid request target")
        path, _, query = target.partition("?")
        params = parse_qs(query)

        if method == "POST":
            # Forwarding triggers actions in MPS, so only accept requests from our own page
            if not self._same_origin(headers):
                return self._text(HTTPStatus.FORBIDDEN, "cross-origin request rejected")
            form = parse_qs(body.decode(errors="replace"))
            if path == "/__forward":
                return self._forward(form)
            if path == "/__raise":
                return self._raise(form)
            if path == "/__history/delete" and (entry := _first(form, "target")):
                self.history.delete(entry)
                return Response(HTTPStatus.NO_CONTENT)
            if path == "/__history/clear":
                self.history.clear()
                return Response(HTTPStatus.NO_CONTENT)
            if path == "/__activity/delete" and (entry_id := _first(form, "id") or "").isdigit():
                self.history.delete_activity(int(entry_id))
                return Response(HTTPStatus.NO_CONTENT)
            if path == "/__activity/clear":
                self.history.clear_activity()
                return Response(HTTPStatus.NO_CONTENT)
            return self._text(HTTPStatus.NOT_FOUND, "not found")

        if method != "GET":
            return self._text(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
        if path == "/":
            return self._page(None)
        if path == "/__instances":
            fragment_target = normalize_target(_first(params, "target") or "")
            return Response(HTTPStatus.OK, self._instances_html(self.discovery.instances(), fragment_target).encode())
        if path == "/__history":
            return Response(HTTPStatus.OK, self._history_html(self.discovery.instances()).encode())
        if path == "/__activity":
            revision, entries = self.history.activity()
            # Unchanged: skip the entries, the page polls this every few seconds
            if _first(params, "rev") == revision:
                return self._json({"rev": revision})
            return self._json({"rev": revision, "entries": entries})
        if path == "/favicon.ico" or path.startswith("/__"):
            return self._text(HTTPStatus.NOT_FOUND, "not found")
        self.history.record_request(target)
        return self._page(target)

    # -- forwarding --

    def _forward(self, form: Mapping[str, list[str]]) -> Response:
        data, status = self._forward_result(form)
        # Every outcome lands in the activity panel of all open pages
        return self._json(self.history.add_activity(data), status)

    def _forward_result(self, form: Mapping[str, list[str]]) -> tuple[dict, HTTPStatus]:
        target = normalize_target(_first(form, "target") or "")
        if target is None:
            return {"ok": False, "message": "missing or invalid target"}, HTTPStatus.BAD_REQUEST
        display = unquote(target)
        if _first(form, "mode") == "reopen":
            entry = self.history.get(target)
            instance = self._last_instance(entry)
            if instance is None:
                return {
                    "ok": False, "needs_choice": True, "target": target, "display": display,
                    "message": "Last used instance is not running; choose an instance.",
                }, HTTPStatus.OK
            project = entry["last_project"]
        else:
            instance, project = self._posted_instance(form)
            if instance is None:
                return {"ok": False, "display": display, "message": project}, HTTPStatus.BAD_REQUEST
        if project not in instance.projects:
            project = instance.projects[0]
        self.history.record_forward(target, instance, project)
        url = f"http://{HOST}:{instance.port}{target}"
        reply = self._send(url)
        return {
            "ok": reply.ok,
            "outcome": reply.outcome,
            "status": reply.status,
            "url": url,
            "display": display,
            "version": instance.version,
            "project": project,
            "config_id": instance.config_id,
            "message": reply.message,
            "mps_lines": list(reply.lines),
            "mps_detail": reply.detail,
            "port": instance.port,
        }, HTTPStatus.OK

    def _raise(self, form: Mapping[str, list[str]]) -> Response:
        instance, project = self._posted_instance(form)
        if instance is None:
            return self._json({"ok": False, "message": project}, HTTPStatus.BAD_REQUEST)
        ok, message = self._focus(instance.pid, project)
        return self._json({"ok": ok, "version": instance.version, "project": project, "message": message})

    def _posted_instance(self, form: Mapping[str, list[str]]) -> tuple[Instance | None, str]:
        """Instance on the posted port and the posted project on it (its first one if unknown), or None and why."""
        port = _first(form, "port") or ""
        instance = self._instance_on(int(port)) if port.isdigit() else None
        if instance is None:
            return None, f"no running MPS instance on port {port}"
        project = _first(form, "project")
        return instance, project if project in instance.projects else instance.projects[0]

    def _last_instance(self, entry: sqlite3.Row | None) -> Instance | None:
        if entry is None or entry["last_port"] is None:
            return None
        instance = self._instance_on(entry["last_port"])
        return instance if instance is not None and instance.pid == entry["last_pid"] else None

    def _instance_on(self, port: int) -> Instance | None:
        for fresh in (False, True):
            if instance := next((i for i in self.discovery.instances(fresh) if i.port == port), None):
                return instance
        return None

    def _same_origin(self, headers: Mapping[str, str]) -> bool:
        if origin := headers.get("Origin"):
            return origin in self._origins
        referer = headers.get("Referer") or ""
        return any(referer.startswith(f"{origin}/") for origin in self._origins)

    # -- rendering --

    def _page(self, target: str | None) -> Response:
        instances = self.discovery.instances()
        revision, entries = self.history.activity()
        # "<" escaped so entry text can't close the script element
        activity = json.dumps({"rev": revision, "entries": entries}).replace("<", "\\u003c")
        current = (
            f'<code id="current" data-empty="{_NO_LINK}"{"" if target else ' class="muted"'}>'
            f'{_link_html(unquote(target)) if target else _NO_LINK}</code>'
        )
        body = (
            '<header class="top"><h1><a href="/" title="Home">MPS node URL switchboard</a></h1>'
            f'<p class="listen">Listening on <code>{HOST}:{self.port}</code></p></header>'
            '<div id="connection" role="alert" hidden></div>'
            '<main class="layout"><div class="route">'
            '<div class="rail rail-from"><span class="step">From</span><span class="stem"></span></div>'
            '<section class="route-link"><div class="section-head"><h2>Link</h2>'
            + _icon_button("x", "Clear link", 'id="clear-link"' + ("" if target else " hidden")) + "</div>"
            f'<div class="ticket">{current}</div></section>'
            f'<div class="rail rail-mid"><span class="stem"></span>{_ARROW_HEADS}</div>'
            '<p class="hint"><span class="armed-only">Choose Instance</span>'
            '<span class="idle-only">Clicking an instance brings its window to the front</span></p>'
            '<div class="rail rail-to"><span class="step">To</span></div>'
            '<section class="route-inst"><div class="section-head"><h2>MPS instances</h2>'
            + _icon_button("refresh", "Refresh instances", 'id="refresh"') + "</div>"
            f'<div id="instances">{self._instances_html(instances, target)}</div></section></div>'
            '<div class="lower"><section><div class="section-head"><h2>History</h2>'
            '<input id="filter" type="search" placeholder="Filter history (press /)" autocomplete="off">'
            + _icon_button("trash", "Clear history", 'id="clear"', danger=True) + "</div>"
            f'<div id="history">{self._history_html(instances)}</div></section>'
            '<aside class="activity"><div class="section-head"><h2>Activity</h2>'
            + _icon_button("trash", "Clear activity", 'id="clear-results"', danger=True) + "</div>"
            '<ol id="results"><li class="placeholder">Forwarded links appear here.</li></ol></aside></div></main>'
            '<div id="toast" role="status" hidden></div>'
            f'<template id="icon-x">{_icon("x")}</template>'
            f'<script type="application/json" id="activity-data">{activity}</script>'
        )
        page = (
            '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>MPS node URL switchboard</title>'
            f"<style>{_CSS}</style></head>\n"
            f'<body{' class="armed"' if target else ""} data-target="{_esc(target or "")}">{body}<script>{_JS}</script></body></html>\n'
        )
        return Response(HTTPStatus.OK, page.encode())

    def _instances_html(self, instances: list[Instance], target: str | None) -> str:
        if not instances:
            return '<p class="empty">No MPS instance is running. Start one and it appears here within a few seconds.</p>'
        entry = self.history.get(target) if target else None
        last = (entry["last_pid"], entry["last_port"], entry["last_project"]) if entry else None
        items = []
        for inst in instances:
            for project in inst.projects:
                is_last = (inst.pid, inst.port, project) == last
                config = (
                    f'<span class="config">{_esc(inst.config_id)}</span>' if inst.config_id
                    else '<span class="config none">no .mpsconfig</span>'
                )
                items.append(
                    f'<li class="line{" last" if is_last else ""}">'
                    f'<button class="instance" data-port="{inst.port}" data-project="{_esc(project)}">'
                    "<kbd></kbd>"
                    f'<span class="ver"><span class="ver-label">MPS</span>'
                    f'<span class="ver-num">{_esc(_version_number(inst.version))}</span></span>'
                    f'<span class="ident"><span class="title"><span class="project">{_esc(project)}</span>'
                    + ('<span class="badge">Last used for this link</span>' if is_last else "")
                    + f"</span>{config}</span>"
                    f'<span class="where"><span>port <b>{inst.port}</b></span><span>pid {inst.pid}</span></span>'
                    "</button></li>"
                )
        return f'<ol class="lines">{"".join(items)}</ol>'

    def _history_html(self, instances: list[Instance]) -> str:
        entries = self.history.entries()
        if not entries:
            return f'<p class="empty">No links yet. Node URLs opened on port {self.port} are listed here.</p>'
        running = {(i.pid, i.port) for i in instances}
        rows = []
        for entry in entries:
            target = entry["target"]
            display = unquote(target)
            if entry["last_port"] is None:
                last = '<span class="muted">Not sent yet</span>'
            else:
                alive = (entry["last_pid"], entry["last_port"]) in running
                state = "" if alive else ", not running"
                last = (
                    f'<span class="{"" if alive else "gone"}">'
                    f'<span class="ver-chip">{_esc(_version_number(entry["last_version"] or "MPS ?"))}</span>'
                    f'<span class="project-name">{_esc(entry["last_project"])}</span></span>'
                    f'<span class="port-note">port {entry["last_port"]}{state}</span>'
                )
            data = f'data-target="{_esc(target)}" data-display="{_esc(display)}"'
            instance_key = f"{entry['last_version'] or ''} {entry['last_project'] or ''}".strip()
            search = f"{display} {instance_key}".lower()
            sort_keys = (
                f'data-sort-link="{_esc(display)}" data-sort-instance="{_esc(instance_key)}" '
                f'data-sort-seen="{_esc(entry["last_seen"])}" data-sort-hits="{entry["hits"]}" '
                f'data-sort-forwards="{entry["forwards"]}"'
            )
            rows.append(
                f'<tr class="select" tabindex="0" title="Send to another MPS…" {data} {sort_keys} '
                f'data-entry data-search="{_esc(search)}">'
                f'<td class="when">{_esc(_local_time(entry["last_seen"]))}</td>'
                f'<td class="link"><code>{_link_html(display)}</code></td>'
                f'<td class="num">{entry["hits"]}</td>'
                f"<td>{last}</td>"
                f'<td class="num">{entry["forwards"]}</td>'
                '<td class="actions"><div class="actions-row">'
                f'<button class="text-button reopen" {data}>Re-send to same MPS</button>'
                # Shareable: the URL MPS itself would answer on its default port
                + _icon_button("copy", "Copy node URL", f'data-url="{_esc(f"http://{HOST}:{DEFAULT_PORT}{target}")}"',
                               extra_class="copy")
                + _icon_button("trash", "Delete from history", data, danger=True, extra_class="delete")
                + "</div></td></tr>"
            )
        return (
            '<div class="table-wrap"><table class="history"><thead><tr>'
            + _sort_header("seen", "Last seen") + _sort_header("link", "Link")
            + _sort_header("hits", "Hits", "Times the link was opened on the switchboard", numeric=True)
            + _sort_header("instance", "Last instance")
            + _sort_header("forwards", "Forwards", "Times the link was sent to an MPS instance", numeric=True)
            + "<th></th></tr></thead>"
            f'<tbody>{"".join(rows)}</tbody></table></div>'
        )

    @staticmethod
    def _text(status: HTTPStatus, message: str) -> Response:
        return Response(status, f"{message}\n".encode(), "text/plain; charset=utf-8")

    @staticmethod
    def _json(data: dict, status: HTTPStatus = HTTPStatus.OK) -> Response:
        return Response(status, json.dumps(data).encode(), "application/json")


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "mps-node-url-switchboard"
    server: Server

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        self._dispatch("POST", self.rfile.read(length))

    def _dispatch(self, method: str, body: bytes = b"") -> None:
        try:
            resp = self.server.app.handle(method, self.path, self.headers, body)
        except Exception:
            traceback.print_exc()
            resp = App._text(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        # Lets the page tell the switchboard apart from whatever else might take over the port
        self.send_header(SWITCHBOARD_HEADER, "1")
        for name, value in resp.headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(resp.body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    app: App


# --- CLI ---------------------------------------------------------------------------------------


def default_db_path() -> Path:
    return _state_home() / "mps-node-url-switchboard" / "history.db"


def _state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def migrate_legacy_history(db: Path, legacy: Path) -> bool:
    """Move the history kept under the tool's former name (mps-port-handler) to `db`, once."""
    if db.exists() or not legacy.exists():
        return False
    db.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        source = legacy.with_name(legacy.name + suffix)
        if source.exists():
            source.rename(db.with_name(db.name + suffix))
    try:
        legacy.parent.rmdir()
    except OSError:
        pass
    return True


def serve(port: int, db: Path) -> int:
    try:
        server = Server((HOST, port), RequestHandler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f"error: port {port} is already in use (an MPS instance or another switchboard?)", file=sys.stderr)
            return 1
        raise
    # Only after binding: a still running old handler would hold the port and its database
    legacy = _state_home() / "mps-port-handler" / "history.db"
    if db == default_db_path() and migrate_legacy_history(db, legacy):
        print(f"moved history from {legacy} to {db}", file=sys.stderr)
    history = History(db)
    server.app = App(port, Discovery(exclude_ports=frozenset({port})), history)
    print(f"mps-node-url-switchboard listening on http://{HOST}:{port}/ (history: {db})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def print_instances() -> int:
    instances = discover_instances()
    if not instances:
        print("no running MPS instance found", file=sys.stderr)
        return 1
    for inst in instances:
        for project in inst.projects:
            print(f"{inst.version}\t{project}\t{inst.config_id or '-'}\t{inst.port}\t{inst.pid}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", nargs="?", choices=("serve", "discover"), default="serve")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--db", type=Path, default=default_db_path(), help="history database path")
    args = parser.parse_args(argv)
    if args.command == "discover":
        return print_instances()
    return serve(args.port, args.db)


if __name__ == "__main__":
    sys.exit(main())
