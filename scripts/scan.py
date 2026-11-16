"""Step 1: inventory a photo folder (any folder, Google Takeout, Apple Photos / iCloud export...).

  python scripts/scan.py "C:\\path\\to\\photos"              # everything
  python scripts/scan.py "C:\\path\\to\\photos" --limit 200  # try a sample first

Records size, sha256, dimensions, video length, the date taken (Google Takeout .json sidecar,
else the photo's EXIF) and GPS for each file, and makes a 256px square preview in
dashboard/data/thumbs/. Read-only on your photos.
"""
import argparse, hashlib, json, os, random, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PIL import Image, ImageOps
import imagehash

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

sys.path.insert(0, os.path.dirname(__file__))
from common import DATA, THUMBS, VID, clean, save, walk_media

ap = argparse.ArgumentParser()
ap.add_argument("source")
ap.add_argument("--limit", type=int, default=0, help="only scan a random sample of N files")
args = ap.parse_args()
src = clean(args.source)
if not os.path.isdir(src):
    sys.exit(f"Folder not found: {src}")

HAS_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # don't flash a console window per video
MINI = os.path.join(DATA, "mini")  # 72px copies for the animation, so hundreds can move smoothly
for d in (THUMBS, MINI):
    if os.path.isdir(d):
        shutil.rmtree(d)
    os.makedirs(d)

# ---------- Google Takeout sidecars ----------
# Takeout writes "<file>.json", "<file>.supplemental-metadata.json", and for copies "<file>.supplemental-metadata(1).json"
# or the older "<file>(1).json" for "<name>(1).<ext>". Names longer than ~46 characters are cut short.
_dir_json = {}


def _jsons(d):
    if d not in _dir_json:
        try:
            _dir_json[d] = [f for f in os.listdir(d) if f.lower().endswith(".json")]
        except OSError:
            _dir_json[d] = []
    return _dir_json[d]


def _split_copy(s):
    """'IMG_1(2).JPG' -> ('IMG_1.JPG', '2');  'IMG_1.JPG.supplemental-metadata(2)' -> ('IMG_1.JPG.supplemental-metadata', '2')"""
    m = re.match(r"^(.*?)\((\d+)\)(\.[^.]+)?$", s)
    if m:
        return m.group(1) + (m.group(3) or ""), m.group(2)
    return s, ""


def sidecar(path):
    d, name = os.path.split(path)
    jsons = _jsons(d)
    if not jsons:
        return None
    base, copy = _split_copy(name)
    tries = [base]
    stem, ext = os.path.splitext(base)
    if stem.lower().endswith("-edited"):  # "IMG_1-edited.jpg" shares "IMG_1.jpg.json"
        tries.append(stem[:-7] + ext)
    for b in tries:
        target = (b + ".supplemental-metadata").lower()
        for f in jsons:
            s, k = _split_copy(f[:-5])
            if k != copy:
                continue
            s = s.lower()
            if s == b.lower() or (target.startswith(s) and len(s) >= min(len(b), 30)):
                return os.path.join(d, f)
    return None


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def square(im, size=256):
    im = im.convert("RGB")
    s = min(im.size)
    left, top = (im.width - s) // 2, (im.height - s) // 2
    return im.crop((left, top, left + s, top + s)).resize((size, size))


def exif_info(im):
    """Date taken and GPS from EXIF, when there is no Takeout sidecar."""
    out = {}
    try:
        ex = im.getexif()
        sub = ex.get_ifd(0x8769)
        raw = sub.get(36867) or sub.get(36868) or ex.get(306)
        if raw:
            out["taken"] = int(datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp())
        gps = ex.get_ifd(0x8825)
        if gps and 2 in gps and 4 in gps:
            dms = lambda v: float(v[0]) + float(v[1]) / 60 + float(v[2]) / 3600
            lat, lon = dms(gps[2]), dms(gps[4])
            if gps.get(1) == "S":
                lat = -lat
            if gps.get(3) == "W":
                lon = -lon
            if lat or lon:
                out["geo"] = [round(lat, 6), round(lon, 6)]
    except Exception:
        pass
    return out


def video_info(path, thumb):
    if not HAS_FFMPEG:
        return 0, 0, 0.0
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height:stream_tags=rotate:stream_side_data=rotation:format=duration", "-of", "json", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    try:
        j = json.loads(r.stdout or "{}")
    except ValueError:
        j = {}
    s = (j.get("streams") or [{}])[0]
    w, h = s.get("width", 0), s.get("height", 0)
    rot = str(s.get("tags", {}).get("rotate", "0"))
    for sd in s.get("side_data_list", []) or []:
        rot = str(sd.get("rotation", rot))
    if rot.lstrip("-") in ("90", "270"):
        w, h = h, w
    dur = float(j.get("format", {}).get("duration", 0) or 0)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1" if dur > 2 else "0", "-i", path, "-frames:v", "1",
                    "-vf", "scale=256:256:force_original_aspect_ratio=increase,crop=256:256", thumb],
                   capture_output=True, creationflags=NO_WINDOW)
    return w, h, dur


def process(args):
    i, path = args
    ext = os.path.splitext(path)[1].lower()
    kind = "video" if ext in VID else "photo"
    rec = {"id": i, "path": path, "name": os.path.basename(path), "ext": ext, "kind": kind,
           "bytes": os.path.getsize(path), "sha256": sha(path)}
    thumb = os.path.join(THUMBS, f"{i}.jpg")
    try:
        if kind == "video":
            rec["w"], rec["h"], rec["duration"] = video_info(path, thumb)
            if not os.path.exists(thumb):  # no ffmpeg: grey placeholder so the dashboard still works
                Image.new("RGB", (256, 256), (60, 60, 66)).save(thumb)
        else:
            with Image.open(path) as im:
                rec["w"], rec["h"] = im.size
                rec.update(exif_info(im))
                im.draft("RGB", (512, 512))
                square(ImageOps.exif_transpose(im)).save(thumb, quality=80)
        with Image.open(thumb) as t:
            rec["phash"] = str(imagehash.phash(t))
            t.convert("RGB").resize((72, 72)).save(os.path.join(MINI, f"{i}.jpg"), quality=72)
    except Exception as e:  # unreadable file: keep it in the inventory, flag it
        rec["error"] = str(e)[:200]
        Image.new("RGB", (256, 256), (230, 228, 222)).save(thumb)
        Image.new("RGB", (72, 72), (230, 228, 222)).save(os.path.join(MINI, f"{i}.jpg"))
    sc = sidecar(path)
    if sc:
        try:
            with open(sc, encoding="utf-8") as f:
                j = json.load(f)
            t = int(j.get("photoTakenTime", {}).get("timestamp", 0) or 0)
            if t:
                rec["taken"] = t
            g = j.get("geoData") or {}
            if g.get("latitude"):
                rec["geo"] = [g["latitude"], g["longitude"]]
        except Exception:
            pass
    if not rec.get("taken"):  # last resort: the file's own date
        try:
            rec["taken_guess"] = int(os.path.getmtime(path))
        except OSError:
            pass
    return rec


files = sorted(walk_media(src))
if not files:
    sys.exit("No photos or videos found in that folder.")
zips = [f for f in os.listdir(src) if f.lower().endswith(".zip")]
if zips:
    print(f"  note: {len(zips)} .zip file(s) in this folder were skipped. Unzip Google Takeout zips first "
          "(right-click > Extract All) to include those photos.")
if args.limit and args.limit < len(files):
    files = sorted(random.Random(0).sample(files, args.limit))
print(f"Scanning {len(files)} files...", flush=True)
if not HAS_FFMPEG and any(os.path.splitext(f)[1].lower() in VID for f in files):
    print("  note: ffmpeg is not installed, so videos get grey previews (install: winget install Gyan.FFmpeg)")

t0 = time.time()
with ThreadPoolExecutor(min(16, (os.cpu_count() or 4) + 2)) as ex:
    recs = []
    for n, r in enumerate(ex.map(process, enumerate(files)), 1):
        recs.append(r)
        if n % 20 == 0 or n == len(files):
            print(f"PROGRESS {n} {len(files)}", flush=True)
        if n % 200 == 0:
            print(f"  {n}/{len(files)}", flush=True)

save(os.path.join(DATA, "inventory.json"), recs)
save(os.path.join(DATA, "source.json"), {"source": src, "limit": args.limit})
bad = [r["name"] for r in recs if "error" in r]
print(f"Done in {time.time() - t0:.0f}s: {len(recs)} files ({sum(r['kind'] == 'video' for r in recs)} videos), "
      f"{sum(r['bytes'] for r in recs) / 1e9:.1f} GB" + (f", {len(bad)} unreadable: {bad[:5]}" if bad else ""))
