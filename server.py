#!/usr/bin/env python3
"""
Hub de Cursos — portal local para navegar pelas pastas de cursos e laboratórios.

    pip install -r requirements.txt
    python server.py                       # serve a pasta atual
    python server.py --root ~/Cursos       # serve outra pasta
    python server.py --port 9000 --no-browser

Toda a navegação é feita direto no sistema de arquivos (sem banco de dados).
Apenas as contagens recursivas e o índice de busca ficam em cache por alguns
segundos (--ttl) para não varrer a árvore inteira a cada requisição.
"""
from __future__ import annotations

import argparse
import math
import mimetypes
import os
import re
import threading
import time
import unicodedata
import webbrowser
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse

# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #

APP_DIR = Path(__file__).resolve().parent
INDEX_HTML = APP_DIR / "index.html"
APP_FILES = {"server.py", "index.html", "requirements.txt", "course.conf.example"}

ROOT: Path = Path(os.environ.get("HUB_ROOT", os.getcwd())).expanduser().resolve()
FOLLOW_SYMLINKS = os.environ.get("HUB_FOLLOW_SYMLINKS") == "1"
HIDE_APP_FILES = APP_DIR == ROOT
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# None = aceita qualquer Host (modo LAN). Protege contra DNS rebinding em modo local.
ALLOWED_HOSTS: set[str] | None = set(LOOPBACK)

CHUNK_SIZE = 1024 * 1024          # 1 MiB por bloco no streaming
MAX_TEXT_BYTES = 2 * 1024 * 1024  # limite de pré-visualização de texto/código
MAX_DEPTH = 24
SEARCH_LIMIT = 200

VIDEO_EXT = {".mp4", ".m4v", ".mkv", ".webm", ".mov", ".ogv"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac"}
SUB_EXT = {".srt", ".vtt"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg"}
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".nfo",
    ".asm", ".s", ".nasm", ".inc", ".py", ".pyw", ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".psm1", ".bat", ".cmd", ".vbs", ".c", ".h", ".cpp", ".cc", ".hpp",
    ".cs", ".java", ".kt", ".go", ".rs", ".rb", ".pl", ".php", ".lua", ".js",
    ".mjs", ".ts", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".css", ".sql", ".rules", ".yar", ".nse", ".diff",
    ".patch", ".ipynb", ".tf", ".hcl",
}
TEXT_NAMES = {"readme", "license", "makefile", "dockerfile", "vagrantfile", "notes", "changelog"}
OFFICE_EXT = {".doc", ".docx", ".odt", ".rtf", ".ppt", ".pptx", ".odp", ".xls", ".xlsx", ".ods", ".epub"}
DOC_EXT = {".pdf"} | TEXT_EXT | OFFICE_EXT

CONF_NAMES = ("course.conf", ".course.conf", ".conf")
BANNER_STEMS = {"banner", "cover", "capa", "poster", "folder"}
IGNORED_NAMES = {"__pycache__", "node_modules", "$RECYCLE.BIN", "System Volume Information",
                 "Thumbs.db", "desktop.ini"}
TRUE_VALUES = {"1", "true", "yes", "y", "sim", "s", "on"}

for _ext, _mime in {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mkv": "video/x-matroska", ".webm": "video/webm",
    ".mov": "video/quicktime", ".ogv": "video/ogg", ".m4a": "audio/mp4", ".opus": "audio/ogg",
    ".flac": "audio/flac", ".vtt": "text/vtt", ".webp": "image/webp", ".avif": "image/avif",
    ".svg": "image/svg+xml", ".pdf": "application/pdf",
}.items():
    mimetypes.add_type(_mime, _ext)


def set_root(path: Path) -> None:
    global ROOT, HIDE_APP_FILES
    ROOT = path.expanduser().resolve()
    HIDE_APP_FILES = APP_DIR == ROOT


# --------------------------------------------------------------------------- #
# Utilitários
# --------------------------------------------------------------------------- #

_NUM_SPLIT = re.compile(r"(\d+)")
_LEAD_NUM = re.compile(
    r"^(?:(?:aula|lesson|lecture|class|ep|episode|epis[oó]dio|v[ií]deo|video|module|m[oó]dulo|"
    r"part|parte|section|se[cç][aã]o|chapter|cap[ií]tulo)[\s#.\-]*)?"
    r"(\d{1,4}(?:[.\-]\d{1,3})*)(?:\s*[-.)\]:–—|]+\s*|\s+)(\S.*)$",
    re.IGNORECASE,
)


def natural_key(s: str) -> list:
    """'2. Aula' vem antes de '10. Aula'."""
    return [int(t) if i % 2 else t.casefold() for i, t in enumerate(_NUM_SPLIT.split(s))]


def normalize(s: str) -> str:
    """Minúsculas e sem acentos, para busca."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).casefold()


def split_title(name: str, is_file: bool = True) -> tuple[str | None, str]:
    """'03 - Kerberoasting_avançado.mp4' -> ('03', 'Kerberoasting avançado')."""
    stem = os.path.splitext(name)[0] if is_file else name
    stem = stem.replace("_", " ")
    if " " not in stem and stem.count(".") >= 2:  # 01.Intro.To.AD
        stem = stem.replace(".", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    m = _LEAD_NUM.match(stem)
    if m:
        return m.group(1), m.group(2).strip()
    return None, stem or name


def ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def is_text_name(name: str) -> bool:
    ext = ext_of(name)
    return ext in TEXT_EXT or (not ext and name.lower() in TEXT_NAMES)


def classify(name: str) -> str:
    ext = ext_of(name)
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in SUB_EXT:
        return "subtitle"
    if ext in IMAGE_EXT:
        return "image"
    if ext in DOC_EXT or is_text_name(name):
        return "doc"
    return "other"


def is_hidden(name: str) -> bool:
    return name.startswith(".") or name in IGNORED_NAMES


def is_within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def join_rel(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def visible_entry(e: os.DirEntry, parent_rel: str) -> bool:
    if is_hidden(e.name):
        return False
    if not parent_rel and HIDE_APP_FILES and e.name in APP_FILES:
        return False
    if e.is_symlink() and not FOLLOW_SYMLINKS:
        try:
            real = Path(e.path).resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return is_within(real, ROOT)
    return True


def entry_is_dir(e: os.DirEntry) -> bool:
    try:
        return e.is_dir()
    except OSError:
        return False


def safe_path(rel: str | None) -> tuple[Path, str]:
    """Resolve um caminho relativo à raiz bloqueando '../', arquivos ocultos e
    links simbólicos que apontem para fora da raiz."""
    rel = (rel or "").replace("\\", "/")
    if "\x00" in rel:
        raise HTTPException(400, "Caminho inválido")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or is_hidden(p) or (os.name == "nt" and ":" in p):
            raise HTTPException(403, "Acesso negado")
    target = ROOT.joinpath(*parts)
    try:
        real = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, "Não encontrado") from None
    if not FOLLOW_SYMLINKS and not is_within(real, ROOT):
        raise HTTPException(403, "Acesso negado")
    return target, "/".join(parts)


def file_api_url(rel: str) -> str:
    return "/api/file?path=" + quote(rel, safe="")


def decode_bytes(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        if exc.start >= len(raw) - 3:  # caractere multibyte cortado no limite
            return raw[: exc.start].decode("utf-8-sig", errors="replace")
        return raw.decode("cp1252", errors="replace")


def new_stats() -> dict[str, int]:
    return {"media": 0, "docs": 0, "images": 0, "files": 0, "dirs": 0}


def quick_stats(d: Path) -> dict[str, int]:
    """Contagem rasa (usada quando a pasta ainda não está no índice)."""
    st = new_stats()
    try:
        with os.scandir(d) as it:
            for e in it:
                if is_hidden(e.name) or e.name in CONF_NAMES:
                    continue
                if entry_is_dir(e):
                    st["dirs"] += 1
                    continue
                _count_file(st, classify(e.name))
    except OSError:
        pass
    return st


def _count_file(st: dict[str, int], kind: str) -> None:
    if kind == "subtitle":
        return
    st["files"] += 1
    if kind in ("video", "audio"):
        st["media"] += 1
    elif kind == "doc":
        st["docs"] += 1
    elif kind == "image":
        st["images"] += 1


# --------------------------------------------------------------------------- #
# course.conf
# --------------------------------------------------------------------------- #

_CONF_LINE = re.compile(r"^([A-Za-z_][\w\-]*)\s*[=:]\s*(.*)$")
_conf_cache: dict[str, tuple[tuple, dict]] = {}


def _clean_value(v: str) -> str:
    v = v.strip()
    if v[:1] in ("'", '"'):
        end = v.find(v[0], 1)
        if end > 0:
            return v[1:end]
    return re.split(r"\s+#", v, maxsplit=1)[0].strip()


def parse_conf_text(text: str) -> dict[str, str]:
    """Aceita 'chave = valor', 'chave: valor' (YAML simples), seções [ini],
    comentários (# ;) e listas YAML ('- item') logo após uma chave."""
    data: dict[str, str] = {}
    last_key: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "//")) or re.fullmatch(r"\[[^\]=]*\]", line):
            continue
        if line.startswith("- ") and last_key:
            item = _clean_value(line[2:])
            data[last_key] = f"{data[last_key]}, {item}" if data[last_key] else item
            continue
        m = _CONF_LINE.match(line)
        if not m:
            continue
        key = m.group(1).lower().replace("-", "_")
        data[key] = _clean_value(m.group(2))
        last_key = key
    return data


def parse_tags(value: str) -> list[str]:
    value = value.strip().strip("[]")
    tags: list[str] = []
    for t in re.split(r"[,;|]", value):
        t = t.strip().strip("'\"").strip()
        if t and t not in tags:
            tags.append(t)
    return tags


def _find_banner(d: Path, raw: str | None, names: list[str]) -> tuple[str | None, Path | None, str | None]:
    """Retorna (valor externo, arquivo local, nome do arquivo dentro da pasta)."""
    if raw:
        if re.match(r"^(https?:)?//|^data:image/", raw, re.I):
            return raw, None, None
        p = Path(raw).expanduser()
        p = p if p.is_absolute() else d / p
        if p.is_file() and ext_of(p.name) in IMAGE_EXT:
            inside = p.parent.resolve() == d.resolve()
            return None, p, (p.name if inside else None)
    for n in sorted(names, key=natural_key):
        if os.path.splitext(n)[0].lower() in BANNER_STEMS and ext_of(n) in IMAGE_EXT:
            return None, d / n, n
    return None, None, None


def load_conf(d: Path, rel: str) -> dict:
    """Lê (com cache por mtime) o course.conf da pasta e detecta o banner."""
    try:
        dmt = d.stat().st_mtime_ns
    except OSError:
        return _empty_conf()
    conf_file = next((d / n for n in CONF_NAMES if (d / n).is_file()), None)
    try:
        cmt = conf_file.stat().st_mtime_ns if conf_file else None
    except OSError:
        conf_file, cmt = None, None
    key = str(d)
    cached = _conf_cache.get(key)
    if cached and cached[0] == (dmt, cmt):
        return cached[1]

    raw: dict[str, str] = {}
    if conf_file:
        try:
            raw = parse_conf_text(decode_bytes(conf_file.read_bytes()[:65536]))
        except OSError:
            raw = {}
    try:
        names = [n for n in os.listdir(d) if not n.startswith(".")]
    except OSError:
        names = []
    external, banner_file, banner_name = _find_banner(d, raw.get("banner") or raw.get("cover"), names)
    banner_url = external
    if banner_file is not None:
        try:
            v = banner_file.stat().st_mtime_ns
        except OSError:
            v = 0
        banner_url = f"/api/banner?path={quote(rel, safe='')}&v={v}"
    try:
        order = float(raw["order"]) if raw.get("order") else None
    except ValueError:
        order = None

    conf = {
        "title": raw.get("title") or None,
        "description": raw.get("description") or raw.get("desc") or None,
        "tags": parse_tags(raw.get("tags", "")),
        "pinned": raw.get("pinned", "").strip().lower() in TRUE_VALUES,
        "hidden": raw.get("hidden", "").strip().lower() in TRUE_VALUES,
        "status": raw.get("status") or None,
        "color": raw.get("color") or None,
        "order": order,
        "banner": banner_url,
        "has_conf": conf_file is not None,
        "_banner_file": banner_file,
        "_banner_name": banner_name,
    }
    _conf_cache[key] = ((dmt, cmt), conf)
    return conf


def _empty_conf() -> dict:
    return {"title": None, "description": None, "tags": [], "pinned": False, "hidden": False,
            "status": None, "color": None, "order": None, "banner": None, "has_conf": False,
            "_banner_file": None, "_banner_name": None}


def public_conf(conf: dict) -> dict:
    return {k: v for k, v in conf.items() if not k.startswith("_")}


def dir_sort_key(item: dict) -> tuple:
    c = item["conf"]
    return (not c["pinned"], c["order"] if c["order"] is not None else math.inf, natural_key(item["name"]))


# --------------------------------------------------------------------------- #
# Índice (busca + contagens recursivas), reconstruído a cada TTL segundos
# --------------------------------------------------------------------------- #

class TreeIndex:
    def __init__(self, ttl: float = 30.0) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._built = 0.0
        self._entries: list[dict] | None = None
        self._stats: dict[str, dict[str, int]] = {}

    def snapshot(self, force: bool = False) -> tuple[list[dict], dict[str, dict[str, int]]]:
        with self._lock:
            if force or self._entries is None or time.monotonic() - self._built > self.ttl:
                self._entries, self._stats = self._build()
                self._built = time.monotonic()
            return self._entries, self._stats

    def _build(self) -> tuple[list[dict], dict[str, dict[str, int]]]:
        entries: list[dict] = []
        stats: dict[str, dict[str, int]] = {}
        seen: set[tuple[int, int]] = set()

        def walk(d: Path, rel: str, depth: int) -> dict[str, int] | None:
            conf = load_conf(d, rel)
            if rel and conf["hidden"]:
                return None
            st = new_stats()
            stats[rel] = st
            try:
                s = d.stat()
                if (s.st_dev, s.st_ino) in seen or depth > MAX_DEPTH:
                    return st
                seen.add((s.st_dev, s.st_ino))
                with os.scandir(d) as it:
                    items = list(it)
            except OSError:
                return st
            skip = set(CONF_NAMES) | {conf["_banner_name"]}
            for e in items:
                if e.name in skip or not visible_entry(e, rel):
                    continue
                child = join_rel(rel, e.name)
                if entry_is_dir(e):
                    sub = walk(Path(e.path), child, depth + 1)
                    if sub is None:
                        continue
                    st["dirs"] += 1 + sub["dirs"]
                    for k in ("media", "docs", "images", "files"):
                        st[k] += sub[k]
                    continue
                kind = classify(e.name)
                _count_file(st, kind)
                if kind == "subtitle":
                    continue
                num, title = split_title(e.name)
                entries.append({
                    "type": kind, "path": child, "name": e.name, "title": title, "num": num,
                    "ext": ext_of(e.name), "text": is_text_name(e.name), "parent": rel, "tags": [],
                    "_hay": normalize(f"{e.name} {title}"),
                })
            if rel:
                name = rel.rsplit("/", 1)[-1]
                num, clean = split_title(name, is_file=False)
                title = conf["title"] or name
                entries.append({
                    "type": "dir", "path": rel, "name": name, "title": title, "num": num,
                    "parent": rel.rsplit("/", 1)[0] if "/" in rel else "", "tags": conf["tags"],
                    "_hay": normalize(" ".join([name, title, " ".join(conf["tags"]), conf["description"] or ""])),
                })
            return st

        walk(ROOT, "", 0)
        return entries, stats


INDEX = TreeIndex()

_TYPE_RANK = {"dir": 0, "video": 1, "audio": 1, "doc": 2, "image": 3, "other": 4}


def search(query: str, scope: str = "") -> list[dict]:
    terms = normalize(query).split()
    if not terms:
        return []
    entries, _ = INDEX.snapshot()
    prefix = scope + "/" if scope else ""
    found: list[tuple[tuple, dict]] = []
    for e in entries:
        if prefix and not e["path"].startswith(prefix):
            continue
        hay = e["_hay"]
        if all(t in hay for t in terms):
            title_n = normalize(e["title"])
            score = (_TYPE_RANK.get(e["type"], 9), 0 if title_n.startswith(terms[0]) else 1,
                     e["path"].count("/"), natural_key(e["path"]))
            found.append((score, e))
    found.sort(key=lambda x: x[0])
    return [{k: v for k, v in e.items() if not k.startswith("_")} for _, e in found[:SEARCH_LIMIT]]


# --------------------------------------------------------------------------- #
# Envio de arquivos com suporte a HTTP Range (seek suave em vídeos)
# --------------------------------------------------------------------------- #

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
# Tipos que, servidos "inline", executariam scripts na origem do app: viram texto puro.
_ACTIVE_TYPES = {".html", ".htm", ".xhtml", ".xml", ".js", ".mjs"}


def guess_mime(name: str) -> str:
    if is_text_name(name) or ext_of(name) in _ACTIVE_TYPES:
        return "text/plain; charset=utf-8"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def content_disposition(name: str, download: bool) -> str:
    kind = "attachment" if download else "inline"
    fallback = name.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "") or "arquivo"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


def _iter_file(path: Path, start: int, length: int):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def send_file(path: Path, request: Request, download: bool = False, media_type: str | None = None) -> Response:
    try:
        st = path.stat()
    except OSError:
        raise HTTPException(404, "Arquivo não encontrado") from None
    if not path.is_file():
        raise HTTPException(404, "Arquivo não encontrado")
    size = st.st_size
    etag = f'"{st.st_mtime_ns:x}-{size:x}"'
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(path.name, download),
        "Last-Modified": formatdate(st.st_mtime, usegmt=True),
        "ETag": etag,
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    if ext_of(path.name) == ".svg":
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    media_type = media_type or guess_mime(path.name)

    start, end, status = 0, size - 1, 200
    rng = request.headers.get("range")
    if_range = request.headers.get("if-range")
    if rng and size > 0 and (not if_range or if_range == etag):
        m = _RANGE_RE.fullmatch(rng.strip())
        if m and (m.group(1) or m.group(2)):  # múltiplos intervalos: responde o arquivo inteiro
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:  # sufixo: bytes=-500
                start, end = max(0, size - int(m.group(2))), size - 1
            if start >= size or start > end:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
            status = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)
    if request.method == "HEAD":
        return Response(status_code=status, headers=headers, media_type=media_type)
    return StreamingResponse(_iter_file(path, start, length), status_code=status,
                             headers=headers, media_type=media_type)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

class HostGuard:
    """Bloqueia requisições com Host desconhecido (anti DNS-rebinding)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and ALLOWED_HOSTS is not None:
            host = dict(scope["headers"]).get(b"host", b"").decode("latin-1").lower()
            hostname = host if host.endswith("]") else host.rsplit(":", 1)[0]
            if hostname.strip("[]") not in ALLOWED_HOSTS:
                await PlainTextResponse("Host não permitido", status_code=400)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = FastAPI(title="Hub de Cursos", docs_url="/api/docs", redoc_url=None)
app.add_middleware(HostGuard)


def hub_title() -> str:
    return load_conf(ROOT, "")["title"] or "Hub de Cursos"


def dir_item(d: Path, rel: str, stats: dict) -> dict | None:
    conf = load_conf(d, rel)
    if conf["hidden"]:
        return None
    name = rel.rsplit("/", 1)[-1]
    num, clean = split_title(name, is_file=False)
    return {
        "name": name, "path": rel, "title": conf["title"] or name, "num": num, "clean": clean,
        "conf": public_conf(conf), "stats": stats.get(rel) or quick_stats(d),
    }


@app.get("/api/list")
def api_list(path: str = ""):
    d, rel = safe_path(path)
    if not d.is_dir():
        raise HTTPException(404, "Pasta não encontrada")
    conf = load_conf(d, rel)
    _, stats = INDEX.snapshot()
    try:
        with os.scandir(d) as it:
            items = sorted(it, key=lambda e: natural_key(e.name))
    except OSError as exc:
        raise HTTPException(500, f"Não foi possível ler a pasta: {exc.strerror}") from None

    dirs, media, docs, images, others, subs = [], [], [], [], [], []
    skip = set(CONF_NAMES) | {conf["_banner_name"]}
    for e in items:
        if e.name in skip or not visible_entry(e, rel):
            continue
        child = join_rel(rel, e.name)
        if entry_is_dir(e):
            item = dir_item(Path(e.path), child, stats)
            if item:
                dirs.append(item)
            continue
        try:
            st = e.stat()
        except OSError:
            continue
        kind = classify(e.name)
        num, title = split_title(e.name)
        item = {"name": e.name, "path": child, "ext": ext_of(e.name), "size": st.st_size,
                "mtime": st.st_mtime, "kind": kind, "num": num, "title": title,
                "text": is_text_name(e.name)}
        {"video": media, "audio": media, "doc": docs, "image": images,
         "subtitle": subs}.get(kind, others).append(item)

    # Associa legendas (aula.srt, aula.pt-BR.vtt) ao vídeo de mesmo nome.
    used = set()
    for m in media:
        stem = os.path.splitext(m["name"])[0]
        m["subtitles"] = []
        for s in subs:
            sstem = os.path.splitext(s["name"])[0]
            if sstem == stem or sstem.startswith(stem + "."):
                lang = sstem[len(stem) + 1:] or "Legenda"
                m["subtitles"].append({"label": lang, "path": s["path"], "lang": lang[:2].lower()})
                used.add(s["path"])
    others.extend(s for s in subs if s["path"] not in used)
    dirs.sort(key=dir_sort_key)

    crumbs = [{"title": hub_title(), "path": ""}]
    acc: list[str] = []
    for part in rel.split("/") if rel else []:
        acc.append(part)
        p = "/".join(acc)
        crumbs.append({"title": load_conf(ROOT.joinpath(*acc), p)["title"] or part, "path": p})

    siblings, parent = [], None
    if rel:
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        parent_dir = ROOT.joinpath(*parent.split("/")) if parent else ROOT
        try:
            with os.scandir(parent_dir) as it:
                for e in it:
                    if visible_entry(e, parent) and entry_is_dir(e):
                        item = dir_item(Path(e.path), join_rel(parent, e.name), stats)
                        if item:
                            siblings.append({k: item[k] for k in ("name", "path", "title", "num", "clean", "conf")})
        except OSError:
            pass
        siblings.sort(key=dir_sort_key)

    name = rel.rsplit("/", 1)[-1] if rel else hub_title()
    num, clean = split_title(name, is_file=False) if rel else (None, name)
    return {
        "path": rel, "name": name, "title": conf["title"] or name, "num": num, "clean": clean,
        "conf": public_conf(conf), "stats": stats.get(rel) or quick_stats(d),
        "breadcrumbs": crumbs, "siblings": siblings, "parent": parent,
        "dirs": dirs, "media": media, "docs": docs, "images": images, "others": others,
        "hub": {"title": hub_title(), "root": str(ROOT)},
    }


@app.get("/api/search")
def api_search(q: str = "", path: str = ""):
    _, scope = safe_path(path) if path else (ROOT, "")
    results = search(q[:200], scope)
    return {"query": q, "scope": scope, "results": results, "limit": SEARCH_LIMIT}


@app.api_route("/api/file", methods=["GET", "HEAD"])
def api_file(request: Request, path: str, download: bool = False):
    f, _ = safe_path(path)
    return send_file(f, request, download=download)


@app.api_route("/api/banner", methods=["GET", "HEAD"])
def api_banner(request: Request, path: str = ""):
    d, rel = safe_path(path)
    banner = load_conf(d, rel)["_banner_file"]
    if banner is None or ext_of(banner.name) not in IMAGE_EXT:
        raise HTTPException(404, "Sem banner")
    return send_file(banner, request)


@app.get("/api/text")
def api_text(path: str):
    f, rel = safe_path(path)
    if not f.is_file():
        raise HTTPException(404, "Arquivo não encontrado")
    size = f.stat().st_size
    with open(f, "rb") as fh:
        raw = fh.read(MAX_TEXT_BYTES)
    if b"\x00" in raw[:8192] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise HTTPException(415, "Arquivo binário — use o download")
    return {"name": f.name, "path": rel, "ext": ext_of(f.name), "size": size,
            "truncated": size > MAX_TEXT_BYTES, "content": decode_bytes(raw)}


@app.get("/api/subtitle")
def api_subtitle(path: str):
    f, _ = safe_path(path)
    if not f.is_file() or ext_of(f.name) not in SUB_EXT:
        raise HTTPException(404, "Legenda não encontrada")
    text = decode_bytes(f.read_bytes()[: 5 * 1024 * 1024]).replace("\r\n", "\n").replace("\r", "\n")
    if ext_of(f.name) == ".srt":
        text = re.sub(r"(\d+):(\d{2}):(\d{2}),(\d{3})",
                      lambda m: f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3)}.{m.group(4)}", text)
        text = "WEBVTT\n\n" + text
    elif not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text
    return Response(text, media_type="text/vtt; charset=utf-8")


@app.post("/api/refresh")
def api_refresh():
    _conf_cache.clear()
    entries, stats = INDEX.snapshot(force=True)
    return {"ok": True, "entries": len(entries), "folders": len(stats)}


@app.get("/", include_in_schema=False)
def index():
    if not INDEX_HTML.is_file():
        return PlainTextResponse(f"index.html não encontrado em {APP_DIR}", status_code=500)
    return FileResponse(INDEX_HTML, media_type="text/html", headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #

def main() -> None:
    global FOLLOW_SYMLINKS, ALLOWED_HOSTS
    ap = argparse.ArgumentParser(description="Hub de Cursos — portal local para suas pastas de cursos.")
    ap.add_argument("-r", "--root", default=os.environ.get("HUB_ROOT", "."),
                    help="pasta raiz dos cursos (padrão: pasta atual)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface de rede (use 0.0.0.0 para acessar de outros dispositivos da LAN)")
    ap.add_argument("-p", "--port", type=int, default=8000, help="porta (padrão: 8000)")
    ap.add_argument("--no-browser", action="store_true", help="não abrir o navegador automaticamente")
    ap.add_argument("--follow-symlinks", action="store_true",
                    help="permitir links simbólicos que apontam para fora da raiz (ex.: HD externo)")
    ap.add_argument("--ttl", type=float, default=30.0,
                    help="segundos de cache do índice de busca/contagens (padrão: 30)")
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        ap.error(f"pasta raiz não existe: {root}")
    set_root(root)
    FOLLOW_SYMLINKS = args.follow_symlinks
    INDEX.ttl = args.ttl
    if args.host in LOOPBACK:
        ALLOWED_HOSTS = set(LOOPBACK)
    else:
        ALLOWED_HOSTS = None
        print("⚠  Servidor exposto na rede: qualquer dispositivo da LAN poderá ler os arquivos da raiz.")

    threading.Thread(target=INDEX.snapshot, daemon=True).start()  # pré-aquece o índice
    shown_host = "localhost" if args.host in LOOPBACK or args.host == "0.0.0.0" else args.host
    url = f"http://{shown_host}:{args.port}/"
    print(f"📚 Hub de Cursos\n   Raiz: {ROOT}\n   URL:  {url}\n   Ctrl+C para encerrar.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
