"""Shazam Digger: turn your Shazams into a picks page you can listen through and sort into Keep / Drop.

    python digger.py list my-shazams.txt     # any phone: a text/CSV list of "Artist - Title" lines
    python digger.py android                 # Android: read new Shazams off the phone over adb
    python digger.py android --screenshots   # also identify songs from screenshots of YouTube show players (macOS)
    python digger.py page                    # rebuild the picks page from data/tracks.json
    python digger.py clear-phone             # Android: show (or with --yes, delete) the pulled Shazams in the Shazam app
    python digger.py find-tracks keeps.txt   # buy list of your keeps with your store/pool search links: out/buy-list.html, .csv, .txt
    python digger.py learn picks.csv         # learn your taste from your Keeps and Drops; liked tracks rise on the page
    python digger.py rekordbox-xml FOLDER keeps.txt   # bought files -> out/rekordbox.xml playlist "Shazam Keeps"

Add --no-network to skip YouTube and preview lookups (offline / testing).
Settings live in config.toml (copy config.example.toml); every setting has a default.
"""
import argparse, asyncio, collections, csv, datetime as dt, html, io, json, os, re, shutil, subprocess, sys, time, unicodedata, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taste as learned  # learns your taste from your Keeps and Drops (taste.py)

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None

HERE = os.path.dirname(os.path.abspath(__file__))
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sept Oct Nov Dec".split(), 1)}
MONTHS["Sep"] = 9
SHOW_SECS = 90  # length of the listening clip cut from a show for an unidentified moment

DEFAULTS = {
    "data_dir": "data",            # tracks.json, shazams.json, state.json, screenshots, clips
    "out_dir": "out",              # the picks page
    "adb": "",                     # path to adb; empty = find it on PATH or in the Android SDK
    "adb_serial": "",              # empty = first connected device
    "ffmpeg": "",                  # path to ffmpeg; empty = find it on PATH (only for the screenshots feature)
    "network": True,               # look up YouTube links and 30 s previews
    "youtube_check": True,         # confirm each YouTube hit with yt-dlp (slower, fewer wrong links)
    "screenshot_folders": ["/sdcard/Pictures/Screenshots", "/sdcard/DCIM/Screenshots"],
    "min_tags": 2,                 # only songs you Shazamed at least this many times reach the page (1 = all of them)
    "store_search": "",            # your music store's search address with {q} for the song, e.g. "https://store.example/search?q={q}"
    "pool_search": "",             # your record pool's search address with {q} for the song, e.g. "https://pool.example/search?q={q}"
    "soulseek_bin": "",            # path to the Sockseek program (github.com/fiso64/sockseek) for the soulseek command
    "soulseek_conf": "",           # path to your sockseek.conf (your Soulseek username and password); empty = Sockseek's own default
    "soulseek_dir": "soulseek",    # where soulseek downloads go
}

# Honest about who is asking. The tool never pretends to be a browser and never tries to get past a robot check.
UA = "shazam-digger/1.0 (+https://github.com/scrunch-intelligence/shazam-digger)"


def load_config(path=None):
    cfg = dict(DEFAULTS)
    path = path or os.path.join(HERE, "config.toml")
    if os.path.exists(path):
        if tomllib is None:
            sys.exit("config.toml needs Python 3.11 or newer")
        with open(path, "rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


CFG = load_config()
P = lambda *a: os.path.join(HERE, CFG["data_dir"], *a)
STATE, RAW, TRACKS, TASTE = P("state.json"), P("shazams.json"), P("tracks.json"), P("taste.json")
DECISIONS = P("decisions.json")  # every Keep/Drop you have given, so learning never forgets older picks


def load(path, default):
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(data, open(path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


def slug(t):
    return re.sub(r"[^a-z0-9]+", "-", f'{t["artist"]}-{t["title"]}'.lower()).strip("-")[:80]


def today_str():
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- input 1: any phone, a list of "Artist - Title"

def parse_list(text):
    """Rows of {artist, title} from a text list ("Artist - Title" per line) or a CSV with Artist and Title columns
    (Shazam's own CSV export works). Blank lines and lines starting with # are skipped."""
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        return []
    head = [h.strip().lower() for h in next(csv.reader([lines[0]]))]
    if "artist" in head and "title" in head:
        rows = []
        for r in csv.DictReader(io.StringIO("\n".join(lines))):
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            if r.get("artist") and r.get("title"):
                row = {"artist": r["artist"], "title": r["title"]}
                for k in ("pick", "genre", "label"):  # picks.csv has a Pick column; genre/label are used if present
                    if r.get(k):
                        row[k] = r[k].lower() if k == "pick" else r[k]
                rows.append(row)
        return rows
    rows = []
    for l in lines:
        l = re.sub(r"^\s*\d+[.)]\s+", "", l.strip())  # "12. Artist - Title"
        m = re.split(r"\s+[-–—]\s+", l, maxsplit=1)
        if len(m) == 2:
            rows.append({"artist": m[0].strip(), "title": m[1].strip()})
        else:
            print("skipped (no ' - ' between artist and title):", l)
    return rows


def count_list(rows):
    """Same track listed more than once = it scores higher (like tagging it twice). First-seen order is kept."""
    order, seen = [], collections.Counter()
    for r in rows:
        k = (r["artist"].lower(), base(r["title"]))
        if k not in seen:
            order.append(r)
        seen[k] += 1
    return [{"artist": r["artist"], "title": r["title"], "versions": [r["title"]], "score": seen[(r["artist"].lower(), base(r["title"]))],
             "first": "", "last": "", "alternates": [], "source": "list"} for r in order]


# ---------------------------------------------------------------- input 2: Android over adb (Shazam's own Library list)

def adb_path():
    if CFG["adb"]:
        return os.path.expanduser(CFG["adb"])
    found = shutil.which("adb")
    if found:
        return found
    for sdk in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"), "~/Library/Android/sdk", "~/Android/Sdk",
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")):
        if sdk:
            p = os.path.join(os.path.expanduser(sdk), "platform-tools", "adb.exe" if os.name == "nt" else "adb")
            if os.path.exists(p):
                return p
    return "adb"


def serial():
    """Which phone: adb_serial in config.toml, else the ANDROID_SERIAL environment variable, else the first one."""
    return CFG["adb_serial"] or os.environ.get("ANDROID_SERIAL", "")


def adb(*a):
    serial_ = ["-s", serial()] if serial() else []
    try:
        return subprocess.run([adb_path(), *serial_, *a], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return ""


def phone_connected():
    devices = [l.split("\t")[0] for l in adb("devices").splitlines() if l.endswith("\tdevice")]
    if serial():
        CFG["adb_serial"] = serial()
        return serial() in devices
    if devices:
        CFG["adb_serial"] = devices[0]  # first device
    return bool(devices)


LAUNCHERS = ("launcher", "nexuslauncher", "trebuchet", "home")


def phone_in_use():
    """True when someone is using the phone: screen on and an app other than the home screen or Shazam in front.
    Checked before every run that taps the phone, so the tool never takes over the phone under you."""
    act = (re.search(r"mResumedActivity.*?\s(\S+/\S+)", adb("shell", "dumpsys activity activities | grep -m1 mResumedActivity")) or [None, ""])[1]
    awake = "mWakefulness=Awake" in adb("shell", "dumpsys power | grep -m1 mWakefulness=")
    pkg = act.split("/")[0].lower()
    return awake and bool(pkg) and pkg != "com.shazam.android" and not any(w in pkg for w in LAUNCHERS)


def check_phone():
    if not phone_connected():
        sys.exit("No Android phone found. Turn on USB debugging, plug in the cable, tap Allow on the phone, then run again.")
    print("Phone:", CFG["adb_serial"])
    if phone_in_use():
        sys.exit("The phone is in use (another app is open). Go to the home screen, then run again.")


def screen_size():
    m = re.search(r"(\d+)x(\d+)", adb("shell", "wm", "size"))
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)


def ui():
    adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
    nodes = []
    for n in re.findall(r"<node [^>]*>", adb("exec-out", "cat", "/sdcard/ui.xml")):
        g = lambda k: (re.search(rf' {k}="([^"]*)"', n) or [None, ""])[1]
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", g("bounds")))
        txt = html.unescape(g("text"))
        nodes.append({"text": txt, "desc": g("content-desc"), "id": g("resource-id").split("/")[-1],
                      "x": (x1 + x2) // 2, "y": (y1 + y2) // 2, "top": y1})
    return nodes


def long_press(n):
    adb("shell", "input", "swipe", str(n["x"]), str(n["y"]), str(n["x"]), str(n["y"]), "900")
    time.sleep(1.5)


def tap(n):
    adb("shell", "input", "tap", str(n["x"]), str(n["y"]))
    time.sleep(1.5)


def open_song_list():
    """Shazam opens straight into listening; back out of that, then Library > Songs. Never starts a Shazam."""
    adb("shell", "am", "start", "-n", "com.shazam.android/.activities.SplashActivity")
    time.sleep(3)
    for _ in range(8):
        nodes = ui()
        texts = {n["text"] for n in nodes}
        if any(n["id"] == "list_title" and n["text"] == "Songs" for n in nodes):
            return True
        if texts & {"Listening for music", "Tap to stop", "Try again", "No result"}:
            adb("shell", "input", "keyevent", "BACK"); time.sleep(1.5); continue
        songs = [n for n in nodes if n["text"] == "Songs"]
        lib = [n for n in nodes if n["text"] == "Library"]
        tap(songs[0] if songs else lib[0]) if (songs or lib) else adb("shell", "input", "keyevent", "BACK")
    return False


def screen_rows():
    rows, cur = [], None
    for n in ui():
        if n["id"] == "title":
            cur = {"title": n["text"], "y": n["top"], "x": n["x"], "cy": n["y"]}; rows.append(cur)
        elif n["id"] == "subtitle" and cur:
            cur["artist"] = n["text"]
        elif n["id"] == "datetime" and cur:
            cur["when"] = n["text"]
    return [r for r in rows if "when" in r]


key = lambda r: (r["title"], r.get("artist", ""), r["when"])


def find_run(seq, sig):
    """Index where the remembered newest Shazams (sig) start inside seq, or -1."""
    sig = [tuple(s) for s in sig]
    for i in range(len(seq) - len(sig) + 1):
        if [key(r) for r in seq[i:i + len(sig)]] == sig:
            return i
    return -1


def read_new_shazams(sig):
    """Scroll from the top until the Shazams we already have appear (or the list ends)."""
    w, h = screen_size()
    got = screen_rows()
    for r in got:
        r["sy"] = r["y"]
    stall = 0
    prev_keys = [key(r) for r in got]
    while stall < 3 and (not sig or find_run(got, sig) < 0):
        adb("shell", "input", "swipe", str(w // 2), str(int(h * .79)), str(w // 2), str(int(h * .375)), "1500")  # slow, so the list doesn't fling
        time.sleep(0.8)
        new = screen_rows()
        if not new:
            stall += 1; continue
        last, lk, nk = got[-len(new):], [key(r) for r in got[-len(new):]], [key(r) for r in new]
        shift = next((last[lk.index(key(r))]["sy"] - r["y"] for r in new if lk.count(key(r)) == 1 and nk.count(key(r)) == 1), int(h * .42))
        lasty = got[-1]["sy"] - shift if got else -1
        added = [r for r in new if r["y"] > lasty + 20]
        if nk == prev_keys:  # the screen did not move: that is the end of the list (repeated rows can fool the shift guess)
            added = []
        prev_keys = nk
        for r in got:
            r["sy"] -= shift
        for r in new:
            r["sy"] = r["y"]
        got += added
        stall = 0 if added else stall + 1
    cut = find_run(got, sig) if sig else -1
    fresh = got if cut < 0 else got[:cut]
    return [{"title": r["title"], "artist": r.get("artist", ""), "when": r["when"]} for r in fresh]


def pull_new_screenshots(since):
    names = []
    for folder in CFG["screenshot_folders"]:
        for f in adb("shell", f"ls {folder} 2>/dev/null").split():
            m = re.match(r"Screenshot_(\d{8}-\d{6})", f)
            if m and m.group(1) > since:
                os.makedirs(P("screenshots"), exist_ok=True)
                adb("pull", f"{folder}/{f}", P("screenshots", f))
                names.append(f)
    return sorted(names)


# ---------------------------------------------------------------- grouping Shazams into tracks

def shazam_time(s, today=None):
    today = today or dt.date.today()
    day, mon, hm = re.match(r"(\d+) (\w+), (\d+:\d+)", s).groups()
    year = today.year - (1 if MONTHS[mon] > today.month else 0)
    h, m = map(int, hm.split(":"))
    return dt.datetime(year, MONTHS[mon], int(day), h, m)


def base(t):
    return re.sub(r"\s*[\(\[][^)\]]*(mix|edit|extended|version|remaster|dub|radio)[^)\]]*[\)\]]", "", t, flags=re.I).strip().lower()


def group(raw, today=None):
    """Repeat Shazams of a track = its love score; a one-off within 3 min of a repeated track is an alternate reading."""
    groups = collections.OrderedDict()
    for r in raw:
        groups.setdefault((base(r["title"]), r["artist"].lower()), []).append(dict(r, t=shazam_time(r["when"], today)))
    tracks = [{"artist": v[0]["artist"], "titles": sorted({x["title"] for x in v}), "shazams": v} for v in groups.values()]
    near = lambda a, b: any(abs((x["t"] - y["t"]).total_seconds()) <= 180 for x in a["shazams"] for y in b["shazams"])
    for a in tracks:
        for b in tracks:
            if a is not b and not a.get("folded") and not b.get("folded") and base(a["titles"][0]) == base(b["titles"][0]) and near(a, b):
                a["shazams"] += b["shazams"]; a["titles"] = sorted(set(a["titles"] + b["titles"])); b["folded"] = True
    tracks = [t for t in tracks if not t.get("folded")]
    for t in [t for t in tracks if len(t["shazams"]) == 1]:
        hosts = [o for o in tracks if o is not t and not o.get("folded") and len(o["shazams"]) > 1 and near(o, t)]
        if hosts:
            host = max(hosts, key=lambda o: len(o["shazams"]))
            host.setdefault("alternates", []).append(f'{t["artist"]} - {t["titles"][0]}')
            host["shazams"] += t["shazams"]; t["folded"] = True
    out = []
    for t in tracks:
        if not t.get("folded"):
            ts = sorted(x["t"] for x in t["shazams"])
            out.append({"artist": t["artist"], "title": t["titles"][0], "versions": t["titles"], "score": len(t["shazams"]),
                        "first": ts[0].strftime("%d %b %H:%M"), "last": ts[-1].strftime("%d %b %H:%M"),
                        "alternates": t.get("alternates", [])})
    return sorted(out, key=lambda t: -t["score"])


KEEP_FIELDS = ("preview", "file", "youtube", "youtube_title", "yt_year", "yt_released", "yt_verified", "yt_none", "released", "released_checked",
               "preview_tried", "checked", "link", "added", "note")


def merge(old_tracks, new_tracks):
    """New grouping, but everything already learned about a track (links, previews, notes) carries over."""
    old = {slug(t): t for t in old_tracks}
    out, have = [], set()
    for t in new_tracks:
        prev = old.get(slug(t), {})
        t.update({k: prev[k] for k in KEEP_FIELDS if k in prev})
        out.append(t); have.add(slug(t))
    out += [t for t in old_tracks if t.get("source") in ("screenshot", "list") and slug(t) not in have]  # never lose these
    return out


# ---------------------------------------------------------------- screenshots of show players (YouTube), macOS only

def ocr_lines(path):
    import Vision, Quartz
    from Foundation import NSURL
    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)
    Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None).performRequests_error_([req], None)
    obs = sorted(req.results() or [], key=lambda o: -o.boundingBox().origin.y)  # top to bottom
    return [o.topCandidates_(1)[0].string() for o in obs]


def read_player(lines):
    """Show title, position in seconds and partial video id from a phone screenshot of a YouTube player."""
    pos = vid = title = None
    for i, l in enumerate(lines):
        m = re.search(r"(\d+(?::\d\d){1,2})\s*/\s*\d+(?::\d\d){1,2}", l)
        if m and pos is None:
            pos = sum(int(x) * 60 ** k for k, x in enumerate(reversed(m.group(1).split(":"))))
            after = [x for x in lines[i + 1:i + 6] if len(x) > 10 and not x.startswith("@") and "In this video" not in x
                     and not re.search(r"\d+(:\d\d)+\s*/|^(comments|subscribe|share|more)\b|likes|views", x, re.I)]
            title = title or (after[0] if after else None)
        v = re.search(r"watch\?v=([\w-]{3,11})", l)
        vid = vid or (v.group(1) if v else None)
        if re.search(r"(sessions|episode|radio|show|live|mix|state of trance|group therapy)\s*#?\d*", l, re.I) and len(l) > 12 and not title:
            title = l
    return {"pos": pos, "title": title, "vid": vid} if pos is not None else None


def shot_time(name):
    return dt.datetime.strptime(re.search(r"(\d{8}-\d{6})", name).group(1), "%Y%m%d-%H%M%S")


def moments(shots):
    """Screenshots taken within 30 s of each other are one moment (more screenshots = you love it more),
    even when some of them don't show the player time. Each shot: {"name", "info" or None}."""
    shots = sorted(shots, key=lambda s: s["name"])
    groups = []
    for s in shots:
        if groups and (shot_time(s["name"]) - shot_time(groups[-1][-1]["name"])).total_seconds() <= 30:
            groups[-1].append(s)
        else:
            groups.append([s])
    out = []
    for g in groups:
        read = [s["info"] for s in g if s["info"]]
        if not read:
            continue
        # the show title is whatever title most screenshots within 2 hours agree on (OCR sometimes grabs a comment)
        near = collections.Counter(s["info"]["title"] for s in shots if s["info"] and s["info"]["title"]
                                   and abs((shot_time(s["name"]) - shot_time(g[0]["name"])).total_seconds()) <= 7200)
        title = near.most_common(1)[0][0] if near else None
        if not title:
            continue
        pos = sorted(i["pos"] for i in read)
        out.append({"title": title, "vid": next((i["vid"] for i in read if i["vid"]), None), "positions": pos, "shots": len(g)})
    return out


def ffmpeg():
    return os.path.expanduser(CFG["ffmpeg"]) if CFG["ffmpeg"] else (shutil.which("ffmpeg") or "ffmpeg")


def identify_moments(found):
    """Download each show's audio once, Shazam a few 12 s slices around each moment, keep the answer seen 2+ times."""
    from shazamio import Shazam
    sh, tracks, FF = Shazam(), [], ffmpeg()
    by_show = collections.defaultdict(list)
    for m in found:
        by_show[m["title"]].append(m)
    fold = lambda s: s.upper().translate(str.maketrans("5S0OIL1", "SSOOIII"))
    os.makedirs(P("show-cache"), exist_ok=True); os.makedirs(P("audio"), exist_ok=True)
    for title, ms in by_show.items():
        part = next((m["vid"] for m in ms if m["vid"]), None)
        hits = youtube_search(title)
        vid = next((v for v, _ in hits if part and fold(v).startswith(fold(part)[:4])), hits[0][0] if hits else None)
        if not vid:
            continue
        audio = P("show-cache", vid + ".m4a")
        if not os.path.exists(audio):
            subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "-f", "bestaudio[ext=m4a]/bestaudio", "-o", audio,
                            f"https://www.youtube.com/watch?v={vid}"], check=True)
        for m in ms:
            start = sorted(m["positions"])[len(m["positions"]) // 2]
            hits, counted = [], collections.Counter()
            for off in (0, 20, 40, -20, 60, 80):
                clip = P("show-cache", "clip.wav")
                subprocess.run([FF, "-v", "error", "-y", "-ss", str(max(0, start + off)), "-t", "12", "-i", audio, "-ac", "1", "-ar", "16000", clip], check=True)
                tr = asyncio.run(sh.recognize(clip)).get("track")
                hits.append(f'{tr["subtitle"]} - {tr["title"]}' if tr else None)
                counted = collections.Counter(h for h in hits if h)
                if counted and counted.most_common(1)[0][1] >= 3 or (len(hits) >= 3 and not counted):
                    break
            at = f"{start // 3600}:{start % 3600 // 60:02d}:{start % 60:02d}".lstrip("0:") or "0"
            best = counted.most_common(1)[0] if counted else None
            t = {"score": m["shots"], "first": f"{title} at {at}", "last": "", "alternates": [], "versions": [],
                 "source": "screenshot", "link": f"https://www.youtube.com/watch?v={vid}&t={start}s", "added": today_str()}
            if best and best[1] >= 2:
                artist, name = best[0].split(" - ", 1)
                t.update(artist=artist, title=name, versions=[name])
            else:
                t.update(artist="Unreleased / ID", title=f"{title} at {at}")
                out = P("audio", slug(t) + ".mp3")
                subprocess.run([FF, "-v", "error", "-y", "-ss", str(start), "-t", str(SHOW_SECS), "-i", audio, "-af",
                                "loudnorm=I=-14:TP=-1:LRA=11", "-ar", "44100", "-b:a", "192k", out], check=True)
                t["file"] = out
            tracks.append(t)
        os.remove(audio)  # show audio is only needed for this run
    return tracks


# ---------------------------------------------------------------- YouTube links and 30 s previews (no API keys)

class BotCheck(Exception):
    """A site asked 'are you a robot?' or told us to slow down. The tool stops and tells you. It never tries to get past it."""


ROBOT = re.compile(r"not a bot|confirm you.re not|captcha|unusual traffic|too many requests|http error 429|rate.?limit", re.I)
LOOKUP_PAUSE = 2  # seconds between lookups: one track at a time, at an easy pace
RECHECK_DAYS = 7  # a track with no match is not searched again for this long


def ytdlp(*args, timeout=90):
    """Run yt-dlp (open source, installed by requirements.txt) and return its output. Raises BotCheck on a robot check."""
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--skip-download", "--socket-timeout", "20", *args],
                           capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if ROBOT.search(r.stderr or ""):
        raise BotCheck("YouTube asked for a robot check or a slow-down. Stopped. Open youtube.com in your own browser, "
                       "clear it yourself, and run again later.")
    return r.stdout


def yt_search(q, n=5):
    """The top n YouTube results for q (id, title, channel, duration), through yt-dlp. Nothing is downloaded."""
    out = []
    for line in ytdlp("--flat-playlist", "--dump-json", f"ytsearch{n}:{q}").splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if isinstance(j, dict) and j.get("id"):
            out.append(j)
    return out


def youtube_search(q, n=5):
    """[(video id, title)] for the screenshots feature (finding a whole show to listen to)."""
    return [(e["id"], e.get("title", "")) for e in yt_search(q, n)]


def yt_info(vid):
    """Everything yt-dlp knows about one video (title, channel, duration, upload date, whether it can play), or None."""
    try:
        j = json.loads(ytdlp("--dump-json", f"https://www.youtube.com/watch?v={vid}"))
    except ValueError:
        return None
    return j if isinstance(j, dict) and j.get("id") else None


def yt_words(s):
    return set(re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str(s or "")).lower()))


def sorted_words(s):
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", str(s or "")).lower())


def yt_norm(s):
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", str(s or "")).lower())


PLAIN_MIX = yt_words("original extended radio club mix edit version remastered remaster full length short single album vocal")
OTHER_VERSION = yt_words("live cover karaoke reaction slowed sped nightcore 8d tutorial instrumental acoustic reverb mashup bootleg podcast")
SET_PHRASES = ("dj set", "live set", "full set", "radio show", "full album")
BLOCKED = ("private", "premium_only", "subscriber_only", "needs_auth")
MIXY = re.compile(r"remix|rework|\bedit\b|\bvip\b|bootleg|mashup|\bflip\b|\bdub\b|\bversion\b|\bmix\b", re.I)


def mix_tags(title):
    """The mix names in a title, such as 'Someone Remix'. Plain 'Original Mix', 'Extended Mix' and 'Radio Edit' are left out."""
    tags = [m for m in re.findall(r"[(\[]([^)\]]*)[)\]]", title or "") if MIXY.search(m)]
    tags += re.findall(r"\s[-–—]\s*([^-–—()\[\]]*(?:remix|rework|bootleg|vip|flip))\s*$", title or "", re.I)  # 'Song - Someone Remix'
    return [m for m in tags if yt_words(m) - PLAIN_MIX]


def yt_judge(t, info):
    """True only when this video is the same release as the track: the artist is named, every title word is there, the mix
    matches, it is not a live/cover/slowed version, and it can play. Anything doubtful is False: a link is never guessed.
    `info` is a yt-dlp record (a search result or the full video info); fields that are missing are not held against it."""
    title = info.get("title") or ""
    credits = [a for a in re.split(r",|&| feat\.? | ft\.? | x | vs\.? | and ", t["artist"], flags=re.I) if a.strip()]
    first = yt_norm(credits[0]) if credits else ""
    owner = yt_norm(" ".join(str(info.get(k) or "") for k in ("channel", "uploader", "artist")))
    if not first or not (first in yt_norm(title) or first in owner):
        return False
    want = yt_words(clean_title(t["title"])) - PLAIN_MIX - yt_words("feat ft the a and of")
    have = yt_words(title) | yt_words(info.get("track"))
    if not want or not want <= have:
        return False
    if (yt_words(title) & OTHER_VERSION) - yt_words(t["title"]):
        return False
    if any(p in " ".join(sorted_words(title)) and p not in " ".join(sorted_words(t["title"])) for p in SET_PHRASES):  # a set or show, not the track
        return False
    ours, theirs = mix_tags(t["title"]), mix_tags(title)
    if any(not (yt_words(m) - PLAIN_MIX) <= yt_words(title) for m in ours) or (theirs and not ours):
        return False
    d = info.get("duration")
    if isinstance(d, (int, float)) and not 90 <= d <= 15 * 60:  # not a snippet, not a full set or album
        return False
    if info.get("availability") in BLOCKED or info.get("live_status") not in (None, "not_live") or info.get("playable_in_embed") is False \
            or (info.get("age_limit") or 0) >= 18:
        return False
    return True


def youtube_lookup(t):
    """Search YouTube (yt-dlp) for the exact release. Returns its checked video info, or None. Never a guess."""
    for e in yt_search(f'{t["artist"]} {t["title"]}', 5):
        if not yt_judge(t, e):
            continue
        if not CFG["youtube_check"]:
            return e
        time.sleep(LOOKUP_PAUSE)
        info = yt_info(e["id"])
        if info and yt_judge(t, info):
            return info
    return None


def set_youtube(t, info):
    t["youtube"] = f'https://www.youtube.com/watch?v={info["id"]}'
    t["youtube_title"] = info.get("title", "")
    t["yt_year"] = (info.get("upload_date") or "")[:4]
    rd = info.get("release_date") or ""
    if len(rd) == 8:
        t["yt_released"] = f"{rd[:4]}-{rd[4:6]}-{rd[6:]}"
    t["yt_verified"] = "full" if CFG["youtube_check"] else "title"
    t.pop("yt_none", None)


def add_youtube(t):
    """A checked YouTube link for the track. A link from an older run is checked once and dropped if it is not the same release."""
    if t.get("youtube") and (t.get("yt_verified") == "full" or (t.get("yt_verified") and not CFG["youtube_check"])):
        return
    if t.get("youtube"):
        m = re.search(r"[?&]v=([\w-]{11})", t["youtube"])
        info = yt_info(m.group(1)) if m else None
        if info and yt_judge(t, info):
            return set_youtube(t, info)
        for k in ("youtube", "youtube_title", "yt_year", "yt_released", "yt_verified"):
            t.pop(k, None)
    if t.get("yt_none") and (dt.date.today() - dt.date.fromisoformat(t["yt_none"])).days < RECHECK_DAYS:
        return
    info = youtube_lookup(t)
    if info:
        set_youtube(t, info)
    else:
        t["yt_none"] = today_str()


def words(s):
    return set(re.findall(r"[a-z0-9]+", s.lower()))


GENERIC = words("mix mixed original extended edit radio club version remix feat ft the a and of")
norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
clean_title = lambda s: re.sub(r"\(.*?\)|\[.*?\]", "", s).strip()


def fits(t, artist, title, credits):
    want = words(t["title"]) - GENERIC
    have = words(title) - GENERIC
    extra = have - want - words(t["artist"]) - words(artist)
    every = all(norm(a)[:6] in norm(" ".join(credits)) for a in re.split(r",|&| feat\.? | x ", t["artist"]) if a.strip())
    return norm(t["artist"].split(",")[0].split("&")[0])[:6] in norm(artist) and want <= have | words(artist) and not extra and every


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def find_preview_info(t):
    """(a 30 s preview address, the release date as YYYY-MM-DD) of the exact version, from iTunes then Deezer. "" where not found."""
    q = urllib.parse.quote(f'{t["artist"].replace(",", " ").replace("&", " ")} {clean_title(t["title"])}')
    for item in get("https://itunes.apple.com/search?entity=song&limit=10&term=" + q).get("results", []):
        if item.get("previewUrl") and fits(t, item["artistName"], item.get("trackName", ""), [item["artistName"], item.get("trackName", "")]):
            return item["previewUrl"], (item.get("releaseDate") or "")[:10]
    for item in get("https://api.deezer.com/search?q=" + q).get("data", [])[:10]:
        if item.get("preview"):
            detail = get(f'https://api.deezer.com/track/{item["id"]}')
            credits = [c["name"] for c in detail.get("contributors", [])] + [item["title"]]
            if fits(t, item["artist"]["name"], item["title"], credits):
                return item["preview"], (detail.get("release_date") or "")[:10]
    return "", ""


def find_preview(t):
    return find_preview_info(t)[0] or None


def wanted(t):
    """Songs you Shazamed min_tags times or more (screenshot finds always count)."""
    return t.get("source") == "screenshot" or t.get("score", 1) >= CFG["min_tags"]


def dkey(t):
    return f'{norm(t["artist"])}|{norm(t["title"])}'


def owned_keys(path=None):
    """Tracks you marked 'I have it'. They are never looked up, never listed again and never reach a buy list."""
    return {k for k, r in ((dkey(r), r) for r in load(path or DECISIONS, [])) if r.get("pick") == "have"}


def fill_in(tracks):
    """A checked YouTube link, a 30 s preview and the release date for every wanted track that does not have them yet.
    One track at a time, a couple of seconds apart. A failure skips that track; a robot check stops everything."""
    owned = owned_keys()
    for i, t in enumerate(tracks, 1):
        if t["artist"].startswith("Unreleased") or t["artist"] == "ID" or not wanted(t) or dkey(t) in owned or t.get("pick") == "have":
            continue
        print(f"  [{i}/{len(tracks)}] {t['artist']} - {t['title']}", flush=True)
        try:
            add_youtube(t)
            tried = t.get("preview_tried")
            if (not t.get("preview") or not t.get("released_checked")) and not (tried and (dt.date.today() - dt.date.fromisoformat(tried)).days < RECHECK_DAYS):
                url, released = find_preview_info(t)
                t["preview"] = url or t.get("preview", "")
                if released:
                    t["released"] = released
                t["released_checked"] = True
                t["preview_tried"] = today_str()
            t["checked"] = True
        except BotCheck as e:
            print(e)
            return
        except Exception as e:
            print("    lookup failed:", e)
        time.sleep(LOOKUP_PAUSE)


# ---------------------------------------------------------------- the picks page

def taste_score(t, taste):
    """How many times you kept this artist before (any of the artists on a collab)."""
    return sum(taste.get(norm(a), 0) for a in re.split(r",|&| feat\.? | ft\.? | x | vs\.? ", t["artist"]) if norm(a))


def build_page(tracks, out=None, taste=None):
    """taste: the saved taste.json ({"artists": counts, "model": learned}) or, for old callers, an artist-count dict."""
    out = out or os.path.join(HERE, CFG["out_dir"], "picks.html")
    taste = load(TASTE, {}) if taste is None else taste
    model = taste.get("model") if isinstance(taste.get("model"), dict) else None
    taste = taste.get("artists", {}) if ("artists" in taste or model) else taste
    owned = owned_keys()  # 'I have it': you own those, so they never come back
    tracks = [t for t in tracks if wanted(t) and dkey(t) not in owned and t.get("pick") != "have"]
    if model:  # learned from your Keeps and Drops: most likely keeps first
        tracks = sorted(tracks, key=lambda t: -learned.score(model, t))
        ss = sound_scores(tracks)
        if ss:  # sound taste too: average the two places in line (tracks without sound keep their place)
            place = {slug(t): i for i, t in enumerate(tracks)}
            by_sound = {k: i for i, k in enumerate(sorted(ss, key=lambda k: -ss[k]))}
            n_s = max(1, len(by_sound) - 1)
            tracks = sorted(tracks, key=lambda t: place[slug(t)] + (by_sound[slug(t)] / n_s * (len(tracks) - 1) if slug(t) in by_sound else place[slug(t)]))
    else:
        tracks = sorted(tracks, key=lambda t: -taste_score(t, taste))  # stable: tag order stays
    def rel(f):  # local clip files are referenced relative to the page
        return os.path.relpath(f, os.path.dirname(out)).replace(os.sep, "/") if f else ""
    search = lambda t: "https://www.youtube.com/results?search_query=" + urllib.parse.quote(f'{t["artist"]} {t["title"]}')
    # Every row must play: a preview, a local clip or a checked video. A track that was looked up and has none of those
    # goes to "Can't play here" at the end of the page. (Tracks never looked up, e.g. --no-network, stay in the list.)
    plays = lambda t: bool(t.get("preview") or t.get("file") or t.get("youtube") or t.get("link"))
    data = [{"id": slug(t), "artist": t["artist"], "title": t["title"], "score": t.get("score", 1), "when": t.get("first", ""),
             "also": t.get("alternates", []), "src": t.get("source", "shazam"), "audio": t.get("preview") or rel(t.get("file")),
             "yt": t.get("youtube") or t.get("link") or "", "yt_year": t.get("yt_year", ""), "rel": release_of(t),
             "checked": bool(t.get("checked")), "liked": taste_score(t, taste), "search": search(t),
             "store": store_link(t), "pool": pool_link(t)} for t in tracks if plays(t) or not t.get("checked")]
    cant = [{"id": slug(t), "artist": t["artist"], "title": t["title"], "search": search(t)} for t in tracks if not plays(t) and t.get("checked")]
    tpl = open(os.path.join(HERE, "phone_template.html"), encoding="utf-8").read()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    blob = lambda x: json.dumps(x, ensure_ascii=False).replace("</", "<\\/")
    with open(out, "w", encoding="utf-8") as f:
        f.write(tpl.replace("__DATA__", blob(data)).replace("__CANT__", blob(cant)))
    return out


def release_of(t):
    """The best known release date as YYYY-MM-DD: from the music service match, else the video's own release date. "" if unknown."""
    for k in ("released", "yt_released"):
        if re.fullmatch(r"\d{4}-\d\d-\d\d", t.get(k) or ""):
            return t[k]
    return ""


# ---------------------------------------------------------------- sound taste (optional: ./setup.sh --sound)

SOUND_DIR = P("sound")


def sound_vectors(rows, fetch=True):
    """{slug: fingerprint} for the rows that have a preview (cached; new ones are made when fetch is True)."""
    import sound
    import numpy as np
    out = {}
    for t in rows:
        k = slug(t)
        f = os.path.join(SOUND_DIR, k + ".npz")
        v = None
        if os.path.exists(f):
            try:
                v = np.load(f)["emb"]
            except Exception:
                v = None
        elif fetch:
            v = sound.fingerprint_track(t, SOUND_DIR, k, UA)
        if v is not None:
            out[k] = v
    return out


def sound_model(fetch=False):
    """(model, every cached fingerprint) from your saved decisions, or (None, {}) when sound is not set up."""
    try:
        import sound
        if not sound.available():
            return None, {}
        known = {slug(t): t for t in load(TRACKS, [])}
        dec = [dict(known.get(slug(r), {}), **r) for r in load(DECISIONS, [])]
        vecs = sound_vectors(dec, fetch)
        keeps = [vecs[slug(r)] for r in dec if r["pick"] in ("keep", "have") and slug(r) in vecs]
        drops = [vecs[slug(r)] for r in dec if r["pick"] == "drop" and slug(r) in vecs]
        return sound.build(keeps, drops, list(vecs.values())), {"keeps": keeps, "drops": drops, "all": vecs}
    except Exception as e:
        print("sound taste skipped:", e)
        return None, {}


def sound_scores(tracks, fetch=False):
    """{slug: sound score} for tracks with a fingerprint, or {} when there is no sound model."""
    m, _ = sound_model()
    if m is None:
        return {}
    return {k: m.score(v) for k, v in sound_vectors(tracks, fetch).items()}


def learn_sound():
    """Called by learn: fingerprint the previews, build the sound model, print its honest check."""
    import sound
    if not sound.available():
        print("Sound taste is off (run ./setup.sh --sound to add it).")
        return
    print("Sound taste: listening to previews (each one once)...")
    sound_vectors([t for t in load(TRACKS, []) if wanted(t)], fetch=True)
    m, d = sound_model(fetch=True)
    if m is None:
        print(f"Sound taste needs {sound.MIN_EACH} Keeps and {sound.MIN_EACH} Drops with a preview; you have "
              f"{len(d.get('keeps', []))} and {len(d.get('drops', []))}.")
        return
    c = sound.check(d["keeps"], d["drops"], list(d["all"].values()))
    print(f"Sound taste learned from {len(d['keeps'])} Keeps and {len(d['drops'])} Drops with sound. Honest check (each hidden, "
          f"then guessed): {c} where 0.5 is a coin toss and 1 is perfect.")


# ---------------------------------------------------------------- commands

def finish(tracks, network):
    for t in tracks:
        t.setdefault("added", today_str())
    if network:
        print("Looking up YouTube links and previews (Ctrl-C is safe; found links are kept next run)...")
        try:
            fill_in(tracks)
        except KeyboardInterrupt:
            print("stopped early")
    save(TRACKS, tracks)
    out = build_page(tracks)
    print(f"{len(tracks)} tracks -> {out}")


def cmd_list(path, network):
    rows = parse_list(open(path, encoding="utf-8-sig").read())
    if not rows:
        sys.exit("No tracks found. Use one 'Artist - Title' per line, or a CSV with Artist and Title columns.")
    new = count_list(rows)
    print(f"{len(rows)} lines -> {len(new)} tracks")
    finish(merge(load(TRACKS, []), new), network)


def cmd_android(network, screenshots, full=False):
    state, tracks = load(STATE, {}), load(TRACKS, [])
    new_shazams, shots = [], []
    check_phone()
    if open_song_list():
        sig = [] if full else (state.get("newest") or [list(key(r)) for r in load(RAW, [])[:5]])
        new_shazams = read_new_shazams(sig)
        adb("shell", "input", "keyevent", "HOME")
        state["last_pull"] = {"at": dt.datetime.now().isoformat(timespec="seconds"), "count": len(new_shazams)}
    else:
        print("Could not open Shazam's Library > Songs list on the phone.")
    if screenshots:
        names = pull_new_screenshots(state.get("last_screenshot", "00000000-000000"))
        for f in names:
            shots.append({"name": f, "info": read_player(ocr_lines(P("screenshots", f)))})
        if names:
            state["last_screenshot"] = re.search(r"(\d{8}-\d{6})", names[-1]).group(1)
        print(f"{len(names)} new screenshots")
    old = load(RAW, [])
    if new_shazams:  # rows read again (full read, or an unfinished clear) replace their saved copies; nothing saved is lost
        have = collections.Counter(key(r) for r in new_shazams)
        old = [r for r in old if not have[key(r)] or have.subtract([key(r)])]
    raw = new_shazams + old
    if raw:
        state["newest"] = [list(key(r)) for r in raw[:5]]
    save(RAW, raw)
    tracks = merge(tracks, group(raw))
    if shots and network:
        try:
            tracks += identify_moments(moments(shots))
        except BotCheck as e:
            print(e)
    save(STATE, state)
    print(f"{len(new_shazams)} new Shazams")
    finish(tracks, network)


# ---------------------------------------------------------------- clean up: delete pulled Shazams from the phone

DELETE_WORDS = re.compile(r"^(delete|remove)\b", re.I)


def on_screen_matches(saved):
    """Rows on the phone's Songs screen that are in the saved pull (title, artist and time all equal)."""
    want = {tuple(key(r)) for r in saved}
    return [r for r in screen_rows() if key(r) in want]


def scan_phone(saved):
    """Scroll the whole Songs list once; return the phone rows that are saved on disk and the count of all rows seen."""
    w, h = screen_size()
    seen, hits, stall = set(), [], 0
    while stall < 3:
        rows = screen_rows()
        before = len(seen)
        for r in rows:
            k = key(r)
            if k not in {key(x) for x in hits} and k in {tuple(key(x)) for x in saved}:
                hits.append(r)
            seen.add(k)
        stall = 0 if len(seen) > before else stall + 1
        adb("shell", "input", "swipe", str(w // 2), str(int(h * .79)), str(w // 2), str(int(h * .375)), "1500")
        time.sleep(0.8)
    return hits, len(seen)


def delete_one(row, really):
    """Long-press the row and find Delete/Remove. really=False (rehearsal) stops there and backs out without tapping it;
    really=True taps it and confirms."""
    long_press({"x": row["x"], "y": row["cy"]})
    nodes = ui()
    btn = next((n for n in nodes if DELETE_WORDS.match(n["text"] or n["desc"])), None)
    if not btn:  # some versions show a menu (three dots) first
        more = next((n for n in nodes if re.search(r"more options|more", n["desc"], re.I)), None)
        if more:
            tap(more); nodes = ui()
            btn = next((n for n in nodes if DELETE_WORDS.match(n["text"] or n["desc"])), None)
    if not btn or not really:
        adb("shell", "input", "keyevent", "BACK"); time.sleep(1)
        adb("shell", "input", "keyevent", "BACK")
        return "no Delete button found" if not btn else f'found the "{btn["text"] or btn["desc"]}" button, stopped (rehearsal)'
    tap(btn)
    nodes = ui()
    yes = next((n for n in nodes if DELETE_WORDS.match(n["text"]) or n["text"].upper() in ("OK", "YES")), None)
    if yes:
        tap(yes)
    return "deleted"


def restart_shazam():
    """Back to the top of the Songs list."""
    adb("shell", "am", "force-stop", "com.shazam.android")
    return open_song_list()


def cmd_clear_phone(yes, rehearse):
    saved = load(RAW, [])
    if not saved or not load(STATE, {}).get("last_pull"):
        sys.exit("Nothing pulled yet. Run 'python digger.py android' first; only Shazams saved on this computer can be cleared.")
    print(f"{len(saved)} Shazams are saved in {RAW}")
    check_phone()
    if not open_song_list():
        sys.exit("Could not open Shazam's Library > Songs list on the phone.")
    hits, total = scan_phone(saved)
    print(f"Phone list: {total} rows, {len(hits)} of them are saved on this computer and can be deleted:")
    for r in hits:
        print(f"  {r.get('artist', '')} - {r['title']}  ({r['when']})")
    restart_shazam()
    if rehearse and hits:
        r = next(iter(on_screen_matches(saved)), None)
        print("Rehearsal (nothing deleted):", delete_one(r, False) if r else "first saved row not on the first screen")
    if not yes:
        print("Dry run: nothing deleted. Add --yes to delete these from the Shazam app (cannot be undone).")
        adb("shell", "input", "keyevent", "HOME")
        return
    done = 0
    for _ in range(len(hits) + 20):
        w, h = screen_size()
        for _ in range(60):
            rows = on_screen_matches(saved)
            if rows:
                break
            if not screen_rows():
                break
            adb("shell", "input", "swipe", str(w // 2), str(int(h * .79)), str(w // 2), str(int(h * .375)), "1500"); time.sleep(0.8)
        if not rows:
            break
        result = delete_one(rows[0], True)
        print(f"  {rows[0].get('artist', '')} - {rows[0]['title']}: {result}")
        if result != "deleted":
            break
        done += 1
    state = load(STATE, {})
    state.pop("newest", None)  # the remembered newest rows are gone from the phone; next pull reads the whole list
    save(STATE, state)
    adb("shell", "input", "keyevent", "HOME")
    print(f"{done} Shazams deleted from the phone. They stay saved in {RAW}.")


# ---------------------------------------------------------------- buy list: your keeps with your own search links

def search_link(tpl, t):
    """Fill a search address template ({q} = artist and title), or "" when the template is empty or has no {q}."""
    tpl = tpl or ""
    return tpl.replace("{q}", urllib.parse.quote(f'{t["artist"]} {clean_title(t["title"])}')) if "{q}" in tpl else ""


def store_link(t):
    """Your music store's search page for this song (store_search in config.toml), or "" when none is set."""
    return search_link(CFG.get("store_search"), t)


def pool_link(t):
    """Your record pool's search page for this song (pool_search in config.toml), or "" when none is set."""
    return search_link(CFG.get("pool_search"), t)


def buy_rows(tracks):
    """One row per keep with your store and pool search links. Nothing is fetched: you open the links yourself."""
    return [{"want": f'{t["artist"]} - {t["title"]}', "store": store_link(t), "pool": pool_link(t)} for t in tracks]


FIELDS = ["want", "store", "pool"]


def write_buy_list(rows, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "buy-list.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS); w.writeheader(); w.writerows(rows)
    e = lambda x: html.escape(str(x))
    link = lambda u, label: f'<a href="{e(u)}" target="_blank" rel="noopener">{label}</a>' if u else ""
    body = "".join(f'<tr><td>{e(r["want"])}</td><td>{link(r["store"], "Store")} {link(r["pool"], "Pool")}</td></tr>' for r in rows)
    page = ('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Buy list</title><style>body{font:15px system-ui;margin:16px;background:#fff;color:#111}'
            'table{border-collapse:collapse;width:100%}td,th{padding:6px;border-bottom:1px solid #ddd;text-align:left}'
            '</style><h1>Buy list</h1><p>Your keeps. Open the links in your own browser and check the match before you buy.</p>'
            '<table><tr><th>You kept</th><th></th></tr>' + body + '</table>')
    open(os.path.join(out_dir, "buy-list.html"), "w", encoding="utf-8").write(page)
    open(os.path.join(out_dir, "buy-list.txt"), "w", encoding="utf-8").write(
        "".join("\t".join(x for x in (r["want"], r["store"], r["pool"]) if x) + "\n" for r in rows))


def only_keeps(rows):
    """From a picks.csv, only the Keeps. Drops and 'I have it' rows never reach a buy list or a playlist."""
    return [r for r in rows if r.get("pick", "keep") == "keep"]


def cmd_find_tracks(path):
    rows = only_keeps(parse_list(open(path, encoding="utf-8-sig").read()))
    if not rows:
        sys.exit("No tracks found. Use the keeps.txt / keeps.csv from the picks page, or 'Artist - Title' lines.")
    out_dir = os.path.join(HERE, CFG["out_dir"])
    found = buy_rows(count_list(rows))
    write_buy_list(found, out_dir)
    print(f"{len(found)} tracks -> {out_dir}/buy-list.html, buy-list.csv, buy-list.txt")
    if not (CFG.get("store_search") or CFG.get("pool_search")):
        print("No store_search or pool_search set in config.toml, so the list has no links yet.")


# ---------------------------------------------------------------- learn from your Keeps and Drops

def learn(rows, taste=None):
    """Add one point per kept track to each of its artists."""
    taste = taste if taste is not None else {"artists": {}}
    for r in rows:
        for a in re.split(r",|&| feat\.? | ft\.? | x | vs\.? ", r["artist"]):
            if norm(a):
                taste["artists"][norm(a)] = taste["artists"].get(norm(a), 0) + 1
    return taste


def remember_decisions(new, path=None):
    """Every Keep, Drop and 'I have it' you ever gave is kept in data/decisions.json, so each learn run uses all of them,
    not only the latest file. The newest pick for a song wins. Returns (keeps, drops). 'I have it' means you own the song,
    so it teaches the taste like a Keep: it comes back in keeps with pick "keep" and have=True (the file keeps the word
    "have"). It never goes on a buy list: only the keeps file you give find-tracks is used for that."""
    path = path or DECISIONS
    known = {dkey(r): r for r in load(path, [])}
    today = dt.date.today().isoformat()
    for r in new:
        if r.get("pick") in ("keep", "drop", "have"):
            known[dkey(r)] = dict(r, at=r.get("at") or today)
    rows = list(known.values())
    save(path, rows)
    keeps = [dict(r, pick="keep", have=r["pick"] == "have") for r in rows if r["pick"] in ("keep", "have")]
    return keeps, [r for r in rows if r["pick"] == "drop"]


def cmd_learn(path, drops_file=None, library_file=None, network=False):
    """Learn from a picks file (picks.csv from the page: Artist, Title, Pick) or a keeps list, plus an optional list of
    drops and an optional library list (tracks you play; they count as Keeps). Retrain whenever you decide more."""
    rows = parse_list(open(path, encoding="utf-8-sig").read())
    if not rows:
        sys.exit("No tracks found. Use picks.csv (or keeps.txt / keeps.csv) from the picks page.")
    new = [dict(r, pick=r.get("pick", "keep")) for r in rows]
    if drops_file:
        new += [dict(r, pick="drop") for r in parse_list(open(drops_file, encoding="utf-8-sig").read())]
    keeps, drops = remember_decisions(new)
    library = parse_list(open(library_file, encoding="utf-8-sig").read()) if library_file else []
    taste = learn(keeps, load(TASTE, {"artists": {}}))
    if drops or library:
        taste["model"] = learned.train(keeps, drops, library)
        if len(keeps) >= 5 and len(drops) >= 5:
            c = learned.check(keeps, drops, library)
            taste["model"]["check"] = c
            print(f"Honest check (each song hidden, then guessed): {c['keeps_in_top']} of your {c['keeps']} keeps land in its "
                  f"top {c['keeps']} places; score {c['auc']} where 0.5 is a coin toss and 1 is perfect.")
        days = sorted({(r.get("at") or "")[:10] for r in keeps + drops} - {""})
        if len(days) >= 2:
            ts = learned.time_split(keeps + drops, days[len(days) // 2], library)
            if ts.get("auc") is not None:
                print(f"Time-split check (older decisions teach, newer ones test): score {ts['auc']}, 90% range "
                      f"{ts['interval_90'][0]} to {ts['interval_90'][1]} over {ts['test']} decisions.")
        top = learned.strongest(taste["model"], 5)
        print("Learned for:", ", ".join(k for k, _ in top["for"]) or "-", "| against:", ", ".join(k for k, _ in top["against"]) or "-")
    save(TASTE, taste)
    learn_sound()
    n_have = sum(1 for r in keeps if r.get("have"))
    print(f"Learned from {len(keeps)} keeps ({n_have} of them 'I have it'), {len(drops)} drops, {len(library)} library tracks. Rebuilding the page...")
    finish(load(TRACKS, []), network)


# ---------------------------------------------------------------- Soulseek (optional): find and download your keeps

def cmd_soulseek(path, look=False, run=subprocess.run):
    """Hand your keeps to Sockseek (github.com/fiso64/sockseek), an open source Soulseek downloader you install
    yourself. It searches Soulseek for each 'Artist - Title' and downloads the best copy. --look only searches."""
    exe = os.path.expanduser(CFG["soulseek_bin"]) or shutil.which("sockseek") or ""
    if not exe or not os.path.exists(exe):
        sys.exit("Set soulseek_bin in config.toml to your Sockseek program (get it from github.com/fiso64/sockseek/releases).")
    rows = parse_list(open(path, encoding="utf-8-sig").read()) if os.path.exists(path) else parse_list(path)
    if not rows:
        sys.exit("No 'Artist - Title' lines found.")
    out = os.path.join(HERE, os.path.expanduser(CFG["soulseek_dir"]))
    os.makedirs(out, exist_ok=True)
    lst = os.path.join(out, "wanted.csv")
    with open(lst, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(["Artist", "Title"]); w.writerows([r["artist"], r["title"]] for r in rows)
    cmd = [exe, lst, "-o", out, "--name-format", "{artist} - {title}", "--concurrent-jobs", "2"]
    if CFG["soulseek_conf"]:
        cmd += ["--config", os.path.expanduser(CFG["soulseek_conf"])]
    if look:
        cmd += ["--print", "results"]
    print(f"{len(rows)} tracks -> {'search only' if look else out}")
    return run(cmd)


# ---------------------------------------------------------------- Rekordbox: a playlist of the keeps you have bought

AUDIO = (".mp3", ".wav", ".aiff", ".aif", ".flac", ".m4a", ".aac", ".ogg")


def match_files(tracks, files):
    """Pair each keep with the audio file whose name holds the artist's first name and every title word."""
    pairs, used = [], set()
    for t in tracks:
        first = words(re.split(r",|&| feat| x ", t["artist"])[0]) - GENERIC
        want = words(clean_title(t["title"])) - GENERIC
        mix_w = words(" ".join(re.findall(r"[(\[]([^)\]]*)[)\]]", t["title"]))) - {"mix", "original"}
        cands = [f for f in files if f not in used and first <= words(os.path.basename(f)) and want <= words(os.path.basename(f))]
        cands.sort(key=lambda f: (not mix_w <= words(os.path.basename(f)), len(os.path.basename(f))))
        pairs.append((t, cands[0] if cands else None))
        if cands:
            used.add(cands[0])
    return pairs


def file_url(path):
    p = os.path.abspath(path).replace(os.sep, "/")
    return "file://localhost" + ("" if p.startswith("/") else "/") + urllib.parse.quote(p)


def rekordbox_xml(pairs, name="Shazam Keeps"):
    a = lambda x: html.escape(str(x), quote=True)
    have = [(t, f) for t, f in pairs if f]
    coll = "".join(f'<TRACK TrackID="{i}" Name="{a(t["title"])}" Artist="{a(t["artist"])}" Kind="{a(os.path.splitext(f)[1][1:].upper())} File" '
                   f'Location="{a(file_url(f))}"/>' for i, (t, f) in enumerate(have, 1))
    keys = "".join(f'<TRACK Key="{i}"/>' for i in range(1, len(have) + 1))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<DJ_PLAYLISTS Version="1.0.0">'
            '<PRODUCT Name="rekordbox" Version="6.0.0" Company="AlphaTheta"/>'
            f'<COLLECTION Entries="{len(have)}">{coll}</COLLECTION>'
            f'<PLAYLISTS><NODE Type="0" Name="ROOT" Count="1"><NODE Name="{a(name)}" Type="1" KeyType="0" Entries="{len(have)}">'
            f'{keys}</NODE></NODE></PLAYLISTS></DJ_PLAYLISTS>\n')


def cmd_rekordbox(folder, keeps):
    rows = only_keeps(parse_list(open(keeps, encoding="utf-8-sig").read()))
    files = [os.path.join(r, f) for r, _, fs in os.walk(os.path.expanduser(folder)) for f in fs if f.lower().endswith(AUDIO)]
    pairs = match_files(count_list(rows), files)
    out = os.path.join(HERE, CFG["out_dir"], "rekordbox.xml")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write(rekordbox_xml(pairs))
    missing = [t for t, f in pairs if not f]
    print(f"{len(pairs) - len(missing)} of {len(pairs)} keeps found in {folder} -> {out}")
    for t in missing:
        print(f"  still to buy: {t['artist']} - {t['title']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Turn your Shazams into a Keep/Drop picks page.")
    ap.add_argument("command", choices=["list", "android", "page", "clear-phone", "find-tracks", "learn", "soulseek", "rekordbox-xml"])
    ap.add_argument("file", nargs="?", help="list / find-tracks / learn: a .txt or .csv of 'Artist - Title' lines; rekordbox-xml: your music folder")
    ap.add_argument("keeps", nargs="?", help="rekordbox-xml: your keeps.txt or keeps.csv")
    ap.add_argument("--drops", help="learn: an extra list of tracks you dropped")
    ap.add_argument("--library", help="learn: a list of tracks you play (your DJ library); they count as Keeps")
    ap.add_argument("--all", action="store_true", help="android: read the whole Songs list, not just the new Shazams")
    ap.add_argument("--yes", action="store_true", help="clear-phone: really delete (default is a dry run)")
    ap.add_argument("--dry-run", action="store_true", help="clear-phone: only show what would be deleted (the default)")
    ap.add_argument("--rehearse", action="store_true", help="clear-phone dry run: open the delete box for one Shazam, then press Cancel")
    ap.add_argument("--look", action="store_true", help="soulseek: only search and show what is there, download nothing")
    ap.add_argument("--no-network", action="store_true", help="skip YouTube and preview lookups")
    ap.add_argument("--screenshots", action="store_true", help="android: also identify songs from YouTube player screenshots (macOS)")
    a = ap.parse_args(argv)
    network = CFG["network"] and not a.no_network
    if a.command == "list":
        if not a.file:
            ap.error("list needs a file, e.g. python digger.py list sample/shazams.txt")
        cmd_list(a.file, network)
    elif a.command == "android":
        cmd_android(network, a.screenshots, a.all)
    elif a.command == "clear-phone":
        cmd_clear_phone(a.yes and not a.dry_run, a.rehearse)
    elif a.command == "find-tracks":
        if not a.file:
            ap.error("find-tracks needs your keeps file, e.g. python digger.py find-tracks keeps.txt")
        cmd_find_tracks(a.file)
    elif a.command == "learn":
        if not a.file:
            ap.error("learn needs your keeps file, e.g. python digger.py learn keeps.txt")
        cmd_learn(a.file, a.drops, a.library, network)
    elif a.command == "soulseek":
        if not a.file:
            ap.error("soulseek needs your keeps file or one 'Artist - Title', e.g. python digger.py soulseek keeps.txt")
        cmd_soulseek(a.file, a.look)
    elif a.command == "rekordbox-xml":
        if not (a.file and a.keeps):
            ap.error("rekordbox-xml needs a music folder and your keeps file, e.g. python digger.py rekordbox-xml ~/Music/Bought keeps.txt")
        cmd_rekordbox(a.file, a.keeps)
    else:
        finish(load(TRACKS, []), network)


if __name__ == "__main__":
    main()
