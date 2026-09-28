# -*- coding: utf-8 -*-
"""
下载引擎：命令行和图形界面共用。
通过回调把日志、单条状态、总进度抛给调用方，自身不依赖任何界面。
"""
import csv
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import core
import sources as src_mod
import yt_dlp
from autoupload import Pipeline


class Engine:
    def __init__(self, cfg, on_log=None, on_status=None, on_progress=None):
        self.cfg = cfg
        self.on_log = on_log or (lambda m: print(m, flush=True))
        self.on_status = on_status or (lambda t, s, p="": None)
        self.on_progress = on_progress or (lambda **kw: None)
        self.root = core.ROOT
        self.state = self.root / cfg.get("state_dir", "state")
        self.state.mkdir(parents=True, exist_ok=True)
        self.out_root = self.root / cfg.get("output_dir", "videos")
        self.done_file = self.state / "done.txt"
        self.skip_file = self.state / "skipped.txt"
        self.fail_file = self.state / "failed.txt"
        self.tasks_file = self.state / "tasks.csv"
        self.stop_event = threading.Event()
        self.deferred = []
        self.def_lock = threading.Lock()
        self.nojs_warned = False
        self.pipeline = None          # 边下边传
        self.reload_state()

    def reload_state(self):
        self.done = core.load_set(self.done_file)
        self.skipped = core.load_map(self.skip_file)

    # ------------------------------------------------ 需求表 → 视频清单
    def expand(self, srcs):
        tasks = []
        for s in srcs:
            if self.stop_event.is_set():
                break
            sc = core.site_cfg(self.cfg, s["site"])
            try:
                title, entries = core.expand_source(s["url"], sc, s["site"])
            except Exception as e:
                self.on_log(f"✖ 第{s['no']}行展开失败 [{s['site']}] "
                            f"{str(e).strip().splitlines()[-1][:110]}")
                continue
            sub = src_mod.source_dirname(s)
            self.on_log(f"第{s['no']:>3}行 [{s['site']:<8}] "
                        f"{(s['title'] or title)[:30]:<30} → {len(entries)} 个")
            for i, e in enumerate(entries, 1):
                e.update({"src_no": s["no"], "src_title": s["title"] or title,
                          "sub": sub, "idx": i, "total": len(entries)})
                tasks.append(e)
        self.save_tasks(tasks)
        return tasks

    FIELDS = ["src_no", "src_title", "site", "idx", "total", "id", "raw_id",
              "title", "duration", "url", "sub"]

    def save_tasks(self, tasks):
        with self.tasks_file.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(tasks)

    def load_tasks(self):
        if not self.tasks_file.exists():
            return []
        with self.tasks_file.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        for r in rows:
            for k in ("src_no", "idx", "total"):
                try:
                    r[k] = int(r[k])
                except (ValueError, KeyError, TypeError):
                    r[k] = 0
        return rows

    def state_of(self, t):
        if t["id"] in self.done:
            return "done"
        if t["id"] in self.skipped:
            return "skip"
        return "todo"

    # ------------------------------------------------ 下载单个
    def download_one(self, t, overrides=None):
        if self.stop_event.is_set():
            return "stopped"
        sc = dict(core.site_cfg(self.cfg, t["site"]))
        sc.update(overrides or {})
        out_dir = self.out_root / t["sub"]
        out_dir.mkdir(parents=True, exist_ok=True)
        width = max(3, len(str(t.get("total") or 999)))
        tag = f"{t['src_no']}-{t['idx']}"
        attempts = int(sc.get("attempts", 3) or 3)
        attempt, cli_i, relaxed, err = 0, 0, False, None
        last = {"t": 0.0}

        def hook(d):
            if self.stop_event.is_set():
                raise KeyboardInterrupt("stopped")
            now = time.time()
            if d.get("status") == "downloading" and now - last["t"] > 0.5:
                last["t"] = now
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                pct = f"{got * 100 / total:5.1f}%" if total else f"{got / 1048576:.0f}MB"
                spd = d.get("speed")
                self.on_status(t, "下载中",
                               f"{pct}  {(spd or 0) / 1048576:.1f}MB/s")
            elif d.get("status") == "finished":
                self.on_status(t, "下载中", "处理中…")

        while attempt < attempts:
            if self.stop_event.is_set():
                return "stopped"
            attempt += 1
            client = core.CLIENT_ROTATION[cli_i] if (sc.get("rotate_clients") and cli_i) else None
            self.on_status(t, "下载中" + (f"({client})" if client else
                                       (f"(第{attempt}次)" if attempt > 1 else "")), "")
            opts = core.build_dl_opts(sc, t["idx"], out_dir, width=width,
                                      relaxed=relaxed, client=client)
            opts["progress_hooks"] = [hook]
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    rc = ydl.download([t["url"]])
                if rc == 0:
                    if t["id"] not in self.done:
                        core.append_line(self.done_file, t["id"])
                        self.done.add(t["id"])
                    if self.pipeline:
                        self.pipeline.submit(t)
                    self.on_status(t, "已完成", "100%")
                    self.on_log(f"完成 [{t['site']}] {tag} {t['title'][:32]}")
                    return "ok"
                err = f"返回码 {rc}"
            except BaseException as e:
                if self.stop_event.is_set():
                    self.on_status(t, "已暂停(可续传)", "")
                    return "stopped"
                msg = str(e).strip()
                err = msg.splitlines()[-1] if msg else repr(e)
                kind = core.classify_error(msg)

                if kind == "client" and core.is_nojs(msg):
                    if not self.nojs_warned:
                        self.nojs_warned = True
                        self.on_log("✖ 缺 JavaScript 运行时，YouTube 拿不到视频地址，"
                                    "换客户端也没用：winget install DenoLand.Deno")
                    self.defer(t)
                    self.on_status(t, "缺 deno·稍后重试", "")
                    return "deferred"
                if kind == "client":
                    if not relaxed:
                        relaxed, attempt = True, attempt - 1
                        continue
                    if sc.get("rotate_clients") and cli_i + 1 < len(core.CLIENT_ROTATION):
                        cli_i, attempt = cli_i + 1, attempt - 1
                        self.on_log(f"{tag} 换 {core.CLIENT_ROTATION[cli_i]} 客户端重试")
                        continue
                    kind = "rate"
                if kind == "permanent":
                    reason = core.permanent_reason(msg)
                    core.append_line(self.skip_file,
                                     f"{t['src_no']}\t{t['id']}\t{t['title']}\t{reason}")
                    self.skipped[t["id"]] = reason
                    self.on_status(t, f"已跳过({reason})", "")
                    self.on_log(f"跳过 [{t['site']}] {tag} {t['title'][:30]}：{reason}")
                    return "skip"
                if kind == "rate":
                    self.defer(t)
                    self.on_status(t, "被拒·稍后重试", err[:40])
                    return "deferred"
            for _ in range(int(5 * attempt * 2)):
                if self.stop_event.is_set():
                    return "stopped"
                time.sleep(0.5)

        self.on_status(t, "失败", (err or "")[:60])
        self.on_log(f"✖ 失败 [{t['site']}] {tag} {t['title'][:30]}: {err}")
        core.append_line(self.fail_file, f"{t['src_no']}\t{t['id']}\t{t['title']}\t{err}")
        return "fail"

    def defer(self, t):
        with self.def_lock:
            self.deferred.append(t)

    # ------------------------------------------------ 轮循下载
    def run(self, tasks, workers=0, round_gap=0.0, max_rounds=0, overrides=None):
        self.stop_event.clear()
        self.nojs_warned = False
        todo = [t for t in tasks if self.state_of(t) == "todo"]
        if not todo:
            self.on_log("所选范围内没有待下载的视频。")
            return
        ok = skip = fail = 0
        done_n = 0
        rnd = 0
        total_n = len(todo)
        self.pipeline = Pipeline(self.cfg, on_log=self.on_log)
        self.pipeline.start()
        if not self.pipeline.enabled:
            self.pipeline = None
        self.on_log(f"开始下载 {total_n} 个")
        while todo and not self.stop_event.is_set():
            rnd += 1
            with self.def_lock:
                self.deferred = []
            if rnd > 1:
                self.on_log(f"—— 第 {rnd} 轮：重试上一轮被拒的 {len(todo)} 个 ——")
            by_site = {}
            for t in todo:
                by_site.setdefault(t["site"], []).append(t)
            for site, group in by_site.items():
                if self.stop_event.is_set():
                    break
                sc = core.site_cfg(self.cfg, site)
                w = int(workers or sc.get("workers", 2) or 2)
                self.on_log(f"[{site}] {len(group)} 个，并行 {w}")
                for t in group:
                    self.on_status(t, "排队中", "")
                with ThreadPoolExecutor(max_workers=w) as ex:
                    futs = [ex.submit(self.download_one, t, overrides) for t in group]
                    for fut in as_completed(futs):
                        res = fut.result()
                        ok += res == "ok"
                        skip += res == "skip"
                        fail += res == "fail"
                        done_n += res in ("ok", "skip", "fail")
                        self.on_progress(done=done_n, total=total_n, ok=ok,
                                         skip=skip, fail=fail, rnd=rnd,
                                         pending=len(self.deferred))
            with self.def_lock:
                todo = list(self.deferred)
            if not todo or self.stop_event.is_set():
                break
            if max_rounds and rnd >= max_rounds:
                self.on_log(f"已达最多 {max_rounds} 轮，剩余 {len(todo)} 个记入 failed.txt")
                for t in todo:
                    core.append_line(self.fail_file,
                                     f"{t['src_no']}\t{t['id']}\t{t['title']}\t被拒，轮循结束")
                    self.on_status(t, "失败(被拒)", "")
                break
            if ok == 0 and round_gap <= 0:
                self.on_log("⚠ 整轮零成功，说明还在被持续拦截。建议配 Cookie 或换代理节点；"
                            "也可以把轮循间隔设成 10~30 分钟慢慢磨。")
                break
            if round_gap > 0:
                self.on_log(f"等待 {round_gap:g} 分钟后开始第 {rnd + 1} 轮 …")
                end = time.time() + round_gap * 60
                while time.time() < end and not self.stop_event.is_set():
                    time.sleep(0.5)
        if self.pipeline:
            if self.stop_event.is_set():
                self.on_log("正在把已下好的文件传完 …")
            self.pipeline.finish(wait=True)
            self.pipeline = None
        self.on_log(f"{'已停止' if self.stop_event.is_set() else '全部结束'}："
                    f"共 {rnd} 轮，成功 {ok}，跳过 {skip}，失败 {fail}")
        self.on_progress(done=done_n, total=total_n, ok=ok, skip=skip, fail=fail,
                         rnd=rnd, pending=0, finished=True)
