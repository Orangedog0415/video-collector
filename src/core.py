# -*- coding: utf-8 -*-
"""
多站点视频采集核心：站点识别、按站点配置、列表展开、下载与错误分类。
被 cli.py 和 gui.py 共用。
"""
import glob
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    raise SystemExit('未安装 yt-dlp，请先运行:  pip install -U "yt-dlp[default]"')

ROOT = Path(__file__).resolve().parent.parent
FFMPEG = None                      # 探测到的 ffmpeg 路径
file_lock = threading.Lock()

# ---------------------------------------------------------------- 站点识别
SITE_PATTERNS = [
    ("youtube", r"(youtube\.com|youtu\.be)"),
    ("bilibili", r"(bilibili\.com|b23\.tv)"),
    ("douyin", r"(douyin\.com|iesdouyin\.com)"),
    ("kuaishou", r"kuaishou\.com"),
    ("xiaohongshu", r"(xiaohongshu\.com|xhslink\.com)"),
]


def site_of(url: str) -> str:
    u = (url or "").lower()
    for name, pat in SITE_PATTERNS:
        if re.search(pat, u):
            return name
    return "other"


def clean_url(url: str) -> str:
    """清掉分享文案残留，例如 https://v.douyin.com/xxx/ S@Y.zG 复制来的尾巴"""
    u = (url or "").strip()
    m = re.search(r"https?://[^\s，,；;、]+", u)
    if not m:
        return u
    u = m.group(0).rstrip("。．.,，、")
    # 抖音短链后面常粘着无关字符：只保留 /<code>/ 这一段
    m2 = re.match(r"(https?://v\.douyin\.com/[A-Za-z0-9_-]+/?)", u)
    if m2:
        return m2.group(1)
    return u


# ---------------------------------------------------------------- 配置
DEFAULT_CONFIG = {
    "output_dir": "videos",
    "state_dir": "state",
    "sources_file": "sources/客家话_0928.xlsx",
    "sites": {
        "default": {
            "proxy": "", "cookies_from_browser": "", "cookies_file": "",
            "workers": 2, "sleep": 3, "max_height": 1080,
            "audio_only": False, "audio_format": "wav",
            "subs": False, "auto_subs": False, "sub_langs": "zh.*,nan.*,hak.*,en.*",
            "frags": 4, "attempts": 3, "throttle_kb": 50, "rotate_clients": False,
        },
        "youtube": {"proxy": "http://127.0.0.1:7890", "workers": 3, "sleep": 5,
                    "rotate_clients": True},
        "bilibili": {"proxy": "", "workers": 2, "sleep": 3},
        "douyin": {"proxy": "", "workers": 1, "sleep": 8, "cookies_from_browser": "chrome"},
    },
    "server": {"host": "", "port": 22, "user": "", "remote": "~/datasets/hakka"},
}


def load_config(path=None):
    p = Path(path) if path else ROOT / "config.json"
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))       # 深拷贝
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        for k, v in user.items():
            if k == "sites" and isinstance(v, dict):
                for site, sv in v.items():
                    cfg["sites"].setdefault(site, {}).update(sv)
            elif isinstance(v, dict):
                cfg.setdefault(k, {}).update(v)
            else:
                cfg[k] = v
    return cfg


def site_cfg(cfg, site):
    """站点配置 = default 叠加该站点的覆盖项"""
    out = dict(cfg["sites"].get("default", {}))
    out.update(cfg["sites"].get(site, {}))
    return out


# ---------------------------------------------------------------- 外部依赖探测
def find_ffmpeg(extra=None):
    home = os.path.expanduser("~")
    la = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    cands = [extra, FFMPEG, shutil.which("ffmpeg")]
    pats = [
        os.path.join(la, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        os.path.join(la, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", "ffmpeg.exe"),
        os.path.join(home, "scoop", "shims", "ffmpeg.exe"),
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\ffmpeg\bin\ffmpeg.exe",
        str(ROOT / "bin" / "ffmpeg.exe"),
        "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg",
    ]
    for pat in pats:
        cands.extend(sorted(glob.glob(pat, recursive=True)))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def find_js_runtime():
    """deno 是 yt-dlp 唯一开箱即用的 JS 运行时(解 YouTube n 参数必需)，优先找它。
    找到后把目录加进 PATH —— yt-dlp 只在 PATH 里找。"""
    home = os.path.expanduser("~")
    la = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    groups = [
        ("deno", [shutil.which("deno"),
                  os.path.join(home, ".deno", "bin", "deno.exe"),
                  os.path.join(home, ".deno", "bin", "deno"),
                  os.path.join(la, "Microsoft", "WinGet", "Links", "deno.exe"),
                  os.path.join(la, "Microsoft", "WinGet", "Packages", "*Deno*", "deno.exe"),
                  os.path.join(la, "Microsoft", "WinGet", "Packages", "*Deno*", "**", "deno.exe"),
                  os.path.join(home, "scoop", "shims", "deno.exe"),
                  r"C:\ProgramData\chocolatey\bin\deno.exe"]),
        ("node", [shutil.which("node"), os.path.join(pf, "nodejs", "node.exe")]),
        ("bun", [shutil.which("bun")]),
    ]
    for name, cands in groups:
        paths = []
        for c in cands:
            if not c:
                continue
            paths.extend(sorted(glob.glob(c, recursive=True)) if "*" in c else [c])
        for path in paths:
            if os.path.isfile(path):
                d = os.path.dirname(path)
                if d and d not in os.environ.get("PATH", "").split(os.pathsep):
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                return name, path
    return None, None


def has_ejs():
    try:
        import yt_dlp_ejs  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------- 错误分类
RATE_LIMIT_KEYS = ("not a bot", "sign in to confirm", "http error 429", "too many requests",
                   "rate-limit", "rate limit", "rate limited", "try again later",
                   "failed to extract any player response", "this content isn't available",
                   "风控", "-412", "请求过于频繁")
PERMANENT_KEYS = ("private video", "removed by the uploader", "members-only", "join this channel",
                  "made this video available in your country", "has been terminated",
                  "video has been removed", "copyright", "age-restricted", "this live event",
                  "稿件不可见", "视频已失效", "已下架", "404 not found")
CLIENT_ERROR_KEYS = ("page needs to be reloaded", "playability status", "unplayable",
                     "requested format is not available", "no video formats found")
NOJS_KEYS = ("n challenge solving failed", "only images are available",
             "requires a gvs po token", "javascript runtime")
CLIENT_ROTATION = ("", "tv", "web_safari")


def is_nojs(msg):
    return any(k in (msg or "").lower() for k in NOJS_KEYS)


def permanent_reason(msg):
    m = (msg or "").lower()
    for k, zh in (("private video", "私有视频"), ("removed by the uploader", "已被上传者删除"),
                  ("video has been removed", "已被删除"), ("members-only", "会员专享"),
                  ("join this channel", "会员专享"), ("has been terminated", "账号已封"),
                  ("made this video available in your country", "地区限制"),
                  ("age-restricted", "年龄限制"), ("copyright", "版权下架"),
                  ("this live event", "直播未开始"), ("稿件不可见", "稿件不可见"),
                  ("视频已失效", "视频已失效"), ("已下架", "已下架"),
                  ("404 not found", "链接失效")):
        if k in m:
            return zh
    return "不可下载"


def classify_error(msg):
    m = (msg or "").lower()
    if any(k in m for k in PERMANENT_KEYS):
        return "permanent"
    if any(k in m for k in RATE_LIMIT_KEYS):
        return "rate"
    if any(k in m for k in CLIENT_ERROR_KEYS):
        return "client"
    if "video unavailable" in m or "unable to download api page" in m:
        return "rate"        # 没给具体原因的多半是被拦，不当成永久失效
    return "retry"


# ---------------------------------------------------------------- yt-dlp 选项
def base_opts(sc):
    o = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "windowsfilenames": True, "retries": 10, "fragment_retries": 10,
        "socket_timeout": 30, "continuedl": True, "ignoreerrors": False,
        "concurrent_fragment_downloads": int(sc.get("frags", 4) or 4),
    }
    if sc.get("proxy"):
        o["proxy"] = sc["proxy"]
    if sc.get("cookies_from_browser"):
        o["cookiesfrombrowser"] = (sc["cookies_from_browser"],)
    if sc.get("cookies_file"):
        o["cookiefile"] = sc["cookies_file"]
    return o


def build_dl_opts(sc, idx, out_dir, width=4, relaxed=False, client=None):
    o = base_opts(sc)
    ff = find_ffmpeg()
    if ff:
        o["ffmpeg_location"] = ff
    o["outtmpl"] = str(Path(out_dir) / f"{idx:0{width}d}_%(title).80B [%(id)s].%(ext)s")
    if sc.get("audio_only"):
        o["format"] = "bestaudio/best"
        if ff and sc.get("audio_format") not in (None, "", "original"):
            o["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                    "preferredcodec": sc["audio_format"]}]
    elif relaxed or not ff:
        o["format"] = "bv*+ba/b/best" if ff else "b"
    else:
        h = int(sc.get("max_height", 1080) or 1080)
        o["format"] = f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b"
        o["merge_output_format"] = "mp4"
    if sc.get("subs") or sc.get("auto_subs"):
        o["writesubtitles"] = bool(sc.get("subs"))
        o["writeautomaticsub"] = bool(sc.get("auto_subs"))
        o["subtitleslangs"] = str(sc.get("sub_langs", "zh.*")).split(",")
        o["subtitlesformat"] = "srt/vtt/best"
    if not sc.get("no_info_json"):
        o["writeinfojson"] = True
    if sc.get("sleep"):
        o["sleep_interval"] = float(sc["sleep"])
        o["max_sleep_interval"] = float(sc["sleep"]) * 2
    if sc.get("throttle_kb"):
        o["throttledratelimit"] = int(sc["throttle_kb"]) * 1024
    if client:
        o["extractor_args"] = {"youtube": {"player_client": [client]}}
    return o


# ---------------------------------------------------------------- 列表展开
def expand_source(url, sc, site=None):
    """把一条链接展开成视频列表。播放列表/频道/合集 → 多条；单个视频 → 一条。
    返回 (来源标题, [ {id,title,duration,url,site} ])"""
    url = clean_url(url)
    site = site or site_of(url)
    opts = base_opts(sc)
    opts["extract_flat"] = "in_playlist"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    entries = info.get("entries")
    if not entries:                      # 单个视频：B站 BV、抖音、YouTube watch
        vid = info.get("id") or url
        return (info.get("title") or vid), [{
            "id": f"{site}:{vid}", "raw_id": vid,
            "title": info.get("title") or "", "duration": info.get("duration") or "",
            "url": info.get("webpage_url") or url, "site": site,
        }]

    out, stack = [], list(entries)
    while stack:                          # 频道页可能是「列表的列表」，摊平
        e = stack.pop(0)
        if not e:
            continue
        if e.get("_type") == "playlist" and e.get("entries"):
            stack = list(e["entries"]) + stack
            continue
        vid = e.get("id") or ""
        out.append({
            "id": f"{site}:{vid}", "raw_id": vid,
            "title": e.get("title") or "", "duration": e.get("duration") or "",
            "url": e.get("url") or e.get("webpage_url") or vid,
            "site": site,
        })
    return (info.get("title") or url), out


# ---------------------------------------------------------------- 状态文件
def load_set(p):
    p = Path(p)
    if p.exists():
        return {l.strip() for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
                if l.strip()}
    return set()


def append_line(p, line):
    p = Path(p)
    with file_lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def load_map(p, key=1, val=3):
    """读 TSV 状态文件为 {id: 备注}"""
    out = {}
    p = Path(p)
    if p.exists():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            cols = line.split("\t")
            if len(cols) > key:
                out[cols[key]] = cols[val] if len(cols) > val else ""
    return out


def safe_name(s, limit=40):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (s or "").strip())
    return (s[:limit] or "untitled").rstrip(". ")
