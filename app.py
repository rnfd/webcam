#!/usr/bin/env python3
"""Two Reolink cameras -> WebRTC pages (sub-second), each camera independent.

MediaMTX (see mediamtx.yml) serves each camera as its own WebRTC stream, and
this app serves one link per camera ("/1", "/2") plus a combined page ("/") that
shows both — live viewing is always per-camera, never the stack. Recording is
the other way round: it stream-copies the stacked `composite` path, so a session
lands on disk and in Google Drive as one video with both cameras in it.

The app also runs the record button, Telegram control, motion alerts, camera
health alerts, and delivery to Telegram/Drive.

Run:   ./run.sh              (starts MediaMTX + this app)
Env:   PORT WEBRTC_PORT REC_DIR FFMPEG CAM_IPS
"""
import atexit, glob, json, os, shutil, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT        = int(os.environ.get("PORT", "8088"))
WEBRTC_PORT = int(os.environ.get("WEBRTC_PORT", "8889"))
REC_DIR = os.environ.get("REC_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings"))
os.makedirs(REC_DIR, exist_ok=True)

# Two switches live as files in a directory shared with the MediaMTX service, so
# both processes (and a restart of either) agree on what is on:
#   disabled  present -> cameras are off the air; the publishers never open an
#                        RTSP session, so no video leaves the cameras at all
#   follow    present -> detection alerts go to Telegram
#   exposed   present -> the site is public (/expose): nginx opens the vhost the
#                        Cloudflare tunnel lands on; absent, that vhost refuses
STATE_DIR = os.environ.get("CAMS_STATE",
              os.path.join(os.path.dirname(os.path.abspath(__file__)), "state"))

def _flag(name): return os.path.join(STATE_DIR, name)
def _flag_get(name): return os.path.exists(_flag(name))
def _flag_set(name, on):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if on:
            with open(_flag(name), "w"): pass
        else:
            try: os.remove(_flag(name))
            except FileNotFoundError: pass
        return True
    except OSError as e:
        print(f"state: cannot update {name}: {e}"); return False

def cams_enabled(): return not _flag_get("disabled")
def follow_on():    return _flag_get("follow")
def exposed_on():   return _flag_get("exposed")

# -------------------- /expose and /close: the site without the VPN --------------------
# WEB_HOST normally resolves to LAN_IP, reachable only over the LAN or the VPN.
# /expose repoints that Cloudflare DNS record at the tunnel CF_TUNNEL (a proxied
# CNAME) and raises the "exposed" flag that opens the tunnel's nginx vhost;
# /close puts the LAN address back, drops the flag and kicks every WebRTC
# viewer through the MediaMTX API (VPN viewers reconnect on their own; public
# ones cannot). The DNS token arrives as a systemd credential.
WEB_HOST  = os.environ.get("WEB_HOST")
LAN_IP    = os.environ.get("LAN_IP")
CF_ZONE   = os.environ.get("CF_ZONE")
CF_TUNNEL = os.environ.get("CF_TUNNEL")
MTX_API   = os.environ.get("MTX_API")                    # e.g. http://127.0.0.1:9997
# The WebRTC media needs a direct path to this box, which sits behind the
# router's NAT: /expose forwards MEDIA_PORT (TCP+UDP) to LAN_IP on a Linksys
# router through its JNAP API and /close removes the rule again. ROUTER_PASS
# is the router's admin password (root-only env file); unset = no router step.
ROUTER_URL  = os.environ.get("ROUTER_URL")                # e.g. http://192.168.1.1
ROUTER_USER = os.environ.get("ROUTER_USER", "admin")
ROUTER_PASS = os.environ.get("ROUTER_PASS")
MEDIA_PORT  = int(os.environ.get("WEBRTC_MEDIA_PORT", "8189"))
CF_TOKEN_FILE = os.environ.get("CF_TOKEN_FILE") or (
    os.path.join(os.environ["CREDENTIALS_DIRECTORY"], "cf-token")
    if os.environ.get("CREDENTIALS_DIRECTORY") else None)

def _cf_token():
    try:
        with open(CF_TOKEN_FILE) as f: return f.read().strip()
    except (OSError, TypeError): return None

def _expose_ready(): return bool(WEB_HOST and LAN_IP and CF_ZONE and CF_TUNNEL and _cf_token())

def _cf_api(method, path, body=None):
    import urllib.request, urllib.error
    req = urllib.request.Request("https://api.cloudflare.com/client/v4" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + _cf_token(),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r: res = json.load(r)
    except urllib.error.HTTPError as e:
        try: res = json.load(e)
        except Exception: raise RuntimeError(f"Cloudflare HTTP {e.code}") from None
    if not res.get("success"):
        raise RuntimeError("; ".join(str(x.get("message", x)) for x in res.get("errors", []))
                           or "Cloudflare refused")
    return res["result"]

_CF_ZONE_ID = None
def _cf_zone_id():
    global _CF_ZONE_ID
    if not _CF_ZONE_ID:
        zones = _cf_api("GET", f"/zones?name={CF_ZONE}")
        if not zones: raise RuntimeError(f"zone {CF_ZONE} not visible to the token")
        _CF_ZONE_ID = zones[0]["id"]
    return _CF_ZONE_ID

def _dns_point(public):
    """Point WEB_HOST at the tunnel (public) or back at LAN_IP (VPN only).
    Idempotent. Returns None, or a string saying what went wrong."""
    want = ({"type": "CNAME", "name": WEB_HOST, "content": f"{CF_TUNNEL}.cfargotunnel.com",
             "ttl": 1, "proxied": True} if public else
            {"type": "A", "name": WEB_HOST, "content": LAN_IP, "ttl": 60, "proxied": False})
    try:
        zid = _cf_zone_id()
        recs = [r for r in _cf_api("GET", f"/zones/{zid}/dns_records?name={WEB_HOST}")
                if r["type"] in ("A", "AAAA", "CNAME")]
        if len(recs) == 1 and all(recs[0][k] == want[k] for k in ("type", "content", "proxied")):
            return None
        for r in recs[1:]:                       # a CNAME cannot share its name with anything
            _cf_api("DELETE", f"/zones/{zid}/dns_records/{r['id']}")
        if recs:
            try: _cf_api("PUT", f"/zones/{zid}/dns_records/{recs[0]['id']}", want)
            except RuntimeError:                  # some type changes need delete + create
                _cf_api("DELETE", f"/zones/{zid}/dns_records/{recs[0]['id']}")
                _cf_api("POST", f"/zones/{zid}/dns_records", want)
        else:
            _cf_api("POST", f"/zones/{zid}/dns_records", want)
        print(f"dns: {WEB_HOST} -> {want['type']} {want['content']}"
              f"{' (proxied)' if want['proxied'] else ''}")
        return None
    except Exception as e:
        print(f"dns: could not point {WEB_HOST}: {e}"); return str(e)

def _jnap(action, body=None):
    """One Linksys JNAP call (HTTP POST, action in a header, JSON in and out)."""
    import urllib.request, base64
    auth = base64.b64encode(f"{ROUTER_USER}:{ROUTER_PASS}".encode()).decode()
    req = urllib.request.Request(ROUTER_URL.rstrip("/") + "/JNAP/", data=json.dumps(body or {}).encode(),
                                 headers={"X-JNAP-Action": "http://linksys.com/jnap/" + action,
                                          "X-JNAP-Authorization": "Basic " + auth,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r: res = json.load(r)
    if res.get("result") != "OK":
        raise RuntimeError(f"{action.rsplit('/', 1)[-1]} {res.get('result')} {res.get('error', '')}".strip())
    return res.get("output", {})

def _router_ready(): return bool(ROUTER_URL and ROUTER_PASS and LAN_IP)

def _router_rule(r): return r.get("externalPort") == MEDIA_PORT and r.get("internalServerIPAddress") == LAN_IP

def _router_forward(on):
    """Add (on) or drop the router's single-port forward of MEDIA_PORT to this box.
    The API replaces the whole list, so the other rules are read first and written
    back untouched. Idempotent. Returns None, or a string saying what went wrong."""
    try:
        rules = _jnap("firewall/GetSinglePortForwardingRules").get("rules", [])
        have = [r for r in rules if _router_rule(r)]
        if on and len(have) == 1 and have[0].get("isEnabled") and have[0].get("protocol") == "Both" \
                and have[0].get("internalPort") == MEDIA_PORT:
            return None
        if not on and not have: return None
        want = [r for r in rules if not _router_rule(r)]
        if on: want.append({"isEnabled": True, "externalPort": MEDIA_PORT, "internalPort": MEDIA_PORT,
                            "protocol": "Both", "internalServerIPAddress": LAN_IP,
                            "description": "cams webrtc (/expose)"})
        _jnap("firewall/SetSinglePortForwardingRules", {"rules": want})
        print(f"router: forward {MEDIA_PORT} -> {LAN_IP} {'added' if on else 'removed'}")
        return None
    except Exception as e:
        print(f"router: could not {'add' if on else 'remove'} the forward: {e}"); return str(e)

def _router_forwarded():
    """Is the media port forwarded here right now? None when unknown."""
    try: return any(r.get("isEnabled") for r in _jnap("firewall/GetSinglePortForwardingRules").get("rules", []) if _router_rule(r))
    except Exception: return None

def _mtx_kick_viewers():
    """Drop every WebRTC viewer. Used by /close: the public ones cannot come back
    once the vhost is shut; VPN viewers reconnect by themselves in a few seconds."""
    import urllib.request
    if not MTX_API: return 0
    n = 0
    try:
        with urllib.request.urlopen(MTX_API + "/v3/webrtcsessions/list", timeout=5) as r:
            items = json.load(r).get("items", [])
        for sess in items:
            try:
                urllib.request.urlopen(urllib.request.Request(
                    MTX_API + f"/v3/webrtcsessions/kick/{sess['id']}", method="POST"), timeout=5)
                n += 1
            except Exception as e: print(f"mediamtx: kick {sess.get('id')} failed: {e}")
    except Exception as e: print("mediamtx: could not list viewers:", e)
    return n

def find_ffmpeg():
    if os.environ.get("FFMPEG"): return os.environ["FFMPEG"]
    w = shutil.which("ffmpeg")
    if w: return w
    hits = sorted(glob.glob("/nix/store/*ffmpeg*-bin/bin/ffmpeg"))
    if hits: return hits[0]
    sys.exit("ffmpeg not found: set FFMPEG=/path/to/ffmpeg")
FFMPEG = find_ffmpeg()

def find_mediamtx():
    if os.environ.get("MEDIAMTX"): return os.environ["MEDIAMTX"]
    w = shutil.which("mediamtx")
    if w: return w
    hits = sorted(glob.glob("/nix/store/*mediamtx*/bin/mediamtx"))
    return hits[-1] if hits else None

def _port_open(port, host="127.0.0.1"):
    import socket
    s = socket.socket(); s.settimeout(0.4)
    try: s.connect((host, port)); return True
    except OSError: return False
    finally: s.close()

_MEDIAMTX_PROC = None
def ensure_mediamtx():
    """Start MediaMTX (the WebRTC server) if it isn't already listening.
    Skipped when CAMS_NO_AUTOMTX is set (e.g. systemd manages MediaMTX)."""
    global _MEDIAMTX_PROC
    if os.environ.get("CAMS_NO_AUTOMTX"):
        print("CAMS_NO_AUTOMTX set; assuming MediaMTX is managed externally."); return
    if _port_open(WEBRTC_PORT):
        print(f"MediaMTX already running on :{WEBRTC_PORT}"); return
    mmx = find_mediamtx()
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mediamtx.yml")
    if not mmx or not os.path.isfile(cfg):
        print("WARNING: MediaMTX not found or mediamtx.yml missing; WebRTC will not work.")
        print("         Install: nix profile install nixpkgs#mediamtx  (or run ./run.sh)")
        return
    print(f"Starting MediaMTX: {mmx} {cfg}")
    _MEDIAMTX_PROC = subprocess.Popen([mmx, cfg])
    for _ in range(50):
        if _port_open(WEBRTC_PORT): print("MediaMTX is up."); return
        if _MEDIAMTX_PROC.poll() is not None:
            print("WARNING: MediaMTX exited during startup."); return
        time.sleep(0.2)
    print("WARNING: MediaMTX did not open its port in time.")

# ---------------------------- Telegram delivery ----------------------------
import uuid as _uuid
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TG_CHAT  = os.environ.get("TELEGRAM_CHAT_ID")
TG_LIMIT = 50 * 1024 * 1024   # cloud Bot API upload cap

# Cameras for motion detection / per-camera snapshots. CAM_IPS="ip=Name,ip=Name"
CAM_USER = os.environ.get("CAM_USER", "admin")
CAM_PASS = os.environ.get("CAM_PASS", "")
CAMS = []
for _it in os.environ.get("CAM_IPS", "").split(","):
    _ip, _, _nm = _it.strip().partition("=")
    if _ip: CAMS.append((_ip, _nm or _ip))
MOTION_COOLDOWN = int(os.environ.get("MOTION_COOLDOWN", "30"))  # seconds per event
PLAYBACK   = os.environ.get("PLAYBACK_URL", "http://localhost:9996")  # MediaMTX playback
# Which recorded path motion clips are cut from ({n} = camera number). The sub
# paths carry the rolling buffer, so clips stay cheap to cut and small to send.
CLIP_PATH  = os.environ.get("CLIP_PATH", "cam{n}sub")
MOTION_PRE  = int(os.environ.get("MOTION_PRE", "3"))   # seconds before detection
MOTION_POST = int(os.environ.get("MOTION_POST", "3"))  # seconds after detection
# motion-clip compression (re-encode to keep the Telegram video small)
CLIP_CRF   = os.environ.get("CLIP_CRF", "30")      # higher = smaller/lower quality
CLIP_WIDTH = os.environ.get("CLIP_WIDTH", "480")   # scale to this width
CLIP_ABR   = os.environ.get("CLIP_ABR", "24k")     # audio bitrate (mono)

def _fmt_size(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024: return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"

def _tg_api(method, boundary=None, body=None, fields=None):
    import urllib.request, urllib.parse
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    if body is not None:
        req = urllib.request.Request(url, data=body,
              headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(fields or {}).encode())
    import json as _json
    r = _json.loads(urllib.request.urlopen(req, timeout=120).read().decode())
    if method in _TG_SENDS and r.get("ok"): _tg_remember(r["result"])
    return r

# /clear needs to know what the bot has posted, and the Bot API offers no way to
# read a chat's history — so every message the bot sends is noted here (chat,
# message id, time) in the state dir, where it survives a restart. Telegram only
# lets a bot delete a message for 48 hours, so older entries are dropped.
_TG_SENDS = {"sendMessage", "sendPhoto", "sendVideo"}
_TG_SENT = "tg-sent.json"
TG_DELETE_WINDOW = 48 * 3600
_tg_sent_lock = threading.Lock()

def _tg_sent_load():
    try:
        with open(_flag(_TG_SENT)) as f: return json.load(f)
    except Exception:
        return []

def _tg_sent_save(items):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _flag(_TG_SENT + ".tmp")
        with open(tmp, "w") as f: json.dump(items, f)
        os.replace(tmp, _flag(_TG_SENT))
    except OSError as e:
        print("telegram: cannot note sent message:", e)

def _tg_remember(msg):
    now = time.time()
    with _tg_sent_lock:
        items = [x for x in _tg_sent_load() if now - x[2] < TG_DELETE_WINDOW]
        items.append([msg["chat"]["id"], msg["message_id"], msg.get("date", now)])
        _tg_sent_save(items)

def _tg_clear():
    """Delete everything the bot has posted in this chat that can still be
    deleted. Returns (deleted, failed)."""
    now = time.time()
    with _tg_sent_lock:
        ids = sorted(x[1] for x in _tg_sent_load()
                     if str(x[0]) == str(TG_CHAT) and now - x[2] < TG_DELETE_WINDOW)
    done = []
    for i in range(0, len(ids), 100):                 # deleteMessages takes 100 at a time
        chunk = ids[i:i+100]
        try:
            if _tg_api("deleteMessages", fields={"chat_id": TG_CHAT,
                                                 "message_ids": json.dumps(chunk)}).get("ok"):
                done += chunk
        except Exception as e:
            print("telegram: delete failed:", e)
    with _tg_sent_lock:
        gone = set(done)
        _tg_sent_save([x for x in _tg_sent_load()
                       if not (str(x[0]) == str(TG_CHAT) and x[1] in gone)])
    return len(done), len(ids) - len(done)

def _tg_multipart(fields, file_field, filepath, ctype="video/mp4"):
    b = _uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    out += (f"--{b}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{os.path.basename(filepath)}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
    with open(filepath, "rb") as f: out += f.read()
    out += f"\r\n--{b}--\r\n".encode()
    return b, bytes(out)

def _dur(elapsed):
    return f"{elapsed//60:02d}:{elapsed%60:02d}"

def _tg_send_link(base, elapsed, link):
    text = (f"📹 {base}  ({_dur(elapsed)})\n{link}" if link
            else f"📹 {base}  ({_dur(elapsed)})\nSaved to Drive (link unavailable).")
    r = _tg_api("sendMessage", fields={"chat_id": TG_CHAT, "text": text})
    print("telegram: link sent" if r.get("ok") else f"telegram: API error {r}")

def _tg_send_initial(base, elapsed):
    """Post the first 'uploading' message; return its message_id (or None)."""
    r = _tg_api("sendMessage", fields={"chat_id": TG_CHAT,
                "text": f"📹 {base}  ({_dur(elapsed)})\nUploading…  ⏳"})
    return r.get("result", {}).get("message_id") if r.get("ok") else None

def _tg_edit(msg_id, text):
    try:
        _tg_api("editMessageText", fields={"chat_id": TG_CHAT, "message_id": msg_id,
                "text": text, "disable_web_page_preview": "true"})
    except Exception:
        pass

def _tg_finalize(msg_id, base, elapsed, link, size=None):
    sz = f" · {_fmt_size(size)}" if size else ""
    text = (f"📹 {base}  ({_dur(elapsed)}{sz})\n✅ Uploaded\n{link}" if link
            else f"📹 {base}  ({_dur(elapsed)}{sz})\n✅ Saved to Drive (link unavailable).")
    _tg_api("editMessageText", fields={"chat_id": TG_CHAT, "message_id": msg_id,
            "text": text})
    print("telegram: finalized with link" if link else "telegram: finalized")

class _Prog:
    """Throttled progress callback that edits the Telegram message."""
    def __init__(self, msg_id, base, elapsed):
        self.msg_id, self.base, self.elapsed = msg_id, base, elapsed
        self.last_t, self.last_pct = 0.0, -1
    def __call__(self, pct):
        now = time.time()
        if pct == self.last_pct: return
        if pct < 100 and now - self.last_t < 3: return   # <=1 edit / 3s
        self.last_t, self.last_pct = now, pct
        filled = pct * 12 // 100
        bar = "█" * filled + "░" * (12 - filled)
        _tg_edit(self.msg_id,
                 f"📹 {self.base}  ({_dur(self.elapsed)})\nUploading  [{bar}] {pct}%")

# ---------------------------- Google Drive (rclone) ----------------------------
RCLONE       = os.environ.get("RCLONE") or shutil.which("rclone")
GDRIVE_REMOTE = os.environ.get("GDRIVE_REMOTE")          # e.g. "gdrive:CameraClips"
RCLONE_CONF  = os.environ.get("RCLONE_CONFIG")           # e.g. /etc/cams/rclone.conf

def _gdrive_enabled():
    return bool(RCLONE and GDRIVE_REMOTE and RCLONE_CONF and os.path.isfile(RCLONE_CONF))

def _gdrive_upload(mp4, on_progress=None):
    """Upload mp4 with progress; make it public; return the shareable link."""
    import re
    dest = GDRIVE_REMOTE.rstrip("/")
    name = os.path.basename(mp4)
    cmd = [RCLONE, "--config", RCLONE_CONF, "copy", mp4, dest + "/",
           "--stats", "1s", "--stats-one-line"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        for line in proc.stderr:                     # rclone stats -> stderr
            m = re.search(r"(\d+)%", line)
            if m and on_progress:
                try: on_progress(int(m.group(1)))
                except Exception: pass
        proc.wait(timeout=1800)
        if proc.returncode != 0:
            print("gdrive: upload failed rc", proc.returncode); return None
    except Exception as e:
        print("gdrive: upload failed:", e); return None
    if on_progress:
        try: on_progress(100)
        except Exception: pass
    # create a public ("anyone with the link") share link (retry for listing lag)
    for _ in range(6):
        try:
            link = subprocess.run([RCLONE, "--config", RCLONE_CONF, "link",
                                   f"{dest}/{name}"],
                                  capture_output=True, text=True, timeout=60).stdout.strip()
        except Exception:
            link = ""
        if link.startswith("http"):
            print(f"gdrive: uploaded {name}"); return link
        time.sleep(2)
    print(f"gdrive: uploaded {name} (link not resolved yet)"); return None

# ------------------------------ post-record fan-out ------------------------------
def _tg_enabled():
    return bool(TG_TOKEN and TG_CHAT)

def _any_sink():
    return _tg_enabled() or _gdrive_enabled()

def _post_record(mkv_path, elapsed):
    """Remux the finished recording to MP4 once, then fan out to each sink."""
    base = os.path.basename(mkv_path)
    mp4 = (mkv_path[:-4] if mkv_path.endswith(".mkv") else mkv_path) + ".mp4"
    try:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", mkv_path,
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
                        "-movflags", "+faststart", mp4],
                       check=True, timeout=600)
    except Exception as e:
        print("post-record: remux failed:", e); return
    try:
        _deliver(mp4, elapsed)
    finally:
        try: os.path.exists(mp4) and os.remove(mp4)
        except OSError: pass

def _deliver(mp4, elapsed):
    """Upload one finished mp4 to Drive and post its link to Telegram, with a
    progress bar on the way. Used by both recordings and timelapses."""
    base = os.path.basename(mp4)
    size = os.path.getsize(mp4)
    tg, gd = _tg_enabled(), _gdrive_enabled()
    msg_id = _tg_send_initial(base, elapsed) if tg else None
    prog = _Prog(msg_id, base, elapsed) if (tg and msg_id) else None
    link = _gdrive_upload(mp4, on_progress=prog) if gd else None
    if tg:
        try:
            if msg_id: _tg_finalize(msg_id, base, elapsed, link, size)
            else:      _tg_send_link(base, elapsed, link)
        except Exception as e: print("telegram: failed:", e)
    return link

# ---------------------------------------------------------------------------
MAX_REC_SECONDS = int(os.environ.get("REC_MAX_SECONDS", str(8 * 3600)))  # 8h cap

_PTZ_CAP = {}
def _view_cams():
    """Panes shown on the page, in CAM_IPS order. Each camera has two MediaMTX
    paths: "cam1" is the main stream (full camera quality, stream-copied) and
    "cam1sub" the small one, which the pane's HD/SD button switches to when the
    link is too thin for the real thing. Falls back to two cameras."""
    if CAMS:
        if not _PTZ_CAP:                       # probe each camera once, at startup
            _PTZ_CAP.update({ip: _cam_has_ptz(ip) for ip, _nm in CAMS})
        cams = [(nm, _PTZ_CAP.get(ip, False)) for ip, nm in CAMS]
    else:
        cams = [("Camera 1", False), ("Camera 2", False)]
    return [{"path": f"cam{i}", "sd": f"cam{i}sub", "name": nm, "ptz": ptz}
            for i, (nm, ptz) in enumerate(cams, 1)]

def _cam_paths():
    """Record the single stacked composite (resilient: black pane if a cam is down)."""
    return [("composite", "Cameras")]

def _stop_ffmpeg(p):
    """Ask ffmpeg to finish its file cleanly; force it only if it will not."""
    if not p or p.poll() is not None: return
    try:
        if p.stdin: p.stdin.write(b"q"); p.stdin.flush()
    except Exception: pass
    try: p.send_signal(signal.SIGINT)
    except Exception: pass
    try: p.wait(timeout=8)
    except subprocess.TimeoutExpired:
        p.terminate()
        try: p.wait(timeout=4)
        except subprocess.TimeoutExpired: p.kill()

class Recorder:
    """Records each camera independently, so one camera failing never stops the
    others. A session is 'recording' while any per-camera recorder is alive."""
    def __init__(self):
        self.lock = threading.Lock(); self.sessions = []; self.started = None; self._timer = None
    def _arm_cap(self):
        self._disarm_cap()
        self._timer = threading.Timer(MAX_REC_SECONDS, self._auto_stop)
        self._timer.daemon = True; self._timer.start()
    def _disarm_cap(self):
        if self._timer:
            try: self._timer.cancel()
            except Exception: pass
            self._timer = None
    def _alive(self):
        return any(s["proc"] and s["proc"].poll() is None for s in self.sessions)
    def _auto_stop(self):
        if self._alive():
            print(f"recorder: reached {MAX_REC_SECONDS}s cap, auto-stopping"); self.stop()
    def _state(self):
        n = sum(1 for s in self.sessions if s["proc"] and s["proc"].poll() is None)
        rec = n > 0
        return {"recording": rec, "cams": n,
                "elapsed": int(time.time()-self.started) if (rec and self.started) else 0,
                "enabled": cams_enabled(), "follow": follow_on()}
    def status(self):
        with self.lock:
            return self._state()
    def start(self):
        with self.lock:
            if self._alive(): return self._state()
            if not cams_enabled():
                return {"recording": False, "cams": 0, "elapsed": 0,
                        "error": "cameras are disabled"}
            ts = time.strftime("%Y%m%d_%H%M%S"); self.sessions = []
            for path, name in _cam_paths():
                mkv = os.path.join(REC_DIR, f"rec_{ts}_{path}.mkv")
                cmd = [FFMPEG, "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp",
                       "-i", f"rtsp://localhost:8554/{path}", "-map", "0", "-c", "copy",
                       "-f", "matroska", mkv]
                self.sessions.append({"path": path, "name": name, "mkv": mkv,
                                      "proc": subprocess.Popen(cmd, stdin=subprocess.PIPE)})
            time.sleep(1.5)                                   # let them connect
            for s in list(self.sessions):                    # drop cameras that didn't start
                if s["proc"].poll() is not None:
                    try: os.path.exists(s["mkv"]) and os.path.getsize(s["mkv"]) == 0 and os.remove(s["mkv"])
                    except OSError: pass
                    self.sessions.remove(s)
            if not self.sessions:
                self.started = None
                return {"recording": False, "cams": 0, "elapsed": 0, "error": "no cameras available"}
            self.started = time.time(); self._arm_cap()
            return self._state()
    def stop(self):
        with self.lock:
            self._disarm_cap()
            sess = self.sessions; st = self.started
            self.sessions = []; self.started = None
        if not sess: return {"recording": False, "cams": 0, "elapsed": 0}
        elapsed = int(time.time()-st) if st else 0
        for s in sess:
            _stop_ffmpeg(s["proc"])
            # upload each camera's file (if it captured anything)
            if _any_sink() and os.path.exists(s["mkv"]) and os.path.getsize(s["mkv"]) > 2000:
                threading.Thread(target=_post_record, args=(s["mkv"], elapsed),
                                 daemon=True).start()
        return {"recording": False, "cams": 0, "elapsed": 0}
REC = Recorder()

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__TITLE__</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#000;color:#eee;font:15px/1.4 -apple-system,system-ui,sans-serif}
/* One pane per camera. Narrow screens stack them, wide screens sit them side by side. */
.grid{position:fixed;inset:0 0 64px 0;display:grid;gap:6px;padding:6px;
  grid-template-columns:1fr;grid-auto-rows:1fr}
@media (min-width:900px){ .grid{grid-template-columns:repeat(2,1fr);grid-auto-rows:1fr} }
/* One camera on its own page: full width and height, no empty second column. */
.grid.one{grid-template-columns:1fr;grid-auto-rows:1fr}
.pane{position:relative;min-width:0;min-height:0;background:#000;border-radius:6px;overflow:hidden}
.pane video{width:100%;height:100%;object-fit:contain;background:#000;display:block}
.lbl{position:absolute;left:8px;top:8px;padding:3px 9px;border-radius:999px;
  background:rgba(0,0,0,.55);color:#ddd;font-size:12px;pointer-events:none;z-index:2}
.btns{position:absolute;right:8px;top:8px;display:flex;gap:6px;z-index:2}
/* Cameras off: nothing is published, so the pane draws its own panel rather than
   playing a stream of a title card. Behind the label and buttons (z-index). */
.pane.off::after{content:'⏸ cameras off';position:absolute;inset:0;z-index:1;
  display:flex;align-items:center;justify-content:center;
  background:#000;color:#6b7280;font:600 15px system-ui;letter-spacing:.02em}
.btns button{font:600 14px system-ui;color:#fff;background:rgba(0,0,0,.55);border:0;
  border-radius:999px;padding:6px 10px;cursor:pointer}
.btns button.on{background:#2563eb}
.msg{position:absolute;left:0;right:0;bottom:8px;text-align:center;color:#9ca3af;font-size:12px}
/* Pan/tilt pad: press and hold an arrow to move, release to stop. */
.pad{position:absolute;left:8px;bottom:8px;display:grid;gap:4px;
  grid-template-columns:repeat(3,40px);grid-template-rows:repeat(3,40px);touch-action:none}
.pad button{font:600 17px system-ui;color:#fff;background:rgba(0,0,0,.55);border:0;border-radius:8px;
  cursor:pointer;padding:0;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.pad button:active{background:#2563eb}
.pad .u{grid-area:1/2}.pad .l{grid-area:2/1}.pad .r{grid-area:2/3}.pad .d{grid-area:3/2}
.pad[hidden]{display:none}
.msg.err{color:#f87171}
.bar{position:fixed;left:0;right:0;bottom:0;height:64px;display:flex;gap:12px;align-items:center;
  justify-content:center;background:#0b0b0b;border-top:1px solid #222;padding:0 12px;padding-bottom:env(safe-area-inset-bottom)}
#rec{font:600 16px system-ui;color:#fff;background:#dc2626;border:0;border-radius:999px;
  padding:8px 24px;cursor:pointer;min-width:170px;display:flex;flex-direction:column;
  align-items:center;gap:1px;line-height:1.2}
#rec.on{background:#374151}
#rsub{font:500 11px system-ui;opacity:.8;font-variant-numeric:tabular-nums}
#rsub[hidden]{display:none}
#off{color:#fbbf24;font:600 13px system-ui}
#off[hidden]{display:none}
.nav{display:flex;gap:8px}
.nav a{color:#cbd5e1;text-decoration:none;font:600 14px system-ui;background:#1f2937;border-radius:999px;padding:10px 14px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#dc2626;margin-right:8px;vertical-align:middle;animation:blink 1s steps(2,start) infinite}
@keyframes blink{to{opacity:.2}}
#err{position:fixed;left:8px;right:8px;bottom:70px;color:#f87171;font-size:12px;text-align:center}
</style></head><body>
<div class="grid" id="grid"></div>
<div class="bar"><span id="off" hidden>⏸ cameras off</span>__NAV__<button id="rec"__RECATTR__><span id="rlab">● Record</span><span id="rsub" hidden></span></button></div>
<div id="err"></div>
<script>
var CAMS=__CAMS__, WHEP_BASE=__WHEP_BASE__,
    grid=document.getElementById('grid'),rec=document.getElementById('rec'),
    rlab=document.getElementById('rlab'),rsub=document.getElementById('rsub'),
    off=document.getElementById('off'),err=document.getElementById('err'),
    players=[],busy=false;
if(!window.RTCPeerConnection){ err.textContent='this browser has no WebRTC support'; }
function note(p,m,bad){ p.msg.textContent=m; p.msg.className='msg'+(bad?' err':''); }
// Nothing is published while the cameras are off, so there is no stream to play
// and no point reconnecting at one: drop the peer connections and let the panes
// show their own panel. Nulling p.pc makes every in-flight callback stale, which
// is what cancels the reconnect timers already in the air.
var camsOff=false;
function teardown(){
  players.forEach(function(p){
    try{ p.pc && p.pc.close(); }catch(_){}
    p.pc=null; p.video.srcObject=null; note(p,'');
    p.video.parentNode.classList.add('off');
  });
}
function restore(){
  players.forEach(function(p){
    p.video.parentNode.classList.remove('off');
    note(p,'connecting…'); setup(p);
  });
}
// One independent WHEP player per camera: its own peer connection and its own
// reconnect loop, so one camera dropping never disturbs the other pane.
function setup(p){
  if(camsOff) return;
  var pc=new RTCPeerConnection({iceServers:[]}), path=p.path;
  p.pc=pc;
  function stale(){ return p.pc!==pc || p.path!==path; }
  pc.addTransceiver('video',{direction:'recvonly'});
  pc.addTransceiver('audio',{direction:'recvonly'});
  pc.ontrack=function(e){ if(stale())return; try{e.receiver.playoutDelayHint=0;}catch(_){}
    if(p.video.srcObject!==e.streams[0]) p.video.srcObject=e.streams[0];
    p.video.play().catch(function(){});
    if(sndPref()===p.hd && p.video.muted) listen(p,true); };   // the camera you chose
  pc.onconnectionstatechange=function(){
    if(stale()) return;
    if(pc.connectionState==='connected'){ note(p,''); }
    else if(pc.connectionState==='failed'||pc.connectionState==='disconnected'){
      note(p,'reconnecting…');
      setTimeout(function(){ if(stale())return; try{pc.close()}catch(_){}; setup(p); },1500);
    }
  };
  (async function(){
    try{
      var offer=await pc.createOffer(); await pc.setLocalDescription(offer);
      await new Promise(function(res){ if(pc.iceGatheringState==='complete')return res();
        var t=setTimeout(res,1500);
        pc.onicegatheringstatechange=function(){ if(pc.iceGatheringState==='complete'){clearTimeout(t);res();} }; });
      if(stale()||camsOff){ try{pc.close()}catch(_){}; return; }   // switched off mid-gather
      var resp=await fetch(WHEP_BASE+'/'+path+'/whep',{method:'POST',
        headers:{'Content-Type':'application/sdp'},body:pc.localDescription.sdp});
      if(stale()) { try{pc.close()}catch(_){}; return; }
      if(!resp.ok){ note(p,'signaling HTTP '+resp.status,1);
        setTimeout(function(){ if(stale())return; try{pc.close()}catch(_){}; setup(p); },3000); return; }
      await pc.setRemoteDescription({type:'answer',sdp:await resp.text()});
    }catch(e){ if(stale())return; note(p,'error: '+e.message,1);
      setTimeout(function(){ if(stale())return; try{pc.close()}catch(_){}; setup(p); },3000); }
  })();
}
// Sound comes from exactly one camera at a time: unmuting a pane mutes the rest.
// The choice is remembered per browser (by camera, not by HD/SD path) and comes
// back on the next visit, on the combined page and the single-camera links alike.
var SND_KEY='cam-sound', armed=false;
function sndPref(){ try{ return localStorage.getItem(SND_KEY)||''; }catch(_){ return ''; } }
function sndSave(v){ try{ localStorage.setItem(SND_KEY,v); }catch(_){} }
// Browsers refuse to start unmuted audio without a gesture; if that happens,
// wait for the next tap anywhere and turn the sound on then.
function armGesture(q){
  note(q,'tap anywhere for sound');
  if(armed) return; armed=true;
  var h=function(){ document.removeEventListener('click',h); document.removeEventListener('touchend',h);
    armed=false; q.video.play().then(function(){ note(q,''); }).catch(function(){}); };
  document.addEventListener('click',h); document.addEventListener('touchend',h);
}
function listen(p, want){
  var on = (want===undefined) ? p.video.muted : want;
  players.forEach(function(q){
    var live = on && q===p;
    q.video.muted=!live; q.spk.classList.toggle('on',live);
    q.spk.textContent = live ? '🔇' : '🔊';
    if(live){ q.video.volume=1;
      var pr=q.video.play(); if(pr && pr.catch) pr.catch(function(){ armGesture(q); }); }
  });
  sndSave(on ? p.hd : '');
}
CAMS.forEach(function(c){
  var pane=document.createElement('div'); pane.className='pane';
  pane.innerHTML='<video playsinline webkit-playsinline autoplay muted></video>'+
    '<span class="lbl"></span><div class="btns">'+
    (c.ptz?'<button class="mv" title="pan / tilt">✥</button>':'')+
    (CAMS.length>1?'<button class="ex" title="fill the window">⤢</button>':'')+
    '<button class="q">HD</button><button class="spk">🔊</button></div>'+
    (c.ptz?'<div class="pad"><button class="u">▲</button><button class="l">◀</button>'+
           '<button class="r">▶</button><button class="d">▼</button></div>':'')+
    '<div class="msg">connecting…</div>';
  pane.querySelector('.lbl').textContent=c.name;
  grid.appendChild(pane);
  var p={hd:c.path,sd:c.sd||c.path,path:c.path,name:c.name,n:players.length+1,
         video:pane.querySelector('video'),q:pane.querySelector('.q'),
         spk:pane.querySelector('.spk'),msg:pane.querySelector('.msg'),
         ex:pane.querySelector('.ex')};
  // ⤢ opens this camera's own page, where it gets the whole window.
  if(p.ex) p.ex.onclick=function(){ location.href='/'+p.n; };
  p.pad=pane.querySelector('.pad'); p.mv=pane.querySelector('.mv');
  if(p.pad){
    if(p.mv) p.mv.onclick=function(){ p.pad.hidden=!p.pad.hidden; p.mv.classList.toggle('on',!p.pad.hidden); };
    if(p.mv) p.mv.classList.add('on');
    // Hold to move, release to stop. Pointer events cover mouse and touch; the
    // release is also caught on leave/cancel, and the server stops the camera on
    // its own if no release ever arrives.
    [['u','Up'],['d','Down'],['l','Left'],['r','Right']].forEach(function(b){
      var el=p.pad.querySelector('.'+b[0]);
      var go=function(e){ e.preventDefault(); move(p,b[1]); };
      var end=function(e){ e.preventDefault(); move(p,'Stop'); };
      el.addEventListener('pointerdown',go);
      ['pointerup','pointerleave','pointercancel'].forEach(function(ev){ el.addEventListener(ev,end); });
      el.addEventListener('contextmenu',function(e){ e.preventDefault(); });
    });
  }
  p.q.classList.add('on');
  // HD is the camera's main stream, copied through untouched; SD is its small
  // stream, for a link that cannot carry the real one. Switching restarts the
  // player on the other path.
  p.q.onclick=function(){
    p.path = (p.path===p.hd) ? p.sd : p.hd;
    p.q.textContent = (p.path===p.hd) ? 'HD' : 'SD';
    p.q.classList.toggle('on', p.path===p.hd);
    note(p,'switching…');
    try{ p.pc && p.pc.close(); }catch(_){}
    setup(p);
  };
  p.spk.onclick=function(){ listen(p); };
  players.push(p); setup(p);            // HD, autoplays muted; tap 🔊 for audio
});
if(CAMS.length<2) grid.classList.add('one');
function move(p,op){
  fetch('ptz?n='+p.n+'&op='+op,{method:'POST'})
    .then(function(r){ if(!r.ok) note(p,'move failed',1); else if(op!=='Stop') note(p,''); })
    .catch(function(e){ note(p,'move error: '+e.message,1); });
}
// On a single-camera page the arrow keys drive that camera: hold to move, release
// to stop (auto-repeat must not re-send, so track the key state).
if(players.length===1 && players[0].pad){
  var solo=players[0], KEYS={ArrowUp:'Up',ArrowDown:'Down',ArrowLeft:'Left',ArrowRight:'Right'}, held=null;
  document.addEventListener('keydown',function(e){
    var op=KEYS[e.key]; if(!op||held===e.key) return;
    e.preventDefault(); held=e.key; move(solo,op);
  });
  document.addEventListener('keyup',function(e){
    if(!KEYS[e.key]||held!==e.key) return;
    e.preventDefault(); held=null; move(solo,'Stop');
  });
  window.addEventListener('blur',function(){ if(held){ held=null; move(solo,'Stop'); } });
}
function fmt(s){var m=Math.floor(s/60),ss=s%60;return (m<10?'0':'')+m+':'+(ss<10?'0':'')+ss;}
// The button is its own status line: while recording it grows a timer under the
// label. Stopped, it just says what it does — that it is idle goes without saying.
function render(st){
  var nowOff = (st.enabled===false);      // /disable from Telegram shows up here too
  off.hidden = !nowOff; rec.disabled = nowOff;
  // The players follow the switch, on the edge only — not on every poll.
  if(nowOff!==camsOff){ camsOff=nowOff; if(nowOff) teardown(); else restore(); }
  if(st.recording){
    rlab.textContent='■ Stop'; rec.classList.add('on');
    rsub.innerHTML='<span class="dot"></span>REC '+fmt(st.elapsed); rsub.hidden=false;
  }else{
    rlab.textContent='● Record'; rec.classList.remove('on');
    rsub.textContent=''; rsub.hidden=true;
  }
}
function poll(){ fetch('status').then(r=>r.json()).then(render).catch(function(){}); }
rec.onclick=function(){
  if(busy)return; busy=true; rec.disabled=true;
  var on=rec.classList.contains('on');
  fetch(on?'record/stop':'record/start',{method:'POST'}).then(r=>r.json()).then(render)
    .catch(function(e){err.textContent='record error: '+e.message;})
    .finally(function(){busy=false;rec.disabled=false;});
};
poll(); setInterval(poll,2000);
</script></body></html>"""
# WHEP base. Behind the nginx proxy (WHEP_URL set) it's same-origin (""), so
# paths resolve to /cam1/whep etc. Standalone, target MediaMTX's port directly.
_whep_env = os.environ.get("WHEP_URL")
_whep_base = "''" if _whep_env else ("'http://'+location.hostname+':%d'" % WEBRTC_PORT)
PAGE = PAGE.replace("__WHEP_BASE__", _whep_base)

def _render(cams, title, nav, public=False):
    # Public (/expose) pages only watch: no record button, no pan/tilt pad.
    if public: cams = [{**c, "ptz": False} for c in cams]
    return (PAGE.replace("__CAMS__", json.dumps(cams))
                .replace("__TITLE__", title)
                .replace("__NAV__", nav)
                .replace("__RECATTR__", " hidden" if public else "")).encode()

def _build_pages(public=False):
    """One page per link: "/" shows every camera, "/1", "/2", ... show one each.
    They are separate links on purpose — open them in two windows, or watch both
    in one. Every page keeps the record button, which records the composite —
    except the public set, which is view-only."""
    cams = _view_cams()
    # No per-camera links down here: each pane's ⤢ button opens its own page.
    pages = {"/": _render(cams, "Cameras", "", public)}
    for i, c in enumerate(cams, 1):
        pages[f"/{i}"] = _render([c], c["name"], '<span class="nav"><a href="/">← both</a></span>', public)
    return pages
# Built in main(), not here: the pages carry each camera's PTZ capability, and
# asking the camera for it needs helpers defined further down this file.
PAGES = {}
PUBLIC_PAGES = {}   # the same links as served through the tunnel (/expose): watch only

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _json(self,obj,code=200):
        b=json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type","application/json"); self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def _public(self):
        # Set by the nginx vhost the Cloudflare tunnel lands on (/expose); that
        # vhost also refuses /record and /ptz itself, this is the second lock.
        return self.headers.get("X-Cams-Public") == "1"
    def do_POST(self):
        p, _, q = self.path.partition("?")
        if self._public(): self._json({"ok": False, "error": "view only"}, 403); return
        if p=="/record/start": self._json(REC.start()); return
        if p=="/record/stop":  self._json(REC.stop());  return
        if p=="/ptz":
            import urllib.parse
            a = urllib.parse.parse_qs(q)
            try: n = int(a.get("n", ["0"])[0])
            except ValueError: n = 0
            r = _ptz(n, a.get("op", [""])[0])
            self._json(r, 200 if r.get("ok") else 400); return
        self.send_error(404)
    def do_GET(self):
        p=self.path.split("?",1)[0]
        pages = PUBLIC_PAGES if self._public() else PAGES
        if p in pages:
            b=pages[p]; self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Content-Length",str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if p=="/status": self._json(REC.status()); return
        self.send_error(404)

def _shutdown():
    REC.stop()
    if _MEDIAMTX_PROC:
        try: _MEDIAMTX_PROC.terminate()
        except Exception: pass

# -------------------- Telegram command control (start/stop) --------------------
def _tg_reply(text):
    try: _tg_api("sendMessage", fields={"chat_id": TG_CHAT, "text": text})
    except Exception as e: print("telegram: reply failed:", e)

def _tg_set_commands():
    import json as _json
    cmds = [{"command": "enable",   "description": "Cameras on"},
            {"command": "disable",  "description": "Cameras off — nothing is captured"},
            {"command": "follow",   "description": "Alert me on person / pet / motion"},
            {"command": "unfollow", "description": "Stop detection alerts"},
            {"command": "expose",   "description": "Make the site public — no VPN needed, view only"},
            {"command": "close",    "description": "Back to VPN only"},
            {"command": "timelapse","description": "e.g. /timelapse 8h 100x — film 8h, play it 100x faster"},
            {"command": "record",   "description": "Start recording"},
            {"command": "stop",     "description": "Stop recording"},
            {"command": "status",   "description": "What is on right now"},
            {"command": "clear",    "description": "Delete the bot's messages from this chat"},
            {"command": "help",     "description": "What the commands do; /help timelapse for detail"}]
    try: _tg_api("setMyCommands", fields={"commands": _json.dumps(cmds)})
    except Exception: pass

def _tg_snap_reply(caption):
    """Send one composite snapshot (both cameras stacked); fall back to text."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".jpg"); os.close(fd)
    ok = False
    try:
        subprocess.run([FFMPEG, "-loglevel", "error", "-rtsp_transport", "tcp",
                        "-i", "rtsp://localhost:8554/composite", "-frames:v", "1",
                        "-q:v", "3", "-y", path], check=True, timeout=15)
        if os.path.getsize(path) > 0:
            with open(path, "rb") as f: ok = _tg_send_photo(f.read(), caption)
    except Exception as e:
        print("snapshot failed:", e)
    finally:
        try: os.remove(path)
        except OSError: pass
    if not ok: _tg_reply(caption)

# /help: one source for both the overview and the per-command detail
# (/help timelapse). Built at call time so the limits quoted are the live ones.
HELP_TOPICS = {   # command -> (one-liner for the overview, detail for /help <command>)
    "enable":   ("cameras on",
                 "Wakes both cameras: restores the settings /disable saved, recalls "
                 "each camera's saved view (twice — the head settles a few degrees off "
                 "on the first try) and restarts the feeds. The pictures are back "
                 "within a few seconds."),
    "disable":  ("cameras off, nothing is captured",
                 "Turns the cameras off as far as the network allows: saves where each "
                 "one is looking, tilts the lens down into the base, switches off the "
                 "camera's own recording, alerts and LEDs, and stops every feed. A "
                 "running recording or timelapse is finished first and sent as usual. "
                 "Nothing is captured until /enable."),
    "follow":   ("alert me on person / pet / motion",
                 "Sends a short clip here whenever a camera reports a person, a pet or "
                 "motion. Independent of recording — it works whether or not you are "
                 "recording. Needs the cameras on."),
    "unfollow": ("stop those alerts", "Stops the detection clips. Nothing else changes."),
    "expose":   ("make the site public, no VPN needed", None),   # detail below (quotes the host)
    "close":    ("back to VPN only", None),
    "record":   ("start recording", None),        # detail filled in below (needs the cap)
    "stop":     ("stop recording",
                 "Stops the recording. The file is finalised, converted to MP4, "
                 "uploaded to Google Drive, and the link is posted here with a "
                 "progress bar while it uploads."),
    "timelapse":("film for hours, play it back fast, e.g. /timelapse 8h 100x", None),
    "status":   ("what is on right now",
                 "Replies with the current picture from both cameras and whether the "
                 "cameras are on, whether alerts are on, whether a recording is "
                 "running and for how long, and the progress of a timelapse if one "
                 "is running."),
    "clear":    ("delete the bot's messages from this chat",
                 "Deletes every message the bot sent here in the last 48 hours — that "
                 "is as far back as Telegram lets a bot delete. In a group the bot "
                 "has to be an admin with \"delete messages\" to remove your /clear "
                 "as well."),
    "help":     ("this list; /help <command> for detail", None),
}
HELP_ALIASES = {"rec": "record", "start": "record", "tl": "timelapse"}
HELP_GROUPS = [("Cameras", ["enable", "disable"]),
               ("Alerts", ["follow", "unfollow"]),
               ("Access", ["expose", "close"]),
               ("Recording", ["record", "stop"]),
               ("Timelapse", ["timelapse"]),
               ("Info", ["status", "clear", "help"])]

def _help_detail(cmd):
    """Detail text for one command; the two that quote live limits are built here."""
    if cmd == "record":
        return ("Starts recording both cameras into one stacked video (camera 1 on top, "
                f"camera 2 below, sound mixed). Stops by itself after {MAX_REC_SECONDS // 3600}h "
                "or on /stop, then the clip goes to Google Drive and the link is posted "
                "here. /rec and /start do the same thing.")
    if cmd == "timelapse":
        return ("/timelapse <duration> <speed>x films both cameras and speeds the result "
                "up, one film per camera.\n\n"
                "Duration: 8h, 90m, 45s, 2h30m, or a bare number of hours "
                f"(up to {TL_MAX_HOURS:g}h).\n"
                f"Speed: 100x, 60x… — either order; leave it out for {TL_SPEED:g}x. "
                "The film runs duration ÷ speed long, so 8h at 100x gives 4m 48s "
                f"and keeps a frame every {100 / TL_FPS:.1f}s at 100x.\n\n"
                "/timelapse on its own shows progress: time filmed, time left, and "
                "how much film each camera has so far.\n"
                "/timelapse stop finishes early and still sends what it has.\n\n"
                "A camera that drops (or /disable) just leaves a gap: filming resumes "
                "when it is back and the pieces are joined. At the end each film goes "
                "to Google Drive with the link posted here.\n\n"
                "Examples: /timelapse 8h 100x · /timelapse 20m 10x · /timelapse 2 60x")
    if cmd == "expose":
        return (f"Makes https://{WEB_HOST or 'the site'} reachable from anywhere, no VPN: the "
                "name is pointed at the Cloudflare tunnel instead of the LAN address, which "
                "takes a minute or so to spread. Anyone with the link can then watch both "
                "cameras — watch only: no recording, no pan/tilt from the public side. The "
                "video itself needs a direct path to the box, so the router is told to "
                f"forward port {MEDIA_PORT} here at the same time. The Telegram commands "
                "work as before. It stays public until /close.")
    if cmd == "close":
        return (f"Points https://{WEB_HOST or 'the site'} back at the LAN address, so it is "
                "VPN-only again, removes the router's port forward and disconnects everyone "
                "watching (VPN viewers reconnect by themselves). The tunnel route is refused "
                "the moment /close runs; DNS catches up within a minute or so.")
    if cmd == "help":
        return "/help lists the commands; /help <command> explains one, e.g. /help timelapse."
    return HELP_TOPICS[cmd][1]

def _help_text(topic=""):
    topic = HELP_ALIASES.get(topic.lstrip("/"), topic.lstrip("/"))
    if topic in HELP_TOPICS:
        return f"/{topic} — {HELP_TOPICS[topic][0]}\n\n{_help_detail(topic)}"
    lines = []
    if topic: lines += [f"I don't know /{topic}. Here is what I understand:", ""]
    for title, cmds in HELP_GROUPS:
        lines.append(f"{title}:")
        lines += [f"  /{c} — {HELP_TOPICS[c][0]}" for c in cmds]
        lines.append("")
    lines.append("/help <command> explains one in detail, e.g. /help timelapse.")
    return "\n".join(lines).rstrip()

def _status_lines():
    st = REC.status()
    return [("📷 Cameras on" if st["enabled"] else "⏸ Cameras off — lenses parked, nothing is captured"),
            ("👁 Following detections" if st["follow"] else "🚫 Not following detections"),
            (f"🌍 Public — https://{WEB_HOST} needs no VPN (/close)" if exposed_on() else "🔒 VPN only"),
            (f"🔴 Recording {_dur(st['elapsed'])}" if st["recording"] else "⏹ Not recording")] + (
           [_tl_status_line(tl)] if (tl := TL.status())["active"] else [])

def _tl_status_line(tl):
    return (f"🎞 Timelapse {tl['speed']:g}x · {_span(tl['elapsed'])} in, {_span(tl['left'])} left · film so far "
            + ", ".join(f"{nm} {_span(f)}" for nm, f in tl["film"].items()))

def _tg_note(text, secs=5):
    """A reply that removes itself after a few seconds, so /clear leaves the chat
    clean instead of leaving its own confirmation behind."""
    try: r = _tg_api("sendMessage", fields={"chat_id": TG_CHAT, "text": text})
    except Exception as e: print("telegram: reply failed:", e); return
    mid = r.get("result", {}).get("message_id") if r.get("ok") else None
    if mid:
        def drop():
            try: _tg_api("deleteMessage", fields={"chat_id": TG_CHAT, "message_id": mid})
            except Exception: pass
        t = threading.Timer(secs, drop); t.daemon = True; t.start()

def _tg_handle(text, msg_id=None):
    """Run one command; may block (record/stop/snapshot), so it runs off the poll loop."""
    if text == "/clear":
        # the command itself goes too; in a group that needs the bot to be an
        # admin with "delete messages", and it is simply left there otherwise
        if msg_id:
            try: _tg_api("deleteMessage", fields={"chat_id": TG_CHAT, "message_id": msg_id})
            except Exception as e:
                why = e.read().decode(errors="replace") if hasattr(e, "read") else e
                print(f"telegram: cannot delete the /clear command ({msg_id}): {why}")
        n, failed = _tg_clear()
        if failed:
            _tg_reply(f"⚠️ Deleted {n}, but {failed} could not be deleted.")
        else:
            _tg_note(f"🧹 Deleted {n} message{'s' if n != 1 else ''}." if n
                     else "🧹 Nothing to delete (Telegram only allows the last 48 hours).")
    elif text == "/enable":
        _tg_reply("📷 Waking the cameras — putting the lenses back…")
        failed = _cameras_on()                 # settings back, lens back to its view
        if not _flag_set("disabled", False):
            _tg_reply("⚠️ Cameras are awake but the switch file could not be cleared."); return
        msg = "📷 Cameras on. The feeds come back in a few seconds."
        if failed: msg += "\n⚠️ Could not restore the view on: " + ", ".join(failed)
        _tg_reply(msg)
    elif text == "/disable":
        if TL.status()["active"]: TL.stop()      # nothing to film with the lenses parked; sends what it has
        if REC.status().get("recording"): REC.stop()
        if not _flag_set("disabled", True):    # stop the feeds first, then the cameras
            _tg_reply("⚠️ Could not disable (state dir not writable)."); return
        _tg_reply("⏸ Stopping the feeds and turning the cameras away…")
        failed = _cameras_off()
        msg = ("⏸ Cameras off — lenses parked face-down, LEDs and their own "
               "recording and alerts switched off. Nothing is being captured.")
        if failed: msg += "\n⚠️ Could not park: " + ", ".join(failed)
        _tg_reply(msg)
    elif text == "/follow":
        _flag_set("follow", True)
        _tg_reply("👁 Following — I will send a clip on person, pet or motion."
                  + ("" if cams_enabled() else "\n⚠️ Cameras are off, so nothing will trigger until /enable."))
    elif text == "/unfollow":
        _flag_set("follow", False)
        _tg_reply("🚫 Not following detections any more.")
    elif text == "/expose":
        if not _expose_ready():
            _tg_reply("⚠️ Public access is not set up on this box (no host, tunnel or DNS token)."); return
        if exposed_on() and not _dns_point(True):
            _tg_reply(f"🌍 Already public — https://{WEB_HOST} works without the VPN. /close takes it back."); return
        if not _flag_set("exposed", True):        # open the tunnel vhost first, then point DNS at it
            _tg_reply("⚠️ Could not expose (state dir not writable)."); return
        why = _dns_point(True)
        if why:
            _flag_set("exposed", False)
            _tg_reply(f"⚠️ Could not point DNS at the tunnel: {why}. Still VPN-only."); return
        # the media path: without the forward the page loads but shows no video
        rwhy = _router_forward(True) if _router_ready() else "no router password on this box"
        _tg_reply(f"🌍 Public — https://{WEB_HOST} now works without the VPN, view only "
                  "(no recording or pan/tilt from there). DNS takes a minute or so to "
                  "switch. /close takes it back."
                  + (f"\n⚠️ Router: {rwhy} — the page will load but public viewers may get "
                     f"no video until {MEDIA_PORT} TCP+UDP is forwarded to {LAN_IP}." if rwhy else ""))
    elif text == "/close":
        if not _expose_ready():
            _tg_reply("⚠️ Public access is not set up on this box."); return
        was = exposed_on()
        _flag_set("exposed", False)               # shut the tunnel vhost first, then move DNS
        why = _dns_point(False)
        rwhy = _router_forward(False) if _router_ready() else None
        kicked = _mtx_kick_viewers()
        warn = ((f"\n⚠️ DNS could not be moved back: {why}. Run /close again in a moment." if why else "")
                + (f"\n⚠️ Router: {rwhy} — port {MEDIA_PORT} may still be forwarded." if rwhy else ""))
        if not was and not why:
            _tg_reply("🔒 Already VPN-only." + warn); return
        _tg_reply((f"🔒 Closed — https://{WEB_HOST} is VPN-only again" if not why
                   else "🔒 The tunnel route is shut")
                  + (f"; {kicked} viewer{'s' if kicked != 1 else ''} disconnected" if kicked else "")
                  + (". DNS catches up within a minute or so." if not why else ".") + warn)
    elif text.startswith("/timelapse"):
        arg = text[len("/timelapse"):].strip()
        if arg in ("stop", "off", "cancel", "done", "finish", "now"):
            r = TL.stop()
            _tg_reply("🎞 Finishing the timelapse now — building the film from what it has…"
                      if r["ok"] else f"⚠️ {r['error']}.")
        elif not arg:
            tl = TL.status()
            _tg_reply(_tl_status_line(tl) if tl["active"]
                      else "No timelapse running. Start one with e.g. /timelapse 8h 100x.")
        else:
            secs, speed = _parse_timelapse(arg)
            if not secs or secs <= 0:
                _tg_reply("⚠️ I need a duration and a speed, e.g. /timelapse 8h 100x, "
                          "/timelapse 90m 60x, or /timelapse stop."); return
            if speed < 2:
                _tg_reply("⚠️ The speed-up has to be at least 2x."); return
            if secs > TL_MAX_HOURS * 3600:
                _tg_reply(f"⚠️ That is longer than the {TL_MAX_HOURS:g}h limit."); return
            r = TL.start(secs, speed)
            if not r["ok"]: _tg_reply(f"⚠️ Could not start: {r['error']}."); return
            _tg_reply(f"🎞 Timelapse started — {_span(secs)} at {speed:g}x, a frame every "
                      f"{speed / TL_FPS:.1f}s. Each camera's film will run about "
                      f"{_span(secs / speed)}. /timelapse stop finishes it early.")
    elif text in ("/record", "/rec", "/start"):
        was = REC.status().get("recording")     # start() returns the live state either way
        st = REC.start()
        if st.get("error"):   _tg_reply(f"⚠️ Could not start: {st['error']}.")
        elif was:             _tg_reply("Already recording.")
        else:                 _tg_snap_reply("🔴 Recording started.")
    elif text == "/stop":
        if REC.status().get("recording"):
            _tg_reply("⏹ Stopping…")              # instant ack, then finalize/upload
            REC.stop()
        else:
            _tg_reply("Not recording.")
    elif text == "/status":
        msg = "\n".join(_status_lines())
        if cams_enabled(): _tg_snap_reply(msg)
        else:              _tg_reply(msg)
    elif text == "/help" or text.startswith("/help "):
        _tg_reply(_help_text(text[len("/help"):].strip()))
    elif text.startswith("/"):
        _tg_reply(f"I don't know {text.split()[0]}. /help lists what I understand.")

def _tg_get(params, read_timeout):
    import urllib.request, urllib.parse, json as _json
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates?{urllib.parse.urlencode(params)}"
    return _json.loads(urllib.request.urlopen(url, timeout=read_timeout).read().decode())

STALE_SECONDS = 60      # ignore commands older than this (queued during downtime)

def _tg_command_loop():
    """Long-poll getUpdates; dispatch each owner command to a worker thread.
    Restart-proof: drains any backlog on startup and skips stale commands, so a
    service restart never replays queued commands or lags on old ones."""
    import urllib.error
    print("telegram: command poller started")
    _tg_set_commands()
    # Drain backlog: confirm everything pending so we start from 'now'.
    offset = 0
    try:
        res = _tg_get({"offset": -1, "timeout": 0}, 15).get("result", [])
        if res: offset = res[-1]["update_id"] + 1
        print(f"telegram: starting fresh at offset {offset}")
    except Exception as e:
        print("telegram: drain failed:", e)
    while True:
        try:
            data = _tg_get({"offset": offset, "timeout": 20,
                            "allowed_updates": '["message"]'}, 35)
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                m = upd.get("message") or {}
                if str(m.get("chat", {}).get("id")) != str(TG_CHAT):
                    print(f"telegram: ignoring a message from chat {m.get('chat', {}).get('id')} "
                          f"({m.get('chat', {}).get('type')}); only {TG_CHAT} is listened to")
                    continue                                  # owner only
                # "/timelapse@bot_name 8h 100x" is how Telegram sends a command
                # picked from the menu in a group: drop only the @mention on the
                # command word, never what follows it.
                text = (m.get("text") or "").strip().lower()
                if text.startswith("/"):
                    cmd, _, rest = text.partition(" ")
                    text = (cmd.split("@")[0] + " " + rest).strip()
                print(f"telegram: got {text or '(no text)'!r}")
                if not text:
                    continue
                age = time.time() - m.get("date", time.time())
                if age > STALE_SECONDS:
                    print(f"telegram: skipping stale {text!r} ({age:.0f}s old)")
                    continue
                threading.Thread(target=_tg_handle, args=(text, m.get("message_id")),
                                 daemon=True).start()
        except urllib.error.HTTPError as e:
            # 409 = another getUpdates consumer (e.g. brief overlap on restart)
            print("telegram: getUpdates conflict, backing off" if e.code == 409
                  else f"telegram: poll HTTP {e.code}")
            time.sleep(3)
        except Exception as e:
            print("telegram: poll error:", e)
            time.sleep(3)

# -------------------- Motion detection (Reolink AI) -> Telegram --------------------
def _cam_api(ip, body, timeout=6):
    import urllib.request, urllib.parse, ssl, json as _json
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    qs = urllib.parse.urlencode({"user": CAM_USER, "password": CAM_PASS})
    req = urllib.request.Request(f"http://{ip}/cgi-bin/api.cgi?{qs}",
          data=_json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return _json.loads(urllib.request.urlopen(req, timeout=timeout, context=ctx).read().decode())

# ------------------------------ PTZ (pan/tilt) ------------------------------
# Reolink PtzCtrl: a direction command starts the motor and it keeps going until
# a Stop. The page holds a button down and releases it, but a lost release (tab
# closed, network drop) would leave a camera spinning, so every move also arms a
# server-side Stop.
PTZ_OPS = {"Left", "Right", "Up", "Down", "LeftUp", "LeftDown", "RightUp", "RightDown", "Stop"}
PTZ_SPEED    = int(os.environ.get("PTZ_SPEED", "32"))
PTZ_MAX_MOVE = float(os.environ.get("PTZ_MAX_MOVE", "4"))   # seconds a move may run unattended
_ptz_timers = {}
_ptz_lock = threading.Lock()

def _cam_has_ptz(ip):
    """Does this camera pan/tilt? Unreachable cameras are given the benefit of the
    doubt, so a camera that is merely down does not lose its controls."""
    try:
        ab = _cam_api(ip, [{"cmd": "GetAbility", "action": 0,
                            "param": {"User": {"userName": CAM_USER}}}], timeout=4)[0]["value"]["Ability"]
        ch = (ab.get("abilityChn") or [{}])[0]
        ok = bool(ch.get("ptzCtrl", {}).get("ver")) and bool(ch.get("ptzDirection", {}).get("ver"))
        print(f"ptz: {ip} {'supports pan/tilt' if ok else 'has no pan/tilt'}")
        return ok
    except Exception as e:
        print(f"ptz: {ip} ability check failed ({e}); assuming it pans/tilts")
        return True

def _ptz_send(ip, op):
    _cam_api(ip, [{"cmd": "PtzCtrl", "action": 0,
                   "param": {"channel": 0, "op": op, "speed": PTZ_SPEED}}])

def _ptz(n, op):
    """Move camera n (1-based, CAM_IPS order). 'Stop' halts it."""
    if op not in PTZ_OPS: return {"ok": False, "error": "bad op"}
    if not (1 <= n <= len(CAMS)): return {"ok": False, "error": "no such camera"}
    ip, name = CAMS[n-1]
    with _ptz_lock:                      # a new command always replaces the old auto-stop
        t = _ptz_timers.pop(ip, None)
        if t:
            try: t.cancel()
            except Exception: pass
    try:
        _ptz_send(ip, op)
    except Exception as e:
        print("ptz: command failed:", e); return {"ok": False, "error": str(e)}
    if op != "Stop":
        def _auto_stop():
            with _ptz_lock: _ptz_timers.pop(ip, None)
            try: _ptz_send(ip, "Stop"); print(f"ptz: auto-stopped {name} after {PTZ_MAX_MOVE}s")
            except Exception as e: print("ptz: auto-stop failed:", e)
        t = threading.Timer(PTZ_MAX_MOVE, _auto_stop); t.daemon = True; t.start()
        with _ptz_lock: _ptz_timers[ip] = t
    return {"ok": True, "cam": name, "op": op}

# --------------------- turning the cameras themselves off ---------------------
# There is no power or sleep command on an E1 Pro, so "off" is what Reolink's own
# privacy mode does, plus everything else the camera does on its own:
#   * remember where the camera is looking (a PTZ preset), then tilt the lens down
#     into the base until it hits the stop
#   * stop its own recording and push notifications, and switch off the IR and
#     status LEDs, so nothing is lit and nothing is captured
# The previous settings are written to the state dir, so /enable puts back what
# was actually there rather than assuming defaults — and survives a restart.
PARK_PRESET = int(os.environ.get("PARK_PRESET", "5"))
PARK_SECS   = float(os.environ.get("PARK_SECS", "9"))    # long enough to reach the tilt stop
UNPARK_SETTLE = float(os.environ.get("UNPARK_SETTLE", "4"))  # pause between the two recalls
_SETTINGS = "camera-settings.json"

def _cam_toggles(ip):
    """Read the camera-side switches we are about to change."""
    got = {}
    def one(key, cmd, param, dig):
        try: got[key] = dig(_cam_api(ip, [{"cmd": cmd, "action": 0, "param": param}])[0]["value"])
        except Exception as e: print(f"cam {ip}: cannot read {cmd}: {e}")
    one("powerLed", "GetPowerLed", {"channel": 0}, lambda v: v["PowerLed"]["state"])
    one("irLights", "GetIrLights", {"channel": 0}, lambda v: v["IrLights"]["state"])
    one("push",     "GetPushV20",  {"channel": 0}, lambda v: v["Push"])
    one("rec",      "GetRecV20",   {"channel": 0}, lambda v: v["Rec"])
    return got

def _cam_apply(ip, want):
    """Apply the switches. push/rec are sent back whole, with only enable changed,
    so schedules and every other field the camera holds are left alone."""
    def send(cmd, param):
        try: _cam_api(ip, [{"cmd": cmd, "action": 0, "param": param}], timeout=8)
        except Exception as e: print(f"cam {ip}: {cmd} failed: {e}")
    if "powerLed" in want: send("SetPowerLed", {"PowerLed": {"channel": 0, "state": want["powerLed"]}})
    if "irLights" in want: send("SetIrLights", {"IrLights": {"channel": 0, "state": want["irLights"]}})
    if "push" in want:     send("SetPushV20", {"Push": want["push"]})
    if "rec"  in want:     send("SetRecV20",  {"Rec":  want["rec"]})

def _cam_park(ip):
    """Save where the camera looks, then tilt the lens down into the base."""
    try:
        _cam_api(ip, [{"cmd": "SetPtzPreset", "action": 0,
                       "param": {"PtzPreset": {"channel": 0, "enable": 1,
                                               "id": PARK_PRESET, "name": "home"}}}])
    except Exception as e:
        print(f"cam {ip}: could not save its position: {e}"); return False
    try:
        _ptz_send(ip, "Down"); time.sleep(PARK_SECS)
    except Exception as e:
        print(f"cam {ip}: park failed: {e}"); return False
    finally:
        try: _ptz_send(ip, "Stop")
        except Exception: pass
    return True

def _cam_unpark(ip):
    """Recall the saved position. Twice: after driving into the tilt stop the head
    settles a few degrees off on the first recall, and a second one takes that up."""
    def to_pos():
        _cam_api(ip, [{"cmd": "PtzCtrl", "action": 0,
                       "param": {"channel": 0, "op": "ToPos", "id": PARK_PRESET, "speed": PTZ_SPEED}}])
    try:
        to_pos()
    except Exception as e:
        print(f"cam {ip}: could not return to its saved position: {e}"); return False
    time.sleep(UNPARK_SETTLE)
    try: to_pos()
    except Exception as e: print(f"cam {ip}: second recall failed: {e}")
    return True

def _cameras_off():
    """Park and silence every camera, in parallel. Returns the names that failed."""
    saved, failed, lock = {}, [], threading.Lock()
    def work(ip, name):
        got = _cam_toggles(ip)
        with lock: saved[ip] = got
        off = {}
        if "powerLed" in got: off["powerLed"] = "Off"
        if "irLights" in got: off["irLights"] = "Off"
        if "push" in got: off["push"] = dict(got["push"], enable=0)
        if "rec"  in got: off["rec"]  = dict(got["rec"],  enable=0)
        _cam_apply(ip, off)
        if not _cam_park(ip):
            with lock: failed.append(name)
    ts = [threading.Thread(target=work, args=(ip, nm)) for ip, nm in CAMS]
    for t in ts: t.start()
    # the file is written as soon as the settings are read, before the lenses have
    # finished moving, so an interrupted /disable still leaves /enable a way back
    deadline = time.time() + 5
    while len(saved) < len(CAMS) and time.time() < deadline: time.sleep(0.2)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, _SETTINGS), "w") as f: json.dump(saved, f)
    except OSError as e:
        print("state: cannot save camera settings:", e)
    for t in ts: t.join(PARK_SECS + 20)
    return failed

def _cameras_on():
    """Put back what was there before /disable and return each lens to its view."""
    try:
        with open(os.path.join(STATE_DIR, _SETTINGS)) as f: saved = json.load(f)
    except Exception:
        saved = {}                       # nothing saved (or never disabled): just unpark
    failed, lock = [], threading.Lock()
    def work(ip, name):
        if ip not in saved:
            print(f"cam {ip}: nothing saved, leaving it where it is"); return
        _cam_apply(ip, saved[ip])
        if not _cam_unpark(ip):          # only recall a preset this app stored
            with lock: failed.append(name)
    ts = [threading.Thread(target=work, args=(ip, nm)) for ip, nm in CAMS]
    for t in ts: t.start()
    for t in ts: t.join(30)
    try: os.remove(os.path.join(STATE_DIR, _SETTINGS))
    except OSError: pass
    return failed

def _cam_detections(ip):
    """Return the set of active detections on a camera: pet / person / motion."""
    out = set()
    try:
        ai = _cam_api(ip, [{"cmd": "GetAiState", "action": 0, "param": {"channel": 0}}])[0].get("value", {})
        if ai.get("dog_cat", {}).get("alarm_state"): out.add("pet")
        if ai.get("people", {}).get("alarm_state"):  out.add("person")
    except Exception: pass
    if not out:                                   # fall back to generic motion
        try:
            if _cam_api(ip, [{"cmd": "GetMdState", "action": 0, "param": {"channel": 0}}])[0].get("value", {}).get("state"):
                out.add("motion")
        except Exception: pass
    return out

def _cam_snapshot(ip):
    import urllib.request, urllib.parse, ssl, time as _t
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    qs = urllib.parse.urlencode({"cmd": "Snap", "channel": 0, "rs": str(int(_t.time()*1000)),
                                 "user": CAM_USER, "password": CAM_PASS})
    try:
        d = urllib.request.urlopen(f"http://{ip}/cgi-bin/api.cgi?{qs}", timeout=8, context=ctx).read()
        return d if d[:2] == b"\xff\xd8" else None
    except Exception: return None

_MOTION_LABEL = {"pet": "🐕 Pet", "person": "🧍 Person", "motion": "🟡 Motion"}

def _playback_clip(path, det_epoch):
    """Fetch [det-PRE, det+POST] from MediaMTX playback, remux to mp4+aac."""
    import urllib.request, urllib.parse, datetime, tempfile
    start = datetime.datetime.fromtimestamp(det_epoch - MOTION_PRE, datetime.timezone.utc)
    url = PLAYBACK + "/get?" + urllib.parse.urlencode(
        {"path": path, "start": start.isoformat().replace("+00:00", "Z"),
         "duration": MOTION_PRE + MOTION_POST})
    fd, raw = tempfile.mkstemp(suffix=".mp4"); os.close(fd)
    fd, out = tempfile.mkstemp(suffix=".mp4"); os.close(fd)
    try:
        with open(raw, "wb") as f: f.write(urllib.request.urlopen(url, timeout=30).read())
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", raw,
                        "-vf", f"scale={CLIP_WIDTH}:-2", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", CLIP_CRF, "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", CLIP_ABR, "-ac", "1",
                        "-movflags", "+faststart", out], check=True, timeout=120)
        os.remove(raw)
        return out
    except Exception as e:
        print("motion: clip fetch failed:", e)
        for p in (raw, out):
            try: os.path.exists(p) and os.remove(p)
            except OSError: pass
        return None

def _tg_send_video(path, caption):
    boundary, body = _tg_multipart({"chat_id": TG_CHAT, "caption": caption,
                                    "supports_streaming": "true"}, "video", path, ctype="video/mp4")
    return _tg_api("sendVideo", boundary=boundary, body=body).get("ok")

def _tg_send_photo(img_bytes, caption):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".jpg"); os.close(fd)
    with open(path, "wb") as f: f.write(img_bytes)
    try:
        boundary, body = _tg_multipart({"chat_id": TG_CHAT, "caption": caption},
                                       "photo", path, ctype="image/jpeg")
        return _tg_api("sendPhoto", boundary=boundary, body=body).get("ok")
    finally:
        try: os.remove(path)
        except OSError: pass

def _notify_motion(ip, name, ev, path, det_epoch):
    label = _MOTION_LABEL.get(ev, ev)
    time.sleep(MOTION_POST + 1.5)              # wait for the +POST seconds to record
    clip = _playback_clip(path, det_epoch)
    if clip:
        try:
            cap = f"{label} detected · {name}  ({_fmt_size(os.path.getsize(clip))})"
            if not _tg_send_video(clip, cap):
                _tg_reply(cap)
        except Exception as e:
            print("motion: video send failed:", e)
        finally:
            try: os.remove(clip)
            except OSError: pass
    else:                                       # fall back to a snapshot
        cap = f"{label} detected · {name}"
        img = _cam_snapshot(ip)
        if img: _tg_send_photo(img, cap)
        else:   _tg_reply(cap)
    print("motion:", label, name)

def _motion_watch():
    """Alert Telegram on camera detections (pet/person/motion) while /follow is on
    and the cameras are enabled — independent of whether anything is recording."""
    print("motion: watcher started")
    last = {}
    while True:
        try:
            if TG_TOKEN and TG_CHAT and follow_on() and cams_enabled():
                for i, (ip, name) in enumerate(CAMS):
                    campath = CLIP_PATH.replace("{n}", str(i+1))
                    for ev in _cam_detections(ip):
                        key = (ip, ev)
                        now = time.time()
                        if now - last.get(key, 0) > MOTION_COOLDOWN:
                            last[key] = now
                            threading.Thread(target=_notify_motion,
                                             args=(ip, name, ev, campath, now),
                                             daemon=True).start()
            time.sleep(2)
        except Exception as e:
            print("motion: loop error:", e); time.sleep(5)

# ------------------------------- /timelapse --------------------------------
# A plain timelapse: /timelapse 8h 100x watches each camera for 8 hours and
# turns it into a film 100 times shorter (8h -> 4m48s). One ffmpeg per camera
# reads the HD path and keeps one frame every speed/fps seconds (100x at 30 fps
# is a frame every 3.3 s), encoding straight into the film as the frames
# arrive, so nothing piles up on disk and the end of a session only has to join
# the pieces. It decodes every frame rather than just keyframes: the cameras
# put one every 2 s, and sampling from those would make the film stutter.
#
# A camera that drops, or /disable, ends that camera's current piece; a new one
# starts when it is back. Every piece is encoded the same way and at the same
# size, so they join with a stream copy. /timelapse stop finishes early and
# still delivers what it has.
TL_FPS    = int(os.environ.get("TL_FPS", "30"))        # film frame rate
TL_SPEED  = float(os.environ.get("TL_SPEED", "100"))   # when the command names none
TL_WIDTH  = int(os.environ.get("TL_WIDTH", "1920"))    # film size; the HD stream is
TL_HEIGHT = int(os.environ.get("TL_HEIGHT", "1078"))   # 2880x1616, same shape
TL_CRF    = os.environ.get("TL_CRF", "23")
TL_PATH   = os.environ.get("TL_PATH", "cam{n}")        # HD path ({n} = camera number)
TL_MAX_HOURS = float(os.environ.get("TL_MAX_HOURS", "24"))

def _parse_duration(text):
    """'8h' / '90m' / '45s' / '2h30m' / '8' (hours) -> seconds, or None."""
    import re
    t = text.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", t): return float(t) * 3600
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", t)
    if not parts or re.sub(r"\d|\.|\s|[hms]", "", t): return None
    return sum(float(v) * {"h": 3600, "m": 60, "s": 1}[u] for v, u in parts)

def _parse_timelapse(arg):
    """'8h 100x' (either order, speed optional) -> (seconds or None, speed)."""
    import re
    speed = TL_SPEED
    m = re.search(r"(\d+(?:\.\d+)?)\s*[x×]", arg)
    if m:
        speed = float(m.group(1)); arg = arg[:m.start()] + arg[m.end():]
    return _parse_duration(arg), speed

def _span(secs):
    """28800 -> '8h 00m', 288 -> '4m 48s', 42 -> '42s'."""
    s = int(round(secs))
    if s >= 3600: return f"{s//3600}h {s%3600//60:02d}m"
    if s >= 60:   return f"{s//60}m {s%60:02d}s"
    return f"{s}s"

def _concat(files, out_path):
    lst = out_path + ".txt"
    with open(lst, "w") as f:
        for p in files: f.write("file '%s'\n" % p.replace("'", "'\\''"))
    try:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", lst, "-c", "copy", "-movflags", "+faststart", out_path],
                       check=True, timeout=900)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 2000
    except Exception as e:
        print("timelapse: assembly failed:", e); return False
    finally:
        try: os.remove(lst)
        except OSError: pass

_TL_META = "session.json"

def _tl_pieces(sess_dir, n):
    """Camera n's pieces in a session, in order — whatever reached the disk,
    including a piece cut short by a restart (MPEG-TS stays readable)."""
    return [f for f in sorted(glob.glob(os.path.join(sess_dir, f"cam{n}_*.ts")))
            if os.path.getsize(f) > 2000]

def _film_secs(path):
    """Length of a finished film, from its frame count."""
    import re
    try:
        r = subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-stats", "-i", path,
                            "-map", "0:v", "-c", "copy", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=300)
        return int(re.findall(r"frame=\s*(\d+)", r.stderr)[-1]) / TL_FPS
    except Exception:
        return 0

def _tl_deliver(sess_dir, speed, real, how="finished"):
    """Join each camera's pieces into one film, send the films, drop the session."""
    made = []
    for i, (_ip, name) in enumerate(CAMS, 1):
        files = _tl_pieces(sess_dir, i)
        if not files: continue
        out = os.path.join(REC_DIR, f"{os.path.basename(sess_dir)}_cam{i}.mp4")
        if _concat(files, out):
            made.append((name, out, _film_secs(out)))
    if _tg_enabled():
        at = f" — {_span(real)} at {speed:g}x" if speed and real else ""
        if made:
            _tg_reply(f"🎞 Timelapse {how}{at}. "
                      + ", ".join(f"{nm}: {_span(film)} film" for nm, _p, film in made)
                      + ".\nUploading…")
        else:
            _tg_reply(f"🎞 Timelapse {how} with nothing filmed (were the cameras off?).")
    for _nm, path, film in made:
        try: _deliver(path, int(film))
        finally:
            try: os.path.exists(path) and os.remove(path)
            except OSError: pass
    shutil.rmtree(sess_dir, ignore_errors=True)
    print(f"timelapse: {os.path.basename(sess_dir)} delivered")

class Timelapse:
    """One session across all cameras; each camera makes its own film.

    The session lives on disk as well as here: its pieces, and a session.json
    with the deadline and speed, sit in its directory until the films are sent.
    So a restart of the app (a deploy, a crash) does not lose it — recover()
    picks a session with time left back up where it was, and finishes and
    sends one whose time ran out while the app was down."""
    def __init__(self):
        self.lock = threading.Lock()
        self.active = False; self.started = 0.0; self.until = 0.0; self.speed = TL_SPEED
        self.dir = None; self.captured = {}; self.live = {}
        self.stop_evt = threading.Event()

    def status(self):
        with self.lock:
            if not self.active: return {"active": False}
            now = time.time()
            # real time filmed so far, including the piece still being written
            shot = {nm: self.captured[nm] + (now - self.live[nm] if self.live[nm] else 0)
                    for nm in self.captured}
            return {"active": True, "elapsed": int(now - self.started),
                    "left": max(0, int(self.until - now)), "speed": self.speed,
                    "film": {nm: s / self.speed for nm, s in shot.items()}}

    def _save(self):
        """Write the session's state next to its pieces (caller holds the lock)."""
        meta = {"started": self.started, "until": self.until, "speed": self.speed,
                "captured": self.captured, "live": self.live}
        try:
            tmp = os.path.join(self.dir, _TL_META + ".tmp")
            with open(tmp, "w") as f: json.dump(meta, f)
            os.replace(tmp, os.path.join(self.dir, _TL_META))
        except OSError as e:
            print("timelapse: cannot save session:", e)

    def _begin(self, sess_dir, started, until, speed, captured=None):
        names = [name for _ip, name in CAMS]
        self.dir = sess_dir
        self.captured = {nm: float((captured or {}).get(nm, 0)) for nm in names}
        self.live = {nm: None for nm in names}
        self.active = True; self.started = started; self.until = until; self.speed = speed
        self.stop_evt.clear()
        self._save()

    def start(self, secs, speed):
        with self.lock:
            if self.active: return {"ok": False, "error": "a timelapse is already running"}
            if not cams_enabled(): return {"ok": False, "error": "cameras are disabled"}
            if not CAMS: return {"ok": False, "error": "no cameras configured"}
            sess_dir = os.path.join(REC_DIR, "timelapse_" + time.strftime("%Y%m%d_%H%M%S"))
            try: os.makedirs(sess_dir, exist_ok=True)
            except OSError as e: return {"ok": False, "error": f"cannot create {sess_dir}: {e}"}
            now = time.time()
            self._begin(sess_dir, now, now + secs, speed)
        threading.Thread(target=self._run, daemon=True).start()
        return {"ok": True}

    def recover(self):
        """After a restart: resume the newest session that still has time left;
        finish and send every other one left on disk."""
        now, resume, finish = time.time(), None, []
        for d in sorted(glob.glob(os.path.join(REC_DIR, "timelapse_*")), reverse=True):
            if not os.path.isdir(d): continue
            try:
                with open(os.path.join(d, _TL_META)) as f: meta = json.load(f)
            except Exception:
                meta = None                    # no state saved: can only be finished
            if meta and resume is None and CAMS and meta["until"] - now > 60:
                resume = (d, meta)
            else:
                finish.append((d, meta))
        if resume:
            d, meta = resume
            # a piece the restart cut short counts up to its last write
            captured = dict(meta.get("captured") or {})
            for nm, t0 in (meta.get("live") or {}).items():
                if not t0: continue
                i = next((i for i, (_ip, n) in enumerate(CAMS, 1) if n == nm), None)
                pieces = _tl_pieces(d, i) if i else []
                if pieces:
                    captured[nm] = captured.get(nm, 0) + max(0, os.path.getmtime(pieces[-1]) - t0)
            with self.lock:
                self._begin(d, meta["started"], meta["until"], meta["speed"], captured)
            print(f"timelapse: resumed {os.path.basename(d)}, {_span(meta['until'] - now)} left")
            if _tg_enabled():
                _tg_reply(f"🎞 The app restarted mid-timelapse — resumed, "
                          f"{_span(meta['until'] - now)} left at {meta['speed']:g}x.")
            threading.Thread(target=self._run, daemon=True).start()
        def finish_all():
            for d, meta in finish:
                print(f"timelapse: finishing {os.path.basename(d)} left by a restart")
                end = min(now, meta["until"]) if meta else 0
                _tl_deliver(d, meta and meta["speed"], meta and end - meta["started"],
                            how="finished (the app restarted while it ran)")
        if finish: threading.Thread(target=finish_all, daemon=True).start()

    def stop(self):
        with self.lock:
            if not self.active: return {"ok": False, "error": "no timelapse is running"}
        self.stop_evt.set()
        return {"ok": True}

    def _over(self):
        return self.stop_evt.is_set() or time.time() >= self.until

    def _run(self):
        ts = [threading.Thread(target=self._film, args=(i, name), daemon=True)
              for i, (_ip, name) in enumerate(CAMS, 1)]
        print(f"timelapse: running at {self.speed:g}x")
        for t in ts: t.start()
        for t in ts: t.join()
        with self.lock:
            sess_dir, speed = self.dir, self.speed
            real = min(time.time(), self.until) - self.started
            self.active = False; self.dir = None
        _tl_deliver(sess_dir, speed, real)

    def _film(self, n, name):
        """Keep one camera's film going until the session ends: one piece per
        uninterrupted run of the camera."""
        src = "rtsp://localhost:8554/" + TL_PATH.replace("{n}", str(n))
        W, H = TL_WIDTH, TL_HEIGHT
        # fps keeps one frame per interval; settb+setpts lay them out one film
        # frame apart (setpts alone would round to the interval's coarse
        # timebase and bunch them up); the fixed size means an OFFLINE
        # placeholder (a different size) cannot break the join. Pieces are
        # MPEG-TS so one cut short by a crash is still readable.
        vf = (f"fps={TL_FPS}/{self.speed:g},settb=1/{TL_FPS},setpts=N,"
              f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        k, fails = len(glob.glob(os.path.join(self.dir, f"cam{n}_*.ts"))), 0
        while not self._over():
            if not cams_enabled():
                self.stop_evt.wait(2); continue
            k += 1
            out = os.path.join(self.dir, f"cam{n}_{k:03d}.ts")
            cmd = [FFMPEG, "-loglevel", "error", "-nostdin", "-rtsp_transport", "tcp",
                   "-i", src, "-an", "-vf", vf,
                   # passthrough, not -r: -r would stretch each piece's last frame
                   # to its real-time length (a 3 s freeze at 100x)
                   "-fps_mode", "passthrough", "-enc_time_base", f"1/{TL_FPS}",
                   "-c:v", "libx264", "-preset", "medium", "-crf", TL_CRF,
                   "-pix_fmt", "yuv420p", "-threads", "2", "-f", "mpegts", out]
            t0 = time.time()
            # a camera that stays away is retried quietly, backing off to 30 s
            try: p = subprocess.Popen(cmd, stderr=subprocess.DEVNULL if fails else None)
            except Exception as e:
                print(f"timelapse: {name}: cannot start ffmpeg: {e}"); self.stop_evt.wait(5); continue
            with self.lock: self.live[name] = t0; self._save()
            while p.poll() is None and not self._over() and cams_enabled():
                self.stop_evt.wait(1)
            _stop_ffmpeg(p)
            ran = time.time() - t0
            with self.lock:
                self.live[name] = None
                got = os.path.exists(out) and os.path.getsize(out) > 2000
                if got: self.captured[name] += ran
                self._save()
            if got:
                fails = 0; print(f"timelapse: {name} piece {k} ended after {_span(ran)}")
            else:
                if not fails: print(f"timelapse: {name}: no picture, retrying until it is back")
                fails += 1
            if not self._over(): self.stop_evt.wait(min(30, 3 * fails or 3))
TL = Timelapse()

def _cam_reachable(ip):
    import socket
    s = socket.socket(); s.settimeout(3)
    try: s.connect((ip, 554)); return True       # RTSP port up = camera online
    except OSError: return False
    finally: s.close()

def _cam_health_watch():
    """Notify Telegram when a camera goes offline / comes back (debounced)."""
    print("health: watcher started")
    state = {ip: _cam_reachable(ip) for ip, _ in CAMS}   # baseline, no notify
    fails = {ip: 0 for ip, _ in CAMS}
    while True:
        time.sleep(10)
        for ip, name in CAMS:
            up = _cam_reachable(ip)
            if up:
                fails[ip] = 0
                if not state[ip]:
                    state[ip] = True
                    print("health:", name, "reconnected")
                    if TG_TOKEN and TG_CHAT: _tg_reply(f"✅ {name} reconnected")
            else:
                fails[ip] += 1
                if state[ip] and fails[ip] >= 2:          # ~20s before declaring down
                    state[ip] = False
                    print("health:", name, "disconnected")
                    if TG_TOKEN and TG_CHAT: _tg_reply(f"🔌 {name} disconnected")

def main():
    ensure_mediamtx()
    PAGES.update(_build_pages())
    PUBLIC_PAGES.update(_build_pages(public=True))
    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, lambda *_:(_shutdown(), sys.exit(0)))
    if CAMS:
        threading.Thread(target=_cam_health_watch, daemon=True).start()
    TL.recover()                              # a timelapse the last run left behind
    if TG_TOKEN and TG_CHAT:
        threading.Thread(target=_tg_command_loop, daemon=True).start()
        if CAMS:
            threading.Thread(target=_motion_watch, daemon=True).start()
    print(f"App on http://0.0.0.0:{PORT}  | WebRTC via MediaMTX :{WEBRTC_PORT} | recordings: {REC_DIR} | cap {MAX_REC_SECONDS}s")
    print("Links: " + "  ".join(f"{p} ({t})" for p, t in
          [("/", "both")] + [(f"/{i}", c["name"]) for i, c in enumerate(_view_cams(), 1)]))
    try: ThreadingHTTPServer(("0.0.0.0",PORT),H).serve_forever()
    except KeyboardInterrupt: pass
    finally: REC.stop()

if __name__=="__main__":
    main()
