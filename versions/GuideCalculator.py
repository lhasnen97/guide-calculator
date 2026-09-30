# GuideCalculator.py
# Calculates guide positions by dividing the timeline resolution into equal
# divisions, Rule of Thirds, Golden Ratio, and more. Origin is the center of
# the frame (0, 0) as per Resolve's coordinate system. Includes live preview
# styled like Resolve's viewer (rulers, +Y up, 1px guides, safe-area outlines),
# with presets and copy/export.
#
# Note: DaVinci Resolve 20 removed guide placement from the scripting API.
# This tool calculates positions only — guides must be placed manually.
#
# Install: ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
# Run via: Workspace > Scripts > GuideCalculator
#
# Resolve 21.1 runs Scripts-menu scripts in its own Python, which has no
# PySide6, and Resolve's own Qt 5 would clash with PySide6's Qt 6 anyway.
# So when launched from Resolve, this script reads the resolution there and
# re-opens itself in a separate system Python that has PySide6 installed.

import ctypes
import math
import os
import re
import threading
import time
import subprocess
import sys
import json
from pathlib import Path

# The window process launched with Resolve's own Python gets the private
# PySide6 folder through this variable. Nothing else sets it, so Resolve's
# in-process interpreter never loads Qt 6 next to Resolve's Qt 5.
if os.environ.get("GUIDECALC_QT_LIB"):
    sys.path.insert(0, os.environ["GUIDECALC_QT_LIB"])

try:
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
        QSpinBox, QPushButton, QSizePolicy, QCheckBox, QComboBox, QButtonGroup,
        QInputDialog, QListWidget, QListWidgetItem, QMessageBox
    )
    from PySide6.QtCore import Qt, QTimer, QRectF, QEvent, QObject, Signal
    from PySide6.QtGui import (
        QPainter, QPen, QColor, QPalette, QFontMetrics, QFontDatabase,
        QIntValidator
    )
    HAVE_QT = True
except ImportError:
    # Inside Resolve's Python: only the launcher below runs. The stub lets
    # the widget classes still be defined without PySide6.
    HAVE_QT = False
    QWidget = QObject = object
    Signal = lambda *types: None

# ── Resolve API helpers ───────────────────────────────────────────────────────

RESOLVE_MODULES_DIR = (
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
    "Developer/Scripting/Modules"
)


def inside_resolve():
    """True when run from Resolve's Scripts menu or console, where Resolve
    injects `resolve` / `bmd` as globals."""
    return "resolve" in globals() or "bmd" in globals()


def get_resolve():
    """Return the Resolve app object, or None.
    From a standalone Python this connects through DaVinciResolveScript,
    which needs Preferences > System > General > External scripting = Local."""
    if "resolve" in globals():
        return globals()["resolve"]
    try:
        if "bmd" in globals():
            return globals()["bmd"].scriptapp("Resolve")
        if RESOLVE_MODULES_DIR not in sys.path:
            sys.path.append(RESOLVE_MODULES_DIR)
        import DaVinciResolveScript as dvr
        return dvr.scriptapp("Resolve")
    except Exception:
        return None


def to_px(val):
    """Convert a Resolve setting value to an int > 0, or None."""
    try:
        n = int(float(val))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def get_timeline_size():
    """Read the current timeline resolution from Resolve.
    Returns (width, height) as ints, or (None, None) if unavailable.
    Falls back to the project resolution when no timeline is open.
    Timeline.GetSettings() already returns the project values unless the
    timeline uses custom settings."""
    try:
        r = get_resolve()
        project = r.GetProjectManager().GetCurrentProject() if r else None
        if not project:
            return None, None
        source = project.GetCurrentTimeline() or project
        keys = ("timelineResolutionWidth", "timelineResolutionHeight")
        try:
            settings = source.GetSettings() or {}
            w, h = (settings.get(k) for k in keys)
        except AttributeError:
            # Resolve 20 and earlier only have GetSetting(key)
            w, h = (source.GetSetting(k) for k in keys)
        w, h = to_px(w), to_px(h)
        return (w, h) if w and h else (None, None)
    except Exception:
        return None, None


# ── launching from Resolve ────────────────────────────────────────────────────

PYTHON_CANDIDATES = [
    "/usr/local/bin/python3",
    "/opt/homebrew/bin/python3",
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3",
    "/usr/bin/python3",
]

SCRIPT_DIRS = [
    Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility",
    Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"),
]

# Resolve 21.1+ ships its own Python. With a private copy of PySide6 it can
# run the window, so a Mac without a system Python + PySide6 still works.
RESOLVE_PYTHON = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Applications/ResolvePython"
QT_VERSION = "6.10.2"
QT_LIB_DIR = Path.home() / "Library/Application Support/GuideCalculator" / f"PySide6-{QT_VERSION}"
QT_READY_MARKER = QT_LIB_DIR / ".complete"
QT_LIB_ENV = "GUIDECALC_QT_LIB"
# A tiny app bundle whose executable is a link to Resolve's Python. macOS only
# lets bundled apps bring their window to the front, and it also gives the
# window its own name in the menu bar.
APP_BUNDLE = QT_LIB_DIR.parent / "Guide Calculator.app"
APP_EXEC = APP_BUNDLE / "Contents/MacOS/GuideCalculator"
APP_INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>Guide Calculator</string>
<key>CFBundleIdentifier</key><string>local.guidecalculator</string>
<key>CFBundleExecutable</key><string>GuideCalculator</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
"""
LOG_FILE = Path.home() / "Library/Logs/GuideCalculator.log"

# Pinned PySide6 wheels from PyPI (macOS 13+, universal2), checked by SHA-256.
QT_WHEELS = [
    ("shiboken6-6.10.2-cp39-abi3-macosx_13_0_universal2.whl",
     "https://files.pythonhosted.org/packages/fb/38/3912eb08a3b865b5fcdb4bdce8076cacc211986cee587f5cb62e637791af/shiboken6-6.10.2-cp39-abi3-macosx_13_0_universal2.whl",
     "3bd4e94e9a3c8c1fa8362fd752d399ef39265d5264e4e37bae61cdaa2a00c8c7"),
    ("pyside6-6.10.2-cp39-abi3-macosx_13_0_universal2.whl",
     "https://files.pythonhosted.org/packages/35/0f/5736889fc850794623692cb369e295a994175e51295fa52134626f486296/pyside6-6.10.2-cp39-abi3-macosx_13_0_universal2.whl",
     "4b084293caa7845d0064aaf6af258e0f7caae03a14a33537d0a552131afddaf0"),
    ("pyside6_essentials-6.10.2-cp39-abi3-macosx_13_0_universal2.whl",
     "https://files.pythonhosted.org/packages/1d/2e/5f18a77f5e0bd730bacec93a690d0ef3c96a9711d213653eacecbf241b8d/pyside6_essentials-6.10.2-cp39-abi3-macosx_13_0_universal2.whl",
     "1dee2cb9803ff135f881dadeb5c0edcef793d1ec4f8a9140a1348cecb71074e1"),
]


def find_script_path():
    """Path to this file. Resolve may not define __file__, so fall back to
    looking in the Scripts/Utility folders."""
    f = globals().get("__file__")
    if f and os.path.exists(f):
        return f
    for d in SCRIPT_DIRS:
        if (d / "GuideCalculator.py").exists():
            return str(d / "GuideCalculator.py")
    return None


def find_qt_python():
    """First system Python that can import PySide6, or None."""
    for py in PYTHON_CANDIDATES:
        if not os.path.exists(py):
            continue
        try:
            ok = subprocess.run([py, "-c", "import PySide6"],
                                capture_output=True, timeout=15).returncode == 0
        except Exception:
            ok = False
        if ok:
            return py
    return None


def log(msg):
    """Print (shows in Resolve's console when run there) and append to
    ~/Library/Logs/GuideCalculator.log, since the window's process has no
    terminal."""
    print(f"GuideCalculator: {msg}")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def notify(msg):
    """macOS notification, best effort."""
    safe = msg.replace("\\", "").replace('"', "'")
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification "{safe}" with title "Guide Calculator"'],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def child_env(extra=None):
    """Environment for the window process, minus anything from Resolve's
    process that could point Python or Qt at Resolve's own copies."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DYLD_", "QT_PLUGIN", "QT_QPA_PLATFORM_PLUGIN"))
           and k not in ("PYTHONHOME", "PYTHONPATH")}
    env.update(extra or {})
    return env


def ensure_app_bundle():
    """Create or repair the Guide Calculator.app wrapper. Returns its
    executable, or Resolve's Python itself if the wrapper can't be made."""
    try:
        if not (APP_EXEC.is_symlink() and os.readlink(APP_EXEC) == RESOLVE_PYTHON):
            APP_EXEC.parent.mkdir(parents=True, exist_ok=True)
            if APP_EXEC.is_symlink() or APP_EXEC.exists():
                APP_EXEC.unlink()
            APP_EXEC.symlink_to(RESOLVE_PYTHON)
            (APP_BUNDLE / "Contents/Info.plist").write_text(APP_INFO_PLIST)
        return str(APP_EXEC)
    except OSError as e:
        log(f"couldn't create the app wrapper ({e}); the window may open behind Resolve")
        return RESOLVE_PYTHON


def launch_window_process():
    """Open the calculator window in a separate process, passing the timeline
    resolution read here inside Resolve. Uses a system Python with PySide6 if
    there is one, otherwise Resolve's own Python with a private PySide6,
    downloading that the first time."""
    script = find_script_path()
    if not script:
        log("couldn't find GuideCalculator.py in Resolve's Scripts folder.")
        return False
    args = [script]
    w, h = get_timeline_size()
    if w and h:
        args += ["--width", str(w), "--height", str(h)]

    py = find_qt_python()
    if py:
        cmd, env = [py] + args, child_env()
    elif os.path.exists(RESOLVE_PYTHON):
        env = child_env({QT_LIB_ENV: str(QT_LIB_DIR)})
        cmd = [ensure_app_bundle()] + args
        if not QT_READY_MARKER.exists():
            cmd.append("--install-qt")
    else:
        log("needs Python 3 with PySide6 (pip3 install PySide6), or "
            "DaVinci Resolve 21.1 or later.")
        return False
    subprocess.Popen(cmd, env=env, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def install_qt():
    """Download the pinned PySide6 wheels with curl, check their SHA-256,
    and unpack them into QT_LIB_DIR. Returns True when it's ready."""
    import hashlib
    import shutil
    import tempfile
    import zipfile

    if QT_READY_MARKER.exists():
        return True
    QT_LIB_DIR.parent.mkdir(parents=True, exist_ok=True)
    lock = QT_LIB_DIR.parent / f".installing-{QT_VERSION}"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < 15 * 60:
            notify("Still setting up. The window opens when it's done.")
            return False
        lock.unlink()  # left over from an interrupted run
        return install_qt()

    try:
        notify("First run: downloading PySide6 (about 110 MB). "
               "The window opens when it's done.")
        log(f"downloading PySide6 {QT_VERSION} into {QT_LIB_DIR}")
        partial = QT_LIB_DIR.with_name(QT_LIB_DIR.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        with tempfile.TemporaryDirectory() as tmp:
            for name, url, sha in QT_WHEELS:
                dest = os.path.join(tmp, name)
                r = subprocess.run(["curl", "-fsSL", "--retry", "2", "-o", dest, url],
                                   capture_output=True, text=True, timeout=1800)
                if r.returncode != 0:
                    raise RuntimeError(f"download failed for {name}: {r.stderr.strip()}")
                h = hashlib.sha256()
                with open(dest, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                if h.hexdigest() != sha:
                    raise RuntimeError(f"{name} didn't match its published checksum")
                zipfile.ZipFile(dest).extractall(partial)
        shutil.rmtree(QT_LIB_DIR, ignore_errors=True)
        partial.rename(QT_LIB_DIR)
        QT_READY_MARKER.write_text(QT_VERSION)
        log("PySide6 ready")
        return True
    except Exception as e:
        log(f"setup failed: {e}")
        notify("Setup failed. Check your internet connection and run it again.")
        return False
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def install_qt_and_relaunch(argv):
    """Runs in Resolve's Python on first launch: install PySide6, then
    replace this process with the window."""
    if not install_qt():
        return
    notify("Ready. Opening Guide Calculator.")
    args = [a for a in argv[1:] if a != "--install-qt"]
    exe = ensure_app_bundle()
    os.execve(exe, [exe, find_script_path()] + args,
              child_env({QT_LIB_ENV: str(QT_LIB_DIR)}))


def parse_size_args(argv):
    """Read --width/--height passed by launch_window_process()."""
    vals = {}
    for flag in ("--width", "--height"):
        if flag in argv:
            i = argv.index(flag)
            vals[flag] = to_px(argv[i + 1]) if i + 1 < len(argv) else None
    return vals.get("--width"), vals.get("--height")


# ── sending guides to Resolve ─────────────────────────────────────────────────
# The scripting API has no access to viewer guides, but Resolve's View > Guides
# menu has Add Guide… and Clear All Guides. These helpers drive that menu and
# the Add Guide dialog through macOS accessibility (System Events) plus one
# real mouse click per guide, which needs Accessibility permission in
# System Settings > Privacy & Security.

ACCESSIBILITY_SETTINGS_URL = (
    "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
)

APPLESCRIPT_HELPERS = r'''
on guidesMenu()
	tell application "System Events" to tell process "DaVinci Resolve"
		return menu 1 of menu item "Guides" of menu 1 of menu bar item "View" of menu bar 1
	end tell
end guidesMenu

on clickGuidesItem(prefix)
	tell application "System Events"
		set mi to first menu item of my guidesMenu() whose name starts with prefix
		if not (enabled of mi) then error "View > Guides is unavailable. Show a timeline in Resolve's viewer first."
		click mi
	end tell
end clickGuidesItem

on waitForDialog(wantOpen)
	-- each exists check is ~40ms, so poll without extra delays
	tell application "System Events" to tell process "DaVinci Resolve"
		repeat 150 times
			if (exists window "Add Guide") is wantOpen then return
		end repeat
	end tell
	if wantOpen then error "Resolve's Add Guide dialog didn't open."
	error "Resolve didn't accept the guide."
end waitForDialog
'''

AS_ACTIVATE = '''
tell application "DaVinci Resolve" to activate
tell application "System Events" to tell process "DaVinci Resolve"
	repeat 50 times
		if frontmost then exit repeat
		delay 0.02
	end repeat
end tell
'''

AS_CLEAR = '''
my clickGuidesItem("__CLEAR_ITEM__")
'''

# View > Guides clear commands, by which guides the calculator is making
CLEAR_ITEMS = {
    "both": "Clear All Guides",
    "x": "Clear All Vertical Guides",
    "y": "Clear All Horizontal Guides",
}

AS_OPEN_FIRST = '''
my clickGuidesItem("Add Guide")
my waitForDialog(true)
tell application "System Events" to tell process "DaVinci Resolve"
	set w to window "Add Guide"
	if value of checkbox "Pixels" of w is not 1 then click checkbox "Pixels" of w
end tell
'''

# Finds Resolve's Position box (a click-to-edit number exposed as static
# text) and returns "index,x,y,wx:wy": its static-text index, its centre in
# screen points, and the dialog's position (to notice if the dialog moves).
# The box ignores accessibility clicks and Tab, so real_click() opens it.
AS_LOCATE = '''
tell application "System Events" to tell process "DaVinci Resolve"
	set w to window "Add Guide"
	set vals to value of every static text of w
end tell
set idx to 0
repeat with i from 1 to count of vals
	try
		set n to ((item i of vals) as text) as number
		set idx to i
		exit repeat
	end try
end repeat
if idx is 0 then error "Couldn't find the Position field in Resolve's Add Guide dialog."
tell application "System Events" to tell process "DaVinci Resolve"
	set {fx, fy} to position of static text idx of w
	set {fw, fh} to size of static text idx of w
	set {wx, wy} to position of w
end tell
return (idx as text) & "," & ((round (fx + fw / 2)) as text) & "," & ((round (fy + fh / 2)) as text) & "," & (wx as text) & ":" & (wy as text)
'''

# Types into the (now editing) Position box and reads the value back.
# If a click landed on another app, Resolve isn't frontmost: type nothing.
AS_TYPE = '''
tell application "System Events" to tell process "DaVinci Resolve"
	if not frontmost then return "__NOT_FRONT__"
	keystroke "a" using command down
	keystroke "__POS__"
	key code 48 -- tab commits the value
	repeat 10 times
		-- by index each time: a stored reference is by name, which the edit changes
		set v to value of static text __IDX__ of window "Add Guide" as text
		if v is "__POS__" then exit repeat
	end repeat
end tell
return v
'''

# Sets the orientation, presses Add, waits for the dialog to close, then
# (unless this was the last guide) opens Add Guide… for the next one.
AS_FINISH = '''
tell application "System Events" to tell process "DaVinci Resolve"
	set w to window "Add Guide"
	if value of radio button "__ORIENT__" of w is not 1 then click radio button "__ORIENT__" of w
	click button "Add" of w
end tell
my waitForDialog(false)
'''

AS_OPEN_NEXT = '''
tell application "System Events" to click (first menu item of my guidesMenu() whose name starts with "Add Guide")
my waitForDialog(true)
tell application "System Events" to tell process "DaVinci Resolve" to set {wx, wy} to position of window "Add Guide"
return (wx as text) & ":" & (wy as text)
'''

AS_CANCEL = '''
tell application "System Events" to tell process "DaVinci Resolve"
	if exists window "Add Guide" then click button "Cancel" of window "Add Guide"
end tell
'''


class AutomationError(Exception):
    def __init__(self, msg, needs_permission=False):
        super().__init__(msg)
        self.needs_permission = needs_permission


def run_applescript(body, timeout=30):
    """Run APPLESCRIPT_HELPERS + body via osascript and return its result.
    Raises AutomationError with a readable message; needs_permission is set
    when macOS blocked accessibility."""
    try:
        r = subprocess.run(["osascript", "-"], input=APPLESCRIPT_HELPERS + "\n" + body,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AutomationError("Resolve didn't respond in time.")
    except OSError as e:
        raise AutomationError(f"Couldn't run osascript: {e}")
    if r.returncode == 0:
        return r.stdout.strip()
    err = r.stderr.strip()
    # -1719 alone also means "no such element", so match macOS's wording
    if any(s in err for s in ("-25211", "assistive access", "not allowed to send keystrokes")):
        raise AutomationError("macOS needs to allow this app to control Resolve.",
                              needs_permission=True)
    if "-1728" in err and "DaVinci Resolve" in err:
        raise AutomationError("Couldn't find Resolve's View > Guides menu. Is Resolve open?")
    m = re.search(r"execution error: (.*?)(?: \(-?\d+\))?$", err)
    raise AutomationError(m.group(1) if m else (err or "AppleScript failed."))


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_AS = None


def _app_services():
    """ApplicationServices via ctypes, so no extra packages are needed."""
    global _AS
    if _AS is None:
        lib = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        lib.CGEventCreate.restype = ctypes.c_void_p
        lib.CGEventCreate.argtypes = [ctypes.c_void_p]
        lib.CGEventGetLocation.restype = _CGPoint
        lib.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        lib.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        lib.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                                _CGPoint, ctypes.c_uint32]
        lib.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        lib.CGWarpMouseCursorPosition.argtypes = [_CGPoint]
        lib.CFRelease.argtypes = [ctypes.c_void_p]
        _AS = lib
    return _AS


def accessibility_trusted():
    try:
        return bool(_app_services().AXIsProcessTrusted())
    except Exception:
        return True  # can't tell; let osascript report any problem


def real_click(x, y):
    """Post a genuine left click at screen point (x, y), then put the cursor
    back where it was."""
    cg = _app_services()
    probe = cg.CGEventCreate(None)
    home = cg.CGEventGetLocation(probe)
    cg.CFRelease(probe)
    pt = _CGPoint(x, y)
    # mouse moved, left down, left up (kCGHIDEventTap = 0, left button = 0)
    for event_type in (5, 1, 2):
        ev = cg.CGEventCreateMouseEvent(None, event_type, pt, 0)
        cg.CGEventPost(0, ev)
        cg.CFRelease(ev)
        time.sleep(0.015)
    cg.CGWarpMouseCursorPosition(home)
    time.sleep(0.03)


def _same_number(text, value):
    try:
        return abs(float(text.replace("\u2212", "-").replace(",", ".")) - value) < 0.51
    except ValueError:
        return False


def _parse_location(text):
    """AS_LOCATE result -> (index, x, y, dialog position)."""
    idx, x, y, where = text.split(",")
    return int(idx), float(x), float(y), where


def clear_resolve_guides(axes="both"):
    """Clear Resolve's guides: "both", or only vertical ("x") / horizontal ("y")."""
    run_applescript(AS_ACTIVATE + AS_CLEAR.replace("__CLEAR_ITEM__", CLEAR_ITEMS[axes]))


def send_guides_to_resolve(gx, gy, clear_first=True, clear_axes="both", attempts=3):
    """Clear (optionally, only the clear_axes guides), then add every
    vertical (X) and horizontal (Y) guide through View > Guides > Add Guide…
    (pixels, +Y up).

    Per guide: one real click on the Position box, one script to type and
    read the value back, one to press Add and open the dialog for the next
    guide. The box's location is reused while the dialog stays put and
    looked up again whenever it opens somewhere else. Nothing is typed
    unless Resolve is frontmost, a value that doesn't take is retried, and
    the dialog is cancelled rather than adding a guide in the wrong place."""
    if not accessibility_trusted():
        raise AutomationError("macOS needs to allow this app to control Resolve.",
                              needs_permission=True)
    guides = [(v, "Vertical") for v in gx] + [(v, "Horizontal") for v in gy]
    clear = AS_CLEAR.replace("__CLEAR_ITEM__", CLEAR_ITEMS[clear_axes])
    start = AS_ACTIVATE + (clear if clear_first else "")
    if not guides:
        run_applescript(start)
        return
    try:
        idx, x, y, where = _parse_location(run_applescript(start + AS_OPEN_FIRST + AS_LOCATE))
        for n, (value, orientation) in enumerate(guides):
            text = f"{value:g}"
            for attempt in range(attempts):
                real_click(x, y)
                typed = run_applescript(AS_TYPE.replace("__IDX__", str(idx))
                                        .replace("__POS__", text))
                if _same_number(typed, value):
                    break
                if typed == "__NOT_FRONT__":
                    # the click hit another app; bring Resolve back first
                    run_applescript(AS_ACTIVATE)
                idx, x, y, where = _parse_location(run_applescript(AS_LOCATE))
            else:
                raise AutomationError(
                    f"Resolve's Position box didn't take {text} (it shows {typed}). "
                    "Cancelled so no guide lands in the wrong place.")
            is_last = n == len(guides) - 1
            reopened_at = run_applescript(AS_FINISH.replace("__ORIENT__", orientation)
                                          + ("" if is_last else AS_OPEN_NEXT))
            if not is_last and reopened_at != where:
                idx, x, y, where = _parse_location(run_applescript(AS_LOCATE))
    except AutomationError:
        try:
            run_applescript(AS_CANCEL)  # don't leave the dialog open
        except AutomationError:
            pass
        raise


# ── guide math ────────────────────────────────────────────────────────────────

PHI = (1 + math.sqrt(5)) / 2


def round2(v):
    """Round half up to 2 decimal places."""
    return math.floor(v * 100 + 0.5) / 100


def calc_guides(total, divisions):
    """Return pixel offsets from the frame centre for (divisions - 1)
    evenly-spaced guide positions across `total` pixels."""
    half = total / 2
    return [round2((total / divisions) * i - half) for i in range(1, divisions)]


def calc_rule_of_thirds(total):
    """Return guide positions for Rule of Thirds (divides into 3 equal parts)."""
    return calc_guides(total, 3)


def calc_golden_ratio(total):
    """Return guide positions for the Golden Ratio grid: 1/φ² and 1/φ of the
    frame (≈ 38.2% and 61.8%), symmetric about the centre."""
    half = total / 2
    return sorted(round2(total / PHI ** i - half) for i in (1, 2))


def calc_fibonacci(total):
    """Return guide positions at 3/8 and 5/8 of the frame. Consecutive
    Fibonacci numbers (3, 5, 8) give the whole-number version of the
    golden ratio grid."""
    half = total / 2
    return [round2(total * f / 8 - half) for f in (3, 5)]


def calc_safe_zones(width, height):
    """Return safe areas as fractions of the frame (EBU R 95 / SMPTE ST 2046-1
    for 16:9: action safe 93%, title safe 90%)."""
    return {
        "action_safe": 0.93,
        "title_safe": 0.9,
    }


def format_guides(guides, format_type="compact"):
    """Format guides in various formats."""
    if format_type == "compact":
        parts = []
        for v in guides:
            s = f"+{v:g}" if v > 0 else f"{v:g}"
            parts.append(s)
        return ",  ".join(parts)
    elif format_type == "json":
        return json.dumps([round(g, 2) for g in guides])
    elif format_type == "csv":
        return ",".join([str(round(g, 2)) for g in guides])
    elif format_type == "resolve":
        # Resolve-friendly format with frame info
        lines = []
        for i, v in enumerate(guides, 1):
            lines.append(f"Guide {i}: {v:+.2f}px")
        return "\n".join(lines)
    return format_guides(guides, "compact")


# ── clipboard ─────────────────────────────────────────────────────────────────

def copy_to_clipboard(text):
    """Write text to the system clipboard."""
    try:
        QApplication.clipboard().setText(text)
        return True
    except Exception:
        return False


# ── theme ─────────────────────────────────────────────────────────────────────
# Colours sampled from Resolve 21's viewer so the preview reads like the real
# thing: black frame on a dark canvas, rulers top/left, 1px yellow guides,
# white safe-area outlines.

C_CHROME    = "#212121"
C_PANEL     = "#262626"
C_FIELD     = "#1a1a1a"
C_BORDER    = "#333333"
C_CANVAS    = "#1a1a1a"
C_RULER     = "#1b1b1b"
C_RULER_TXT = "#707070"
C_FRAME     = "#000000"
C_GUIDE     = "#d4cb74"
C_GUIDE_HI  = "#fff4a8"
C_SAFE      = "#e6e6e6"
C_TEXT      = "#d2d2d2"
C_TEXT_DIM  = "#8c8c8c"
C_ERROR     = "#e0675f"
C_OK        = "#8fc79a"

STYLESHEET = f"""
QWidget {{ color: {C_TEXT}; font-size: 12px; }}
QWidget#root, QWidget#sidebar {{ background: {C_PANEL}; }}
QWidget#topbar {{ background: {C_CHROME}; border-bottom: 1px solid {C_BORDER}; }}
QWidget#sidebar {{ border-left: 1px solid {C_BORDER}; }}
QLabel#section {{ color: {C_TEXT_DIM}; font-size: 10px; font-weight: 600;
                  letter-spacing: 1px; padding-top: 6px; }}
QLabel#dim {{ color: {C_TEXT_DIM}; }}
QLineEdit, QSpinBox, QComboBox {{
    background: {C_FIELD}; border: 1px solid {C_BORDER}; border-radius: 4px;
    padding: 3px 6px; min-height: 18px; selection-background-color: #4a4a4a;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: #5a5a5a; }}
QSpinBox:disabled {{ color: #555555; }}
QComboBox QAbstractItemView {{ background: {C_PANEL}; border: 1px solid {C_BORDER};
                               selection-background-color: #3d3d3d; }}
QPushButton {{
    background: #333333; border: 1px solid #3c3c3c; border-radius: 4px;
    padding: 4px 10px; min-height: 18px;
}}
QPushButton:hover {{ background: #3b3b3b; }}
QPushButton:pressed {{ background: #2c2c2c; }}
QPushButton:disabled {{ color: #5c5c5c; background: #2c2c2c; }}
QPushButton#seg {{ border-radius: 0; padding: 5px 4px; }}
QPushButton#seg:checked {{ background: #4a4a4a; border-color: #5a5a5a; color: #ffffff; }}
QPushButton#small {{ padding: 1px 8px; font-size: 11px; min-height: 14px; }}
QPushButton#primary {{ background: #4a4a4a; border-color: #5a5a5a; color: #ffffff; }}
QPushButton#primary:hover {{ background: #555555; }}
QPushButton#primary:disabled {{ color: #5c5c5c; background: #2c2c2c; border-color: #3c3c3c; }}
QListWidget {{ background: {C_FIELD}; border: 1px solid {C_BORDER}; border-radius: 4px;
               outline: none; padding: 2px; }}
QListWidget::item {{ padding: 3px 6px; border-radius: 3px; }}
QListWidget::item:hover {{ background: #2e2e2e; }}
QListWidget::item:selected {{ background: #3a3a3a; color: {C_GUIDE_HI}; }}
QCheckBox {{ spacing: 6px; }}
QToolTip {{ background: {C_PANEL}; color: {C_TEXT}; border: 1px solid {C_BORDER}; }}
"""


def apply_dark_theme(app):
    """Resolve is always dark, so the calculator is too, whatever macOS uses."""
    app.setStyle("Fusion")
    pal = QPalette()
    for role, col in [
        (QPalette.Window, C_PANEL), (QPalette.Base, C_FIELD),
        (QPalette.AlternateBase, C_PANEL), (QPalette.Button, "#333333"),
        (QPalette.Text, C_TEXT), (QPalette.WindowText, C_TEXT),
        (QPalette.ButtonText, C_TEXT), (QPalette.Highlight, "#4a4a4a"),
        (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, C_PANEL),
        (QPalette.ToolTipText, C_TEXT), (QPalette.PlaceholderText, "#5e5e5e"),
    ]:
        pal.setColor(role, QColor(col))
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)


def fmt_val(v):
    """+320 / -320 / 0, trimming trailing zeros."""
    return "0" if v == 0 else f"{v:+g}"


def aspect_label(w, h):
    """16:9 for common ratios, 2.39:1 style otherwise."""
    g = math.gcd(w, h)
    a, b = w // g, h // g
    return f"{a}:{b}" if a <= 32 and b <= 32 else f"{w / h:.2f}:1"


def mono_font(size=11):
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    f.setPointSize(size)
    return f


# ── viewer ────────────────────────────────────────────────────────────────────

class GuideViewer(QWidget):
    """Resolve-style viewer: rulers with 0 at the centre and +Y up, the frame
    in black, guides as 1px lines across the whole viewer. Hover a guide to
    see its value; click to copy it."""

    RULER = 22
    PAD = 28
    HIT = 5  # px either side of a guide that counts as hovering it

    guideClicked = Signal(str, float)   # axis ("x"/"y"), value
    guideHovered = Signal(object)       # (axis, value) or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.frame_w = self.frame_h = None
        self.guides_x, self.guides_y = [], []
        self.safe = {"action_safe": 0.93, "title_safe": 0.9}
        self.show_safe = {"action_safe": True, "title_safe": True}
        self.hover = None       # guide under the mouse
        self.highlight = None   # guide hovered in the sidebar lists
        self.mouse_pos = None
        self.setMouseTracking(True)
        self.setMinimumSize(420, 300)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ── data ──

    def set_data(self, w, h, gx, gy):
        self.frame_w, self.frame_h = w, h
        self.guides_x, self.guides_y = list(gx), list(gy)
        self.hover = None
        self.update()

    def set_safe_visible(self, action, title):
        self.show_safe = {"action_safe": action, "title_safe": title}
        self.update()

    def set_highlight(self, guide):
        self.highlight = guide
        self.update()

    # ── geometry ──

    def _view_rect(self):
        r = self.RULER
        return QRectF(r, r, self.width() - r, self.height() - r)

    def _frame_rect(self):
        area = self._view_rect().adjusted(self.PAD, self.PAD, -self.PAD, -self.PAD)
        if not self.frame_w or area.width() <= 0 or area.height() <= 0:
            return None, 0
        scale = min(area.width() / self.frame_w, area.height() / self.frame_h)
        fw, fh = self.frame_w * scale, self.frame_h * scale
        rect = QRectF(area.center().x() - fw / 2, area.center().y() - fh / 2, fw, fh)
        # snap to whole pixels so 1px lines stay crisp
        return QRectF(round(rect.x()), round(rect.y()), round(fw), round(fh)), scale

    def _sx(self, v, rect, scale):
        return round(rect.center().x() + v * scale)

    def _sy(self, v, rect, scale):
        return round(rect.center().y() - v * scale)  # Resolve: +Y is up

    # ── painting ──

    def paintEvent(self, event):
        p = QPainter(self)
        view = self._view_rect()
        p.fillRect(self.rect(), QColor(C_CANVAS))
        rect, scale = self._frame_rect()

        if rect is None:
            self._draw_rulers(p, None, 0, [])
            p.setPen(QColor(C_TEXT_DIM))
            p.drawText(view, Qt.AlignCenter, "Enter a resolution to preview guides")
            p.end()
            return

        p.fillRect(rect, QColor(C_FRAME))

        # safe areas: white 1px outlines, as in Resolve's viewer
        p.setPen(QPen(QColor(C_SAFE), 1))
        p.setBrush(Qt.NoBrush)
        for key in ("action_safe", "title_safe"):
            if self.show_safe[key]:
                pct = self.safe[key]
                w, h = round(rect.width() * pct), round(rect.height() * pct)
                p.drawRect(QRectF(rect.center().x() - w / 2, rect.center().y() - h / 2,
                                  w - 1, h - 1).toRect())

        # centre mark
        cx, cy = self._sx(0, rect, scale), self._sy(0, rect, scale)
        p.setPen(QPen(QColor("#6a6a6a"), 1))
        p.drawLine(cx - 5, cy, cx + 5, cy)
        p.drawLine(cx, cy - 5, cx, cy + 5)

        # guides across the whole viewer
        active = self.hover or self.highlight
        for axis, values in (("x", self.guides_x), ("y", self.guides_y)):
            for v in values:
                on = active == (axis, v)
                p.setPen(QPen(QColor(C_GUIDE_HI if on else C_GUIDE), 2 if on else 1))
                if axis == "x":
                    sx = self._sx(v, rect, scale)
                    p.drawLine(sx, int(view.top()), sx, self.height())
                else:
                    sy = self._sy(v, rect, scale)
                    p.drawLine(int(view.left()), sy, self.width(), sy)

        self._draw_rulers(p, rect, scale, active)

        # caption
        p.setPen(QColor(C_TEXT_DIM))
        p.setFont(self.font())
        caption = f"{self.frame_w} × {self.frame_h}   {aspect_label(self.frame_w, self.frame_h)}"
        p.drawText(QRectF(view.left() + 10, view.bottom() - 22, 400, 18),
                   Qt.AlignLeft | Qt.AlignVCenter, caption)

        if self.hover and self.mouse_pos:
            self._draw_hover_tag(p, view)
        p.end()

    def _tick_step(self, scale):
        """Smallest 1/2/5 × 10ⁿ step that leaves ≥ 64px between labels."""
        if scale <= 0:
            return 100
        n = 10 ** math.floor(math.log10(64 / scale))
        for m in (1, 2, 5, 10):
            if m * n * scale >= 64:
                return m * n
        return 10 * n

    def _draw_rulers(self, p, rect, scale, active):
        R = self.RULER
        W, H = self.width(), self.height()
        p.fillRect(QRectF(R, 0, W - R, R), QColor(C_RULER))
        p.fillRect(QRectF(0, R, R, H - R), QColor(C_RULER))
        p.fillRect(QRectF(0, 0, R, R), QColor(C_CHROME))
        p.setPen(QColor("#2c2c2c"))
        p.drawLine(R, R - 1, W, R - 1)
        p.drawLine(R - 1, R, R - 1, H)
        if rect is None:
            return

        font = mono_font(9)
        p.setFont(font)
        fm = QFontMetrics(font)
        step = self._tick_step(scale)
        minor = step / 5

        # guide value tags go first so tick labels can make way for them
        tags_x, tags_y = [], []
        for v in self.guides_x:
            sx = self._sx(v, rect, scale)
            tw = fm.horizontalAdvance(fmt_val(v)) + 8
            r = QRectF(sx - tw / 2, 2, tw, R - 5)
            if sx > R and not any(r.intersects(t) for _, t in tags_x):
                tags_x.append((v, r))
        for v in self.guides_y:
            sy = self._sy(v, rect, scale)
            tw = fm.horizontalAdvance(fmt_val(v)) + 8
            r = QRectF(2, sy - tw / 2, R - 5, tw)
            if sy > R and not any(r.intersects(t) for _, t in tags_y):
                tags_y.append((v, r))

        # horizontal ruler ticks
        cx = rect.center().x()
        lo = math.floor(-(cx - R) / scale / minor) * minor
        hi = (W - cx) / scale
        v = lo
        while v <= hi:
            sx = round(cx + v * scale)
            major = abs(v / step - round(v / step)) < 1e-6
            if sx > R:
                p.setPen(QColor("#4a4a4a" if major else "#333333"))
                p.drawLine(sx, R - (9 if major else 4), sx, R - 1)
                if major:
                    lbl = f"{round(v):d}"
                    lr = QRectF(sx + 3, 1, fm.horizontalAdvance(lbl) + 2, 13)
                    if lr.right() < W and not any(lr.intersects(t) for _, t in tags_x):
                        p.setPen(QColor(C_RULER_TXT))
                        p.drawText(lr, Qt.AlignLeft | Qt.AlignVCenter, lbl)
            v += minor

        # vertical ruler ticks (+ up)
        cy = rect.center().y()
        hi = (cy - R) / scale
        lo = -(H - cy) / scale
        v = math.floor(lo / minor) * minor
        while v <= hi:
            sy = round(cy - v * scale)
            major = abs(v / step - round(v / step)) < 1e-6
            if sy > R:
                p.setPen(QColor("#4a4a4a" if major else "#333333"))
                p.drawLine(R - (9 if major else 4), sy, R - 1, sy)
                if major:
                    lbl = f"{round(v):d}"
                    lw = fm.horizontalAdvance(lbl) + 2
                    lr = QRectF(1, sy - 3 - lw, 13, lw)
                    if lr.top() > R and not any(lr.intersects(t) for _, t in tags_y):
                        p.save()
                        p.translate(1, sy - 3)
                        p.rotate(-90)
                        p.setPen(QColor(C_RULER_TXT))
                        p.drawText(QRectF(0, 0, lw, 13), Qt.AlignLeft | Qt.AlignVCenter, lbl)
                        p.restore()
            v += minor

        # guide tags on top
        for axis, tags in (("x", tags_x), ("y", tags_y)):
            for val, r in tags:
                on = active == (axis, val)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(C_GUIDE_HI if on else C_GUIDE))
                p.setRenderHint(QPainter.Antialiasing, True)
                p.drawRoundedRect(r, 2, 2)
                p.setRenderHint(QPainter.Antialiasing, False)
                p.setPen(QColor("#111111"))
                if axis == "x":
                    p.drawText(r, Qt.AlignCenter, fmt_val(val))
                else:
                    p.save()
                    p.translate(r.left(), r.bottom())
                    p.rotate(-90)
                    p.drawText(QRectF(0, 0, r.height(), r.width()), Qt.AlignCenter, fmt_val(val))
                    p.restore()
        p.setBrush(Qt.NoBrush)

    def _draw_hover_tag(self, p, view):
        axis, v = self.hover
        text = f"{'X' if axis == 'x' else 'Y'}  {fmt_val(v)}    click to copy"
        font = self.font()
        p.setFont(font)
        fm = QFontMetrics(font)
        w, h = fm.horizontalAdvance(text) + 16, 22
        x = min(self.mouse_pos.x() + 14, view.right() - w - 4)
        y = min(self.mouse_pos.y() + 16, view.bottom() - h - 4)
        r = QRectF(x, y, w, h)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor("#444444"), 1))
        p.setBrush(QColor(C_PANEL))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(QColor(C_TEXT))
        p.drawText(r, Qt.AlignCenter, text)

    # ── interaction ──

    def _guide_at(self, pos):
        rect, scale = self._frame_rect()
        if rect is None or not self._view_rect().contains(pos):
            return None
        best, best_d = None, self.HIT + 1
        for v in self.guides_x:
            d = abs(pos.x() - self._sx(v, rect, scale))
            if d < best_d:
                best, best_d = ("x", v), d
        for v in self.guides_y:
            d = abs(pos.y() - self._sy(v, rect, scale))
            if d < best_d:
                best, best_d = ("y", v), d
        return best

    def mouseMoveEvent(self, event):
        self.mouse_pos = event.position()
        hit = self._guide_at(self.mouse_pos)
        if hit != self.hover:
            self.hover = hit
            self.setCursor(Qt.PointingHandCursor if hit else Qt.ArrowCursor)
            self.guideHovered.emit(hit)
        self.update()

    def leaveEvent(self, event):
        self.hover = None
        self.mouse_pos = None
        self.guideHovered.emit(None)
        self.update()

    def mousePressEvent(self, event):
        if self.hover and event.button() == Qt.LeftButton:
            self.guideClicked.emit(*self.hover)


# ── config management ─────────────────────────────────────────────────────────

CONFIG_DIR = None
CONFIG_FILE = None
CONFIG_AVAILABLE = False

try:
    CONFIG_DIR = Path.home() / ".guidecalc"
    CONFIG_FILE = CONFIG_DIR / "presets.json"
    CONFIG_AVAILABLE = True
except Exception:
    CONFIG_AVAILABLE = False

class PresetManager:
    def __init__(self):
        self.config_available = CONFIG_AVAILABLE
        self.presets = self._load_presets()

    def _load_presets(self):
        if not self.config_available:
            return self._default_presets()

        try:
            if CONFIG_DIR and CONFIG_FILE and CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r") as f:
                    loaded = json.load(f)
                    return loaded if loaded else self._default_presets()
        except (IOError, json.JSONDecodeError, OSError, PermissionError, TypeError):
            pass

        return self._default_presets()

    def _default_presets(self):
        return {
            "1080p (Full HD)": {"w": 1920, "h": 1080, "aspect": "16:9"},
            "4K (UHD)": {"w": 3840, "h": 2160, "aspect": "16:9"},
            "DCI 4K": {"w": 4096, "h": 2160, "aspect": "17:9"},
            "720p (HD)": {"w": 1280, "h": 720, "aspect": "16:9"},
            "Instagram (Portrait)": {"w": 1080, "h": 1350, "aspect": "4:5"},
            "Instagram (Square)": {"w": 1080, "h": 1080, "aspect": "1:1"},
            "YouTube (16:9)": {"w": 1920, "h": 1080, "aspect": "16:9"},
            "6K": {"w": 6144, "h": 3240, "aspect": "17:9"},
            "Scope (2.39:1)": {"w": 3840, "h": 1608, "aspect": "2.39:1"},
        }

    def save(self, presets):
        if not self.config_available or not CONFIG_DIR or not CONFIG_FILE:
            return False

        try:
            CONFIG_DIR.mkdir(exist_ok=True, parents=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(presets, f, indent=2)
            self.presets = presets
            return True
        except (IOError, OSError, PermissionError, TypeError):
            return False

    def add_preset(self, name, w, h, aspect=""):
        self.presets[name] = {"w": w, "h": h, "aspect": aspect}
        return self.save(self.presets)

    def delete_preset(self, name):
        if name in self.presets:
            del self.presets[name]
            return self.save(self.presets)
        return False

# ── main window ───────────────────────────────────────────────────────────────

GRID_MODES = [
    ("Equal", "equal"),
    ("Thirds", "thirds"),
    ("Golden", "golden"),
    ("Fibonacci", "fibonacci"),
]

GUIDE_AXES = [
    ("Both", "both"),
    ("Vertical", "x"),
    ("Horizontal", "y"),
]

AXIS_NAMES = {"both": "", "x": "vertical ", "y": "horizontal "}

COPY_FORMATS = [
    ("Text", "compact"),
    ("JSON", "json"),
    ("CSV", "csv"),
    ("Numbered list", "resolve"),
]

SAVE_PRESET_ITEM = "Save current as preset…"


class AutomationSignals(QObject):
    """Carries results from the automation thread back to the UI thread."""
    finished = Signal(bool, str, bool)  # ok, message, needs_permission


class GuideCalculator(QWidget):

    def __init__(self, initial_size=(None, None)):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("Guide Calculator")
        self.setMinimumSize(860, 620)
        self.preset_manager = PresetManager()
        self.current_grid_mode = "equal"
        self.axes = "both"
        self._last_gx, self._last_gy = [], []
        self._last_w = self._last_h = None
        self._automation = AutomationSignals()
        self._automation.finished.connect(self._on_automation_finished)

        self._redraw_timer = QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.setInterval(120)
        self._redraw_timer.timeout.connect(self._do_calc)

        self._build_ui()

        w, h = initial_size
        if w and h:
            self._set_size(w, h)
            self._set_status(f"Loaded from timeline: {w} × {h}", ok=True)
        else:
            self._try_load_resolution()

    # ── layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.preview = GuideViewer()
        self.preview.guideClicked.connect(self._copy_single)
        self.preview.guideHovered.connect(self._on_viewer_hover)
        body.addWidget(self.preview, stretch=1)
        body.addWidget(self._build_sidebar())
        root.addLayout(body, stretch=1)

    def _build_topbar(self):
        bar = QWidget()
        bar.setObjectName("topbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(8)

        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(210)
        self.preset_combo.activated.connect(self._on_preset_activated)
        self._fill_presets()
        row.addWidget(self.preset_combo)
        row.addSpacing(6)

        validator = QIntValidator(1, 32768, self)
        self.width_edit = QLineEdit()
        self.height_edit = QLineEdit()
        for edit, hint in ((self.width_edit, "Width"), (self.height_edit, "Height")):
            edit.setPlaceholderText(hint)
            edit.setValidator(validator)
            edit.setFixedWidth(72)
            edit.setAlignment(Qt.AlignCenter)
            edit.setFont(mono_font(12))
            edit.textChanged.connect(self._on_input_changed)
        row.addWidget(self.width_edit)
        times = QLabel("×")
        times.setObjectName("dim")
        row.addWidget(times)
        row.addWidget(self.height_edit)
        row.addSpacing(6)

        self.reload_btn = QPushButton("↺  From Timeline")
        self.reload_btn.setToolTip("Read the resolution of Resolve's current timeline")
        self.reload_btn.clicked.connect(self._reload_resolution)
        row.addWidget(self.reload_btn)

        row.addStretch()
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("dim")
        row.addWidget(self.status_lbl)
        return bar

    def _section(self, text):
        lbl = QLabel(text.upper())
        lbl.setObjectName("section")
        return lbl

    def _build_sidebar(self):
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(260)
        col = QVBoxLayout(side)
        col.setContentsMargins(14, 10, 14, 14)
        col.setSpacing(6)

        # grid mode: segmented control
        col.addWidget(self._section("Grid"))
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.grid_mode_group = QButtonGroup(self)
        self.grid_mode_group.setExclusive(True)
        for label, mode_id in GRID_MODES:
            btn = QPushButton(label)
            btn.setObjectName("seg")
            btn.setCheckable(True)
            btn.setChecked(mode_id == "equal")
            btn.toggled.connect(lambda checked, m=mode_id: checked and self._set_grid_mode(m))
            self.grid_mode_group.addButton(btn)
            seg.addWidget(btn)
        col.addLayout(seg)

        # which guides to make: both, or only vertical / horizontal
        axes_row = QHBoxLayout()
        axes_row.setSpacing(0)
        self.axes_group = QButtonGroup(self)
        self.axes_group.setExclusive(True)
        for label, axes in GUIDE_AXES:
            btn = QPushButton(label)
            btn.setObjectName("seg")
            btn.setCheckable(True)
            btn.setChecked(axes == "both")
            btn.toggled.connect(lambda checked, a=axes: checked and self._set_axes(a))
            self.axes_group.addButton(btn)
            axes_row.addWidget(btn)
        col.addLayout(axes_row)

        # Equal mode: columns set the vertical guides, rows the horizontal
        div_row = QHBoxLayout()
        div_row.setSpacing(6)
        self.cols_spin, self.rows_spin = QSpinBox(), QSpinBox()
        for label, spin, tip in (
            ("Columns", self.cols_spin, "Equal columns across the width (vertical guides)"),
            ("Rows", self.rows_spin, "Equal rows down the height (horizontal guides)"),
        ):
            lbl = QLabel(label)
            lbl.setObjectName("dim")
            lbl.setToolTip(tip)
            spin.setRange(2, 20)
            spin.setValue(3)
            spin.setFixedWidth(58)
            spin.setToolTip(tip)
            spin.valueChanged.connect(self._on_input_changed)
            div_row.addWidget(lbl)
            div_row.addWidget(spin)
            if spin is self.cols_spin:
                div_row.addStretch()
        col.addLayout(div_row)

        col.addSpacing(4)
        col.addWidget(self._section("Safe areas"))
        self.safe_action_check = QCheckBox("Action safe  93%")
        self.safe_title_check = QCheckBox("Title safe  90%")
        for cb in (self.safe_action_check, self.safe_title_check):
            cb.setChecked(True)
            cb.toggled.connect(self._update_safe_zones)
            col.addWidget(cb)

        col.addSpacing(4)
        self.list_x = self._build_guide_list(col, "Vertical guides  ·  X", "x")
        self.list_y = self._build_guide_list(col, "Horizontal guides  ·  Y", "y")

        col.addSpacing(6)
        copy_row = QHBoxLayout()
        copy_row.setSpacing(6)
        self.format_combo = QComboBox()
        for label, fmt in COPY_FORMATS:
            self.format_combo.addItem(label, fmt)
        copy_row.addWidget(self.format_combo, stretch=1)
        self.copy_all_btn = QPushButton("Copy all")
        self.copy_all_btn.setObjectName("primary")
        self.copy_all_btn.clicked.connect(self._copy_all)
        copy_row.addWidget(self.copy_all_btn)
        col.addLayout(copy_row)

        hint = QLabel("Click a guide or a value to copy it.")
        hint.setObjectName("dim")
        hint.setStyleSheet("font-size: 11px;")
        col.addWidget(hint)

        col.addSpacing(6)
        col.addWidget(self._section("Resolve viewer"))
        send_row = QHBoxLayout()
        send_row.setSpacing(6)
        self.send_btn = QPushButton("Send guides")
        self.send_btn.setObjectName("primary")
        self.send_btn.setToolTip("Add these guides to Resolve's viewer (View > Guides > Add Guide)")
        self.send_btn.clicked.connect(self._send_to_resolve)
        send_row.addWidget(self.send_btn, stretch=1)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Clear these guides in Resolve's viewer (vertical, horizontal or all)")
        self.clear_btn.clicked.connect(self._clear_resolve_guides)
        send_row.addWidget(self.clear_btn)
        col.addLayout(send_row)
        self.replace_check = QCheckBox("Replace existing guides")
        self.replace_check.setChecked(True)
        col.addWidget(self.replace_check)
        return side

    def _build_guide_list(self, col, title, axis):
        box = QWidget()
        box_col = QVBoxLayout(box)
        box_col.setContentsMargins(0, 0, 0, 0)
        box_col.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(self._section(title))
        head.addStretch()
        btn = QPushButton("Copy")
        btn.setObjectName("small")
        btn.clicked.connect(lambda checked=False, a=axis: self._copy_axis(a))
        head.addWidget(btn, alignment=Qt.AlignBottom)
        box_col.addLayout(head)

        lst = QListWidget()
        lst.setFont(mono_font(12))
        lst.setMouseTracking(True)
        lst.setMinimumHeight(60)
        lst.itemClicked.connect(lambda item, a=axis: self._copy_single(a, item.data(Qt.UserRole)))
        lst.itemEntered.connect(lambda item, a=axis: self.preview.set_highlight((a, item.data(Qt.UserRole))))
        lst.viewport().installEventFilter(self)
        box_col.addWidget(lst, stretch=1)
        col.addWidget(box, stretch=1)
        if axis == "x":
            self.copy_vert_btn, self.list_x_box = btn, box
        else:
            self.copy_horiz_btn, self.list_y_box = btn, box
        return lst

    def eventFilter(self, obj, event):
        # clear the viewer highlight when the mouse leaves a guide list
        if event.type() == QEvent.Leave:
            self.preview.set_highlight(None)
        return False

    # ── presets ───────────────────────────────────────────────────────────────

    def _fill_presets(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Custom", None)
        for name, data in self.preset_manager.presets.items():
            self.preset_combo.addItem(f"{name}   {data['w']}×{data['h']}", name)
        self.preset_combo.insertSeparator(self.preset_combo.count())
        self.preset_combo.addItem(SAVE_PRESET_ITEM, SAVE_PRESET_ITEM)
        self.preset_combo.blockSignals(False)

    def _sync_preset_combo(self, w, h):
        """Show the preset matching the current size, else Custom."""
        idx = 0
        for i in range(self.preset_combo.count()):
            name = self.preset_combo.itemData(i)
            data = self.preset_manager.presets.get(name) if isinstance(name, str) else None
            if data and data["w"] == w and data["h"] == h:
                idx = i
                break
        self.preset_combo.blockSignals(True)
        self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.blockSignals(False)

    def _on_preset_activated(self, index):
        name = self.preset_combo.itemData(index)
        if name == SAVE_PRESET_ITEM:
            self._save_preset()
        elif name:
            self._load_preset(name)

    def _load_preset(self, preset_name):
        data = self.preset_manager.presets.get(preset_name)
        if data:
            self._set_size(data["w"], data["h"])
            self._set_status(f"Preset: {preset_name}")

    def _save_preset(self):
        if not self._do_calc():
            self._set_status("Enter a resolution first.", error=True)
            self._sync_preset_combo(None, None)
            return
        name, ok = QInputDialog.getText(
            self, "Save Preset", "Preset name:",
            text=f"{self._last_w}×{self._last_h}"
        )
        if ok and name:
            aspect = aspect_label(self._last_w, self._last_h)
            success = self.preset_manager.add_preset(name, self._last_w, self._last_h, aspect)
            self._fill_presets()
            if success:
                self._set_status(f"Saved preset: {name}", ok=True)
            else:
                self._set_status("Preset saved for this session only (couldn't write file)")
        self._sync_preset_combo(self._last_w, self._last_h)

    # ── resolution ────────────────────────────────────────────────────────────

    def _set_size(self, w, h):
        self.width_edit.setText(str(w))
        self.height_edit.setText(str(h))
        self._do_calc()

    def _try_load_resolution(self):
        w, h = get_timeline_size()
        if w and h:
            self._set_size(w, h)
            self._set_status(f"Loaded from timeline: {w} × {h}", ok=True)
        else:
            self._set_size(1920, 1080)
            self._set_status("Resolve not reachable, showing 1920 × 1080")

    def _reload_resolution(self):
        w, h = get_timeline_size()
        if w and h:
            self._set_size(w, h)
            self._set_status(f"Loaded from timeline: {w} × {h}", ok=True)
        else:
            self._set_status("Couldn't reach Resolve. Is External scripting set to Local?", error=True)

    # ── grid & safe areas ─────────────────────────────────────────────────────

    def _set_grid_mode(self, mode):
        self.current_grid_mode = mode
        for btn, (_, mode_id) in zip(self.grid_mode_group.buttons(), GRID_MODES):
            btn.setChecked(mode_id == mode)
        self._update_division_spins()
        self._do_calc()

    def _set_axes(self, axes):
        self.axes = axes
        for btn, (_, a) in zip(self.axes_group.buttons(), GUIDE_AXES):
            btn.setChecked(a == axes)
        self.list_x_box.setVisible(axes != "y")
        self.list_y_box.setVisible(axes != "x")
        self.replace_check.setText(f"Replace existing {AXIS_NAMES[axes]}guides")
        self._update_division_spins()
        self._do_calc()

    def _update_division_spins(self):
        """Columns/Rows only apply to Equal mode and to the axes being made."""
        equal = self.current_grid_mode == "equal"
        self.cols_spin.setEnabled(equal and self.axes != "y")
        self.rows_spin.setEnabled(equal and self.axes != "x")

    def _update_safe_zones(self):
        self.preview.set_safe_visible(self.safe_action_check.isChecked(),
                                      self.safe_title_check.isChecked())

    # ── calculation ───────────────────────────────────────────────────────────

    def _on_input_changed(self):
        self._set_status("")
        self._redraw_timer.start()

    def _read_size(self):
        try:
            w, h = int(self.width_edit.text()), int(self.height_edit.text())
            return (w, h) if w > 0 and h > 0 else (None, None)
        except ValueError:
            return None, None

    def _do_calc(self):
        w, h = self._read_size()
        if not w:
            self._last_w = self._last_h = None
            self._last_gx, self._last_gy = [], []
            self.preview.set_data(None, None, [], [])
            self._fill_list(self.list_x, [], "x")
            self._fill_list(self.list_y, [], "y")
            self._sync_preset_combo(None, None)
            self._set_copy_enabled(False)
            return False

        mode = self.current_grid_mode
        if mode == "thirds":
            gx, gy = calc_rule_of_thirds(w), calc_rule_of_thirds(h)
        elif mode == "golden":
            gx, gy = calc_golden_ratio(w), calc_golden_ratio(h)
        elif mode == "fibonacci":
            gx, gy = calc_fibonacci(w), calc_fibonacci(h)
        else:
            gx = calc_guides(w, self.cols_spin.value())
            gy = calc_guides(h, self.rows_spin.value())

        # Y reads top to bottom, matching Resolve's ruler (+Y up)
        gx, gy = sorted(gx), sorted(gy, reverse=True)
        if self.axes == "x":
            gy = []
        elif self.axes == "y":
            gx = []
        self._last_w, self._last_h = w, h
        self._last_gx, self._last_gy = gx, gy

        self.preview.set_data(w, h, gx, gy)
        self._fill_list(self.list_x, gx, "x")
        self._fill_list(self.list_y, gy, "y")
        self._sync_preset_combo(w, h)
        self._set_copy_enabled(True)
        self.copy_vert_btn.setEnabled(bool(gx))
        self.copy_horiz_btn.setEnabled(bool(gy))
        return True

    def _fill_list(self, lst, values, axis):
        lst.clear()
        for v in values:
            item = QListWidgetItem(fmt_val(v))
            item.setData(Qt.UserRole, v)
            item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lst.addItem(item)

    def _set_copy_enabled(self, on):
        for btn in (self.copy_all_btn, self.copy_vert_btn, self.copy_horiz_btn, self.send_btn):
            btn.setEnabled(on)

    # ── clipboard & export ────────────────────────────────────────────────────

    def _on_viewer_hover(self, guide):
        """Mirror the viewer hover in the sidebar lists."""
        for lst, axis in ((self.list_x, "x"), (self.list_y, "y")):
            lst.clearSelection()
            if guide and guide[0] == axis:
                for i in range(lst.count()):
                    if lst.item(i).data(Qt.UserRole) == guide[1]:
                        lst.item(i).setSelected(True)
                        lst.scrollToItem(lst.item(i))

    def _copy_single(self, axis, value):
        text = f"{value:g}"
        if copy_to_clipboard(text):
            self._set_status(f"Copied {'X' if axis == 'x' else 'Y'} {fmt_val(value)}", ok=True)

    def _copy_axis(self, axis):
        if not self._do_calc():
            return
        values = self._last_gx if axis == "x" else self._last_gy
        if copy_to_clipboard(format_guides(values)):
            label = "vertical" if axis == "x" else "horizontal"
            self._set_status(f"Copied {label} guides", ok=True)

    def export_text(self, fmt):
        """Export in fmt, leaving out an axis that isn't being made."""
        w, h, gx, gy = self._last_w, self._last_h, self._last_gx, self._last_gy
        show_x, show_y = self.axes != "y", self.axes != "x"
        if fmt == "json":
            data = {
                "resolution": {"width": w, "height": h},
                "aspect_ratio": aspect_label(w, h),
                "grid_mode": self.current_grid_mode,
            }
            if self.current_grid_mode == "equal":
                if show_x:
                    data["columns"] = self.cols_spin.value()
                if show_y:
                    data["rows"] = self.rows_spin.value()
            if show_x:
                data["vertical_guides"] = [round(g, 2) for g in gx]
            if show_y:
                data["horizontal_guides"] = [round(g, 2) for g in gy]
            return json.dumps(data, indent=2)
        if fmt == "csv":
            return "\n".join([
                "axis,position",
                *[f"x,{round(g, 2)}" for g in gx],
                *[f"y,{round(g, 2)}" for g in gy],
            ])
        parts = []
        if fmt == "resolve":
            if show_x:
                parts.append(f"Vertical guides (X)\n{format_guides(gx, 'resolve')}")
            if show_y:
                parts.append(f"Horizontal guides (Y, top to bottom)\n{format_guides(gy, 'resolve')}")
            return "\n\n".join(parts)
        if show_x:
            parts.append(f"Vertical (X): {format_guides(gx)}")
        if show_y:
            parts.append(f"Horizontal (Y): {format_guides(gy)}")
        return "\n".join(parts)

    def _copy_all(self):
        if not self._do_calc():
            return
        fmt = self.format_combo.currentData()
        if copy_to_clipboard(self.export_text(fmt)):
            self._set_status(f"Copied {AXIS_NAMES[self.axes]}guides as {self.format_combo.currentText()}", ok=True)
        else:
            self._set_status("Copy failed.", error=True)

    # ── Resolve viewer guides ─────────────────────────────────────────────────

    def _send_to_resolve(self):
        if not self._do_calc():
            return
        w, h = self._last_w, self._last_h
        tw, th = get_timeline_size()
        if tw and (tw, th) != (w, h):
            answer = QMessageBox.question(
                self, "Different resolution",
                f"These guides are for {w} × {h}, but Resolve's timeline is "
                f"{tw} × {th}.\n\nSend them anyway?")
            if answer != QMessageBox.Yes:
                return
        gx, gy, axes = self._last_gx, self._last_gy, self.axes
        n = len(gx) + len(gy)
        kind = AXIS_NAMES[axes]
        clear_first = self.replace_check.isChecked()
        self._run_automation(
            lambda: send_guides_to_resolve(gx, gy, clear_first, clear_axes=axes),
            f"Adding {n} {kind}guides in Resolve. Hands off the mouse until it's done…",
            f"Added {n} {kind}guides to Resolve's viewer")

    def _clear_resolve_guides(self):
        axes = self.axes
        kind = AXIS_NAMES[axes] or "all "
        self._run_automation(
            lambda: clear_resolve_guides(axes),
            f"Clearing {kind}guides in Resolve…",
            f"Cleared {kind}guides in Resolve")

    def _run_automation(self, job, busy_msg, done_msg):
        self.send_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)
        self._set_status(busy_msg)

        def work():
            try:
                job()
                self._automation.finished.emit(True, done_msg, False)
            except AutomationError as e:
                self._automation.finished.emit(False, str(e), e.needs_permission)

        threading.Thread(target=work, daemon=True).start()

    def _on_automation_finished(self, ok, msg, needs_permission):
        self.clear_btn.setEnabled(True)
        self.send_btn.setEnabled(self._last_w is not None)
        self._set_status(msg, ok=ok, error=not ok)
        if needs_permission:
            self.raise_()
            self.activateWindow()
            box = QMessageBox(self)
            box.setWindowTitle("Allow control of Resolve")
            box.setText("macOS needs your permission before Guide Calculator can use "
                        "Resolve's View > Guides menu.")
            box.setInformativeText(
                "In System Settings > Privacy & Security > Accessibility, turn on the "
                "app macOS lists for this request (Python, DaVinci Resolve or Terminal), "
                "then try again.")
            open_btn = box.addButton("Open Settings", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is open_btn:
                subprocess.run(["open", ACCESSIBILITY_SETTINGS_URL])

    # ── status ────────────────────────────────────────────────────────────────

    def _set_status(self, msg, error=False, ok=False):
        color = C_ERROR if error else (C_OK if ok else C_TEXT_DIM)
        self.status_lbl.setStyleSheet(f"color: {color};")
        self.status_lbl.setText(msg)


# ── entry point ───────────────────────────────────────────────────────────────

def bring_app_to_front():
    """Make this process the active app. Resolve's own Python is a plain
    executable rather than an app bundle, so its window otherwise opens
    behind Resolve."""
    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p
        send = objc.objc_msgSend
        send.restype = ctypes.c_void_p
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        nsapp = send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        send(nsapp, objc.sel_registerName(b"setActivationPolicy:"), 0)  # regular app
        send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
        send(nsapp, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)
    except Exception:
        pass


def main():
    if inside_resolve():
        launch_window_process()
        return
    if "--install-qt" in sys.argv:
        install_qt_and_relaunch(sys.argv)
        return
    if not HAVE_QT:
        print("GuideCalculator needs PySide6: pip3 install PySide6")
        return
    # Resolve's Python can't find Qt's plugins on its own, so point Qt at the
    # ones that ship with this PySide6 before the app starts.
    import PySide6
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.setLibraryPaths([os.path.join(os.path.dirname(PySide6.__file__), "Qt", "plugins")])
    app = QApplication.instance() or QApplication([])
    apply_dark_theme(app)
    win = GuideCalculator(parse_size_args(sys.argv))
    win.resize(1120, 720)
    win.show()
    win.raise_()
    win.activateWindow()
    for delay in (0, 300, 900):  # the window can take a moment to appear
        QTimer.singleShot(delay, bring_app_to_front)
    app.exec()


main()
