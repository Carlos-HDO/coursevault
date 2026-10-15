# CourseVault

*[Leia em português](README.pt-BR.md)*

A local, dark-mode portal (Obsidian-like) for browsing your folder of courses and labs:
course cards, clickable tags, instant search, a video player with smooth seeking,
per-lesson progress, and previews for PDFs, Markdown and code. It can also organize
the library for you: edit covers, favorite folders, and create, rename, move or delete items.

No database and no Node build: one `server.py` (FastAPI) plus one `index.html`
(Tailwind via CDN + plain JavaScript). The interface is available in **English and
Portuguese** — use the language button in the top-right corner.

## Installation

Requires Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt    # = pip install fastapi uvicorn
```

## Running

```bash
# from the courses root folder
cd ~/Courses
python /path/to/server.py

# or point to the root explicitly
python server.py --root ~/Courses
```

The browser opens at `http://localhost:8000`.

| Option | Description |
|---|---|
| `-r, --root FOLDER` | Courses root folder (default: current folder, or `$HUB_ROOT`) |
| `-p, --port 8000` | HTTP port |
| `--host 0.0.0.0` | Expose on the LAN (e.g. watch on a tablet). The default `127.0.0.1` only accepts local access |
| `--no-browser` | Do not open the browser automatically |
| `--follow-symlinks` | Allow symlinks that point outside the root (e.g. courses on an external drive). Those items are read-only |
| `--allow-host NAME` | With `--host 0.0.0.0`: extra name accepted in the `Host` header (repeatable). IP addresses and the machine's own name always work |
| `--ttl 30` | Seconds to cache the search index and recursive counts |
| `--read-only` | Disable all editing (cover, create, rename, move and delete) |

If `server.py` lives inside the courses folder itself, the app files are hidden in the library.

### `cursos.sh` launcher

`cursos.sh` starts the server with the project's `.venv`, `--follow-symlinks` and a default
folder. Link it somewhere on your `PATH`:

```bash
ln -s "$PWD/cursos.sh" ~/.local/bin/cursos
cursos                     # default folder ($COURSEVAULT_ROOT)
cursos ~/Downloads/course  # any folder
cursos . --port 9000       # extra options go to server.py
```

The script finds the project through the symlink, so it keeps working if the project folder moves.

## Language

The interface follows the browser language on first visit (Portuguese for `pt-*`, English
otherwise). The **EN/PT** button in the top-right corner switches it, and the choice is saved
in the browser. Server error messages follow the same language. The command line (`--help`,
startup messages, `cursos.sh`) follows the system `LANG`.

## Per-folder settings (`course.conf`)

Optional, in any folder. Also read as `.course.conf` or `.conf`.
See `course.conf.example`:

```ini
pinned = true
banner = banner.jpg            # image inside the folder or library, or external URL
tags = Active Directory, Pentest, Windows, Kerberos
title = Active Directory Exploitation
description = Notes and lessons on attacking AD environments.
```

Extra fields: `status`, `color`, `order`, `hidden`, `banner_pos` (vertical framing of the cover, 0–100)
and `banner = none` (no cover). Without `banner`, the app looks for `banner.*`, `cover.*` or `capa.*`
in the folder and, if there is none, draws a gradient.
A `course.conf` in the root sets the library title (`title = My Courses`).

## Features

- **Dashboard:** favorites section, card grid (cover, title, lesson/material/folder counts, tags, status), tag filter (several tags combine with AND), and a "Continue watching" shortcut.
- **Navigation:** breadcrumbs, pills for sibling folders, modules (subfolders) with progress. URLs live in the hash (`#/Course/Module?play=lesson.mp4`), so back, forward and bookmarks work.
- **Videos** (`.mp4 .mkv .webm .mov .m4v`) and audio: list with clean titles (`03 - Kerberoasting_advanced.mp4` becomes **03** Kerberoasting advanced). The player is only created when you click (one video at a time) and has PiP, fullscreen, speed, previous/next, autoplay of the next lesson and resume from where you stopped. A lesson is marked as watched at 90%. `.srt`/`.vtt` subtitles with the same name (`lesson.en.srt`) load automatically.
- **Materials:** PDFs in the browser viewer; code (`.asm .py .sh .c .ps1 …`) with syntax highlighting and line numbers; rendered Markdown (with relative images); copy, open-in-new-tab and download buttons.
- **Search** (`/` or `Ctrl+K`): ignores accents and case and looks through folders, lessons, files and tags. It can be limited to the current folder. `Enter` opens the first result and the arrow keys navigate.
- **Favorites:** the star on cards, modules and the folder header favorites any folder. Favorites (and folders with `pinned = true`) come first in the root, in modules and in sibling pills.
- **Edit cover:** the "Edit cover" button in a folder header lets you upload an image (choose, drag or paste with Ctrl+V), use a URL, remove the cover and adjust its vertical framing. The image is saved as `banner.<ext>` in the folder and `course.conf` is updated. The previous cover is never deleted: it becomes `.<name>.bak`, hidden in the library.
- **Organize folders and files:** every folder, lesson and file has a ⋯ menu with **Rename**, **Move to…** and **Delete**. "New folder" and "Select" sit at the top of the root and in each folder header. In selection mode, click items and use the bottom bar to move or delete several at once. "Move to…" opens a folder browser with library-wide search and lets you create the destination folder on the spot. Subtitles with the same name follow their video when renaming, moving or deleting. Progress, favorites and "Continue watching" follow items to their new path. Nothing is overwritten: if an item with the same name already exists at the destination, the operation stops.
- **Trash:** "Delete" erases nothing. Items go to `.lixeira/<date_time>/<original path>` in the library root (hidden in the app). To restore, move the item back with your file manager; to free space for good, delete the `.lixeira` folder.
- **Progress and favorites** are stored in the browser's `localStorage`. The app only touches your folders when you ask it to: editing a cover (`banner.*` and `course.conf`) or creating, renaming, moving and deleting items.

## Security

- Listens only on `127.0.0.1` by default and rejects unknown `Host` headers (DNS-rebinding protection). In LAN mode (`--host 0.0.0.0`) the check stays on: IP addresses and the machine's own name (`hostname`, `hostname.local`) are accepted; add other names with `--allow-host`.
- Blocks `../`, absolute paths, hidden files (`.something`) and symlinks pointing outside the root (unless you use `--follow-symlinks`). Even with `--follow-symlinks`, links to hidden files or folders (e.g. `~/.ssh`) are refused, and whatever lives outside the root is read-only.
- A `banner` in `course.conf` only accepts images inside the folder itself or inside the library.
- Write routes (`PUT`/`PATCH`/`DELETE`, the JSON-only `POST /api/fs/*` and `POST /api/refresh`) reject requests whose `Origin` is another site. With `--host 0.0.0.0`, any device on the LAN can edit too; use `--read-only` to prevent that.
- Names containing `/ \ < > : " | ? *`, starting with a dot or ending with a dot are refused (NTFS-compatible). The root, `.lixeira` and the app files cannot be changed.
- Only video, audio, images, PDF, subtitles and text are shown in the browser. Web code (`.html`, `.xhtml`, `.shtml`, `.js`, `.xml`) is served as plain text, SVG with a `sandbox` CSP, and every other type (e.g. `.xht`, `.svgz`, `.rss`) as a download, so no material can run scripts on the app's origin.
- The highlight.js, marked and DOMPurify scripts from the CDN are checked with SRI (`integrity`). Tailwind is pinned to an exact version, but its CDN does not support SRI.

## Notes

- **MKV:** the browser only plays it if the codecs are supported (H.264/VP9/AV1 + AAC/Opus). MKV with HEVC or AC3 shows a warning with a download link to open it in VLC/mpv.
- **Offline:** Tailwind, highlight.js and marked come from a CDN. Without internet the app works, but unstyled.
- **New files** show up in listings immediately. Counts and search refresh within `--ttl` seconds, or right away with the ⟳ (reindex) button.

## API

All routes accept an `X-Lang: pt|en` header (or `Accept-Language`) for the language of error messages.

| Route | Description |
|---|---|
| `GET /api/list?path=` | Folder contents (subfolders with conf and counts, media, materials, breadcrumbs, siblings) |
| `GET /api/search?q=&path=` | Global search or search within a folder |
| `GET/HEAD /api/file?path=[&download=1]` | File with `Range` support (206 Partial Content) |
| `GET /api/banner?path=` | Folder cover |
| `GET /api/text?path=` | Text/code content (up to 2 MB) |
| `GET /api/subtitle?path=` | Subtitle converted to WebVTT |
| `PUT /api/banner?path=` | Upload a new cover (body = image; JPG, PNG, WebP, GIF or AVIF, up to 15 MB) |
| `DELETE /api/banner?path=` | Remove the cover (`banner = none`; the file becomes `.bak`) |
| `PATCH /api/conf?path=` | Change `banner` (http/https URL) and/or `banner_pos` (0–100) in `course.conf`; JSON |
| `GET /api/tree?path=` | Subfolders of a folder (folder browser) |
| `POST /api/fs/mkdir` | Create a folder: `{parent, name}` |
| `POST /api/fs/rename` | Rename: `{path, name}`; returns `moved: [{from, to}]` |
| `POST /api/fs/move` | Move several items: `{paths, dest}`; returns `moved` |
| `POST /api/fs/trash` | Move to `.lixeira`: `{paths}` |
| `POST /api/refresh` | Rebuild the index |
| `GET /api/docs` | Interactive documentation (Swagger) |
