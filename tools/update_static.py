#!/usr/bin/env python3
"""
Gera a pasta static/ para o CourseVault funcionar sem internet:

    python tools/update_static.py            # baixa o que faltar e gera static/app.css
    python tools/update_static.py --css      # só gera o CSS de novo (após mudar classes no index.html)

- highlight.js, marked e DOMPurify vêm do cdnjs, conferidos pelo hash SRI;
- as fontes Inter e JetBrains Mono vêm dos pacotes Fontsource (npm), conferidos pelo hash
  do registro, com a licença OFL ao lado;
- o CSS é gerado com o binário standalone do Tailwind v3.4.16 (já traz o plugin
  typography; não precisa de Node), conferido pelo SHA-256 publicado no release. Ele fica em
  cache em ~/.cache/coursevault-build.

Só a biblioteca padrão do Python. Para trocar uma versão, atualize a URL e o hash abaixo.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
STATIC = PROJECT / "static"
TOOLS = PROJECT / "tools"

CDNJS = "https://cdnjs.cloudflare.com/ajax/libs/"
VENDOR = {  # destino em static/vendor -> (URL, SRI sha384)
    "atom-one-dark.min.css": ("highlight.js/11.9.0/styles/atom-one-dark.min.css",
                              "sha384-oaMLBGEzBOJx3UHwac0cVndtX5fxGQIfnAeFZ35RTgqPcYlbprH9o9PUV/F8Le07"),
    "highlight.min.js": ("highlight.js/11.9.0/highlight.min.js",
                         "sha384-F/bZzf7p3Joyp5psL90p/p89AZJsndkSoGwRpXcZhleCWhd8SnRuoYo4d0yirjJp"),
    "x86asm.min.js": ("highlight.js/11.9.0/languages/x86asm.min.js",
                      "sha384-eKsHYF6apOh4wN/zUOnYUx5s4086rCTH2ZG0R09R49HLu6/1Kn/6J7Zn6M8++8On"),
    "powershell.min.js": ("highlight.js/11.9.0/languages/powershell.min.js",
                          "sha384-LWJZQx0dgGhEK7snfNYrQs5K+QKD1sOmE02sOQCz4br9UmqSJDvPLoUVFaUyFnjq"),
    "dos.min.js": ("highlight.js/11.9.0/languages/dos.min.js",
                   "sha384-6+/BHm9a7L7KFZE/myAKBddEjwfPovVtBzDrEu/6VqocfSqvRMyM+Lf3k9n89yGH"),
    "marked.min.js": ("marked/12.0.2/marked.min.js",
                      "sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi"),
    "purify.min.js": ("dompurify/3.1.6/purify.min.js",
                      "sha384-+VfUPEb0PdtChMwmBcBmykRMDd+v6D/oFmB3rZM/puCMDYcIvF968OimRh4KQY9a"),
}

FONTS = {  # família no CSS -> (pacote npm, versão, integridade sha512, prefixo dos arquivos)
    "Inter": ("@fontsource-variable/inter", "5.3.0",
              "sha512-OupL48va4JNofb97w6NYeF9S7W/kHNKM0Er8Dem5nqi4jeOLrVJDoE8tZEpnMJmtkvNbB1EIPPwHcdkF6b1oUA==",
              "inter"),
    "JetBrains Mono": ("@fontsource-variable/jetbrains-mono", "5.3.0",
                       "sha512-F32xpS2NsGYoQi2ADSkKTgpJj7ozajsGgDJ8woTnqjmIB+dxDIqImjl4pXZVEExu8UFZ2ndhmX18EBS/hdz3Lw==",
                       "jetbrains-mono"),
}
FONT_SUBSETS = ("latin", "latin-ext")

TAILWIND_VERSION = "3.4.16"
TAILWIND_SHA256 = {
    "linux-x64": "33f254b54c8754f16efbe2be1de38ca25192630dc36f164595a770d4bbf4d893",
    "linux-arm64": "1e6746bba6f3d34d7550889a1a009ab90ee3794a5ebce60ed10688ad10680a87",
    "macos-x64": "220962a6f371fc31605f89569ad647309cbd83471cd8c29b83f235a501c39dce",
    "macos-arm64": "01751c6019c1b4bf787d2e0b1f221bef1bcc010cef55313fc0691f3b6a3b676f",
    "windows-x64.exe": "",  # sem hash fixado: preencha a partir do sha256sums.txt do release se usar
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "coursevault-update-static"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def check_sri(data: bytes, sri: str, what: str) -> None:
    algo, _, expected = sri.partition("-")
    got = base64.b64encode(hashlib.new(algo, data).digest()).decode()
    if got != expected:
        sys.exit(f"✗ hash não confere: {what}\n  esperado {sri}\n  recebido {algo}-{got}")


def write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def vendor(force: bool) -> None:
    for name, (rel, sri) in VENDOR.items():
        dst = STATIC / "vendor" / name
        if dst.is_file() and not force:
            check_sri(dst.read_bytes(), sri, str(dst))
            continue
        data = fetch(CDNJS + rel)
        check_sri(data, sri, rel)
        write(dst, data)
        print(f"✓ vendor/{name}")


def fonts(force: bool) -> str:
    """Extrai os .woff2 e devolve as regras @font-face (com os nomes usados no app)."""
    rules: list[str] = []
    for family, (pkg, version, integrity, prefix) in FONTS.items():
        files = {sub: STATIC / "fonts" / f"{prefix}-{sub}.woff2" for sub in FONT_SUBSETS}
        license_file = STATIC / "fonts" / f"{prefix}-OFL.txt"
        css_cache = STATIC / "fonts" / f".{prefix}.css"  # CSS original do pacote (para os unicode-range)
        if force or not all(f.is_file() for f in [*files.values(), license_file, css_cache]):
            tgz = fetch(f"https://registry.npmjs.org/{pkg}/-/{pkg.rsplit('/', 1)[1]}-{version}.tgz")
            check_sri(tgz, integrity, f"{pkg}@{version}")
            with tarfile.open(fileobj=io.BytesIO(tgz)) as tar:
                def member(name: str) -> bytes:
                    return tar.extractfile(f"package/{name}").read()
                for sub, f in files.items():
                    write(f, member(f"files/{prefix}-{sub}-wght-normal.woff2"))
                write(license_file, member("LICENSE"))
                write(css_cache, member("index.css"))
            print(f"✓ fonts/{prefix} ({pkg}@{version})")
        css = css_cache.read_text(encoding="utf-8")
        for sub in FONT_SUBSETS:
            m = re.search(rf"/\* {re.escape(prefix)}-{re.escape(sub)}-wght-normal \*/\s*@font-face\s*{{(.*?)}}", css, re.S)
            if not m:
                sys.exit(f"✗ @font-face de {prefix}-{sub} não encontrado no CSS do pacote")
            weight = re.search(r"font-weight:\s*([^;]+);", m.group(1)).group(1).strip()
            urange = re.search(r"unicode-range:\s*([^;]+);", m.group(1)).group(1).strip()
            rules.append(f"@font-face{{font-family:'{family}';font-style:normal;font-display:swap;"
                         f"font-weight:{weight};src:url(fonts/{prefix}-{sub}.woff2) format('woff2');"
                         f"unicode-range:{urange}}}")
    return "\n".join(rules) + "\n"


def tailwind_binary() -> Path:
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system())
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    asset = f"{system}-{arch}" + (".exe" if system == "windows" else "")
    expected = TAILWIND_SHA256.get(asset)
    if not system or not arch or expected is None:
        sys.exit(f"✗ plataforma sem binário do Tailwind: {platform.system()} {platform.machine()}")
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "coursevault-build"
    exe = cache / f"tailwindcss-{TAILWIND_VERSION}-{asset}"
    if exe.is_file() and expected and hashlib.sha256(exe.read_bytes()).hexdigest() == expected:
        return exe
    url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/v{TAILWIND_VERSION}/tailwindcss-{asset}"
    print(f"↓ Tailwind CLI {TAILWIND_VERSION} ({asset})…")
    data = fetch(url)
    got = hashlib.sha256(data).hexdigest()
    if expected and got != expected:
        sys.exit(f"✗ SHA-256 do Tailwind CLI não confere\n  esperado {expected}\n  recebido {got}")
    write(exe, data)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def build_css(font_rules: str) -> None:
    out = STATIC / "app.css"
    tmp = STATIC / ".app.tailwind.css"
    subprocess.run([str(tailwind_binary()), "-c", str(TOOLS / "tailwind.config.js"), "-i", str(TOOLS / "tailwind.css"),
                    "-o", str(tmp), "--minify"], cwd=PROJECT, check=True)
    write(out, font_rules.encode() + tmp.read_bytes())
    tmp.unlink()
    print(f"✓ app.css ({out.stat().st_size // 1024} KB)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Gera static/ (CSS, fontes e bibliotecas) para uso offline.")
    ap.add_argument("--css", action="store_true", help="só gera o static/app.css de novo")
    ap.add_argument("--force", action="store_true", help="baixa tudo de novo")
    args = ap.parse_args()
    if not args.css:
        vendor(args.force)
    build_css(fonts(args.force and not args.css))


if __name__ == "__main__":
    main()
