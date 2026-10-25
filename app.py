#!/usr/bin/env python3
"""
Steam Workshop Downloader (local)
---------------------------------
Pega un enlace del Workshop de Steam, elige una carpeta y descarga el mod.

Descarga con DepotDownloader (SteamKit2, la misma red oficial de Steam) y permite
iniciar sesión escaneando un QR con la app móvil de Steam: sin escribir la
contraseña en esta aplicación.

Ejecutar:  python app.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zipfile
from pathlib import Path

import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Rutas y constantes
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = BASE_DIR / "config.json"
VENDOR_DIR = BASE_DIR / "vendor"

DD_VERSION = "3.4.0"
DD_DIR = VENDOR_DIR / "depotdownloader"
DD_EXE = DD_DIR / "DepotDownloader.exe"
DD_ZIP_URL = (
    "https://github.com/SteamRE/DepotDownloader/releases/download/"
    f"DepotDownloader_{DD_VERSION}/DepotDownloader-windows-x64.zip"
)

# Item pequeño y público (Team Fortress 2, gratis) que solo sirve para completar
# el inicio de sesión con QR: así DepotDownloader guarda la sesión del usuario.
LOGIN_APP = "440"
LOGIN_PUBFILE = "155101824"

API_DETAILS = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
API_COLLECTION = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"

DEFAULT_PORT = 8756
MAX_COLLECTION_ITEMS = 200


class UserError(Exception):
    """Error con un mensaje pensado para mostrarse tal cual al usuario."""

    def __init__(self, message: str, needs_auth: bool = False):
        super().__init__(message)
        self.needs_auth = needs_auth


# --------------------------------------------------------------------------- #
# Configuración persistente
# --------------------------------------------------------------------------- #

_config_lock = threading.Lock()
DEFAULT_CONFIG = {
    "dest": "",
    "naming": "title",
    "steam_user": "",
    "recent": [],
}


def load_config() -> dict:
    with _config_lock:
        try:
            raw = json.loads(CONFIG_PATH.read_text("utf-8"))
        except Exception:
            raw = {}
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
    return cfg


def save_config(cfg: dict) -> None:
    with _config_lock:
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), "utf-8")
        except Exception:
            pass


def default_dest() -> str:
    return str(Path.home() / "Downloads" / "SteamMods")


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def expand_path(text: str) -> Path:
    text = (text or "").strip().strip('"').strip()
    return Path(os.path.expandvars(os.path.expanduser(text)))


_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(name: str, fallback: str = "mod") -> str:
    name = _INVALID_CHARS.sub("_", name or "").strip().strip(".")
    if not name or name.upper() in _RESERVED:
        name = fallback
    return name[:80].rstrip(" .") or fallback


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def steam_libraries() -> list[str]:
    roots = [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
    ]
    try:
        import winreg

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        value, _ = winreg.QueryValueEx(key, "SteamPath")
        if value:
            roots.insert(0, Path(value))
    except Exception:
        pass

    found: list[str] = []
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if not vdf.exists():
            continue
        try:
            text = vdf.read_text("utf-8", errors="ignore")
        except Exception:
            continue
        for match in re.finditer(r'"path"\s*"([^"]+)"', text):
            path = match.group(1).replace("\\\\", "\\")
            if path and path not in found and Path(path).exists():
                found.append(path)
    return found


def parse_ref(text: str) -> str | None:
    """Saca el ID del Workshop de un enlace (o de un ID suelto)."""
    text = (text or "").strip()
    m = re.search(r"[?&]id=(\d+)", text) or re.search(r"/filedetails/(\d+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{4,20}", text):
        return text
    return None


# --------------------------------------------------------------------------- #
# API de Steam (pública, sin clave)
# --------------------------------------------------------------------------- #

def fetch_details(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    data = {"itemcount": len(ids)}
    for i, fid in enumerate(ids):
        data[f"publishedfileids[{i}]"] = str(fid)

    try:
        r = requests.post(API_DETAILS, data=data, timeout=30)
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        raise UserError(f"No pude conectar con la API de Steam ({exc.__class__.__name__}).") from exc

    items = payload.get("response", {}).get("publishedfiledetails", []) or []
    out = []
    for it in items:
        if int(it.get("result", 0)) != 1:
            continue
        out.append(
            {
                "fileid": str(it.get("publishedfileid")),
                "appid": int(it.get("consumer_app_id") or it.get("creator_app_id") or 0),
                "title": (it.get("title") or "").strip() or f"Workshop {it.get('publishedfileid')}",
                "size": int(it.get("file_size") or 0),
                "thumb": it.get("preview_url") or "",
            }
        )
    return out


def fetch_collection(cid: str) -> list[str]:
    try:
        r = requests.post(
            API_COLLECTION,
            data={"collectioncount": 1, "publishedfileids[0]": str(cid)},
            timeout=30,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception:
        return []

    details = (payload.get("response", {}).get("collectiondetails") or [{}])[0]
    if int(details.get("result", 0)) != 1:
        return []
    return [
        str(c.get("publishedfileid"))
        for c in details.get("children", [])
        if c.get("publishedfileid")
    ]


# --------------------------------------------------------------------------- #
# Cuenta de Steam
#
# DepotDownloader guarda el token de sesión en "account.config", dentro del
# almacenamiento aislado de .NET (no en su carpeta). Ese archivo es un protobuf
# comprimido con deflate: lo leemos para saber qué cuenta está conectada y así
# poder reutilizar la sesión (login con token, sin volver a escanear el QR).
# --------------------------------------------------------------------------- #

ISOLATED_ROOT = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "IsolatedStorage"


def _account_files() -> list[Path]:
    if not ISOLATED_ROOT.exists():
        return []
    try:
        files = [p for p in ISOLATED_ROOT.rglob("account.config") if p.is_file()]
    except Exception:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while i < len(buf):
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 70:
            break
    raise ValueError("varint inválido")


def _skip_field(buf: bytes, i: int, wire: int) -> int:
    if wire == 0:
        _, i = _read_varint(buf, i)
    elif wire == 1:
        i += 8
    elif wire == 5:
        i += 4
    else:
        raise ValueError("wire type no soportado")
    return i


def _login_tokens(raw: bytes) -> dict[str, str]:
    """Devuelve {usuario: token} del account.config de DepotDownloader."""
    if not raw:
        return {}
    try:
        import zlib

        data = zlib.decompress(raw, -15)  # deflate en crudo
    except Exception:
        data = raw

    tokens: dict[str, str] = {}
    i = 0
    while i < len(data):
        key, i = _read_varint(data, i)
        field, wire = key >> 3, key & 7
        if wire != 2:
            i = _skip_field(data, i, wire)
            continue
        length, i = _read_varint(data, i)
        payload = data[i : i + length]
        i += length
        if field != 4:  # 4 = LoginTokens
            continue

        name = value = ""
        j = 0
        while j < len(payload):
            entry_key, j = _read_varint(payload, j)
            entry_field, entry_wire = entry_key >> 3, entry_key & 7
            if entry_wire != 2:
                j = _skip_field(payload, j, entry_wire)
                continue
            entry_len, j = _read_varint(payload, j)
            chunk = payload[j : j + entry_len]
            j += entry_len
            if entry_field == 1:
                name = chunk.decode("utf-8", "ignore")
            elif entry_field == 2:
                value = chunk.decode("utf-8", "ignore")
        if name:
            tokens[name] = value
    return tokens


def steam_accounts() -> list[str]:
    for path in _account_files():
        try:
            tokens = _login_tokens(path.read_bytes())
        except Exception:
            continue
        if tokens:
            return [u for u in tokens if u]
    return []


def current_steam_user() -> str:
    accounts = steam_accounts()
    return accounts[0] if accounts else ""


def forget_steam_account() -> None:
    for path in _account_files():
        try:
            if _login_tokens(path.read_bytes()):
                path.unlink(missing_ok=True)
        except Exception:
            pass
    cfg = load_config()
    if cfg.get("steam_user"):
        cfg["steam_user"] = ""
        save_config(cfg)


# --------------------------------------------------------------------------- #
# Ejecución de procesos
# --------------------------------------------------------------------------- #

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
PROGRESS_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d+)?)%\s+(.+)$")


def _exec(job: "Job", cmd: list[str], timeout: int, on_line=None) -> tuple[int, list[str]]:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    env = dict(os.environ)
    env.setdefault("DOTNET_NOLOGO", "1")
    env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")

    proc = subprocess.Popen(
        cmd,
        cwd=str(DD_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="cp437",  # DepotDownloader dibuja el QR con bloques CP437 (0xDB)
        errors="replace",
        bufsize=1,
        creationflags=flags,
        env=env,
    )
    job.proc = proc
    lines: list[str] = []

    def handle(line: str) -> None:
        # Ojo: no quitamos los espacios finales; el QR los necesita para su
        # geometría (zona de silencio y columnas del borde).
        clean = ANSI_RE.sub("", line)
        lines.append(clean.rstrip())
        if len(lines) > 400:
            del lines[:-400]
        if on_line:
            try:
                on_line(clean)
            except Exception:
                pass

    def reader():
        buf: list[str] = []
        try:
            while True:
                ch = proc.stdout.read(1)  # type: ignore[union-attr]
                if not ch:
                    break
                if ch in "\r\n":
                    handle("".join(buf))
                    buf.clear()
                else:
                    buf.append(ch)
            handle("".join(buf))
        except Exception:
            pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        thread.join(5)
        job.proc = None
        return -9, lines
    finally:
        if job.proc is proc:
            job.proc = None
    thread.join(10)
    return int(proc.returncode or 0), lines


# --------------------------------------------------------------------------- #
# DepotDownloader: instalación
# --------------------------------------------------------------------------- #

def ensure_downloader(job: "Job") -> None:
    if DD_EXE.exists():
        return

    job.set(phase="preparando", message="Descargando DepotDownloader (solo la primera vez)…", percent=4)
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    DD_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = VENDOR_DIR / "depotdownloader.zip"

    try:
        with requests.get(DD_ZIP_URL, stream=True, timeout=180) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(zip_path, "wb") as fh:
                for chunk in r.iter_content(256 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        job.set(
                            percent=4 + 18 * done / total,
                            message=f"Descargando DepotDownloader… {done * 100 // total}%",
                        )
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(DD_DIR)
    except Exception as exc:
        raise UserError(f"No pude descargar DepotDownloader: {exc}") from exc
    finally:
        zip_path.unlink(missing_ok=True)

    if not DD_EXE.exists():
        raise UserError("DepotDownloader no se instaló correctamente (falta el .exe).")


# --------------------------------------------------------------------------- #
# QR de inicio de sesión
# --------------------------------------------------------------------------- #

QR_LINE_RE = re.compile(r"^[ \u2588]{40,}$")


def _qr_matrix(lines: list[str], module_w: int) -> list[str] | None:
    rows = []
    for line in lines:
        row = []
        for i in range(0, len(line), module_w):
            chunk = line[i : i + module_w]
            row.append("1" if chunk.strip() else "0")
        rows.append("".join(row))
    return rows


def _try_build_qr(buf: list[str]) -> dict | None:
    if len(buf) < 21:
        return None
    for width in (2, 1):
        rows = _qr_matrix(buf, width)
        if not rows:
            continue
        # Quitar la zona de silencio (filas/columnas en blanco de los bordes)
        while rows and "1" not in rows[0]:
            rows.pop(0)
        while rows and "1" not in rows[-1]:
            rows.pop()
        if not rows:
            continue
        while all(r[0] == "0" for r in rows):
            rows = [r[1:] for r in rows]
        while all(r[-1] == "0" for r in rows):
            rows = [r[:-1] for r in rows]
        size = len(rows[0]) if rows else 0
        if size >= 21 and len(rows) == size and all(len(r) == size for r in rows):
            return {"size": size, "rows": rows}
    return None


class QrCollector:
    """Reconstruye el QR que DepotDownloader dibuja en la consola."""

    def __init__(self, job: "Job"):
        self.job = job
        self.buf: list[str] = []

    def feed(self, line: str) -> None:
        if QR_LINE_RE.match(line):
            self.buf.append(line)
            matrix = _try_build_qr(self.buf)
            if matrix:
                self.buf.clear()
                self.job.set(
                    qr=matrix,
                    phase="esperando",
                    message="Esperando confirmación en tu móvil…",
                    indeterminate=True,
                )
        else:
            self.buf.clear()


# --------------------------------------------------------------------------- #
# Trabajos (jobs)
# --------------------------------------------------------------------------- #

_jobs: dict[str, "Job"] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()  # un proceso de Steam a la vez


class Job:
    def __init__(self, ids: list[str], dest: str, naming: str, kind: str = "download"):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.ids = ids
        self.dest = dest
        self.naming = naming
        self.status = "queued"  # queued | running | done | error | cancelled
        self.phase = "En cola"
        self.message = "Esperando turno…"
        self.percent = 0.0
        self.indeterminate = False
        self.qr: dict | None = None
        self.needs_auth = False
        self.error = ""
        self.items: list[dict] = []
        self.folders: list[str] = []
        self.logs: list[str] = []
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.created = time.time()
        self.finished: float | None = None

    def set(self, **kw) -> None:
        for key, value in kw.items():
            setattr(self, key, value)
        if "percent" in kw:
            self.percent = max(0.0, min(100.0, float(self.percent)))

    def log(self, line: str) -> None:
        line = (line or "").strip()
        if not line:
            return
        self.logs.append(line[:400])
        if len(self.logs) > 100:
            del self.logs[:-100]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "percent": round(self.percent, 1),
            "indeterminate": self.indeterminate,
            "elapsed": round((self.finished or time.time()) - self.created, 1),
            "qr": self.qr,
            "needs_auth": self.needs_auth,
            "error": self.error,
            "items": self.items,
            "folders": self.folders,
            "dest": self.dest,
            "logs": self.logs[-40:],
        }


def _register(job: Job) -> None:
    with _jobs_lock:
        _jobs[job.id] = job
        for key, old in list(_jobs.items()):
            if old.finished and time.time() - old.finished > 6 * 3600:
                _jobs.pop(key, None)


def _finish(job: Job) -> None:
    job.finished = time.time()
    job.proc = None


# --------------------------------------------------------------------------- #
# Descarga de un mod
# --------------------------------------------------------------------------- #

def _has_payload(folder: Path) -> bool:
    if not folder.exists():
        return False
    for path in folder.rglob("*"):
        if path.is_file() and ".DepotDownloader" not in path.parts:
            return True
    return False


def _clean_internal(folder: Path) -> None:
    shutil.rmtree(folder / ".DepotDownloader", ignore_errors=True)


def _access_error(text: str) -> bool:
    needles = (
        "is not available from this account",
        "No valid depot key",
        "Unable to get steam3 credentials",
        "InvalidPassword",
        "SteamGuard",
        "not authorized",
        "Access token was rejected",
        "LogOn requires a username and password",
    )
    return any(n.lower() in text.lower() for n in needles)


def download_item(job: Job, item: dict, cfg: dict, base: float, span: float) -> None:
    appid = int(item["appid"])
    fileid = str(item["fileid"])
    title = item["title"]
    if not appid:
        raise UserError("Steam no devolvió la aplicación de este mod, así que no se puede descargar.")

    folder_name = safe_name(title, fileid) if job.naming == "title" else safe_name(fileid)
    target = Path(job.dest) / folder_name

    user = current_steam_user()
    platforms: list[tuple[str, list[str]]] = []
    if user:
        platforms.append((user, ["-username", user, "-remember-password"]))
    platforms.append(("anónimo", []))

    last_text = ""
    for label, auth in platforms:
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        job.set(
            phase="descargando",
            indeterminate=True,
            qr=None,
            message=f"Conectando con Steam ({label})…",
        )

        def on_line(line: str, _title=title) -> None:
            job.log(line)
            match = PROGRESS_RE.match(line)
            if match:
                pct = min(float(match.group(1)), 100.0)
                job.set(
                    indeterminate=False,
                    percent=base + span * pct / 100.0,
                    message=f"Descargando {_title}… {pct:.0f}%",
                )
                return
            low = line.lower()
            if "pre-allocating" in low or "downloading " in low or "validating " in low:
                name = line.split(" ", 1)[-1].strip()
                job.set(message=f"Descargando {_title}… {Path(name).name}")

        args = [
            str(DD_EXE),
            "-app",
            str(appid),
            "-pubfile",
            fileid,
            "-dir",
            str(target),
            "-max-downloads",
            "6",
            *auth,
        ]
        code, lines = _exec(job, args, timeout=7200, on_line=on_line)
        last_text = "\n".join(lines)

        if job.cancelled:
            shutil.rmtree(target, ignore_errors=True)
            raise UserError("Descarga cancelada.")

        if _has_payload(target):
            _clean_internal(target)
            job.folders.append(str(target))
            return

        _clean_internal(target)
        shutil.rmtree(target, ignore_errors=True)

        if code == -9:
            raise UserError("La descarga tardó demasiado y se detuvo. Inténtalo de nuevo.")
        if not _access_error(last_text):
            break

    low = last_text.lower()
    if "unsupported file type" in low:
        raise UserError(f"«{title}» no es un mod descargable (puede ser una guía, arte o vídeo).")
    if "unable to download manifest" in low or "encountered 403" in low or "encountered 401" in low:
        raise UserError(f"Steam no entregó los archivos de «{title}». Vuelve a intentarlo en un rato.")
    if "no username given" in low or _access_error(last_text):
        raise UserError(
            f"«{title}» es de un juego que no permite descargas anónimas. "
            "Inicia sesión con tu cuenta de Steam y vuelve a intentarlo.",
            needs_auth=True,
        )
    raise UserError(f"No se pudo descargar «{title}». Revisa el registro para más detalles.")


def _run_download(job: Job, cfg: dict) -> None:
    job.set(phase="consultando", message="Leyendo datos del Workshop…", percent=1)
    details = fetch_details(job.ids)
    if not details:
        raise UserError("Steam no encontró ese mod (¿está eliminado o es privado?).")

    job.items = [
        {
            "fileid": d["fileid"],
            "appid": d["appid"],
            "title": d["title"],
            "size": d["size"],
            "size_text": human_size(d["size"]) if d["size"] else "—",
        }
        for d in details
    ]

    ensure_downloader(job)
    job.set(phase="descargando", percent=24)

    dest_root = expand_path(job.dest)
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise UserError(f"No puedo crear la carpeta de destino «{dest_root}»: {exc}") from exc

    count = len(details)
    span = (99.0 - 24.0) / count
    for index, item in enumerate(details):
        download_item(job, item, cfg, 24.0 + span * index, span)

    job.set(percent=99.0, message="Terminando…")


def run_download_job(job: Job, cfg: dict) -> None:
    with _run_lock:
        job.status = "running"
        try:
            _run_download(job, cfg)
            job.status = "cancelled" if job.cancelled else "done"
            job.percent = 100.0
            job.phase = "Listo"
            job.message = "Descarga completada."
        except UserError as exc:
            job.status = "cancelled" if job.cancelled else "error"
            job.error = str(exc)
            job.needs_auth = exc.needs_auth
            job.message = "No se pudo descargar."
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"Error inesperado: {exc.__class__.__name__}: {exc}"
            job.message = "No se pudo descargar."
        finally:
            _finish(job)

            if job.status == "done":
                cfg = load_config()
                recent = cfg.get("recent") or []
                recent.insert(
                    0,
                    {
                        "title": ", ".join(i["title"] for i in job.items)[:120],
                        "folder": job.folders[0] if job.folders else job.dest,
                        "when": int(time.time()),
                    },
                )
                cfg["recent"] = recent[:10]
                save_config(cfg)


# --------------------------------------------------------------------------- #
# Inicio de sesión con QR (app móvil de Steam)
# --------------------------------------------------------------------------- #

def run_login_job(job: Job) -> None:
    with _run_lock:
        job.status = "running"
        try:
            ensure_downloader(job)
            job.set(
                phase="conectando",
                message="Conectando con Steam…",
                percent=30,
                indeterminate=True,
            )

            staging = VENDOR_DIR / "login"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)

            qr = QrCollector(job)
            detected = {"user": ""}

            def on_line(line: str) -> None:
                job.log(line)
                qr.feed(line)
                match = re.search(r"-username (\S+) -remember-password", line)
                if match:
                    detected["user"] = match.group(1)

            args = [
                str(DD_EXE),
                "-app",
                LOGIN_APP,
                "-pubfile",
                LOGIN_PUBFILE,
                "-dir",
                str(staging),
                "-max-downloads",
                "2",
                "-qr",
                "-remember-password",
            ]
            _exec(job, args, timeout=300, on_line=on_line)

            user = current_steam_user() or detected["user"]
            if not user:
                if job.cancelled:
                    raise UserError("Inicio de sesión cancelado.")
                raise UserError(
                    "No se completó el inicio de sesión. Vuelve a intentarlo y escanea el código "
                    "con la app de Steam antes de que caduque."
                )

            cfg = load_config()
            cfg["steam_user"] = user
            save_config(cfg)

            job.status = "done"
            job.phase = "Listo"
            job.percent = 100.0
            job.indeterminate = False
            job.qr = None
            job.message = f"Conectado como {user}."
        except UserError as exc:
            job.status = "cancelled" if job.cancelled else "error"
            job.error = str(exc)
            job.message = "No se pudo conectar."
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"Error inesperado: {exc.__class__.__name__}: {exc}"
            job.message = "No se pudo conectar."
        finally:
            shutil.rmtree(VENDOR_DIR / "login", ignore_errors=True)
            _finish(job)


# --------------------------------------------------------------------------- #
# API HTTP
# --------------------------------------------------------------------------- #

app = FastAPI(title="Steam Workshop Downloader", docs_url=None, redoc_url=None)


class ResolveIn(BaseModel):
    input: str = ""


class DownloadIn(BaseModel):
    ids: list[str] = []
    dest: str = ""
    naming: str = "title"


class OpenIn(BaseModel):
    path: str = ""


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


def _steam_state() -> dict:
    user = current_steam_user()
    return {
        "connected": bool(user),
        "user": user,
        "ready": DD_EXE.exists(),
    }


@app.get("/api/config")
def get_config():
    cfg = load_config()
    libs = steam_libraries()
    dest = cfg.get("dest") or default_dest()
    suggestions = [dest, str(Path.home() / "Downloads" / "SteamMods")]
    for lib in libs:
        suggestions.append(str(Path(lib) / "steamapps" / "workshop" / "content"))
    seen, unique = set(), []
    for path in suggestions:
        if path.lower() not in seen:
            seen.add(path.lower())
            unique.append(path)

    return {
        "dest": dest,
        "naming": cfg.get("naming") or "title",
        "libraries": libs,
        "suggestions": unique,
        "steam": _steam_state(),
    }


@app.post("/api/resolve")
def resolve(body: ResolveIn):
    fid = parse_ref(body.input)
    if not fid:
        if re.search(r"/app/\d+/workshop", body.input or ""):
            raise HTTPException(
                400,
                "Ese es el enlace general del Workshop del juego, no de un mod. Abre dentro el mod "
                "que quieras y copia su enlace (el que lleva ?id=…).",
            )
        raise HTTPException(400, "No reconozco el enlace. Pega la URL del mod o su ID numérico.")

    details = fetch_details([fid])
    if details and details[0]["size"] > 0:
        d = details[0]
        return {
            "kind": "item",
            "title": d["title"],
            "count": 1,
            "total_size": d["size"],
            "total_size_text": human_size(d["size"]),
            "items": [
                {
                    "fileid": d["fileid"],
                    "appid": d["appid"],
                    "title": d["title"],
                    "size_text": human_size(d["size"]) if d["size"] else "—",
                    "thumb": d["thumb"],
                }
            ],
        }

    children = fetch_collection(fid)
    if children:
        children = children[:MAX_COLLECTION_ITEMS]
        items = fetch_details(children)
        if not items:
            raise HTTPException(404, "La colección está vacía o sus mods no son accesibles.")
        total = sum(i["size"] for i in items)
        return {
            "kind": "collection",
            "title": (details[0]["title"] if details else "") or "Colección del Workshop",
            "count": len(items),
            "total_size": total,
            "total_size_text": human_size(total),
            "items": [
                {
                    "fileid": i["fileid"],
                    "appid": i["appid"],
                    "title": i["title"],
                    "size_text": human_size(i["size"]) if i["size"] else "—",
                    "thumb": i["thumb"],
                }
                for i in items
            ],
        }

    if details:
        d = details[0]
        raise HTTPException(
            400,
            f"«{d['title']}» no tiene archivos descargables (puede ser una guía, arte o un mod retirado).",
        )
    raise HTTPException(404, "Ese enlace no corresponde a un mod del Workshop.")


@app.post("/api/download")
def start_download(body: DownloadIn):
    ids = [str(i).strip() for i in body.ids if re.fullmatch(r"\d{4,20}", str(i).strip())]
    if not ids:
        raise HTTPException(400, "No hay ningún mod para descargar. Busca el enlace primero.")

    dest_text = (body.dest or "").strip()
    if not dest_text:
        raise HTTPException(400, "Elige una carpeta de destino.")

    dest_path = expand_path(dest_text)
    if dest_path.exists() and not dest_path.is_dir():
        raise HTTPException(400, "La ruta de destino es un archivo, no una carpeta.")
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(400, f"No puedo usar esa carpeta: {exc}") from exc

    cfg = load_config()
    cfg["dest"] = str(dest_path)
    cfg["naming"] = "id" if body.naming == "id" else "title"
    save_config(cfg)

    job = Job(ids, str(dest_path), cfg["naming"])
    _register(job)
    threading.Thread(target=run_download_job, args=(job, cfg), daemon=True).start()
    return {"id": job.id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Ese trabajo ya no existe.")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Ese trabajo ya no existe.")
    job.cancelled = True
    job.set(message="Cancelando…")
    proc = job.proc
    if proc and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    return {"ok": True}


@app.get("/api/steam")
def steam_status():
    return _steam_state()


@app.post("/api/steam/login")
def steam_login():
    for old in list(_jobs.values()):
        if old.kind == "login" and old.status in ("queued", "running"):
            raise HTTPException(409, "Ya hay un inicio de sesión en curso.")
    job = Job([], "", "title", kind="login")
    _register(job)
    threading.Thread(target=run_login_job, args=(job,), daemon=True).start()
    return {"id": job.id}


@app.post("/api/steam/logout")
def steam_logout():
    forget_steam_account()
    return {"ok": True}


@app.post("/api/open")
def open_folder(body: OpenIn):
    path = expand_path(body.path)
    if not path.exists():
        raise HTTPException(404, "La carpeta no existe.")
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        raise HTTPException(400, f"No pude abrir la carpeta: {exc}") from exc
    return {"ok": True}


_browse_lock = threading.Lock()


@app.post("/api/browse")
def browse_folder():
    """Abre el selector de carpetas nativo (se ejecuta en el PC donde corre la app)."""
    if not _browse_lock.acquire(blocking=False):
        raise HTTPException(409, "Ya hay un selector de carpetas abierto.")
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            start = load_config().get("dest") or str(Path.home())
            chosen = filedialog.askdirectory(
                parent=root,
                title="Elige la carpeta donde guardar los mods",
                initialdir=start,
                mustexist=False,
            )
        finally:
            root.destroy()
        return {"path": chosen or ""}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"No pude abrir el selector de carpetas: {exc}") from exc
    finally:
        _browse_lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Descargador de mods de Steam Workshop")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print("=" * 56)
    print("  Steam Workshop Downloader")
    print(f"  Abre: {url}")
    print("  (cierra esta ventana para detener la app)")
    print("=" * 56)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if sys.version_info < (3, 9):
        print("Necesitas Python 3.9 o superior.")
        raise SystemExit(1)
    main()
