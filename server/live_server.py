"""TV Ambilight server: Android TV screen -> LED colours -> WLED.

- Receives Hyperion protobuf frames from the Android "Hyperion Grabber" app on TCP 19445 (via `adb reverse`).
- Turns each frame into one colour per LED around the screen (clockwise from the top-left corner),
  with black-bar removal, adaptive smoothing, saturation / contrast controls.
- Sends the colours to a WLED controller over the network (DDP, UDP port 4048).
- Keeps the grabber on the TV running (watchdog over ADB) and serves a live web page.

Settings (environment variables, see README):
  LEDLIVE_TV        TV address for ADB, e.g. 192.168.1.50:5555            (required)
  LEDLIVE_WLED      WLED address or mDNS name, e.g. wled-a1b2c3.local     (required)
  LEDLIVE_LAYOUT    LEDs per side: top,right,bottom,left (default 33,21,33,21)
  LEDLIVE_TV_MODEL  part of the TV model name, used to re-find the TV if its IP changes (default: any)
  LEDLIVE_WEB_BIND  127.0.0.1 (this machine only, default) or 0.0.0.0 (whole network)
  LEDLIVE_HP_BIND   127.0.0.1 (frames via the ADB tunnel, default) or 0.0.0.0 (TV sends directly over Wi-Fi,
                    ~40 ms faster per frame; only the TV's address is accepted)

Run:  python3 live_server.py
"""
import colorsys, json, math, os, socket, struct, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HP_PORT, WEB_PORT = 19445, int(os.environ.get("LEDLIVE_WEB_PORT", "8765"))
WEB_BIND = os.environ.get("LEDLIVE_WEB_BIND", "127.0.0.1")  # 0.0.0.0 = reachable from other devices
HERE = os.path.dirname(os.path.abspath(__file__))
# LEDs per side, clockwise from the top-left corner (measure yours with tools/corner_test.py)
TOP, RIGHT, BOTTOM, LEFT = (int(v) for v in os.environ.get("LEDLIVE_LAYOUT", "33,21,33,21").split(","))
NUM_LEDS = TOP + RIGHT + BOTTOM + LEFT
P = dict(blur_radius=4, smoothing=0.7, sat_gain=1.8, contrast=1.0, gamma=0.75, gain=1.15, black_cut=0.06,
         black_bars=1.0, send=1.0)
ADJUSTABLE = {"sat_gain": (0.0, 3.0), "contrast": (0.5, 2.5), "black_bars": (0.0, 1.0),
              "send": (0.0, 1.0), "smoothing": (0.0, 1.0)}  # page controls: (min, max)
BIG_CHANGE = 0.08   # a colour change this large (0-1, per channel) skips smoothing. Measured noise is <0.01
                    # for 90% of frames, so anything bigger is a real change and should show at once
SETTINGS = os.path.join(HERE, "settings.json")
try:
    P.update({k: float(v) for k, v in json.load(open(SETTINGS)).items() if k in ADJUSTABLE})
except (OSError, ValueError):
    pass
REPLY = b"\x08\x01\x10\x01"
REPLY_MSG = struct.pack(">I", len(REPLY)) + REPLY

# ------------------------------------------------------------------ LED processing

_zone_cache = {}


def zones(W, H):
    if (W, H) in _zone_cache:
        return _zone_cache[(W, H)]
    dx, dy = max(1, round(W * 0.12)), max(1, round(H * 0.15))
    z = []
    for i in range(TOP):
        x0 = i * W // TOP; z.append((x0, max(x0 + 1, (i + 1) * W // TOP), 0, dy))
    for i in range(RIGHT):
        y0 = i * H // RIGHT; z.append((W - dx, W, y0, max(y0 + 1, (i + 1) * H // RIGHT)))
    for i in range(BOTTOM):
        x1 = W - i * W // BOTTOM; z.append((min(x1 - 1, W - (i + 1) * W // BOTTOM), x1, H - dy, H))
    for i in range(LEFT):
        y1 = H - i * H // LEFT; z.append((0, dx, min(y1 - 1, H - (i + 1) * H // LEFT), y1))
    _zone_cache[(W, H)] = z
    return z


def zone_means(data, W, H):
    out = []
    for x0, x1, y0, y1 in zones(W, H):
        r = g = b = n = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                p = (y * W + x) * 3
                r += data[p]; g += data[p + 1]; b += data[p + 2]; n += 1
        out.append((r / n / 255, g / n / 255, b / n / 255))
    return out


_kernel = None


def blur(leds, rad):
    global _kernel
    if _kernel is None:
        w = [math.exp(-(k * k) / (2 * (rad / 1.5) ** 2)) for k in range(-rad, rad + 1)]
        s = sum(w); _kernel = [x / s for x in w]
    n = len(leds)
    return [tuple(sum(leds[(i + k) % n][c] * _kernel[j] for j, k in enumerate(range(-rad, rad + 1))) for c in range(3))
            for i in range(n)]


def grade(c):
    h, s, v = colorsys.rgb_to_hsv(*c)
    s = min(1.0, s * P["sat_gain"]); v = min(1.0, v ** P["gamma"] * P["gain"])
    # contrast: push brightness away from mid-grey (>1 = darker darks, brighter brights)
    v = min(1.0, max(0.0, (v - 0.5) * P["contrast"] + 0.5))
    return colorsys.hsv_to_rgb(h, s, 0.0 if v < P["black_cut"] else v)


class BarDetector:
    """Finds letterbox (top/bottom) and pillarbox (left/right) black bars and keeps a stable crop.

    - A bar row/column must be near-pure black (bars are encoded black; dark scenes are not).
    - Top/bottom (and left/right) use the thinner side, so subtitles inside a bar don't shrink the crop.
    - A new crop only applies after it has been seen steadily: 3 s to add/grow bars, 1 s to remove them.
    - A frame that is dark almost everywhere says nothing about bars and is ignored.
    """
    DARK = 24          # brightest channel at or below this counts as black (0-255)
    NOISE = 0.04       # fraction of non-black pixels a bar line may still contain
    MAX_FRAC = 0.30    # bars never cover more than 30% of a side
    GROW_S, SHRINK_S = 3.0, 1.0

    def __init__(self):
        self.crop = (0, 0, 0, 0)   # top, bottom, left, right (pixels)
        self.size = None
        self.pending, self.since = None, 0.0

    MOSTLY = 0.5       # a bar line with subtitles/logos: at least half of it still black

    def _bright(self, data, idxs):
        return sum(1 for p in idxs if max(data[p], data[p + 1], data[p + 2]) > self.DARK)

    def detect(self, data, W, H):
        rows = [[(y * W + x) * 3 for x in range(W)] for y in range(H)]
        cols = [[(y * W + x) * 3 for y in range(H)] for x in range(W)]
        strict = lambda line: self._bright(data, line) <= int(len(line) * self.NOISE)
        loose = lambda line: self._bright(data, line) <= int(len(line) * (1 - self.MOSTLY))

        if sum(1 for r in rows if strict(r)) >= H - 1:   # (almost) all black: says nothing about bars
            return None

        def run(lines, test, limit):
            n = 0
            for line in lines[:limit]:
                if not test(line):
                    break
                n += 1
            return n

        def pair(lines, limit):
            a_s, b_s = run(lines, strict, limit), run(lines[::-1], strict, limit)
            a_l, b_l = run(lines, loose, limit), run(lines[::-1], loose, limit)
            # symmetric crop; a side with subtitles may count "mostly black" lines,
            # but never deeper than the cleanly black opposite side
            return min(max(a_s, a_l), max(b_s, b_l), max(a_s, b_s))

        v = pair(rows, int(H * self.MAX_FRAC))
        hz = pair(cols, int(W * self.MAX_FRAC))
        return (v, v, hz, hz)

    def update(self, data, W, H, now):
        if self.size != (W, H):                    # capture size changed: start over
            self.size, self.crop, self.pending = (W, H), (0, 0, 0, 0), None
        cand = self.detect(data, W, H)
        if cand is None or cand == self.crop:
            self.pending = None
            return self.crop
        if cand != self.pending:
            self.pending, self.since = cand, now
            return self.crop
        need = self.GROW_S if sum(cand) > sum(self.crop) else self.SHRINK_S
        if now - self.since >= need:
            self.crop, self.pending = cand, None
            t, _, l, _ = cand
            print(f"[{time.strftime('%H:%M:%S')}] black bars: "
                  + (f"{t} rows top/bottom, {l} cols left/right of {W}x{H} cropped" if any(cand) else "none"), flush=True)
        return self.crop


def crop_image(data, W, H, crop):
    t, b, l, r = crop
    if not any(crop):
        return data, W, H
    nw, nh = W - l - r, H - t - b
    out = bytearray()
    for y in range(t, H - b):
        s = (y * W + l) * 3
        out += data[s:s + nw * 3]
    return bytes(out), nw, nh


def hexs(cols):
    return "".join("%02x%02x%02x" % tuple(max(0, min(255, round(v * 255))) for v in c) for c in cols)

# ------------------------------------------------------------------ shared state

lock = threading.Condition()
state = dict(seq=0, connected=False, client="", w=0, h=0, img="", raw="", leds="", t=0.0, fps=0.0,
             gap_ms=0, kbps=0.0, total=0, params={k: P[k] for k in ADJUSTABLE},
             layout=[TOP, RIGHT, BOTTOM, LEFT])
_smooth = None
_times = []


def set_params(new):
    """Apply slider values, re-grade the current LEDs right away, save to settings.json."""
    changed = {}
    for k, v in new.items():
        if k in ADJUSTABLE:
            lo, hi = ADJUSTABLE[k]
            P[k] = changed[k] = round(min(hi, max(lo, float(v))), 3)
    json.dump({k: P[k] for k in ADJUSTABLE}, open(SETTINGS, "w"))
    with lock:
        state["params"] = {k: P[k] for k in ADJUSTABLE}
        if _smooth is not None:
            graded = [grade(c) for c in _smooth]
            state["leds"] = hexs(graded)
            if P["send"] >= 0.5:
                ddp.send(led_bytes(graded))     # show slider changes on the strip right away
        state["seq"] += 1
        lock.notify_all()
    return changed


bars = BarDetector()

# ------------------------------------------------------------------ DDP output to WLED

WLED_HOST = os.environ.get("LEDLIVE_WLED", "")
DDP_PORT = 4048
KEEPALIVE_S = 1.0          # resend the last colours this often on a still screen (WLED drops live mode after 2.5 s)


class DDPSender:
    def __init__(self, host):
        self.host, self.addr, self.resolved_at = host, None, 0.0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq, self.sent, self.errors, self.last_send, self.last_rgb = 0, 0, 0, 0.0, None

    def _address(self):
        if self.addr is None or time.time() - self.resolved_at > 60:     # re-resolve each minute (DHCP may change)
            try:
                self.addr = (socket.gethostbyname(self.host), DDP_PORT)
            except OSError:
                if self.addr is None:
                    raise
            self.resolved_at = time.time()
        return self.addr

    def send(self, rgb):
        if not self.host:          # LEDLIVE_WLED not set: preview only
            return
        self.last_rgb = rgb
        self.seq = self.seq % 15 + 1
        # DDP header: v1 + push, sequence, RGB 8-bit, output 1, offset 0, data length
        header = bytes([0x41, self.seq, 0x0B, 0x01]) + struct.pack(">IH", 0, len(rgb))
        try:
            self.sock.sendto(header + rgb, self._address())
            self.sent += 1
        except OSError:
            self.errors += 1
        self.last_send = time.time()


ddp = DDPSender(WLED_HOST)


def led_bytes(cols):
    return bytes(max(0, min(255, round(v * 255))) for c in cols for v in c)


def keepalive():
    while True:
        time.sleep(0.25)
        if P["send"] >= 0.5 and ddp.last_rgb and time.time() - ddp.last_send > KEEPALIVE_S:
            with lock:
                connected = state["connected"]
            if connected:          # only hold the colours while the TV grabber is connected
                ddp.send(ddp.last_rgb)


def smooth_step(new, old, smoothing):
    """Adaptive smoothing for one LED.

    smoothing 0 = no smoothing (instant), 1 = very smooth. Small changes (noise, slow pans) move a fraction
    of the way each frame; big jumps (scene cuts, fades to black) go through immediately.
    """
    base = 1.0 - 0.9 * smoothing                  # weight of the new colour for small changes
    delta = max(abs(a - b) for a, b in zip(new, old))
    k = min(1.0, base + (1.0 - base) * min(1.0, delta / BIG_CHANGE))
    return tuple(k * a + (1 - k) * b for a, b in zip(new, old))


def on_frame(w, h, data, nbytes):
    global _smooth
    now = time.time()
    crop = bars.update(data, w, h, now) if P["black_bars"] >= 0.5 else (0, 0, 0, 0)
    cdata, cw, ch = crop_image(data, w, h, crop)
    means = zone_means(cdata, cw, ch)
    raw = blur(means, P["blur_radius"])
    _smooth = raw if _smooth is None or len(_smooth) != len(raw) else \
        [smooth_step(nc, oc, P["smoothing"]) for nc, oc in zip(raw, _smooth)]
    _times.append((now, nbytes))
    while _times and now - _times[0][0] > 3:
        _times.pop(0)
    span = max(0.001, _times[-1][0] - _times[0][0]) if len(_times) > 1 else 1
    graded = [grade(c) for c in _smooth]
    if P["send"] >= 0.5:
        ddp.send(led_bytes(graded))
    with lock:
        gap = (now - state["t"]) * 1000 if state["t"] else 0
        state.update(seq=state["seq"] + 1, w=w, h=h, img=data.hex(), raw=hexs(means), crop=list(crop),
                     leds=hexs(graded), t=now, ddp=dict(host=WLED_HOST, sent=ddp.sent, errors=ddp.errors),
                     fps=round((len(_times) - 1) / span, 1) if len(_times) > 1 else 0,
                     gap_ms=round(gap), kbps=round(sum(b for _, b in _times) / span / 1024, 1),
                     total=state["total"] + 1)
        lock.notify_all()

# ------------------------------------------------------------------ Hyperion protobuf receiver


def varint(buf, i):
    shift = val = 0
    while True:
        b = buf[i]; i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7


def fields(buf):
    i, out = 0, {}
    while i < len(buf):
        key, i = varint(buf, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = varint(buf, i)
        elif wt == 2:
            n, i = varint(buf, i); v = buf[i:i + n]; i += n
        elif wt == 5:
            v = buf[i:i + 4]; i += 4
        elif wt == 1:
            v = buf[i:i + 8]; i += 8
        else:
            raise ValueError("wire type %d" % wt)
        out[num] = v
    return out


QUICKACK = getattr(socket, "TCP_QUICKACK", None)   # Linux only


def recv_exact(c, n):
    d = bytearray()
    while len(d) < n:
        x = c.recv(n - len(d))
        if not x:
            raise ConnectionError("closed")
        d += x
        if QUICKACK:
            # acknowledge at once: the sender writes the 4-byte length and the picture separately and
            # waits for this ACK before sending the picture (Linux would otherwise delay it ~40 ms)
            c.setsockopt(socket.IPPROTO_TCP, QUICKACK, 1)
    return bytes(d)


def handle(conn, addr):
    with lock:
        state.update(connected=True, client=f"{addr[0]}:{addr[1]}"); lock.notify_all()
    print(f"[{time.strftime('%H:%M:%S')}] grabber connected {addr[0]}:{addr[1]}", flush=True)
    try:
        while True:
            size = struct.unpack(">I", recv_exact(conn, 4))[0]
            msg = recv_exact(conn, size)
            conn.sendall(REPLY_MSG)
            req = fields(msg)
            if req.get(1) == 2 and 11 in req:
                img = fields(req[11])
                w, h, data = img[2], img[3], img[4]
                if len(data) == w * h * 3:
                    on_frame(w, h, data, size + 4)
    except (ConnectionError, OSError, ValueError) as e:
        print(f"[{time.strftime('%H:%M:%S')}] grabber disconnected ({e})", flush=True)
    finally:
        conn.close()
        with lock:
            state["connected"] = False; lock.notify_all()


HP_BIND = os.environ.get("LEDLIVE_HP_BIND", "127.0.0.1")   # 0.0.0.0 = the TV can connect directly over Wi-Fi


def hyperion_server():
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HP_BIND, HP_PORT)); s.listen(4)
    print(f"Hyperion receiver on {HP_BIND}:{HP_PORT}", flush=True)
    while True:
        c, a = s.accept()
        # only the TV (direct Wi-Fi) or this machine (the ADB tunnel) may send frames
        if a[0] != "127.0.0.1" and a[0] != TV.partition(":")[0]:
            print(f"[{time.strftime('%H:%M:%S')}] refused frame connection from {a[0]}", flush=True)
            c.close(); continue
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=handle, args=(c, a), daemon=True).start()

# ------------------------------------------------------------------ web page


import hashlib, hmac, html, secrets
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

AUTH_FILE = os.path.join(HERE, "auth.json")          # created by set_password.py: salted hash, never the password
SESSIONS_FILE = os.path.join(HERE, "sessions.json")  # sha256 of session tokens -> expiry (survives restarts)
COOKIE = "ledlive_session"
SESSION_DAYS = 30
_sessions_lock = threading.Lock()


def load_auth():
    try:
        return json.load(open(AUTH_FILE))
    except (OSError, ValueError):
        return None


def password_ok(user, pw):
    auth = load_auth()
    if not auth:
        return True
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(auth["salt"]), auth["iterations"]).hex()
    return hmac.compare_digest(user.strip().lower(), auth["user"].lower()) and hmac.compare_digest(digest, auth["hash"])


def _load_sessions():
    try:
        s = json.load(open(SESSIONS_FILE))
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in s.items() if v > now}


def _save_sessions(s):
    fd = os.open(SESSIONS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(s, f)


def new_session():
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        s = _load_sessions()
        s[hashlib.sha256(token.encode()).hexdigest()] = time.time() + SESSION_DAYS * 86400
        _save_sessions(s)
    return token


def session_ok(token):
    if not token:
        return False
    with _sessions_lock:
        return hashlib.sha256(token.encode()).hexdigest() in _load_sessions()


def end_session(token):
    if token:
        with _sessions_lock:
            s = _load_sessions(); s.pop(hashlib.sha256(token.encode()).hexdigest(), None); _save_sessions(s)


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>LED Live sign in</title>
<style>
:root{--bg:#f6f6f4;--card:#fff;--text:#151515;--muted:#6b6b66;--line:rgba(0,0,0,.12);--bad:#c9443b;--btn:#151515;--btntext:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#0e1013;--card:#171a1f;--text:#eef0f2;--muted:#8a919b;--line:rgba(255,255,255,.14);--bad:#ef6a5f;--btn:#eef0f2;--btntext:#111}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
form{width:100%;max-width:340px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}
h1{font-size:20px;margin:0 0 4px}p{color:var(--muted);font-size:13px;margin:0 0 18px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px}
input{width:100%;font:inherit;color:var(--text);background:transparent;border:1px solid var(--line);border-radius:8px;padding:9px 10px}
button{width:100%;margin-top:18px;font:inherit;font-weight:600;background:var(--btn);color:var(--btntext);border:0;border-radius:8px;padding:10px;cursor:pointer}
.err{color:var(--bad);font-size:13px;margin-top:12px}
</style></head><body>
<form method="post" action="/login">
<h1>LED live</h1><p>Sign in to see the TV backlight data.</p>
<label for="u">Email</label><input id="u" name="user" type="email" autocomplete="username" required value="__USER__">
<label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required __FOCUS__>
<button type="submit">Sign in</button>__ERROR__
</form></body></html>"""


class Web(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def token(self):
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return c[COOKIE].value if COOKIE in c else None

    def signed_in(self):
        return load_auth() is None or session_ok(self.token())

    def send_body(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)

    def redirect(self, where, headers=()):
        self.send_response(303); self.send_header("Location", where); self.send_header("Content-Length", "0")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()

    def login_page(self, user="", error=""):
        page = (LOGIN_PAGE.replace("__USER__", html.escape(user, quote=True))
                .replace("__FOCUS__", "autofocus" if user else "")
                .replace("__ERROR__", f'<div class="err" role="alert">{html.escape(error)}</div>' if error else ""))
        self.send_body(200, page.encode())

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/login":
            return self.redirect("/") if self.signed_in() else self.login_page()
        if path == "/logout":
            end_session(self.token())
            return self.redirect("/login", [("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")])
        if not self.signed_in():
            if path in ("/", "/index.html"):
                return self.redirect("/login")
            return self.send_body(401, b"sign in required", "text/plain")
        if path in ("/", "/index.html"):
            body = open(os.path.join(HERE, "index.html"), "rb").read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = -1
            try:
                while True:
                    with lock:
                        lock.wait_for(lambda: state["seq"] != last, timeout=1.0)
                        snap = dict(state)
                    last = snap["seq"]
                    snap["age_ms"] = round((time.time() - snap["t"]) * 1000) if snap["t"] else None
                    self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/login":
            n = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
            form = parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            user, pw = form.get("user", [""])[0], form.get("password", [""])[0]
            if password_ok(user, pw):
                tok = new_session()
                return self.redirect("/", [("Set-Cookie", f"{COOKIE}={tok}; Path=/; Max-Age={SESSION_DAYS * 86400}; "
                                                          "HttpOnly; SameSite=Lax")])
            time.sleep(1)   # slow down guessing
            return self.login_page(user, "Wrong email or password.")
        if not self.signed_in():
            return self.send_body(401, b"sign in required", "text/plain")
        if self.path != "/params":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.dumps(set_params(json.loads(self.rfile.read(n) or b"{}"))).encode()
        except (ValueError, TypeError):
            return self.send_error(400)
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


# ------------------------------------------------------------------ watchdog: keep the TV grabber running

import re, subprocess

TV = os.environ.get("LEDLIVE_TV", "")
GRABBER = "com.abrenoch.hyperiongrabber"
TOGGLE = GRABBER + "/com.abrenoch.hyperiongrabber.common.ToggleActivity"
WD_EVERY = 10          # seconds between checks


def adb(*args, timeout=15):
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout + r.stderr
    except (subprocess.TimeoutExpired, OSError) as e:
        return f"ERR {e}"


def tv(*args, timeout=15):
    return adb("-s", TV, *args, timeout=timeout)


TV_MODEL_HINT = os.environ.get("LEDLIVE_TV_MODEL", "")   # empty = accept the first Android device found


def find_tv():
    """The TV's address changed (new DHCP lease): scan the home subnet for ADB on 5555 and pick the TV."""
    global TV
    host, _, port = TV.partition(":")
    prefix = host.rsplit(".", 1)[0]
    found = []
    lock_found = threading.Lock()

    def probe(ip):
        try:
            with socket.create_connection((ip, int(port or 5555)), timeout=0.4):
                with lock_found:
                    found.append(ip)
        except OSError:
            pass
    threads = [threading.Thread(target=probe, args=(f"{prefix}.{n}",)) for n in range(1, 255)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for ip in found:
        cand = f"{ip}:{port or 5555}"
        adb("connect", cand, timeout=10)
        model = adb("-s", cand, "shell", "getprop ro.product.model", timeout=10)
        if TV_MODEL_HINT.lower() in model.lower():
            if cand != TV:
                print(f"[{time.strftime('%H:%M:%S')}] watchdog: TV found at new address {cand} (was {TV})", flush=True)
            TV = cand
            return True
    return False


def wd_note(msg, restart=False):
    with lock:
        state["wd"] = dict(state["wd"], status=msg, checked=time.time(),
                           restarts=state["wd"]["restarts"] + (1 if restart else 0),
                           last_restart=time.time() if restart else state["wd"]["last_restart"])
        state["seq"] += 1
        lock.notify_all()
    if restart or not msg.startswith("ok"):
        print(f"[{time.strftime('%H:%M:%S')}] watchdog: {msg}", flush=True)


def accept_capture_prompt():
    """If Android asks for screen-capture permission, press START NOW."""
    tv("shell", "uiautomator dump /sdcard/ledlive_ui.xml >/dev/null 2>&1", timeout=20)
    xml = tv("shell", "cat /sdcard/ledlive_ui.xml", timeout=10)
    m = re.search(r'text="START NOW"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if m:
        x1, y1, x2, y2 = map(int, m.groups())
        tv("shell", f"input tap {(x1 + x2) // 2} {(y1 + y2) // 2}")
        return True
    return False


def grabber_running():
    """True / False, or None when the TV didn't answer properly (then the watchdog does nothing)."""
    out = tv("shell", f"dumpsys activity services {GRABBER}", timeout=25)
    if out.startswith("ERR") or "error:" in out or "ACTIVITY MANAGER SERVICES" not in out:
        return None
    # a live service is listed as "* ServiceRecord{...}"; "* Restarting ServiceRecord" means it crashed
    return re.search(r"^\s*\* ServiceRecord\{[^}]*HyperionScreenService", out, re.M) is not None


def start_grabber(reason):
    tv("shell", f"am start -n {TOGGLE}")
    time.sleep(3)
    accepted = accept_capture_prompt()
    time.sleep(2)
    ok = grabber_running()
    wd_note(f"restarted grabber ({reason})" + (", accepted capture prompt" if accepted else "") +
            ("" if ok else " - still not running"), restart=True)


def watchdog():
    if not TV:
        wd_note("off - set LEDLIVE_TV to the TV's address to enable auto-restart")
        return
    time.sleep(15)          # let a running grabber reconnect first (it retries every 5 s)
    misses = 0
    unreachable = 0
    while True:
        try:
            if f"{TV}\tdevice" not in adb("devices"):
                adb("connect", TV, timeout=10)
                if f"{TV}\tdevice" not in adb("devices"):
                    unreachable += 1
                    # ~30 s unreachable: the TV may be off, or its address changed - look for it once a minute
                    if unreachable >= 3 and unreachable % 6 == 3 and find_tv():
                        unreachable = 0
                        continue
                    wd_note("TV not reachable over ADB (off, rebooting or network debugging off)")
                    time.sleep(WD_EVERY); continue
            unreachable = 0
            if "tcp:19445" not in tv("reverse", "--list"):
                tv("reverse", f"tcp:{HP_PORT}", f"tcp:{HP_PORT}")
                wd_note("re-created port tunnel")
            power = tv("shell", "dumpsys power | grep 'Display Power'")
            if "state=ON" not in power:
                wd_note("ok - TV screen off, waiting"); time.sleep(WD_EVERY); continue
            with lock:
                connected = state["connected"]
            if connected:
                # frames are flowing (or the link is open on a still screen): nothing to fix,
                # and no need to poke the slow TV with dumpsys
                misses = 0
                wd_note("ok - grabber connected")
                time.sleep(WD_EVERY); continue
            misses += 1
            running = grabber_running()
            if running is None:
                wd_note("TV did not answer, will retry")
            elif not running:
                # killed by the TV (low memory) or crashed: start it right away
                start_grabber("it had stopped"); misses = 0
            elif misses >= 3:
                # alive but not connected for ~30 s (e.g. Android revived it without capture permission)
                tv("shell", f"am start -n {TOGGLE}"); time.sleep(3)    # stop ...
                start_grabber("running but not connected"); misses = 0   # ... and start it again
            else:
                wd_note("grabber running, waiting for it to connect")
        except Exception as e:  # never let the watchdog die
            wd_note(f"error: {e}")
        time.sleep(WD_EVERY)


if __name__ == "__main__":
    state["wd"] = dict(status="starting", checked=0, restarts=0, last_restart=0)
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=keepalive, daemon=True).start()
    print(f"Layout: {NUM_LEDS} LEDs (top {TOP}, right {RIGHT}, bottom {BOTTOM}, left {LEFT})", flush=True)
    print(f"Sending LED colours to WLED at {WLED_HOST}:{DDP_PORT} (DDP)" if WLED_HOST
          else "LEDLIVE_WLED not set: preview only, nothing is sent to LEDs", flush=True)
    threading.Thread(target=hyperion_server, daemon=True).start()
    print(f"Live page on http://{WEB_BIND}:{WEB_PORT}", flush=True)
    ThreadingHTTPServer((WEB_BIND, WEB_PORT), Web).serve_forever()
