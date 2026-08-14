from __future__ import annotations

import sys
import threading
import urllib.request
import urllib.parse
import urllib.error
import json
import os
import ctypes
import ctypes.wintypes
import subprocess
import hashlib

from PIL import Image, ImageEnhance, ImageOps

from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QSystemTrayIcon, QMenu,
    QDialog, QLineEdit, QFormLayout, QDialogButtonBox,
    QComboBox, QGraphicsDropShadowEffect, QSizePolicy,
    QCheckBox,
)
from PyQt6.QtCore import (
    Qt, QRect, QPoint, QTimer, QObject, pyqtSignal,
    QEvent, QPropertyAnimation, QEasingCurve, QByteArray,
    QRectF, QAbstractNativeEventFilter,
)
from PyQt6.QtGui import (
    QPainter, QColor, QPixmap, QImage, QCursor,
    QPen, QFont, QIcon, QWheelEvent, QClipboard,
)

def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

import json as _json
from pathlib import Path

_CFG_PATH = Path(os.getenv("APPDATA", ".")) / "ScreenTranslator" / "config.json"

_CFG_DEFAULTS = {
    "deepl_api_key": "",
    "target_lang": "RU",
    "hotkey": "ctrl+shift+t",
    "font_size": 14,
    "live_interval_ms": 600,
    "dark_theme": True,
}

def cfg_load() -> dict:
    if _CFG_PATH.exists():
        try:
            return {**_CFG_DEFAULTS, **_json.loads(_CFG_PATH.read_text("utf-8"))}
        except Exception:
            pass
    return dict(_CFG_DEFAULTS)

def cfg_save(d: dict):
    _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CFG_PATH.write_text(_json.dumps(d, ensure_ascii=False, indent=2), "utf-8")

CFG = cfg_load()

WM_HOTKEY    = 0x0312
MOD_ALT      = 0x0001
MOD_CONTROL  = 0x0002
MOD_SHIFT    = 0x0004
MOD_WIN      = 0x0008
MOD_NOREPEAT = 0x4000

_VK_MAP = {
    "a":0x41,"b":0x42,"c":0x43,"d":0x44,"e":0x45,"f":0x46,"g":0x47,
    "h":0x48,"i":0x49,"j":0x4A,"k":0x4B,"l":0x4C,"m":0x4D,"n":0x4E,
    "o":0x4F,"p":0x50,"q":0x51,"r":0x52,"s":0x53,"t":0x54,"u":0x55,
    "v":0x56,"w":0x57,"x":0x58,"y":0x59,"z":0x5A,
    "f1":0x70,"f2":0x71,"f3":0x72,"f4":0x73,"f5":0x74,"f6":0x75,
    "f7":0x76,"f8":0x77,"f9":0x78,"f10":0x79,"f11":0x7A,"f12":0x7B,
    "0":0x30,"1":0x31,"2":0x32,"3":0x33,"4":0x34,
    "5":0x35,"6":0x36,"7":0x37,"8":0x38,"9":0x39,
    "space":0x20,"enter":0x0D,"tab":0x09,"esc":0x1B,
    "insert":0x2D,"delete":0x2E,"home":0x24,"end":0x23,
    "pageup":0x21,"pagedown":0x22,
    "left":0x25,"up":0x26,"right":0x27,"down":0x28,
    "backspace":0x08,
}

_HOTKEY_ID = 1

class _HotkeyNativeFilter(QAbstractNativeEventFilter):

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def nativeEventFilter(self, eventType, message):
        try:
            if bytes(eventType) == b"windows_generic_MSG":
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY and msg.wParam == _HOTKEY_ID:
                    self._callback()
        except Exception as exc:
            print(f"[Hotkey] nativeEventFilter error: {exc}")
        return False, 0

class WinHotkeyListener(QObject):
    triggered = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._backend: str | None = None   

        
        self._registered = False
        self._native_filter: _HotkeyNativeFilter | None = None

        
        self._pynput_listener = None
        self._pynput_hotkey: set = set()
        self._pynput_pressed: set = set()

    

    def register(self, hotkey_str: str) -> bool:
        self.unregister()
        if self._try_winapi(hotkey_str):
            self._backend = "winapi"
            print(f"[Hotkey] WinAPI  ← {hotkey_str}")
            return True
        if self._try_pynput(hotkey_str):
            self._backend = "pynput"
            print(f"[Hotkey] pynput  ← {hotkey_str}")
            return True
        if self._try_keyboard(hotkey_str):
            self._backend = "keyboard"
            print(f"[Hotkey] keyboard ← {hotkey_str}")
            return True
        print(f"[Hotkey] все бэкенды не сработали для «{hotkey_str}»")
        return False

    def unregister(self):
        
        if self._registered and sys.platform == "win32":
            ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._registered = False
        if self._native_filter is not None:
            app = QApplication.instance()
            if app is not None:
                app.removeNativeEventFilter(self._native_filter)
            self._native_filter = None

        
        if self._pynput_listener is not None:
            try:
                self._pynput_listener.stop()
            except Exception:
                pass
            self._pynput_listener = None

        
        try:
            import keyboard as _kb
            _kb.remove_all_hotkeys()
        except Exception:
            pass

        self._backend = None

    

    def _try_winapi(self, hotkey_str: str) -> bool:
        if sys.platform != "win32":
            return False
        parts = [p.strip().lower() for p in hotkey_str.split("+")]
        mods = MOD_NOREPEAT
        vk   = 0
        for p in parts:
            if p == "ctrl":    mods |= MOD_CONTROL
            elif p == "alt":   mods |= MOD_ALT
            elif p == "shift": mods |= MOD_SHIFT
            elif p == "win":   mods |= MOD_WIN
            else:              vk = _VK_MAP.get(p, 0)
        if vk == 0:
            return False
        
        ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
        ok = bool(ctypes.windll.user32.RegisterHotKey(None, _HOTKEY_ID, mods, vk))
        if ok:
            self._registered = True
            app = QApplication.instance()
            if app is not None:
                self._native_filter = _HotkeyNativeFilter(self.triggered.emit)
                app.installNativeEventFilter(self._native_filter)
            else:
                
                ctypes.windll.user32.UnregisterHotKey(None, _HOTKEY_ID)
                self._registered = False
                ok = False
        return ok

    

    def _try_pynput(self, hotkey_str: str) -> bool:
        try:
            from pynput import keyboard as _pk

            parts = [p.strip().lower() for p in hotkey_str.split("+")]
            combo: set = set()
            for p in parts:
                if p == "ctrl":
                    combo.add(_pk.Key.ctrl)
                elif p == "alt":
                    combo.add(_pk.Key.alt)
                elif p == "shift":
                    combo.add(_pk.Key.shift)
                elif p == "win":
                    combo.add(_pk.Key.cmd)
                elif len(p) == 1:
                    combo.add(_pk.KeyCode.from_char(p))
                else:
                    try:
                        combo.add(_pk.Key[p])
                    except KeyError:
                        pass

            if not combo:
                return False

            self._pynput_hotkey  = combo
            self._pynput_pressed = set()

            def on_press(key):
                self._pynput_pressed.add(key)
                
                norm = self._pynput_normalize(self._pynput_pressed)
                if self._pynput_normalize(self._pynput_hotkey) <= norm:
                    self.triggered.emit()

            def on_release(key):
                self._pynput_pressed.discard(key)

            self._pynput_listener = _pk.Listener(
                on_press=on_press,
                on_release=on_release,
            )
            self._pynput_listener.start()
            return True

        except Exception as exc:
            print(f"[Hotkey] pynput недоступен: {exc}")
            return False

    @staticmethod
    def _pynput_normalize(keys: set) -> set:
        try:
            from pynput import keyboard as _pk
            result = set()
            for k in keys:
                if k in (_pk.Key.ctrl_l, _pk.Key.ctrl_r):
                    result.add(_pk.Key.ctrl)
                elif k in (_pk.Key.alt_l, _pk.Key.alt_r, _pk.Key.alt_gr):
                    result.add(_pk.Key.alt)
                elif k in (_pk.Key.shift_l, _pk.Key.shift_r):
                    result.add(_pk.Key.shift)
                elif k in (_pk.Key.cmd_l, _pk.Key.cmd_r):
                    result.add(_pk.Key.cmd)
                else:
                    result.add(k)
            return result
        except Exception:
            return keys

    

    def _try_keyboard(self, hotkey_str: str) -> bool:
        try:
            import keyboard as _kb
            _kb.add_hotkey(
                hotkey_str,
                self.triggered.emit,
                suppress=False,
                trigger_on_release=True,
            )
            return True
        except Exception as exc:
            print(f"[Hotkey] keyboard недоступен: {exc}")
            return False

_USE_WINRT: bool | None = None

_winrt_engine = None
_winrt_engine_lock = threading.Lock()

def _prepare_ocr_image(img: Image.Image) -> Image.Image:
    img = img.convert("RGB")
    w, h = img.size
    if w < 100 or h < 30:
        scale = max(100 / max(w, 1), 30 / max(h, 1), 2.0)
        img = img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS,
        )
    if max(img.size) < 1400:
        scale = 1.5
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            Image.Resampling.LANCZOS,
        )
    img = ImageEnhance.Contrast(img).enhance(1.25)
    img = ImageEnhance.Sharpness(img).enhance(1.15)
    return img

import re as _re

_GARBAGE_PATTERNS = _re.compile(
    r"""
    ^https?://
    | ^www\.
    | ^\S+\.\S+/
    | ^[\W\d]{0,3}$
    | ^\d+\s*[×x]\s*\d+$
    | ^(Ln|Col|Spaces?|UTF)\b
    """,
    _re.VERBOSE | _re.IGNORECASE,
)

_MIN_LETTER_RATIO = 0.45

def _clean_ocr_text(raw: str) -> str:
    lines = raw.splitlines()
    clean: list[str] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if _GARBAGE_PATTERNS.search(s):
            continue
        letters = sum(1 for c in s if c.isalpha())
        if len(s) > 4 and letters / len(s) < 0.30:
            continue
        clean.append(s)
    result = "\n".join(clean)
    result = _re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()

def _get_winrt_engine():
    global _winrt_engine
    with _winrt_engine_lock:
        if _winrt_engine is not None:
            return _winrt_engine
        import winrt.windows.media.ocr as wocr
        import winrt.windows.globalization as wg

        engine = None
        try:
            engine = wocr.OcrEngine.try_create_from_user_profile_languages()
        except Exception:
            pass
        if engine is None:
            for lang_code in ("ru-RU", "en-US"):
                try:
                    engine = wocr.OcrEngine.try_create_from_language(wg.Language(lang_code))
                    if engine:
                        break
                except Exception:
                    continue
        if engine is None:
            raise RuntimeError("Windows OCR: не удалось создать OcrEngine.")
        _winrt_engine = engine
        return _winrt_engine

def ocr_recognize(img: Image.Image) -> str:
    global _USE_WINRT
    img = _prepare_ocr_image(img)

    if _USE_WINRT is None:
        try:
            import winrt.windows.media.ocr
            import winrt.windows.graphics.imaging
            import winrt.windows.storage.streams
            import winrt.windows.globalization
            import winrt.windows.foundation
            _USE_WINRT = True
        except (ImportError, ModuleNotFoundError):
            _USE_WINRT = False

    if _USE_WINRT:
        try:
            text = _clean_ocr_text(_winrt_ocr(img))
            if text:
                return text
        except Exception as exc:
            print(f"[OCR] WinRT error: {exc}")
            _USE_WINRT = False

    try:
        import pytesseract
        langs = []
        try:
            available = pytesseract.get_languages(config="")
            if "eng" in available: langs.append("eng")
            if "rus" in available: langs.append("rus")
        except Exception:
            pass
        lang = "+".join(langs) if langs else "eng"
        raw = pytesseract.image_to_string(img, lang=lang, config="--psm 6")
        return _clean_ocr_text(raw)
    except Exception as exc:
        raise RuntimeError(
            "OCR не работает.\n\n"
            "Windows OCR:\npip install "
            "winrt-Windows.Media.Ocr "
            "winrt-Windows.Graphics.Imaging "
            "winrt-Windows.Storage.Streams "
            "winrt-Windows.Globalization "
            "winrt-Windows.Foundation\n\n"
            "или Tesseract:\nhttps://github.com/UB-Mannheim/tesseract/wiki\n\n"
            f"{exc}"
        ) from exc

def _winrt_ocr(img: Image.Image) -> str:
    import asyncio
    import winrt.windows.graphics.imaging as wgi
    import winrt.windows.storage.streams as wss

    async def _run():
        engine = _get_winrt_engine()          

        rgba = img.convert("RGBA")
        raw_bytes = rgba.tobytes()
        writer = wss.DataWriter()
        writer.write_bytes(raw_bytes)
        buffer = writer.detach_buffer()
        bitmap = wgi.SoftwareBitmap(
            wgi.BitmapPixelFormat.RGBA8, rgba.width, rgba.height,
        )
        bitmap.copy_from_buffer(buffer)
        result = await engine.recognize_async(bitmap)
        return result.text

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()

def _google_translate(text: str, target_lang: str) -> tuple[str, str]:
    
    lang_map = {
        "EN-US": "en", "EN-GB": "en", "RU": "ru", "UK": "uk",
        "DE": "de", "FR": "fr", "ES": "es", "IT": "it",
        "PL": "pl", "ZH": "zh-cn", "JA": "ja",
    }
    tl = lang_map.get(target_lang.upper(), target_lang.lower()[:2])

    params = urllib.parse.urlencode({
        "client": "gtx",
        "sl": "auto",
        "tl": tl,
        "dt": "t",
        "q": text,
    })
    url = f"https://translate.googleapis.com/translate_a/single?{params}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read())

    translated = "".join(
        part[0] for part in data[0] if part[0]
    )
    src_lang = (data[2] or "?").upper()
    return translated.strip(), src_lang

def translate(text: str, target_lang: str, api_key: str) -> tuple[str, str]:
    text = text.strip()
    if not text:
        return "", "?"

    
    if api_key:
        url = (
            "https://api-free.deepl.com/v2/translate"
            if api_key.endswith(":fx")
            else "https://api.deepl.com/v2/translate"
        )
        data = urllib.parse.urlencode({
            "text": text,
            "target_lang": target_lang,
        }).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Authorization": f"DeepL-Auth-Key {api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                js = json.loads(response.read())
            translations = js.get("translations") or []
            if translations:
                r = translations[0]
                return (
                    r.get("text", "").strip(),
                    r.get("detected_source_language", "?"),
                )
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 456:
                
                print("[Translate] DeepL limit (456) → Google fallback")
                return _google_translate(text, target_lang)
            if code == 403:
                raise RuntimeError("DeepL: неверный API-ключ (403)") from exc
            try:
                body = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                body = ""
            raise RuntimeError(
                f"DeepL HTTP {code}" + (f": {body[:300]}" if body else "")
            ) from exc
        except Exception as exc:
            raise RuntimeError(f"DeepL: ошибка сети: {exc}") from exc

    
    try:
        return _google_translate(text, target_lang)
    except Exception as exc:
        raise RuntimeError(
            "Перевод недоступен.\n\n"
            "Задайте DeepL API-ключ в Настройках\n"
            f"(Google-fallback также не ответил: {exc})"
        ) from exc

def grab_region_qscreen(global_rect: QRect) -> Image.Image:
    screen = QApplication.primaryScreen()
    
    full_px = screen.grabWindow(0)

    
    dpr = screen.devicePixelRatio()

    phys_x      = int(global_rect.x()      * dpr)
    phys_y      = int(global_rect.y()      * dpr)
    phys_width  = max(1, int(global_rect.width()  * dpr))
    phys_height = max(1, int(global_rect.height() * dpr))

    cropped_px = full_px.copy(phys_x, phys_y, phys_width, phys_height)

    qimg = cropped_px.toImage().convertToFormat(QImage.Format.Format_RGB888)
    ptr  = qimg.bits()
    ptr.setsize(qimg.sizeInBytes())
    return Image.frombuffer(
        "RGB",
        (qimg.width(), qimg.height()),
        bytes(ptr),
        "raw", "RGB", 0, 1,
    )

def grab_full_desktop_qscreen() -> tuple[Image.Image, int, int]:
    vdesktop = virtual_desktop_geometry()
    canvas = Image.new("RGB", (max(1, vdesktop.width()), max(1, vdesktop.height())), "black")

    for screen in QApplication.screens():
        px = screen.grabWindow(0)
        qimg = px.toImage().convertToFormat(QImage.Format.Format_RGB888)
        ptr = qimg.bits()
        ptr.setsize(qimg.sizeInBytes())
        img = Image.frombuffer(
            "RGB",
            (qimg.width(), qimg.height()),
            bytes(ptr),
            "raw", "RGB", 0, 1,
        )

        geo = screen.geometry()  

        
        
        
        
        if img.size != (geo.width(), geo.height()) and geo.width() > 0 and geo.height() > 0:
            img = img.resize((geo.width(), geo.height()), Image.Resampling.LANCZOS)

        paste_x = geo.x() - vdesktop.x()
        paste_y = geo.y() - vdesktop.y()
        canvas.paste(img, (paste_x, paste_y))

    return canvas, vdesktop.x(), vdesktop.y()

def pil_to_qpixmap(img: Image.Image) -> QPixmap:
    img = img.convert("RGB")
    w, h = img.size
    qimg = QImage(
        img.tobytes("raw", "RGB"), w, h, w * 3,
        QImage.Format.Format_RGB888,
    )
    return QPixmap.fromImage(qimg.copy())

def virtual_desktop_geometry() -> QRect:
    screens = QApplication.screens()
    if not screens:
        return QRect(0, 0, 1, 1)
    rect = QRect(screens[0].geometry())
    for s in screens[1:]:
        rect = rect.united(s.geometry())
    return rect

def screen_for_rect(rect: QRect):
    center = rect.center()
    for screen in QApplication.screens():
        if screen.geometry().contains(center):
            return screen
    best_screen = QApplication.primaryScreen()
    best_area = -1
    for screen in QApplication.screens():
        inter = screen.geometry().intersected(rect)
        if not inter.isEmpty():
            area = inter.width() * inter.height()
            if area > best_area:
                best_area = area
                best_screen = screen
    return best_screen

_HANDLE_SIZE = 10          
_HANDLE_HIT  = 16          

class SelectionOverlay(QWidget):
    live_request    = pyqtSignal(object, object)
    cancel_requested = pyqtSignal()

    def __init__(self, on_done, on_change):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )

        self._on_done   = on_done
        self._on_change = on_change

        self._p0: QPoint | None = None
        self._p1: QPoint | None = None

        self._dragging  = False
        self._finished  = False

        
        self._resize_handle: str | None = None   
        self._drag_start_pos:  QPoint | None = None
        self._drag_start_rect: QRect  | None = None

        self._translation = ""
        self._original    = ""
        self._src_lang    = "?"
        self._card_opacity = 0.0

        
        self._card_offset      = QPoint(0, 0)   
        self._dragging_card    = False
        self._card_drag_start_mouse:  QPoint | None = None
        self._card_drag_start_offset: QPoint | None = None

        
        self._typed_translation = ""
        self._full_translation  = ""
        self._type_timer = QTimer(self)
        self._type_timer.setSingleShot(False)
        self._type_timer.timeout.connect(self._on_type_tick)

        
        self._font_size: int = max(11, int(CFG.get("font_size", 14)))

        
        self._full_img, self._ox, self._oy = grab_full_desktop_qscreen()
        self._pixmap = pil_to_qpixmap(self._full_img)

        
        
        
        self._dimmed_pixmap = QPixmap(self._pixmap.size())
        self._dimmed_pixmap.fill(Qt.GlobalColor.transparent)
        _dp = QPainter(self._dimmed_pixmap)
        _dp.drawPixmap(0, 0, self._pixmap)
        _dp.fillRect(self._dimmed_pixmap.rect(), QColor(0, 0, 0, 95))
        _dp.end()

        self.setGeometry(virtual_desktop_geometry())
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        
        self._pinned = False

    

    def selected_rect(self) -> QRect | None:
        if self._p0 is None or self._p1 is None:
            return None
        rect = QRect(self._p0, self._p1).normalized()
        if rect.width() < 10 or rect.height() < 10:
            return None
        return rect

    def global_selected_rect(self) -> QRect | None:
        rect = self.selected_rect()
        if rect is None:
            return None
        geo = self.geometry()
        return QRect(geo.x() + rect.x(), geo.y() + rect.y(), rect.width(), rect.height())

    def crop_from_rect(self, rect: QRect) -> Image.Image:
        if self.width() <= 0 or self.height() <= 0:
            return Image.new("RGB", (1, 1), "black")
        sx = self._full_img.width  / self.width()
        sy = self._full_img.height / self.height()
        x1 = max(0,                  int(rect.x() * sx))
        y1 = max(0,                  int(rect.y() * sy))
        x2 = min(self._full_img.width,  int((rect.x() + rect.width())  * sx))
        y2 = min(self._full_img.height, int((rect.y() + rect.height()) * sy))
        if x2 <= x1 or y2 <= y1:
            return Image.new("RGB", (1, 1), "black")
        return self._full_img.crop((x1, y1, x2, y2))

    

    def _handle_rects(self, sel: QRect) -> dict[str, QRect]:
        hs = _HANDLE_HIT
        corners = {
            "tl": QPoint(sel.left(),  sel.top()),
            "tr": QPoint(sel.right(), sel.top()),
            "bl": QPoint(sel.left(),  sel.bottom()),
            "br": QPoint(sel.right(), sel.bottom()),
        }
        return {
            name: QRect(pt.x() - hs // 2, pt.y() - hs // 2, hs, hs)
            for name, pt in corners.items()
        }

    def _hit_handle(self, pos: QPoint) -> str | None:
        sel = self.selected_rect()
        if sel is None:
            return None
        for name, rect in self._handle_rects(sel).items():
            if rect.contains(pos):
                return name
        return None

    def _cursor_for_handle(self, handle: str | None):
        if handle is None:
            return QCursor(Qt.CursorShape.CrossCursor)
        if handle in ("tl", "br"):
            return QCursor(Qt.CursorShape.SizeFDiagCursor)
        return QCursor(Qt.CursorShape.SizeBDiagCursor)

    

    def set_translation(self, translated: str, original: str, src_lang: str):
        new_text = translated.strip()
        self._original = original.strip()
        self._src_lang = src_lang or "?"
        if new_text != self._full_translation:
            self._full_translation  = new_text
            self._typed_translation = ""
            self._translation       = ""
            self._card_opacity      = 0.0
            self._type_timer.stop()
            if new_text:
                self._type_timer.start(30)
        self.update()

    def _on_type_tick(self):
        full = self._full_translation
        cur  = self._typed_translation
        self._card_opacity = min(1.0, self._card_opacity + 0.12)
        if len(cur) >= len(full):
            self._type_timer.stop()
            self._translation = full
            self.update()
            return
        n = max(2, min(5, len(full) // 50))
        self._typed_translation = full[: len(cur) + n]
        cursor = "|" if (len(self._typed_translation) % 4 < 2) else ""
        self._translation = self._typed_translation + cursor
        self.update()

    def clear_translation(self):
        self._type_timer.stop()
        self._translation       = ""
        self._original          = ""
        self._src_lang          = "?"
        self._typed_translation = ""
        self._full_translation  = ""
        self._card_opacity      = 0.0
        self._card_offset       = QPoint(0, 0)
        self.update()

    

    def _paint_scene(self, p: QPainter, sel_rect: QRect | None):
        
        p.drawPixmap(self.rect(), self._dimmed_pixmap, self._dimmed_pixmap.rect())

        if sel_rect is not None:
            if self.width() > 0 and self.height() > 0:
                sx = self._pixmap.width()  / self.width()
                sy = self._pixmap.height() / self.height()
                src = QRect(
                    int(sel_rect.x() * sx), int(sel_rect.y() * sy),
                    int(sel_rect.width() * sx), int(sel_rect.height() * sy),
                )
            else:
                src = sel_rect
            
            p.drawPixmap(sel_rect, self._pixmap, src)

            
            p.setPen(QPen(QColor(0, 162, 255), 2))
            p.drawRect(sel_rect)

            
            p.setFont(QFont("Segoe UI", 9))
            p.setPen(QColor(255, 255, 255))
            p.drawText(
                sel_rect.x() + 5,
                max(17, sel_rect.y() - 7),
                f"{sel_rect.width()} × {sel_rect.height()}",
            )

            
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 162, 255, 220))
            hs = _HANDLE_SIZE
            for _, hr in self._handle_rects(sel_rect).items():
                p.drawRoundedRect(hr, 3, 3)

            
            self._draw_swap_button(p, sel_rect)

        else:
            p.setFont(QFont("Segoe UI", 12, QFont.Weight.Bold))
            p.setPen(QColor(255, 255, 255, 220))
            hk = CFG.get("hotkey", "ctrl+shift+t").upper()
            msg = f"Выделите область с текстом  •  {hk} — закрыть  •  Esc — отмена"
            fm  = p.fontMetrics()
            x0  = max(12, (self.width() - fm.horizontalAdvance(msg)) // 2)
            p.drawText(x0, 38, msg)

    def _draw_swap_button(self, p: QPainter, sel: QRect):
        tgt  = CFG.get("target_lang", "RU")[:2]
        label = f"⇄ {tgt}"
        font  = QFont("Segoe UI", 9, QFont.Weight.Bold)
        p.setFont(font)
        fm   = p.fontMetrics()
        bw   = fm.horizontalAdvance(label) + 16
        bh   = 22
        bx   = sel.right() - bw - 2
        by   = sel.top() - bh - 4
        if by < 4:
            by = sel.top() + 4

        self._swap_btn_rect = QRect(bx, by, bw, bh)

        
        p.setPen(QPen(QColor(0, 162, 255, 180), 1))
        p.setBrush(QColor(20, 30, 50, 200))
        p.drawRoundedRect(self._swap_btn_rect, 6, 6)

        p.setPen(QColor(100, 200, 255))
        p.drawText(self._swap_btn_rect, Qt.AlignmentFlag.AlignCenter, label)

    def paintEvent(self, _):
        sel = self.selected_rect()

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self._paint_scene(p, sel)

        if sel is not None and self._translation:
            self._draw_translation_card(p, sel)

    def _draw_translation_card(self, p: QPainter, rect: QRect):
        width    = min(max(rect.width(), 300), 640)
        font     = QFont("Segoe UI", self._font_size)
        font_hdr = QFont("Segoe UI", 9)
        p.setFont(font)

        pad_h   = 16
        pad_top = 32
        
        btn_area_w = 56
        text_w     = width - pad_h * 2 - btn_area_w
        max_card_h = int(self.height() * 0.55)

        flags = Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop

        available = QRect(0, 0, text_w, 9999)
        text_rect = p.fontMetrics().boundingRect(
            available, flags,
            self._full_translation if self._full_translation else self._translation,
        )

        card_h = min(max(64, text_rect.height() + pad_top + 18), max_card_h)

        x = rect.x()
        y = rect.bottom() + 12
        if y + card_h > self.height() - 8:
            y = rect.top() - card_h - 12
        if y < 8:
            y = max(8, min(self.height() - card_h - 8, rect.bottom() + 12))
        x = max(8, min(x, self.width() - width - 8))

        
        
        x += self._card_offset.x()
        y += self._card_offset.y()
        x = max(4, min(x, self.width()  - width  - 4))
        y = max(4, min(y, self.height() - card_h - 4))

        card = QRect(x, y, width, card_h)

        p.setOpacity(self._card_opacity)

        
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 100))
        p.drawRoundedRect(QRect(card.x() + 4, card.y() + 6, card.width(), card.height()), 16, 16)

        
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(QColor(15, 18, 28, 230))
        p.drawRoundedRect(card, 16, 16)

        
        sep_y = card.y() + 28
        p.setPen(QPen(QColor(80, 140, 255, 120), 1))
        p.drawLine(card.x() + 16, sep_y, card.right() - 16, sep_y)

        
        p.setPen(QColor(100, 160, 255))
        p.setFont(font_hdr)
        header = f"{self._src_lang.upper()} → {CFG['target_lang']}"
        p.drawText(card.adjusted(pad_h, 8, -pad_h - btn_area_w, 0), Qt.AlignmentFlag.AlignLeft, header)

        
        p.setPen(QColor(240, 245, 255))
        p.setFont(font)
        p.drawText(card.adjusted(pad_h, pad_top, -pad_h - btn_area_w, -12), flags, self._translation)

        
        self._copy_btn_rect = QRect(card.right() - btn_area_w + 4, card.y() + 8, 22, 22)
        p.setPen(QPen(QColor(80, 140, 255, 160), 1))
        p.setBrush(QColor(30, 50, 90, 180))
        p.drawRoundedRect(self._copy_btn_rect, 5, 5)
        p.setPen(QColor(200, 220, 255))
        p.setFont(QFont("Segoe UI", 11))
        p.drawText(self._copy_btn_rect, Qt.AlignmentFlag.AlignCenter, "⎘")

        
        self._pin_btn_rect = QRect(card.right() - btn_area_w + 4, card.y() + 34, 22, 22)
        pin_color = QColor(255, 200, 50, 220) if self._pinned else QColor(80, 140, 255, 160)
        p.setPen(QPen(pin_color, 1))
        p.setBrush(QColor(30, 50, 90, 180))
        p.drawRoundedRect(self._pin_btn_rect, 5, 5)
        p.setPen(QColor(200, 220, 255))
        p.setFont(QFont("Segoe UI", 12))
        p.drawText(self._pin_btn_rect, Qt.AlignmentFlag.AlignCenter, "📌")

        p.setOpacity(1.0)

        
        self._card_rect = card

    

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        pos = e.position().toPoint()

        
        if hasattr(self, "_copy_btn_rect") and self._copy_btn_rect.contains(pos):
            if self._full_translation:
                QApplication.clipboard().setText(self._full_translation)
            return

        
        if hasattr(self, "_pin_btn_rect") and self._pin_btn_rect.contains(pos):
            self._pinned = not self._pinned
            self.update()
            return

        
        if hasattr(self, "_swap_btn_rect") and self._swap_btn_rect.contains(pos):
            self._swap_language()
            return

        
        
        if hasattr(self, "_card_rect") and self._card_rect.contains(pos) and self._translation:
            self._dragging_card         = True
            self._card_drag_start_mouse = pos
            self._card_drag_start_offset = QPoint(self._card_offset)
            self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
            return

        
        handle = self._hit_handle(pos)
        if handle and self.selected_rect() is not None:
            self._resize_handle    = handle
            self._drag_start_pos   = pos
            self._drag_start_rect  = QRect(self.selected_rect())
            self.setCursor(self._cursor_for_handle(handle))
            return

        
        self._resize_handle = None
        self._dragging  = True
        self._finished  = False
        self._p0 = pos
        self._p1 = pos
        self.clear_translation()
        self.update()
        self._on_change(self._current_crop(), self.global_selected_rect(), True)

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()

        
        
        
        
        if (
            self._dragging_card
            and self._card_drag_start_mouse is not None
            and self._card_drag_start_offset is not None
        ):
            delta = pos - self._card_drag_start_mouse
            self._card_offset = self._card_drag_start_offset + delta
            self.update()
            return

        
        if (
            self._resize_handle
            and self._drag_start_rect is not None
            and self._drag_start_pos is not None
        ):
            dx = pos.x() - self._drag_start_pos.x()
            dy = pos.y() - self._drag_start_pos.y()
            r  = QRect(self._drag_start_rect)

            h = self._resize_handle
            if h == "tl":
                r.setTopLeft(r.topLeft() + QPoint(dx, dy))
            elif h == "tr":
                r.setTopRight(r.topRight() + QPoint(dx, dy))
            elif h == "bl":
                r.setBottomLeft(r.bottomLeft() + QPoint(dx, dy))
            elif h == "br":
                r.setBottomRight(r.bottomRight() + QPoint(dx, dy))

            r = r.normalized()
            self._p0 = r.topLeft()
            self._p1 = r.bottomRight()

            self.update()

            region = self.global_selected_rect()
            if region:
                self._on_change(self._current_crop(), region, False)
            return

        
        if self._dragging and self._p0 is not None:
            self._p1 = pos
            self.update()
            region = self.global_selected_rect()
            if region:
                self._on_change(self._current_crop(), region, False)
            return

        
        if hasattr(self, "_card_rect") and self._card_rect.contains(pos) and self._translation:
            self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        else:
            handle = self._hit_handle(pos)
            self.setCursor(self._cursor_for_handle(handle))

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return

        
        if self._dragging_card:
            self._dragging_card          = False
            self._card_drag_start_mouse  = None
            self._card_drag_start_offset = None
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            return

        
        if self._resize_handle:
            self._resize_handle   = None
            self._drag_start_pos  = None
            self._drag_start_rect = None
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
            region = self.global_selected_rect()
            if region:
                self._on_done(self._current_crop(), region)
            return

        if not self._dragging:
            return
        self._dragging = False
        self._finished = True

        region = self.global_selected_rect()
        if region is None:
            return
        self._on_done(self._current_crop(), region)
        self.update()

    def mouseDoubleClickEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position().toPoint()
        if hasattr(self, "_card_rect") and self._card_rect.contains(pos):
            self._card_offset = QPoint(0, 0)
            self.update()

    def wheelEvent(self, e: QWheelEvent):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = e.angleDelta().y()
            if delta > 0:
                self._font_size = min(self._font_size + 1, 32)
            elif delta < 0:
                self._font_size = max(self._font_size - 1, 8)
            CFG["font_size"] = self._font_size
            self.update()
            e.accept()
        else:
            super().wheelEvent(e)

    def _swap_language(self):
        langs = [code for code, _ in _LANGS]
        tgt   = CFG.get("target_lang", "RU")
        src   = self._src_lang.upper().replace("-", "_")

        
        
        candidates = [c for c in langs if c.startswith(src[:2])]
        new_tgt = candidates[0] if candidates and candidates[0] != tgt else (
            next((c for c in langs if c != tgt), tgt)
        )
        CFG["target_lang"] = new_tgt
        cfg_save(CFG)

        self.update()

        
        region = self.global_selected_rect()
        if region:
            self._on_change(self._current_crop(), region, True)

    def _current_crop(self) -> Image.Image:
        rect = self.selected_rect()
        if rect is None:
            return Image.new("RGB", (1, 1), "black")
        return self.crop_from_rect(rect)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.cancel_requested.emit()

    def closeEvent(self, ev):
        self._dragging      = False
        self._finished      = False
        self._resize_handle = None
        super().closeEvent(ev)

class ErrorOverlay(QWidget):
    def __init__(self, msg: str, region: QRect):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        card = QWidget(self)
        card.setObjectName("card")
        card.setStyleSheet("""
            QWidget#card {
                background: #2d0a0a;
                border-radius: 10px;
                border: 1px solid #8b0000;
            }
            QLabel  { color: #ff8080; background: transparent; }
            QPushButton {
                color: #ff8080;
                background: rgba(255,0,0,0.15);
                border: 1px solid #8b0000;
                border-radius: 5px;
                padding: 4px 12px;
            }
        """)
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 120))
        card.setGraphicsEffect(shadow)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 10, 14, 12)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Ошибка"))
        hdr.addStretch()
        btn_x = QPushButton("✕")
        btn_x.setFixedSize(22, 22)
        btn_x.clicked.connect(self.close)
        hdr.addWidget(btn_x)
        lay.addLayout(hdr)

        lbl = QLabel(msg)
        lbl.setWordWrap(True)
        lbl.setFont(QFont("Segoe UI", 11))
        lay.addWidget(lbl)

        self.setFixedWidth(max(min(region.width(), 420), 280))
        self.adjustSize()
        oh = self.sizeHint().height()

        screen = screen_for_rect(region)
        sg = screen.geometry()
        x  = region.x()
        y  = region.bottom() + 8
        if y + oh > sg.bottom():
            y = region.top() - oh - 8
        x = max(sg.left() + 4, min(x, sg.right() - self.width() - 4))
        y = max(sg.top() + 4, y)

        self.move(x, y)
        self.show()
        self.raise_()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self.close()

_LANGS = [
    ("RU",    "Русский"),
    ("UK",    "Українська"),
    ("EN-US", "English (US)"),
    ("EN-GB", "English (UK)"),
    ("DE",    "Deutsch"),
    ("FR",    "Français"),
    ("ES",    "Español"),
    ("IT",    "Italiano"),
    ("PL",    "Polski"),
    ("ZH",    "中文"),
    ("JA",    "日本語"),
]

_DARK_STYLE = """
QDialog, QWidget {
    background: #1a1d27;
    color: #d0d8f0;
}
QLineEdit, QComboBox {
    background: #252836;
    border: 1px solid #3a4060;
    border-radius: 6px;
    color: #d0d8f0;
    padding: 4px 8px;
}
QLabel { color: #a0aac0; }
QPushButton {
    background: #2a3a70;
    color: #d0d8f0;
    border: 1px solid #3a4a90;
    border-radius: 6px;
    padding: 5px 14px;
}
QPushButton:hover { background: #3a4a90; }
QCheckBox { color: #a0aac0; }
"""

_LIGHT_STYLE = ""   

class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(430)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )
        self._apply_theme()

        form = QFormLayout(self)

        self.ed_key = QLineEdit(CFG.get("deepl_api_key", ""))
        self.ed_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ed_key.setPlaceholderText("xxxxxxxxxx:fx  (оставь пустым — Google Translate)")
        form.addRow("DeepL API-ключ:", self.ed_key)

        lnk = QLabel('<a href="https://www.deepl.com/pro-api">Получить бесплатно ↗</a>')
        lnk.setOpenExternalLinks(True)
        form.addRow("", lnk)

        self.cb = QComboBox()
        for code, name in _LANGS:
            self.cb.addItem(name, code)
        idx = next(
            (i for i, (code, _) in enumerate(_LANGS) if code == CFG.get("target_lang")),
            0,
        )
        self.cb.setCurrentIndex(idx)
        form.addRow("Язык перевода:", self.cb)

        self.ed_hk = QLineEdit(CFG.get("hotkey", "ctrl+shift+t"))
        self.ed_hk.setPlaceholderText("ctrl+shift+t")
        form.addRow("Глобальный хоткей:", self.ed_hk)

        self.ed_live = QLineEdit(str(CFG.get("live_interval_ms", 600)))
        self.ed_live.setPlaceholderText("300–1500")
        form.addRow("Интервал live OCR (мс):", self.ed_live)

        self.chk_dark = QCheckBox("Тёмная тема")
        self.chk_dark.setChecked(bool(CFG.get("dark_theme", True)))
        self.chk_dark.stateChanged.connect(self._on_theme_toggle)
        form.addRow("", self.chk_dark)

        bbox = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bbox.accepted.connect(self._ok)
        bbox.rejected.connect(self.reject)
        form.addRow(bbox)

    def _apply_theme(self):
        if CFG.get("dark_theme", True):
            self.setStyleSheet(_DARK_STYLE)
        else:
            self.setStyleSheet(_LIGHT_STYLE)

    def _on_theme_toggle(self):
        CFG["dark_theme"] = self.chk_dark.isChecked()
        self._apply_theme()

    def _ok(self):
        hotkey = (self.ed_hk.text().strip().lower()) or "ctrl+shift+t"
        try:
            live_ms = int(self.ed_live.text().strip())
        except Exception:
            live_ms = 600
        live_ms = max(250, min(live_ms, 3000))

        CFG["deepl_api_key"]    = self.ed_key.text().strip()
        CFG["target_lang"]      = self.cb.currentData()
        CFG["hotkey"]           = hotkey
        CFG["live_interval_ms"] = live_ms
        CFG["dark_theme"]       = self.chk_dark.isChecked()
        cfg_save(CFG)
        self.accept()

class _Bridge(QObject):
    trigger = pyqtSignal()
    result  = pyqtSignal(int, str, str, str)
    error   = pyqtSignal(int, str)

class App:
    def __init__(self, qapp: QApplication):
        self._app = qapp

        self._sel: SelectionOverlay | None = None
        self._res: QWidget          | None = None

        self._bridge = _Bridge()
        self._bridge.trigger.connect(self._toggle_selector, Qt.ConnectionType.QueuedConnection)
        self._bridge.result.connect(self._on_worker_result, Qt.ConnectionType.QueuedConnection)
        self._bridge.error.connect(self._on_worker_error,  Qt.ConnectionType.QueuedConnection)

        self._live_timer = QTimer(self._app)
        self._live_timer.setSingleShot(True)
        self._live_timer.timeout.connect(self._start_live_job)

        self._pending_crop:   Image.Image | None = None
        self._pending_region: QRect       | None = None

        self._request_id = 0
        self._last_request_signature = ""

        self._worker_running           = False
        self._reschedule_after_worker  = False

        self._last_translation = ""
        self._last_original    = ""
        self._last_src_lang    = "?"

        
        self._hotkey_listener = WinHotkeyListener()
        self._hotkey_listener.triggered.connect(self._bridge.trigger)

        self._tray = self._make_tray()

    

    def _make_tray(self) -> QSystemTrayIcon:
        px = QPixmap(22, 22)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#0077ff"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(0, 0, 22, 22, 4, 4)
        p.setPen(QColor("white"))
        p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, "T")
        p.end()

        tray = QSystemTrayIcon(QIcon(px), self._app)
        self._update_tray_tooltip(tray)

        menu = QMenu()
        self._tray_action_translate = menu.addAction(
            self._tray_label(), self._toggle_selector
        )
        menu.addSeparator()
        
        self._tray_action_lang = menu.addAction(
            f"🌐  Язык: {CFG.get('target_lang', 'RU')}"
        )
        self._tray_action_lang.setEnabled(False)
        menu.addSeparator()
        menu.addAction("⚙  Настройки", self._settings)
        menu.addSeparator()
        menu.addAction("✕  Выход", self._app.quit)

        tray.setContextMenu(menu)
        tray.show()
        return tray

    def _tray_label(self) -> str:
        return f"▶  Перевести  [{CFG.get('hotkey', 'ctrl+shift+t').upper()}]"

    def _update_tray_tooltip(self, tray: QSystemTrayIcon | None = None):
        t = tray or self._tray
        hk  = CFG.get("hotkey", "ctrl+shift+t").upper()
        lng = CFG.get("target_lang", "RU")
        t.setToolTip(f"ScreenTranslator  [{hk}]  →{lng}")

    

    def register_hotkey(self):
        hk = CFG.get("hotkey", "ctrl+shift+t")
        ok = self._hotkey_listener.register(hk)
        if not ok:
            self._tray.showMessage(
                "ScreenTranslator",
                f"Хоткей «{hk}» не зарегистрирован.\n"
                "Проверь сочетание клавиш в Настройках.",
                QSystemTrayIcon.MessageIcon.Warning,
                4000,
            )

    def unregister_hotkeys(self):
        self._hotkey_listener.unregister()

    

    def _toggle_selector(self):
        if self._sel is not None:
            self._close_selector()
        else:
            self._open_selector()

    def _open_selector(self):
        if self._res is not None:
            self._res.close()
            self._res = None
        self._close_selector()

        self._sel = SelectionOverlay(
            on_done=self._on_area,
            on_change=self._on_selection_change,
        )
        self._sel.cancel_requested.connect(self._close_selector)
        self._sel.show()
        self._sel.raise_()
        self._sel.activateWindow()
        self._sel.setFocus()

    def _close_selector(self):
        if self._live_timer.isActive():
            self._live_timer.stop()
        self._pending_crop   = None
        self._pending_region = None

        if self._sel is not None:
            try:
                self._sel.close()
                self._sel.deleteLater()
            except Exception:
                pass
        self._sel = None

        self._request_id += 1
        self._worker_running          = False
        self._reschedule_after_worker = False
        self._last_request_signature  = ""

    

    def _on_selection_change(self, crop: Image.Image, region: QRect | None, immediate: bool):
        if region is None or region.width() < 10 or region.height() < 10:
            return
        self._pending_crop   = crop.copy()
        self._pending_region = QRect(region)
        delay = 50 if immediate else max(250, int(CFG.get("live_interval_ms", 600)))
        self._live_timer.start(delay)

    def _on_area(self, crop: Image.Image, region: QRect):
        self._pending_crop   = crop.copy()
        self._pending_region = QRect(region)
        self._live_timer.start(20)

    def _start_live_job(self):
        if self._sel is None:
            return
        if self._pending_crop is None or self._pending_region is None:
            return

        crop   = self._pending_crop.copy()
        region = QRect(self._pending_region)

        signature = self._image_signature(crop)
        if signature == self._last_request_signature:
            return
        self._last_request_signature = signature

        self._request_id += 1
        request_id = self._request_id

        if self._worker_running:
            self._reschedule_after_worker = True
            return
        self._worker_running = True

        if crop.width < 20 or crop.height < 12:
            self._worker_running = False
            return

        threading.Thread(
            target=self._live_worker,
            args=(request_id, crop, region),
            daemon=True,
        ).start()

    @staticmethod
    def _image_signature(img: Image.Image) -> str:
        thumb = img.convert("L").resize((64, 32), Image.Resampling.BILINEAR)
        return hashlib.sha1(thumb.tobytes()).hexdigest()

    def _live_worker(self, request_id: int, crop: Image.Image, region: QRect):
        try:
            text = ocr_recognize(crop).strip()
            if not text:
                self._bridge.result.emit(request_id, "", "", "?")
                return
            translated, src_lang = translate(
                text,
                CFG.get("target_lang", "RU"),
                CFG.get("deepl_api_key", ""),
            )
            self._bridge.result.emit(request_id, translated, text, src_lang)
        except Exception as exc:
            self._bridge.error.emit(request_id, str(exc))

    

    def _on_worker_result(self, request_id: int, translated: str, original: str, src_lang: str):
        self._worker_running = False
        if request_id != self._request_id:
            if self._reschedule_after_worker:
                self._reschedule_after_worker = False
                self._live_timer.start(20)
            return
        if self._sel is not None:
            self._sel.set_translation(translated, original, src_lang)
        self._last_translation = translated
        self._last_original    = original
        self._last_src_lang    = src_lang
        if self._reschedule_after_worker:
            self._reschedule_after_worker = False
            self._live_timer.start(20)

    def _on_worker_error(self, request_id: int, msg: str):
        self._worker_running = False
        if request_id != self._request_id:
            if self._reschedule_after_worker:
                self._reschedule_after_worker = False
                self._live_timer.start(20)
            return
        if self._sel is not None:
            self._sel.set_translation(f"Ошибка: {msg}", "", "?")
        if self._reschedule_after_worker:
            self._reschedule_after_worker = False
            self._live_timer.start(20)

    

    def _settings(self):
        dlg = SettingsDialog()
        if not dlg.exec():
            return
        self.unregister_hotkeys()
        self.register_hotkey()
        self._update_tray_tooltip()
        
        self._tray_action_translate.setText(self._tray_label())
        self._tray_action_lang.setText(f"🌐  Язык: {CFG.get('target_lang', 'RU')}")

if __name__ == "__main__":
    enable_dpi_awareness()

    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    qapp = QApplication(sys.argv)
    qapp.setQuitOnLastWindowClosed(False)

    controller = App(qapp)
    controller.register_hotkey()

    if not CFG.get("deepl_api_key"):
        hk = CFG.get("hotkey", "ctrl+shift+t").upper()
        controller._tray.showMessage(
            "ScreenTranslator",
            f"Хоткей: {hk}\n"
            "DeepL API-ключ не задан — используется Google Translate.\n"
            "Для лучшего качества: Настройки → вставь бесплатный ключ.",
            QSystemTrayIcon.MessageIcon.Information,
            6000,
        )

    try:
        exit_code = qapp.exec()
    finally:
        controller.unregister_hotkeys()

    sys.exit(exit_code)