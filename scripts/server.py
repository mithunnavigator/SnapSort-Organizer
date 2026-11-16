"""The dashboard app.  python scripts/server.py   ->  http://127.0.0.1:8765

Serves dashboard/ plus a small local API so the page can do everything itself:
pick folders, peek at a folder, run the whole sort (scan, duplicates, Claude labels,
build folders) with live progress, edit folders, browse photos at full size, move a
photo to another folder, and open folders in File Explorer.
Listens on 127.0.0.1 only: nothing is reachable from other computers.
"""
import glob, hashlib, io, json, mimetypes, os, random, re, shutil, subprocess, sys, threading, time, urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
from common import (CATEGORIES, DATA, IMG, JEV_OUT, LABELS, ROOT, VID, WORK, clean, find_claude, folder_name, inside, load,
                    norm, recycle, save, walk_media)
import google_photos as gp

mimetypes.add_type("text/javascript", ".js")  # the Windows registry sometimes maps .js to text/plain
mimetypes.add_type("text/css", ".css")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHILD_ENV = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

PORT = int(os.environ.get("PORT", 8765))
DASH = os.path.join(ROOT, "dashboard")
PEEK = os.path.join(DASH, "peek")
OVERRIDES = os.path.join(WORK, "overrides.json")
DELETED = os.path.join(WORK, "deleted.json")  # duplicates the user sent to the Recycle Bin from the dashboard
SCRIPTS = os.path.join(ROOT, "scripts")
PY = sys.executable
job = {"step": None, "running": False, "log": [], "code": None, "phase": None, "phases": [], "done": 0, "total": 0,
       "started": None, "phase_started": None, "timings": {}}


def has_jev_key():
    if os.environ.get("JEV_KEY") or os.environ.get("TYPESAFE_API_KEY"):
        return True
    env = os.path.join(ROOT, ".env")
    return os.path.exists(env) and bool(re.search(r"^(JEV_KEY|TYPESAFE_API_KEY)=\S", open(env, encoding="utf-8").read(), re.M))


def state():
    inv = load(os.path.join(DATA, "inventory.json"), [])
    src = load(os.path.join(DATA, "source.json"), {})
    claude_files = glob.glob(os.path.join(LABELS, "*.json"))
    return {
        "categories": load(CATEGORIES, []),
        "source": src.get("source"), "limit": src.get("limit"), "sorted": src.get("sorted"),
        "files": len(inv), "bytes": sum(r["bytes"] for r in inv),
        "steps": {
            "scan": bool(inv),
            "dupes": os.path.exists(os.path.join(DATA, "dupes.json")),
            "label": bool(claude_files) or os.path.exists(JEV_OUT),
            "jev_labels": os.path.exists(JEV_OUT),
            "apply": bool(src.get("sorted")) and os.path.exists(os.path.join(DATA, "labels.json")),
        },
        "jev_key": has_jev_key(),
        "claude_cli": bool(find_claude()),
        "overrides": len(load(OVERRIDES, {})),
        "deleted": [int(k) for k in load(DELETED, {})],
        "google_source": gp.is_google_folder(src.get("source")),
        "job": {k: job[k] for k in ("step", "running", "code", "phase", "phases", "done", "total", "started", "phase_started", "timings")}
               | {"log": job["log"][-40:], "now": time.time()},
        "platform": sys.platform,
    }


def live_labels():
    """Labels written so far (Claude batches + confident Jev answers), for the live animation."""
    out = {}
    for f in sorted(glob.glob(os.path.join(LABELS, "*.json"))):
        out.update({str(k): v for k, v in load(f, {}).items()})
    for k, v in load(JEV_OUT, {}).items():
        if "key" in v and (v.get("confidence", 0) >= 0.7 or k not in out):
            out[k] = v["key"]
    return out


def clear_library():
    """A new folder (or sample size) means the old labels no longer match."""
    for f in (os.path.join(DATA, "labels.json"), os.path.join(DATA, "dupes.json"), JEV_OUT, OVERRIDES, DELETED):
        if os.path.exists(f):
            os.remove(f)
    for d in (LABELS, os.path.join(WORK, "sheets")):
        shutil.rmtree(d, ignore_errors=True)


def categories_fingerprint():
    return hashlib.sha1(open(CATEGORIES, "rb").read()).hexdigest()[:12]


def stream(cmd):
    """Run one script, keep its output for the log, and read PROGRESS lines."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", cwd=ROOT, env=CHILD_ENV, creationflags=NO_WINDOW)
    for line in p.stdout:
        line = line.rstrip()
        m = re.match(r"PROGRESS (\d+) (\d+)", line)
        if m:
            job["done"], job["total"] = int(m.group(1)), int(m.group(2))
        else:
            job["log"].append(line)
    return p.wait()


def start_job(step, phases, body):
    if job["running"]:
        raise RuntimeError("Something is already running. Wait for it to finish.")
    now = time.time()
    job.update(step=step, running=True, log=[f"$ {step}"], code=None, phase=None, phases=phases, done=0, total=0,
               started=now, phase_started=now, timings={})
    threading.Thread(target=body, daemon=True).start()


def run_phase(name, cmd):
    job.update(phase=name, done=0, total=0, phase_started=time.time())
    code = stream([PY, "-u"] + cmd)
    job["timings"][name] = round(time.time() - job["phase_started"], 1)
    return code


def sort_all(args):
    source = clean(args.get("source"))
    dest = clean(args.get("dest"))
    limit = int(args.get("limit") or 0)
    labeler = args.get("labeler") or "claude"
    if not source or not os.path.isdir(source):
        raise ValueError("Choose the folder with your photos first.")
    if dest and inside(dest, source):
        raise ValueError("Put the sorted copies outside the photos folder, for example right next to it.")
    if dest and inside(dest, ROOT):
        raise ValueError("Put the sorted copies somewhere outside SnapSort Organizer's own folder.")
    if labeler == "claude" and find_claude() and claude_signed_in() is False:
        raise ValueError("Claude Code is installed but not signed in. Open a terminal (or PowerShell), type  claude  "
                         "and sign in, then press Start sorting again.")
    if labeler == "claude" and not find_claude():
        raise ValueError("Claude Code is not installed, so it can't label the photos. Install it, or use Jev under Advanced.")
    if labeler == "jev" and not has_jev_key():
        raise ValueError("Add JEV_KEY=your-key to a .env file in this folder to use Jev.")
    src = load(os.path.join(DATA, "source.json"), {})
    have = os.path.exists(os.path.join(DATA, "inventory.json"))
    need_scan = not have or src.get("source") != source or int(src.get("limit") or 0) != limit
    need_scan = need_scan or bool(load(DELETED, {}))  # duplicates were deleted since the last scan
    manifest = os.path.join(source, gp.MANIFEST)  # more photos were downloaded from Google since the last scan
    if not need_scan and os.path.exists(manifest):
        need_scan = os.path.getmtime(manifest) > os.path.getmtime(os.path.join(DATA, "inventory.json"))
    phases = (["scan"] if need_scan else []) + ["dupes", "label", "apply"]

    def body():
        def fail(code):
            job.update(running=False, code=code)
        if need_scan:
            clear_library()
            if run_phase("scan", [os.path.join(SCRIPTS, "scan.py"), source] + (["--limit", str(limit)] if limit else [])):
                return fail(1)
        if run_phase("dupes", [os.path.join(SCRIPTS, "dupes.py")]):
            return fail(1)
        marker = os.path.join(LABELS, ".categories")
        if load(marker) != categories_fingerprint():  # folders changed since the last labels
            shutil.rmtree(LABELS, ignore_errors=True)
            if os.path.exists(JEV_OUT):
                os.remove(JEV_OUT)
        os.makedirs(LABELS, exist_ok=True)
        save(marker, categories_fingerprint())
        script = "label_jev.py" if labeler == "jev" else "label_claude.py"
        if run_phase("label", [os.path.join(SCRIPTS, script)]):
            total = len(load(os.path.join(DATA, "inventory.json"), []))
            if not total or len(live_labels()) < 0.95 * total:
                return fail(1)
            job["log"].append(f"A few files could not be labelled; they go to the catch-all folders.")
        if run_phase("apply", [os.path.join(SCRIPTS, "apply.py"), dest]):
            return fail(1)
        job.update(running=False, code=0, phase="done")
    start_job("sort", phases, body)


def run_single(step, args):
    cmds = {
        "scan": ["scan.py", args.get("source", "")] + (["--limit", str(args["limit"])] if args.get("limit") else []),
        "dupes": ["dupes.py"], "estimate": ["label_jev.py", "--estimate"], "jev": ["label_jev.py"],
        "claude": ["label_claude.py"], "apply": ["apply.py", args.get("dest", "")],
    }
    if step not in cmds:
        raise ValueError("unknown step")

    def body():
        if step == "scan":
            clear_library()
        code = run_phase(step, [os.path.join(SCRIPTS, cmds[step][0])] + cmds[step][1:])
        job.update(running=False, code=code, phase="done" if code == 0 else step)
    start_job(step, [step], body)


def claude_signed_in():
    """True / False from `claude auth status`, or None if that can't be told (then sorting just tries)."""
    try:
        r = subprocess.run([find_claude(), "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30, creationflags=NO_WINDOW)
        return bool(json.loads(r.stdout).get("loggedIn"))
    except Exception:
        return None


def move_copy(i, key):
    """After "Move to" in the dashboard, move the sorted COPY too (never the original) and update sorted.csv."""
    import csv
    src = load(os.path.join(DATA, "source.json"), {})
    dest = src.get("sorted")
    cats = {c["key"]: c for c in load(CATEGORIES, [])}
    rec = next((r for r in load(os.path.join(DATA, "inventory.json"), []) if r["id"] == i), None)
    csv_path = os.path.join(dest or "", "sorted.csv")
    if not dest or key not in cats or not rec or not os.path.exists(csv_path):
        return
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        if os.path.normcase(row["original_path"]) != os.path.normcase(rec["path"]):
            continue
        if row["folder"].replace("/", "\\").lower().startswith("_review"):
            return  # duplicates stay in _Review
        old = os.path.join(dest, row["file"])
        folder = folder_name(cats[key]["label"])
        new = os.path.join(dest, folder, os.path.basename(old))
        if not inside(old, dest) or not os.path.isfile(old) or os.path.normcase(old) == os.path.normcase(new):
            return
        os.makedirs(os.path.dirname(new), exist_ok=True)
        if os.path.exists(new):
            b, e = os.path.splitext(os.path.basename(old))
            new = os.path.join(dest, folder, f"{b}__{i}{e}")
        os.replace(old, new)
        row["file"], row["folder"] = os.path.relpath(new, dest), folder
        break
    else:
        return
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["file", "folder", "original_path"])
        w.writeheader()
        w.writerows(rows)


def suggest_dest(source):
    parent, name = os.path.split(source.rstrip("\\/"))
    if not name:  # a whole drive, e.g. D:\
        return os.path.join(source, "Sorted photos")
    return source + " (sorted)"


def delete_dupes(ids):
    """Send duplicate files to the Recycle Bin, at the user's request from the dashboard.
    Only files that dupes.py marked as extra copies, only inside the photos folder, and only while the
    copy being kept still exists. Their copies in <sorted>/_Review/duplicates go too."""
    import csv
    src = load(os.path.join(DATA, "source.json"), {})
    source, dest = src.get("source"), src.get("sorted")
    inv = {r["id"]: r for r in load(os.path.join(DATA, "inventory.json"), [])}
    groups = load(os.path.join(DATA, "dupes.json"), [])
    keeper_of = {d: g["keep"] for g in groups for d in g["dupes"]}
    deleted = load(DELETED, {})
    want = set(keeper_of) if ids is None else {int(i) for i in ids}
    review = {}
    if dest and os.path.exists(os.path.join(dest, "sorted.csv")):
        with open(os.path.join(dest, "sorted.csv"), encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row["folder"].replace("/", "\\").lower().startswith("_review"):
                    review[norm(row["original_path"])] = os.path.join(dest, row["file"])
    done, freed, skipped = [], 0, []
    for i in sorted(want):
        rec = inv.get(i)
        if i not in keeper_of or not rec:
            skipped.append(f"#{i}: not a duplicate")
            continue
        if str(i) in deleted:
            continue
        keep = inv.get(keeper_of[i])
        if not keep or not os.path.isfile(keep["path"]):
            skipped.append(f"{rec['name']}: the copy being kept is missing, so this one stays")
            continue
        if not source or not inside(rec["path"], source):
            skipped.append(f"{rec['name']}: not inside the photos folder")
            continue
        err = recycle(rec["path"])
        if err:
            skipped.append(f"{rec['name']}: {err}")
            continue
        copy = review.get(norm(rec["path"]))
        if copy and dest and inside(copy, dest) and os.path.isfile(copy):
            recycle(copy)
        deleted[str(i)] = {"path": rec["path"], "bytes": rec["bytes"], "at": int(time.time())}
        done.append(i)
        freed += rec["bytes"]
    save(DELETED, deleted)
    return {"deleted": done, "freed": freed, "skipped": skipped}


def google_import(sid, dest):
    dest = clean(dest) or gp.default_folder()
    if inside(dest, ROOT):
        raise ValueError("Download somewhere outside SnapSort Organizer's own folder.")

    def body():
        code = run_phase("import", [os.path.join(SCRIPTS, "google_photos.py"), "import", sid, dest])
        job.update(running=False, code=code, phase="done" if code == 0 else "import")
    start_job("import", ["import"], body)
    job["dest"] = dest


def peek(source):
    """Count what is in a folder and make a few tiny previews, right after it is picked."""
    source = clean(source)
    if not source or not os.path.isdir(source):
        raise ValueError("That folder does not exist.")
    if inside(source, ROOT):
        raise ValueError("That is SnapSort Organizer's own folder. Pick the folder with your photos.")
    photos, videos, size, pics = 0, 0, 0, []
    for path in walk_media(source):
        ext = os.path.splitext(path)[1].lower()
        if ext in IMG:
            photos += 1
            if ext in (".jpg", ".jpeg", ".png", ".heic", ".webp"):
                pics.append(path)
        else:
            videos += 1
        try:
            size += os.path.getsize(path)
        except OSError:
            pass
    zips = 0
    try:
        zips = sum(f.lower().endswith(".zip") for f in os.listdir(source))
    except OSError:
        pass
    shutil.rmtree(PEEK, ignore_errors=True)
    os.makedirs(PEEK)
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass
    out, stamp = [], int(time.time())
    for i, p in enumerate(random.Random(3).sample(pics, min(60, len(pics)))):
        try:
            with Image.open(p) as im:
                im.draft("RGB", (160, 160))
                im = ImageOps.exif_transpose(im).convert("RGB")
                s = min(im.size)
                im = im.crop(((im.width - s) // 2, (im.height - s) // 2, (im.width + s) // 2, (im.height + s) // 2)).resize((64, 64))
                im.save(os.path.join(PEEK, f"{i}.jpg"), quality=70)
                out.append(f"peek/{i}.jpg?{stamp}")
        except Exception:
            pass
    return {"source": source, "photos": photos, "videos": videos, "bytes": size, "previews": out, "zips": zips,
            "suggested_dest": suggest_dest(source)}


def open_path(path, reveal=False):
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path] if reveal else ["open", path])
    elif sys.platform.startswith("win"):
        path = os.path.normpath(path[4:] if path.startswith("\\\\?\\") else path)  # Explorer can't take \\?\ paths
        # explorer wants /select,"C:\a b\c.jpg" as one raw argument, so build the command line by hand
        subprocess.Popen(f'explorer /select,"{path}"' if reveal else f'explorer "{path}"')
    else:
        subprocess.run(["xdg-open", os.path.dirname(path) if reveal else path])


_PICK_TK = r"""
import sys, tkinter as tk
from tkinter import filedialog
r = tk.Tk(); r.withdraw(); r.attributes("-topmost", True); r.update()
p = filedialog.askdirectory(parent=r, title=sys.argv[1], mustexist=False)
sys.stdout.write(p or "")
"""

_PICK_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object System.Windows.Forms.FolderBrowserDialog
$f.Description = $env:PICK_PROMPT
$f.ShowNewFolderButton = $true
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}
if ($f.ShowDialog($owner) -eq 'OK') { [Console]::Out.Write($f.SelectedPath) }
"""


def pick_folder(prompt):
    """Native folder picker, shown in front of the browser (macOS Finder dialog or Windows Explorer dialog)."""
    prompt = prompt.replace('"', "'")[:120]
    if sys.platform == "darwin":
        script = ('tell application (path to frontmost application as text)\n'
                  f'  POSIX path of (choose folder with prompt "{prompt}")\n'
                  'end tell')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        return r.stdout.strip().rstrip("/") or None
    if sys.platform.startswith("win"):
        try:  # tkinter gives the modern Windows folder dialog
            r = subprocess.run([PY, "-c", _PICK_TK, prompt], capture_output=True, text=True, encoding="utf-8",
                               env=CHILD_ENV, timeout=1800, creationflags=NO_WINDOW)
            if r.returncode == 0:
                return clean(r.stdout) or None
        except Exception:
            pass
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", _PICK_PS], capture_output=True, text=True,
                           encoding="utf-8", env=dict(CHILD_ENV, PICK_PROMPT=prompt), timeout=1800, creationflags=NO_WINDOW)
        return clean(r.stdout) or None
    return None


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=DASH, **k)

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def end_headers(self):
        if self.path.startswith(("/data/", "/peek/")) or self.path.endswith((".js", ".css", ".html")) or self.path == "/":
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/state":
            return self.send_json(state())
        if u.path == "/api/labels":
            return self.send_json(live_labels())
        m = re.match(r"^/api/file/(\d+)$", u.path)
        if m:
            return self.send_original(int(m.group(1)))
        if u.path == "/api/google/status":
            return self.send_json(gp.status() | {"folder": gp.default_folder()})
        if u.path == "/api/google/session":
            try:
                return self.send_json(gp.session_state(urllib.parse.parse_qs(u.query).get("id", [""])[0]))
            except Exception as e:
                return self.send_json({"error": str(e)}, 400)
        if u.path == "/api/google/callback":
            return self.google_callback(urllib.parse.parse_qs(u.query))
        if u.path.startswith(("/work/", "/scripts/", "/.venv/")) or u.path.endswith((".py", ".env")):
            return self.send_error(404)
        return super().do_GET()

    def google_callback(self, q):
        """Google sends the browser back here after sign-in (the OAuth loopback redirect)."""
        ok, msg = False, ""
        if q.get("error"):
            msg = "Sign-in was cancelled." if q["error"][0] == "access_denied" else "Google said: " + q["error"][0]
        else:
            try:
                tok = gp.finish_sign_in(q.get("state", [""])[0], q.get("code", [""])[0])
                ok, msg = True, f"Signed in{(' as ' + tok['email']) if tok.get('email') else ''}."
            except Exception as e:
                msg = str(e)
        import html
        page = (f"<!doctype html><meta charset=utf-8><title>SnapSort Organizer</title>"
                f"<body style='font:16px system-ui,Segoe UI,sans-serif;display:grid;place-items:center;height:90vh;margin:0'>"
                f"<div style='text-align:center'><h2>{'✓ ' if ok else ''}{html.escape(msg)}</h2>"
                f"<p>{'You can close this tab and go back to SnapSort Organizer.' if ok else 'Close this tab and try again in SnapSort Organizer.'}</p></div>"
                f"<script>setTimeout(()=>window.close(),{1500 if ok else 8000})</script>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def send_original(self, i):
        inv = load(os.path.join(DATA, "inventory.json"), [])
        rec = next((r for r in inv if r["id"] == i), None)
        if not rec or not os.path.exists(rec["path"]):
            return self.send_error(404)
        path = rec["path"]
        if rec["ext"] in (".heic", ".heif", ".tif", ".tiff", ".bmp"):  # browsers can't show these: convert
            from PIL import Image, ImageOps
            try:
                from pillow_heif import register_heif_opener
                register_heif_opener()
            except ImportError:
                pass
            im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
            im.thumbnail((2400, 2400))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88)
            b = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if rec["ext"] == ".mov":
            ctype = "video/mp4"  # most iPhone .mov files play fine as mp4
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                start = int(m.group(1) or 0)
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
            return self.send_json({"error": "forbidden"}, 403)  # only this page may call the API
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        p = urllib.parse.urlparse(self.path).path
        try:
            if p == "/api/sort":
                sort_all(body)
            elif p == "/api/run":
                run_single(body.get("step"), body)
            elif p == "/api/peek":
                return self.send_json(peek(body.get("source", "")))
            elif p == "/api/categories":
                cats = body["categories"]
                keys = [c["key"] for c in cats]
                if len(keys) != len(set(keys)) or not all(c.get("label") and c.get("kind") in ("photo", "video") for c in cats):
                    raise ValueError("Every folder needs a name.")
                if not any(c["kind"] == "photo" for c in cats) or not any(c["kind"] == "video" for c in cats):
                    raise ValueError("Keep at least one photo folder and one video folder.")
                save(CATEGORIES, cats)
                save(os.path.join(DATA, "categories.json"), cats)
            elif p == "/api/move":
                ov = load(OVERRIDES, {})
                ov[str(body["id"])] = body["key"]
                save(OVERRIDES, ov)
                labels = load(os.path.join(DATA, "labels.json"), {})
                labels[str(body["id"])] = body["key"]
                save(os.path.join(DATA, "labels.json"), labels)
                move_copy(int(body["id"]), body["key"])
            elif p == "/api/open":
                src = load(os.path.join(DATA, "source.json"), {})
                if body.get("id") is not None:
                    rec = next(r for r in load(os.path.join(DATA, "inventory.json")) if r["id"] == int(body["id"]))
                    open_path(rec["path"], reveal=True)
                elif src.get("sorted"):
                    key = body.get("key")
                    cats = {c["key"]: c for c in load(CATEGORIES, [])}
                    folder = os.path.join("_Review", "duplicates") if key == "_dupes" else \
                        folder_name(cats[key]["label"]) if key in cats else ""
                    path = os.path.join(src["sorted"], folder) if folder else src["sorted"]
                    open_path(path if os.path.isdir(path) else src["sorted"])
                else:
                    raise ValueError("Sort first, then the folders can be opened.")
            elif p == "/api/pick":
                return self.send_json({"path": pick_folder(body.get("prompt") or "Choose the folder with your photos")})
            elif p == "/api/dupes/delete":
                if job["running"]:
                    raise ValueError("Wait for the current job to finish first.")
                return self.send_json(delete_dupes(body.get("ids")) | {"state": state()})
            elif p == "/api/google/setup":
                gp.save_client(body.get("client_id"), body.get("client_secret"))
                return self.send_json(gp.status())
            elif p == "/api/google/connect":
                host = "localhost" if (self.headers.get("Host") or "").startswith("localhost") else "127.0.0.1"
                return self.send_json({"url": gp.auth_url(f"http://{host}:{PORT}/api/google/callback")})
            elif p == "/api/google/disconnect":
                gp.sign_out()
                return self.send_json(gp.status())
            elif p == "/api/google/pick":
                return self.send_json(gp.new_session())
            elif p == "/api/google/import":
                google_import(body.get("id", ""), body.get("dest", ""))
                return self.send_json(state() | {"dest": job.get("dest")})
            else:
                return self.send_json({"error": "not found"}, 404)
        except Exception as e:
            return self.send_json({"error": str(e)}, 400)
        return self.send_json(state())


if __name__ == "__main__":
    os.makedirs(DATA, exist_ok=True)
    print(f"SnapSort Organizer running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
