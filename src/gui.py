"""
DeepNoise Desktop GUI Application.
Features:
- Single toggle button: "START" (Green) -> "STOP" (Red)
- Minimize and Close buttons both minimize to the Windows notification area (System Tray)
- Human-friendly, clean UI with zero broken symbols or technical jargon
- No vertical scrollbars
- Auto-selects Windows default microphone on startup
- Integrated VB-Cable detection with 1-click silent install
"""
import os
import sys
import math
import re
import threading
import time
import signal
import ctypes
from ctypes import wintypes
from typing import List, Dict, Optional

import dearpygui.dearpygui as dpg
import sounddevice as sd
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu
import webbrowser

import numpy as np

from src.devices import (
    get_input_devices,
    auto_select_input_device,
    check_vbcable_status,
    auto_select_output_device,
    get_physical_output_device,
    install_vbcable_driver,
    is_laptop_microphone,
    find_matching_cable_input,
)
from src.config import load_config, save_config
from src.stream import AudioEngine, create_engine_from_config
from src.router import SmartMicRouter
from src.rnnoise import RNNoise, FRAME_SIZE, SAMPLE_RATE
from src.dsp import (
    HighPassFilter,
    AdaptiveNoiseGate,
    SpeechLeveler,
    TransientSuppressor,
    process_mono_frame,
)
from src.host_api_resolver import resolve_single, selection_hint
from src.audio_errors import explain_start_error
from src.autostart import is_autostart_enabled, set_autostart, sync_autostart_with_config


# Win32 Constants for System Tray Minimize/Close hook
user32 = ctypes.windll.user32
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
GWLP_WNDPROC = -4
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002
WM_CLOSE = 0x0010
WM_SYSCOMMAND = 0x0112
WM_NCHITTEST = 0x0084
WM_WINDOWPOSCHANGING = 0x0046
WM_MOVING = 0x0216
SC_MOVE = 0xF010
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
HTCLIENT = 1
HTCAPTION = 2
SW_HIDE = 0
SW_RESTORE = 9
WS_MAXIMIZEBOX = 0x00010000
WS_THICKFRAME = 0x00040000
SWP_FRAMECHANGED = 0x0020
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOZORDER = 0x0004


class WINDOWPOS(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("hwndInsertAfter", wintypes.HWND),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("cx", ctypes.c_int),
        ("cy", ctypes.c_int),
        ("flags", wintypes.UINT),
    ]


class ACCENT_POLICY(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_int),
        ("AnimationId", ctypes.c_int),
    ]


class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
    _fields_ = [
        ("Attrib", ctypes.c_int),
        ("pvData", ctypes.c_void_p),
        ("cbData", ctypes.c_size_t),
    ]


user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CallWindowProcW.restype = ctypes.c_longlong
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL


def init_dpi_awareness() -> tuple[int, float]:
    """
    Enables Windows Per-Monitor V2 DPI awareness and queries active monitor DPI.
    Prevents Windows DWM from bilinearly upscaling the window and blurring fonts.
    Returns (dpi, dpi_scale).
    """
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (-4)
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            # Per-Monitor awareness (Windows 8.1+)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        scale = max(1.0, dpi / 96.0)
        return dpi, scale
    except Exception:
        return 96, 1.0


def force_foreground_window(hwnd: int):
    """
    Bypasses Windows Foreground Lock Timeout (Anti-Focus-Stealing) via AttachThreadInput.
    Ensures the window is restored, brought to top, and granted foreground input focus.
    """
    if not hwnd or sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        fore_hwnd = user32.GetForegroundWindow()
        fore_thread = user32.GetWindowThreadProcessId(fore_hwnd, None)
        cur_thread = kernel32.GetCurrentThreadId()

        user32.ShowWindow(hwnd, SW_RESTORE)
        if fore_thread and cur_thread and fore_thread != cur_thread:
            user32.AttachThreadInput(cur_thread, fore_thread, True)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.AttachThreadInput(cur_thread, fore_thread, False)
        else:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
    except Exception:
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass


def apply_dwm_title_bar(hwnd: int):
    """
    Applies Windows 10/11 Light Mode and custom title bar colors
    to match the application's clean white aesthetic.
    """
    if not hwnd or sys.platform != "win32":
        return
    try:
        dwmapi = ctypes.windll.dwmapi
        # DWMWA_USE_IMMERSIVE_DARK_MODE: 0 disables dark mode for native light title bar
        false_val = ctypes.c_int(0)
        if dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(false_val), ctypes.sizeof(false_val)) != 0:
            dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(false_val), ctypes.sizeof(false_val))

        # DWMWA_CAPTION_COLOR: 35 (Win11 COLORREF 0x00BBGGRR -> Pure white 0x00FFFFFF)
        caption_color = ctypes.c_int(0x00FFFFFF)
        dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption_color), ctypes.sizeof(caption_color))

        # DWMWA_TEXT_COLOR: 36 (Win11 COLORREF dark text 0x002B2018 -> RGB(24, 32, 43))
        text_color = ctypes.c_int(0x002B2018)
        dwmapi.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text_color), ctypes.sizeof(text_color))

        # DWMWA_BORDER_COLOR: 34 (Win11 COLORREF RGB(220, 226, 235) is 0x00EBE2DC)
        border_color = ctypes.c_int(0x00EBE2DC)
        dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(border_color), ctypes.sizeof(border_color))
    except Exception as e:
        print(f"[WARN] Failed to apply DWM title bar styling: {e}")


def clean_device_label(name: str) -> str:
    """Format raw audio device name into a clean, human-friendly label."""
    cleaned = name
    for tag in ["(Windows WASAPI)", "(Windows DirectSound)", "(MME)", "(Windows WDM-KS)"]:
        cleaned = cleaned.replace(tag, "")
    # Clean up prefixes like '2- ' or '1- '
    cleaned = re.sub(r'\(\s*\d+-\s*', '(', cleaned)
    cleaned = cleaned.strip()
    if len(cleaned) > 52:
        cleaned = cleaned[:49] + "..."
    return cleaned


if getattr(sys, 'frozen', False):
    APP_DIR = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
else:
    APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

ICON_PNG_PATH = os.path.join(APP_DIR, "deepnoise-icon.png")
ICON_ICO_PATH = os.path.join(APP_DIR, "deepnoise.ico")


def ensure_app_icon_ico():
    """Ensure application .ico exists with multi-resolution sizes from PNG icon."""
    if os.path.exists(ICON_PNG_PATH):
        try:
            if not os.path.exists(ICON_ICO_PATH) or os.path.getmtime(ICON_PNG_PATH) > os.path.getmtime(ICON_ICO_PATH):
                img = Image.open(ICON_PNG_PATH)
                img.save(
                    ICON_ICO_PATH,
                    format='ICO',
                    sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
                )
        except Exception as e:
            print(f"[WARN] Failed to convert icon to ICO: {e}")


def create_tray_icon_image(is_active: bool = False) -> Image.Image:
    """Generate 64x64 icon for Windows notification area / system tray."""
    size = (64, 64)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))

    if os.path.exists(ICON_PNG_PATH):
        try:
            base_icon = Image.open(ICON_PNG_PATH).convert("RGBA")
            base_icon = base_icon.resize((50, 50), Image.Resampling.LANCZOS)
            canvas.paste(base_icon, (4, 4), mask=base_icon)
        except Exception:
            draw = ImageDraw.Draw(canvas)
            draw.ellipse((6, 6, 58, 58), fill=(245, 247, 250, 240), outline=(215, 222, 232), width=2)
    else:
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((6, 6, 58, 58), fill=(245, 247, 250, 240), outline=(215, 222, 232), width=2)

    # Status indicator dot in bottom-right corner
    draw = ImageDraw.Draw(canvas)
    dot_color = (40, 215, 120, 255) if is_active else (225, 75, 75, 255)
    draw.ellipse((42, 42, 60, 60), fill=dot_color, outline=(255, 255, 255, 255), width=2)
    return canvas


def _image_to_dpg_texture(img: Image.Image) -> list:
    """Convert a PIL RGBA image to a Dear PyGui static texture data list (floats 0-1)."""
    img = img.convert("RGBA")
    raw = img.tobytes()
    data = [b / 255.0 for b in raw]
    return data


class DeepNoiseGUI:
    def __init__(self):
        # Explicit AppUserModelID ensures Windows Taskbar pins the custom icon instead of python.exe
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("deepnoise.ai.noisecancellation.1")
        except Exception:
            pass

        ensure_app_icon_ico()

        # Ensure Start Menu shortcut and Windows Recent registration
        try:
            from src.shortcut import ensure_shortcut_and_recent
            ensure_shortcut_and_recent()
        except Exception as e:
            print(f"[WARN] Failed to configure shortcut / recent: {e}")

        # 1. Detect and apply monitor DPI scaling
        self.dpi, self.dpi_scale = init_dpi_awareness()
        print(f"[GUI] Active Monitor DPI: {self.dpi} (DPI Scale: {self.dpi_scale:.2f}x)")

        self.config = load_config()
        self.router = SmartMicRouter(self.config)
        self.is_autostart_active = bool(self.config.get("start_with_windows", True))
        try:
            sync_autostart_with_config(self.config)
            self.is_autostart_active = is_autostart_enabled()
        except Exception:
            pass
        self.mic_boost_db = int(self.config.get("mic_boost_db", 0))
        self.engine: Optional[AudioEngine] = None
        self.is_running = False
        self._is_testing_voice = False
        self._state_lock = threading.Lock()
        self._vad_timer_lock = threading.Lock()
        self._vad_save_timer: Optional[threading.Timer] = None
        self._cancel_voice_test = False

        self.input_devices: List[Dict] = []
        self.device_map: Dict[str, int] = {}
        self.selected_input_name: Optional[str] = self.config.get("input_device_name")
        self.selected_output_name: Optional[str] = self.config.get("output_device_name")
        self.selected_input_idx: Optional[int] = self.config.get("input_device_id")
        self.selected_output_idx: Optional[int] = self.config.get("output_device_id")
        self.is_vbcable_installed = False
        self.has_icon_texture = False
        self._ui_built = False
        self._app_running = True
        self._is_rescanning = False

        self._hwnd: Optional[int] = None
        self._old_wndproc = None
        self._wndproc_cb = None
        self._fixed_x: Optional[int] = None
        self._fixed_y: Optional[int] = None
        self._fixed_w: Optional[int] = None
        self._fixed_h: Optional[int] = None
        self._lock_window_pos: bool = False
        self._tray_icon: Optional[pystray.Icon] = None
        self._device_change_timer: Optional[threading.Timer] = None
        self._device_change_lock = threading.Lock()

        self._refresh_devices()

        # Start stream health watchdog
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

    def s(self, val: float) -> int:
        """Scale pixel value according to active monitor DPI."""
        return max(1, int(round(val * self.dpi_scale)))

    def _release_state_lock(self):
        """Idempotently releases _state_lock without raising if already unlocked."""
        try:
            self._state_lock.release()
        except RuntimeError:
            pass

    def _teardown_engine_and_timers(self):
        """Unified teardown: halts background workers, cancels timers, and restores audio routing."""
        self._app_running = False
        self._cancel_voice_test = True
        for timer in (self._device_change_timer, self._vad_save_timer):
            if timer and timer.is_alive():
                try:
                    timer.cancel()
                except Exception:
                    pass
        if self.engine:
            try:
                self.engine.prepare_for_stop()
            except Exception:
                pass
            self.engine = None
        elif self.router and self.router.is_swapped:
            try:
                self.router.restore_original()
            except Exception:
                pass

    def _compute_current_gain(self) -> float:
        dev_name = ""
        try:
            if self.selected_input_idx is not None and self.selected_input_idx >= 0:
                dev_name = sd.query_devices(self.selected_input_idx)['name']
        except Exception:
            pass
        is_laptop_mic = is_laptop_microphone(dev_name, self.selected_input_idx)
        base_gain = 1.2 if is_laptop_mic else 1.0
        boost_mult = 10.0 ** (self.mic_boost_db / 20.0)
        return base_gain * boost_mult

    def set_mic_boost(self, boost_db: int):
        self.mic_boost_db = boost_db
        self.config["mic_boost_db"] = boost_db
        save_config(self.config)
        self._update_boost_buttons_ui()
        if self.engine:
            new_gain = self._compute_current_gain()
            self.engine.mic_gain = new_gain
            self.engine.mic_boost_db = float(boost_db)
            print(f"[GUI] Live Microphone Gain updated to: {new_gain:.2f}x (+{boost_db} dB)")

    def _update_boost_buttons_ui(self):
        if not self._ui_built:
            return
        try:
            for b in [0, 5, 10, 15]:
                tag = f"btn_boost_{b}"
                if dpg.does_item_exist(tag):
                    if b == self.mic_boost_db:
                        dpg.bind_item_theme(tag, self.theme_boost_active)
                    else:
                        dpg.bind_item_theme(tag, self.theme_boost_inactive)
        except Exception:
            pass

    def _get_windows_default_input(self) -> int:
        """Find the Windows default recording device index directly from WASAPI."""
        # 1. Query the WASAPI Host API's default_input_device (matches Windows Settings exactly)
        for api in sd.query_hostapis():
            if 'WASAPI' in api['name'] and api['default_input_device'] >= 0:
                wasapi_def = api['default_input_device']
                for d in self.input_devices:
                    if d['index'] == wasapi_def:
                        return d['index']
                try:
                    def_name = sd.query_devices(wasapi_def)['name']
                    for d in self.input_devices:
                        if d['name'] == def_name:
                            return d['index']
                except Exception:
                    pass

        # 2. General PortAudio default
        default_in = sd.default.device[0]
        for d in self.input_devices:
            if d['index'] == default_in:
                return d['index']

        return self.input_devices[0]['index'] if self.input_devices else default_in

    def _watchdog_loop(self):
        """Monitors stream health every 1.0s and triggers emergency mic restoration and debounced hotplug recovery if audio stalls."""
        while self._app_running:
            time.sleep(1.0)
            if self.is_running and self.engine and not self._is_rescanning:
                if not self.engine.is_stream_alive():
                    # Emergency Restore Vector (Watchdog): If stream dead while swapped, restore physical mic immediately
                    if self.router and self.router.is_swapped:
                        print("[WATCHDOG] Audio stream stalled while microphone swapped. Emergency restoring default capture...")
                        try:
                            self.router.restore_original()
                            self.set_status_pill("▲ Audio stream stalled • Restored physical mic", [195, 115, 15])
                        except Exception as e:
                            print(f"[WATCHDOG] Emergency restore error: {e}")
                    with self._device_change_lock:
                        if not self._device_change_timer or not self._device_change_timer.is_alive():
                            print("[WATCHDOG] Audio stream stalled or disconnected. Triggering rescan...")
                            self._device_change_timer = threading.Timer(0.3, self._handle_hardware_rescan)
                            self._device_change_timer.daemon = True
                            self._device_change_timer.start()

    def _refresh_devices(self, reinit_portaudio: bool = True):
        """Scans connected devices and updates status."""
        if reinit_portaudio and not self.is_running:
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass

        self.input_devices = get_input_devices()
        self.device_map = {}
        for d in self.input_devices:
            label = clean_device_label(d['name'])
            # Avoid duplicate keys if multiple devices have similar names
            if label in self.device_map:
                label = f"{label} #{d['index']}"
            self.device_map[label] = d['index']

        # Match persistent device identity by NAME first, then fallback
        if self.selected_input_name and self.selected_input_name in self.device_map:
            self.selected_input_idx = self.device_map[self.selected_input_name]
        elif self.selected_input_idx is not None and self.selected_input_idx in self.device_map.values():
            self.selected_input_name = self._get_selected_input_label()
        else:
            best_input = auto_select_input_device()
            self.selected_input_idx = best_input['index'] if (best_input and 'index' in best_input and best_input['index'] in self.device_map.values()) else self._get_windows_default_input()
            self.selected_input_name = self._get_selected_input_label()
            self.config["input_device_name"] = self.selected_input_name
            self.config["input_device_id"] = self.selected_input_idx
            save_config(self.config)

        self.is_vbcable_installed, cable_in, _ = check_vbcable_status(force_rescan=reinit_portaudio)
        matching_cable = find_matching_cable_input(self.selected_input_idx)
        if self.is_vbcable_installed and matching_cable:
            self.selected_output_idx = matching_cable['index']
            self.selected_output_name = matching_cable['name']
        elif self.is_vbcable_installed and cable_in:
            self.selected_output_idx = cable_in['index']
            self.selected_output_name = cable_in['name']
        else:
            target_out, _ = auto_select_output_device()
            self.selected_output_idx = target_out['index']
            self.selected_output_name = target_out.get('name', 'Default Output')

        if self._ui_built and dpg.does_item_exist("input_combo"):
            combo_items = list(self.device_map.keys())
            current_label = self._get_selected_input_label()
            dpg.configure_item("input_combo", items=combo_items, default_value=current_label)
            self._update_selection_hint(current_label)


    def _on_device_change_event(self):
        """Debounced listener for Windows WM_DEVICECHANGE hotplug signals."""
        with self._device_change_lock:
            if self._device_change_timer and self._device_change_timer.is_alive():
                self._device_change_timer.cancel()
            # 600ms debounce allows Windows PnP audio stack to fully register endpoints
            self._device_change_timer = threading.Timer(0.6, self._handle_hardware_rescan)
            self._device_change_timer.daemon = True
            self._device_change_timer.start()

    def _handle_hardware_rescan(self):
        """Processes hardware changes, updates dropdown UI, and auto-switches to newly plugged mic if stopped."""
        print("[HARDWARE] Audio device change detected. Rescanning...")
        with self._device_change_lock:
            self._is_rescanning = True
            try:
                was_running = self.is_running
                if was_running and self.engine:
                    print("[HARDWARE] Pausing active stream for clean PortAudio reinitialization...")
                    self.engine.stop()
                    self.engine = None
                    self.is_running = False

                # Safe PortAudio reinitialization: no active streams exist right now
                try:
                    sd._terminate()
                    sd._initialize()
                except Exception as e:
                    print(f"[HARDWARE] PortAudio reinit error: {e}")

                old_devices = dict(self.device_map)
                self._refresh_devices(reinit_portaudio=False)
                new_devices = dict(self.device_map)

                added = set(new_devices.keys()) - set(old_devices.keys())
                removed = set(old_devices.keys()) - set(new_devices.keys())

                if added:
                    print(f"[HARDWARE] New microphone detected: {', '.join(added)}")
                    # If stream was stopped before the rescan, auto-select newly plugged microphone
                    if not was_running:
                        for label in added:
                            self.selected_input_idx = new_devices[label]
                            self.selected_input_name = label
                            self.config["input_device_name"] = label
                            self.config["input_device_id"] = self.selected_input_idx
                            save_config(self.config)
                            print(f"[HARDWARE] Auto-selected new microphone: {label}")
                            break
                    else:
                        # Edge Case 1.6: User plugs new USB headset while active mid-call.
                        # Update original_mic_id to the new headset, keeping CABLE Output default so call isn't interrupted.
                        if self.router and self.router.is_swapped:
                            guid, name = self.router.get_current_default_mic()
                            if guid and "cable" not in name.lower():
                                self.router.original_mic_id = guid
                                self.config["original_mic_id"] = guid
                                save_config(self.config)
                                print(f"[HARDWARE] Updated original_mic_id to new headset: {name}")

                if removed:
                    print(f"[HARDWARE] Microphone removed: {', '.join(removed)}")
                    # Name-based check: if active microphone was removed or is not in new devices
                    if self.selected_input_name in removed or self.selected_input_name not in new_devices:
                        self.selected_input_idx = self._get_windows_default_input()
                        self.selected_input_name = self._get_selected_input_label()
                        self.config["input_device_name"] = self.selected_input_name
                        self.config["input_device_id"] = self.selected_input_idx
                        save_config(self.config)
                        print(f"[HARDWARE] Active microphone disconnected. Fallback to: {self.selected_input_name} (index {self.selected_input_idx})")

                # If active device is in new devices, keep its index updated in case of index shifts
                if self.selected_input_name in new_devices:
                    self.selected_input_idx = new_devices[self.selected_input_name]

                # Validate output endpoint
                self.is_vbcable_installed, cable_in, _ = check_vbcable_status()
                if self.is_vbcable_installed and cable_in:
                    self.selected_output_idx = cable_in['index']
                    self.selected_output_name = cable_in['name']
                else:
                    target_out, _ = auto_select_output_device()
                    self.selected_output_idx = target_out['index']
                    self.selected_output_name = target_out.get('name', 'Default Output')

                # Update UI dropdown
                if self._ui_built and dpg.does_item_exist("input_combo"):
                    combo_items = list(self.device_map.keys())
                    current_label = self._get_selected_input_label()
                    dpg.configure_item("input_combo", items=combo_items, default_value=current_label)

                self._update_cable_banner()

                # Seamlessly restart stream if it was running before
                if was_running and self._app_running:
                    acquired = self._state_lock.acquire(timeout=4.0)
                    if acquired:
                        try:
                            # Re-verify that user didn't stop the stream while rescan was running
                            if not self._app_running:
                                return
                            print(f"[HARDWARE] Resuming stream on [{self.selected_input_idx}] {self.selected_input_name} -> [{self.selected_output_idx}] {self.selected_output_name}")
                            self.engine = create_engine_from_config(
                                self.config,
                                self.selected_input_idx,
                                self.selected_output_idx,
                                router=self.router,
                            )
                            self.engine.start()
                            self.is_running = True
                            if self._ui_built and dpg.does_item_exist("status_badge_text"):
                                dpg.set_value("status_badge_text", "[ ACTIVE ]")
                                dpg.configure_item("status_badge_text", color=[25, 145, 75])
                                dpg.configure_item("btn_toggle", label="STOP", enabled=True)
                                dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
                            self.set_status_pill("● AI Filter Active • Clean Voice Routed", [22, 138, 72])
                            self._update_tray_icon()
                            self._set_voice_test_button_state(False)
                        except Exception as e:
                            print(f"[HARDWARE] Failed to restart stream after rescan: {e}")
                            self.is_running = False
                            if self.engine:
                                try:
                                    self.engine.prepare_for_stop()
                                except Exception:
                                    pass
                                self.engine = None
                            if self._ui_built and dpg.does_item_exist("status_badge_text"):
                                dpg.set_value("status_badge_text", "[ STOPPED ]")
                                dpg.configure_item("status_badge_text", color=[210, 45, 45])
                                dpg.configure_item("btn_toggle", label="START", enabled=True)
                                dpg.bind_item_theme("btn_toggle", self.theme_start_btn)
                            self._set_voice_test_button_state(True)
                            self.set_status_pill(f"▲ {self._format_audio_error(e)}", [210, 45, 45])
                        finally:
                            self._release_state_lock()
                    else:
                        print("[HARDWARE] Could not acquire state lock to resume stream; aborting auto-restart.")

            except Exception as e:
                print(f"[HARDWARE] Error during hardware rescan: {e}")
            finally:
                self._is_rescanning = False

    def _get_selected_input_label(self) -> str:
        for label, idx in self.device_map.items():
            if idx == self.selected_input_idx:
                return label
        return list(self.device_map.keys())[0] if self.device_map else "No Microphone Detected"

    def on_input_changed(self, sender, app_data):
        """Handles user selecting a different microphone from dropdown (Option B: locked while active)."""
        if self.is_running or self._is_testing_voice or not self._state_lock.acquire(blocking=False):
            # Option B: Lock microphone switching while active/testing.
            # Revert dropdown value back to currently active microphone and warn user.
            current_label = self._get_selected_input_label()
            if dpg.does_item_exist("input_combo"):
                dpg.set_value("input_combo", current_label)
            self._update_selection_hint(current_label)
            self.set_status_pill("▲ Stop noise cancellation first to switch microphone.", [195, 115, 15])
            return

        try:
            if app_data in self.device_map:
                new_idx = self.device_map[app_data]
                self.selected_input_idx = new_idx
                self.selected_input_name = app_data
                matching_cable = find_matching_cable_input(new_idx)
                if self.is_vbcable_installed and matching_cable:
                    self.selected_output_idx = matching_cable['index']
                    self.selected_output_name = matching_cable['name']
                elif self.is_vbcable_installed:
                    target_out, _ = auto_select_output_device()
                    if target_out:
                        self.selected_output_idx = target_out['index']
                        self.selected_output_name = target_out.get('name', 'Default Output')
                print(f"[GUI] Switched microphone to: [{new_idx}] {app_data} (output: [{self.selected_output_idx}] {self.selected_output_name})")
                self.config["input_device_name"] = app_data
                self.config["input_device_id"] = new_idx
                self.config["output_device_name"] = self.selected_output_name
                self.config["output_device_id"] = self.selected_output_idx
                save_config(self.config)
                self._update_selection_hint(app_data)
                self.set_status_pill(f"● Microphone selected: {clean_device_label(app_data)}", [22, 138, 72])
        finally:
            self._release_state_lock()

    def _update_selection_hint(self, selected_label: Optional[str] = None):
        """Updates the dynamic advisory hint beneath the microphone dropdown."""
        if not self._ui_built or not dpg.does_item_exist("input_selection_hint"):
            return
        label = selected_label or self._get_selected_input_label()
        available = list(self.device_map.keys())
        hint = selection_hint(label, available)
        if hint:
            dpg.set_value("input_selection_hint", f"▲ {hint}")
            dpg.show_item("input_selection_hint")
        else:
            dpg.hide_item("input_selection_hint")


    def _get_idle_status(self) -> str:
        """Returns the single-line idle status string based on driver and auto_route configuration."""
        if not self.is_vbcable_installed:
            return "▲ Virtual Cable required • Please install driver"
        if self.config.get("auto_route", True):
            return "● Ready • Default Mic Routing ON"
        return "● Ready • Manual Routing Mode"

    def _get_idle_color(self) -> List[int]:
        """Returns the color for the idle status pill."""
        if not self.is_vbcable_installed:
            return [195, 115, 15]  # Warning Amber
        return [90, 105, 125]  # Muted Slate

    def set_status_pill(self, text: str, color: Optional[List[int]] = None):
        """Updates the compact single-line status pill with text and optional color."""
        if not self._ui_built or not dpg.does_item_exist("active_notice"):
            return
        dpg.set_value("active_notice", text)
        if color:
            dpg.configure_item("active_notice", color=color)

    def _format_audio_error(self, e: Exception) -> str:
        """Translates technical PortAudio / driver exceptions into clear, actionable advice."""
        explained = explain_start_error(e)
        print(f"[AUDIO] Stream error [{explained.kind}] code={explained.code}: {explained.technical}")
        return f"{explained.title}. {explained.advice}"


    def _set_voice_test_button_state(self, enabled: bool):
        """Sets the voice test button enabled/disabled state, contextual label, and theme."""
        if not self._ui_built or not dpg.does_item_exist("btn_voice_test"):
            return
        if enabled and not self.is_vbcable_installed:
            enabled = False
        if enabled:
            dpg.configure_item("btn_voice_test", enabled=True, label="Hear Myself (11s Voice Test)")
            dpg.bind_item_theme("btn_voice_test", self.theme_voice_btn)
        else:
            dpg.configure_item("btn_voice_test", enabled=False, label="Voice Test Unavailable")
            dpg.bind_item_theme("btn_voice_test", self.theme_voice_btn_disabled)

    def _update_toggle_ui(self, tag: str, is_on: bool):
        """Refreshes the visual switch track and knob colors and position."""
        if getattr(self, '_ui_built', False) is not True:
            return
        bg_tag = f"{tag}_sw_bg"
        knob_tag = f"{tag}_sw_knob"
        txt_tag = f"{tag}_sw_txt"
        try:
            if not dpg.does_item_exist(bg_tag) or not dpg.does_item_exist(knob_tag):
                return

            w = self.s(36)
            h = self.s(18)
            r = self.s(9)
            knob_r = self.s(6.5)

            if is_on:
                bg_fill = (0, 122, 255)
                bg_border = (0, 105, 225)
                knob_center = (w - r, r)
                knob_fill = (255, 255, 255)
                txt_color = [15, 23, 42]
            else:
                bg_fill = (218, 226, 238)
                bg_border = (195, 206, 220)
                knob_center = (r, r)
                knob_fill = (255, 255, 255)
                txt_color = [45, 60, 80]

            dpg.configure_item(bg_tag, fill=bg_fill, color=bg_border)
            dpg.configure_item(knob_tag, center=knob_center, fill=knob_fill, color=knob_fill)
            if dpg.does_item_exist(txt_tag):
                dpg.configure_item(txt_tag, color=txt_color)
        except Exception:
            pass

    def _on_toggle_switch_clicked(self, tag: str):
        """Click dispatcher for custom toggle switches."""
        if tag == "chk_auto_route":
            curr = bool(self.config.get("auto_route", True))
            self.on_auto_route_toggled(tag, not curr)
            self._update_toggle_ui(tag, bool(self.config.get("auto_route", True)))
        elif tag == "chk_music_mode":
            curr = bool(self.config.get("music_mode", False))
            self.on_music_mode_toggled(tag, not curr)
            self._update_toggle_ui(tag, bool(self.config.get("music_mode", False)))
        elif tag == "chk_autostart":
            curr = getattr(self, "is_autostart_active", False)
            self.on_autostart_toggled(tag, not curr)
            self._update_toggle_ui(tag, self.is_autostart_active)

    def _add_toggle_row(self, tag: str, label: str, initial_value: bool, on_click):
        """Builds a modern toggle switch row with responsive hit area."""
        w = self.s(36)
        h = self.s(18)
        r = self.s(9)
        knob_r = self.s(6.5)

        bg_tag = f"{tag}_sw_bg"
        knob_tag = f"{tag}_sw_knob"
        dl_tag = f"{tag}_sw_dl"
        txt_tag = f"{tag}_sw_txt"
        handler_tag = f"{tag}_sw_handler"

        if initial_value:
            bg_fill = (0, 122, 255)
            bg_border = (0, 105, 225)
            knob_center = (w - r, r)
            knob_fill = (255, 255, 255)
            txt_color = [15, 23, 42]
        else:
            bg_fill = (218, 226, 238)
            bg_border = (195, 206, 220)
            knob_center = (r, r)
            knob_fill = (255, 255, 255)
            txt_color = [45, 60, 80]

        with dpg.group(horizontal=True, tag=f"{tag}_sw_row"):
            with dpg.drawlist(width=w, height=h, tag=dl_tag):
                dpg.draw_rectangle((0, 0), (w, h), rounding=r, fill=bg_fill, color=bg_border, tag=bg_tag)
                dpg.draw_circle(knob_center, knob_r, fill=knob_fill, color=knob_fill, tag=knob_tag)
            dpg.add_spacer(width=self.s(6))
            dpg.add_text(label, color=txt_color, tag=txt_tag)

        with dpg.item_handler_registry(tag=handler_tag):
            dpg.add_item_clicked_handler(button=dpg.mvMouseButton_Left, callback=lambda s, a: on_click())
        dpg.bind_item_handler_registry(dl_tag, handler_tag)
        dpg.bind_item_handler_registry(txt_tag, handler_tag)

    def on_auto_route_toggled(self, sender, app_data):
        """Handles user toggling default microphone switch setting (Option B: locked while active)."""
        if self.is_running or self._is_testing_voice or not self._state_lock.acquire(blocking=False):
            # Option B: Lock auto-route preference while active/testing.
            # Revert checkbox value back to saved config setting and warn user.
            current_setting = self.config.get("auto_route", True)
            if dpg.does_item_exist("chk_auto_route"):
                dpg.set_value("chk_auto_route", current_setting)
            self.set_status_pill("▲ Stop noise cancellation first to change routing preference.", [240, 180, 50])
            return

        try:
            self.config["auto_route"] = bool(app_data)
            save_config(self.config)
            print(f"[GUI] Set as Default Microphone while active: {app_data}")
            if getattr(self, '_ui_built', False) is True and dpg.does_item_exist("chk_auto_route"):
                dpg.set_value("chk_auto_route", bool(app_data))
            self._update_toggle_ui("chk_auto_route", bool(app_data))
            if not self.is_running:
                self.set_status_pill(self._get_idle_status(), self._get_idle_color())
        finally:
            self._release_state_lock()

    def on_music_mode_toggled(self, sender, app_data):
        """Live toggle for Music & Performance Mode (bypasses RNNoise, extends hangover to 800ms)."""
        enabled = bool(app_data)
        self.config["music_mode"] = enabled
        save_config(self.config)
        print(f"[GUI] Music & Performance Mode: {'ENABLED' if enabled else 'DISABLED'}")
        if getattr(self, '_ui_built', False) is True and dpg.does_item_exist("chk_music_mode"):
            dpg.set_value("chk_music_mode", enabled)
        self._update_toggle_ui("chk_music_mode", enabled)
        if self.engine:
            if enabled:
                self.engine.denoise_enabled = False
                self.engine.gate.hangover_frames = int(800.0 / 10.0)
                self.engine.gate.decay_rate = math.exp(-10.0 / 250.0)
            else:
                self.engine.denoise_enabled = self.config.get("denoise_enabled", True)
                hangover = float(self.config.get("vad_hangover_ms", 320.0))
                decay = float(self.config.get("vad_decay_ms", 80.0))
                self.engine.gate.hangover_frames = int(hangover / 10.0)
                self.engine.gate.decay_rate = math.exp(-10.0 / decay)

    def on_autostart_toggled(self, sender, app_data):
        """Toggles automatic launch on Windows startup via HKCU Run registry key."""
        target = bool(app_data)
        success = set_autostart(target)
        if success:
            self.is_autostart_active = target
            self.config["start_with_windows"] = target
            save_config(self.config)
            print(f"[CONFIG] Start with Windows set to: {target}")
            if getattr(self, '_ui_built', False) is True and dpg.does_item_exist("chk_autostart"):
                dpg.set_value("chk_autostart", target)
            self._update_toggle_ui("chk_autostart", target)
            self.set_status_pill(
                f"● Windows Startup: {'Enabled' if target else 'Disabled'}",
                [22, 138, 72] if target else [90, 105, 125]
            )
        else:
            self.set_status_pill("▲ Failed to update Windows Startup setting", [210, 45, 45])

    def on_vad_slider_changed(self, sender, app_data):
        """Live thread-safe adjustment of VAD sensitivity without restarting stream."""
        new_val = round(float(app_data), 2)
        self.config["vad_threshold"] = new_val
        self.config["vad_customized"] = True
        if self.engine:
            self.engine.set_vad_threshold(new_val)
        with self._vad_timer_lock:
            if self._vad_save_timer and self._vad_save_timer.is_alive():
                self._vad_save_timer.cancel()
            self._vad_save_timer = threading.Timer(0.5, lambda: save_config(self.config))
            self._vad_save_timer.daemon = True
            self._vad_save_timer.start()

    def toggle_stream(self, sender=None, app_data=None):
        """
        Single toggle button handling Start and Stop with symmetric 3-second state lock.
        Spam-proof with thread locks and smooth status feedback.
        """
        if not self._app_running:
            return

        if self._is_testing_voice:
            self.set_status_pill("▲ Microphone voice test in progress. Please wait.", [195, 115, 15])
            return

        if not self._state_lock.acquire(blocking=False):
            # Already transitioning (locked) - ignore spam clicks
            return

        if not self.is_running:
            # START FLOW
            if self.selected_input_idx is None or self.selected_input_idx < 0:
                self.set_status_pill("▲ No microphone detected. Please connect a mic.", [210, 45, 45])
                self._release_state_lock()
                return
            if self.selected_output_idx is None or self.selected_output_idx < 0:
                self.set_status_pill("▲ No audio output device detected.", [210, 45, 45])
                self._release_state_lock()
                return

            if not self.is_vbcable_installed:
                # Dynamically re-verify before showing modal in case driver was just installed
                self._refresh_devices(reinit_portaudio=True)
                if not self.is_vbcable_installed:
                    self.show_install_modal()
                    self.set_status_pill("▲ Virtual Cable required to route clean voice.", [210, 45, 45])
                    self._release_state_lock()
                    return

            # Lock UI immediately into Starting state (Crimson Red button)
            dpg.configure_item("btn_toggle", label="Starting...", enabled=False)
            dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
            dpg.set_value("status_badge_text", "[ STARTING... ]")
            dpg.configure_item("status_badge_text", color=[195, 115, 15])
            self._set_voice_test_button_state(False)
            self.set_status_pill("◌ Starting AI Filter • Routing to CABLE Output...", [195, 115, 15])

            def _start_worker():
                start_time = time.monotonic()
                success = False
                try:
                    self.engine = create_engine_from_config(
                        self.config,
                        self.selected_input_idx,
                        self.selected_output_idx,
                        router=self.router,
                    )
                    self.engine.start()
                    self.is_running = True
                    success = True

                    pair = getattr(self.engine, 'resolved_pair', None)
                    if pair:
                        if pair.input != self.selected_input_idx:
                            print(f"[GUI] Synchronizing selected input to resolved safe device [{pair.input}]")
                            self.selected_input_idx = pair.input
                            try:
                                self.selected_input_name = sd.query_devices(pair.input)['name']
                            except Exception:
                                pass
                        if pair.output != self.selected_output_idx:
                            self.selected_output_idx = pair.output
                            try:
                                self.selected_output_name = sd.query_devices(pair.output)['name']
                            except Exception:
                                pass
                        if dpg.does_item_exist("input_combo"):
                            dpg.configure_item("input_combo", default_value=self._get_selected_input_label())

                    self.config["input_device_id"] = self.selected_input_idx
                    self.config["input_device_name"] = self.selected_input_name
                    self.config["output_device_id"] = self.selected_output_idx
                    self.config["output_device_name"] = self.selected_output_name
                    save_config(self.config)
                    self._update_tray_icon()
                except Exception as e:
                    print(f"[ERROR] Failed to start stream: {e}")
                    if self.engine:
                        try:
                            self.engine.prepare_for_stop()
                        except Exception:
                            pass
                        self.engine = None
                    self.is_running = False
                    self.set_status_pill(f"▲ {self._format_audio_error(e)}", [210, 45, 45])
                finally:
                    # Enforce symmetric 3-second state lock
                    elapsed = time.monotonic() - start_time
                    remaining = max(0.0, 3.0 - elapsed)
                    if remaining > 0:
                        time.sleep(remaining)

                    if success and self.is_running:
                        dpg.set_value("status_badge_text", "[ ACTIVE ]")
                        dpg.configure_item("status_badge_text", color=[25, 145, 75])
                        dpg.configure_item("btn_toggle", label="STOP", enabled=True)
                        dpg.bind_item_theme("btn_toggle", self.theme_stop_btn)
                        pair = getattr(self.engine, 'resolved_pair', None)
                        if pair and pair.ambiguous:
                            self.set_status_pill("● Active • Default mic selected (Confirm in dropdown)", [195, 115, 15])
                        elif self.config.get("auto_route", True):
                            if getattr(self.engine, 'router_swap_success', True):
                                self.set_status_pill("● AI Filter Active • Clean Voice Routed", [22, 138, 72])
                            else:
                                self.set_status_pill("▲ AI Filter Active • Auto-Route Failed (Select CABLE in Apps)", [195, 115, 15])
                        else:
                            self.set_status_pill("● AI Filter Active • Manual Output Mode", [22, 138, 72])
                        self._set_voice_test_button_state(False)
                    else:
                        dpg.set_value("status_badge_text", "[ STOPPED ]")
                        dpg.configure_item("status_badge_text", color=[210, 45, 45])
                        dpg.configure_item("btn_toggle", label="START", enabled=True)
                        dpg.bind_item_theme("btn_toggle", self.theme_start_btn)
                        self._set_voice_test_button_state(True)

                    self._release_state_lock()

            threading.Thread(target=_start_worker, daemon=True).start()

        else:
            # STOP FLOW
            # Lock UI immediately into Stopping state
            dpg.configure_item("btn_toggle", label="Stopping...", enabled=False)
            dpg.set_value("status_badge_text", "[ STOPPING... ]")
            dpg.configure_item("status_badge_text", color=[195, 115, 15])
            self.set_status_pill("◌ Restoring physical microphone...", [195, 115, 15])

            def _stop_worker():
                start_time = time.monotonic()
                try:
                    if self.engine:
                        self.engine.prepare_for_stop()
                        self.engine = None
                    elif self.router and self.router.is_swapped:
                        self.router.restore_original()
                    self.is_running = False
                    self._update_tray_icon()
                except Exception as e:
                    print(f"[ERROR] Failed to stop stream cleanly: {e}")
                finally:
                    # Enforce symmetric 3-second state lock
                    elapsed = time.monotonic() - start_time
                    remaining = max(0.0, 3.0 - elapsed)
                    if remaining > 0:
                        time.sleep(remaining)

                    dpg.set_value("status_badge_text", "[ STOPPED ]")
                    dpg.configure_item("status_badge_text", color=[210, 45, 45])
                    dpg.configure_item("btn_toggle", label="START", enabled=True)
                    dpg.bind_item_theme("btn_toggle", self.theme_start_btn)
                    self._set_voice_test_button_state(True)
                    self.set_status_pill(self._get_idle_status(), self._get_idle_color())
                    self._release_state_lock()

            threading.Thread(target=_stop_worker, daemon=True).start()

    def start_voice_test(self, sender=None, app_data=None):
        """Starts the 11-second in-memory voice preview test."""
        if not self.is_vbcable_installed:
            self.set_status_pill("▲ Virtual Cable required • Please install driver", [195, 115, 15])
            return
        if self.is_running:
            self.set_status_pill("▲ Stop noise cancellation first to run voice test.", [195, 115, 15])
            return
        if self._is_testing_voice:
            return
        if not self._state_lock.acquire(blocking=False):
            return

        self._is_testing_voice = True
        self._cancel_voice_test = False
        dpg.configure_item("btn_voice_test", enabled=False, label="Testing...")
        dpg.configure_item("btn_toggle", enabled=False)

        threading.Thread(target=self._run_voice_test_worker, daemon=True).start()

    def _run_voice_test_worker(self):
        """
        Executes the 3-phase Voice Test:
        Phase 1: Record 11s from physical microphone (speakers MUTED, zero acoustic feedback).
                 Processed in-flight through HighPassFilter, RNNoise, AdaptiveNoiseGate, and mic_gain.
                 Stored strictly in RAM (numpy float32 buffer).
        Phase 2: Play back 11s processed audio through primary physical playback device.
        Phase 3: Immediate RAM purge (buffer deleted, zero disk files).
        """
        rn = None
        try:
            if self.selected_input_idx is None or self.selected_input_idx < 0:
                self.set_status_pill("▲ No microphone selected for voice test.", [210, 45, 45])
                return

            # Resolve safe non-WDM-KS input endpoint for voice preview
            try:
                raw_devs = sd.query_devices()
                raw_apis = sd.query_hostapis()
                safe_input_idx, notes = resolve_single(raw_devs, raw_apis, self.selected_input_idx, is_input=True)
                for note in notes:
                    print(f"[VOICE TEST] {note}")
            except Exception as e:
                print(f"[VOICE TEST] Input resolution fallback: {e}")
                safe_input_idx = self.selected_input_idx

            in_info = sd.query_devices(safe_input_idx)
            in_channels = min(2, max(1, in_info['max_input_channels']))

            # Instantiate DSP chain matching current settings
            boost_db = self.config.get("mic_boost_db", 0)
            boost_mult = 10.0 ** (boost_db / 20.0)
            dev_name = in_info.get('name', '')
            is_laptop = is_laptop_microphone(dev_name, safe_input_idx)
            base_gain = 1.2 if is_laptop else 1.0
            test_gain = base_gain * boost_mult
            out_gain = float(self.config.get("output_gain", 1.08))
            total_gain = out_gain * test_gain
            vad_th = float(self.config.get("vad_threshold", 0.70))

            music_mode = self.config.get("music_mode", False)
            rn = None if music_mode else RNNoise()
            hpf = HighPassFilter(cutoff_hz=80.0, sample_rate=float(SAMPLE_RATE))
            ts = TransientSuppressor()
            hangover_ms = 800.0 if music_mode else float(self.config.get("vad_hangover_ms", 320.0))
            decay_ms = 250.0 if music_mode else float(self.config.get("vad_decay_ms", 80.0))
            gate = AdaptiveNoiseGate(
                threshold=vad_th,
                close_threshold=float(self.config.get("vad_close_threshold", 0.52)),
                hangover_ms=hangover_ms,
                decay_ms=decay_ms,
                attack_ms=28.0,
                frame_ms=10.0,
                onset_threshold=float(self.config.get("vad_onset_threshold", 0.35)),
                onset_snr_db=float(self.config.get("vad_onset_snr_db", 10.0)),
                lookahead=True,
                floor_gain=float(self.config.get("vad_floor_gain", 0.06)),
                cooldown_ms=150.0,
            )
            gate.recalibrate()
            leveler = SpeechLeveler() if self.config.get("auto_level", False) else None

            # Phase 1: Record 11 seconds (speakers MUTED, RAM only)
            total_chunks = 1100
            chunk_size = FRAME_SIZE
            processed_chunks = []

            # Query host API for WASAPI auto_convert sample rate resiliency (e.g. 16kHz Bluetooth mics)
            in_extra_settings = None
            try:
                raw_apis = sd.query_hostapis()
                in_api_idx = in_info.get('hostapi')
                if in_api_idx is not None and isinstance(raw_apis, (list, tuple)) and in_api_idx < len(raw_apis):
                    if 'WASAPI' in raw_apis[in_api_idx]['name'].upper():
                        in_extra_settings = sd.WasapiSettings(auto_convert=True)
            except Exception:
                in_extra_settings = None

            print(f"[VOICE TEST] Phase 1: Recording 11s from [{safe_input_idx}] {in_info['name']}...")
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                blocksize=chunk_size,
                device=safe_input_idx,
                channels=in_channels,
                dtype='float32',
                extra_settings=in_extra_settings,
            ) as stream:
                for chunk_idx in range(total_chunks):
                    if not self._app_running or self._cancel_voice_test:
                        break
                    indata, _ = stream.read(chunk_size)
                    # Mono mix
                    if indata.ndim > 1 and indata.shape[1] >= 2:
                        mono = (indata[:, 0] + indata[:, 1]) * 0.5
                    elif indata.ndim > 1:
                        mono = indata[:, 0].copy()
                    else:
                        mono = indata.copy()

                    # Unified Single-Frame DSP Pipeline
                    mono, _, _, _ = process_mono_frame(
                        frame_mono=mono,
                        rnnoise=rn,
                        hpf=hpf,
                        gate=gate,
                        total_gain=total_gain,
                        denoise_enabled=bool(rn is not None),
                        leveler=leveler,
                        transient_suppressor=ts,
                        mic_boost_db=float(boost_db),
                    )
                    processed_chunks.append(mono)

                    # Update countdown UI once every 100 chunks (~1.0s)
                    if chunk_idx % 100 == 0:
                        sec_left = 11 - (chunk_idx // 100)
                        self.set_status_pill(f"◌ Voice Test: Recording to RAM... ({sec_left}s left)", [20, 110, 200])
                        dpg.configure_item("btn_voice_test", label=f"Recording... ({sec_left}s)")

            if not self._app_running or self._cancel_voice_test or not processed_chunks:
                return

            # Combine processed chunks strictly in RAM
            full_audio = np.concatenate(processed_chunks, axis=0)

            # Phase 2: Playback 11s through primary physical speakers/headphones
            out_dev = get_physical_output_device(force_rescan=True)
            out_idx = out_dev['index'] if (out_dev and isinstance(out_dev, dict)) else (out_dev if isinstance(out_dev, int) else sd.default.device[1])
            try:
                raw_devs = sd.query_devices()
                raw_apis = sd.query_hostapis()
                safe_out_idx, _ = resolve_single(raw_devs, raw_apis, out_idx, is_input=False)
            except Exception:
                safe_out_idx = out_idx

            out_info = sd.query_devices(safe_out_idx)
            out_channels = min(2, max(1, out_info['max_output_channels']))

            out_extra_settings = None
            try:
                raw_apis = sd.query_hostapis()
                out_api_idx = out_info.get('hostapi')
                if out_api_idx is not None and isinstance(raw_apis, (list, tuple)) and out_api_idx < len(raw_apis):
                    if 'WASAPI' in raw_apis[out_api_idx]['name'].upper():
                        out_extra_settings = sd.WasapiSettings(auto_convert=True)
            except Exception:
                out_extra_settings = None

            print(f"[VOICE TEST] Phase 2: Playing back 11s to [{safe_out_idx}] {out_info['name']}...")
            if out_channels == 2:
                playback_data = np.column_stack((full_audio, full_audio))
            else:
                playback_data = full_audio

            sd.play(playback_data, samplerate=SAMPLE_RATE, device=safe_out_idx, extra_settings=out_extra_settings)

            # Countdown playback (11s)
            for sec in range(11, 0, -1):
                if not self._app_running or self._cancel_voice_test:
                    sd.stop()
                    break
                self.set_status_pill(f"◌ Voice Test: Playing back clean voice... ({sec}s left)", [20, 110, 200])
                dpg.configure_item("btn_voice_test", label=f"Playing back... ({sec}s)")
                time.sleep(1.0)
            sd.stop()

            # Phase 3: Immediate Purge
            del full_audio
            del playback_data
            del processed_chunks
            print("[VOICE TEST] Phase 3: RAM buffer purged. Zero disk clutter.")
            self.set_status_pill("● Voice test complete • Clean audio verified", [22, 138, 72])

        except Exception as e:
            print(f"[VOICE TEST] Error during voice preview: {e}")
            self.set_status_pill(f"▲ {self._format_audio_error(e)}", [210, 45, 45])
        finally:
            if rn is not None:
                try:
                    rn.close()
                except Exception:
                    pass
            self._is_testing_voice = False
            self._set_voice_test_button_state(True)
            if dpg.does_item_exist("btn_toggle"):
                dpg.configure_item("btn_toggle", enabled=True)
            self._release_state_lock()

    def restore_window(self):
        """Restores window from notification area / system tray and forces foreground focus."""
        if self._hwnd:
            if getattr(self, "_lock_window_pos", False) and self._fixed_x is not None:
                user32.SetWindowPos(
                    self._hwnd, 0,
                    self._fixed_x, self._fixed_y, 0, 0,
                    SWP_NOSIZE | SWP_NOZORDER
                )
            force_foreground_window(self._hwnd)

    def _update_tray_icon(self):
        if self._tray_icon:
            self._tray_icon.icon = create_tray_icon_image(self.is_running)
            self._tray_icon.title = f"DeepNoise: {'Active' if self.is_running else 'Stopped'}"

    def run_driver_install_thread(self):
        """Runs silent VB-Cable installation in background."""
        dpg.show_item("install_spinner")
        dpg.set_value(
            "install_status_text",
            "Installing VB-Audio Virtual Cable...\n\n"
            "A Windows permission prompt (UAC) may appear.\n"
            "Please click 'Yes' to allow the installation."
        )
        dpg.configure_item("btn_install_confirm", enabled=False)
        dpg.configure_item("btn_install_cancel", enabled=False)

        def worker():
            success = install_vbcable_driver()

            # Robust polling loop: Wait for Windows AudioEndpointBuilder and Audiosrv to register endpoints
            # Check every 1.0s up to 10 seconds
            for _ in range(10):
                time.sleep(1.0)
                self._refresh_devices(reinit_portaudio=True)
                if self.is_vbcable_installed:
                    break

            dpg.hide_item("install_spinner")
            dpg.hide_item("modal_buttons_row")
            dpg.show_item("btn_install_done")

            if self.is_vbcable_installed:
                dpg.set_value(
                    "install_status_text",
                    "Virtual Audio Driver Installed & Ready!\n\n"
                    "No restart required. Clean, denoised audio is now\n"
                    "ready for all your games and voice apps."
                )
                self._update_cable_banner()
            elif success:
                dpg.set_value(
                    "install_status_text",
                    "Driver Setup Completed!\n\n"
                    "If the device does not appear immediately,\n"
                    "a quick Windows restart will finalize it."
                )
                self._update_cable_banner()
            else:
                dpg.set_value(
                    "install_status_text",
                    "Installation was cancelled or failed.\n\n"
                    "You can continue using Headphone Monitor Mode."
                )

        threading.Thread(target=worker, daemon=True).start()

    def on_install_done_click(self, sender, app_data):
        dpg.hide_item("modal_vbcable")
        self._refresh_devices(reinit_portaudio=True)
        self._update_cable_banner()

    def on_install_skip_click(self, sender, app_data):
        dpg.hide_item("modal_vbcable")
        self._refresh_devices(reinit_portaudio=True)
        self._update_cable_banner()

    def show_install_modal(self, sender=None, app_data=None):
        self._refresh_devices(reinit_portaudio=True)
        if self.is_vbcable_installed:
            self._update_cable_banner()
            return
        dpg.show_item("modal_buttons_row")
        dpg.configure_item("btn_install_confirm", enabled=True)
        dpg.configure_item("btn_install_cancel", enabled=True)
        dpg.hide_item("btn_install_done")
        dpg.hide_item("install_spinner")
        dpg.set_value(
            "install_status_text",
            "To send clean, noise-free audio into games, voice chats,\n"
            "and calls, the Virtual Audio driver is required.\n\n"
            "Do you want me to install this now?"
        )
        if dpg.does_item_exist("modal_vbcable"):
            try:
                vp_w = dpg.get_viewport_client_width()
                vp_h = dpg.get_viewport_client_height()
                w = self.s(450)
                h = self.s(240)
                dpg.set_item_pos("modal_vbcable", [max(0, (vp_w - w) // 2), max(0, (vp_h - h) // 2)])
            except Exception:
                pass
            dpg.show_item("modal_vbcable")

    def show_how_to_use_modal(self, sender=None, app_data=None):
        """Displays the How to Use DeepNoise modal dialog, dynamically centered on the active viewport."""
        if dpg.does_item_exist("modal_how_to_use"):
            try:
                vp_w = dpg.get_viewport_client_width()
                vp_h = dpg.get_viewport_client_height()
                w = self.s(450)
                h = self.s(380)
                dpg.set_item_pos("modal_how_to_use", [max(0, (vp_w - w) // 2), max(0, (vp_h - h) // 2)])
            except Exception:
                pass
            dpg.show_item("modal_how_to_use")

    def _update_cable_banner(self):
        """Updates the status card and bottom status pill."""
        if self.is_vbcable_installed:
            dpg.set_value("banner_text", "Virtual Mic: Ready for All Voice Apps & Games")
            dpg.configure_item("banner_text", color=[22, 138, 72])
            dpg.hide_item("banner_install_btn")
            dpg.configure_item("banner_install_btn", enabled=False)
            if dpg.does_item_exist("banner_how_to_use_btn"):
                dpg.show_item("banner_how_to_use_btn")
                dpg.configure_item("banner_how_to_use_btn", enabled=True)
            self._set_voice_test_button_state(not self.is_running)
            if not self.is_running:
                self.set_status_pill(self._get_idle_status(), self._get_idle_color())
        else:
            dpg.set_value("banner_text", "Driver not installed")
            dpg.configure_item("banner_text", color=[195, 115, 15])
            dpg.show_item("banner_install_btn")
            dpg.configure_item("banner_install_btn", enabled=True)
            if dpg.does_item_exist("banner_how_to_use_btn"):
                dpg.hide_item("banner_how_to_use_btn")
                dpg.configure_item("banner_how_to_use_btn", enabled=False)
            self._set_voice_test_button_state(False)
            if not self.is_running:
                self.set_status_pill(self._get_idle_status(), self._get_idle_color())

    def _setup_window_hook(self):
        """Subclass the DearPyGui window so Minimize and Close minimize to tray."""
        title = "DeepNoise - AI Noise Cancellation"
        self._hwnd = None
        for _ in range(40):
            self._hwnd = user32.FindWindowW(None, title)
            if self._hwnd:
                break
            time.sleep(0.05)

        if not self._hwnd:
            print("[WARN] DeepNoise window handle not found for subclassing.")
            return

        # Force foreground activation on initial launch
        force_foreground_window(self._hwnd)
        apply_dwm_title_bar(self._hwnd)

        # Disable Windows Maximize button and resizing frame for compact widget
        try:
            style = user32.GetWindowLongW(self._hwnd, GWL_STYLE)
            style &= ~WS_MAXIMIZEBOX
            style &= ~WS_THICKFRAME
            user32.SetWindowLongW(self._hwnd, GWL_STYLE, style)
            user32.SetWindowPos(self._hwnd, 0, 0, 0, 0, 0, SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER)

            # Remove Move and Maximize from System Menu
            hmenu = user32.GetSystemMenu(self._hwnd, False)
            if hmenu:
                user32.RemoveMenu(hmenu, SC_MOVE, 0)
                user32.RemoveMenu(hmenu, SC_MAXIMIZE, 0)
        except Exception as e:
            print(f"[WARN] Failed to configure window style: {e}")

        # Enable Apple-style liquid glass transparency & frosted blur behind
        try:
            ex_style = user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED)
            # Alpha 242 (~95% opacity): subtle glass translucency while keeping every text glyph razor-sharp
            user32.SetLayeredWindowAttributes(self._hwnd, 0, 242, LWA_ALPHA)

            # Apply Windows DWM Blur Behind accent policy
            accent = ACCENT_POLICY()
            accent.AccentState = 3  # ACCENT_ENABLE_BLURBEHIND
            accent.AccentFlags = 2
            accent.GradientColor = 0xD8FFFFFF  # Frosted crystal white tint

            data = WINDOWCOMPOSITIONATTRIBDATA()
            data.Attrib = 19  # WCA_ACCENT_POLICY
            data.pvData = ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p)
            data.cbData = ctypes.sizeof(accent)

            set_comp = getattr(user32, "SetWindowCompositionAttribute", None)
            if set_comp:
                set_comp(self._hwnd, ctypes.byref(data))
        except Exception as e:
            print(f"[WARN] Failed to apply glass transparency: {e}")

        # Ensure fixed position coordinates are captured
        if not getattr(self, "_lock_window_pos", False) or self._fixed_x is None:
            rect = wintypes.RECT()
            user32.GetWindowRect(self._hwnd, ctypes.byref(rect))
            self._fixed_x = rect.left
            self._fixed_y = rect.top
            self._fixed_w = rect.right - rect.left
            self._fixed_h = rect.bottom - rect.top
            self._lock_window_pos = True

        WM_DEVICECHANGE = 0x0219
        WM_DEEPNOISE_RESTORE = user32.RegisterWindowMessageW("DEEPNOISE_RESTORE_WINDOW_MSG")

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_SYSCOMMAND:
                cmd = wparam & 0xFFF0
                if cmd == SC_MINIMIZE:
                    # Minimize -> Hide to system tray
                    user32.ShowWindow(hwnd, SW_HIDE)
                    return 0
                elif cmd == SC_MAXIMIZE:
                    # Maximize -> Block completely (fixed compact widget)
                    return 0
                elif cmd == SC_MOVE:
                    # Move -> Block completely (window position is locked)
                    return 0
            elif msg == WM_NCHITTEST:
                # Intercept title bar hit test so dragging cannot move the window,
                # while preserving clicks to focus and minimize/close buttons
                hit = user32.CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)
                if hit == HTCAPTION:
                    return HTCLIENT
                return hit
            elif msg == WM_MOVING and getattr(self, "_lock_window_pos", False) and self._fixed_x is not None:
                rect_ptr = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT))
                rect_ptr.contents.left = self._fixed_x
                rect_ptr.contents.top = self._fixed_y
                rect_ptr.contents.right = self._fixed_x + self._fixed_w
                rect_ptr.contents.bottom = self._fixed_y + self._fixed_h
                return 1
            elif msg == WM_WINDOWPOSCHANGING and getattr(self, "_lock_window_pos", False) and self._fixed_x is not None:
                pos_ptr = ctypes.cast(lparam, ctypes.POINTER(WINDOWPOS))
                pos_ptr.contents.flags |= SWP_NOMOVE
                pos_ptr.contents.x = self._fixed_x
                pos_ptr.contents.y = self._fixed_y
                return user32.CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)
            elif msg == WM_CLOSE:
                # Close button [X] -> Hide to system tray
                user32.ShowWindow(hwnd, SW_HIDE)
                return 0
            elif msg == 0x0011:  # WM_QUERYENDSESSION (Windows Shutdown / Restart Manager)
                return 1
            elif msg == 0x0016:  # WM_ENDSESSION (Windows Shutdown / Restart Manager)
                if wparam:
                    self._teardown_engine_and_timers()
                    dpg.stop_dearpygui()
                    return 0
            elif WM_DEEPNOISE_RESTORE and msg == WM_DEEPNOISE_RESTORE:
                self.restore_window()
                return 0
            elif msg == WM_DEVICECHANGE:
                self._on_device_change_event()
                return 1
            elif msg == 0x0082:  # WM_NCDESTROY
                self._teardown_engine_and_timers()
                # Cleanly unhook subclass procedure before window destruction
                user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, self._old_wndproc)
                return user32.CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)
            return user32.CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)

        self._wndproc_cb = WNDPROC(wndproc)
        self._old_wndproc = user32.SetWindowLongPtrW(self._hwnd, GWLP_WNDPROC, self._wndproc_cb)

        # Set window titlebar and taskbar icon explicitly via Win32
        WM_SETICON = 0x0080
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        if os.path.exists(ICON_ICO_PATH):
            try:
                # Query Windows native icon metrics for small (titlebar/Task Manager) and large (Alt+Tab) icons
                cx_sm = user32.GetSystemMetrics(49) or 16  # SM_CXSMICON
                cy_sm = user32.GetSystemMetrics(50) or 16  # SM_CYSMICON
                cx_lg = user32.GetSystemMetrics(11) or 32  # SM_CXICON
                cy_lg = user32.GetSystemMetrics(12) or 32  # SM_CYICON

                hicon_sm = user32.LoadImageW(None, ICON_ICO_PATH, IMAGE_ICON, cx_sm, cy_sm, LR_LOADFROMFILE)
                hicon_lg = user32.LoadImageW(None, ICON_ICO_PATH, IMAGE_ICON, cx_lg, cy_lg, LR_LOADFROMFILE)
                if hicon_sm:
                    user32.SendMessageW(self._hwnd, WM_SETICON, 0, hicon_sm)
                if hicon_lg:
                    user32.SendMessageW(self._hwnd, WM_SETICON, 1, hicon_lg)
            except Exception:
                pass

    def _start_tray_icon(self):
        """Starts system tray icon in background thread."""
        def on_open(icon, item):
            self.restore_window()

        def on_toggle(icon, item):
            self.toggle_stream()

        def on_exit(icon, item):
            print("[APP] Exiting DeepNoise...")
            self._teardown_engine_and_timers()
            icon.stop()
            dpg.stop_dearpygui()

        menu = Menu(
            item("Open DeepNoise", on_open, default=True),
            item("Toggle Start / Stop", on_toggle),
            pystray.Menu.SEPARATOR,
            item("Exit", on_exit),
        )

        self._tray_icon = pystray.Icon("deepnoise", create_tray_icon_image(self.is_running), "DeepNoise Noise Cancellation", menu)
        threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def build_ui(self):
        dpg.create_context()
        self._ui_built = True

        # --- APP ICON TEXTURE REGISTRY ---
        self.has_icon_texture = False
        if os.path.exists(ICON_PNG_PATH):
            try:
                iw, ih, ic, idata = dpg.load_image(ICON_PNG_PATH)
                with dpg.texture_registry(show=False):
                    dpg.add_static_texture(width=iw, height=ih, default_value=idata, tag="app_icon_texture")
                self.has_icon_texture = True
            except Exception as e:
                print(f"[WARN] Failed to load icon texture: {e}")

        # --- SOCIAL ICON TEXTURES (from original assets in root) ---
        self.social_textures = {}
        social_assets = {
            "tiktok": ("icon_tiktok", os.path.join(APP_DIR, "tiktok.avif")),
            "instagram": ("icon_instagram", os.path.join(APP_DIR, "instagram.webp")),
        }
        for kind, (tag, path) in social_assets.items():
            try:
                if not os.path.exists(path):
                    print(f"[WARN] Social icon not found: {path}")
                    continue
                icon_img = Image.open(path).convert("RGBA").resize((32, 32), Image.Resampling.LANCZOS)
                data = _image_to_dpg_texture(icon_img)
                with dpg.texture_registry(show=False):
                    dpg.add_static_texture(width=32, height=32, default_value=data, tag=tag)
                self.social_textures[kind] = tag
            except Exception as e:
                print(f"[WARN] Failed to load {kind} icon texture: {e}")

        # --- FONT CONFIGURATION: BUNDLED INTER (WITH SYSTEM FALLBACK) ---
        bundled_font_dir = os.path.join(APP_DIR, "assets", "fonts")
        system_font_dir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")

        inter_reg = os.path.join(bundled_font_dir, "Inter-Regular.ttf")
        inter_semi = os.path.join(bundled_font_dir, "Inter-SemiBold.ttf")
        inter_bold = os.path.join(bundled_font_dir, "Inter-Bold.ttf")

        if os.path.exists(inter_reg):
            path_regular = inter_reg
            path_semibold = inter_semi if os.path.exists(inter_semi) else inter_reg
            path_bold = inter_bold if os.path.exists(inter_bold) else path_semibold
            font_family = "Bundled Inter"
        elif os.path.exists(os.path.join(system_font_dir, "segoeui.ttf")):
            path_regular = os.path.join(system_font_dir, "segoeui.ttf")
            path_semibold = os.path.join(system_font_dir, "seguisb.ttf")
            path_bold = os.path.join(system_font_dir, "segoeuib.ttf")
            font_family = "System Segoe UI"
        else:
            path_regular = os.path.join(system_font_dir, "arial.ttf")
            path_semibold = os.path.join(system_font_dir, "arialbd.ttf")
            path_bold = os.path.join(system_font_dir, "arialbd.ttf")
            font_family = "Universal Arial"

        print(f"[GUI] Active Font Engine: {font_family}")

        with dpg.font_registry():
            if path_regular and os.path.exists(path_regular):
                self.font_body = dpg.add_font(path_regular, self.s(17))
                bold_file = path_semibold if (path_semibold and os.path.exists(path_semibold)) else path_bold
                self.font_header = dpg.add_font(bold_file, self.s(18)) if (bold_file and os.path.exists(bold_file)) else self.font_body
                self.font_btn = dpg.add_font(path_bold if (path_bold and os.path.exists(path_bold)) else bold_file, self.s(20))
                # Notice text reduced by -1 as requested (from 19 to 18)
                self.font_notice = dpg.add_font(path_regular, self.s(18))
                self.font_small = dpg.add_font(path_regular, self.s(17))
                dpg.bind_font(self.font_body)
            else:
                self.font_body = None
                self.font_header = None
                self.font_btn = None
                self.font_notice = None
                self.font_small = None

        # Apple Liquid Glass theme with translucent frosted crystal styling
        with dpg.theme() as global_theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (250, 252, 255, 238))
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (244, 247, 252, 215))
                dpg.add_theme_color(dpg.mvThemeCol_PopupBg, (255, 255, 255, 248))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (212, 224, 238, 190))
                dpg.add_theme_color(dpg.mvThemeCol_BorderShadow, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (240, 244, 251, 210))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (230, 238, 248, 235))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (220, 230, 245, 245))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, (246, 248, 252, 220))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (240, 244, 250, 240))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, (246, 248, 252, 220))
                dpg.add_theme_color(dpg.mvThemeCol_MenuBarBg, (255, 255, 255, 240))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrab, (195, 208, 224, 160))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabHovered, (170, 185, 205, 200))
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarGrabActive, (145, 162, 185, 220))
                dpg.add_theme_color(dpg.mvThemeCol_CheckMark, (0, 122, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, (0, 122, 255))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, (0, 105, 225))
                dpg.add_theme_color(dpg.mvThemeCol_Button, (242, 246, 252, 210))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (230, 238, 248, 235))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (218, 228, 242, 245))
                dpg.add_theme_color(dpg.mvThemeCol_Header, (232, 242, 255, 190))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderHovered, (218, 232, 252, 220))
                dpg.add_theme_color(dpg.mvThemeCol_HeaderActive, (198, 220, 248, 240))
                dpg.add_theme_color(dpg.mvThemeCol_Separator, (216, 225, 238, 170))
                dpg.add_theme_color(dpg.mvThemeCol_SeparatorHovered, (195, 208, 225, 200))
                dpg.add_theme_color(dpg.mvThemeCol_SeparatorActive, (170, 185, 210, 220))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (15, 23, 42))
                dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, (120, 135, 155))
                dpg.add_theme_color(dpg.mvThemeCol_TextSelectedBg, (180, 215, 255, 180))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(8))
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, self.s(10))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, self.s(9))
                dpg.add_theme_style(dpg.mvStyleVar_GrabRounding, self.s(8))
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, self.s(10), self.s(8))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(14), self.s(12))

        # Start button theme (Apple Spring Green with crisp white text)
        with dpg.theme() as self.theme_start_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (48, 185, 92))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (56, 205, 102))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (38, 160, 78))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(8))

        # Stop button theme (Apple Crimson Coral with crisp white text)
        with dpg.theme() as self.theme_stop_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (235, 55, 55))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (250, 70, 70))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (205, 40, 40))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(8))

        # Banner install button theme (Clean Apple Accent with subtle crystal border)
        with dpg.theme() as theme_banner_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (235, 243, 255, 230))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (222, 235, 255, 245))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (205, 225, 250, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (155, 195, 248))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (10, 80, 185))
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(7))

        # Status card child window theme (Frosted glass plate)
        with dpg.theme() as theme_status_card:
            with dpg.theme_component(dpg.mvChildWindow):
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (244, 247, 252, 215))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (215, 225, 238, 190))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(14), self.s(6))
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, self.s(9))

        # Status text group theme (Zero item spacing so spacer sets exact vertical offset)
        with dpg.theme() as theme_status_text_group:
            with dpg.theme_component(dpg.mvGroup):
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 0, 0)

        # Sponsor link button theme (Blue text, transparent background)
        with dpg.theme() as theme_link_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (235, 242, 252, 180))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (220, 232, 248, 220))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (0, 122, 255))

        # Boost active button theme (Apple System Blue with White text)
        with dpg.theme() as self.theme_boost_active:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 122, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (25, 140, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (0, 105, 225))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (0, 122, 255))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(7))

        # Boost inactive button theme (Frosted Glass capsule with subtle crystal outline and dark slate text)
        with dpg.theme() as self.theme_boost_inactive:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (244, 247, 252, 210))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (234, 240, 248, 235))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (222, 230, 240, 245))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (212, 222, 235, 180))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (25, 38, 55))
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(7))

        # Voice test button theme (Apple System Electric Blue with White text)
        with dpg.theme() as self.theme_voice_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 122, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (25, 140, 255))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (0, 105, 225))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(7))

        # Icon button theme (transparent background with subtle hover for social icons)
        with dpg.theme() as self.theme_icon_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (0, 0, 0, 0))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (255, 255, 255, 30))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (255, 255, 255, 60))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(6))

        # Voice test button theme (Frosted surface - Unavailable while active)
        with dpg.theme() as self.theme_voice_btn_disabled:
            for state in (True, False):
                with dpg.theme_component(dpg.mvButton, enabled_state=state):
                    dpg.add_theme_color(dpg.mvThemeCol_Button, (240, 243, 248, 180))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (240, 243, 248, 180))
                    dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (240, 243, 248, 180))
                    dpg.add_theme_color(dpg.mvThemeCol_Border, (222, 228, 236, 160))
                    dpg.add_theme_color(dpg.mvThemeCol_Text, (135, 148, 165))
                    dpg.add_theme_color(dpg.mvThemeCol_TextDisabled, (135, 148, 165))
                    dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(7))

        # Unified Modal theme (Apple frosted glass modal)
        with dpg.theme() as theme_modal:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (255, 255, 255, 250))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBg, (246, 248, 252, 240))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgActive, (240, 244, 250, 250))
                dpg.add_theme_color(dpg.mvThemeCol_TitleBgCollapsed, (246, 248, 252, 240))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (212, 222, 235, 200))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (15, 23, 42))
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, self.s(10), self.s(10))
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, self.s(18), self.s(16))
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, self.s(10))
                dpg.add_theme_color(dpg.mvThemeCol_Button, (235, 55, 55))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (250, 70, 70))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (205, 40, 40))
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (235, 55, 55))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (250, 70, 70))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (205, 40, 40))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (255, 255, 255))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(7))

        # Modal secondary/cancel button theme (Frosted glass light surface)
        with dpg.theme() as theme_secondary_btn:
            with dpg.theme_component(dpg.mvButton):
                dpg.add_theme_color(dpg.mvThemeCol_Button, (244, 247, 252, 210))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (234, 240, 248, 235))
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, (222, 230, 240, 245))
                dpg.add_theme_color(dpg.mvThemeCol_Border, (210, 220, 232, 180))
                dpg.add_theme_color(dpg.mvThemeCol_Text, (35, 50, 70))
                dpg.add_theme_style(dpg.mvStyleVar_FrameBorderSize, 1.0)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, self.s(7))

        dpg.bind_theme(global_theme)

        # Main window: zero scrollbar, no resize, no move
        with dpg.window(
            tag="main_window",
            no_title_bar=True,
            no_resize=True,
            no_move=True,
            no_scrollbar=True,
            no_scroll_with_mouse=True,
        ):
            # --- HEADER ---
            with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchProp):
                dpg.add_table_column(init_width_or_weight=0.74)
                dpg.add_table_column(init_width_or_weight=0.26)
                with dpg.table_row():
                    with dpg.group(horizontal=True):
                        if self.has_icon_texture:
                            dpg.add_image("app_icon_texture", width=self.s(22), height=self.s(22))
                            dpg.add_spacer(width=self.s(3))
                        dpg.add_text("DEEPNOISE - AI NOISE CANCELLATION", color=[12, 20, 35], tag="header_title_text")
                    dpg.add_text("[ STOPPED ]", tag="status_badge_text", color=[225, 45, 45])

            dpg.add_spacer(height=self.s(3))

            # --- VB-CABLE STATUS CARD ---
            with dpg.child_window(tag="status_card", width=-1, height=self.s(44), border=True, no_scrollbar=True):
                with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchProp):
                    dpg.add_table_column(init_width_or_weight=1.0)
                    dpg.add_table_column(width_fixed=True, init_width_or_weight=self.s(120))
                    with dpg.table_row():
                        with dpg.group(tag="status_text_group"):
                            dpg.add_spacer(height=self.s(3))
                            dpg.add_text(
                                "Virtual Mic Status",
                                tag="banner_text",
                                color=[18, 140, 68]
                            )
                        with dpg.group(tag="status_btn_group"):
                            dpg.add_button(
                                label="Install Driver",
                                callback=self.show_install_modal,
                                tag="banner_install_btn",
                                width=-1,
                                height=self.s(28),
                                show=False
                            )
                            dpg.add_button(
                                label="How to Use",
                                callback=self.show_how_to_use_modal,
                                tag="banner_how_to_use_btn",
                                width=-1,
                                height=self.s(28),
                                show=False
                            )
            dpg.bind_item_theme("status_card", theme_status_card)
            dpg.bind_item_theme("status_text_group", theme_status_text_group)
            dpg.bind_item_theme("status_btn_group", theme_status_text_group)
            dpg.bind_item_theme("banner_install_btn", theme_banner_btn)
            dpg.bind_item_theme("banner_how_to_use_btn", theme_banner_btn)

            dpg.add_spacer(height=self.s(3))

            # --- INPUT DEVICE SELECTION ---
            dpg.add_text("Microphone Input:", color=[25, 38, 55])
            combo_items = list(self.device_map.keys())
            dpg.add_combo(
                items=combo_items,
                default_value=self._get_selected_input_label(),
                callback=self.on_input_changed,
                tag="input_combo",
                width=-1
            )
            dpg.add_text("", tag="input_selection_hint", wrap=self.s(360), color=[195, 115, 15], show=False)

            dpg.add_spacer(height=self.s(4))


            # --- MICROPHONE BOOST (0, 5, 10, 15 dB) ---
            dpg.add_text("Microphone Boost:", color=[25, 38, 55])
            with dpg.table(header_row=False, policy=dpg.mvTable_SizingStretchSame):
                dpg.add_table_column(init_width_or_weight=1.0)
                dpg.add_table_column(init_width_or_weight=1.0)
                dpg.add_table_column(init_width_or_weight=1.0)
                dpg.add_table_column(init_width_or_weight=1.0)
                with dpg.table_row():
                    dpg.add_button(
                        label="0 dB",
                        width=-1,
                        height=self.s(30),
                        tag="btn_boost_0",
                        callback=lambda: self.set_mic_boost(0)
                    )
                    dpg.add_button(
                        label="+5 dB",
                        width=-1,
                        height=self.s(30),
                        tag="btn_boost_5",
                        callback=lambda: self.set_mic_boost(5)
                    )
                    dpg.add_button(
                        label="+10 dB",
                        width=-1,
                        height=self.s(30),
                        tag="btn_boost_10",
                        callback=lambda: self.set_mic_boost(10)
                    )
                    dpg.add_button(
                        label="+15 dB",
                        width=-1,
                        height=self.s(30),
                        tag="btn_boost_15",
                        callback=lambda: self.set_mic_boost(15)
                    )

            dpg.add_spacer(height=self.s(5))

            # --- VOICE SENSITIVITY THRESHOLD SLIDER ---
            dpg.add_text("Voice Sensitivity Threshold:", color=[25, 38, 55])
            dpg.add_slider_float(
                default_value=float(self.config.get("vad_threshold", 0.70)),
                min_value=0.40,
                max_value=0.90,
                format="%.2f",
                callback=self.on_vad_slider_changed,
                tag="slider_vad",
                width=-1,
                height=self.s(26)
            )

            dpg.add_spacer(height=self.s(4))

            # --- DEFAULT MICROPHONE SWITCH TOGGLE ---
            dpg.add_checkbox(
                label="Set as Default Microphone while active",
                default_value=self.config.get("auto_route", True),
                callback=self.on_auto_route_toggled,
                tag="chk_auto_route",
                show=False,
            )
            self._add_toggle_row(
                tag="chk_auto_route",
                label="Set as Default Microphone while active",
                initial_value=self.config.get("auto_route", True),
                on_click=lambda: self._on_toggle_switch_clicked("chk_auto_route")
            )

            dpg.add_spacer(height=self.s(6))

            # --- MUSIC & PERFORMANCE MODE TOGGLE ---
            dpg.add_checkbox(
                label="Music & Singing Mode (Bypasses AI filter, 800ms sustain)",
                default_value=self.config.get("music_mode", False),
                callback=self.on_music_mode_toggled,
                tag="chk_music_mode",
                show=False,
            )
            self._add_toggle_row(
                tag="chk_music_mode",
                label="Music & Singing Mode (Bypasses AI filter, 800ms sustain)",
                initial_value=self.config.get("music_mode", False),
                on_click=lambda: self._on_toggle_switch_clicked("chk_music_mode")
            )

            dpg.add_spacer(height=self.s(6))

            # --- AUTO STARTUP TOGGLE ---
            dpg.add_checkbox(
                label="Launch automatically on Windows startup",
                default_value=self.is_autostart_active,
                callback=self.on_autostart_toggled,
                tag="chk_autostart",
                show=False,
            )
            self._add_toggle_row(
                tag="chk_autostart",
                label="Launch automatically on Windows startup",
                initial_value=self.is_autostart_active,
                on_click=lambda: self._on_toggle_switch_clicked("chk_autostart")
            )

            dpg.add_spacer(height=self.s(6))

            # --- SINGLE ACTION BUTTON: START / STOP ---
            dpg.add_button(
                label="START",
                callback=self.toggle_stream,
                tag="btn_toggle",
                width=-1,
                height=self.s(50)
            )
            dpg.bind_item_theme("btn_toggle", self.theme_start_btn)

            dpg.add_spacer(height=self.s(4))

            # --- HEAR MYSELF (11s VOICE PREVIEW TEST) ---
            dpg.add_button(
                label="Hear Myself (11s Voice Test)",
                callback=self.start_voice_test,
                tag="btn_voice_test",
                width=-1,
                height=self.s(32)
            )
            dpg.bind_item_theme("btn_voice_test", self.theme_voice_btn)

            dpg.add_spacer(height=self.s(6))

            # --- COMPACT STATUS PILL (OPTION B) ---
            dpg.add_text(
                self._get_idle_status(),
                tag="active_notice",
                color=self._get_idle_color(),
                wrap=self.s(485)
            )

            dpg.add_spacer(height=self.s(6))
            dpg.add_separator()
            dpg.add_spacer(height=self.s(4))

# --- FOOTER: CREATOR ---
            with dpg.group(horizontal=True, tag="footer_sponsor_row"):
                dpg.add_text("Created by loredounipass", color=[85, 102, 125], tag="footer_creator_text")
                dpg.add_spacer(width=12)

                dpg.add_text("Follow Me", color=[85, 102, 125], tag="footer_follow_text")

                dpg.add_spacer(width=4)

                if "tiktok" in self.social_textures:
                    dpg.add_image_button(
                        "icon_tiktok",
                        callback=lambda: webbrowser.open("https://www.tiktok.com/@erickcedeno00"),
                        tag="footer_tiktok_btn",
                        width=32,
                        height=32,
                    )
                    dpg.bind_item_theme("footer_tiktok_btn", self.theme_icon_btn)
                    with dpg.tooltip(parent="footer_tiktok_btn"):
                        dpg.add_text("TikTok — click here")
                else:
                    dpg.add_button(
                        label="TikTok click here",
                        callback=lambda: webbrowser.open("https://www.tiktok.com/@erickcedeno00"),
                        tag="footer_tiktok_btn",
                        width=self.s(70),
                        height=self.s(32),
                    )

                dpg.add_spacer(width=4)

                if "instagram" in self.social_textures:
                    dpg.add_image_button(
                        "icon_instagram",
                        callback=lambda: webbrowser.open("https://www.instagram.com/crypto_society00/?hl=en"),
                        tag="footer_instagram_btn",
                        width=32,
                        height=32,
                    )
                    dpg.bind_item_theme("footer_instagram_btn", self.theme_icon_btn)
                    with dpg.tooltip(parent="footer_instagram_btn"):
                        dpg.add_text("Instagram — click here")
                else:
                    dpg.add_button(
                        label="Instagram click here",
                        callback=lambda: webbrowser.open("https://www.instagram.com/crypto_society00/?hl=en"),
                        tag="footer_instagram_btn",
                        width=self.s(85),
                        height=self.s(32),
                    )



            # Bind specialized fonts to elements
            if self.font_header:
                dpg.bind_item_font("header_title_text", self.font_header)
                dpg.bind_item_font("status_badge_text", self.font_header)
            if self.font_btn:
                dpg.bind_item_font("btn_toggle", self.font_btn)
                dpg.bind_item_font("btn_voice_test", self.font_body if self.font_body else self.font_btn)
            if self.font_notice:
                dpg.bind_item_font("active_notice", self.font_notice)
            if self.font_small:
                dpg.bind_item_font("footer_creator_text", self.font_small)

            # Initialize boost button states
            self._update_boost_buttons_ui()

            # --- MODAL: VB-CABLE SETUP ---
            modal_w = self.s(450)
            modal_h = self.s(240)
            modal_x = max(0, (self.s(520) - modal_w) // 2)
            modal_y = max(0, (self.s(652) - modal_h) // 2)
            with dpg.window(
                label="Virtual Audio Cable Setup",
                modal=True,
                show=False,
                tag="modal_vbcable",
                no_resize=True,
                no_move=True,
                no_scrollbar=True,
                width=modal_w,
                height=modal_h,
                pos=[modal_x, modal_y]
            ):
                dpg.add_text(
                    "To send clean, noise-free audio into games, voice chats,\n"
                    "and calls, the Virtual Audio driver is required.\n\n"
                    "Do you want me to install this now?",
                    tag="install_status_text",
                    color=[35, 45, 60]
                )
                dpg.add_spacer(height=self.s(8))
                dpg.add_loading_indicator(tag="install_spinner", show=False, radius=float(self.s(3)), color=[35, 115, 225], secondary_color=[200, 215, 235])
                dpg.add_spacer(height=self.s(8))

                with dpg.group(horizontal=True, tag="modal_buttons_row"):
                    dpg.add_button(
                        label="Install (1-Click)",
                        callback=lambda s, a: self.run_driver_install_thread(),
                        tag="btn_install_confirm",
                        width=self.s(180),
                        height=self.s(34)
                    )
                    dpg.add_button(
                        label="Not Now",
                        callback=self.on_install_skip_click,
                        tag="btn_install_cancel",
                        width=self.s(110),
                        height=self.s(34)
                    )

                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=(modal_w - self.s(130)) // 2 - self.s(18))
                    dpg.add_button(
                        label="OK",
                        callback=self.on_install_done_click,
                        tag="btn_install_done",
                        show=False,
                        width=self.s(130),
                        height=self.s(34)
                    )
            dpg.bind_item_theme("modal_vbcable", theme_modal)
            dpg.bind_item_theme("btn_install_confirm", self.theme_voice_btn)
            dpg.bind_item_theme("btn_install_cancel", theme_secondary_btn)
            dpg.bind_item_theme("btn_install_done", self.theme_voice_btn)

            # --- MODAL: HOW TO USE DEEPNOISE ---
            guide_w = self.s(450)
            guide_h = self.s(380)
            guide_x = max(0, (self.s(520) - guide_w) // 2)
            guide_y = max(0, (self.s(652) - guide_h) // 2)
            with dpg.window(
                label="How to Use DeepNoise",
                modal=True,
                show=False,
                tag="modal_how_to_use",
                no_resize=True,
                no_move=True,
                no_scrollbar=True,
                width=guide_w,
                height=guide_h,
                pos=[guide_x, guide_y]
            ):
                dpg.add_text("Just click START. Most apps will switch to clean\naudio automatically.", color=[35, 45, 60])
                dpg.add_spacer(height=self.s(6))

                dpg.add_text("Notes: Only When Needed (Games & Voice Apps)", color=[25, 105, 200])
                dpg.add_text(
                    "If an app or game doesn't pick up the noise filter,\ngo to its audio settings and choose:",
                    color=[50, 62, 78]
                )
                dpg.add_spacer(height=self.s(2))
                dpg.add_text("Microphone  ->  CABLE Output", color=[22, 138, 72])
                dpg.add_spacer(height=self.s(2))
                dpg.add_text("(This guarantees clean voice, no restart needed.)", color=[100, 115, 135])

                dpg.add_spacer(height=self.s(12))
                with dpg.group(horizontal=True):
                    dpg.add_spacer(width=(guide_w - self.s(130)) // 2 - self.s(18))
                    dpg.add_button(
                        label="Got It",
                        callback=lambda: dpg.hide_item("modal_how_to_use"),
                        tag="btn_how_to_use_ok",
                        width=self.s(130),
                        height=self.s(34)
                    )
            dpg.bind_item_theme("modal_how_to_use", theme_modal)
            dpg.bind_item_theme("btn_how_to_use_ok", self.theme_voice_btn)

        # Viewport configuration - positioned bottom-right on active monitor work area (taskbar-aware) from frame 0
        vp_w = self.s(520)
        vp_h = self.s(652)
        try:
            class MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]
            cursor_pos = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(cursor_pos))
            MONITOR_DEFAULTTONEAREST = 2
            hmon = user32.MonitorFromPoint(cursor_pos, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            margin_x = self.s(16)
            margin_y = self.s(12)
            if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                x_pos = max(mi.rcWork.left, mi.rcWork.right - vp_w - margin_x)
                y_pos = max(mi.rcWork.top, mi.rcWork.bottom - vp_h - margin_y)
            else:
                work_rect = wintypes.RECT()
                user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_rect), 0)
                x_pos = max(work_rect.left, work_rect.right - vp_w - margin_x)
                y_pos = max(work_rect.top, work_rect.bottom - vp_h - margin_y)
        except Exception:
            try:
                screen_w = user32.GetSystemMetrics(0) or 1920
                screen_h = user32.GetSystemMetrics(1) or 1080
            except Exception:
                screen_w = 1920
                screen_h = 1080
            margin_x = self.s(16)
            margin_y = self.s(12)
            x_pos = max(0, screen_w - vp_w - margin_x)
            y_pos = max(0, screen_h - vp_h - margin_y)

        dpg.create_viewport(
            title="DeepNoise - AI Noise Cancellation",
            small_icon=ICON_ICO_PATH if os.path.exists(ICON_ICO_PATH) else "",
            large_icon=ICON_ICO_PATH if os.path.exists(ICON_ICO_PATH) else "",
            width=vp_w,
            height=vp_h,
            x_pos=x_pos,
            y_pos=y_pos,
            resizable=False,
            decorated=True
        )
        dpg.setup_dearpygui()
        dpg.show_viewport()
        dpg.set_primary_window("main_window", True)

        # Render 3 layout frames to settle font glyphs, then fine-tune viewport height
        dpg.render_dearpygui_frame()
        dpg.render_dearpygui_frame()
        dpg.render_dearpygui_frame()
        try:
            rect_max = dpg.get_item_rect_max("footer_sponsor_row")
            diff = dpg.get_viewport_height() - dpg.get_viewport_client_height()
            auto_fit_h = int(rect_max[1] + self.s(26)) + diff
            if abs(auto_fit_h - dpg.get_viewport_height()) > 2:
                dpg.set_viewport_height(auto_fit_h)
            dpg.render_dearpygui_frame()
        except Exception:
            pass

        # Synchronous bottom-right window placement on active monitor work area (taskbar-aware)
        try:
            hwnd = user32.FindWindowW(None, "DeepNoise - AI Noise Cancellation")
            if hwnd:
                apply_dwm_title_bar(hwnd)
                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                win_w = rect.right - rect.left
                win_h = rect.bottom - rect.top
                MONITOR_DEFAULTTONEAREST = 2
                hmon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                mi = MONITORINFO()
                mi.cbSize = ctypes.sizeof(MONITORINFO)
                if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                    margin_x = self.s(16)
                    margin_y = self.s(12)
                    target_x = max(mi.rcWork.left, mi.rcWork.right - win_w - margin_x)
                    target_y = max(mi.rcWork.top, mi.rcWork.bottom - win_h - margin_y)
                    user32.SetWindowPos(hwnd, 0, target_x, target_y, 0, 0, SWP_NOSIZE | SWP_NOZORDER)
                    self._fixed_x = target_x
                    self._fixed_y = target_y
                    self._fixed_w = win_w
                    self._fixed_h = win_h
                    self._lock_window_pos = True
        except Exception as e:
            print(f"[WARN] Failed to position window: {e}")

        self._update_cable_banner()
        self._update_selection_hint()

        if not self.is_vbcable_installed:

            dpg.show_item("modal_vbcable")

        # Start tray icon & window hook
        self._start_tray_icon()
        threading.Thread(target=self._setup_window_hook, daemon=True).start()

        # Handle terminal signals cleanly (Ctrl+C / SIGINT / SIGTERM)
        def _sig_handler(sig, frame):
            print("\n[GUI] Terminal exit signal received. Closing cleanly...")
            dpg.stop_dearpygui()

        try:
            signal.signal(signal.SIGINT, _sig_handler)
            signal.signal(signal.SIGTERM, _sig_handler)
        except Exception:
            pass

        # Run UI event loop
        dpg.start_dearpygui()

        # Teardown background workers and restore audio before destroying DPG context
        self._teardown_engine_and_timers()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass

        dpg.destroy_context()


# Backwards compatibility alias for tests
GetsuGUI = DeepNoiseGUI


def launch_gui():
    app = DeepNoiseGUI()
    app.build_ui()


if __name__ == "__main__":
    from src.single_instance import activate_existing_instance
    if activate_existing_instance():
        sys.exit(0)
    launch_gui()
