# -*- coding: utf-8 -*-
"""
边下边传：下载完一个视频就排队上传，传完校验无误再删本地。
这样本地始终只占几 GB，不用等全部下完再统一上传。
"""
import queue
import shlex
import threading
import time
from pathlib import Path

import core
import uploader as up


class Pipeline:
    def __init__(self, cfg, on_log=None):
        self.cfg = cfg
        au = cfg.get("auto_upload", {})
        srv = cfg.get("server", {})
        self.enabled = bool(au.get("enabled"))
        self.batch = int(au.get("batch", 3) or 3)
        self.idle = float(au.get("idle_seconds", 60) or 60)
        self.delete_local = bool(au.get("delete_local", True))
        self.on_log = on_log or (lambda m: print(m, flush=True))
        self.args = type("A", (), {
            "host": srv.get("host", ""), "port": int(srv.get("port", 22) or 22),
            "user": srv.get("user", ""), "remote": srv.get("remote", "~/datasets"),
            "cipher": "aes128-gcm@openssh.com", "timeout": 900,
        })()
        self.out_root = core.ROOT / cfg.get("output_dir", "videos")
        self.record = core.ROOT / cfg.get("state_dir", "state") / "uploaded.txt"
        self.q = queue.Queue()
        self.thread = None
        self.stop_event = threading.Event()
        self.sent = 0
        self.failed = 0

    # ------------------------------------------------ 生命周期
    def start(self):
        if not self.enabled or self.thread:
            return
        if not self.args.host or not self.args.user:
            self.on_log("⚠ 边下边传已开启，但 config.json 的 server 段没填，已跳过上传")
            self.enabled = False
            return
        r = up.ssh_cmd(self.args, "echo ok", timeout=30)
        if r.returncode != 0:
            self.on_log(f"⚠ 服务器连不上，边下边传关闭：{r.stderr.strip()[:110]}")
            self.enabled = False
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.on_log(f"边下边传已开启 → {self.args.user}@{self.args.host}:{self.args.remote}"
                    f"（每 {self.batch} 个或 {self.idle:.0f} 秒传一次"
                    f"{'，传完删本地' if self.delete_local else ''}）")

    def submit(self, task):
        if self.enabled:
            self.q.put(task)

    def finish(self, wait=True):
        """下载结束后调用：把剩下的传完"""
        if not self.thread:
            return
        self.q.put(None)                   # 收尾信号
        if wait:
            self.thread.join(timeout=1800)
        self.thread = None
        if self.sent or self.failed:
            self.on_log(f"边下边传统计：已上传 {self.sent} 个文件，失败 {self.failed} 个")

    def abort(self):
        self.stop_event.set()
        self.q.put(None)

    # ------------------------------------------------ 内部
    def files_of(self, task):
        """一个视频产生的所有成品文件：视频 + info.json + 字幕"""
        d = self.out_root / task["sub"]
        if not d.exists():
            return []
        raw = task.get("raw_id") or ""
        out = []
        for f in d.glob(f"*[[]{raw}[]]*"):
            if f.is_file() and ".part" not in f.name and not f.name.endswith((".ytdl", ".tmp")):
                out.append(f)
        return out

    def _loop(self):
        pending, last = [], time.time()
        while not self.stop_event.is_set():
            timeout = max(1.0, self.idle - (time.time() - last))
            try:
                item = self.q.get(timeout=timeout)
            except queue.Empty:
                item = "__tick__"
            if item is None:                       # 收尾：传完剩下的就退出
                self._flush(pending)
                return
            if item != "__tick__":
                pending.extend(self.files_of(item))
            if len(pending) >= self.batch or (pending and time.time() - last >= self.idle):
                self._flush(pending)
                pending, last = [], time.time()
        self._flush(pending)

    def _flush(self, files):
        files = [f for f in files if f.exists()]
        if not files:
            return
        by_dir = {}
        for f in files:
            by_dir.setdefault(f.parent, []).append(f)
        for d, group in by_dir.items():
            sub = d.relative_to(self.out_root).as_posix()
            target = f"{self.args.remote}/{sub}"
            r = up.ssh_cmd(self.args, f"mkdir -p {shlex.quote(target)}")
            if r.returncode != 0:
                self.failed += len(group)
                self.on_log(f"⚠ 上传建目录失败: {r.stderr.strip()[:90]}")
                continue
            cmd = (["scp", "-P", str(self.args.port), "-q", "-o", "Compression=no",
                    "-c", self.args.cipher] + up.SSH_OPTS +
                   [str(f) for f in group] +
                   [f"{self.args.user}@{self.args.host}:{target}/"])
            r = up.sh(cmd, timeout=self.args.timeout)
            if r.returncode != 0:
                self.failed += len(group)
                self.on_log(f"⚠ 上传失败({r.returncode})，文件留在本地稍后可用 uploader.py 补传："
                            f"{r.stderr.strip()[:90]}")
                continue
            got = up.remote_sizes(self.args, sub, [f.name for f in group])
            good = [f for f in group if got.get(f.name) == f.stat().st_size]
            for f in good:
                core.append_line(self.record, f"{sub}/{f.name}")
            self.sent += len(good)
            bad = len(group) - len(good)
            if bad:
                self.failed += bad
                self.on_log(f"⚠ {bad} 个文件校验不一致，保留本地")
            if self.delete_local:
                for f in good:
                    try:
                        f.unlink()
                    except OSError:
                        pass
            self.on_log(f"↑ 已上传 {len(good)} 个文件到 {sub}"
                        f"{'（本地已删）' if self.delete_local else ''}")
