# -*- coding: utf-8 -*-
"""
多站点视频采集 - 图形界面
需求表驱动：一行一个来源（播放列表/频道/单视频），按来源分目录下载。
依赖: pip install -U "yt-dlp[default]" openpyxl   可选: ffmpeg, deno
"""
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core                    # noqa: E402
import sources as src_mod      # noqa: E402
from engine import Engine      # noqa: E402

TAGS = {"done": "#2e7d32", "fail": "#c62828", "run": "#1565c0",
        "part": "#ef6c00", "skip": "#9e9e9e"}


class App:
    COLS = ("no", "src", "site", "idx", "title", "status", "prog")

    def __init__(self, root):
        self.root = root
        root.title("多站点视频采集")
        root.geometry("1320x880")
        root.minsize(1000, 640)
        self.q = queue.Queue()
        self.cfg = core.load_config()
        self.tasks = []
        self.row_of = {}          # task id -> tree iid
        self.eng = Engine(self.cfg,
                          on_log=lambda m: self.q.put(("log", m)),
                          on_status=lambda t, s, p="": self.q.put(("row", t["id"], s, p)),
                          on_progress=lambda **kw: self.q.put(("prog", kw)))
        self.running = False
        self.build_ui()
        self.apply_font()
        self.check_env()
        self.load_existing()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(120, self.poll)

    # ------------------------------------------------ 界面
    def build_ui(self):
        pad = dict(padx=4, pady=3)
        top = ttk.LabelFrame(self.root, text="设置（站点级的代理/Cookie/并发在 config.json 里改）")
        top.pack(fill="x", padx=8, pady=6)

        r0 = ttk.Frame(top); r0.pack(fill="x")
        ttk.Label(r0, text="需求表").pack(side="left", **pad)
        self.v_src = tk.StringVar(value=self.cfg.get("sources_file", ""))
        ttk.Entry(r0, textvariable=self.v_src, width=52).pack(side="left")
        ttk.Button(r0, text="…", width=3, command=self.browse_src).pack(side="left")
        ttk.Label(r0, text="  输出目录").pack(side="left", **pad)
        self.v_out = tk.StringVar(value=self.cfg.get("output_dir", "videos"))
        ttk.Entry(r0, textvariable=self.v_out, width=26).pack(side="left")
        ttk.Button(r0, text="打开", command=self.open_out).pack(side="left", padx=4)
        ttk.Button(r0, text="编辑 config.json", command=self.edit_config).pack(side="left", padx=4)

        r1 = ttk.Frame(top); r1.pack(fill="x")
        ttk.Label(r1, text="站点筛选").pack(side="left", **pad)
        self.v_site = tk.StringVar(value="全部")
        ttk.Combobox(r1, textvariable=self.v_site, width=10, state="readonly",
                     values=["全部", "youtube", "bilibili", "douyin", "other"]
                     ).pack(side="left")
        ttk.Label(r1, text="  需求表行号(留空=全部)").pack(side="left", **pad)
        self.v_only = tk.StringVar()
        ttk.Entry(r1, textvariable=self.v_only, width=14).pack(side="left")
        ttk.Label(r1, text="  并行数(0=按站点配置)").pack(side="left", **pad)
        self.v_workers = tk.IntVar(value=0)
        ttk.Spinbox(r1, from_=0, to=16, width=4, textvariable=self.v_workers).pack(side="left")
        self.v_audio = tk.BooleanVar(value=False)
        ttk.Checkbutton(r1, text="仅音频", variable=self.v_audio).pack(side="left", padx=8)
        self.v_autoup = tk.BooleanVar(
            value=bool(self.cfg.get("auto_upload", {}).get("enabled")))
        ttk.Checkbutton(r1, text="边下边传(传完删本地)",
                        variable=self.v_autoup).pack(side="left", padx=4)
        ttk.Label(r1, text="轮循间隔(分)").pack(side="left", **pad)
        self.v_gap = tk.DoubleVar(value=0)
        ttk.Spinbox(r1, from_=0, to=720, increment=5, width=5,
                    textvariable=self.v_gap).pack(side="left")
        ttk.Label(r1, text="最多轮数(0=不限)").pack(side="left", **pad)
        self.v_rounds = tk.IntVar(value=0)
        ttk.Spinbox(r1, from_=0, to=99, width=4, textvariable=self.v_rounds).pack(side="left")
        ttk.Label(r1, text="  字号").pack(side="left", **pad)
        self.v_font = tk.IntVar(value=11)
        ttk.Spinbox(r1, from_=8, to=24, width=4, textvariable=self.v_font,
                    command=self.apply_font).pack(side="left")

        ctl = ttk.Frame(self.root); ctl.pack(fill="x", padx=8)
        self.btn_expand = ttk.Button(ctl, text="① 读取需求表", command=self.do_expand)
        self.btn_expand.pack(side="left", padx=3, pady=4)
        self.btn_start = ttk.Button(ctl, text="② 开始/继续下载", command=self.do_start)
        self.btn_start.pack(side="left", padx=3)
        self.btn_sel = ttk.Button(ctl, text="下载选中", command=lambda: self.do_start(True))
        self.btn_sel.pack(side="left", padx=3)
        self.btn_retry = ttk.Button(ctl, text="只重试失败项", command=self.do_retry)
        self.btn_retry.pack(side="left", padx=3)
        self.btn_stop = ttk.Button(ctl, text="■ 停止(保留进度)", command=self.do_stop,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=3)

        mid = ttk.Frame(self.root); mid.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree = ttk.Treeview(mid, columns=self.COLS, show="headings", selectmode="extended")
        for c, txt, w, anc in [("no", "行", 45, "e"), ("src", "来源", 260, "w"),
                               ("site", "站点", 80, "w"), ("idx", "#", 50, "e"),
                               ("title", "标题", 430, "w"), ("status", "状态", 130, "w"),
                               ("prog", "进度", 150, "w")]:
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=anc, stretch=(c == "title"))
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        for k, color in TAGS.items():
            self.tree.tag_configure(k, foreground=color)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="下载选中", command=lambda: self.do_start(True))
        self.menu.add_command(label="标记为未完成(重下)", command=lambda: self.mark(False))
        self.menu.add_command(label="标记为已完成(跳过)", command=lambda: self.mark(True))
        self.menu.add_command(label="清除「已跳过」标记", command=self.unskip)
        self.menu.add_command(label="复制链接", command=self.copy_links)
        self.tree.bind("<Button-3>", self.popup)

        bot = ttk.Frame(self.root); bot.pack(fill="x", padx=8)
        self.pbar = ttk.Progressbar(bot, mode="determinate")
        self.pbar.pack(side="left", fill="x", expand=True, pady=4)
        self.v_sum = tk.StringVar(value="")
        ttk.Label(bot, textvariable=self.v_sum, width=52).pack(side="left", padx=6)

        self.txt = ScrolledText(self.root, height=9, state="disabled")
        self.txt.pack(fill="both", padx=8, pady=(0, 8))

    def apply_font(self, *_):
        try:
            size = max(8, min(24, int(self.v_font.get())))
        except (tk.TclError, ValueError):
            return
        for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(n).configure(size=size, family="Microsoft YaHei UI")
            except tk.TclError:
                pass
        st = ttk.Style()
        st.configure("Treeview", rowheight=int(size * 2.2))
        st.configure("Treeview.Heading", font=("Microsoft YaHei UI", size, "bold"))
        self.txt.configure(font=("Consolas", size))

    # ------------------------------------------------ 环境与配置
    def check_env(self):
        name, path = core.find_js_runtime()
        if name == "deno":
            self.log(f"JavaScript 运行时: deno ({path})")
        elif name:
            self.log(f"⚠ 只找到 {name}，yt-dlp 默认只用 deno；YouTube 会失败："
                     "winget install DenoLand.Deno")
        else:
            self.log("⚠ 未找到 deno，YouTube 会失败: winget install DenoLand.Deno")
        if not core.has_ejs():
            self.log('⚠ 缺 challenge solver: pip install -U "yt-dlp[default]"')
        ff = core.find_ffmpeg()
        self.log(f"ffmpeg: {ff}" if ff else
                 "⚠ 未找到 ffmpeg，画质受限: winget install Gyan.FFmpeg")

    def edit_config(self):
        p = core.ROOT / "config.json"
        if not p.exists():
            sample = core.ROOT / "config.example.json"
            p.write_text(sample.read_text(encoding="utf-8") if sample.exists() else "{}",
                         encoding="utf-8")
        if sys.platform.startswith("win"):
            os.startfile(str(p))
        else:
            subprocess.Popen(["xdg-open", str(p)])
        self.log("改完 config.json 后重启本程序生效")

    def browse_src(self):
        f = filedialog.askopenfilename(filetypes=[("需求表", "*.xlsx *.xlsm *.csv"),
                                                  ("所有文件", "*.*")])
        if f:
            self.v_src.set(f)

    def open_out(self):
        d = core.ROOT / self.v_out.get()
        d.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(d))
        else:
            subprocess.Popen(["xdg-open", str(d)])

    # ------------------------------------------------ 清单
    def read_srcs(self):
        srcs = src_mod.read_sources(core.ROOT / self.v_src.get())
        if self.v_site.get() != "全部":
            srcs = [s for s in srcs if s["site"] == self.v_site.get()]
        only = self.v_only.get().replace("，", ",").strip()
        if only:
            keep = {int(x) for x in only.split(",") if x.strip().isdigit()}
            srcs = [s for s in srcs if s["no"] in keep]
        return srcs

    def do_expand(self):
        if self.running:
            return
        self.btn_expand.configure(state="disabled")
        self.log("正在展开需求表（播放列表/频道要逐个访问，可能要几分钟）…")

        def job():
            try:
                srcs = self.read_srcs()
                tasks = self.eng.expand(srcs)
                self.q.put(("tasks", tasks))
            except Exception as e:
                self.q.put(("log", f"读取失败: {e}"))
                self.q.put(("tasks", None))
        threading.Thread(target=job, daemon=True).start()

    def load_existing(self):
        tasks = self.eng.load_tasks()
        if tasks:
            self.fill(tasks)
            self.log(f"已载入上次的清单 {len(tasks)} 个视频（需求表有更新就点「读取需求表」）")
        else:
            self.log("还没有清单，先点「① 读取需求表」")

    def fill(self, tasks):
        self.tasks = tasks
        self.tree.delete(*self.tree.get_children())
        self.row_of.clear()
        self.eng.reload_state()
        for t in tasks:
            st = self.eng.state_of(t)
            status = {"done": "已完成", "skip": f"已跳过({self.eng.skipped.get(t['id'], '')})",
                      "todo": "待下载"}[st]
            tag = {"done": "done", "skip": "skip", "todo": ""}[st]
            iid = self.tree.insert("", "end", tags=(tag,),
                                   values=(t["src_no"], t["src_title"][:40], t["site"],
                                           t["idx"], t["title"][:70], status, ""))
            self.row_of[t["id"]] = iid
        self.refresh_summary()

    def refresh_summary(self):
        n = len(self.tasks)
        d = sum(1 for t in self.tasks if self.eng.state_of(t) == "done")
        s = sum(1 for t in self.tasks if self.eng.state_of(t) == "skip")
        if not self.running:
            self.pbar.configure(maximum=max(n, 1), value=d + s)
            self.v_sum.set(f"共 {n}，已完成 {d}，已跳过 {s}，剩余 {n - d - s}")

    # ------------------------------------------------ 下载
    def selected(self):
        sel = set(self.tree.selection())
        return [t for t in self.tasks if self.row_of.get(t["id"]) in sel]

    def do_start(self, only_selected=False):
        if self.running:
            return
        if not self.tasks:
            messagebox.showinfo("提示", "请先点「① 读取需求表」")
            return
        tasks = self.selected() if only_selected else self.tasks
        if only_selected and not tasks:
            messagebox.showinfo("提示", "请先在列表里选中要下载的视频（Ctrl/Shift 多选）")
            return
        if only_selected:            # 手动选中时忽略已完成/已跳过标记，强制重下
            for t in tasks:
                self.eng.done.discard(t["id"])
                self.eng.skipped.pop(t["id"], None)
        self.launch(tasks)

    def do_retry(self):
        ids = {l.split("\t")[1] for l in
               core.load_set(self.eng.fail_file) if len(l.split("\t")) > 1}
        tasks = [t for t in self.tasks if t["id"] in ids and t["id"] not in self.eng.done]
        if not tasks:
            messagebox.showinfo("提示", "failed.txt 里没有待重试的视频。")
            return
        self.launch(tasks)

    def launch(self, tasks):
        self.running = True
        self.toggle(True)
        fp = self.eng.fail_file
        if fp.exists():
            fp.replace(fp.with_name("failed_prev.txt"))
        self.cfg.setdefault("auto_upload", {})["enabled"] = bool(self.v_autoup.get())
        overrides = {"audio_only": True} if self.v_audio.get() else None
        args = dict(workers=int(self.v_workers.get() or 0),
                    round_gap=float(self.v_gap.get() or 0),
                    max_rounds=int(self.v_rounds.get() or 0),
                    overrides=overrides)
        threading.Thread(target=lambda: self.eng.run(tasks, **args), daemon=True).start()

    def do_stop(self):
        self.eng.stop_event.set()
        self.btn_stop.configure(state="disabled")
        self.log("正在停止…（未完成的 .part 会保留，下次从断点续传）")

    def toggle(self, running):
        for b in (self.btn_start, self.btn_sel, self.btn_retry, self.btn_expand):
            b.configure(state="disabled" if running else "normal")
        self.btn_stop.configure(state="normal" if running else "disabled")

    # ------------------------------------------------ 右键
    def popup(self, ev):
        row = self.tree.identify_row(ev.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        self.menu.tk_popup(ev.x_root, ev.y_root)

    def mark(self, done_flag):
        sel = self.selected()
        if not sel:
            return
        ids = {t["id"] for t in sel}
        with core.file_lock:
            cur = core.load_set(self.eng.done_file)
            cur = (cur | ids) if done_flag else (cur - ids)
            self.eng.done_file.write_text("".join(i + "\n" for i in sorted(cur)),
                                          encoding="utf-8")
        self.eng.done = cur
        for t in sel:
            self.set_row(t["id"], "已完成" if done_flag else "待下载", "",
                         "done" if done_flag else "")
        self.refresh_summary()

    def unskip(self):
        sel = self.selected()
        ids = {t["id"] for t in sel}
        p = self.eng.skip_file
        if p.exists():
            keep = [l for l in p.read_text(encoding="utf-8").splitlines()
                    if len(l.split("\t")) < 2 or l.split("\t")[1] not in ids]
            p.write_text("".join(l + "\n" for l in keep), encoding="utf-8")
        self.eng.reload_state()
        for t in sel:
            self.set_row(t["id"], "待下载", "", "")
        self.refresh_summary()

    def copy_links(self):
        links = "\n".join(t["url"] for t in self.selected())
        if links:
            self.root.clipboard_clear()
            self.root.clipboard_append(links)

    # ------------------------------------------------ 刷新
    def log(self, msg):
        self.q.put(("log", msg))

    def set_row(self, tid, status=None, prog=None, tag=None):
        iid = self.row_of.get(tid)
        if not iid or not self.tree.exists(iid):
            return
        vals = list(self.tree.item(iid, "values"))
        if status is not None:
            vals[5] = status
        if prog is not None:
            vals[6] = prog
        self.tree.item(iid, values=vals)
        if tag is not None:
            self.tree.item(iid, tags=(tag,))

    @staticmethod
    def tag_for(status):
        if status.startswith("已完成"):
            return "done"
        if status.startswith("已跳过"):
            return "skip"
        if status.startswith("失败"):
            return "fail"
        if status.startswith(("被拒", "缺 deno", "已暂停")):
            return "part"
        if status.startswith(("下载中", "后处理")):
            return "run"
        return ""

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "log":
                    self.txt.configure(state="normal")
                    self.txt.insert("end", time.strftime("[%H:%M:%S] ") + str(m[1]) + "\n")
                    self.txt.see("end")
                    self.txt.configure(state="disabled")
                elif m[0] == "row":
                    _, tid, status, prog = m
                    self.set_row(tid, status, prog, self.tag_for(status))
                elif m[0] == "prog":
                    k = m[1]
                    self.pbar.configure(maximum=max(k.get("total", 1), 1),
                                        value=k.get("done", 0))
                    self.v_sum.set(f"第{k.get('rnd', 1)}轮 {k.get('done', 0)}/{k.get('total', 0)}"
                                   f" · 成功 {k.get('ok', 0)} 跳过 {k.get('skip', 0)}"
                                   f" 待重试 {k.get('pending', 0)} 失败 {k.get('fail', 0)}")
                    if k.get("finished"):
                        self.running = False
                        self.toggle(False)
                        self.refresh_summary()
                elif m[0] == "tasks":
                    self.btn_expand.configure(state="normal")
                    if m[1] is not None:
                        self.fill(m[1])
                        self.log(f"清单展开完成：{len(m[1])} 个视频")
        except queue.Empty:
            pass
        self.root.after(150, self.poll)

    def on_close(self):
        if self.running and not messagebox.askyesno(
                "退出", "正在下载，确定退出吗？\n未完成的文件会保留，下次可以继续。"):
            return
        self.eng.stop_event.set()
        self.root.destroy()


def main():
    try:
        from ctypes import windll      # 高 DPI：必须在建窗口前设置
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
