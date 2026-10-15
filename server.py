#!/usr/bin/env python3
"""
CourseVault — portal local para navegar pelas pastas de cursos e laboratórios.

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
import asyncio
import errno
import getpass
import hashlib
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote, urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

# --------------------------------------------------------------------------- #
# Configuração
# --------------------------------------------------------------------------- #

APP_DIR = Path(__file__).resolve().parent
INDEX_HTML = APP_DIR / "index.html"
APP_FILES = {"server.py", "index.html", "requirements.txt", "course.conf.example", "cursos.sh",
             "README.md", "README.pt-BR.md", "assets", "pyproject.toml", "LICENSE",
             "build", "dist", "coursevault.egg-info"}

ROOT: Path = Path(os.environ.get("HUB_ROOT", os.getcwd())).expanduser().resolve()
FOLLOW_SYMLINKS = os.environ.get("HUB_FOLLOW_SYMLINKS") == "1"
READ_ONLY = os.environ.get("HUB_READ_ONLY") == "1"
TRASH_DIR = ".lixeira"  # oculto no hub (começa com ponto)
TRASH_MANIFEST = ".manifest.json"  # o que foi excluído em cada lote (caminhos originais)

# Idioma das mensagens: por requisição (header X-Lang do front, ou Accept-Language);
# no terminal, o LANG do sistema.
_LANG: ContextVar[str] = ContextVar("lang", default="pt" if os.environ.get("LANG", "").lower().startswith("pt") else "en")


def tr(pt: str, en: str) -> str:
    return pt if _LANG.get() == "pt" else en
HIDE_APP_FILES = APP_DIR == ROOT
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# Nomes aceitos no header Host (anti DNS rebinding); IPs literais sempre passam.
# None = aceita qualquer nome (--allow-host '*').
ALLOWED_HOSTS: set[str] | None = set(LOOPBACK)
# Senha (--password): sessão em cookie. O token deriva da senha, então a sessão
# sobrevive a reinícios do servidor e cai sozinha quando a senha muda.
AUTH_TOKEN: str | None = None
SESSION_COOKIE = "cv_session"
SESSION_MAX_AGE = 30 * 24 * 3600

CHUNK_SIZE = 1024 * 1024          # 1 MiB por bloco no streaming
MAX_TEXT_BYTES = 2 * 1024 * 1024  # limite de pré-visualização de texto/código
MAX_BANNER_BYTES = 15 * 1024 * 1024  # limite do upload de capa
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
NO_BANNER_VALUES = {"none", "off", "false", "no", "nao", "não", "0"}

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


def reachable(real: Path) -> bool:
    """Destino real (já resolvido) que pode ser lido: dentro da raiz ou, com
    --follow-symlinks, fora dela; nunca dentro de pasta/arquivo oculto (ex.: um
    link para ~/.ssh/id_rsa)."""
    if is_within(real, ROOT):
        parts = real.relative_to(ROOT).parts
    elif FOLLOW_SYMLINKS:
        parts = real.parts[1:]
    else:
        return False
    return not any(is_hidden(p) for p in parts)


def visible_entry(e: os.DirEntry, parent_rel: str) -> bool:
    if is_hidden(e.name):
        return False
    if not parent_rel and HIDE_APP_FILES and e.name in APP_FILES:
        return False
    if e.is_symlink():
        try:
            real = Path(e.path).resolve(strict=True)
        except (OSError, RuntimeError):
            return False
        return reachable(real)
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
        raise HTTPException(400, tr("Caminho inválido", "Invalid path"))
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or is_hidden(p) or (os.name == "nt" and ":" in p):
            raise HTTPException(403, tr("Acesso negado", "Access denied"))
    target = ROOT.joinpath(*parts)
    try:
        real = target.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, tr("Não encontrado", "Not found")) from None
    if not reachable(real):
        raise HTTPException(403, tr("Acesso negado", "Access denied"))
    return target, "/".join(parts)


def require_inside_root(d: Path) -> None:
    """Escrita só dentro da raiz: o que é alcançado por link simbólico para fora
    dela (--follow-symlinks) fica somente leitura."""
    try:
        real = d.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, tr("Não encontrado", "Not found")) from None
    if not is_within(real, ROOT):
        raise HTTPException(403, tr("Fora da raiz: o que é acessado por link simbólico é somente leitura",
                                    "Outside the library root: items reached through a symlink are read-only"))


def read_head(path: Path, limit: int) -> bytes:
    """Lê no máximo `limit` bytes (sem carregar o arquivo inteiro na memória)."""
    with open(path, "rb") as f:
        return f.read(limit)


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
    if raw and raw.strip().lower() in NO_BANNER_VALUES:  # "banner = none" desliga a autodetecção
        return None, None, None
    if raw:
        if re.match(r"^(https?:)?//|^data:image/", raw, re.I):
            return raw, None, None
        p = Path(raw).expanduser()
        p = p if p.is_absolute() else d / p
        try:
            real, d_real = p.resolve(strict=True), d.resolve()
        except (OSError, RuntimeError):
            real = d_real = None
        # Só imagens da própria pasta ou da biblioteca: o course.conf de um curso baixado
        # não pode expor arquivos de outros lugares do computador.
        if (real is not None and real.is_file() and ext_of(real.name) in IMAGE_EXT
                and (is_within(real, d_real) or is_within(real, ROOT))):
            inside = p.parent.resolve() == d_real
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
            raw = parse_conf_text(decode_bytes(read_head(conf_file, 65536)))
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
    try:
        banner_pos = min(100.0, max(0.0, float(raw["banner_pos"]))) if raw.get("banner_pos") else None
    except ValueError:
        banner_pos = None

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
        "banner_pos": banner_pos,
        "has_conf": conf_file is not None,
        "_banner_file": banner_file,
        "_banner_name": banner_name,
    }
    _conf_cache[key] = ((dmt, cmt), conf)
    return conf


def _empty_conf() -> dict:
    return {"title": None, "description": None, "tags": [], "pinned": False, "hidden": False,
            "status": None, "color": None, "order": None, "banner": None, "banner_pos": None,
            "has_conf": False, "_banner_file": None, "_banner_name": None}


def public_conf(conf: dict) -> dict:
    return {k: v for k, v in conf.items() if not k.startswith("_")}


def update_conf(d: Path, updates: dict[str, str | None]) -> None:
    """Grava chaves no course.conf da pasta (cria o arquivo se não existir), trocando
    as linhas existentes no lugar e preservando comentários e demais chaves.
    Valor None remove a chave."""
    conf_file = next((d / n for n in CONF_NAMES if (d / n).is_file()), d / CONF_NAMES[0])
    try:
        lines = decode_bytes(conf_file.read_bytes()).splitlines() if conf_file.is_file() else []
    except OSError as exc:
        raise HTTPException(500, tr(f"Não foi possível ler o {conf_file.name}: {exc.strerror}", f"Could not read {conf_file.name}: {exc.strerror}")) from None
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        m = _CONF_LINE.match(line.strip())
        key = m.group(1).lower().replace("-", "_") if m else None
        if key not in updates:
            out.append(line)
            continue
        if key in pending:  # a primeira ocorrência é substituída; repetições somem
            value = pending.pop(key)
            if value is not None:
                out.append(f"{key} = {_conf_value(value)}")
    out.extend(f"{k} = {_conf_value(v)}" for k, v in pending.items() if v is not None)
    atomic_write(d, conf_file.name, ("\n".join(out) + "\n").encode("utf-8"))
    _conf_cache.pop(str(d), None)


def _conf_value(v: str) -> str:
    return f'"{v}"' if "#" in v or ";" in v else v


def atomic_write(d: Path, name: str, data: bytes) -> None:
    tmp = d / f".{name}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, d / name)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise HTTPException(500, tr(f"Não foi possível gravar {name}: {exc.strerror}", f"Could not write {name}: {exc.strerror}")) from None


def backup_file(d: Path, name: str) -> None:
    """Renomeia o arquivo para .<nome>.bak (oculto no hub) em vez de apagá-lo."""
    try:
        os.replace(d / name, d / f".{name}.bak")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise HTTPException(500, tr(f"Não foi possível mover {name}: {exc.strerror}", f"Could not move {name}: {exc.strerror}")) from None


def sniff_image(head: bytes) -> str | None:
    """Extensão a partir dos magic bytes (SVG fica de fora de propósito)."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis", b"mif1"):
        return ".avif"
    return None


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

    def invalidate(self) -> None:
        with self._lock:
            self._entries = None

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
# Duração e miniatura dos vídeos (ffprobe/ffmpeg, opcionais)
# --------------------------------------------------------------------------- #

PROBE_PER_CALL = 200  # vídeos medidos por chamada de /api/durations (o front pede o resto)


def default_cache_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "coursevault"


class MediaInfo:
    """Cache em disco fora da biblioteca (nada é gravado nas pastas dos cursos). A chave
    inclui tamanho e data de modificação: arquivo trocado é medido de novo."""

    def __init__(self) -> None:
        self.ffprobe: str | None = None
        self.ffmpeg: str | None = None
        self.thumbs: Path | None = None
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ffprobe")
        self._thumb_slots = asyncio.Semaphore(2)  # no máximo 2 ffmpeg gerando miniaturas

    def setup(self, cache_dir: Path) -> None:
        ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
        if not (ffprobe or ffmpeg):
            return
        try:
            (cache_dir / "thumbs").mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(cache_dir / "media.sqlite3", check_same_thread=False)
            db.execute("CREATE TABLE IF NOT EXISTS duration (key TEXT PRIMARY KEY, seconds REAL)")
            db.commit()
        except (OSError, sqlite3.Error) as exc:
            print(tr(f"⚠  Cache de mídia desativado ({cache_dir}): {exc}", f"⚠  Media cache disabled ({cache_dir}): {exc}"))
            return
        self.ffprobe, self.ffmpeg, self.thumbs, self._db = ffprobe, ffmpeg, cache_dir / "thumbs", db

    @staticmethod
    def key(p: Path) -> str | None:
        try:
            st = p.stat()
            return f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}"
        except (OSError, RuntimeError):
            return None

    def cached(self, keys: list[str]) -> dict[str, float]:
        out: dict[str, float] = {}
        if not self._db:
            return out
        with self._lock:
            for i in range(0, len(keys), 500):
                chunk = keys[i:i + 500]
                q = f"SELECT key, seconds FROM duration WHERE key IN ({','.join('?' * len(chunk))})"
                out.update(self._db.execute(q, chunk).fetchall())
        return out

    def _store(self, rows: list[tuple[str, float]]) -> None:
        if rows and self._db:
            with self._lock:
                self._db.executemany("INSERT OR REPLACE INTO duration VALUES (?, ?)", rows)
                self._db.commit()

    def _probe(self, p: Path) -> float:
        """Duração em segundos; -1 quando o ffprobe não consegue ler (não tenta de novo)."""
        try:
            r = subprocess.run(
                [self.ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", "file:" + str(p)],  # "file:" evita protocolos do ffmpeg
                capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
            return max(float(r.stdout.strip().splitlines()[0]), 0.0)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return -1.0

    def durations(self, files: list[Path], probe_limit: int) -> tuple[dict[Path, float], int]:
        """(duração conhecida de cada arquivo, quantos ainda faltam medir)."""
        keys = {f: self.key(f) for f in files}
        known = self.cached([k for k in keys.values() if k])
        out = {f: known[k] for f, k in keys.items() if k in known}
        missing = [f for f, k in keys.items() if k and k not in known]
        todo = missing[:probe_limit] if self.ffprobe else []
        if todo:
            secs = list(self._pool.map(self._probe, todo))
            self._store([(keys[f], s) for f, s in zip(todo, secs)])
            out.update(zip(todo, secs))
        return out, len(missing) - len(todo) if self.ffprobe else 0

    async def thumbnail(self, f: Path) -> Path | None:
        k = self.key(f)
        if not (k and self.ffmpeg and self.thumbs):
            return None
        name = hashlib.sha1(k.encode()).hexdigest()
        out, failed = self.thumbs / f"{name}.jpg", self.thumbs / f"{name}.fail"
        if out.is_file():
            return out
        if failed.exists():
            return None
        async with self._thumb_slots:
            if not out.is_file():
                await run_in_threadpool(self._make_thumb, f, out, failed)
        return out if out.is_file() else None

    def _make_thumb(self, f: Path, out: Path, failed: Path) -> None:
        dur = self.durations([f], 1)[0].get(f, -1)
        tmp = out.with_name(f"{out.stem}.{os.getpid()}.{threading.get_ident()}.tmp.jpg")
        # um quadro a 10% do vídeo (fora de vinhetas/telas pretas); se falhar, o primeiro
        for ss in ([min(max(dur * 0.1, 1.0), 120.0)] if dur > 0 else [5.0]) + [0.0]:
            try:
                subprocess.run(
                    [self.ffmpeg, "-nostdin", "-v", "error", "-ss", f"{ss:.2f}", "-i", "file:" + str(f),
                     "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", "-f", "image2", "-y", str(tmp)],
                    capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                if tmp.is_file() and tmp.stat().st_size > 0:
                    os.replace(tmp, out)
                    return
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
            failed.touch()
        except OSError:
            pass


MEDIA = MediaInfo()


# --------------------------------------------------------------------------- #
# Envio de arquivos com suporte a HTTP Range (seek suave em vídeos)
# --------------------------------------------------------------------------- #

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
# Código-fonte web: exibido como texto puro, nunca interpretado.
_ACTIVE_TYPES = {".html", ".htm", ".xhtml", ".xht", ".shtml", ".xml", ".js", ".mjs"}
SVG_MIME = "image/svg+xml"


def guess_mime(name: str) -> tuple[str, bool]:
    """(Content-Type, pode ser exibido inline). Lista de permissão: o mimetypes do sistema
    mapeia dezenas de extensões para HTML/XML/SVG (.shtml, .xht, .svgz, .rss…), que
    executariam scripts na origem do app; tudo o que não é mídia, imagem, PDF ou texto
    é enviado como download binário."""
    if is_text_name(name) or ext_of(name) in _ACTIVE_TYPES:
        return "text/plain; charset=utf-8", True
    mime = mimetypes.guess_type(name)[0] or ""
    if mime.startswith(("video/", "audio/", "image/")) or mime in ("application/pdf", "text/vtt"):
        return mime, True  # SVG passa daqui, mas sempre com CSP sandbox (send_file)
    return "application/octet-stream", False


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
        raise HTTPException(404, tr("Arquivo não encontrado", "File not found")) from None
    if not path.is_file():
        raise HTTPException(404, tr("Arquivo não encontrado", "File not found"))
    size = st.st_size
    etag = f'"{st.st_mtime_ns:x}-{size:x}"'
    guessed, inline_ok = guess_mime(path.name)
    media_type = media_type or guessed
    download = download or not inline_ok
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(path.name, download),
        "Last-Modified": formatdate(st.st_mtime, usegmt=True),
        "ETag": etag,
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    if media_type.split(";")[0].strip().lower() == SVG_MIME:  # .svg, .svgz…
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"

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

def host_allowed(name: str) -> bool:
    """O ataque de DNS rebinding sempre chega com um nome de domínio no Host (é ele que
    o atacante controla); um IP literal só aparece quando o usuário digitou o IP."""
    if name in ALLOWED_HOSTS:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def lan_host_names() -> set[str]:
    """Nomes desta máquina, para acessar pela LAN (ex.: http://meupc.local:8000)."""
    names = set(LOOPBACK)
    for n in (socket.gethostname(), socket.getfqdn()):
        n = n.strip().lower().rstrip(".")
        if n:
            names |= {n, n.split(".")[0], n.split(".")[0] + ".local"}
    return names


class HostGuard:
    """Bloqueia requisições com Host desconhecido (anti DNS-rebinding) e define o
    idioma das mensagens da requisição."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        lang = (headers.get(b"x-lang") or headers.get(b"accept-language") or b"").decode("latin-1").strip().lower()
        token = _LANG.set("pt" if lang.startswith("pt") else "en") if lang else None
        try:
            await self._guarded(scope, receive, send, headers)
        finally:
            if token is not None:
                _LANG.reset(token)

    async def _guarded(self, scope, receive, send, headers):
        if ALLOWED_HOSTS is not None:
            host = headers.get(b"host", b"").decode("latin-1").lower()
            hostname = host if host.endswith("]") else host.rsplit(":", 1)[0]
            if not host_allowed(hostname.strip("[]")):
                await PlainTextResponse(tr("Host não permitido", "Host not allowed"), status_code=400)(scope, receive, send)
                return
        if AUTH_TOKEN and scope["path"] != "/api/login" and not has_session(headers):
            if scope["path"] == "/" and scope["method"] in ("GET", "HEAD"):
                resp = Response(login_page(), media_type="text/html; charset=utf-8",
                                headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})
            else:
                resp = JSONResponse({"detail": tr("Faça login", "Please log in")}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


def session_token(password: str) -> str:
    return hmac.new(password.encode("utf-8"), b"coursevault-session-v1", hashlib.sha256).hexdigest()


def has_session(headers: dict) -> bool:
    for part in headers.get(b"cookie", b"").decode("latin-1").split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE and hmac.compare_digest(value.encode("latin-1"), (AUTH_TOKEN or "").encode()):
            return True
    return False


class LoginGuard:
    """5 senhas erradas seguidas bloqueiam o endereço por 60 s (contra força bruta na LAN)."""
    MAX_FAILS, BLOCK_SECONDS = 5, 60

    def __init__(self) -> None:
        self._state: dict[str, tuple[int, float]] = {}

    def wait(self, ip: str) -> int:
        _, until = self._state.get(ip, (0, 0.0))
        return max(0, math.ceil(until - time.monotonic()))

    def fail(self, ip: str) -> None:
        n = self._state.get(ip, (0, 0.0))[0] + 1
        self._state[ip] = (0, time.monotonic() + self.BLOCK_SECONDS) if n >= self.MAX_FAILS else (n, 0.0)

    def reset(self, ip: str) -> None:
        self._state.pop(ip, None)


LOGIN_GUARD = LoginGuard()


def login_page() -> str:
    lang = _LANG.get()
    return f"""<!doctype html>
<html lang="{'pt-BR' if lang == 'pt' else 'en'}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>CourseVault</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%238b7cf6'/%3E%3Cpath d='M7 6.5v11l9-5.5z' fill='white'/%3E%3C/svg%3E">
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 16px; box-sizing: border-box;
         background: #0f0f11; color: #d4d4d8; font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }}
  form {{ width: 100%; max-width: 340px; padding: 28px; border: 1px solid #2a2a30; border-radius: 14px; background: #141416; }}
  h1 {{ margin: 0 0 4px; font-size: 20px; color: #fafafa; }}
  p {{ margin: 0 0 20px; font-size: 13px; color: #a1a1aa; }}
  input {{ width: 100%; box-sizing: border-box; height: 42px; padding: 0 12px; border-radius: 9px; border: 1px solid #36363d;
          background: #18181b; color: #fafafa; font-size: 15px; outline: none; }}
  input:focus {{ border-color: #8b7cf6; box-shadow: 0 0 0 3px rgba(139, 124, 246, .2); }}
  button {{ width: 100%; height: 42px; margin-top: 12px; border: 0; border-radius: 9px; background: #8b7cf6; color: #fff;
           font-size: 15px; font-weight: 600; cursor: pointer; }}
  button:hover {{ background: #6d5ce8; }} button:disabled {{ opacity: .5; cursor: default; }}
  #err {{ min-height: 20px; margin: 10px 0 0; color: #fca5a5; font-size: 13px; }}
</style></head>
<body><form id="f" autocomplete="on">
  <h1>CourseVault</h1>
  <p>{tr("Esta biblioteca é protegida por senha.", "This library is password protected.")}</p>
  <input id="pw" type="password" name="password" autocomplete="current-password" autofocus required
         placeholder="{tr('Senha', 'Password')}" aria-label="{tr('Senha', 'Password')}">
  <button id="go" type="submit">{tr("Entrar", "Log in")}</button>
  <div id="err" role="alert"></div>
</form>
<script>
document.getElementById('f').addEventListener('submit', async e => {{
  e.preventDefault();
  const btn = document.getElementById('go'), err = document.getElementById('err');
  btn.disabled = true; err.textContent = '';
  try {{
    const r = await fetch('/api/login', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ password: document.getElementById('pw').value }}) }});
    if (r.ok) {{ location.reload(); return; }}
    const d = await r.json().catch(() => ({{}}));
    err.textContent = d.detail || r.statusText;
  }} catch (x) {{ err.textContent = x.message; }}
  btn.disabled = false;
  document.getElementById('pw').select();
}});
</script></body></html>"""


app = FastAPI(title="CourseVault", docs_url="/api/docs", redoc_url=None)
app.add_middleware(HostGuard)


def hub_title() -> str:
    return load_conf(ROOT, "")["title"] or "CourseVault"


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
        raise HTTPException(404, tr("Pasta não encontrada", "Folder not found"))
    conf = load_conf(d, rel)
    _, stats = INDEX.snapshot()
    try:
        with os.scandir(d) as it:
            items = sorted(it, key=lambda e: natural_key(e.name))
    except OSError as exc:
        raise HTTPException(500, tr(f"Não foi possível ler a pasta: {exc.strerror}", f"Could not read the folder: {exc.strerror}")) from None

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

    crumbs = breadcrumbs(rel)
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
        "hub": {"title": hub_title(), "root": str(ROOT), "writable": not READ_ONLY, "auth": AUTH_TOKEN is not None,
                "media": {"duration": MEDIA.ffprobe is not None, "thumbs": MEDIA.ffmpeg is not None}},
    }


def breadcrumbs(rel: str) -> list[dict]:
    crumbs = [{"title": hub_title(), "path": ""}]
    acc: list[str] = []
    for part in rel.split("/") if rel else []:
        acc.append(part)
        p = "/".join(acc)
        crumbs.append({"title": load_conf(ROOT.joinpath(*acc), p)["title"] or part, "path": p})
    return crumbs


@app.get("/api/tree")
def api_tree(path: str = ""):
    """Subpastas de uma pasta (navegador de pastas do "Mover para…")."""
    d, rel = safe_path(path)
    if not d.is_dir():
        raise HTTPException(404, tr("Pasta não encontrada", "Folder not found"))
    dirs = []
    try:
        with os.scandir(d) as it:
            for e in it:
                if not (visible_entry(e, rel) and entry_is_dir(e)):
                    continue
                child = join_rel(rel, e.name)
                conf = load_conf(Path(e.path), child)
                if conf["hidden"]:
                    continue
                dirs.append({"name": e.name, "path": child, "title": conf["title"] or e.name,
                             "has_dirs": _has_subdirs(Path(e.path), child)})
    except OSError as exc:
        raise HTTPException(500, tr(f"Não foi possível ler a pasta: {exc.strerror}", f"Could not read the folder: {exc.strerror}")) from None
    dirs.sort(key=lambda x: natural_key(x["name"]))
    crumbs = breadcrumbs(rel)
    return {"path": rel, "title": crumbs[-1]["title"], "breadcrumbs": crumbs, "dirs": dirs}


def _has_subdirs(d: Path, rel: str) -> bool:
    try:
        with os.scandir(d) as it:
            return any(visible_entry(e, rel) and entry_is_dir(e) for e in it)
    except OSError:
        return False


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
        raise HTTPException(404, tr("Sem banner", "No cover"))
    return send_file(banner, request)


# --- Escrita (editor de capa) ---------------------------------------------- #
# As rotas usam PUT/PATCH/DELETE: o navegador exige preflight CORS para elas, então
# outro site não consegue chamá-las. O Origin é conferido como segunda barreira.

def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(403, tr("Origem não permitida", "Origin not allowed"))


def check_write(request: Request, json_body: bool = False) -> None:
    if READ_ONLY:
        raise HTTPException(403, tr("Hub em modo somente leitura (--read-only)", "CourseVault is in read-only mode (--read-only)"))
    check_origin(request)
    if json_body and not request.headers.get("content-type", "").lower().startswith("application/json"):
        raise HTTPException(415, "Use Content-Type: application/json")


def writable_dir(path: str) -> tuple[Path, str]:
    d, rel = safe_path(path)
    if not rel or not d.is_dir():
        raise HTTPException(400, tr("Escolha a pasta de um curso", "Pick a course folder"))
    require_inside_root(d)
    return d, rel


def _save_banner(d: Path, rel: str, data: bytes, ext: str) -> dict:
    name = "banner" + ext
    current = load_conf(d, rel)["_banner_name"]
    for old in {current, name}:
        if old and (d / old).exists():
            backup_file(d, old)
    atomic_write(d, name, data)
    update_conf(d, {"banner": name})
    return public_conf(load_conf(d, rel))


def _set_conf(d: Path, rel: str, updates: dict[str, str | None]) -> dict:
    if "banner" in updates:  # a capa local deixa de ser usada: guarda como .bak
        current = load_conf(d, rel)["_banner_name"]
        if current:
            backup_file(d, current)
    update_conf(d, updates)
    return public_conf(load_conf(d, rel))


@app.put("/api/banner")
async def api_banner_put(request: Request, path: str):
    check_write(request)
    d, rel = writable_dir(path)
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BANNER_BYTES:
        raise HTTPException(413, tr("Imagem maior que 15 MB", "Image larger than 15 MB"))
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BANNER_BYTES:
            raise HTTPException(413, tr("Imagem maior que 15 MB", "Image larger than 15 MB"))
    ext = sniff_image(bytes(buf[:16]))
    if not ext:
        raise HTTPException(415, tr("Formato não suportado (use JPG, PNG, WebP, GIF ou AVIF)", "Unsupported format (use JPG, PNG, WebP, GIF or AVIF)"))
    return await run_in_threadpool(_save_banner, d, rel, bytes(buf), ext)


@app.patch("/api/conf")
async def api_conf_patch(request: Request, path: str):
    check_write(request)
    d, rel = writable_dir(path)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, tr("JSON inválido", "Invalid JSON")) from None
    if not isinstance(body, dict) or not body or set(body) - {"banner", "banner_pos"}:
        raise HTTPException(400, tr("Campos permitidos: banner, banner_pos", "Allowed fields: banner, banner_pos"))
    updates: dict[str, str | None] = {}
    if "banner" in body:
        url = body["banner"]
        if not isinstance(url, str) or len(url) > 2000 or not re.fullmatch(r"https?://[^\s\"'<>]+", url, re.I):
            raise HTTPException(400, tr("URL inválida (use http:// ou https://)", "Invalid URL (use http:// or https://)"))
        updates["banner"] = url
    if "banner_pos" in body:
        pos = body["banner_pos"]
        if pos is None:
            updates["banner_pos"] = None
        elif isinstance(pos, (int, float)) and not isinstance(pos, bool) and 0 <= pos <= 100:
            updates["banner_pos"] = f"{round(pos, 1):g}"
        else:
            raise HTTPException(400, tr("banner_pos deve ser um número de 0 a 100", "banner_pos must be a number from 0 to 100"))
    return await run_in_threadpool(_set_conf, d, rel, updates)


@app.delete("/api/banner")
def api_banner_delete(request: Request, path: str):
    check_write(request)
    d, rel = writable_dir(path)
    return _set_conf(d, rel, {"banner": "none", "banner_pos": None})


# --- Escrita (organizar pastas e arquivos) --------------------------------- #

_BAD_NAME = re.compile(r'[\\/<>:"|?*\x00-\x1f]')


class MkdirBody(BaseModel):
    parent: str = ""
    name: str


class RenameBody(BaseModel):
    path: str
    name: str


class MoveBody(BaseModel):
    paths: list[str]
    dest: str = ""


class TrashBody(BaseModel):
    paths: list[str]


def valid_name(name: str) -> str:
    name = unicodedata.normalize("NFC", name.strip())
    if not name or name in (".", "..") or name.startswith("."):
        raise HTTPException(400, tr("Nome inválido: não pode ficar vazio nem começar com ponto", "Invalid name: it cannot be empty or start with a dot"))
    if _BAD_NAME.search(name):
        raise HTTPException(400, tr('O nome não pode conter / \\ < > : " | ? *', 'Names cannot contain / \\ < > : " | ? *'))
    if name.endswith("."):
        raise HTTPException(400, tr("O nome não pode terminar com ponto", "Names cannot end with a dot"))
    if len(name.encode("utf-8")) > 255:
        raise HTTPException(400, tr("Nome longo demais", "Name too long"))
    if name in CONF_NAMES or name in IGNORED_NAMES:
        raise HTTPException(400, tr("Nome reservado", "Reserved name"))
    return name


def item_path(path: str) -> tuple[Path, str]:
    """Item que pode ser renomeado/movido/excluído (nunca a raiz nem os arquivos do app)."""
    p, rel = safe_path(path)
    if not rel:
        raise HTTPException(400, tr("A raiz dos cursos não pode ser alterada", "The library root cannot be changed"))
    if HIDE_APP_FILES and "/" not in rel and rel in APP_FILES:
        raise HTTPException(403, tr("Arquivo do próprio Hub", "This is a CourseVault app file"))
    require_inside_root(p.parent)  # o item pode ser um link; a pasta onde ele está, não
    return p, rel


def parent_of(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def companions(p: Path) -> list[Path]:
    """Legendas que pertencem a um vídeo/áudio (aula.srt, aula.pt-BR.vtt). Se houver
    outra mídia com nome mais específico (aula.parte2.mp4), a legenda fica com ela."""
    if classify(p.name) not in ("video", "audio") or not p.is_file():
        return []
    try:
        names = os.listdir(p.parent)
    except OSError:
        return []
    stem = os.path.splitext(p.name)[0]
    media_stems = [os.path.splitext(n)[0] for n in names if classify(n) in ("video", "audio")]
    out = []
    for n in names:
        if ext_of(n) not in SUB_EXT:
            continue
        s = os.path.splitext(n)[0]
        owners = [m for m in media_stems if s == m or s.startswith(m + ".")]
        if owners and max(owners, key=len) == stem:
            out.append(p.parent / n)
    return out


def _same_entry(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)  # renomear só trocando maiúsculas/minúsculas
    except OSError:
        return False


def move_entry(src: Path, dst: Path, allow_copy: bool) -> None:
    """Renomeia/move sem nunca sobrescrever. Entre discos diferentes só copia se permitido."""
    if os.path.lexists(dst) and not _same_entry(src, dst):
        raise HTTPException(409, tr(f"Já existe “{dst.name}” no destino", f"“{dst.name}” already exists in the destination"))
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise HTTPException(500, tr(f"Não foi possível mover “{src.name}”: {exc.strerror}", f"Could not move “{src.name}”: {exc.strerror}")) from None
        if not allow_copy:
            raise HTTPException(409, tr(f"“{src.name}” está em outro disco; mova pelo gerenciador de arquivos", f"“{src.name}” is on another disk; move it with your file manager")) from None
        try:
            shutil.move(str(src), str(dst))
        except OSError as exc2:
            raise HTTPException(500, tr(f"Não foi possível mover “{src.name}”: {exc2.strerror}", f"Could not move “{src.name}”: {exc2.strerror}")) from None


def fs_changed() -> None:
    _conf_cache.clear()
    INDEX.invalidate()


def partial(exc: HTTPException, key: str, done: list) -> JSONResponse:
    """Erro no meio de um lote: devolve também o que já foi feito (o front migra o progresso)."""
    fs_changed()
    return JSONResponse({"detail": exc.detail, key: done}, status_code=exc.status_code)


@app.post("/api/fs/mkdir")
def api_mkdir(request: Request, body: MkdirBody):
    check_write(request, json_body=True)
    parent, parent_rel = safe_path(body.parent)
    if not parent.is_dir():
        raise HTTPException(400, tr("Destino não é uma pasta", "Destination is not a folder"))
    require_inside_root(parent)
    name = valid_name(body.name)
    try:
        (parent / name).mkdir()
    except FileExistsError:
        raise HTTPException(409, tr(f"Já existe “{name}” aqui", f"“{name}” already exists here")) from None
    except OSError as exc:
        raise HTTPException(500, tr(f"Não foi possível criar a pasta: {exc.strerror}", f"Could not create the folder: {exc.strerror}")) from None
    fs_changed()
    return {"path": join_rel(parent_rel, name)}


@app.post("/api/fs/rename")
def api_rename(request: Request, body: RenameBody):
    check_write(request, json_body=True)
    src, rel = item_path(body.path)
    name = valid_name(body.name)
    parent_rel = parent_of(rel)
    moved: list[dict] = []
    if name != src.name:
        subs = companions(src)
        move_entry(src, src.parent / name, allow_copy=False)
        moved.append({"from": rel, "to": join_rel(parent_rel, name)})
        old_stem, new_stem = os.path.splitext(src.name)[0], os.path.splitext(name)[0]
        for s in subs:  # aula.pt-BR.srt -> nova.pt-BR.srt
            new_sub = new_stem + s.name[len(old_stem):]
            try:
                move_entry(s, s.parent / new_sub, allow_copy=False)
                moved.append({"from": join_rel(parent_rel, s.name), "to": join_rel(parent_rel, new_sub)})
            except HTTPException:
                pass  # legenda com conflito de nome fica como está
    fs_changed()
    return {"moved": moved}


@app.post("/api/fs/move")
def api_move(request: Request, body: MoveBody):
    check_write(request, json_body=True)
    dest, dest_rel = safe_path(body.dest)
    if not dest.is_dir():
        raise HTTPException(400, tr("Destino não é uma pasta", "Destination is not a folder"))
    require_inside_root(dest)
    if not body.paths or len(body.paths) > 2000:
        raise HTTPException(400, tr("Nenhum item para mover", "Nothing to move"))
    items = [item_path(p) for p in body.paths]  # valida tudo antes de mexer em qualquer coisa
    for _, rel in items:
        if dest_rel == rel or dest_rel.startswith(rel + "/"):
            raise HTTPException(400, tr(f"Não dá para mover “{rel.rsplit('/', 1)[-1]}” para dentro dele mesmo", f"Cannot move “{rel.rsplit('/', 1)[-1]}” into itself"))
    moved: list[dict] = []
    try:
        for src, rel in items:
            if parent_of(rel) == dest_rel or not os.path.lexists(src):
                continue  # já está no destino, ou foi junto com a pasta/vídeo anterior
            group = [src, *companions(src)]
            for p in group:
                if not os.path.lexists(p):
                    continue
                move_entry(p, dest / p.name, allow_copy=True)
                moved.append({"from": join_rel(parent_of(rel), p.name), "to": join_rel(dest_rel, p.name)})
    except HTTPException as exc:
        if moved:
            return partial(exc, "moved", moved)
        raise
    fs_changed()
    return {"moved": moved}


@app.post("/api/fs/trash")
def api_trash(request: Request, body: TrashBody):
    check_write(request, json_body=True)
    if not body.paths or len(body.paths) > 2000:
        raise HTTPException(400, tr("Nenhum item para excluir", "Nothing to delete"))
    items = [item_path(p) for p in body.paths]
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    base = ROOT / TRASH_DIR / stamp
    trashed: list[str] = []
    try:
        for src, rel in items:
            if not os.path.lexists(src):
                continue
            for p in [src, *companions(src)]:
                r = join_rel(parent_of(rel), p.name)
                if not os.path.lexists(p):
                    continue
                dst = base.joinpath(*r.split("/"))  # preserva o caminho original
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise HTTPException(500, tr(f"Não foi possível criar a lixeira: {exc.strerror}", f"Could not create the trash folder: {exc.strerror}")) from None
                move_entry(p, dst, allow_copy=False)
                trashed.append(r)
    except HTTPException as exc:
        if trashed:
            _manifest_update(base, add=trashed)
            return partial(exc, "trashed", trashed)
        raise
    if trashed:
        _manifest_update(base, add=trashed)
    fs_changed()
    return {"trashed": trashed, "folder": f"{TRASH_DIR}/{stamp}"}


# --- Lixeira (listar, restaurar, excluir de vez) ---------------------------- #

_STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{6}")


def trash_root() -> Path:
    t = ROOT / TRASH_DIR
    if t.is_symlink():  # nunca apagar/restaurar através de um link
        raise HTTPException(403, tr("A .lixeira não pode ser um link simbólico", ".lixeira cannot be a symlink"))
    return t


def _manifest_read(batch: Path) -> list[str] | None:
    try:
        data = json.loads(read_head(batch / TRASH_MANIFEST, 4 * 1024 * 1024))
    except (OSError, ValueError):
        return None
    return [x for x in data if isinstance(x, str)] if isinstance(data, list) else None


def _manifest_update(batch: Path, add: list[str] = (), remove: set[str] = frozenset()) -> None:
    """Lotes do mesmo segundo compartilham a pasta: o manifesto é mesclado, não trocado."""
    items = _manifest_read(batch) or []
    items = [x for x in dict.fromkeys([*items, *add]) if x not in remove]
    try:
        if items:
            atomic_write(batch, TRASH_MANIFEST, json.dumps(items, ensure_ascii=False, indent=1).encode("utf-8"))
        else:
            (batch / TRASH_MANIFEST).unlink(missing_ok=True)
    except (OSError, HTTPException):
        pass  # sem manifesto, a listagem deduz os itens pela estrutura


def _guess_trashed(batch: Path, rel: str = "") -> list[str]:
    """Lotes sem manifesto: desce pelas pastas que ainda existem na biblioteca (eram só o
    caminho até o item); a primeira que não existe mais é o item excluído."""
    out: list[str] = []
    try:
        entries = sorted(os.scandir(batch.joinpath(*rel.split("/")) if rel else batch), key=lambda e: natural_key(e.name))
    except OSError:
        return out
    for e in entries:
        if e.name.startswith("."):
            continue
        child = join_rel(rel, e.name)
        orig = ROOT.joinpath(*child.split("/"))
        if entry_is_dir(e) and not e.is_symlink() and orig.is_dir() and not orig.is_symlink():
            out.extend(_guess_trashed(batch, child))
        else:
            out.append(child)
    return out


def _tree_size(p: Path) -> tuple[int, int]:
    """(bytes, arquivos) sem seguir links."""
    try:
        if p.is_symlink() or not p.is_dir():
            return p.lstat().st_size, 1
    except OSError:
        return 0, 0
    size = files = 0
    for dirpath, _, names in os.walk(p):
        for n in names:
            try:
                size += os.lstat(os.path.join(dirpath, n)).st_size
                files += 1
            except OSError:
                pass
    return size, files


def _prune_empty(start: Path, stop: Path) -> None:
    """Remove pastas vazias de start até stop (inclusive), sem passar de stop."""
    d = start
    while d == stop or stop in d.parents:
        try:
            os.rmdir(d)
        except OSError:
            return
        d = d.parent


def _batch_cleanup(batch: Path) -> None:
    try:
        left = [n for n in os.listdir(batch) if n != TRASH_MANIFEST]
    except OSError:
        return
    if not left:
        (batch / TRASH_MANIFEST).unlink(missing_ok=True)
        _prune_empty(batch, batch)


@app.get("/api/trash")
def api_trash_list():
    t = trash_root()
    batches: list[dict] = []
    try:
        names = sorted((n for n in os.listdir(t) if _STAMP_RE.fullmatch(n) and (t / n).is_dir()), reverse=True)
    except OSError:
        names = []
    total = 0
    for name in names:
        b = t / name
        manifest = _manifest_read(b)
        rels = [r for r in manifest if os.path.lexists(b.joinpath(*r.split("/")))] if manifest is not None else []
        if not rels:  # sem manifesto, ou manifesto desatualizado (mexeram na pasta à mão)
            rels = _guess_trashed(b)
        # legendas aparecem junto do vídeo, não como itens soltos
        subs: dict[str, list[str]] = {}
        for r in rels:
            for c in companions(b.joinpath(*r.split("/"))):
                cr = join_rel(parent_of(r), c.name)
                if cr in rels and cr != r:
                    subs.setdefault(r, []).append(cr)
        owned = {c for cs in subs.values() for c in cs}
        items = []
        for r in rels:
            if r in owned:
                continue
            p = b.joinpath(*r.split("/"))
            size, files = _tree_size(p)
            for c in subs.get(r, []):
                size += _tree_size(b.joinpath(*c.split("/")))[0]
            total += size
            is_dir = p.is_dir() and not p.is_symlink()
            items.append({"path": r, "name": r.rsplit("/", 1)[-1], "kind": "dir" if is_dir else classify(p.name),
                          "size": size, "files": files if is_dir else None, "subtitles": len(subs.get(r, [])),
                          "conflict": os.path.lexists(ROOT.joinpath(*r.split("/")))})
        if items:
            batches.append({"batch": name, "items": items})
    return {"folder": TRASH_DIR, "batches": batches, "size": total}


class TrashRef(BaseModel):
    batch: str
    path: str


class TrashOpBody(BaseModel):
    items: list[TrashRef] = []
    all: bool = False


def trash_entry(ref: TrashRef) -> tuple[Path, Path, list[str]]:
    """(pasta do lote, item dentro dela, partes do caminho original), validados."""
    if not _STAMP_RE.fullmatch(ref.batch):
        raise HTTPException(400, tr("Lote inválido", "Invalid trash batch"))
    parts = [p for p in ref.path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or "\x00" in ref.path or any(p == ".." or is_hidden(p) for p in parts):
        raise HTTPException(400, tr("Caminho inválido", "Invalid path"))
    batch = trash_root() / ref.batch
    p = batch.joinpath(*parts)
    if not os.path.lexists(p):
        raise HTTPException(404, tr(f"“{parts[-1]}” não está mais na lixeira", f"“{parts[-1]}” is no longer in the trash"))
    try:
        parent_real = p.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(404, tr("Não encontrado", "Not found")) from None
    if not is_within(parent_real, batch.resolve()):
        raise HTTPException(403, tr("Acesso negado", "Access denied"))
    return batch, p, parts


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()


@app.post("/api/trash/restore")
def api_trash_restore(request: Request, body: TrashOpBody):
    check_write(request, json_body=True)
    if not body.items or len(body.items) > 2000:
        raise HTTPException(400, tr("Nenhum item para restaurar", "Nothing to restore"))
    entries = [trash_entry(ref) for ref in body.items]  # valida tudo antes de mexer
    restored: list[str] = []
    try:
        for batch, p, parts in entries:
            rel = "/".join(parts)
            if HIDE_APP_FILES and len(parts) == 1 and rel in APP_FILES:
                raise HTTPException(403, tr("Arquivo do próprio Hub", "This is a CourseVault app file"))
            dst = ROOT.joinpath(*parts)
            anchor = dst.parent  # a pasta de destino (ou o ancestral que ainda existe) fica na raiz
            while not anchor.exists() and anchor != ROOT:
                anchor = anchor.parent
            require_inside_root(anchor)
            subs = companions(p)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(500, tr(f"Não foi possível recriar a pasta: {exc.strerror}", f"Could not recreate the folder: {exc.strerror}")) from None
            move_entry(p, dst, allow_copy=False)
            done = [rel]
            for s in subs:  # legendas voltam junto; se houver conflito, ficam na lixeira
                try:
                    move_entry(s, dst.parent / s.name, allow_copy=False)
                    done.append(join_rel(parent_of(rel), s.name))
                except HTTPException:
                    pass
            restored.extend(done)
            _manifest_update(batch, remove=set(done))
            _prune_empty(p.parent, batch)
            _batch_cleanup(batch)
    except HTTPException as exc:
        if restored:
            return partial(exc, "restored", restored)
        raise
    fs_changed()
    return {"restored": restored}


@app.post("/api/trash/delete")
def api_trash_delete(request: Request, body: TrashOpBody):
    """Exclui de vez (sem volta): os itens indicados ou, com all=true, a lixeira inteira."""
    check_write(request, json_body=True)
    t = trash_root()
    try:
        if body.all:
            if t.exists():
                shutil.rmtree(t)
            return {"deleted": "all"}
        if not body.items or len(body.items) > 2000:
            raise HTTPException(400, tr("Nenhum item para excluir", "Nothing to delete"))
        deleted: list[str] = []
        for batch, p, parts in [trash_entry(ref) for ref in body.items]:
            rel = "/".join(parts)
            for x in [p, *companions(p)]:
                _remove(x)
                deleted.append(join_rel(parent_of(rel), x.name))
            _manifest_update(batch, remove=set(deleted))
            _prune_empty(p.parent, batch)
            _batch_cleanup(batch)
        return {"deleted": deleted}
    except OSError as exc:
        raise HTTPException(500, tr(f"Não foi possível excluir: {exc.strerror}", f"Could not delete: {exc.strerror}")) from None


@app.get("/api/text")
def api_text(path: str):
    f, rel = safe_path(path)
    if not f.is_file():
        raise HTTPException(404, tr("Arquivo não encontrado", "File not found"))
    size = f.stat().st_size
    with open(f, "rb") as fh:
        raw = fh.read(MAX_TEXT_BYTES)
    if b"\x00" in raw[:8192] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise HTTPException(415, tr("Arquivo binário — use o download", "Binary file — use download"))
    return {"name": f.name, "path": rel, "ext": ext_of(f.name), "size": size,
            "truncated": size > MAX_TEXT_BYTES, "content": decode_bytes(raw)}


@app.get("/api/subtitle")
def api_subtitle(path: str):
    f, _ = safe_path(path)
    if not f.is_file() or ext_of(f.name) not in SUB_EXT:
        raise HTTPException(404, tr("Legenda não encontrada", "Subtitle not found"))
    text = decode_bytes(read_head(f, 5 * 1024 * 1024)).replace("\r\n", "\n").replace("\r", "\n")
    if ext_of(f.name) == ".srt":
        text = re.sub(r"(\d+):(\d{2}):(\d{2}),(\d{3})",
                      lambda m: f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3)}.{m.group(4)}", text)
        text = "WEBVTT\n\n" + text
    elif not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text
    return Response(text, media_type="text/vtt; charset=utf-8")


@app.get("/api/durations")
def api_durations(path: str = "", probe: bool = True):
    """Duração das aulas da pasta, total de cada subpasta e da pasta inteira. Com probe=0
    só devolve o que já está em cache (usado na raiz, para não medir a biblioteca toda).
    "pending" > 0: ainda há vídeos para medir; o front chama de novo."""
    d, rel = safe_path(path)
    if not MEDIA.ffprobe:
        return {"enabled": False}
    entries, _ = INDEX.snapshot()
    prefix = rel + "/" if rel else ""
    media = [e for e in entries if e["type"] in ("video", "audio") and e["path"].startswith(prefix)]
    media.sort(key=lambda e: e["parent"] != rel)  # mede primeiro as aulas que estão na tela
    files = {e["path"]: ROOT.joinpath(*e["path"].split("/")) for e in media}
    secs, pending = MEDIA.durations(list(files.values()), PROBE_PER_CALL if probe else 0)

    def bucket() -> dict:
        return {"seconds": 0.0, "count": 0, "known": 0}
    total, dirs, out_files = bucket(), {}, {}
    for e in media:
        s = secs.get(files[e["path"]])
        child = e["path"][len(prefix):].split("/", 1)[0]
        targets = [total] + ([dirs.setdefault(join_rel(rel, child), bucket())] if e["parent"] != rel else [])
        for b in targets:
            b["count"] += 1
            if s is not None:
                b["known"] += 1
                b["seconds"] += max(s, 0.0)
        if e["parent"] == rel and s is not None and s > 0:
            out_files[e["path"]] = round(s, 1)
    return {"enabled": True, "path": rel, "files": out_files, "dirs": dirs, "total": total, "pending": pending}


@app.get("/api/thumb")
async def api_thumb(request: Request, path: str):
    f, _ = safe_path(path)
    if not MEDIA.ffmpeg or classify(f.name) != "video" or not f.is_file():
        raise HTTPException(404, tr("Sem miniatura", "No thumbnail"))
    thumb = await MEDIA.thumbnail(f)
    if thumb is None:
        raise HTTPException(404, tr("Sem miniatura", "No thumbnail"))
    return send_file(thumb, request, media_type="image/jpeg")


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
async def api_login(request: Request, body: LoginBody):
    check_origin(request)
    if not AUTH_TOKEN:
        return {"ok": True}
    ip = request.client.host if request.client else ""
    wait = LOGIN_GUARD.wait(ip)
    if wait:
        raise HTTPException(429, tr(f"Muitas tentativas. Tente de novo em {wait} s", f"Too many attempts. Try again in {wait} s"))
    if not hmac.compare_digest(session_token(body.password), AUTH_TOKEN):
        LOGIN_GUARD.fail(ip)
        await asyncio.sleep(1)
        raise HTTPException(401, tr("Senha incorreta", "Wrong password"))
    LOGIN_GUARD.reset(ip)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(SESSION_COOKIE, AUTH_TOKEN, max_age=SESSION_MAX_AGE, httponly=True, samesite="strict", path="/")
    return resp


@app.post("/api/logout")
def api_logout(request: Request):
    check_origin(request)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return resp


@app.post("/api/refresh")
def api_refresh(request: Request):
    check_origin(request)  # reindexar a árvore toda é caro: outro site não pode disparar
    _conf_cache.clear()
    entries, stats = INDEX.snapshot(force=True)
    return {"ok": True, "entries": len(entries), "folders": len(stats)}


@app.get("/", include_in_schema=False)
def index():
    if not INDEX_HTML.is_file():
        return PlainTextResponse(tr(f"index.html não encontrado em {APP_DIR}", f"index.html not found in {APP_DIR}"), status_code=500)
    return FileResponse(INDEX_HTML, media_type="text/html", headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #

def main() -> None:
    global FOLLOW_SYMLINKS, ALLOWED_HOSTS, READ_ONLY, AUTH_TOKEN, SESSION_COOKIE
    ap = argparse.ArgumentParser(description=tr("CourseVault — portal local para suas pastas de cursos.",
                                             "CourseVault — a local portal for your course folders."))
    ap.add_argument("-r", "--root", default=os.environ.get("HUB_ROOT", "."),
                    help=tr("pasta raiz dos cursos (padrão: pasta atual)", "courses root folder (default: current folder)"))
    ap.add_argument("--host", default="127.0.0.1",
                    help=tr("interface de rede (use 0.0.0.0 para acessar de outros dispositivos da LAN)", "network interface (use 0.0.0.0 to allow other devices on the LAN)"))
    ap.add_argument("-p", "--port", type=int, default=8000, help=tr("porta (padrão: 8000)", "port (default: 8000)"))
    ap.add_argument("--no-browser", action="store_true", help=tr("não abrir o navegador automaticamente", "do not open the browser automatically"))
    ap.add_argument("--follow-symlinks", action="store_true",
                    help=tr("permitir links simbólicos que apontam para fora da raiz (ex.: HD externo)", "allow symlinks that point outside the root (e.g. external drive)"))
    ap.add_argument("--ttl", type=float, default=30.0,
                    help=tr("segundos de cache do índice de busca/contagens (padrão: 30)", "seconds to cache the search index/counts (default: 30)"))
    ap.add_argument("--allow-host", action="append", default=[], metavar=tr("NOME", "NAME"),
                    help=tr("nome extra aceito no header Host em modo LAN (repetível; '*' aceita qualquer um)",
                            "extra name accepted in the Host header in LAN mode (repeatable; '*' accepts any)"))
    ap.add_argument("--read-only", action="store_true",
                    help=tr("desativa a edição (capa, criar, renomear, mover e excluir)", "disable editing (cover, create, rename, move and delete)"))
    ap.add_argument("--password", action="store_true",
                    help=tr("exige senha para abrir o hub (pedida no terminal, ou na variável COURSEVAULT_PASSWORD)",
                            "require a password to open CourseVault (asked in the terminal, or set COURSEVAULT_PASSWORD)"))
    ap.add_argument("--no-ffmpeg", action="store_true",
                    help=tr("não usar ffprobe/ffmpeg (duração e miniaturas dos vídeos)", "do not use ffprobe/ffmpeg (video durations and thumbnails)"))
    args = ap.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        ap.error(tr(f"pasta raiz não existe: {root}", f"root folder does not exist: {root}"))
    set_root(root)
    FOLLOW_SYMLINKS = args.follow_symlinks
    READ_ONLY = READ_ONLY or args.read_only
    INDEX.ttl = args.ttl
    if args.host in LOOPBACK:
        ALLOWED_HOSTS = set(LOOPBACK)
    else:
        print(tr("⚠  Servidor exposto na rede: qualquer dispositivo da LAN poderá ler os arquivos da raiz.", "⚠  Server exposed to the network: any device on the LAN can read the files under the root."))
        extra = {h.strip().lower().strip("[]") for h in args.allow_host if h.strip()}
        if "*" in extra:
            ALLOWED_HOSTS = None
            print(tr("⚠  --allow-host '*': sem proteção contra DNS rebinding (qualquer site aberto na LAN pode acessar o hub).",
                     "⚠  --allow-host '*': no DNS-rebinding protection (any website opened on the LAN can reach CourseVault)."))
        else:
            ALLOWED_HOSTS = lan_host_names() | extra
            print(tr(f"   Acesse pelo IP ou por: {', '.join(sorted(ALLOWED_HOSTS - LOOPBACK))} (outros nomes: --allow-host)",
                     f"   Use the IP address or: {', '.join(sorted(ALLOWED_HOSTS - LOOPBACK))} (other names: --allow-host)"))

    password = os.environ.get("COURSEVAULT_PASSWORD") or None
    if args.password and not password:
        if not sys.stdin.isatty():
            ap.error(tr("--password sem terminal: defina COURSEVAULT_PASSWORD", "--password without a terminal: set COURSEVAULT_PASSWORD"))
        try:
            password = getpass.getpass(tr("Senha do hub: ", "CourseVault password: "))
            if not password or password != getpass.getpass(tr("Repita a senha: ", "Repeat the password: ")):
                ap.error(tr("as senhas não conferem (ou estão vazias)", "passwords do not match (or are empty)"))
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
    if password:
        AUTH_TOKEN = session_token(password)
        SESSION_COOKIE = f"cv_session_{args.port}"  # cookies não separam portas: um por instância
        print(tr("🔒 Senha ativada: é preciso entrar para usar o hub.", "🔒 Password enabled: log in to use CourseVault."))
    elif args.host not in LOOPBACK:
        print(tr("   Dica: use --password para exigir senha na LAN.", "   Tip: use --password to require a password on the LAN."))
    if not args.no_ffmpeg:
        MEDIA.setup(default_cache_dir())

    threading.Thread(target=INDEX.snapshot, daemon=True).start()  # pré-aquece o índice
    shown_host = "localhost" if args.host in LOOPBACK or args.host == "0.0.0.0" else args.host
    url = f"http://{shown_host}:{args.port}/"
    print(tr(f"📚 Hub de Cursos\n   Raiz: {ROOT}\n   URL:  {url}\n   Ctrl+C para encerrar.", f"📚 CourseVault\n   Root: {ROOT}\n   URL:  {url}\n   Press Ctrl+C to stop."))
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
