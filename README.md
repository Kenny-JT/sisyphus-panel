# Sisyphus Panel

Local side panel for AI agents and CLI tools. Drop a markdown file, see it rendered live in the browser — LaTeX, Mermaid, syntax-highlighted code, the lot.

Use it when terminal-only output is awkward for the content you're producing (formulas, diagrams, multi-block code) but you don't want to leave the terminal as your primary surface.

```
┌──────────────┐    write .md     ┌──────────────┐    SSE push    ┌────────────┐
│  AI / CLI /  │ ───────────────► │  panel.py    │ ─────────────► │  Browser   │
│  any process │                  │  watches dir │                │  (rendered)│
└──────────────┘                  └──────────────┘                └────────────┘
```

## Features

- **Live render** — markdown + LaTeX (KaTeX) + Mermaid + Prism code highlighting
- **Just write a file** — no API, no SDK, no daemon protocol; any tool that can write a `.md` file works
- **Restart-resilient** — messages persist across panel restarts; nothing gets wiped silently
- **Soft delete + history** — deleted messages go to `messages/.trash/`; the **History** button in the toolbar lists them newest-first and one-click restores them back into the panel. Auto-purged after 10 days.
- **Reorder pages** — drag pages up and down in the sidebar, or move one with Alt+↑ / Alt+↓ (the focused row, else the open page). Tab or ↑ ↓ moves between pages, Enter opens one and Delete moves it to the Trash. The order is saved in `messages/.order.json`, so it survives restarts and shows up in every open tab. New and restored pages land at the top; **Sort by newest** (next to the Pages label) drops the manual order.
- **Collapsible cards** — click the triangle to fold long messages; sticky header keeps title + delete button reachable while scrolling
- **Long pages stay responsive** — a page goes in a slice at a time, so a document of several hundred KB shows its first screen at once and can be scrolled while the rest follows
- **Reading progress** — a ring and a percentage in the top bar show how far through the open page you are (100% once the end of the text is in view)
- **Zero install** — Python 3.8+ stdlib only, no `pip install` needed; one HTML file, four CDN libs

## Quick start

```powershell
git clone https://github.com/Kenny-JT/sisyphus-panel.git
cd sisyphus-panel
py -3.12 panel.py
```

(Linux/macOS: `python3 panel.py`. Add `--no-browser` to skip the auto-open.)

The panel opens at <http://localhost:7878>. Hit Ctrl+C to stop. Drop a `.md` file into `messages/` and watch it render.

### Run it in the background (Windows)

Double-click `start-panel.vbs` (or run `wscript start-panel.vbs`; add `--no-browser` to skip the auto-open). The server starts with no console window and no taskbar button, and its output is appended to `panel.log` (once that passes 1 MB, the next start moves it to `panel.log.1`). If the panel is already running, it just opens the page instead of starting a second copy. Stop it with `stop-panel.bat`. `start-panel.bat` still runs it in a visible console if you want to watch the log live.

### Open it as a Chrome app (Windows)

If you installed the panel as a Chrome app (on the panel page: ⋮ → Cast, save, and share → Install page as app), it can open in that app's window instead of a browser tab. Put the arguments from the app's shortcut into a one-line `chrome-app.txt` next to `panel.py`: the shortcut is in `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Chrome Apps`, and you want everything after `chrome_proxy.exe` in its Target, e.g. `--profile-directory=Default --app-id=abcdefghijklmnopabcdefghijklmnop`. `panel.py` then opens the app when it starts, and `start-panel.vbs` opens it when the panel is already running. Delete the file to go back to a browser tab. It is git-ignored, since the arguments belong to your own Chrome profile.

## Push content

### Method A · Write a file (the recommended way)

Any process that can write a UTF-8 file can push to the panel. New pages show up at the top of the sidebar; a timestamp prefix keeps the filenames tidy:

```powershell
"# Hello`n`n$E = mc^2$" | Set-Content "messages/20260509-120000-greeting.md" -Encoding utf8
```

The watcher polls every ~0.8s and pushes new/edited files to all connected browsers via SSE.

### Method B · HTTP POST

```powershell
# Plain text body
Invoke-RestMethod http://localhost:7878/push -Method Post `
  -Body "## Formula`n`n`$`$E = mc^2`$`$"

# JSON body with a slug for the filename suffix
Invoke-RestMethod http://localhost:7878/push -Method Post `
  -ContentType 'application/json' `
  -Body (@{ content = '# Hello'; slug = 'greeting' } | ConvertTo-Json)
```

```bash
# curl
curl -X POST http://localhost:7878/push --data-binary "## Title `$E=mc^2`$"
```

## Supported markdown extensions

| Type           | Syntax                                    |
| -------------- | ----------------------------------------- |
| Inline math    | `$E = mc^2$` or `\(E = mc^2\)`            |
| Display math   | `$$ ... $$` or `\[ ... \]`                |
| Code blocks    | ` ```python ` etc., language auto-loaded  |
| Mermaid        | ` ```mermaid ` flowcharts/sequence/class  |
| Tables, lists, blockquotes, links | standard GFM           |

## HTTP API

| Method | Path                            | Purpose                                                            |
| ------ | ------------------------------- | ------------------------------------------------------------------ |
| GET    | `/`                             | Panel HTML                                                         |
| GET    | `/messages`                     | All current messages as JSON array                                 |
| GET    | `/trash`                        | All deleted messages as JSON array (each item includes `purge_at`) |
| GET    | `/events`                       | SSE stream — `message`, `delete` and `order` events                |
| GET    | `/health`                       | `{"status":"ok","messages_dir":...}`                               |
| GET    | `/order`                        | Saved sidebar order: `{"order": [filenames, top first]}`           |
| POST   | `/push`                         | Push a message (text body or JSON)                                 |
| POST   | `/clear`                        | Move all messages to `.trash/`                                     |
| POST   | `/trash/restore/<filename>`     | Restore one message from `.trash/` back to `messages/`             |
| PUT    | `/order`                        | Save the sidebar order (body `{"order": [...]}`); pushed to every tab as an SSE `order` event |
| DELETE | `/messages/<filename>`          | Move one message to `.trash/`                                      |

## How delete and restore work

The browser × button (or `DELETE /messages/<filename>` or `POST /clear`) doesn't `unlink()` — it moves the file to `messages/.trash/` and resets its mtime to "now". The retention sweep (`gc_trash()`) runs:

- Once at panel startup
- Every hour while the watcher is running

Files older than **10 days** in `.trash/` are permanently removed. Adjust `TRASH_RETENTION_DAYS` in `panel.py` if you want a different window.

To restore a soft-deleted file inside the retention window, click **History** in the toolbar — the modal lists every file in `.trash/` newest-first, with title preview and a per-row "auto-purges in N days" countdown. One click on **Restore** moves it back into `messages/`, the watcher's next poll detects the file, and your browser sees it via SSE just like any new push. (Or `POST /trash/restore/<filename>` directly.)

If a same-named file already exists in `messages/`, the restored copy is saved as `<stem>.restored-<ms>.md` so neither is overwritten. The internal `.deleted-<ms>` collision marker (added when two trashed files would have collided) is stripped on restore.

## Use with OpenCode / Claude Code (or any agent CLI)

The panel doesn't know or care which agent is writing files. To make your agent panel-aware, give it instructions like:

> When the response contains LaTeX formulas, Mermaid diagrams, or long code blocks, also write the same content as a markdown file to `<absolute-path>/messages/{YYYYMMDD-HHMMSS}-{slug}.md`. The panel will render it.

For OpenCode users, see `AGENTS.md` snippets in the repo's `examples/` folder (if present) or roll your own. The trigger logic — explicit phrases vs. implicit content-type detection — is up to your agent definition.

## Tech notes

- **Backend**: Python 3.8+ stdlib only — `http.server`, `socketserver`, `threading`, `Queue`. No `pip install`. Single file: `panel.py`.
- **Transport**: SSE (`text/event-stream`) with 15-second heartbeat. Browser auto-reconnects on disconnect; on every (re)connect it re-fetches `/messages` and `/order` to recover any events missed while disconnected.
- **Frontend**: Single HTML file, CDN-loaded `marked@12` + `katex@0.16` + `prismjs@1.29` + `mermaid@10.9`. Dark theme. No build step.
- **Watcher**: Polling-based (mtime diff every 800ms). Reliable across editors and OSes; no inotify/FSEvents dependency.

## Troubleshooting

**Port 7878 already in use?** Edit `PORT` at the top of `panel.py`.

**Browser shows "reconnecting…"?** The panel process probably isn't running. Restart it; the browser will reconnect within a few seconds and reload history automatically. If it was running in the background, the end of `panel.log` says why it stopped.

**Lost a message after browser refresh?** You shouldn't — files persist in `messages/`, and refresh re-fetches them. If it's missing from disk too, check `messages/.trash/` (it's recoverable for 10 days).

**msys2 / mingw Python on Windows fails to import stdlib?** Use the `py` launcher: `py -3.12 panel.py`. The `start-panel.bat` already does this.

## License

MIT.
