"""
Sisyphus Panel - side-channel markdown renderer for AI agents.

Watches markdown files under ./messages/ and pushes them to a browser panel
in real time via SSE (Server-Sent Events). The browser renders LaTeX with
KaTeX, syntax-highlights code with Prism, and renders diagrams with Mermaid.

Run:      python panel.py
Open:     http://localhost:7878
Push:     drop a *.md file into ./messages/  (or POST /push, body = text or {"content","slug"})
Clear:    Clear button in the UI  (or POST /clear -> moves files to .trash/)
History:  History button in the UI (lists .trash/ and lets you restore deleted files)
Reorder:  drag pages in the sidebar or Alt+Up/Down (saved to messages/.order.json; GET/PUT /order)
App:      chrome-app.txt (optional, per machine) opens the page in the panel's installed Chrome app

Pure Python 3.8+ stdlib, no pip install required.
"""

from __future__ import annotations

import http.server
import json
import os
import re
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
# Sidebar page order saved by drag-to-reorder: {"order": [filenames, top first]}.
# Not *.md, so the message globs never pick it up.
ORDER_FILE = MESSAGES_DIR / ".order.json"
# Optional, per machine (git-ignored): one line of Chrome arguments that open the
# panel's installed Chrome app, copied from the app's shortcut, e.g.
# "--profile-directory=Default --app-id=<id>". With it the page opens in that app
# window instead of a browser tab (Windows). start-panel.vbs reads it too.
CHROME_APP_FILE = ROOT / "chrome-app.txt"
MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
TRASH_DIR.mkdir(parents=True, exist_ok=True)

PORT = 7878
HOST = "127.0.0.1"
POLL_INTERVAL_SEC = 0.8
TRASH_RETENTION_DAYS = 10
TRASH_GC_INTERVAL_SEC = 3600

# Suffix appended by _safe_trash_target when two trashed files would collide;
# stripped by _original_name_from_trash on restore so the user never sees it
# in messages/.
DELETED_SUFFIX_RE = re.compile(r"\.deleted-\d+$")
# Suffix used by _safe_restore_target when restoring would collide with an
# existing file in messages/; left visible so the user can see the conflict.
RESTORED_SUFFIX_FMT = ".restored-{ms}"

_clients_lock = threading.Lock()
_clients: list[Queue] = []
_known_file_mtimes: dict[str, float] = {}
# Serializes order reads/writes so the file and the last `order` broadcast agree.
_order_lock = threading.Lock()


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


def list_trash() -> list[dict]:
    out = []
    if not TRASH_DIR.exists():
        return out
    for f in sorted(TRASH_DIR.glob("*.md")):
        msg = _read_message(f)
        if msg:
            # Surface the mtime-relative purge deadline so the UI can show
            # "auto-purges in N days" without re-deriving the policy.
            msg["purge_at"] = msg["mtime"] + TRASH_RETENTION_DAYS * 86400
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


def _original_name_from_trash(name: str) -> str:
    """Strip the internal `.deleted-<ms>` collision marker so a restored file
    lands back in messages/ under its user-visible original name."""
    stem = Path(name).stem
    suffix = Path(name).suffix
    cleaned = DELETED_SUFFIX_RE.sub("", stem)
    return f"{cleaned}{suffix}"


def _safe_restore_target(filename: str) -> Path:
    target = MESSAGES_DIR / filename
    if not target.exists():
        return target
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    marker = RESTORED_SUFFIX_FMT.format(ms=int(time.time() * 1000))
    return MESSAGES_DIR / f"{stem}{marker}{suffix}"


def _restore_from_trash(src: Path) -> Path | None:
    """Move src out of TRASH_DIR back into MESSAGES_DIR. Touches mtime so the
    watcher's next poll detects it as a new file and broadcasts a `message`
    event to all connected browsers."""
    try:
        original_name = _original_name_from_trash(src.name)
        target = _safe_restore_target(original_name)
        src.replace(target)
        os.utime(target, None)
        return target
    except Exception as exc:
        print(f"[panel] restore failed for {src.name}: {exc}", file=sys.stderr)
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


def _is_valid_order(order) -> bool:
    """A list of unique bare *.md filenames (no directory parts)."""
    return (
        isinstance(order, list)
        and all(isinstance(n, str) and n.endswith(".md") and Path(n).name == n for n in order)
        and len(set(order)) == len(order)
    )


def read_order() -> list[str]:
    """Saved sidebar order, top first; [] when nothing valid is saved. It may
    still name pages deleted since it was saved; the UI skips those. Raises
    OSError when the file exists but can't be read."""
    try:
        data = json.loads(ORDER_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except ValueError as exc:
        print(f"[panel] ignoring malformed {ORDER_FILE.name}: {exc}", file=sys.stderr)
        return []
    order = data.get("order") if isinstance(data, dict) else None
    if not _is_valid_order(order):
        print(f"[panel] ignoring malformed {ORDER_FILE.name}", file=sys.stderr)
        return []
    return order


def write_order(order: list[str]) -> None:
    # Write a temp file and rename it over the old one, so a crash mid-write
    # can't leave a truncated order file behind.
    tmp = ORDER_FILE.with_name(ORDER_FILE.name + ".tmp")
    tmp.write_text(json.dumps({"order": order}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ORDER_FILE)


def forget_order_slot(name: str) -> None:
    """Drop `name` from the saved order, so a page restored from the trash
    lands on top like a new push instead of in a slot it held before."""
    with _order_lock:
        try:
            order = read_order()
            if name not in order:
                return
            order.remove(name)
            write_order(order)
        except OSError as exc:
            print(f"[panel] could not update {ORDER_FILE.name}: {exc}", file=sys.stderr)
            return
        broadcast({"order": order}, event="order")


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
    SILENT_PATHS = ("/events", "/messages", "/trash", "/health", "/order")

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
        if path == "/trash":
            return self._serve_trash()
        if path == "/order":
            return self._serve_order()
        if path == "/health":
            return self._serve_json({"status": "ok", "messages_dir": str(MESSAGES_DIR)})
        self.send_error(404, "not found")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/push":
            return self._handle_push()
        if path == "/clear":
            return self._handle_clear()
        if path.startswith("/trash/restore/"):
            return self._handle_restore(unquote(path[len("/trash/restore/"):]))
        self.send_error(404, "not found")

    def do_PUT(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/order":
            return self._handle_put_order()
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

    def _serve_trash(self) -> None:
        self._serve_json(list_trash())

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
            # Two tabs deleting the same page: the other request moved it first.
            if not target.exists():
                self.send_error(404, "no such message")
                return
            self.send_error(500, "trash move failed")
            return
        self._serve_json({"status": "ok", "deleted": filename, "trashed_as": moved.name})

    def _handle_restore(self, filename: str) -> None:
        # Same path-traversal guard as delete, but anchored at TRASH_DIR.
        src = TRASH_DIR / filename
        try:
            if src.resolve().parent != TRASH_DIR.resolve():
                self.send_error(400, "invalid filename")
                return
        except (OSError, ValueError):
            self.send_error(400, "invalid filename")
            return
        if not src.exists():
            self.send_error(404, "no such trash file")
            return
        moved = _restore_from_trash(src)
        if moved is None:
            self.send_error(500, "restore failed")
            return
        forget_order_slot(moved.name)
        # Don't broadcast manually: the watcher's next poll cycle will detect
        # the new file in MESSAGES_DIR and push a `message` event to every
        # connected browser, which keeps the SSE story single-sourced.
        self._serve_json({"status": "ok", "restored": filename, "as": moved.name})

    def _serve_order(self) -> None:
        try:
            with _order_lock:
                order = read_order()
        except OSError as exc:
            # An error, not [], so tabs keep the order they show instead of
            # falling back to newest first (and saving that over it).
            print(f"[panel] reading {ORDER_FILE.name} failed: {exc}", file=sys.stderr)
            self.send_error(500, "could not read the order")
            return
        self._serve_json({"order": order})

    def _handle_put_order(self) -> None:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            order = json.loads(raw.decode("utf-8")).get("order")
        except (ValueError, AttributeError):
            order = None
        if not _is_valid_order(order):
            self.send_error(400, 'expected JSON {"order": [unique *.md filenames]}')
            return
        with _order_lock:
            try:
                write_order(order)
            except OSError as exc:
                print(f"[panel] saving {ORDER_FILE.name} failed: {exc}", file=sys.stderr)
                self.send_error(500, "could not save the order")
                return
            # Broadcast inside the lock: racing PUTs reach every tab in the
            # same order they hit the disk.
            broadcast({"order": order}, event="order")
        print(f"[panel] -> order ({len(order)} pages)")
        self._serve_json({"status": "ok", "order": order})

    def _handle_clear(self) -> None:
        moved = 0
        for f in MESSAGES_DIR.glob("*.md"):
            if _move_to_trash(f) is not None:
                moved += 1
        self._serve_json({"status": "ok", "deleted": moved})


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Pending-connection backlog (socketserver's default is 5): a burst of tabs
    # reconnecting at once shouldn't be refused.
    request_queue_size = 32


def open_page(url: str) -> None:
    """Open the panel: in its installed Chrome app if chrome-app.txt names one, else in a browser tab."""
    app = ""
    if CHROME_APP_FILE.exists():
        with CHROME_APP_FILE.open(encoding="utf-8-sig") as f:
            app = f.readline().strip()
    if app:
        # ShellExecute finds chrome.exe through Chrome's App Paths registration.
        os.startfile("chrome.exe", arguments=app)
    else:
        webbrowser.open(url)


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
        threading.Timer(0.6, open_page, (url,)).start()

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
