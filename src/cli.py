# -*- coding: utf-8 -*-
"""
命令行批量采集（无界面，适合挂着跑）。

  python src/cli.py list                      展开需求表，看看每条有多少个视频
  python src/cli.py download                  下载全部
  python src/cli.py download --site bilibili  只下某个站点
  python src/cli.py download --only 1,5,12    只下需求表的第 1/5/12 行
  python src/cli.py download --round-gap 20   被拒后每轮之间等 20 分钟
  python src/cli.py status                    看进度

断点续传：完成的视频记在 state/done.txt，重复运行自动跳过；
中断的 .part 下次从断点继续；确认失效的记在 state/skipped.txt，不再重试。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core          # noqa: E402
import sources as src_mod   # noqa: E402
from engine import Engine   # noqa: E402


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def main():
    ap = argparse.ArgumentParser(description="多站点视频采集")
    ap.add_argument("cmd", choices=["list", "download", "status"])
    ap.add_argument("--config", default=None)
    ap.add_argument("--sources", default=None, help="需求表路径，默认读 config.json")
    ap.add_argument("--site", default=None, help="只处理某站点: youtube/bilibili/douyin")
    ap.add_argument("--only", default=None, help="只处理需求表的第几行，逗号分隔")
    ap.add_argument("--workers", type=int, default=0, help="覆盖站点默认并行数")
    ap.add_argument("--audio-only", action="store_true", help="只下音频")
    ap.add_argument("--round-gap", type=float, default=0, help="轮次间隔(分钟)")
    ap.add_argument("--max-rounds", type=int, default=0, help="0=不限")
    ap.add_argument("--refresh", action="store_true", help="重新展开需求表")
    args = ap.parse_args()

    cfg = core.load_config(args.config)
    eng = Engine(cfg, on_log=log)

    name, path = core.find_js_runtime()
    log(f"JavaScript 运行时: {name} ({path})" if name == "deno" else
        "⚠ 未找到 deno，YouTube 会下载失败: winget install DenoLand.Deno")
    ff = core.find_ffmpeg()
    log(f"ffmpeg: {ff}" if ff else "⚠ 未找到 ffmpeg，画质受限: winget install Gyan.FFmpeg")

    srcs = src_mod.read_sources(core.ROOT / (args.sources or cfg["sources_file"]))
    if args.site:
        srcs = [s for s in srcs if s["site"] == args.site]
    if args.only:
        keep = {int(x) for x in args.only.replace("，", ",").split(",") if x.strip()}
        srcs = [s for s in srcs if s["no"] in keep]
    log(f"需求表 {len(srcs)} 条来源")

    if args.cmd == "list":
        tasks = eng.expand(srcs)
        log(f"共 {len(tasks)} 个视频，清单写入 {eng.tasks_file}")
        return

    tasks = eng.load_tasks()
    keep_no = {s["no"] for s in srcs}
    if args.cmd == "status":
        sel = [t for t in tasks if t["src_no"] in keep_no]
        todo = [t for t in sel if eng.state_of(t) == "todo"]
        log(f"清单 {len(sel)} 个：已完成 {len([t for t in sel if eng.state_of(t) == 'done'])}，"
            f"已跳过 {len([t for t in sel if eng.state_of(t) == 'skip'])}，待下载 {len(todo)}")
        return

    if args.refresh or not tasks:
        tasks = eng.expand(srcs)
    else:
        log(f"沿用已有清单（要重新展开加 --refresh）")
    tasks = [t for t in tasks if t["src_no"] in keep_no]

    overrides = {"audio_only": True} if args.audio_only else None
    last = {"n": 0}

    def on_prog(done=0, total=0, ok=0, skip=0, fail=0, rnd=1, pending=0, finished=False):
        if done != last["n"] and (done % 10 == 0 or finished):
            last["n"] = done
            log(f"  第{rnd}轮 {done}/{total} · 成功 {ok} 跳过 {skip} 失败 {fail} 待重试 {pending}")

    eng.on_progress = on_prog
    eng.run(tasks, workers=args.workers, round_gap=args.round_gap,
            max_rounds=args.max_rounds, overrides=overrides)


if __name__ == "__main__":
    main()
