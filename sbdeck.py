"""
SBDeck — programa de la PC (Windows 10/11).
Autor: Ulises Agustin Rodriguez · https://uarodriguez.com · Licencia MIT

Sirve la web de la tablet, le manda los datos en vivo por WebSocket y ejecuta lo que
tocás en los botones. Vive como un ícono en la bandeja del sistema, al lado del reloj.

Carpetas:
  web/       la página de la tablet (index.html y el logo)
  sonidos/   los .mp3 / .wav que usan los botones de sonido
  tools/     MultiMonitorTool (lo baja SBDeck.bat)
  data/      config.json, deck.json (tus botones), tareas.json y sbdeck.log  (no se sube a GitHub)
"""
import sys
sys.coinit_flags = 0  # COM en modo multihilo; tiene que ir antes de importar comtypes/pycaw

import asyncio
import base64
import ctypes
import datetime as dt
import glob
import io
import ipaddress
import json
import logging
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import warnings
import webbrowser
from ctypes import wintypes
from pathlib import Path

import numpy as np
import psutil
from aiohttp import WSMsgType, web

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
DATA = BASE / "data"
SOUNDS = BASE / "sonidos"
DATA.mkdir(exist_ok=True)
SOUNDS.mkdir(exist_ok=True)
CONFIG_FILE = DATA / "config.json"
DECK_FILE = DATA / "deck.json"
TASKS_FILE = DATA / "tareas.json"
STATE_FILE = DATA / "estado.json"
TOOLS = BASE / "tools"
MMT = TOOLS / "MultiMonitorTool.exe"
FIRST_RUN = not CONFIG_FILE.exists()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(DATA / "sbdeck.log", encoding="utf-8"), logging.StreamHandler(sys.stdout)]
    if sys.stdout else [logging.FileHandler(DATA / "sbdeck.log", encoding="utf-8")],
)
log = logging.getLogger("sbdeck")


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, obj):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


CFG = {"puerto": 8765, "solo_red_local": True, "ping_a": "1.1.1.1", "actualizar_al_iniciar": True}
CFG.update(load_json(CONFIG_FILE, {}))
save_json(CONFIG_FILE, CFG)

# ---------- dependencias opcionales: si falta una, esa parte se apaga y el resto sigue ----------
try:
    import comtypes
    from comtypes import CLSCTX_ALL
    from pycaw.constants import CLSID_MMDeviceEnumerator
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume, IAudioMeterInformation, IMMDeviceEnumerator
    HAS_CAW = True
except Exception as e:
    HAS_CAW = False
    log.warning("Mezclador desactivado (pycaw): %s", e)
try:
    import pyaudiowpatch as pyaudio
    HAS_PA = True
except Exception as e:
    HAS_PA = False
    log.warning("Espectro desactivado (PyAudioWPatch): %s", e)
try:
    import keyboard
    HAS_KB = True
except Exception as e:
    HAS_KB = False
    log.warning("Atajos desactivados (keyboard): %s", e)
try:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as MediaManager
    from winrt.windows.storage.streams import DataReader
    HAS_WINRT = True
    try:
        from winrt.windows.storage.streams import Buffer, InputStreamOptions
        HAS_BUF = True
    except Exception:
        HAS_BUF = False
except Exception as e:
    HAS_WINRT = False
    log.warning("Modo Música desactivado (winrt): %s", e)
try:
    from PIL import Image, ImageDraw
    HAS_PIL = True
except Exception:
    HAS_PIL = False
try:
    import pystray
    HAS_TRAY = HAS_PIL
except Exception:
    HAS_TRAY = False
try:
    from icoextract import IconExtractor
    HAS_ICO = True
except Exception:
    HAS_ICO = False
try:
    import pythoncom
    import win32com.client
    HAS_WIN32 = True
except Exception:
    HAS_WIN32 = False

CREATE_NO_WINDOW = 0x08000000


def now_hm():
    return dt.datetime.now().strftime("%H:%M")


# =====================================================================================
# Sensores: MSI Afterburner (memoria compartida MAHM) + psutil
# =====================================================================================
FILE_MAP_READ = 0x0004
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.OpenFileMappingW.restype = wintypes.HANDLE
k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.MapViewOfFile.restype = ctypes.c_void_p
k32.MapViewOfFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
MAHM_SIG = 0x4D41484D  # 'MAHM'


def read_afterburner():
    """Devuelve {nombre del sensor: valor} tal como los muestra Afterburner. {} si no está abierto."""
    h = k32.OpenFileMappingW(FILE_MAP_READ, False, "MAHMSharedMemory")
    if not h:
        return {}
    try:
        p = k32.MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
        if not p:
            return {}
        try:
            sig, _ver, hsize, n, esize = struct.unpack_from("<5I", ctypes.string_at(p, 20))
            if sig != MAHM_SIG or n == 0 or n > 1024:
                return {}
            raw = ctypes.string_at(p, hsize + n * esize)
        finally:
            k32.UnmapViewOfFile(p)
    finally:
        k32.CloseHandle(h)
    out = {}
    for i in range(n):
        off = hsize + i * esize
        name = raw[off:off + 260].split(b"\0", 1)[0].decode("latin-1").strip()
        val = struct.unpack_from("<f", raw, off + 260 * 5)[0]
        if name and name not in out and abs(val) < 1e7:
            out[name] = val
    return out


class Pinger(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.ms = None

    def run(self):
        while True:
            t = time.perf_counter()
            try:
                with socket.create_connection((CFG["ping_a"], 443), timeout=2):
                    self.ms = (time.perf_counter() - t) * 1000
            except OSError:
                self.ms = None
            time.sleep(5)


PINGER = Pinger()
_net = {"t": time.time(), "r": psutil.net_io_counters().bytes_recv, "s": psutil.net_io_counters().bytes_sent}
psutil.cpu_percent(None)


def pick(ab, *names):
    for n in names:
        v = ab.get(n)
        if v is not None:
            return v
    return None


def read_stats():
    try:
        ab = read_afterburner()
    except Exception:
        ab = {}
    fps = pick(ab, "Framerate")
    if fps is not None and fps <= 0:
        fps = None
    clocks = [v for k, v in ab.items() if re.fullmatch(r"CPU\d* clock", k)]
    vram = pick(ab, "Memory usage")
    vm = psutil.virtual_memory()
    io_ = psutil.net_io_counters()
    t = time.time()
    span = max(0.2, t - _net["t"])
    down = (io_.bytes_recv - _net["r"]) / span / 1e6
    up = (io_.bytes_sent - _net["s"]) / span / 1e6
    _net.update(t=t, r=io_.bytes_recv, s=io_.bytes_sent)
    freq = None
    if clocks:
        freq = max(clocks) / 1000
    else:
        try:
            freq = psutil.cpu_freq().current / 1000
        except Exception:
            pass
    return {
        "fps": fps,
        "low": pick(ab, "Framerate 1% low") if fps else None,
        "ft": pick(ab, "Frametime") if fps else None,
        "cpu": {"t": pick(ab, "CPU temperature"), "u": psutil.cpu_percent(None), "c": freq},
        "gpu": {"t": pick(ab, "GPU temperature", "GPU1 temperature"), "u": pick(ab, "GPU usage", "GPU1 usage"),
                "v": vram / 1024 if vram is not None else None},
        "ram": {"used": (vm.total - vm.available) / 2**30, "total": round(vm.total / 2**30)},
        "net": {"d": max(0, down), "u": max(0, up), "p": PINGER.ms},
    }


# =====================================================================================
# Íconos reales: de programas instalados (.lnk / .exe / .ico) y de sitios web
# =====================================================================================
ICON_CACHE = {}
ICON_FAILED = set()


def to_dataurl(img, size=128, fmt="PNG"):
    img = img.convert("RGBA") if fmt == "PNG" else img.convert("RGB")
    w, h = img.size
    if fmt == "JPEG" and w and h and abs(w / h - 1) > 0.15:  # portadas 16:9 -> recorte cuadrado al centro
        s = min(w, h)
        img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, fmt, quality=82) if fmt == "JPEG" else img.save(buf, fmt)
    mime = "image/jpeg" if fmt == "JPEG" else "image/png"
    return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode()


def open_ico(data):
    im = Image.open(io.BytesIO(data))
    try:
        sizes = im.ico.sizes()
        im = im.ico.getimage(max(sizes))
    except Exception:
        pass
    return im


def start_menu_apps():
    dirs = [
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.path.expanduser("~"), "Desktop"),
        os.path.expandvars(r"%PUBLIC%\Desktop"),
    ]
    seen = {}
    for d in dirs:
        for p in glob.glob(os.path.join(d, "**", "*.lnk"), recursive=True):
            name = Path(p).stem
            low = name.lower()
            if any(w in low for w in ("uninstall", "desinstal", "readme", "léame", "help", "ayuda")):
                continue
            seen.setdefault(low, {"name": name, "path": p})
    return sorted(seen.values(), key=lambda a: a["name"].lower())


def resolve_app(name):
    if not name:
        return None
    if os.path.exists(name):
        return name
    low = name.lower()
    apps = start_menu_apps()
    exact = [a for a in apps if a["name"].lower() == low]
    if exact:
        return exact[0]["path"]
    part = sorted((a for a in apps if low in a["name"].lower()), key=lambda a: len(a["name"]))
    if part:
        return part[0]["path"]
    import shutil
    return shutil.which(name)


def icon_for_file(path):
    if not HAS_PIL or not path:
        return None
    if path in ICON_CACHE:
        return ICON_CACHE[path]
    target, cands = path, []
    if path.lower().endswith(".lnk") and HAS_WIN32:
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        except Exception:
            pass
        try:
            lnk = win32com.client.Dispatch("WScript.Shell").CreateShortcut(path)
            target = lnk.TargetPath or path
            loc = lnk.IconLocation or ""
            f, _, n = loc.rpartition(",")
            f = os.path.expandvars(f.strip('"'))
            if f:
                cands.append((f, int(n or 0)))
        except Exception as e:
            log.info("No pude leer el acceso directo %s: %s", path, e)
    cands.append((target, 0))
    result = None
    for f, n in cands:
        if not f or not os.path.exists(f):
            continue
        try:
            if f.lower().endswith(".ico"):
                result = to_dataurl(open_ico(Path(f).read_bytes()))
            elif HAS_ICO and f.lower().endswith((".exe", ".dll")):
                ex = IconExtractor(f)
                try:
                    data = ex.get_icon(num=max(n, 0))
                except Exception:
                    data = ex.get_icon(num=0)
                result = to_dataurl(open_ico(data.getvalue()))
        except Exception as e:
            log.info("Sin ícono en %s: %s", f, e)
        if result:
            break
    ICON_CACHE[path] = result
    return result


def icon_for_url(url):
    if not HAS_PIL:
        return None
    host = urllib.parse.urlparse(url if "://" in url else "https://" + url).hostname
    if not host:
        return None
    if host in ICON_CACHE:
        return ICON_CACHE[host]
    result = None
    for u in (f"https://www.google.com/s2/favicons?domain={host}&sz=128", f"https://{host}/favicon.ico"):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=6).read()
            im = open_ico(data) if data[:4] == b"\0\0\1\0" else Image.open(io.BytesIO(data))
            if im.size[0] >= 16:
                result = to_dataurl(im)
                break
        except Exception:
            continue
    ICON_CACHE[host] = result
    return result


def real_icon(kind, param):
    if kind == "app":
        return icon_for_file(resolve_app(param))
    if kind == "url":
        return icon_for_url(param)
    return None


# =====================================================================================
# Audio: mezclador por aplicación (pycaw) y espectro (soundcard)
# =====================================================================================
PRETTY = {"chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "discord": "Discord", "spotify": "Spotify",
          "obs64": "OBS", "steam": "Steam", "vlc": "VLC", "ms-teams": "Teams", "zoom": "Zoom",
          "whatsapp": "WhatsApp", "telegram": "Telegram", "opera": "Opera", "brave": "Brave"}
GLYPH = {"discord": "chat", "chrome": "globe", "msedge": "globe", "firefox": "globe", "opera": "globe",
         "brave": "globe", "spotify": "music", "vlc": "music", "steam": "game"}


def _activate(dev, iface):
    return dev.Activate(iface._iid_, CLSCTX_ALL, None).QueryInterface(iface)


def default_endpoint(flow):  # 0 = salida, 1 = entrada
    en = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
    return en.GetDefaultAudioEndpoint(flow, 0)


class Audio:
    def __init__(self):
        self.ok = HAS_CAW
        self.groups = {}
        self.spk_vol = self.spk_meter = self.mic_vol = None
        self.ep_t = 0
        if self.ok:
            self.endpoints()

    def endpoints(self):
        self.ep_t = time.time()
        try:
            spk = default_endpoint(0)
            self.spk_vol = _activate(spk, IAudioEndpointVolume)
            self.spk_meter = _activate(spk, IAudioMeterInformation)
        except Exception as e:
            log.warning("Sin salida de audio: %s", e)
            self.spk_vol = self.spk_meter = None
        try:
            self.mic_vol = _activate(default_endpoint(1), IAudioEndpointVolume)
        except Exception as e:
            log.warning("Sin micrófono: %s", e)
            self.mic_vol = None

    def refresh(self):
        if not self.ok:
            return
        if time.time() - self.ep_t > 10:
            self.endpoints()
        groups = {}
        for s in AudioUtilities.GetAllSessions():
            p = s.Process
            if p is None:
                continue
            try:
                key = p.name().lower()
            except Exception:
                continue
            key = key[:-4] if key.endswith(".exe") else key
            g = groups.setdefault(key, {"name": PRETTY.get(key, key.capitalize()), "vols": [], "meters": [], "exe": None})
            g["vols"].append(s.SimpleAudioVolume)
            try:
                g["meters"].append(s._ctl.QueryInterface(IAudioMeterInformation))
            except Exception:
                pass
            if not g["exe"]:
                try:
                    g["exe"] = p.exe()
                except Exception:
                    pass
        self.groups = groups

    def master(self):
        try:
            return {"vol": round(self.spk_vol.GetMasterVolumeLevelScalar(), 3), "muted": bool(self.spk_vol.GetMute())}
        except Exception:
            return {"vol": 0, "muted": False}

    def state(self):
        st = {"master": self.master()}
        for k, g in self.groups.items():
            try:
                sv = g["vols"][0]
                st[k] = {"vol": round(sv.GetMasterVolume(), 3), "muted": bool(sv.GetMute())}
            except Exception:
                pass
        return st

    def mixer(self):
        st = self.state()
        out = [{"id": "master", "name": "General", "glyph": "speaker", **st["master"]}]
        for k in sorted(self.groups, key=lambda k: self.groups[k]["name"].lower()):
            if k not in st:
                continue
            g = self.groups[k]
            out.append({"id": k, "name": g["name"], "glyph": GLYPH.get(k, "speaker"),
                        "icon": ICON_CACHE.get(g["exe"]) if g["exe"] else None, **st[k]})
        return out[:8]

    def levels(self):
        lv = {}
        try:
            lv["master"] = round(self.spk_meter.GetPeakValue(), 3)
        except Exception:
            lv["master"] = 0
        for k, g in self.groups.items():
            peak = 0
            for m in g["meters"]:
                try:
                    peak = max(peak, m.GetPeakValue())
                except Exception:
                    pass
            lv[k] = round(peak, 3)
        return lv

    def set_volume(self, key, v):
        v = max(0.0, min(1.0, float(v)))
        if key == "master":
            self.spk_vol.SetMasterVolumeLevelScalar(v, None)
            return
        for sv in self.groups.get(key, {}).get("vols", []):
            try:
                sv.SetMasterVolume(v, None)
            except Exception:
                pass

    def set_mute(self, key, m):
        if key == "master":
            self.spk_vol.SetMute(bool(m), None)
            return
        for sv in self.groups.get(key, {}).get("vols", []):
            try:
                sv.SetMute(bool(m), None)
            except Exception:
                pass

    def mic_muted(self):
        try:
            return bool(self.mic_vol.GetMute())
        except Exception:
            return False

    def toggle_mic(self):
        if self.mic_vol is None:
            raise RuntimeError("no hay micrófono predeterminado")
        self.mic_vol.SetMute(not self.mic_muted(), None)
        return self.mic_muted()


SPEC_ON = threading.Event()
N_FFT = 2048
WIN = np.hanning(N_FFT).astype(np.float32)
_EDGES = {}


def to_bands(x, rate):
    if rate not in _EDGES:
        freqs = np.fft.rfftfreq(N_FFT, 1 / rate)
        _EDGES[rate] = np.searchsorted(freqs, np.geomspace(40, min(16000, rate / 2 - 100), 33))
    idx = _EDGES[rate]
    mag = np.abs(np.fft.rfft(x * WIN)) / (N_FFT / 4)
    out = []
    for a, b in zip(idx[:-1], idx[1:]):
        v = mag[a:max(b, a + 1)].max()
        db = 20 * np.log10(v + 1e-9)
        out.append(round(float(min(1, max(0, (db + 70) / 66))), 2))
    return out


class Analyzer:
    """Escucha el micrófono o lo que suena en la PC (loopback WASAPI) y calcula 32 bandas."""

    def __init__(self, kind):
        self.kind = kind
        self.bands = [0] * 32
        self.t = 0
        self.buf = np.zeros(N_FFT, np.float32)
        self.stream = None
        self.rate = 48000
        self.ch = 1
        self.err = None

    def device(self, pa):
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        if self.kind == "mic":
            return pa.get_device_info_by_index(wasapi["defaultInputDevice"])
        spk = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if spk.get("isLoopbackDevice"):
            return spk
        for lb in pa.get_loopback_device_info_generator():
            if spk["name"] in lb["name"]:
                return lb
        raise RuntimeError("no encontré la salida de audio en modo loopback")

    def start(self, pa):
        dev = self.device(pa)
        self.ch = max(1, int(dev["maxInputChannels"]))
        self.rate = int(dev["defaultSampleRate"])
        self.stream = pa.open(format=pyaudio.paFloat32, channels=self.ch, rate=self.rate, input=True,
                              input_device_index=dev["index"], frames_per_buffer=1024, stream_callback=self._cb)
        self.err = None
        log.info("Espectro %s: %s", self.kind, dev["name"])

    def _cb(self, data, frames, info, status):
        x = np.frombuffer(data, dtype=np.float32)
        if self.ch > 1:
            x = x.reshape(-1, self.ch).mean(axis=1)
        self.buf = np.concatenate([self.buf[len(x):], x]) if len(x) < N_FFT else x[-N_FFT:]
        self.bands = to_bands(self.buf, self.rate)
        self.t = time.time()
        return (None, pyaudio.paContinue)

    def stop(self):
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
        except Exception:
            pass
        self.stream = None

    def get(self):
        # sin datos recientes = no está sonando nada (el loopback no manda nada en silencio)
        return self.bands if time.time() - self.t < 0.5 else [0] * 32


class SpectrumThread(threading.Thread):
    """Abre la captura solo mientras alguien mira el modo Audio, y la cierra después."""

    def __init__(self, analyzers):
        super().__init__(daemon=True)
        self.analyzers = analyzers

    def run(self):
        pa, opened = None, 0
        while True:
            want = SPEC_ON.is_set()
            if want and pa is None:
                try:
                    pa = pyaudio.PyAudio()
                    opened = time.time()
                    for a in self.analyzers:
                        try:
                            a.start(pa)
                        except Exception as e:
                            a.err = f"{a.kind}: {e}"
                            log.warning("Espectro (%s): %s", a.kind, e)
                except Exception as e:
                    log.warning("No pude abrir el audio: %s", e)
                    pa = None
                    time.sleep(3)
            elif pa is not None and (not want or time.time() - opened > 120):
                # se cierra al salir del modo, y se reabre cada 2 min por si cambiaste de auriculares/parlantes
                for a in self.analyzers:
                    a.stop()
                try:
                    pa.terminate()
                except Exception:
                    pass
                pa = None
            time.sleep(0.3)


# =====================================================================================
# Música: sesión multimedia de Windows (YouTube Music en Chrome, Spotify, etc.)
# =====================================================================================
class Media:
    def __init__(self):
        self.mgr = None
        self.key = None
        self.art = None
        self.art_tries = 0
        self.art_t = 0
        self.err = None

    async def session(self):
        if not HAS_WINRT:
            return None
        if self.mgr is None:
            self.mgr = await MediaManager.request_async()
        return self.mgr.get_current_session()

    @staticmethod
    def source(aumid):
        a = (aumid or "").lower()
        for k, v in (("chrome", "Chrome"), ("msedge", "Edge"), ("spotify", "Spotify"), ("firefox", "Firefox"),
                     ("opera", "Opera"), ("brave", "Brave")):
            if k in a:
                return v
        return (aumid or "").split("!")[0].split(".")[0] or "Windows"

    async def thumb(self, props):
        if not HAS_PIL or props.thumbnail is None:
            return None
        stream = await props.thumbnail.open_read_async()
        size = int(stream.size)
        if size <= 0:
            return None
        data, errs = None, []
        if HAS_BUF:
            try:
                buf = Buffer(size)
                res = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
                data = bytes(res)
            except Exception as e:
                errs.append(f"read_async: {e}")
        if not data:
            try:
                reader = DataReader(stream.get_input_stream_at(0))
                await reader.load_async(size)
                arr = bytearray(size)
                reader.read_bytes(arr)
                data = bytes(arr)
            except Exception as e:
                errs.append(f"read_bytes: {e}")
        if not data:
            try:
                reader = DataReader(stream.get_input_stream_at(0))
                await reader.load_async(size)
                data = bytes(memoryview(reader.read_buffer(size)))
            except Exception as e:
                errs.append(f"read_buffer: {e}")
        if not data:
            raise RuntimeError("; ".join(errs))
        return to_dataurl(Image.open(io.BytesIO(data)), 320, "JPEG")

    async def info(self):
        s = await self.session()
        if s is None:
            return None, False
        props = await s.try_get_media_properties_async()
        tl = s.get_timeline_properties()
        pb = s.get_playback_info()
        playing = int(pb.playback_status) == 4
        dur = (tl.end_time - tl.start_time).total_seconds()
        pos = (tl.position - tl.start_time).total_seconds()
        if playing:
            try:
                pos += (dt.datetime.now(dt.timezone.utc) - tl.last_updated_time).total_seconds()
            except Exception:
                pass
        key = (props.title, props.artist)
        art_changed = False
        if key != self.key:
            self.key, self.art, self.art_tries, self.art_t = key, None, 0, 0
            art_changed = True
        # el navegador a veces manda la portada unos segundos después del título: reintento
        if self.art is None and self.art_tries < 8 and time.time() - self.art_t > 2:
            self.art_tries += 1
            self.art_t = time.time()
            try:
                art = await self.thumb(props)
                if art:
                    self.art, art_changed, self.err = art, True, None
            except Exception as e:
                self.err = f"portada: {e}"
                if self.art_tries == 1:
                    log.info("No pude leer la portada: %s", e)
        info = {"title": props.title, "artist": " · ".join(x for x in (props.artist, props.album_title) if x),
                "src": self.source(s.source_app_user_model_id), "playing": playing,
                "pos": max(0, min(pos, dur)) if dur > 0 else 0, "dur": max(0, dur)}
        return info, art_changed

    async def cmd(self, c, pos=None):
        s = await self.session()
        if s is None:
            if HAS_KB:
                keyboard.send({"play": "play/pause media", "next": "next track", "prev": "previous track"}.get(c, "play/pause media"))
            return
        if c == "play":
            await s.try_toggle_play_pause_async()
        elif c == "next":
            await s.try_skip_next_async()
        elif c == "prev":
            await s.try_skip_previous_async()
        elif c == "seek" and pos is not None:
            await s.try_change_playback_position_async(int(float(pos) * 10_000_000))


# =====================================================================================
# Acciones de los botones
# =====================================================================================
winmm = ctypes.WinDLL("winmm")
winmm.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.UINT, wintypes.HANDLE]


def play_sound(name):
    p = Path(name)
    if not p.is_absolute():
        p = SOUNDS / name
    if not p.exists():
        raise FileNotFoundError(f"no está {p.name} en la carpeta sonidos")

    def run():
        alias = f"sb{threading.get_ident()}"
        if winmm.mciSendStringW(f'open "{p}" type mpegvideo alias {alias}', None, 0, None) == 0:
            winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
            winmm.mciSendStringW(f"close {alias}", None, 0, None)

    threading.Thread(target=run, daemon=True).start()


def open_app(param):
    target = resolve_app(param)
    if not target:
        raise FileNotFoundError(f"no encontré «{param}»")
    os.startfile(target)


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32), ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD), ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


def list_displays():
    """Monitores encendidos, con cuál es el principal de Windows (el de la barra de tareas)."""
    out, i = [], 0
    while True:
        d = DISPLAY_DEVICEW()
        d.cb = ctypes.sizeof(d)
        if not ctypes.windll.user32.EnumDisplayDevicesW(None, i, ctypes.byref(d), 0):
            break
        if d.StateFlags & 0x1:
            out.append({"name": d.DeviceName, "primary": bool(d.StateFlags & 0x4)})
        i += 1
    return out


def mmt(*args):
    subprocess.run([str(MMT), *args], creationflags=CREATE_NO_WINDOW, timeout=20)


def run_display(param, on):
    """solo-principal / solo:N apagan los demás monitores; al volver a tocar (o «extender») se prenden."""
    param = (param or "solo-principal").lower()
    st = load_json(STATE_FILE, {})
    off = st.get("apagados", [])
    restore = param == "extender" or on is False or (on is None and off)
    if restore:
        if off and MMT.exists():
            mmt("/enable", *off)
        else:
            subprocess.Popen(["DisplaySwitch.exe", "/extend"], creationflags=CREATE_NO_WINDOW)
        st["apagados"] = []
        save_json(STATE_FILE, st)
        return
    if not MMT.exists():
        raise RuntimeError("falta tools\\MultiMonitorTool.exe: abrí SBDeck.bat")
    disp = list_displays()
    m = re.fullmatch(r"solo:(\d+)", param)
    keep = rf"\\.\DISPLAY{m.group(1)}" if m else next((d["name"] for d in disp if d["primary"]), None)
    names = [d["name"] for d in disp]
    if keep not in names:
        raise RuntimeError(f"no encontré el monitor {keep}")
    to_off = [n for n in names if n != keep]
    if not to_off:
        return
    if m and not next(d["primary"] for d in disp if d["name"] == keep):
        mmt("/SetPrimary", keep)
    mmt("/disable", *to_off)
    st["apagados"] = to_off
    save_json(STATE_FILE, st)
    log.info("Monitores apagados: %s (queda %s)", to_off, keep)


# =====================================================================================
# Servidor
# =====================================================================================
class Hub:
    def __init__(self):
        self.clients = {}
        self.last_media = None
        self.art = None

    def want(self, mode):
        return any(c.get("mode") == mode for c in self.clients.values())

    async def send(self, ws, type_, data):
        try:
            await ws.send_str(json.dumps({"type": type_, "data": data}, separators=(",", ":")))
        except Exception:
            pass

    async def broadcast(self, type_, data, skip=None):
        if not self.clients:
            return
        msg = json.dumps({"type": type_, "data": data}, separators=(",", ":"))
        for ws in list(self.clients):
            if ws is skip:
                continue
            try:
                await ws.send_str(msg)
            except Exception:
                pass


HUB = Hub()
AUDIO = Audio()
MEDIA = Media()
MIC_AN = Analyzer("mic")
OUT_AN = Analyzer("out")
TRAY = None
LOOP = None
STOP = None


async def in_thread(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


async def fill_icons(deck):
    """Busca el logo real de los botones de programas y webs que todavía no tienen imagen."""
    changed = False
    for p in deck.get("pages", []):
        for k in p.get("keys", []):
            if k.get("img") or k.get("a") not in ("app", "url") or not k.get("p"):
                continue
            key = (k["a"], k["p"])
            if key in ICON_FAILED:
                continue
            img = await in_thread(real_icon, k["a"], k["p"])
            if img:
                k["img"] = img
                changed = True
            else:
                ICON_FAILED.add(key)
    if changed:
        save_json(DECK_FILE, deck)
        await HUB.broadcast("deck", deck)


async def do_action(ws, d):
    kind, param, on = d.get("kind"), (d.get("param") or "").strip(), d.get("on")
    try:
        if kind == "app":
            await in_thread(open_app, param)
        elif kind == "url":
            webbrowser.open(param if "://" in param else "https://" + param)
        elif kind == "hotkey":
            if not HAS_KB:
                raise RuntimeError("falta la librería keyboard")
            keyboard.send(param)
        elif kind == "mic":
            muted = AUDIO.toggle_mic()
            await HUB.broadcast("mic", {"muted": muted})
        elif kind == "sound":
            play_sound(param)
        elif kind == "display":
            await in_thread(run_display, param, on)
        elif kind == "media":
            await MEDIA.cmd(param)
        elif kind == "cmd":
            subprocess.Popen(param, shell=True, creationflags=CREATE_NO_WINDOW)
        else:
            raise ValueError(f"acción desconocida: {kind}")
        log.info("Acción %s · %s", kind, param)
    except Exception as e:
        log.warning("Falló la acción %s · %s: %s", kind, param, e)
        await HUB.send(ws, "toast", f"No se pudo: {e}")


async def handle(ws, m):
    t, d = m.get("type"), m.get("data")
    if t in ("hello", "mode"):
        HUB.clients[ws]["mode"] = d.get("mode") if isinstance(d, dict) else d
    elif t == "action":
        await do_action(ws, d or {})
    elif t == "volume" and AUDIO.ok:
        AUDIO.set_volume(d["id"], d["vol"])
        if d["id"] == "master":
            await HUB.broadcast("master", {"vol": d["vol"]}, skip=ws)
    elif t == "mute" and AUDIO.ok:
        AUDIO.set_mute(d["id"], d["muted"])
    elif t == "media":
        try:
            await MEDIA.cmd(d.get("cmd"), d.get("pos"))
        except Exception as e:
            log.info("Multimedia: %s", e)
    elif t == "tasks" and isinstance(d, list):
        save_json(TASKS_FILE, d)
        await HUB.broadcast("tasks", d, skip=ws)
    elif t == "deck" and isinstance(d, dict):
        save_json(DECK_FILE, d)
        await HUB.broadcast("deck", d, skip=ws)
        asyncio.ensure_future(fill_icons(d))
    elif t == "req":
        rid, what = d.get("rid"), d.get("what")
        try:
            if what == "apps":
                result = await in_thread(start_menu_apps)
            elif what == "icon":
                result = {"img": await in_thread(real_icon, d.get("kind"), d.get("param"))}
            elif what == "status":
                result = await in_thread(status_list)
            else:
                result = None
            await HUB.send(ws, "res", {"rid": rid, "result": result})
        except Exception as e:
            await HUB.send(ws, "res", {"rid": rid, "result": None, "error": str(e)})


def allowed(request):
    if not CFG.get("solo_red_local", True):
        return True
    try:
        ip = ipaddress.ip_address((request.remote or "").split("%")[0])
        return ip.is_private or ip.is_loopback
    except ValueError:
        return False


async def ws_handler(request):
    if not allowed(request):
        raise web.HTTPForbidden()
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    HUB.clients[ws] = {"mode": None}
    log.info("Tablet conectada desde %s", request.remote)
    try:
        await HUB.send(ws, "deck", load_json(DECK_FILE, None))
        if AUDIO.ok:
            await HUB.send(ws, "mixer", AUDIO.mixer())
            await HUB.send(ws, "mic", {"muted": AUDIO.mic_muted()})
            await HUB.send(ws, "master", AUDIO.master())
        await HUB.send(ws, "media", HUB.last_media)
        await HUB.send(ws, "art", HUB.art)
        await HUB.send(ws, "tasks", load_json(TASKS_FILE, []))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    await handle(ws, json.loads(msg.data))
                except Exception as e:
                    log.warning("Mensaje con error: %s", e)
    finally:
        HUB.clients.pop(ws, None)
        log.info("Tablet desconectada")
    return ws


async def index(request):
    if not allowed(request):
        raise web.HTTPForbidden()
    return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


async def api_tasks(request):
    """Para tus propios scripts: POST /api/tareas con {"t": "texto"} agrega una tarea."""
    if not allowed(request):
        raise web.HTTPForbidden()
    d = await request.json()
    tasks = load_json(TASKS_FILE, [])
    tasks.append({"id": int(time.time() * 1000), "t": str(d.get("t", ""))[:120], "d": False})
    save_json(TASKS_FILE, tasks)
    await HUB.broadcast("tasks", tasks)
    return web.json_response({"ok": True})


async def api_quit(request):
    """Lo usa una copia nueva de SBDeck para cerrar la anterior (solo desde la misma PC)."""
    if not (request.remote or "").startswith(("127.", "::1")):
        raise web.HTTPForbidden()
    LOOP.call_later(0.2, STOP.set)
    return web.json_response({"ok": True})


def status_list():
    ab = bool(read_afterburner())
    spec_err = MIC_AN.err or OUT_AN.err
    return [
        {"name": "MSI Afterburner (FPS y temperaturas)", "ok": ab,
         "note": None if ab else "Abrí Afterburner y activá los sensores en Configuración → Supervisión"},
        {"name": "Mezclador por aplicación", "ok": bool(AUDIO.ok and AUDIO.spk_vol is not None)},
        {"name": "Espectro de audio", "ok": HAS_PA and not spec_err,
         "note": ("Falta PyAudioWPatch: abrí SBDeck.bat" if not HAS_PA else spec_err)},
        {"name": "Música de Windows y portadas", "ok": HAS_WINRT and not MEDIA.err, "note": MEDIA.err},
        {"name": "Atajos de teclado", "ok": HAS_KB},
        {"name": "Apagar monitores", "ok": MMT.exists(), "note": None if MMT.exists() else "Abrí SBDeck.bat para bajar MultiMonitorTool"},
        {"name": "Logos reales de programas", "ok": HAS_ICO and HAS_WIN32},
        {"name": "Se abre solo al prender la PC", "ok": startup_enabled(),
         "note": None if startup_enabled() else "Clic derecho en el ícono de SBDeck → Iniciar con Windows"},
    ]


# ---------- actualizaciones desde GitHub ----------
def git(*args, timeout=60):
    return subprocess.run(["git", *args], cwd=BASE, capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW, timeout=timeout)


def git_update():
    """git pull. Devuelve (hubo_cambios, archivos o mensaje)."""
    import shutil
    if not (BASE / ".git").exists() or not shutil.which("git"):
        return False, "SBDeck no está instalado con git"
    before = git("rev-parse", "HEAD").stdout.strip()
    r = git("pull", "--ff-only")
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[:200]
    after = git("rev-parse", "HEAD").stdout.strip()
    if before == after:
        return False, "ya estaba al día"
    changed = git("diff", "--name-only", before, after).stdout.split()
    if "requirements.txt" in changed:
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=BASE,
                       creationflags=CREATE_NO_WINDOW, timeout=600)
    log.info("Actualizado a %s: %s", after[:7], changed)
    return True, changed


def restart():
    subprocess.Popen([sys.executable, str(BASE / "sbdeck.py")], cwd=BASE)
    LOOP.call_soon_threadsafe(STOP.set)


def update_and_maybe_restart(notify=False):
    try:
        changed, info = git_update()
    except Exception as e:
        changed, info = False, str(e)
    if changed and any(f.endswith(".py") or f == "requirements.txt" for f in info):
        restart()
    elif notify and TRAY:
        try:
            TRAY.notify("Actualizado" if changed else f"Sin cambios: {info}", "SBDeck")
        except Exception:
            pass


# ---------- bucles ----------
async def guard(name, coro_fn):
    while True:
        try:
            await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("Se cayó %s, lo reinicio: %s", name, e)
            await asyncio.sleep(2)


async def stats_loop():
    while True:
        if HUB.clients:
            await HUB.broadcast("stats", await in_thread(read_stats))
        await asyncio.sleep(1)


async def audio_loop():
    last_ids, last_mic, t_refresh = None, None, 0
    while True:
        if HUB.clients and AUDIO.ok and time.time() - t_refresh > 1.5:
            t_refresh = time.time()
            AUDIO.refresh()
            ids = tuple(sorted(AUDIO.groups))
            missing = [g["exe"] for g in AUDIO.groups.values() if g["exe"] and g["exe"] not in ICON_CACHE]
            if ids != last_ids:
                last_ids = ids
                await HUB.broadcast("mixer", AUDIO.mixer())
            if missing:
                for exe in missing:
                    await in_thread(icon_for_file, exe)
                await HUB.broadcast("mixer", AUDIO.mixer())
            await HUB.broadcast("mixstate", AUDIO.state())
            mic = AUDIO.mic_muted()
            if mic != last_mic:
                last_mic = mic
                await HUB.broadcast("mic", {"muted": mic})
        if HUB.want("audio"):
            SPEC_ON.set()
            if AUDIO.ok:
                await HUB.broadcast("levels", AUDIO.levels())
            err = None if HAS_PA else "Falta PyAudioWPatch: abrí SBDeck.bat en la PC"
            if HAS_PA and (MIC_AN.err or OUT_AN.err):
                err = "No se pudo escuchar el audio (" + (OUT_AN.err or MIC_AN.err) + ")"
            await HUB.broadcast("spectrum", {"mic": MIC_AN.get(), "out": OUT_AN.get(), "err": err})
            await asyncio.sleep(0.06)
        else:
            SPEC_ON.clear()
            await asyncio.sleep(0.3)


async def media_loop():
    if not HAS_WINRT:
        return
    last_sent = 0
    while True:
        if HUB.clients:
            info, art_changed = await MEDIA.info()
            prev = HUB.last_media
            changed = (info is None) != (prev is None) or (info and prev and (
                info["title"] != prev["title"] or info["playing"] != prev["playing"]
                or abs(info["pos"] - prev["pos"]) > 3 or info["dur"] != prev["dur"]))
            HUB.last_media = info
            if art_changed:
                HUB.art = MEDIA.art
                await HUB.broadcast("art", HUB.art)
            if changed or time.time() - last_sent > 5:
                last_sent = time.time()
                await HUB.broadcast("media", info)
        await asyncio.sleep(1)


async def web_watch_loop():
    def stamp():
        return max((f.stat().st_mtime for f in WEB.rglob("*") if f.is_file()), default=0)
    last = await in_thread(stamp)
    while True:
        await asyncio.sleep(2)
        now = await in_thread(stamp)
        if now != last:
            last = now
            log.info("Cambió la web: recargo la tablet")
            await HUB.broadcast("reload", None)


# ---------- bandeja del sistema ----------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_cmd():
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw}" "{BASE / "sbdeck.py"}"'


def startup_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "SBDeck")
            return True
    except OSError:
        return False


def toggle_startup():
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if startup_enabled():
            winreg.DeleteValue(k, "SBDeck")
        else:
            winreg.SetValueEx(k, "SBDeck", 0, winreg.REG_SZ, startup_cmd())


def tray_image():
    return Image.open(WEB / "logo.png").convert("RGBA")


def run_tray(url, port):
    global TRAY

    def quit_app(icon, _item):
        icon.stop()
        LOOP.call_soon_threadsafe(STOP.set)

    config_url = f"http://localhost:{port}/?editar"
    menu = pystray.Menu(
        pystray.MenuItem(f"Tablet: {url}", None, enabled=False),
        pystray.MenuItem("Configurar desde la PC", lambda: webbrowser.open(config_url), default=True),
        pystray.MenuItem("Abrir carpeta de sonidos", lambda: os.startfile(SOUNDS)),
        pystray.MenuItem("Buscar actualizaciones",
                         lambda: threading.Thread(target=update_and_maybe_restart, args=(True,), daemon=True).start()),
        pystray.MenuItem("Iniciar con Windows", lambda: toggle_startup(), checked=lambda _i: startup_enabled()),
        pystray.MenuItem("Ver registro", lambda: os.startfile(DATA / "sbdeck.log")),
        pystray.MenuItem("Salir", quit_app),
    )
    TRAY = pystray.Icon("SBDeck", tray_image(), f"SBDeck · {url}", menu)
    TRAY.run_detached()


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def alert(msg):
    ctypes.windll.user32.MessageBoxW(None, msg, "SBDeck", 0x30)


async def main():
    global LOOP, STOP
    LOOP = asyncio.get_running_loop()
    STOP = asyncio.Event()
    port = int(CFG["puerto"])
    url = f"http://{local_ip()}:{port}"

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/api/tareas", api_tasks)
    app.router.add_post("/api/salir", api_quit)
    app.router.add_static("/", WEB, show_index=False)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:  # si ya hay un SBDeck abierto (versión vieja), le pido que se cierre
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/salir", method="POST"), timeout=1)
    except Exception:
        pass
    for attempt in range(20):  # al reiniciarse tras una actualización, el proceso viejo tarda un instante en soltar el puerto
        try:
            await web.TCPSite(runner, "0.0.0.0", port).start()
            break
        except OSError:
            if attempt == 19:
                alert(f"El puerto {port} está ocupado. ¿SBDeck ya está abierto? Si no, cambiá el puerto en data\\config.json.")
                return
            await asyncio.sleep(0.5)

    log.info("SBDeck listo en %s  (Afterburner: %s, mezclador: %s, espectro: %s, música: %s)",
             url, "sí" if read_afterburner() else "no detectado", HAS_CAW, HAS_PA, HAS_WINRT)
    if FIRST_RUN and not startup_enabled():
        try:
            toggle_startup()
            log.info("Primera vez: SBDeck queda configurado para abrirse con Windows")
        except Exception as e:
            log.warning("No pude activar el inicio con Windows: %s", e)
    PINGER.start()
    if HAS_PA:
        SpectrumThread([MIC_AN, OUT_AN]).start()
    if CFG.get("actualizar_al_iniciar", True):
        threading.Thread(target=update_and_maybe_restart, daemon=True).start()
    deck = load_json(DECK_FILE, None)
    if deck:
        asyncio.ensure_future(fill_icons(deck))
    tasks = [asyncio.ensure_future(guard(n, f)) for n, f in
             (("sensores", stats_loop), ("audio", audio_loop), ("música", media_loop), ("web", web_watch_loop))]
    if HAS_TRAY:
        run_tray(url, port)
        if FIRST_RUN:
            try:
                TRAY.notify(f"SBDeck está andando. En la tablet abrí {url}", "SBDeck")
            except Exception:
                pass
    else:
        log.info("Sin ícono de bandeja (falta pystray/Pillow). Cerrá esta ventana para salir.")
    await STOP.wait()
    for t in tasks:
        t.cancel()
    await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
