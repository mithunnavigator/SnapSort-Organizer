"""Google Photos: sign in, let the user pick photos, download them into a local folder to sort.

Google only lets apps read the photos the user picks in Google's own Photos Picker (up to 2,000 per
round; the full-library API was switched off on 31 March 2025). Apps also cannot delete, move or
re-album photos in Google Photos. So SnapSort Organizer downloads the picked photos and sorts those copies.

  python scripts/google_photos.py import <session id> "<download folder>"   (run by the dashboard)

Setup (once): a Google Cloud OAuth client of type "Desktop app" with the Photos Picker API enabled.
Its client ID and secret are saved in google_client.json in this folder. The sign-in token is kept
in work/google_token.json. Neither ever leaves this computer except to talk to Google.
"""
import base64, hashlib, json, os, re, secrets, sys, threading, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from common import ROOT, WORK, load, save

CLIENT_FILE = os.path.join(ROOT, "google_client.json")
TOKEN_FILE = os.path.join(WORK, "google_token.json")
MANIFEST = ".snapsort-google.json"  # in the download folder: which Google items are already there

# Overridable only so the tests can point at a fake Google.
AUTH_URL = os.environ.get("SNAPSORT_GOOGLE_AUTH", "https://accounts.google.com/o/oauth2/v2/auth")
TOKEN_URL = os.environ.get("SNAPSORT_GOOGLE_TOKEN", "https://oauth2.googleapis.com/token")
REVOKE_URL = os.environ.get("SNAPSORT_GOOGLE_REVOKE", "https://oauth2.googleapis.com/revoke")
PICKER = os.environ.get("SNAPSORT_GOOGLE_PICKER", "https://photospicker.googleapis.com/v1")
SCOPES = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly openid email"

_lock = threading.Lock()


# ---------- setup ----------
def client():
    """(client_id, client_secret) or None."""
    if os.environ.get("GOOGLE_CLIENT_ID"):
        return os.environ["GOOGLE_CLIENT_ID"], os.environ.get("GOOGLE_CLIENT_SECRET", "")
    c = load(CLIENT_FILE)
    if not c:
        return None
    c = c.get("installed") or c.get("web") or c  # the JSON Google Cloud lets you download, or our own
    return (c["client_id"], c.get("client_secret", "")) if c.get("client_id") else None


def save_client(client_id, client_secret):
    client_id, client_secret = (client_id or "").strip(), (client_secret or "").strip()
    if not re.match(r"^[\w.-]+\.apps\.googleusercontent\.com$", client_id):
        raise ValueError("That doesn't look like a Google client ID. It ends in .apps.googleusercontent.com.")
    if not client_secret:
        raise ValueError("Paste the client secret too (it starts with GOCSPX-).")
    save(CLIENT_FILE, {"installed": {"client_id": client_id, "client_secret": client_secret}})


# ---------- sign in (OAuth 2.0 for desktop apps: loopback redirect + PKCE) ----------
_pending = {}  # state -> (verifier, redirect_uri, created)


def auth_url(redirect_uri):
    c = client()
    if not c:
        raise ValueError("Set up the Google connection first.")
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v[2] > 900]:
        _pending.pop(k, None)
    _pending[state] = (verifier, redirect_uri, now)
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": c[0], "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPES,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        "access_type": "offline", "prompt": "consent"})


def _post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode())
            msg = j.get("error_description") or j.get("error") or str(e)
        except Exception:
            msg = str(e)
        raise RuntimeError(f"Google said: {msg}")


def _email(id_token):
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email")
    except Exception:
        return None


def finish_sign_in(state, code):
    if state not in _pending:
        raise ValueError("This sign-in link has expired. Press Sign in with Google again.")
    verifier, redirect_uri, _ = _pending.pop(state)
    cid, secret = client()
    t = _post(TOKEN_URL, {"client_id": cid, "client_secret": secret, "code": code, "code_verifier": verifier,
                          "grant_type": "authorization_code", "redirect_uri": redirect_uri})
    tok = {"access_token": t["access_token"], "refresh_token": t.get("refresh_token"),
           "expires": time.time() + int(t.get("expires_in", 3600)) - 60, "email": _email(t.get("id_token", ""))}
    if "photospicker" not in t.get("scope", "photospicker"):
        raise ValueError("Google Photos access wasn't granted. Sign in again and tick the Google Photos box.")
    save(TOKEN_FILE, tok)
    return tok


def status():
    tok = load(TOKEN_FILE) or {}
    return {"configured": bool(client()), "connected": bool(tok.get("refresh_token") or tok.get("access_token")),
            "email": tok.get("email")}


def access_token(force=False):
    with _lock:
        tok = load(TOKEN_FILE)
        if not tok:
            raise RuntimeError("Not signed in to Google Photos.")
        if not force and tok.get("expires", 0) > time.time():
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise RuntimeError("The Google sign-in expired. Sign in again.")
        cid, secret = client()
        try:
            t = _post(TOKEN_URL, {"client_id": cid, "client_secret": secret, "refresh_token": tok["refresh_token"],
                                  "grant_type": "refresh_token"})
        except RuntimeError as e:
            if "invalid_grant" in str(e) or "expired or revoked" in str(e):
                os.remove(TOKEN_FILE)
                raise RuntimeError("The Google sign-in expired or was removed. Sign in again.")
            raise
        tok.update(access_token=t["access_token"], expires=time.time() + int(t.get("expires_in", 3600)) - 60)
        save(TOKEN_FILE, tok)
        return tok["access_token"]


def sign_out():
    tok = load(TOKEN_FILE) or {}
    t = tok.get("refresh_token") or tok.get("access_token")
    if t:
        try:
            _post(REVOKE_URL, {"token": t})
        except Exception:
            pass  # already revoked or offline: forget it locally anyway
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)


# ---------- Photos Picker ----------
def _api(method, path, body=None):
    for attempt in range(2):
        req = urllib.request.Request(PICKER + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + access_token(force=attempt == 1),
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue  # token expired early: refresh once
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 403 and "SERVICE_DISABLED" in detail:
                raise RuntimeError("The Photos Picker API isn't turned on in your Google Cloud project. "
                                   "Enable it at console.cloud.google.com > APIs & Services > Library > Photos Picker API.")
            raise RuntimeError(f"Google Photos error {e.code}: {detail}")


def new_session():
    s = _api("POST", "/sessions", {"pickingConfig": {"maxItemCount": "2000"}})
    return {"id": s["id"], "uri": s["pickerUri"].rstrip("/") + "/autoclose"}


def session_state(sid):
    s = _api("GET", "/sessions/" + urllib.parse.quote(sid))
    return {"ready": bool(s.get("mediaItemsSet")), "poll": s.get("pollingConfig", {}).get("pollInterval"),
            "expires": s.get("expireTime")}


def picked_items(sid):
    out, token = [], ""
    while True:
        q = {"sessionId": sid, "pageSize": 100}
        if token:
            q["pageToken"] = token
        j = _api("GET", "/mediaItems?" + urllib.parse.urlencode(q))
        out += j.get("mediaItems", [])
        token = j.get("nextPageToken")
        if not token:
            return out


def _safe_name(name, fallback):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return name or fallback


def _download(item, dest, taken_names):
    mf = item.get("mediaFile", {})
    video = item.get("type") == "VIDEO" or mf.get("mimeType", "").startswith("video/")
    if video and mf.get("mediaFileMetadata", {}).get("videoMetadata", {}).get("processingStatus") not in (None, "READY"):
        return None, "video still processing in Google Photos"
    url = mf["baseUrl"] + ("=dv" if video else "=d")  # =d keeps EXIF (Google strips the location)
    name = _safe_name(mf.get("filename"), item["id"][:16] + (".mp4" if video else ".jpg"))
    with _lock:
        b, e = os.path.splitext(name)
        n = 2
        while name.lower() in taken_names or os.path.exists(os.path.join(dest, name)):
            name = f"{b}__{n}{e}"
            n += 1
        taken_names.add(name.lower())
    path = os.path.join(dest, name)
    for attempt in range(4):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access_token(force=attempt > 0)})
        try:
            with urllib.request.urlopen(req, timeout=300) as r, open(path + ".part", "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(path + ".part", path)
            break
        except urllib.error.HTTPError as e:
            if e.code in (401, 429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            _cleanup(path)
            return None, f"error {e.code}"
        except OSError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            _cleanup(path)
            return None, str(e)[:80]
    try:  # file date = when the photo was taken, so it sorts and shows correctly in Explorer
        t = datetime.fromisoformat(item["createTime"].replace("Z", "+00:00")).timestamp()
        os.utime(path, (t, t))
    except Exception:
        pass
    return name, None


def _cleanup(path):
    for p in (path, path + ".part"):
        try:
            os.remove(p)
        except OSError:
            pass


def import_session(sid, dest):
    os.makedirs(dest, exist_ok=True)
    man_path = os.path.join(dest, MANIFEST)
    manifest = load(man_path, {"items": {}})
    items = picked_items(sid)
    todo = [it for it in items if it["id"] not in manifest["items"]]
    print(f"{len(items)} picked in Google Photos, {len(items) - len(todo)} already downloaded, {len(todo)} to download.", flush=True)
    taken = {f.lower() for f in os.listdir(dest)}
    done, failed = 0, []
    with ThreadPoolExecutor(4) as ex:
        for it, (name, err) in zip(todo, ex.map(lambda it: _download(it, dest, taken), todo)):
            done += 1
            if name:
                manifest["items"][it["id"]] = {"file": name, "created": it.get("createTime")}
            else:
                failed.append(f"{it.get('mediaFile', {}).get('filename', it['id'])}: {err}")
            if done % 5 == 0 or done == len(todo):
                print(f"PROGRESS {done} {len(todo)}", flush=True)
                save(man_path, manifest)
    save(man_path, manifest)
    try:
        _api("DELETE", "/sessions/" + urllib.parse.quote(sid))  # Google asks apps to clean up finished sessions
    except Exception:
        pass
    print(f"Downloaded {len(todo) - len(failed)} files to {dest}" + (f", {len(failed)} failed" if failed else ""), flush=True)
    for f in failed[:10]:
        print("  could not download " + f, flush=True)
    if failed and len(failed) == len(todo) and not manifest["items"]:
        return 1  # nothing usable at all
    return 0


def is_google_folder(folder):
    return bool(folder) and os.path.exists(os.path.join(folder, MANIFEST))


def default_folder():
    pics = os.path.join(os.path.expanduser("~"), "Pictures")
    return os.path.join(pics if os.path.isdir(pics) else os.path.expanduser("~"), "Google Photos (SnapSort Organizer)")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "import":
        try:
            sys.exit(import_session(sys.argv[2], sys.argv[3]))
        except RuntimeError as e:
            print(str(e), flush=True)
            sys.exit(1)
    sys.exit(__doc__)
