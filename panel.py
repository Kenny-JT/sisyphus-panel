"""
Sisyphus Panel - 旁路渲染面板

监听 messages/ 文件夹中的 markdown 文件，通过 SSE (Server-Sent Events)
实时推送给浏览器面板。浏览器端用 KaTeX 渲染 LaTeX、Prism 高亮代码、
Mermaid 渲染图表。

启动:    python panel.py
访问:    http://localhost:7878
推送消息: 写 .md 文件到 ./messages/  (或 POST /push  body=纯文本或 {"content","slug"})
清空:    前端 Clear 按钮  (或 POST /clear)

仅依赖 Python 3.8+ 标准库，无需 pip install。
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import threading
import time
import webbrowser
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import unquote

ROOT = Path(__file__).parent.resolve()
INDEX_FILE = ROOT / "index.html"
MESSAGES_DIR = ROOT / "messages"
# Trash placement contract: dot-prefixed subdir of MESSAGES_DIR so the
# non-recursive Path.glob("*.md") in list_messages()/watcher_loop ignores it.
TRASH_DIR = MESSAGES_DIR / ".trash"
MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
TRASH_DIR.mkdir(parents=True, exist_ok=True)

PORT = 7878
HOST = "127.0.0.1"
POLL_INTERVAL_SEC = 0.8
TRASH_RETENTION_DAYS = 10
TRASH_GC_INTERVAL_SEC = 3600

_clients_lock = threading.Lock()
_clients: list[Queue] = []
_known_file_mtimes: dict[str, float] = {}


def _read_message(path: Path) -> dict | None:
    try:
        return {
            "id": path.stem,
            "filename": path.name,
            "mtime": path.stat().st_mtime,
            "content": path.read_text(encoding="utf-8"),
        }
    except Exception as exc:
        # File may be mid-write when watcher reads it; next poll will catch it.
        print(f"[panel] read error {path.name}: {exc}", file=sys.stderr)
        return None


def list_messages() -> list[dict]:
    out = []
    for f in sorted(MESSAGES_DIR.glob("*.md")):
        msg = _read_message(f)
        if msg:
            out.append(msg)
    return out


def _safe_trash_target(filename: str) -> Path:
    target = TRASH_DIR / filename
    if not target.exists():
        return target
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    return TRASH_DIR / f"{stem}.deleted-{int(time.time() * 1000)}{suffix}"


def _move_to_trash(src: Path) -> Path | None:
    try:
        target = _safe_trash_target(src.name)
        src.replace(target)
        os.utime(target, None)
        return target
    except Exception as exc:
        print(f"[panel] trash move failed for {src.name}: {exc}", file=sys.stderr)
        return None


def gc_trash() -> int:
    if not TRASH_DIR.exists():
        return 0
    cutoff = time.time() - TRASH_RETENTION_DAYS * 86400
    deleted = 0
    for f in TRASH_DIR.iterdir():
        if not f.is_file():
            continue
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except Exception as exc:
            print(f"[panel] trash gc skipped {f.name}: {exc}", file=sys.stderr)
    if deleted:
        print(f"[panel] trash gc: removed {deleted} expired file(s)")
    return deleted


def broadcast(msg: dict, event: str = "message") -> None:
    payload = f"event: {event}\ndata: {json.dumps(msg, ensure_ascii=False)}\n\n"
    with _clients_lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            _clients.remove(q)


def watcher_loop() -> None:
    """Poll messages/ every POLL_INTERVAL_SEC; broadcast adds/edits/deletes via SSE.

    Files present at startup are seeded into _known_file_mtimes so they are NOT
    re-pushed as 'new' events on every restart. Browsers recover historical
    messages through GET /messages, called both on initial page load and after
    every SSE (re)connect (see index.html:connectSSE).
    """
    global _known_file_mtimes
    _known_file_mtimes = {f.name: f.stat().st_mtime for f in MESSAGES_DIR.glob("*.md")}
    last_gc = time.time()

    while True:
        time.sleep(POLL_INTERVAL_SEC)
        try:
            current = {f.name: f.stat().st_mtime for f in MESSAGES_DIR.glob("*.md")}
        except Exception as exc:
            print(f"[panel] watcher scan error: {exc}", file=sys.stderr)
            continue

        for name, mtime in current.items():
            if name not in _known_file_mtimes or _known_file_mtimes[name] != mtime:
                msg = _read_message(MESSAGES_DIR / name)
                if msg:
                    broadcast(msg, event="message")
                    print(f"[panel] -> push {name}")

        for name in list(_known_file_mtimes.keys()):
            if name not in current:
                broadcast({"id": Path(name).stem, "filename": name}, event="delete")
                print(f"[panel] -> delete {name}")

        _known_file_mtimes = current

        if time.time() - last_gc > TRASH_GC_INTERVAL_SEC:
            gc_trash()
            last_gc = time.time()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "SisyphusPanel/1.0"
    SILENT_PATHS = ("/events", "/messages", "/health")

    def log_message(self, fmt: str, *args) -> None:
        if any(p in self.path for p in self.SILENT_PATHS):
            return
        sys.stderr.write(f"[panel] {self.address_string()} - {fmt % args}\n")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/events":
            return self._serve_sse()
        if path == "/messages":
            return self._serve_messages()
        if path == "/health":
            return self._serve_json({"status": "ok", "messages_dir": str(MESSAGES_DIR)})
        self.send_error(404, "not found")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/push":
            return self._handle_push()
        if path == "/clear":
            return self._handle_clear()
        self.send_error(404, "not found")

    def do_DELETE(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/messages/"):
            return self._handle_delete_one(unquote(path[len("/messages/"):]))
        self.send_error(404, "not found")

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_json(self, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(200, "application/json; charset=utf-8", body)

    def _serve_index(self) -> None:
        if not INDEX_FILE.exists():
            self.send_error(500, "index.html missing next to panel.py")
            return
        body = INDEX_FILE.read_bytes()
        self._send_bytes(200, "text/html; charset=utf-8", body)

    def _serve_messages(self) -> None:
        self._serve_json(list_messages())

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q: Queue = Queue()
        with _clients_lock:
            _clients.append(q)

        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15)
                    self.wfile.write(payload.encode("utf-8"))
                    self.wfile.flush()
                except Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)

    def _handle_push(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""

        content = raw
        slug = "msg"
        if raw.lstrip().startswith("{"):
            try:
                payload = json.loads(raw)
                content = payload.get("content", "")
                slug = payload.get("slug", slug)
            except Exception:
                pass

        safe_slug = "".join(c for c in slug if c.isalnum() or c in "-_")[:40] or "msg"
        # Append milliseconds so two pushes within the same second don't clobber each other.
        ms = int((time.time() % 1) * 1000)
        ts = f"{time.strftime('%Y%m%d-%H%M%S')}-{ms:03d}"
        filename = f"{ts}-{safe_slug}.md"
        (MESSAGES_DIR / filename).write_text(content, encoding="utf-8")
        self._serve_json({"status": "ok", "filename": filename})

    def _handle_delete_one(self, filename: str) -> None:
        # Path-traversal guard: resolved target must sit directly inside MESSAGES_DIR,
        # blocking attacks like DELETE /messages/..%2F..%2Fetc%2Fpasswd.
        target = MESSAGES_DIR / filename
        try:
            if target.resolve().parent != MESSAGES_DIR.resolve():
                self.send_error(400, "invalid filename")
                return
        except (OSError, ValueError):
            self.send_error(400, "invalid filename")
            return
        if not target.exists():
            self.send_error(404, "no such message")
            return
        moved = _move_to_trash(target)
        if moved is None:
            self.send_error(500, "trash move failed")
            return
        self._serve_json({"status": "ok", "deleted": filename, "trashed_as": moved.name})

    def _handle_clear(self) -> None:
        moved = 0
        for f in MESSAGES_DIR.glob("*.md"):
            if _move_to_trash(f) is not None:
                moved += 1
        self._serve_json({"status": "ok", "deleted": moved})


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    if not INDEX_FILE.exists():
        print(f"[panel] ERROR: index.html not found at {INDEX_FILE}", file=sys.stderr)
        return 1

    gc_trash()

    threading.Thread(target=watcher_loop, daemon=True).start()

    url = f"http://{HOST}:{PORT}"
    print("=" * 60)
    print(f"  Sisyphus Panel running at  {url}")
    print(f"  Messages folder:           {MESSAGES_DIR}")
    print(f"  Drop *.md files there or POST to {url}/push")
    print(f"  Press Ctrl+C to stop")
    print("=" * 60)

    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    server = ThreadedServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[panel] shutting down...")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
