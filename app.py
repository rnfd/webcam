#!/usr/bin/env python3
"""Two Reolink cameras -> WebRTC page (sub-second): stacked video, mixed audio,
plus server-side record on/off that survives refreshes.

MediaMTX (see mediamtx.yml) composites both cameras into one 'composite' stream
and serves it over WebRTC. This app serves the mobile page (a WHEP client) and
the record button. Recording stream-copies MediaMTX's composite (no extra load
on the cameras, no re-encode).

Run:   ./run.sh              (starts MediaMTX + this app)
Env:   PORT WEBRTC_PORT REC_DIR FFMPEG RTSP_COMPOSITE
"""
import atexit, glob, json, os, shutil, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT        = int(os.environ.get("PORT", "8088"))
WEBRTC_PORT = int(os.environ.get("WEBRTC_PORT", "8889"))
RTSP_COMPOSITE = os.environ.get("RTSP_COMPOSITE", "rtsp://localhost:8554/composite")
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

def _tg_finalize(msg_id, base, elapsed, link):
    text = (f"📹 {base}  ({_dur(elapsed)})\n✅ Uploaded\n{link}" if link
            else f"📹 {base}  ({_dur(elapsed)})\n✅ Saved to Drive (link unavailable).")
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
        tg, gd = _tg_enabled(), _gdrive_enabled()
        msg_id = _tg_send_initial(base, elapsed) if tg else None
        prog = _Prog(msg_id, base, elapsed) if (tg and msg_id) else None
        link = _gdrive_upload(mp4, on_progress=prog) if gd else None
        if tg:
            try:
                if msg_id: _tg_finalize(msg_id, base, elapsed, link)
                else:      _tg_send_link(base, elapsed, link)
            except Exception as e: print("telegram: failed:", e)
    finally:
        try: os.path.exists(mp4) and os.remove(mp4)
        except OSError: pass

# ---------------------------------------------------------------------------
MAX_REC_SECONDS = int(os.environ.get("REC_MAX_SECONDS", str(8 * 3600)))  # 8h cap

class Recorder:
    def __init__(self):
        self.lock = threading.Lock(); self.proc=None; self.file=None; self.started=None
        self._timer=None
    def _arm_cap(self):
        if self._timer:
            try: self._timer.cancel()
            except Exception: pass
        self._timer = threading.Timer(MAX_REC_SECONDS, self._auto_stop)
        self._timer.daemon = True; self._timer.start()
    def _disarm_cap(self):
        if self._timer:
            try: self._timer.cancel()
            except Exception: pass
            self._timer = None
    def _auto_stop(self):
        if self.proc and self.proc.poll() is None:
            print(f"recorder: reached {MAX_REC_SECONDS}s cap, auto-stopping")
            self.stop()
    def _state(self):
        return {"recording": self.proc is not None,
                "file": os.path.basename(self.file) if self.file else None,
                "elapsed": int(time.time()-self.started) if self.started else 0}
    def status(self):
        with self.lock:
            if self.proc and self.proc.poll() is not None:
                self.proc=None; self.file=None; self.started=None
            return self._state()
    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None: return self._state()
            path = os.path.join(REC_DIR, time.strftime("rec_%Y%m%d_%H%M%S.mkv"))
            # copy the already-composited stream (video H264 + audio Opus) to mkv
            cmd = [FFMPEG, "-loglevel", "error", "-nostdin",
                   "-rtsp_transport", "tcp", "-i", RTSP_COMPOSITE,
                   "-map", "0", "-c", "copy", "-f", "matroska", path]
            # retry a few times in case the composite is momentarily unavailable
            for attempt in range(4):
                p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                time.sleep(0.6)
                if p.poll() is None:              # still running -> connected OK
                    self.proc = p; self.file = path; self.started = time.time()
                    self._arm_cap()               # auto-stop after MAX_REC_SECONDS
                    return self._state()
                try: os.path.exists(path) and os.path.getsize(path) == 0 and os.remove(path)
                except OSError: pass
                time.sleep(0.8)
            # gave up
            self.proc = None; self.file = None; self.started = None
            return {"recording": False, "file": None, "elapsed": 0, "error": "composite unavailable"}
    def stop(self):
        with self.lock:
            self._disarm_cap()
            p=self.proc; done=self.file; st=self.started
            if not p or p.poll() is not None:
                self.proc=None; self.file=None; self.started=None
                return {"recording":False,"file":None,"elapsed":0}
        elapsed = int(time.time()-st) if st else 0
        try:
            if p.stdin:
                try: p.stdin.write(b"q"); p.stdin.flush()
                except Exception: pass
            try: p.send_signal(signal.SIGINT)
            except Exception: pass
            try: p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                p.terminate()
                try: p.wait(timeout=4)
                except subprocess.TimeoutExpired: p.kill()
        finally:
            with self.lock:
                self.proc=None; self.file=None; self.started=None
        # ship the finished file to configured sinks in the background (never blocks stop)
        if done and _any_sink():
            threading.Thread(target=_post_record, args=(done, elapsed),
                             daemon=True).start()
        return {"recording":False,"file":os.path.basename(done) if done else None,"elapsed":0}
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
<div class="bar"><button id="rec">● Record</button><span id="stat">—</span></div>
<div class="gate" id="gate">
  <div style="font-size:20px;font-weight:700">Cameras</div>
  <button class="play" id="go">▶  Play with sound</button>
  <div class="hint">Two independent camera feeds, near real-time. Tap to start.</div>
  <div class="hint" id="cstat" style="color:#4ade80">starting…</div>
</div>
<div id="err"></div>
<script>
var v1=document.getElementById('v1'),v2=document.getElementById('v2'),
    gate=document.getElementById('gate'),go=document.getElementById('go'),
    rec=document.getElementById('rec'),stat=document.getElementById('stat'),err=document.getElementById('err'),
    cstat=document.getElementById('cstat'),WHEP_BASE=__WHEP_BASE__,busy=false,ok={};
function say(m){ cstat.textContent=m; }
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
setup(v1,'cam1'); setup(v2,'cam2');
// Tap = unmute + play both, dismiss the gate no matter what.
go.onclick=function(){
  [v1,v2].forEach(function(v){ v.muted=false; v.volume=1; var p=v.play(); if(p&&p.catch)p.catch(function(){}); });
  gate.style.display='none';
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

def _snapshot():
    """Grab one frame from the composite (both cameras) as a JPEG; return path."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".jpg", prefix="snap_"); os.close(fd)
    try:
        subprocess.run([FFMPEG, "-loglevel", "error", "-rtsp_transport", "tcp",
                        "-i", RTSP_COMPOSITE, "-frames:v", "1", "-q:v", "3", "-y", path],
                       check=True, timeout=15)
        if os.path.getsize(path) > 0:
            return path
    except Exception as e:
        print("snapshot failed:", e)
    try: os.remove(path)
    except OSError: pass
    return None

def _tg_snap_reply(caption):
    """Send the current composite frame as a photo, captioned; fall back to text."""
    snap = _snapshot()
    if not snap:
        _tg_reply(caption); return
    try:
        boundary, body = _tg_multipart({"chat_id": TG_CHAT, "caption": caption},
                                       "photo", snap, ctype="image/jpeg")
        r = _tg_api("sendPhoto", boundary=boundary, body=body)
        if not r.get("ok"): _tg_reply(caption)
    except Exception as e:
        print("telegram: photo failed:", e); _tg_reply(caption)
    finally:
        try: os.remove(snap)
        except OSError: pass

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

def _notify_motion(ip, name, ev):
    cap = f"{_MOTION_LABEL.get(ev, ev)} detected · {name}"
    img = _cam_snapshot(ip)
    if img:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jpg"); os.close(fd)
        with open(path, "wb") as f: f.write(img)
        try:
            boundary, body = _tg_multipart({"chat_id": TG_CHAT, "caption": cap},
                                           "photo", path, ctype="image/jpeg")
            _tg_api("sendPhoto", boundary=boundary, body=body)
        except Exception as e:
            print("motion: photo failed:", e); _tg_reply(cap)
        finally:
            try: os.remove(path)
            except OSError: pass
    else:
        _tg_reply(cap)
    print("motion:", cap)

def _motion_watch():
    """While recording, alert Telegram on camera detections (pet/person/motion)."""
    print("motion: watcher started")
    last = {}
    while True:
        try:
            if TG_TOKEN and TG_CHAT and REC.status().get("recording"):
                for ip, name in CAMS:
                    for ev in _cam_detections(ip):
                        key = (ip, ev)
                        if time.time() - last.get(key, 0) > MOTION_COOLDOWN:
                            last[key] = time.time()
                            threading.Thread(target=_notify_motion, args=(ip, name, ev),
                                             daemon=True).start()
            time.sleep(2)
        except Exception as e:
            print("motion: loop error:", e); time.sleep(5)

def main():
    ensure_mediamtx()
    atexit.register(_shutdown)
    signal.signal(signal.SIGTERM, lambda *_:(_shutdown(), sys.exit(0)))
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
