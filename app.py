#!/usr/bin/env python3
"""Two Reolink cameras -> WebRTC page (sub-second), each camera independent.

MediaMTX (see mediamtx.yml) serves each camera as its own WebRTC stream. This
app serves the mobile page (two WHEP players), the record button, Telegram
control, motion alerts, camera health alerts, and delivery to Telegram/Drive.
Recording, snapshots, and motion are all per-camera, so one camera failing
never stops the others.

Run:   ./run.sh              (starts MediaMTX + this app)
Env:   PORT WEBRTC_PORT REC_DIR FFMPEG CAM_IPS
"""
import atexit, glob, json, os, shutil, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT        = int(os.environ.get("PORT", "8088"))
WEBRTC_PORT = int(os.environ.get("WEBRTC_PORT", "8889"))
REC_DIR = os.environ.get("REC_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings"))
os.makedirs(REC_DIR, exist_ok=True)

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
    finally:
        try: os.path.exists(mp4) and os.remove(mp4)
        except OSError: pass

# ---------------------------------------------------------------------------
MAX_REC_SECONDS = int(os.environ.get("REC_MAX_SECONDS", str(8 * 3600)))  # 8h cap

def _cam_paths():
    """[(mediamtx path, friendly name)] for each camera; falls back to composite."""
    if CAMS:
        return [(f"cam{i+1}", name) for i, (ip, name) in enumerate(CAMS)]
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
                "elapsed": int(time.time()-self.started) if (rec and self.started) else 0}
    def status(self):
        with self.lock:
            return self._state()
    def start(self):
        with self.lock:
            if self._alive(): return self._state()
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
<title>Cameras</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
html,body{margin:0;height:100%;background:#000;color:#eee;font:15px/1.4 -apple-system,system-ui,sans-serif}
.stage{position:fixed;inset:0 0 64px 0;display:flex;flex-direction:column;gap:6px;
  align-items:center;justify-content:center;padding:6px;overflow:auto}
.cam{position:relative;max-width:100%;max-height:calc(50% - 6px);display:flex}
.cam video{max-width:100%;max-height:100%;width:auto;background:#000;border-radius:6px}
.cam b{position:absolute;top:6px;left:8px;padding:1px 7px;border-radius:4px;
  background:rgba(0,0,0,.55);font-size:12px;font-weight:600}
@media (min-aspect-ratio:1/1){ .stage{flex-direction:row}
  .cam{max-width:calc(50% - 6px);max-height:100%} }
.bar{position:fixed;left:0;right:0;bottom:0;height:64px;display:flex;gap:12px;align-items:center;
  justify-content:center;background:#0b0b0b;border-top:1px solid #222;padding:0 12px;padding-bottom:env(safe-area-inset-bottom)}
#rec{font:600 16px system-ui;color:#fff;background:#dc2626;border:0;border-radius:999px;padding:12px 22px;cursor:pointer;min-width:150px}
#rec.on{background:#374151}
#snd{font:600 16px system-ui;color:#fff;background:#374151;border:0;border-radius:999px;padding:12px 18px;cursor:pointer}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:#dc2626;margin-right:8px;vertical-align:middle;animation:blink 1s steps(2,start) infinite}
@keyframes blink{to{opacity:.2}}
#stat{color:#bbb;font-variant-numeric:tabular-nums;min-width:150px}
.gate{position:fixed;inset:0;display:flex;flex-direction:column;gap:14px;align-items:center;justify-content:center;background:#000;text-align:center;padding:24px;z-index:5}
.play{font:600 17px system-ui;color:#000;background:#4ade80;border:0;border-radius:999px;padding:14px 30px;cursor:pointer}
.hint{color:#888;font-size:13px;max-width:22em}
#err{position:fixed;left:8px;right:8px;bottom:70px;color:#f87171;font-size:12px;text-align:center}
</style></head><body>
<div class="stage">
  <div class="cam"><b>Camera 1</b><video id="v1" playsinline webkit-playsinline autoplay controls muted></video></div>
  <div class="cam"><b>Camera 2</b><video id="v2" playsinline webkit-playsinline autoplay controls muted></video></div>
</div>
<div class="bar"><button id="snd">🔊 Sound</button><button id="rec">● Record</button><span id="stat">—</span></div>
<div id="err"></div>
<script>
var v1=document.getElementById('v1'),v2=document.getElementById('v2'),
    snd=document.getElementById('snd'),
    rec=document.getElementById('rec'),stat=document.getElementById('stat'),err=document.getElementById('err'),
    WHEP_BASE=__WHEP_BASE__,busy=false,ok={},muted=true;
function say(m){ err.textContent=m; }
if(!window.RTCPeerConnection){ say('this browser has no WebRTC support'); }
function setup(video, path){
  var pc=new RTCPeerConnection({iceServers:[]});
  pc.addTransceiver('video',{direction:'recvonly'});
  pc.addTransceiver('audio',{direction:'recvonly'});
  pc.ontrack=function(e){ try{e.receiver.playoutDelayHint=0;}catch(_){}
    if(video.srcObject!==e.streams[0]) video.srcObject=e.streams[0]; video.play().catch(function(){}); };
  pc.onconnectionstatechange=function(){
    if(pc.connectionState==='connected'){ ok[path]=1; if(ok.cam1&&ok.cam2){say('connected');err.textContent='';} }
    else if(pc.connectionState==='failed'||pc.connectionState==='disconnected'){
      ok[path]=0; say(path+' '+pc.connectionState+' — retrying…');
      setTimeout(function(){ try{pc.close()}catch(e){}; setup(video,path); },1500);
    }
  };
  (async function(){
    try{
      var offer=await pc.createOffer(); await pc.setLocalDescription(offer);
      await new Promise(function(res){ if(pc.iceGatheringState==='complete')return res();
        var t=setTimeout(res,1500);
        pc.onicegatheringstatechange=function(){ if(pc.iceGatheringState==='complete'){clearTimeout(t);res();} }; });
      var url=WHEP_BASE+'/'+path+'/whep';
      var resp=await fetch(url,{method:'POST',headers:{'Content-Type':'application/sdp'},body:pc.localDescription.sdp});
      if(!resp.ok){ say(path+' signaling HTTP '+resp.status); return; }
      await pc.setRemoteDescription({type:'answer',sdp:await resp.text()});
    }catch(e){ say(path+' error: '+e.message); }
  })();
}
setup(v1,'cam1'); setup(v2,'cam2');   // both autoplay muted immediately
// Sound toggle (unmute needs this tap; muted autoplay needs none).
snd.onclick=function(){
  muted=!muted;
  [v1,v2].forEach(function(v){ v.muted=muted; if(!muted){ v.volume=1; v.play().catch(function(){}); } });
  snd.textContent = muted ? '🔊 Sound' : '🔇 Mute';
};
function fmt(s){var m=Math.floor(s/60),ss=s%60;return (m<10?'0':'')+m+':'+(ss<10?'0':'')+ss;}
function render(st){
  if(st.recording){ rec.textContent='■ Stop'; rec.classList.add('on'); stat.innerHTML='<span class="dot"></span>REC '+fmt(st.elapsed); }
  else{ rec.textContent='● Record'; rec.classList.remove('on'); stat.textContent=st.file?('saved '+st.file):'idle'; }
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

class H(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _json(self,obj,code=200):
        b=json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type","application/json"); self.send_header("Cache-Control","no-store")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        p=self.path.split("?",1)[0]
        if p=="/record/start": self._json(REC.start()); return
        if p=="/record/stop":  self._json(REC.stop());  return
        self.send_error(404)
    def do_GET(self):
        p=self.path.split("?",1)[0]
        if p=="/":
            b=PAGE.encode(); self.send_response(200)
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
    cmds = [{"command": "start", "description": "Start recording + motion alerts"},
            {"command": "stop", "description": "Stop recording"},
            {"command": "status", "description": "Recording status"}]
    try: _tg_api("setMyCommands", fields={"commands": _json.dumps(cmds)})
    except Exception: pass

def _tg_snap_reply(caption):
    """Send a snapshot from each online camera, captioned; fall back to text."""
    sent = False
    for ip, name in CAMS:
        img = _cam_snapshot(ip)
        if img:
            try:
                if _tg_send_photo(img, f"{caption} · {name}"): sent = True
            except Exception as e:
                print("telegram: photo failed:", e)
    if not sent:
        _tg_reply(caption)

def _tg_handle(text):
    """Run one command; may block (start/stop/snapshot), so it runs off the poll loop."""
    if text in ("/start", "/record", "/rec"):
        st = REC.start()
        cap = ("🔴 Recording started — watching for movement." if st.get("recording")
               else "⚠️ Could not start (composite unavailable)."
                    if st.get("error") else "Already recording.")
        _tg_snap_reply(cap)
    elif text == "/stop":
        if REC.status().get("recording"):
            _tg_reply("⏹ Stopping…")              # instant ack, then finalize/upload
            REC.stop()
        else:
            _tg_reply("Not recording.")
    elif text == "/status":
        st = REC.status()
        cap = (f"🔴 Recording  {_dur(st['elapsed'])}" if st.get("recording") else "⏹ Idle")
        _tg_snap_reply(cap)
    elif text == "/help":
        _tg_reply("Camera bot:\n/start — start recording + motion alerts\n/stop — stop\n/status — status")

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
    """While recording, alert Telegram on camera detections (pet/person/motion)."""
    print("motion: watcher started")
    last = {}
    while True:
        try:
            if TG_TOKEN and TG_CHAT and REC.status().get("recording"):
                for i, (ip, name) in enumerate(CAMS):
                    campath = f"cam{i+1}"
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
    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, lambda *_:(_shutdown(), sys.exit(0)))
    if CAMS:
        threading.Thread(target=_cam_health_watch, daemon=True).start()
    if TG_TOKEN and TG_CHAT:
        threading.Thread(target=_tg_command_loop, daemon=True).start()
        if CAMS:
            threading.Thread(target=_motion_watch, daemon=True).start()
    print(f"App on http://0.0.0.0:{PORT}  | WebRTC via MediaMTX :{WEBRTC_PORT} | recordings: {REC_DIR} | cap {MAX_REC_SECONDS}s")
    try: ThreadingHTTPServer(("0.0.0.0",PORT),H).serve_forever()
    except KeyboardInterrupt: pass
    finally: REC.stop()

if __name__=="__main__":
    main()
