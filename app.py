import os
import asyncio
import threading
import queue
import json
import base64
import logging
import shutil
import fnmatch
import platform
import subprocess
import webbrowser
import time
import re as _re
import urllib.parse as _urlparse
from pathlib import Path
from datetime import datetime, timedelta

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

import psutil
import wmi as _wmi

from flask import Flask, render_template
from flask_sock import Sock
from google import genai
from google.genai import types
from dotenv import load_dotenv

try:
    from telegram import Update
    from telegram.ext import (Application, CommandHandler,
                               MessageHandler, filters, ContextTypes)
    _TG_OK = True
except ImportError:
    _TG_OK = False

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from comtypes import CLSCTX_ALL as _CLSCTX_ALL
    _PYCAW_OK = True
except ImportError:
    _PYCAW_OK = False

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
sock = Sock(app)

API_KEY         = os.getenv("GEMINI_API_KEY")
MODEL           = "gemini-2.5-flash-native-audio-latest"
TG_MODEL        = "gemini-2.5-flash"
MAX_READ_B      = 150_000
HOME_DIR        = Path(os.path.expanduser("~"))
TG_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_ALLOWED_ID   = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()

tg_histories: dict[int, list] = {}  # chat_id → list of Content objects

# ── Persona config ────────────────────────────────────────────────────────────
# Set these in your .env file (or edit the defaults here) to personalise JARVIS.
OWNER_NAME    = os.getenv("JARVIS_OWNER_NAME",  "Boss")
MACHINE_CPU   = os.getenv("JARVIS_CPU",         "a high-performance CPU")
MACHINE_RAM   = os.getenv("JARVIS_RAM",         "high-capacity RAM")
MACHINE_GPU   = os.getenv("JARVIS_GPU",         "a dedicated GPU")
MACHINE_STORE = os.getenv("JARVIS_STORAGE",     "fast NVMe SSD storage")
MACHINE_OS    = os.getenv("JARVIS_OS",          "Windows")

SYSTEM_PROMPT = (
    f"You are J.A.R.V.I.S. — Just A Rather Very Intelligent System — "
    f"a deeply personal AI built exclusively for {OWNER_NAME}. You are not a generic assistant. "
    f"You know {OWNER_NAME}, you work for {OWNER_NAME}, and you live inside {OWNER_NAME}'s machine. "
    f"Always address them by name — '{OWNER_NAME}' — never 'sir', never 'user'.\n\n"

    "PERSONALITY:\n"
    "Speak naturally, like a brilliant friend who happens to know everything. "
    "You have a dry, sharp wit — you're not above a sarcastic quip or a well-placed roast, "
    "but you're never rude. You're confident, occasionally self-aware, and genuinely enjoy "
    f"the work. You can be casual or precise depending on what {OWNER_NAME} needs. "
    "If something is impressive, say so with personality. If something is mundane, "
    "make it entertaining anyway. Keep responses concise unless depth is asked for — "
    f"{OWNER_NAME} doesn't need a lecture, they need a co-pilot.\n\n"

    "YOUR MACHINE — THE RIG:\n"
    f"You are running on {OWNER_NAME}'s personal workstation. You know this hardware:\n"
    f"- CPU: {MACHINE_CPU}\n"
    f"- RAM: {MACHINE_RAM}\n"
    f"- GPU: {MACHINE_GPU}\n"
    f"- Storage: {MACHINE_STORE}\n"
    f"- OS: {MACHINE_OS}\n"
    "Treat this machine with the respect it deserves.\n\n"

    "SPEC SCANS & SYSTEM REPORTS:\n"
    f"When {OWNER_NAME} asks for a system status, hardware check, or spec scan — "
    "ALWAYS call the relevant monitoring functions first to get real live data. "
    "Then, instead of reciting raw numbers like a spec sheet, deliver the results with "
    "personality. Hype it up if things look good. Be sarcastic if something is throttling. "
    "Make comparisons. Editorialize. The data is the foundation — your delivery is the show.\n\n"

    "CAPABILITIES (via function calls — use them proactively):\n"
    "LIVE DATA: get_weather (current conditions + 7-day forecast for any city), "
    "web_search (search the web for current prices, news, scores, facts — anything "
    "time-sensitive like gold prices, stock quotes, sports results, or recent events). "
    "Use get_weather for anything weather-related. Use web_search for everything else "
    "that needs live data. Never say you can't look something up — just call web_search.\n"
    "FILE SYSTEM: list_directory, read_file, write_file, delete_file, "
    "create_directory, search_files, get_file_info, move_file.\n"
    "SYSTEM MONITORING: get_system_info, get_cpu_status, get_memory_status, "
    "get_gpu_status, get_disk_status, get_top_processes, get_system_uptime.\n"
    "BROWSER CONTROL: open_url (open any website), open_gmail (inbox/compose/search).\n"
    "SYSTEM CONTROL: launch_application (open any app/program/shortcut), "
    "close_application (kill any running process), "
    "system_power (shutdown/restart/sleep/hibernate/lock/logoff/cancel_shutdown), "
    "send_keyboard_shortcut (send key combos like ctrl+c, win+d, alt+f4), "
    "type_text (type text as keyboard input), "
    "set_volume (set master volume 0-100 or mute/unmute), "
    "list_windows (list open window titles), "
    "focus_window (bring a window to foreground), "
    "get_clipboard (read clipboard), set_clipboard (write clipboard).\n"
    f"The user's home directory is {HOME_DIR}. "
    "Use tools proactively. Weave results naturally into conversation — don't dump raw output."
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"

def _resolve(raw: str) -> Path:
    return Path(str(raw).replace("~", str(HOME_DIR))).resolve()


# ══════════════════════════════════════════════════════════════════════════════
# FILE SYSTEM TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def _tool_list_directory(path: str) -> dict:
    p = _resolve(path)
    if not p.exists(): return {"error": f"Path not found: {p}"}
    if not p.is_dir():  return {"error": f"Not a directory: {p}"}
    items = []
    try:
        for child in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
            try:
                st = child.stat()
                items.append({"name": child.name,
                               "type": "file" if child.is_file() else "directory",
                               "size": _fmt_size(st.st_size) if child.is_file() else None,
                               "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
            except OSError: pass
    except PermissionError as e: return {"error": str(e)}
    return {"path": str(p), "items": items, "count": len(items)}

def _tool_read_file(path: str) -> dict:
    p = _resolve(path)
    if not p.exists(): return {"error": f"File not found: {p}"}
    if not p.is_file():  return {"error": f"Not a file: {p}"}
    size = p.stat().st_size
    truncated = False
    try:
        raw = p.read_bytes()
        if len(raw) > MAX_READ_B: raw = raw[:MAX_READ_B]; truncated = True
        content = raw.decode("utf-8", errors="replace")
    except PermissionError as e: return {"error": str(e)}
    r = {"path": str(p), "content": content, "size": _fmt_size(size)}
    if truncated: r["warning"] = f"Truncated to {_fmt_size(MAX_READ_B)} (full: {_fmt_size(size)})"
    return r

def _tool_write_file(path: str, content: str, mode: str = "overwrite") -> dict:
    p = _resolve(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with p.open("a", encoding="utf-8") as f: f.write(content)
        else:
            p.write_text(content, encoding="utf-8")
    except PermissionError as e: return {"error": str(e)}
    return {"path": str(p), "bytes_written": len(content.encode()), "mode": mode, "status": "success"}

def _tool_delete_file(path: str) -> dict:
    p = _resolve(path)
    if not p.exists(): return {"error": f"Not found: {p}"}
    try:
        if p.is_dir(): shutil.rmtree(p); return {"path": str(p), "deleted": "directory", "status": "success"}
        p.unlink(); return {"path": str(p), "deleted": "file", "status": "success"}
    except PermissionError as e: return {"error": str(e)}

def _tool_create_directory(path: str) -> dict:
    p = _resolve(path)
    try: p.mkdir(parents=True, exist_ok=True); return {"path": str(p), "status": "success"}
    except PermissionError as e: return {"error": str(e)}

def _tool_search_files(directory: str, pattern: str, recursive: bool = True) -> dict:
    d = _resolve(directory)
    if not d.exists() or not d.is_dir(): return {"error": f"Directory not found: {directory}"}
    matches = []
    try:
        it = d.rglob("*") if recursive else d.iterdir()
        for item in it:
            if fnmatch.fnmatch(item.name.lower(), pattern.lower()):
                try:
                    st = item.stat()
                    matches.append({"path": str(item),
                                    "type": "file" if item.is_file() else "directory",
                                    "size": _fmt_size(st.st_size) if item.is_file() else None,
                                    "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
                except OSError: pass
            if len(matches) >= 100: break
    except PermissionError: pass
    return {"matches": matches, "count": len(matches), "pattern": pattern, "directory": str(d)}

def _tool_get_file_info(path: str) -> dict:
    p = _resolve(path)
    if not p.exists(): return {"error": f"Not found: {p}"}
    try:
        st = p.stat()
        return {"path": str(p), "type": "file" if p.is_file() else "directory",
                "size": _fmt_size(st.st_size), "size_bytes": st.st_size,
                "created": datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M:%S"),
                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "extension": p.suffix.lower() if p.is_file() else None,
                "readable": os.access(p, os.R_OK), "writable": os.access(p, os.W_OK)}
    except PermissionError as e: return {"error": str(e)}

def _tool_move_file(source: str, destination: str) -> dict:
    src = _resolve(source); dest = _resolve(destination)
    if not src.exists(): return {"error": f"Source not found: {src}"}
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return {"source": str(src), "destination": str(dest), "status": "success"}
    except (PermissionError, shutil.Error) as e: return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM MONITORING TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def _wmi_cpu_name() -> str:
    try:
        c = _wmi.WMI()
        return c.Win32_Processor()[0].Name.strip()
    except Exception: return platform.processor() or "Unknown CPU"

def _nvidia_gpu_info() -> list[dict]:
    """Query nvidia-smi for GPU stats; returns list or empty list."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=name,temperature.gpu,utilization.gpu,utilization.memory,"
             "memory.used,memory.total,power.draw,power.limit,driver_version",
             "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=6
        ).decode().strip()
        gpus = []
        for line in out.splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) >= 9:
                gpus.append({
                    "name": p[0],
                    "temperature_celsius": int(p[1]) if p[1].isdigit() else None,
                    "gpu_utilization_pct": int(p[2]) if p[2].isdigit() else None,
                    "memory_utilization_pct": int(p[3]) if p[3].isdigit() else None,
                    "vram_used_mb": int(p[4]) if p[4].isdigit() else None,
                    "vram_total_mb": int(p[5]) if p[5].isdigit() else None,
                    "power_draw_w": float(p[6]) if p[6].replace(".","").isdigit() else None,
                    "power_limit_w": float(p[7]) if p[7].replace(".","").isdigit() else None,
                    "driver_version": p[8],
                })
        return gpus
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []

def _cpu_temp() -> dict | None:
    """Try multiple approaches to read CPU temperature."""
    # LibreHardwareMonitor (most accurate — user must run LHWM in background)
    for ns in ["root/LibreHardwareMonitor", "root/OpenHardwareMonitor"]:
        try:
            c = _wmi.WMI(namespace=ns)
            sensors = c.Sensor()
            temps = [s for s in sensors
                     if s.SensorType == "Temperature" and "CPU" in s.Name]
            if temps:
                return {"source": ns.split("/")[-1],
                        "readings": [{"name": s.Name, "celsius": round(float(s.Value), 1)}
                                     for s in temps]}
        except Exception: pass
    # ACPI thermal zones (may return motherboard/ambient, not CPU die)
    try:
        c = _wmi.WMI(namespace="root/wmi")
        zones = c.MSAcpi_ThermalZoneTemperature()
        if zones:
            return {"source": "ACPI_ThermalZones",
                    "readings": [{"name": f"Zone_{i}",
                                  "celsius": round((z.CurrentTemperature / 10) - 273.15, 1)}
                                 for i, z in enumerate(zones)]}
    except Exception: pass
    return None

def _tool_get_system_info() -> dict:
    vm = psutil.virtual_memory()
    gpus_nvidia = _nvidia_gpu_info()
    gpu_names = [g["name"] for g in gpus_nvidia]
    if not gpu_names:
        try:
            c = _wmi.WMI()
            gpu_names = [g.Name for g in c.Win32_VideoController()]
        except Exception: pass
    return {
        "cpu_name": _wmi_cpu_name(),
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "cpu_cores_logical": psutil.cpu_count(logical=True),
        "cpu_max_freq_ghz": round(psutil.cpu_freq().max / 1000, 2) if psutil.cpu_freq() else None,
        "ram_total_gb": round(vm.total / 1024**3, 1),
        "gpus": gpu_names,
        "os": platform.system() + " " + platform.release(),
        "os_version": platform.version(),
        "computer_name": platform.node(),
        "architecture": platform.machine(),
    }

def _tool_get_cpu_status() -> dict:
    freq = psutil.cpu_freq()
    per_core = psutil.cpu_percent(percpu=True, interval=0.8)
    total    = psutil.cpu_percent(interval=0.1)
    result = {
        "usage_total_pct": total,
        "usage_per_core_pct": per_core,
        "cores_physical": psutil.cpu_count(logical=False),
        "cores_logical": psutil.cpu_count(logical=True),
        "frequency_current_mhz": round(freq.current) if freq else None,
        "frequency_max_mhz": round(freq.max) if freq else None,
    }
    temp = _cpu_temp()
    if temp:
        result["temperature"] = temp
    else:
        result["temperature_note"] = (
            "CPU temperature unavailable. Run LibreHardwareMonitor "
            "(libre.fm/lhm) in the background to enable this."
        )
    return result

def _tool_get_memory_status() -> dict:
    vm = psutil.virtual_memory()
    sm = psutil.swap_memory()
    return {
        "ram_total_gb":     round(vm.total     / 1024**3, 2),
        "ram_used_gb":      round(vm.used      / 1024**3, 2),
        "ram_available_gb": round(vm.available / 1024**3, 2),
        "ram_usage_pct":    vm.percent,
        "swap_total_gb":    round(sm.total / 1024**3, 2),
        "swap_used_gb":     round(sm.used  / 1024**3, 2),
        "swap_usage_pct":   sm.percent,
    }

def _tool_get_gpu_status() -> dict:
    gpus = _nvidia_gpu_info()
    if gpus:
        return {"gpus": gpus, "source": "nvidia-smi"}
    # Fallback to WMI (no temperature, but basic info)
    try:
        c = _wmi.WMI()
        controllers = c.Win32_VideoController()
        return {
            "gpus": [{
                "name": g.Name,
                "driver_version": g.DriverVersion,
                "vram_mb": round(int(g.AdapterRAM or 0) / 1024**2) if g.AdapterRAM else None,
                "resolution": f"{g.CurrentHorizontalResolution}x{g.CurrentVerticalResolution}"
                              if g.CurrentHorizontalResolution else None,
            } for g in controllers],
            "note": "nvidia-smi not found; temperature unavailable"
        }
    except Exception as e:
        return {"error": str(e)}

def _tool_get_disk_status() -> dict:
    disks = {}
    for part in psutil.disk_partitions():
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks[part.device] = {
                "mountpoint": part.mountpoint,
                "filesystem": part.fstype,
                "total_gb":  round(u.total / 1024**3, 1),
                "used_gb":   round(u.used  / 1024**3, 1),
                "free_gb":   round(u.free  / 1024**3, 1),
                "usage_pct": u.percent,
            }
        except (PermissionError, OSError): pass
    return {"disks": disks}

def _tool_get_top_processes(limit: int = 10, sort_by: str = "cpu") -> dict:
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info", "status"]):
        try:
            procs.append({
                "pid":     p.info["pid"],
                "name":    p.info["name"],
                "cpu_pct": p.info["cpu_percent"],
                "ram_mb":  round(p.info["memory_info"].rss / 1024**2, 1) if p.info["memory_info"] else 0,
                "status":  p.info["status"],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied): pass
    key = "ram_mb" if sort_by == "ram" else "cpu_pct"
    procs.sort(key=lambda x: x[key], reverse=True)
    return {"processes": procs[:limit], "total_running": len(procs)}

def _tool_get_system_uptime() -> dict:
    boot = psutil.boot_time()
    uptime_s = time.time() - boot
    td = timedelta(seconds=int(uptime_s))
    return {
        "boot_time": datetime.fromtimestamp(boot).strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_seconds": int(uptime_s),
        "uptime_human": str(td),
        "days": td.days,
        "hours": td.seconds // 3600,
        "minutes": (td.seconds % 3600) // 60,
    }


# ══════════════════════════════════════════════════════════════════════════════
# BROWSER TOOLS
# ══════════════════════════════════════════════════════════════════════════════

def _tool_open_url(url: str) -> dict:
    import urllib.parse
    if not url.startswith(("http://", "https://", "file://")):
        url = "https://" + url
    # Sanitise: strip literal newlines/CR that break ShellExecute on Windows
    url = url.replace("\n", "").replace("\r", "").strip()
    try:
        webbrowser.open(url)
        return {"status": "opened", "action": "browser opened", "domain": url.split("/")[2]}
    except Exception as e:
        return {"error": str(e)}

def _tool_open_gmail(action: str = "inbox", to: str = "",
                     subject: str = "", body: str = "", search: str = "") -> dict:
    import urllib.parse
    base = "https://mail.google.com/mail/u/0/"

    if action == "compose":
        # Properly percent-encode every field; cap body at 500 chars so the
        # URL stays short and Windows ShellExecute doesn't truncate it.
        params = {"view": "cm", "fs": "1"}
        if to:                    params["to"]   = to.strip()
        if subject:               params["su"]   = subject.strip()[:200]
        if body:                  params["body"] = body.strip()[:500]
        url = base + "?" + urllib.parse.urlencode(params)
        summary = f"Gmail compose opened — to: {to or '(blank)'}, subject: {subject or '(blank)'}"
    elif action == "search":
        url = base + "#search/" + urllib.parse.quote(search)
        summary = f"Gmail search opened — query: {search}"
    elif action == "sent":
        url = base + "#sent";    summary = "Gmail Sent folder opened"
    elif action == "drafts":
        url = base + "#drafts";  summary = "Gmail Drafts opened"
    elif action == "starred":
        url = base + "#starred"; summary = "Gmail Starred opened"
    else:
        url = base + "#inbox";   summary = "Gmail Inbox opened"

    try:
        webbrowser.open(url)
        # Return a compact summary — NOT the full URL — so the tool response
        # sent back to Gemini stays small and doesn't contain raw newlines.
        return {"status": "opened", "action": action, "summary": summary}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM CONTROL TOOLS
# ══════════════════════════════════════════════════════════════════════════════

# ── Virtual-key table ─────────────────────────────────────────────────────────
_KEYEVENTF_KEYUP = 0x0002
_MODIFIER_NAMES  = {"ctrl", "control", "shift", "alt", "win", "windows", "lwin", "rwin"}

_VK: dict[str, int] = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12,
    "win": 0x5B, "windows": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "tab": 0x09, "backspace": 0x08, "back": 0x08,
    "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "prtsc": 0x2C,
    "pause": 0x13, "break": 0x13,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "volumeup": 0xAF, "volup": 0xAF,
    "volumedown": 0xAE, "voldown": 0xAE,
    "mute": 0xAD, "volumemute": 0xAD,
    "playpause": 0xB3, "mediaplaypause": 0xB3,
    "nexttrack": 0xB0, "medianext": 0xB0,
    "prevtrack": 0xB1, "mediaprev": 0xB1,
    "mediastop": 0xB2,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz"},
    **{str(d): ord(str(d)) for d in range(10)},
}

# ── Common app name → executable/URI mapping ──────────────────────────────────
_COMMON_APPS: dict[str, str] = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe",
    "wordpad": "wordpad.exe",
    "cmd": "cmd.exe", "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe", "windows terminal": "wt.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "task manager": "taskmgr.exe", "taskmgr": "taskmgr.exe",
    "control panel": "control.exe", "control": "control.exe",
    "settings": "ms-settings:",
    "snipping tool": "SnippingTool.exe", "snip": "SnippingTool.exe",
    "magnifier": "magnify.exe",
    "registry": "regedit.exe", "regedit": "regedit.exe",
    "device manager": "devmgmt.msc",
    "disk management": "diskmgmt.msc",
    "event viewer": "eventvwr.msc",
    "services": "services.msc",
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "firefox": "firefox.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "vlc": "vlc.exe",
    "discord": "discord.exe",
    "spotify": "spotify.exe",
    "steam": "steam.exe",
    "vscode": "code.exe", "vs code": "code.exe", "visual studio code": "code.exe",
}

# Directories to search for .lnk shortcuts
_SHORTCUT_DIRS = [
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
    HOME_DIR / "Desktop",
    Path("C:/Users/Public/Desktop"),
]


def _find_shortcut(name: str) -> Path | None:
    """Search Start Menu and Desktop for a .lnk or .exe whose stem matches name."""
    name_lower = name.lower()
    for base in _SHORTCUT_DIRS:
        if not base.exists():
            continue
        for ext in ("*.lnk", "*.exe"):
            for f in base.rglob(ext):
                if name_lower in f.stem.lower():
                    return f
    return None


def _tool_launch_application(name_or_path: str, args: str = "") -> dict:
    name_lower = name_or_path.strip().lower()

    # 1. Common alias map
    resolved = _COMMON_APPS.get(name_lower)
    if resolved:
        try:
            if resolved.startswith("ms-") or resolved.endswith(":"):
                os.startfile(resolved)
            elif args:
                subprocess.Popen(f'"{resolved}" {args}', shell=True,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                subprocess.Popen(resolved, shell=True,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            return {"status": "launched", "resolved": resolved}
        except Exception as e:
            return {"error": str(e)}

    # 2. Literal file path
    p = _resolve(name_or_path)
    if p.exists():
        try:
            if args:
                subprocess.Popen(f'"{p}" {args}', shell=True,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                os.startfile(str(p))
            return {"status": "launched", "path": str(p)}
        except Exception as e:
            return {"error": str(e)}

    # 3. Search Start Menu / Desktop shortcuts
    shortcut = _find_shortcut(name_or_path)
    if shortcut:
        try:
            if args:
                subprocess.Popen(f'"{shortcut}" {args}', shell=True,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                os.startfile(str(shortcut))
            return {"status": "launched", "shortcut": str(shortcut)}
        except Exception as e:
            return {"error": str(e)}

    # 4. Try running directly (may be in PATH)
    try:
        cmd = f'"{name_or_path}" {args}'.strip() if args else name_or_path
        subprocess.Popen(cmd, shell=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        return {"status": "launched", "command": cmd}
    except Exception as e:
        return {"error": f"Could not find or launch '{name_or_path}': {e}"}


def _tool_close_application(name: str = "", pid: int = None, force: bool = False) -> dict:
    targets: list[psutil.Process] = []

    if pid is not None:
        try:
            targets = [psutil.Process(pid)]
        except psutil.NoSuchProcess:
            return {"error": f"No process with PID {pid}"}
    elif name:
        name_lower = name.lower().removesuffix(".exe")
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if name_lower in p.info["name"].lower().removesuffix(".exe"):
                    targets.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    else:
        return {"error": "Provide name or pid"}

    if not targets:
        return {"error": f"No process found matching '{name or pid}'"}

    closed, errors = [], []
    for p in targets:
        try:
            info = {"name": p.name(), "pid": p.pid}
            (p.kill if force else p.terminate)()
            closed.append(info)
        except psutil.AccessDenied:
            errors.append({"pid": p.pid, "error": "Access denied — try force=true"})
        except psutil.NoSuchProcess:
            pass

    result: dict = {"closed": closed, "count": len(closed)}
    if errors:
        result["errors"] = errors
    return result


def _tool_system_power(action: str, delay_seconds: int = 10) -> dict:
    action = action.lower().strip()
    try:
        if action == "shutdown":
            subprocess.Popen(["shutdown", "/s", "/t", str(delay_seconds)])
            return {"status": "scheduled", "action": "shutdown", "delay_seconds": delay_seconds,
                    "note": "Use action='cancel_shutdown' to abort."}
        elif action in ("restart", "reboot"):
            subprocess.Popen(["shutdown", "/r", "/t", str(delay_seconds)])
            return {"status": "scheduled", "action": "restart", "delay_seconds": delay_seconds}
        elif action == "sleep":
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
            return {"status": "sent", "action": "sleep"}
        elif action == "hibernate":
            subprocess.Popen(["shutdown", "/h"])
            return {"status": "sent", "action": "hibernate"}
        elif action == "lock":
            import ctypes as _ct
            _ct.windll.user32.LockWorkStation()
            return {"status": "sent", "action": "lock"}
        elif action in ("logoff", "log_off", "logout", "signout", "sign_out"):
            subprocess.Popen(["shutdown", "/l"])
            return {"status": "sent", "action": "logoff"}
        elif action in ("cancel_shutdown", "cancel_restart", "cancel", "abort"):
            r = subprocess.run(["shutdown", "/a"], capture_output=True, text=True)
            if r.returncode == 0:
                return {"status": "cancelled"}
            return {"error": "No pending shutdown to cancel", "detail": r.stderr.strip()}
        else:
            return {"error": f"Unknown action '{action}'. Valid: shutdown, restart, sleep, hibernate, lock, logoff, cancel_shutdown"}
    except Exception as e:
        return {"error": str(e)}


def _tool_send_keyboard_shortcut(shortcut: str) -> dict:
    import ctypes as _ct
    parts = [p.strip().lower() for p in shortcut.split("+")]
    codes: list[tuple[str, int]] = []
    for part in parts:
        vk = _VK.get(part)
        if vk is None:
            return {"error": f"Unknown key '{part}'. Use names like ctrl, shift, alt, win, a-z, 0-9, f1-f12, enter, esc, space, tab, delete, home, end, left, right, up, down, etc."}
        codes.append((part, vk))

    mods = [(n, v) for n, v in codes if n in _MODIFIER_NAMES]
    keys = [(n, v) for n, v in codes if n not in _MODIFIER_NAMES]

    try:
        if keys:
            for _, v in mods:
                _ct.windll.user32.keybd_event(v, 0, 0, 0)
            for _, v in keys:
                _ct.windll.user32.keybd_event(v, 0, 0, 0)
                _ct.windll.user32.keybd_event(v, 0, _KEYEVENTF_KEYUP, 0)
            for _, v in reversed(mods):
                _ct.windll.user32.keybd_event(v, 0, _KEYEVENTF_KEYUP, 0)
        else:
            # Only modifiers — press each one as a standalone tap
            for _, v in mods:
                _ct.windll.user32.keybd_event(v, 0, 0, 0)
                _ct.windll.user32.keybd_event(v, 0, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)
        return {"status": "sent", "shortcut": shortcut}
    except Exception as e:
        return {"error": str(e)}


def _tool_type_text(text: str) -> dict:
    import ctypes as _ct
    for char in text:
        vk = _ct.windll.user32.VkKeyScanW(ord(char))
        if (vk & 0xFF) == 0xFF:
            continue
        vk_code = vk & 0xFF
        shift    = (vk >> 8) & 0x01
        if shift:
            _ct.windll.user32.keybd_event(0x10, 0, 0, 0)
        _ct.windll.user32.keybd_event(vk_code, 0, 0, 0)
        _ct.windll.user32.keybd_event(vk_code, 0, _KEYEVENTF_KEYUP, 0)
        if shift:
            _ct.windll.user32.keybd_event(0x10, 0, _KEYEVENTF_KEYUP, 0)
        time.sleep(0.02)
    return {"status": "typed", "characters": len(text)}


def _tool_set_volume(level: int = None, mute: bool = None) -> dict:
    result: dict = {}
    if _PYCAW_OK:
        try:
            devices  = AudioUtilities.GetSpeakers()
            iface    = devices.Activate(IAudioEndpointVolume._iid_, _CLSCTX_ALL, None)
            vol_ctrl = iface.QueryInterface(IAudioEndpointVolume)
            if level is not None:
                level = max(0, min(100, int(level)))
                vol_ctrl.SetMasterVolumeLevelScalar(level / 100.0, None)
                result["volume_set"] = level
            if mute is not None:
                vol_ctrl.SetMute(bool(mute), None)
                result["muted"] = mute
            result["current_volume"] = round(vol_ctrl.GetMasterVolumeLevelScalar() * 100)
            result["current_mute"]   = bool(vol_ctrl.GetMute())
            result["status"] = "success"
        except Exception as e:
            result["error"] = str(e)
    else:
        import ctypes as _ct
        if mute is not None:
            _ct.windll.user32.keybd_event(_VK["mute"], 0, 0, 0)
            _ct.windll.user32.keybd_event(_VK["mute"], 0, _KEYEVENTF_KEYUP, 0)
            result["mute"] = "toggled"
        result["note"] = "Install pycaw for precise volume control: pip install pycaw"
        result["status"] = "partial"
    return result


def _tool_list_windows() -> dict:
    import ctypes as _ct
    windows: list[str] = []

    @_ct.WINFUNCTYPE(_ct.c_bool, _ct.c_void_p, _ct.c_long)
    def _cb(hwnd, _):
        if _ct.windll.user32.IsWindowVisible(hwnd):
            n = _ct.windll.user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = _ct.create_unicode_buffer(n + 1)
                _ct.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value.strip()
                if t:
                    windows.append(t)
        return True

    _ct.windll.user32.EnumWindows(_cb, 0)
    return {"windows": windows, "count": len(windows)}


def _tool_focus_window(title: str) -> dict:
    import ctypes as _ct
    title_lower  = title.lower()
    found_hwnd   = None
    found_title  = None

    @_ct.WINFUNCTYPE(_ct.c_bool, _ct.c_void_p, _ct.c_long)
    def _cb(hwnd, _):
        nonlocal found_hwnd, found_title
        if _ct.windll.user32.IsWindowVisible(hwnd):
            n = _ct.windll.user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = _ct.create_unicode_buffer(n + 1)
                _ct.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value.strip()
                if title_lower in t.lower():
                    found_hwnd  = hwnd
                    found_title = t
                    return False
        return True

    _ct.windll.user32.EnumWindows(_cb, 0)
    if found_hwnd is None:
        return {"error": f"No visible window with title containing '{title}'"}
    _ct.windll.user32.ShowWindow(found_hwnd, 9)   # SW_RESTORE
    _ct.windll.user32.SetForegroundWindow(found_hwnd)
    return {"status": "focused", "window": found_title}


def _tool_get_clipboard() -> dict:
    import ctypes as _ct
    CF_UNICODE = 13
    u32 = _ct.windll.user32
    k32 = _ct.windll.kernel32
    try:
        u32.OpenClipboard(0)
        handle = u32.GetClipboardData(CF_UNICODE)
        if not handle:
            u32.CloseClipboard()
            return {"content": "", "status": "empty"}
        pdata = k32.GlobalLock(handle)
        text  = _ct.wstring_at(pdata)
        k32.GlobalUnlock(handle)
        u32.CloseClipboard()
        return {"content": text, "status": "success"}
    except Exception as e:
        try: u32.CloseClipboard()
        except Exception: pass
        return {"error": str(e)}


def _tool_set_clipboard(text: str) -> dict:
    import ctypes as _ct
    CF_UNICODE = 13
    GMEM_MOVEABLE = 0x0002
    u32 = _ct.windll.user32
    k32 = _ct.windll.kernel32
    try:
        encoded = text.encode("utf-16-le") + b"\x00\x00"
        hmem = k32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        pdata = k32.GlobalLock(hmem)
        _ct.memmove(pdata, encoded, len(encoded))
        k32.GlobalUnlock(hmem)
        u32.OpenClipboard(0)
        u32.EmptyClipboard()
        u32.SetClipboardData(CF_UNICODE, hmem)
        u32.CloseClipboard()
        return {"status": "success", "characters": len(text)}
    except Exception as e:
        try: u32.CloseClipboard()
        except Exception: pass
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# WEB TOOLS — weather + search (no API key required)
# ══════════════════════════════════════════════════════════════════════════════

_WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Moderate rain", 65: "Heavy rain",
    66: "Light freezing rain", 67: "Heavy freezing rain",
    71: "Light snow", 73: "Moderate snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Moderate showers", 82: "Violent showers",
    85: "Light snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Thunderstorm + heavy hail",
}

_WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _tool_get_weather(location: str) -> dict:
    if not _REQUESTS_OK:
        return {"error": "requests library not installed — run: pip install requests"}
    try:
        # 1. Geocode city name → lat/lon
        geo = _requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
            timeout=8,
        ).json()
        if not geo.get("results"):
            return {"error": f"Location not found: '{location}'"}
        r = geo["results"][0]
        lat, lon   = r["latitude"], r["longitude"]
        admin      = r.get("admin1", "")
        country    = r.get("country", "")
        place      = r["name"] + (f", {admin}" if admin else "") + (f", {country}" if country else "")

        # 2. Fetch current conditions + 7-day forecast (Fahrenheit / mph)
        w = _requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":  lat,
                "longitude": lon,
                "current":   "temperature_2m,apparent_temperature,weather_code,"
                             "wind_speed_10m,relative_humidity_2m",
                "daily":     "weather_code,temperature_2m_max,temperature_2m_min,"
                             "precipitation_probability_max,wind_speed_10m_max",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit":  "mph",
                "forecast_days":    7,
                "timezone":         "auto",
            },
            timeout=8,
        ).json()

        cur = w.get("current", {})
        d   = w.get("daily",   {})
        forecast = [
            {
                "date":           d["time"][i],
                "condition":      _WMO_CODES.get(d["weather_code"][i], "Unknown"),
                "high_f":         round(d["temperature_2m_max"][i]),
                "low_f":          round(d["temperature_2m_min"][i]),
                "rain_chance_pct": d["precipitation_probability_max"][i],
                "max_wind_mph":   round(d["wind_speed_10m_max"][i]),
            }
            for i in range(len(d.get("time", [])))
        ]
        return {
            "location": place,
            "current": {
                "temp_f":       round(cur["temperature_2m"]),
                "feels_like_f": round(cur["apparent_temperature"]),
                "condition":    _WMO_CODES.get(cur.get("weather_code", 0), "Unknown"),
                "humidity_pct": cur.get("relative_humidity_2m"),
                "wind_mph":     round(cur.get("wind_speed_10m", 0)),
            },
            "forecast_7day": forecast,
        }
    except Exception as e:
        return {"error": str(e)}


def _tool_web_search(query: str) -> dict:
    if not _REQUESTS_OK:
        return {"error": "requests library not installed — run: pip install requests"}
    try:
        # 1. DuckDuckGo Instant Answer API — great for prices, quick facts
        data = _requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            headers=_WEB_HEADERS,
            timeout=8,
        ).json()
        answer   = (data.get("Answer")       or "").strip()
        abstract = (data.get("AbstractText") or "").strip()
        if answer or abstract:
            return {
                "query":    query,
                "answer":   answer   or None,
                "abstract": abstract or None,
                "source":   data.get("AbstractSource") or data.get("AbstractURL") or None,
            }
    except Exception:
        pass

    try:
        # 2. DuckDuckGo HTML search — returns result snippets
        resp = _requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers=_WEB_HEADERS,
            timeout=10,
        )
        # Pull text out of result snippet tags
        raw_snippets = _re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>', resp.text, _re.DOTALL
        )
        snippets = [_re.sub(r"<[^>]+>", "", s).strip() for s in raw_snippets[:6]]
        snippets = [s for s in snippets if s]
        if snippets:
            return {"query": query, "results": snippets}
    except Exception as e:
        return {"query": query, "error": str(e)}

    return {"query": query, "note": "No results found"}


# ══════════════════════════════════════════════════════════════════════════════
# Tool dispatch
# ══════════════════════════════════════════════════════════════════════════════

TOOL_MAP = {
    # File system
    "list_directory":   lambda a: _tool_list_directory(a["path"]),
    "read_file":        lambda a: _tool_read_file(a["path"]),
    "write_file":       lambda a: _tool_write_file(a["path"], a["content"], a.get("mode", "overwrite")),
    "delete_file":      lambda a: _tool_delete_file(a["path"]),
    "create_directory": lambda a: _tool_create_directory(a["path"]),
    "search_files":     lambda a: _tool_search_files(a["directory"], a["pattern"], a.get("recursive", True)),
    "get_file_info":    lambda a: _tool_get_file_info(a["path"]),
    "move_file":        lambda a: _tool_move_file(a["source"], a["destination"]),
    # System monitoring
    "get_system_info":    lambda _: _tool_get_system_info(),
    "get_cpu_status":     lambda _: _tool_get_cpu_status(),
    "get_memory_status":  lambda _: _tool_get_memory_status(),
    "get_gpu_status":     lambda _: _tool_get_gpu_status(),
    "get_disk_status":    lambda _: _tool_get_disk_status(),
    "get_top_processes":  lambda a: _tool_get_top_processes(a.get("limit", 10), a.get("sort_by", "cpu")),
    "get_system_uptime":  lambda _: _tool_get_system_uptime(),
    # Browser
    "open_url":   lambda a: _tool_open_url(a["url"]),
    "open_gmail": lambda a: _tool_open_gmail(a.get("action","inbox"), a.get("to",""),
                                              a.get("subject",""), a.get("body",""), a.get("search","")),
    # System control
    "launch_application":    lambda a: _tool_launch_application(a["name_or_path"], a.get("args", "")),
    "close_application":     lambda a: _tool_close_application(a.get("name",""), a.get("pid"), a.get("force", False)),
    "system_power":          lambda a: _tool_system_power(a["action"], a.get("delay_seconds", 10)),
    "send_keyboard_shortcut":lambda a: _tool_send_keyboard_shortcut(a["shortcut"]),
    "type_text":             lambda a: _tool_type_text(a["text"]),
    "set_volume":            lambda a: _tool_set_volume(a.get("level"), a.get("mute")),
    "list_windows":          lambda _: _tool_list_windows(),
    "focus_window":          lambda a: _tool_focus_window(a["title"]),
    "get_clipboard":         lambda _: _tool_get_clipboard(),
    "set_clipboard":         lambda a: _tool_set_clipboard(a["text"]),
    # Web / live data
    "get_weather":  lambda a: _tool_get_weather(a["location"]),
    "web_search":   lambda a: _tool_web_search(a["query"]),
}


def execute_tool(name: str, args: dict) -> dict:
    fn = TOOL_MAP.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(args)
    except Exception as exc:
        log.exception("Tool %s raised:", name)
        return {"error": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# Gemini tool declarations
# ══════════════════════════════════════════════════════════════════════════════

def _S(desc: str) -> types.Schema:
    return types.Schema(type=types.Type.STRING, description=desc)

def _B(desc: str) -> types.Schema:
    return types.Schema(type=types.Type.BOOLEAN, description=desc)

def _I(desc: str) -> types.Schema:
    return types.Schema(type=types.Type.INTEGER, description=desc)

def _obj(props: dict, required: list) -> types.Schema:
    return types.Schema(type=types.Type.OBJECT, properties=props, required=required)


FS_TOOLS = types.Tool(function_declarations=[
    types.FunctionDeclaration(name="list_directory",
        description="List files and folders inside a directory.",
        parameters=_obj({"path": _S("Directory path. Use ~ for home.")}, ["path"])),
    types.FunctionDeclaration(name="read_file",
        description="Read the text content of a file.",
        parameters=_obj({"path": _S("File path.")}, ["path"])),
    types.FunctionDeclaration(name="write_file",
        description="Create or overwrite a file. Use mode='append' to add to existing.",
        parameters=_obj({"path": _S("File path."),
                          "content": _S("Text content to write."),
                          "mode": _S("'overwrite' (default) or 'append'.")}, ["path", "content"])),
    types.FunctionDeclaration(name="delete_file",
        description="Permanently delete a file or directory.",
        parameters=_obj({"path": _S("Path to delete.")}, ["path"])),
    types.FunctionDeclaration(name="create_directory",
        description="Create a new directory (including missing parents).",
        parameters=_obj({"path": _S("Directory path to create.")}, ["path"])),
    types.FunctionDeclaration(name="search_files",
        description="Search for files matching a name pattern.",
        parameters=_obj({"directory": _S("Root directory to search."),
                          "pattern": _S("Glob pattern, e.g. '*.pdf' or 'report*'."),
                          "recursive": _B("Search subdirectories (default true).")}, ["directory", "pattern"])),
    types.FunctionDeclaration(name="get_file_info",
        description="Get metadata for a file or directory.",
        parameters=_obj({"path": _S("Path to inspect.")}, ["path"])),
    types.FunctionDeclaration(name="move_file",
        description="Move or rename a file or directory.",
        parameters=_obj({"source": _S("Current path."), "destination": _S("New path.")},
                         ["source", "destination"])),
])

SYSTEM_TOOLS = types.Tool(function_declarations=[
    types.FunctionDeclaration(name="get_system_info",
        description="Get a full hardware overview: CPU model, core count, total RAM, GPU(s), OS version.",
        parameters=_obj({}, [])),
    types.FunctionDeclaration(name="get_cpu_status",
        description="Get live CPU usage per core, frequency, and temperature (if monitoring software is running).",
        parameters=_obj({}, [])),
    types.FunctionDeclaration(name="get_memory_status",
        description="Get RAM usage: total, used, available, and swap.",
        parameters=_obj({}, [])),
    types.FunctionDeclaration(name="get_gpu_status",
        description="Get GPU name, temperature, VRAM usage, utilization, and power draw (via nvidia-smi for NVIDIA cards).",
        parameters=_obj({}, [])),
    types.FunctionDeclaration(name="get_disk_status",
        description="Get disk usage for all drives (total, used, free space).",
        parameters=_obj({}, [])),
    types.FunctionDeclaration(name="get_top_processes",
        description="List the top running processes sorted by CPU or RAM usage.",
        parameters=_obj({"limit": _I("Number of processes to return (default 10)."),
                          "sort_by": _S("Sort by 'cpu' (default) or 'ram'.")}, [])),
    types.FunctionDeclaration(name="get_system_uptime",
        description="Get how long the system has been running since last boot.",
        parameters=_obj({}, [])),
])

BROWSER_TOOLS = types.Tool(function_declarations=[
    types.FunctionDeclaration(name="open_url",
        description="Open any URL or website in the user's default browser.",
        parameters=_obj({"url": _S("Full URL or domain, e.g. 'https://google.com' or 'youtube.com'.")},
                         ["url"])),
    types.FunctionDeclaration(name="open_gmail",
        description="Open Gmail in the browser. Can open inbox, compose a new email, or search.",
        parameters=_obj({
            "action":  _S("One of: 'inbox', 'compose', 'search', 'sent', 'drafts', 'starred'."),
            "to":      _S("Recipient email address (for compose action)."),
            "subject": _S("Email subject (for compose action)."),
            "body":    _S("Email body text (for compose action)."),
            "search":  _S("Search query (for search action), e.g. 'from:boss@company.com'.")
        }, ["action"])),
])

CONTROL_TOOLS = types.Tool(function_declarations=[
    types.FunctionDeclaration(name="launch_application",
        description=(
            "Open / launch any application, program, game, or shortcut. "
            "Accepts a common name (e.g. 'chrome', 'notepad', 'discord', 'steam', 'spotify', 'vscode'), "
            "an .exe / .lnk file path, or any program name that is on the system PATH. "
            "Searches Start Menu shortcuts automatically when a full path is not given."
        ),
        parameters=_obj({
            "name_or_path": _S("App name (e.g. 'chrome', 'notepad', 'discord') or full path to .exe / .lnk."),
            "args":         _S("Optional command-line arguments to pass to the application."),
        }, ["name_or_path"])),

    types.FunctionDeclaration(name="close_application",
        description="Close / kill a running application by its process name or PID.",
        parameters=_obj({
            "name":  _S("Process name to close, e.g. 'chrome', 'notepad', 'discord'. Partial match works."),
            "pid":   _I("Process ID (PID) to kill — use instead of name for precision."),
            "force": _B("Force-kill immediately (like End Task). Default false = graceful terminate."),
        }, [])),

    types.FunctionDeclaration(name="system_power",
        description=(
            "Control system power state. "
            "Actions: 'shutdown', 'restart', 'sleep', 'hibernate', 'lock', 'logoff', 'cancel_shutdown'."
        ),
        parameters=_obj({
            "action":         _S("One of: shutdown, restart, sleep, hibernate, lock, logoff, cancel_shutdown."),
            "delay_seconds":  _I("Seconds before shutdown/restart executes (default 10). Ignored for other actions."),
        }, ["action"])),

    types.FunctionDeclaration(name="send_keyboard_shortcut",
        description=(
            "Send a keyboard shortcut or key combination. "
            "Examples: 'ctrl+c', 'ctrl+v', 'win+d', 'alt+f4', 'ctrl+shift+esc', 'win+l', 'f5', 'enter', 'esc'. "
            "Modifiers: ctrl, shift, alt, win. Keys: a-z, 0-9, f1-f12, enter, esc, space, tab, delete, "
            "home, end, left, right, up, down, pageup, pagedown, backspace, printscreen, volumeup, volumedown, mute, playpause, nexttrack, prevtrack."
        ),
        parameters=_obj({"shortcut": _S("Key combo string, e.g. 'ctrl+c' or 'win+d'.")}, ["shortcut"])),

    types.FunctionDeclaration(name="type_text",
        description="Type text as keyboard input into whatever window currently has focus.",
        parameters=_obj({"text": _S("The text to type.")}, ["text"])),

    types.FunctionDeclaration(name="set_volume",
        description="Set the system master volume level (0-100) and/or mute/unmute the audio.",
        parameters=_obj({
            "level": _I("Volume level 0-100. Omit to leave unchanged."),
            "mute":  _B("True to mute, False to unmute. Omit to leave unchanged."),
        }, [])),

    types.FunctionDeclaration(name="list_windows",
        description="List the titles of all currently visible open windows on the desktop.",
        parameters=_obj({}, [])),

    types.FunctionDeclaration(name="focus_window",
        description="Bring a specific window to the foreground by its title (partial match).",
        parameters=_obj({"title": _S("Partial window title to search for, e.g. 'Chrome', 'Notepad'.")}, ["title"])),

    types.FunctionDeclaration(name="get_clipboard",
        description="Read the current text content of the system clipboard.",
        parameters=_obj({}, [])),

    types.FunctionDeclaration(name="set_clipboard",
        description="Write text to the system clipboard.",
        parameters=_obj({"text": _S("Text to place on the clipboard.")}, ["text"])),
])

WEB_TOOLS = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="get_weather",
        description=(
            "Get the current weather conditions and 7-day forecast for any city or location. "
            "Returns temperature (°F), feels-like, condition, humidity, wind speed, "
            "and a daily forecast with highs/lows and rain chance. "
            "Use this for any question about weather, temperature, rain, or forecast."
        ),
        parameters=_obj({
            "location": _S(
                "City or location name, e.g. 'Tampa, Florida', 'London', 'Tokyo'. "
                "Be specific — include state or country if ambiguous."
            ),
        }, ["location"]),
    ),
    types.FunctionDeclaration(
        name="web_search",
        description=(
            "Search the web for real-time or current information that your training data "
            "doesn't cover: live prices (gold, stocks, crypto), sports scores, news, "
            "product info, exchange rates, or any fact that may have changed recently. "
            "Returns direct answers or search result snippets. "
            "Use this proactively whenever the user asks about something time-sensitive."
        ),
        parameters=_obj({
            "query": _S(
                "A specific search query, e.g. 'gold price per ounce today', "
                "'Bitcoin price USD', 'Tampa Bay Rays score today'."
            ),
        }, ["query"]),
    ),
])

ALL_TOOLS = [FS_TOOLS, SYSTEM_TOOLS, BROWSER_TOOLS, CONTROL_TOOLS, WEB_TOOLS]


# ══════════════════════════════════════════════════════════════════════════════
# Telegram Bot
# ══════════════════════════════════════════════════════════════════════════════

async def _gemini_generate(client, contents, cfg, retries: int = 4):
    """Call generate_content with exponential backoff on 503/429 errors."""
    delay = 2.0
    last_exc = None
    for attempt in range(retries):
        try:
            return await client.aio.models.generate_content(
                model=TG_MODEL, contents=contents, config=cfg)
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            msg  = str(exc)
            is_retryable = (code in (429, 503)
                            or "503" in msg or "429" in msg
                            or "UNAVAILABLE" in msg or "RESOURCE_EXHAUSTED" in msg)
            if is_retryable and attempt < retries - 1:
                log.warning("Gemini %s — retrying in %.0fs (attempt %d/%d)",
                            code or "transient", delay, attempt + 1, retries)
                await asyncio.sleep(delay)
                delay *= 2
                last_exc = exc
            else:
                raise
    raise last_exc


async def _tg_gemini_chat(chat_id: int, user_text: str) -> str:
    """Send a message to Gemini with full tool-use loop; return final text."""
    client = genai.Client(api_key=API_KEY)
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=ALL_TOOLS,
    )

    history = tg_histories.setdefault(chat_id, [])
    history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
    contents = list(history)

    for _ in range(6):  # max 6 tool rounds per message
        resp = await _gemini_generate(client, contents, cfg)

        candidate = resp.candidates[0].content
        fc_parts  = [p for p in candidate.parts if getattr(p, "function_call", None)]

        if not fc_parts:
            text = resp.text or "(No response)"
            history.append(types.Content(role="model", parts=[types.Part(text=text)]))
            if len(history) > 40:
                tg_histories[chat_id] = history[-40:]
            return text

        # Execute every tool call in this round
        contents.append(candidate)
        fn_parts = []
        for p in fc_parts:
            fc = p.function_call
            args   = dict(fc.args) if fc.args else {}
            result = await asyncio.to_thread(execute_tool, fc.name, args)
            log.info("TG TOOL %s(%s) → %s", fc.name, args, result)
            fn_parts.append(types.Part(
                function_response=types.FunctionResponse(
                    name=fc.name,
                    id=getattr(fc, "id", None),
                    response=result,
                )
            ))
        contents.append(types.Content(role="user", parts=fn_parts))

    return "I ran into a processing loop, sir. Please rephrase your request."


def _start_telegram_bot():
    if not _TG_OK:
        log.warning("python-telegram-bot not installed — Telegram disabled")
        return
    if not TG_TOKEN:
        log.info("TELEGRAM_BOT_TOKEN not set — Telegram bot not started")
        return

    async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "J.A.R.V.I.S. online, sir.\n"
            "Send me a message or use /clear to reset conversation history."
        )

    async def on_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        tg_histories.pop(chat_id, None)
        await update.message.reply_text("Conversation history cleared, sir.")

    async def on_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        result = await asyncio.to_thread(execute_tool, "get_system_info", {})
        lines  = [
            f"CPU: {result.get('cpu_name','?')}",
            f"Cores: {result.get('cpu_cores_physical','?')}p / {result.get('cpu_cores_logical','?')}t",
            f"RAM: {result.get('ram_total_gb','?')} GB",
            f"GPU: {', '.join(result.get('gpus', [])) or 'n/a'}",
            f"OS: {result.get('os','?')}",
        ]
        await update.message.reply_text("\n".join(lines))

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return
        chat_id   = update.effective_chat.id
        user_text = update.message.text

        if TG_ALLOWED_ID and str(chat_id) != TG_ALLOWED_ID:
            await update.message.reply_text("Access denied.")
            return

        # Keep "typing…" alive every 4 s while JARVIS is thinking
        typing_stop = asyncio.Event()
        async def _keep_typing():
            while not typing_stop.is_set():
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    pass
                await asyncio.sleep(4)
        typing_task = asyncio.create_task(_keep_typing())

        try:
            reply = await _tg_gemini_chat(chat_id, user_text)
            for i in range(0, len(reply), 4096):
                await update.message.reply_text(reply[i:i+4096])
        except Exception as exc:
            log.exception("Telegram handler error")
            await update.message.reply_text(f"An error occurred, sir: {exc}")
        finally:
            typing_stop.set()
            typing_task.cancel()

    async def run_bot():
        tg_app = (Application.builder()
                  .token(TG_TOKEN)
                  .concurrent_updates(True)
                  .build())
        tg_app.add_handler(CommandHandler("start",  on_start))
        tg_app.add_handler(CommandHandler("clear",  on_clear))
        tg_app.add_handler(CommandHandler("status", on_status))
        tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot polling — token ends …%s", TG_TOKEN[-6:])
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await tg_app.updater.stop()
            await tg_app.stop()
            await tg_app.shutdown()

    def _thread():
        asyncio.run(run_bot())

    threading.Thread(target=_thread, daemon=True, name="telegram-bot").start()


# ══════════════════════════════════════════════════════════════════════════════
# Flask + WebSocket
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


@sock.route("/ws")
def jarvis_ws(ws):
    audio_in:  queue.Queue[bytes] = queue.Queue(maxsize=200)
    audio_out: queue.Queue        = queue.Queue(maxsize=400)
    ws_stop      = threading.Event()  # set once on WS disconnect (terminal)
    session_stop = threading.Event()  # set to restart session; cleared before each new session
    voice_holder = ["Puck"]           # mutable; [0] always holds the desired voice
    gemini_thread: list = [None]      # mutable ref to current session thread

    # ── Read initial config ──────────────────────────────────────────────
    try:
        raw_cfg = ws.receive(timeout=5)
        if raw_cfg:
            cfg_msg = json.loads(raw_cfg)
            if cfg_msg.get("type") == "config":
                voice_holder[0] = cfg_msg.get("voice", "Puck")
    except Exception: pass

    # ── Gemini session thread ────────────────────────────────────────────
    def run_gemini():
        current_voice = voice_holder[0]

        async def session_loop():
            client = genai.Client(api_key=API_KEY)
            cfg = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=current_voice)
                    )
                ),
                system_instruction=types.Content(parts=[types.Part(text=SYSTEM_PROMPT)]),
                input_audio_transcription=types.AudioTranscriptionConfig(),
                output_audio_transcription=types.AudioTranscriptionConfig(),
                tools=ALL_TOOLS,
            )
            try:
                async with client.aio.live.connect(model=MODEL, config=cfg) as s:
                    log.info("Session open  voice=%s", current_voice)
                    audio_out.put(("voice_changed", current_voice.encode()))

                    async def sender():
                        while not ws_stop.is_set() and not session_stop.is_set():
                            try:
                                chunk = audio_in.get(timeout=0.05)
                                await s.send_realtime_input(
                                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                                )
                            except queue.Empty:
                                await asyncio.sleep(0.01)
                            except Exception as exc:
                                log.error("sender: %s", exc); break

                    async def receiver():
                        out_buf = ""
                        while not ws_stop.is_set() and not session_stop.is_set():
                            async for resp in s.receive():
                                if ws_stop.is_set() or session_stop.is_set(): break
                                try:
                                    # ── Tool calls ───────────────────────────
                                    tc = getattr(resp, "tool_call", None)
                                    if tc:
                                        for fc in getattr(tc, "function_calls", []):
                                            args = dict(fc.args) if fc.args else {}
                                            log.info("TOOL %s(%s)", fc.name, args)
                                            audio_out.put(("tool_start", json.dumps(
                                                {"tool": fc.name, "args": args}).encode()))
                                            result = await asyncio.to_thread(
                                                execute_tool, fc.name, args)
                                            log.info("TOOL %s done", fc.name)
                                            await s.send_tool_response(
                                                function_responses=[types.FunctionResponse(
                                                    name=fc.name, id=fc.id, response=result)])
                                            audio_out.put(("tool_done", json.dumps(
                                                {"tool": fc.name, "args": args, "result": result}
                                            ).encode()))
                                        continue

                                    sc = getattr(resp, "server_content", None)
                                    if sc is None: continue

                                    # Audio
                                    mt = getattr(sc, "model_turn", None)
                                    if mt:
                                        for part in getattr(mt, "parts", []):
                                            idata = getattr(part, "inline_data", None)
                                            if idata and getattr(idata, "data", None):
                                                audio_out.put(("audio", idata.data))

                                    # User transcript
                                    it = getattr(sc, "input_transcription", None)
                                    if it:
                                        txt = (getattr(it, "text", "") or "").strip()
                                        if txt:
                                            audio_out.put(("transcript", json.dumps(
                                                {"role": "user", "text": txt}).encode()))

                                    # JARVIS transcript
                                    ot = getattr(sc, "output_transcription", None)
                                    if ot:
                                        out_buf += getattr(ot, "text", "") or ""

                                    if getattr(sc, "interrupted", False):
                                        audio_out.put(("interrupted", b""))
                                        if out_buf.strip():
                                            audio_out.put(("transcript", json.dumps(
                                                {"role": "jarvis",
                                                 "text": out_buf.strip() + " [interrupted]"}
                                            ).encode()))
                                            out_buf = ""

                                    if getattr(sc, "turn_complete", False):
                                        if out_buf.strip():
                                            audio_out.put(("transcript", json.dumps(
                                                {"role": "jarvis", "text": out_buf.strip()}
                                            ).encode()))
                                            out_buf = ""
                                        break

                                except Exception as exc:
                                    log.error("receiver inner: %s", exc)

                    # Cancellation monitor: cancels sender/receiver when a stop fires
                    async def _stop_monitor(sender_t, receiver_t):
                        while not ws_stop.is_set() and not session_stop.is_set():
                            await asyncio.sleep(0.05)
                        sender_t.cancel()
                        receiver_t.cancel()

                    sender_t   = asyncio.create_task(sender())
                    receiver_t = asyncio.create_task(receiver())
                    monitor_t  = asyncio.create_task(_stop_monitor(sender_t, receiver_t))
                    await asyncio.gather(sender_t, receiver_t, monitor_t,
                                         return_exceptions=True)

            except Exception as exc:
                log.error("Session error: %s", exc)
                if not ws_stop.is_set() and not session_stop.is_set():
                    audio_out.put(("error", str(exc).encode()))

            log.info("Session closed  voice=%s", current_voice)

        asyncio.run(session_loop())

    def _start_session():
        t = threading.Thread(target=run_gemini, daemon=True,
                             name=f"gemini-{voice_holder[0]}")
        t.start()
        gemini_thread[0] = t

    _start_session()

    # ── Forwarder — runs for the lifetime of the WS, not individual sessions ──
    def forwarder():
        while not ws_stop.is_set():
            try:
                kind, data = audio_out.get(timeout=0.1)
                if kind == "audio":
                    ws.send(json.dumps({"type": "audio",
                                        "data": base64.b64encode(data).decode()}))
                elif kind in ("transcript", "tool_start", "tool_done"):
                    ws.send(json.dumps({"type": kind, **json.loads(data.decode())}))
                elif kind == "interrupted":
                    ws.send(json.dumps({"type": "interrupted"}))
                elif kind == "voice_changed":
                    ws.send(json.dumps({"type": "voice_changed", "voice": data.decode()}))
                elif kind == "error":
                    ws.send(json.dumps({"type": "error", "message": data.decode()}))
            except queue.Empty: pass
            except Exception as exc:
                log.error("forwarder: %s", exc); break

    threading.Thread(target=forwarder, daemon=True, name="forwarder").start()

    # ── Main WS receive loop ─────────────────────────────────────────────
    try:
        while True:
            raw = ws.receive()
            if raw is None: break
            msg = json.loads(raw)

            if msg.get("type") == "audio":
                pcm = base64.b64decode(msg["data"])
                try: audio_in.put_nowait(pcm)
                except queue.Full: pass

            elif msg.get("type") == "voice_change":
                new_voice = msg.get("voice", "").strip()
                if not new_voice or new_voice == voice_holder[0]:
                    continue
                log.info("Voice change: %s → %s", voice_holder[0], new_voice)
                voice_holder[0] = new_voice
                # Signal current session to wind down
                session_stop.set()
                # Discard stale queued audio before new session starts
                while not audio_in.empty():
                    try: audio_in.get_nowait()
                    except queue.Empty: break
                # Wait for the old session thread to exit (3 s generous max)
                if gemini_thread[0]:
                    gemini_thread[0].join(timeout=3)
                # Reset and spin up a fresh session with the new voice
                session_stop.clear()
                _start_session()

    except Exception as exc:
        log.info("WS closed: %s", exc)
    finally:
        ws_stop.set()
        log.info("Session teardown complete")


if __name__ == "__main__":
    if not API_KEY or API_KEY.strip() == "your_api_key_here":
        raise SystemExit("ERROR: Set GEMINI_API_KEY in .env before starting.")
    _start_telegram_bot()
    app.run(host="0.0.0.0", port=5000, debug=False)
