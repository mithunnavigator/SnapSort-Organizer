"""Shared paths and helpers. Everything the tool writes lives inside this repo:
  dashboard/data/   inventory, thumbnails, labels (what the dashboard reads)
  work/             contact sheets and raw labels (scratch)
Your original photos are only ever read, never changed.
"""
import json
import os
import sys

# Windows consoles default to a legacy code page; force UTF-8 so emoji and accented file names print.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "dashboard", "data")
THUMBS = os.path.join(DATA, "thumbs")
WORK = os.path.join(ROOT, "work")
LABELS = os.path.join(WORK, "labels")      # Claude writes one JSON per batch here
JEV_OUT = os.path.join(WORK, "jev.json")   # Jev labels (optional)
CATEGORIES = os.path.join(ROOT, "categories.json")

IMG = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VID = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".webm"}

# Folders never scanned: system/hidden folders, previous sorted output, and this tool itself.
SKIP_DIRS = {"$recycle.bin", "system volume information", "__macosx", ".thumbnails", "@eadir", "node_modules"}


def norm(p):
    """Absolute, case-folded path, for comparing paths on Windows (C:\\Photos == c:/photos/)."""
    p = os.path.expandvars(os.path.expanduser((p or "").strip().strip('"')))
    return os.path.normcase(os.path.abspath(p)).rstrip("\\/")


def clean(p):
    """A path as the user typed or pasted it (quotes, trailing slashes, ~, %USERPROFILE%) -> absolute path."""
    p = os.path.expandvars(os.path.expanduser((p or "").strip().strip('"')))
    if not p:
        return ""
    p = os.path.abspath(p)
    return p if os.path.dirname(p) == p else p.rstrip("\\/")  # keep "C:\" as is


def inside(child, parent):
    c, p = norm(child), norm(parent)
    return c == p or c.startswith(p + os.sep)


def skip_dir(dirpath, name):
    """True for folders a scan should not walk into."""
    n = name.lower()
    if n.startswith(".") or n in SKIP_DIRS or n.endswith(" (sorted)"):
        return True
    full = os.path.join(dirpath, name)
    return inside(full, ROOT) or os.path.exists(os.path.join(full, "sorted.csv"))


LONG = "\\\\?\\"


def lp(p):
    """Windows long-path form (\\\\?\\C:\\...) for paths near the old 260-character limit, so deep Google
    Takeout albums can still be read and copied. Short paths are returned unchanged."""
    if os.name != "nt" or p.startswith(LONG):
        return p
    p = os.path.abspath(p)
    if len(p) < 240:
        return p
    return LONG + "UNC\\" + p[2:] if p.startswith("\\\\") else LONG + p


def unlp(p):
    """Drop the long-path prefix again when the path is short enough to work without it."""
    if p.startswith(LONG + "UNC\\"):
        q = "\\\\" + p[len(LONG) + 4:]
    elif p.startswith(LONG):
        q = p[len(LONG):]
    else:
        return p
    return q if len(q) < 240 else p


def walk_media(src):
    """Every photo/video under src, skipping hidden, system and already-sorted folders.
    Walks in long-path form: a plain os.walk on Windows silently skips folders past 260 characters."""
    root = LONG + os.path.abspath(src) if os.name == "nt" and not os.path.abspath(src).startswith("\\\\") else src
    for dp, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if not skip_dir(unlp(dp), d)]
        for f in fs:
            if f.startswith("._"):
                continue
            if os.path.splitext(f)[1].lower() in IMG | VID:
                yield unlp(os.path.join(dp, f))


def find_claude():
    """The Claude Code command. The Windows installer puts it in ~\\.local\\bin, which a fresh
    Explorer-started process may not have on PATH yet."""
    import shutil
    return shutil.which("claude") or next((p for p in (
        os.path.expanduser(r"~\.local\bin\claude.exe"),
        os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\claude\claude.exe")) if os.path.exists(p)), None)


def recycle(path):
    """Send one file to the Windows Recycle Bin (the Mac Trash elsewhere). Returns an error text, or None.
    Never deletes permanently without a warning: on drives with no Recycle Bin, Windows asks first."""
    if path.startswith(LONG):
        path = path[len(LONG):]
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return "file not found"
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
        FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI, FOF_WANTNUKEWARNING = 3, 4, 0x10, 0x40, 0x400, 0x4000
        if len(path) >= 260:
            return "path too long for the Recycle Bin"
        op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None,  # pFrom must end in two NULs
                             FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING,
                             False, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc or op.fAnyOperationsAborted:
            return "cancelled" if op.fAnyOperationsAborted else f"Windows error {rc}"
        return None if not os.path.exists(path) else "still there"
    if sys.platform == "darwin":
        import shutil
        trash = os.path.expanduser("~/.Trash")
        target = os.path.join(trash, os.path.basename(path))
        b, e = os.path.splitext(target)
        n = 2
        while os.path.exists(target):
            target, n = f"{b} {n}{e}", n + 1
        shutil.move(path, target)
        return None
    return "no trash on this system"


def load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)  # atomic: the dashboard never reads a half-written file


def categories():
    cats = load(CATEGORIES)
    keys = [c["key"] for c in cats]
    assert len(keys) == len(set(keys)), "categories.json has duplicate keys"
    for kind in ("photo", "video"):
        assert any(c["kind"] == kind for c in cats), f"categories.json needs at least one {kind} folder"
    return cats


def folder_name(label):
    """A folder label made safe for Windows file names."""
    s = label.replace("&", "and")
    for ch in '<>:"/\\|?*':
        s = s.replace(ch, "-")
    return s.strip(" .") or "Folder"
