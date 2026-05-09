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
- **Soft delete** — deleted messages move to `messages/.trash/` and are auto-purged after 10 days
- **Collapsible cards** — click the triangle to fold long messages; sticky header keeps title + delete button reachable while scrolling
- **Zero install** — Python 3.8+ stdlib only, no `pip install` needed; one HTML file, four CDN libs

## Quick start

```powershell
git clone https://github.com/Kenny-JT/sisyphus-panel.git
cd sisyphus-panel
py -3.12 panel.py
```

(Linux/macOS: `python3 panel.py`. Add `--no-browser` to skip the auto-open.)

The panel opens at <http://localhost:7878>. Hit Ctrl+C to stop. Drop a `.md` file into `messages/` and watch it render.

## Push content

### Method A · Write a file (the recommended way)

Any process that can write a UTF-8 file can push to the panel. Filenames sort the display order; using a timestamp prefix keeps things tidy:

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

| Method | Path                       | Purpose                                        |
| ------ | -------------------------- | ---------------------------------------------- |
| GET    | `/`                        | Panel HTML                                     |
| GET    | `/messages`                | All current messages as JSON array             |
| GET    | `/events`                  | SSE stream — `message` and `delete` events     |
| GET    | `/health`                  | `{"status":"ok","messages_dir":...}`           |
| POST   | `/push`                    | Push a message (text body or JSON)             |
| POST   | `/clear`                   | Move all messages to `.trash/`                 |
| DELETE | `/messages/<filename>`     | Move one message to `.trash/`                  |

## How delete works

The browser × button (or `DELETE /messages/<filename>` or `POST /clear`) doesn't `unlink()` — it moves the file to `messages/.trash/` and resets its mtime to "now". The retention sweep (`gc_trash()`) runs:

- Once at panel startup
- Every hour while the watcher is running

Files older than **10 days** in `.trash/` are permanently removed. Adjust `TRASH_RETENTION_DAYS` in `panel.py` if you want a different window.

To recover a soft-deleted file before the sweep purges it, just move it back out of `messages/.trash/` into `messages/`. The watcher will pick it up on the next poll.

## Use with OpenCode / Claude Code (or any agent CLI)

The panel doesn't know or care which agent is writing files. To make your agent panel-aware, give it instructions like:

> When the response contains LaTeX formulas, Mermaid diagrams, or long code blocks, also write the same content as a markdown file to `<absolute-path>/messages/{YYYYMMDD-HHMMSS}-{slug}.md`. The panel will render it.

For OpenCode users, see `AGENTS.md` snippets in the repo's `examples/` folder (if present) or roll your own. The trigger logic — explicit phrases vs. implicit content-type detection — is up to your agent definition.

## Tech notes

- **Backend**: Python 3.8+ stdlib only — `http.server`, `socketserver`, `threading`, `Queue`. No `pip install`. Single file: `panel.py`.
- **Transport**: SSE (`text/event-stream`) with 15-second heartbeat. Browser auto-reconnects on disconnect; on every (re)connect it re-fetches `/messages` to recover any events missed while disconnected.
- **Frontend**: Single HTML file, CDN-loaded `marked@12` + `katex@0.16` + `prismjs@1.29` + `mermaid@10.9`. Dark theme. No build step.
- **Watcher**: Polling-based (mtime diff every 800ms). Reliable across editors and OSes; no inotify/FSEvents dependency.

## Troubleshooting

**Port 7878 already in use?** Edit `PORT` at the top of `panel.py`.

**Browser shows "reconnecting…"?** The panel process probably isn't running. Restart it; the browser will reconnect within a few seconds and reload history automatically.

**Lost a message after browser refresh?** You shouldn't — files persist in `messages/`, and refresh re-fetches them. If it's missing from disk too, check `messages/.trash/` (it's recoverable for 10 days).

**msys2 / mingw Python on Windows fails to import stdlib?** Use the `py` launcher: `py -3.12 panel.py`. The `start-panel.bat` already does this.

## License

MIT.
