"""Step 4: build the folders.  python scripts/apply.py "D:\\Photos (sorted)"

Reads labels from work/labels/*.json (Claude) and/or work/jev.json (Jev).
If both exist: Jev's answer wins when it is >= 70% confident, otherwise Claude's.
COPIES files into <output>/<Folder>/ and duplicates into <output>/_Review/duplicates/.
Originals are never moved, renamed or deleted. Also writes sorted.csv and duplicates.csv
(UTF-8 with BOM, so they open cleanly in Excel).
"""
import csv, glob, os, shutil, subprocess, sys

sys.path.insert(0, os.path.dirname(__file__))
from common import CATEGORIES, DATA, JEV_OUT, LABELS, WORK, categories, clean, folder_name, inside, load, lp, norm, save

OVERRIDES = os.path.join(WORK, "overrides.json")
src_dir = load(os.path.join(DATA, "source.json"), {}).get("source", "")
default = clean(src_dir) + " (sorted)" if src_dir else ""
dest = clean(sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else default)
if not dest:
    sys.exit('Usage: python scripts/apply.py "D:\\Photos (sorted)"')
if src_dir and inside(dest, src_dir):
    sys.exit("Put the sorted folders outside the photos folder (for example right next to it), "
             "or the next scan would count them twice.")
recs = load(os.path.join(DATA, "inventory.json"))
cats = {c["key"]: c for c in categories()}
dupes = load(os.path.join(DATA, "dupes.json"), [])

claude = {}
for f in sorted(glob.glob(os.path.join(LABELS, "*.json"))):
    claude.update({str(k): v for k, v in load(f).items()})
jev = load(JEV_OUT, {})
if not claude and not jev:
    sys.exit("No labels yet. Label the files first (Claude: work/labels/*.json, or Jev: scripts/label_jev.py).")

labels, fixed, missing = {}, 0, 0
for r in recs:
    k = str(r["id"])
    j = jev.get(k, {})
    pick = j.get("key") if j.get("confidence", 0) >= 0.7 or k not in claude else claude.get(k)
    pick = pick or claude.get(k)
    if pick is None:
        missing += 1
    if pick not in cats or cats[pick]["kind"] != r["kind"]:  # photos only in photo folders, videos in video folders
        fallback = [c for c in cats.values() if c["kind"] == r["kind"]][-1]["key"]
        fixed += pick is not None
        pick = fallback
    labels[k] = pick

for k, v in load(OVERRIDES, {}).items():  # moves made by hand in the dashboard win
    if k in labels and v in cats:
        labels[k] = v

save(os.path.join(DATA, "labels.json"), labels)
src_info = load(os.path.join(DATA, "source.json"), {})
src_info["sorted"] = dest
save(os.path.join(DATA, "source.json"), src_info)
shutil.copy(CATEGORIES, os.path.join(DATA, "categories.json"))

dupe_of = {d: g for g in dupes for d in g["dupes"]}
byid = {r["id"]: r for r in recs}
originals = {norm(r["path"]) for r in recs}


def copy(src, dst):
    if sys.platform == "darwin" and subprocess.run(["cp", "-c", "-p", src, dst], capture_output=True).returncode == 0:
        return
    shutil.copy2(lp(src), lp(dst))  # a real, independent copy: editing it never changes the original


os.makedirs(dest, exist_ok=True)
old = os.path.join(dest, "sorted.csv")
if os.path.exists(old):  # rebuilding: remove only the copies this tool made last time
    with open(old, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            p = os.path.join(dest, row["file"])
            # safety: only files inside the sorted folder, and never one of the originals
            if inside(p, dest) and norm(p) not in originals and os.path.isfile(lp(p)):
                os.remove(lp(p))
    for d, subdirs, files in os.walk(dest, topdown=False):
        if d != dest and not os.listdir(d):
            os.rmdir(d)
with open(os.path.join(dest, "sorted.csv"), "w", newline="", encoding="utf-8-sig") as fc, \
     open(os.path.join(dest, "duplicates.csv"), "w", newline="", encoding="utf-8-sig") as fd:
    wc, wd = csv.writer(fc), csv.writer(fd)
    wc.writerow(["file", "folder", "original_path"])
    wd.writerow(["duplicate", "kept_copy", "type", "duplicate_path", "kept_path"])
    for n, r in enumerate(recs, 1):
        folder = folder_name(cats[labels[str(r["id"])]]["label"])
        if r["id"] in dupe_of:
            g = dupe_of[r["id"]]
            k = byid[g["keep"]]
            wd.writerow([r["name"], k["name"], g["type"], r["path"], k["path"]])
            folder = os.path.join("_Review", "duplicates")
        out_dir = os.path.join(dest, folder)
        os.makedirs(lp(out_dir), exist_ok=True)
        target = os.path.join(out_dir, r["name"])
        if os.path.exists(lp(target)):  # same name from another folder/year (Windows names ignore case)
            b, e = os.path.splitext(r["name"])
            target = os.path.join(out_dir, f"{b}__{r['id']}{e}")
        if not os.path.exists(lp(target)):
            try:
                copy(r["path"], target)
            except OSError as e:
                print(f"  could not copy {r['name']}: {e}", flush=True)
                continue
        wc.writerow([os.path.relpath(target, dest), folder, r["path"]])
        if n % 25 == 0 or n == len(recs):
            print(f"PROGRESS {n} {len(recs)}", flush=True)
        if n % 500 == 0:
            print(f"  copied {n}/{len(recs)}", flush=True)

counts = {}
for r in recs:
    if r["id"] not in dupe_of:
        counts[labels[str(r["id"])]] = counts.get(labels[str(r["id"])], 0) + 1
print(f"Sorted into {dest}")
for k, c in cats.items():
    print(f"  {c['label']:24} {counts.get(k, 0)}")
print(f"  {'_Review/duplicates':24} {len(dupe_of)}")
if missing or fixed:
    print(f"  ({missing} files had no label and {fixed} had a wrong-type label; they went to the last folder of their kind)")
