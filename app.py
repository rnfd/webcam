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
    return _json.loads(urllib.request.urlopen(req, timeout=120).read().decode())

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
            p = s["proc"]
            if p and p.poll() is None:
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
  background:rgba(0,0,0,.55);color:#ddd;font-size:12px;pointer-events:none}
.btns{position:absolute;right:8px;top:8px;display:flex;gap:6px}
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
<div class="bar"><span id="off" hidden>⏸ cameras off</span>__NAV__<button id="rec"><span id="rlab">● Record</span><span id="rsub" hidden></span></button></div>
<div id="err"></div>
<script>
var CAMS=__CAMS__, WHEP_BASE=__WHEP_BASE__,
    grid=document.getElementById('grid'),rec=document.getElementById('rec'),
    rlab=document.getElementById('rlab'),rsub=document.getElementById('rsub'),
    off=document.getElementById('off'),err=document.getElementById('err'),
    players=[],busy=false;
if(!window.RTCPeerConnection){ err.textContent='this browser has no WebRTC support'; }
function note(p,m,bad){ p.msg.textContent=m; p.msg.className='msg'+(bad?' err':''); }
// One independent WHEP player per camera: its own peer connection and its own
// reconnect loop, so one camera dropping never disturbs the other pane.
function setup(p){
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
  off.hidden = (st.enabled!==false);      // /disable from Telegram shows up here too
  rec.disabled = (st.enabled===false);
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

def _render(cams, title, nav):
    return (PAGE.replace("__CAMS__", json.dumps(cams))
                .replace("__TITLE__", title)
                .replace("__NAV__", nav)).encode()

def _build_pages():
    """One page per link: "/" shows every camera, "/1", "/2", ... show one each.
    They are separate links on purpose — open them in two windows, or watch both
    in one. Every page keeps the record button, which records the composite."""
    cams = _view_cams()
    # No per-camera links down here: each pane's ⤢ button opens its own page.
    pages = {"/": _render(cams, "Cameras", "")}
    for i, c in enumerate(cams, 1):
        pages[f"/{i}"] = _render([c], c["name"], '<span class="nav"><a href="/">← both</a></span>')
    return pages
# Built in main(), not here: the pages carry each camera's PTZ capability, and
# asking the camera for it needs helpers defined further down this file.
PAGES = {}

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _json(self,obj,code=200):
        b=json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type","application/json"); self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        p, _, q = self.path.partition("?")
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
        if p in PAGES:
            b=PAGES[p]; self.send_response(200)
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
            {"command": "timelapse","description": "e.g. /timelapse 8h — keep only the dog moments"},
            {"command": "record",   "description": "Start recording"},
            {"command": "stop",     "description": "Stop recording"},
            {"command": "status",   "description": "What is on right now"}]
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

HELP = ("Cameras:\n"
        "/enable — cameras on\n"
        "/disable — cameras off, nothing is captured\n"
        "/follow — alert me on person / pet / motion\n"
        "/unfollow — stop those alerts\n"
        "/timelapse 8h — watch for the dog for 8h, keep ±5s around each\n"
        "    sighting, then send one film per camera (/timelapse stop ends it)\n"
        "/record — start recording\n"
        "/stop — stop recording\n"
        "/status — what is on right now")

def _status_lines():
    st = REC.status()
    return [("📷 Cameras on" if st["enabled"] else "⏸ Cameras off — lenses parked, nothing is captured"),
            ("👁 Following detections" if st["follow"] else "🚫 Not following detections"),
            (f"🔴 Recording {_dur(st['elapsed'])}" if st["recording"] else "⏹ Not recording")] + (
           [f"🎞 Timelapse running · {_dur(tl['elapsed'])} in, {_dur(tl['left'])} left · "
            + ", ".join(f"{nm} {k}" for nm, k in tl["clips"].items()) + " clips"]
           if (tl := TL.status())["active"] else [])

def _tg_handle(text):
    """Run one command; may block (record/stop/snapshot), so it runs off the poll loop."""
    if text == "/enable":
        _tg_reply("📷 Waking the cameras — putting the lenses back…")
        failed = _cameras_on()                 # settings back, lens back to its view
        if not _flag_set("disabled", False):
            _tg_reply("⚠️ Cameras are awake but the switch file could not be cleared."); return
        msg = "📷 Cameras on. The feeds come back in a few seconds."
        if failed: msg += "\n⚠️ Could not restore the view on: " + ", ".join(failed)
        _tg_reply(msg)
    elif text == "/disable":
        if TL.status()["active"]: TL.stop()      # nothing to watch with the lenses parked
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
    elif text.startswith("/timelapse"):
        arg = text[len("/timelapse"):].strip()
        if arg in ("stop", "off", "cancel"):
            r = TL.stop()
            _tg_reply("🎞 Stopping the timelapse — assembling and uploading what it caught…"
                      if r["ok"] else f"⚠️ {r['error']}.")
        elif not arg:
            tl = TL.status()
            if tl["active"]:
                _tg_reply(f"🎞 Running · {_dur(tl['elapsed'])} in, {_dur(tl['left'])} left · "
                          + ", ".join(f"{nm}: {k}" for nm, k in tl["clips"].items()) + " clips")
            else:
                _tg_reply("No timelapse running. Start one with e.g. /timelapse 8h.")
        else:
            secs = _parse_duration(arg)
            if not secs or secs <= 0:
                _tg_reply("⚠️ I need a duration, e.g. /timelapse 8h, /timelapse 90m, "
                          "or /timelapse stop."); return
            if secs > TL_MAX_HOURS * 3600:
                _tg_reply(f"⚠️ That is longer than the {TL_MAX_HOURS:g}h limit."); return
            r = TL.start(secs)
            if not r["ok"]: _tg_reply(f"⚠️ Could not start: {r['error']}."); return
            msg = (f"🎞 Timelapse for {_dur(int(secs))} — watching both cameras for the dog, "
                   f"keeping ±{TL_PRE:g}s around every sighting. "
                   f"One film per camera to Drive at the end; /timelapse stop ends it early.")
            if r["enabled_dog_on"]:
                msg += "\n(Dog detection was off on " + ", ".join(r["enabled_dog_on"]) + "; I turned it on.)"
            _tg_reply(msg)
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
    elif text == "/help":
        _tg_reply(HELP)

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
                    continue                                  # owner only
                text = (m.get("text") or "").strip().lower().split("@")[0]
                if not text:
                    continue
                age = time.time() - m.get("date", time.time())
                if age > STALE_SECONDS:
                    print(f"telegram: skipping stale {text!r} ({age:.0f}s old)")
                    continue
                threading.Thread(target=_tg_handle, args=(text,), daemon=True).start()
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
# Watch each camera for the dog, and keep only the seconds around each sighting:
# every detection copies [t-5s, t+5s] out of the rolling buffer MediaMTX already
# keeps, so the five seconds *before* the dog appeared are there too. At the end
# the pieces are concatenated per camera — stream-copied, so no quality is lost
# and assembly takes seconds — and each camera's film goes to Google Drive.
TL_PRE   = float(os.environ.get("TL_PRE", "5"))
TL_POST  = float(os.environ.get("TL_POST", "5"))
TL_PATH  = os.environ.get("TL_PATH", "cam{n}")      # HD buffer ({n} = camera number)
TL_MAX_CLIPS = int(os.environ.get("TL_MAX_CLIPS", "600"))   # ~100 min of footage
TL_MAX_HOURS = float(os.environ.get("TL_MAX_HOURS", "24"))

def _parse_duration(text):
    """'8h' / '90m' / '45s' / '2h30m' / '8' (hours) -> seconds, or None."""
    import re
    t = text.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", t): return float(t) * 3600
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([hms])", t)
    if not parts or re.sub(r"\d|\.|\s|[hms]", "", t): return None
    return sum(float(v) * {"h": 3600, "m": 60, "s": 1}[u] for v, u in parts)

def _buffer_grab(path, start_epoch, duration, out_path):
    """Copy a window out of MediaMTX's rolling recording, keeping it as it was
    recorded (no re-encode)."""
    import urllib.request, urllib.parse, datetime, tempfile
    start = datetime.datetime.fromtimestamp(start_epoch, datetime.timezone.utc)
    url = PLAYBACK + "/get?" + urllib.parse.urlencode(
        {"path": path, "start": start.isoformat().replace("+00:00", "Z"), "duration": duration})
    fd, raw = tempfile.mkstemp(suffix=".mp4"); os.close(fd)
    try:
        with open(raw, "wb") as f: f.write(urllib.request.urlopen(url, timeout=60).read())
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", raw, "-c", "copy",
                        "-movflags", "+faststart", out_path], check=True, timeout=120)
        return os.path.getsize(out_path) > 2000
    except Exception as e:
        print("timelapse: clip fetch failed:", e); return False
    finally:
        try: os.remove(raw)
        except OSError: pass

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

def _enable_dog_detection():
    """The dog class ships disabled on these cameras; without it nothing triggers."""
    turned_on = []
    for ip, name in CAMS:
        try:
            cfg = _cam_api(ip, [{"cmd": "GetAiCfg", "action": 0, "param": {"channel": 0}}])[0]["value"]
            if cfg.get("AiDetectType", {}).get("dog_cat"): continue
            want = dict(cfg); want["AiDetectType"] = dict(cfg["AiDetectType"], dog_cat=1)
            _cam_api(ip, [{"cmd": "SetAiCfg", "action": 0, "param": want}])
            turned_on.append(name)
        except Exception as e:
            print(f"timelapse: could not enable dog detection on {name}: {e}")
    return turned_on

class Timelapse:
    """One session across all cameras; each camera collects its own clips."""
    def __init__(self):
        self.lock = threading.Lock()
        self.active = False; self.started = 0.0; self.until = 0.0
        self.dir = None; self.clips = {}; self.thread = None
        self.stop_evt = threading.Event(); self.jobs = []

    def status(self):
        with self.lock:
            if not self.active: return {"active": False}
            return {"active": True, "elapsed": int(time.time() - self.started),
                    "left": max(0, int(self.until - time.time())),
                    "clips": {n: len(v) for n, v in self.clips.items()}}

    def start(self, secs):
        with self.lock:
            if self.active: return {"ok": False, "error": "a timelapse is already running"}
            if not cams_enabled(): return {"ok": False, "error": "cameras are disabled"}
            if not CAMS: return {"ok": False, "error": "no cameras configured"}
            ts = time.strftime("%Y%m%d_%H%M%S")
            self.dir = os.path.join(REC_DIR, f"timelapse_{ts}")
            try: os.makedirs(self.dir, exist_ok=True)
            except OSError as e: return {"ok": False, "error": f"cannot create {self.dir}: {e}"}
            self.clips = {name: [] for _ip, name in CAMS}
            self.active = True; self.started = time.time(); self.until = self.started + secs
            self.stop_evt.clear(); self.jobs = []
        enabled = _enable_dog_detection()
        self.thread = threading.Thread(target=self._watch, daemon=True); self.thread.start()
        return {"ok": True, "secs": secs, "enabled_dog_on": enabled}

    def stop(self):
        with self.lock:
            if not self.active: return {"ok": False, "error": "no timelapse is running"}
        self.stop_evt.set()
        return {"ok": True}

    def _watch(self):
        """Poll each camera for the dog; grab a window around every sighting."""
        last = {}
        gap = TL_PRE + TL_POST                    # never overlap two windows
        print("timelapse: watching for the dog")
        while not self.stop_evt.is_set():
            now = time.time()
            if now >= self.until: break
            if cams_enabled():
                for i, (ip, name) in enumerate(CAMS, 1):
                    with self.lock: n_clips = sum(len(v) for v in self.clips.values())
                    if n_clips >= TL_MAX_CLIPS:
                        print("timelapse: clip cap reached"); self.stop_evt.set(); break
                    try: hit = "pet" in _cam_detections(ip)
                    except Exception: hit = False
                    if hit and now - last.get(name, 0) > gap:
                        last[name] = now
                        t = threading.Thread(target=self._capture, args=(i, name, now), daemon=True)
                        t.start()
                        with self.lock: self.jobs.append(t)
            self.stop_evt.wait(2)
        self._finish()

    def _capture(self, n, name, det_epoch):
        time.sleep(TL_POST + 1.5)                 # let the tail of the window record
        with self.lock:
            if self.dir is None: return
            seq = len(self.clips[name]) + 1
            out = os.path.join(self.dir, f"cam{n}_{seq:04d}.mp4")
        if _buffer_grab(TL_PATH.replace("{n}", str(n)), det_epoch - TL_PRE, TL_PRE + TL_POST, out):
            with self.lock: self.clips[name].append(out)
            print(f"timelapse: {name} +1 clip ({seq})")

    def _finish(self):
        with self.lock:
            jobs, clips, sess_dir = list(self.jobs), dict(self.clips), self.dir
            elapsed = int(time.time() - self.started)
        for t in jobs: t.join(TL_POST + 60)       # let in-flight grabs land
        with self.lock:
            clips = dict(self.clips)
            self.active = False; self.dir = None
        made = []
        for i, (_ip, name) in enumerate(CAMS, 1):
            files = clips.get(name) or []
            if not files: continue
            out = os.path.join(REC_DIR, f"{os.path.basename(sess_dir)}_cam{i}.mp4")
            if _concat(files, out):
                made.append((name, out, len(files)))
        if _tg_enabled():
            if made:
                _tg_reply("🎞 Timelapse finished — " + ", ".join(
                    f"{nm}: {k} clip{'s' if k != 1 else ''}" for nm, _p, k in made)
                    + ".\nUploading…")
            else:
                _tg_reply("🎞 Timelapse finished — the dog never showed up, nothing to upload.")
        for _nm, path, _k in made:
            try: _deliver(path, elapsed)
            finally:
                try: os.path.exists(path) and os.remove(path)
                except OSError: pass
        try: shutil.rmtree(sess_dir, ignore_errors=True)
        except Exception: pass
        print("timelapse: done")
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
    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, lambda *_:(_shutdown(), sys.exit(0)))
    if CAMS:
        threading.Thread(target=_cam_health_watch, daemon=True).start()
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
