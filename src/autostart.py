"""
DeepNoise - Windows Auto Startup Management.
Controls automatic application launch on Windows boot via:
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
"""

import os
import sys
import winreg
from typing import Optional

REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "DeepNoise"


def get_executable_command() -> str:
    """
    Returns the proper execution command for Windows startup.
    - If packaged as a PyInstaller executable, returns quoted path to DeepNoise.exe.
    - If running from source/dev, prefers existing built binary in dist/ or pythonw.exe launcher.
    """
    if getattr(sys, "frozen", False):
        exe_path = os.path.abspath(sys.executable)
        return f'"{exe_path}"'

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Check dist directory for compiled binary
    dist_dir_exe = os.path.join(base_dir, "dist", "deepnoise", "DeepNoise.exe")
    if os.path.exists(dist_dir_exe):
        return f'"{dist_dir_exe}"'

    dist_single_exe = os.path.join(base_dir, "dist", "DeepNoise.exe")
    if os.path.exists(dist_single_exe):
        return f'"{dist_single_exe}"'

    # Source mode: use pythonw.exe to avoid console window on startup
    python_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(python_dir, "pythonw.exe")
    launcher = pythonw if os.path.exists(pythonw) else sys.executable
    main_py = os.path.join(base_dir, "src", "main.py")
    return f'"{launcher}" "{main_py}"'


def is_autostart_enabled() -> bool:
    """
    Checks whether DeepNoise is registered in Windows Startup.
    Returns True if HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run contains DeepNoise.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except Exception as e:
        print(f"[WARN] Failed to query autostart registry key: {e}")
        return False


def set_autostart(enable: bool) -> bool:
    """
    Enables or disables auto startup for DeepNoise in Windows.
    Modifies HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.
    Returns True on success, False otherwise.
    """
    try:
        if enable:
            cmd = get_executable_command()
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE)
            except FileNotFoundError:
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY)

            with key:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                print(f"[AUTOSTART] Registered in Windows Startup: {cmd}")
            return True
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, APP_NAME)
                    print("[AUTOSTART] Removed from Windows Startup.")
            except FileNotFoundError:
                pass
            return True
    except Exception as e:
        print(f"[ERROR] Failed to update Windows autostart: {e}")
        return False


def sync_autostart_with_config(config: dict) -> bool:
    """
    Ensures Windows Startup registry state matches user config preference.
    """
    desired = bool(config.get("start_with_windows", True))
    current = is_autostart_enabled()
    if desired != current:
        return set_autostart(desired)
    return True

