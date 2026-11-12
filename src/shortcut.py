"""
DeepNoise - Start Menu Shortcut & Windows Recent Apps Integration.
Ensures DeepNoise has a valid Start Menu shortcut and notifies Windows Shell
via SHAddToRecentDocs so launches appear in 'Recent' / 'Agregadas recientemente'.
"""

import os
import sys
import ctypes
from typing import Optional

APP_USER_MODEL_ID = "deepnoise.ai.noisecancellation.1"


def get_shortcut_path() -> str:
    """Returns the full path to DeepNoise Start Menu shortcut."""
    programs_dir = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")
    return os.path.join(programs_dir, "DeepNoise.lnk")


def get_target_executable() -> tuple[str, str, str]:
    """
    Determines target executable, working directory, and icon path.
    Returns (target_path, working_dir, icon_path).
    """
    if getattr(sys, "frozen", False):
        exe_path = os.path.abspath(sys.executable)
        app_dir = os.path.dirname(exe_path)
        ico_path = os.path.join(app_dir, "deepnoise.ico")
        return exe_path, app_dir, ico_path

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ico_path = os.path.join(base_dir, "deepnoise.ico")

    # Check dist directory for built executable
    dist_exe = os.path.join(base_dir, "dist", "deepnoise", "DeepNoise.exe")
    if os.path.exists(dist_exe):
        return dist_exe, os.path.dirname(dist_exe), ico_path

    dist_single = os.path.join(base_dir, "dist", "DeepNoise.exe")
    if os.path.exists(dist_single):
        return dist_single, os.path.dirname(dist_single), ico_path

    main_py = os.path.join(base_dir, "src", "main.py")
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    launcher = pythonw if os.path.exists(pythonw) else sys.executable
    return launcher, base_dir, ico_path


def create_or_update_shortcut() -> Optional[str]:
    """
    Creates or updates the Start Menu shortcut using native Windows COM Dispatch (WScript.Shell).
    Ensures TargetPath, WorkingDirectory, and Icon are properly configured.
    """
    if sys.platform != "win32":
        return None

    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut_path = get_shortcut_path()
        target_exe, working_dir, icon_path = get_target_executable()

        os.makedirs(os.path.dirname(shortcut_path), exist_ok=True)
        sc = shell.CreateShortcut(shortcut_path)
        sc.TargetPath = target_exe
        sc.WorkingDirectory = working_dir
        sc.Description = "DeepNoise - AI Noise Cancellation"
        if os.path.exists(icon_path):
            sc.IconLocation = f"{icon_path},0"
        sc.Save()
        return shortcut_path
    except Exception as e:
        print(f"[WARN] Failed to configure Start Menu shortcut: {e}")
        return None


def register_recent_docs(shortcut_path: Optional[str] = None):
    """
    Notifies Windows Shell via SHAddToRecentDocs so DeepNoise is recorded
    in Windows Recent applications and Start Menu history.
    """
    if sys.platform != "win32":
        return
    try:
        shell32 = ctypes.windll.shell32
        SHARD_PATHW = 3

        sc_path = shortcut_path or get_shortcut_path()
        if os.path.exists(sc_path):
            shell32.SHAddToRecentDocs(SHARD_PATHW, ctypes.c_wchar_p(sc_path))

        target_exe, _, _ = get_target_executable()
        if os.path.exists(target_exe):
            shell32.SHAddToRecentDocs(SHARD_PATHW, ctypes.c_wchar_p(target_exe))

        print("[SHORTCUT] Registered DeepNoise with Windows Shell Recent Documents.")
    except Exception as e:
        print(f"[WARN] Failed to register recent docs: {e}")


def ensure_shortcut_and_recent():
    """Ensures shortcut is created and registered with Windows Recent Docs."""
    sc = create_or_update_shortcut()
    register_recent_docs(sc)
