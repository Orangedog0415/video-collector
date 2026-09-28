# -*- coding: utf-8 -*-
"""
核对：需求表 → 本地已下载 → 服务器上实际存在，找出遗漏。
  python src/verify.py
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core                     # noqa: E402
from engine import Engine       # noqa: E402

ID_RE = re.compile(r"\[([A-Za-z0-9_.\-]{4,40})\]")
VIDEO_EXT = (".mp4", ".webm", ".mkv", ".flv", ".m4a", ".wav", ".mp3", ".flac")


def ssh(args, cmd, timeout=180):
    return subprocess.run(["ssh", "-p", str(args.port), "-o", "BatchMode=yes",
                           "-o", "ConnectTimeout=15", f"{args.user}@{args.host}", cmd],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def main():
    cfg = core.load_config()
    srv = cfg.get("server", {})
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default=str(core.ROOT / cfg.get("output_dir", "videos")))
    ap.add_argument("--host", default=srv.get("host", ""))
    ap.add_argument("--port", type=int, default=int(srv.get("port", 22) or 22))
    ap.add_argument("--user", default=srv.get("user", ""))
    ap.add_argument("--remote", default=srv.get("remote", "~/datasets/hakka"))
    ap.add_argument("--no-server", action="store_true", help="只核对本地，不连服务器")
    args = ap.parse_args()

    local = Path(args.local)
    eng = Engine(cfg)
    tasks = eng.load_tasks()
    if not tasks:
        raise SystemExit("还没有清单，先跑: python src/cli.py list")

    done = eng.done
    skipped = eng.skipped
    should = {t["raw_id"]: t for t in tasks if t["id"] in done}
    never = [t for t in tasks if t["id"] not in done and t["id"] not in skipped]

    local_ids = set()
    local_files = 0
    if local.exists():
        for f in local.rglob("*"):
            if f.is_file() and f.suffix.lower() in VIDEO_EXT:
                local_files += 1
                m = ID_RE.search(f.name)
                if m:
                    local_ids.add(m.group(1))

    remote_ids, remote_total, zero = set(), 0, []
    if not args.no_server and args.host:
        print("正在读取服务器文件列表 …")
        r = ssh(args, f"find {args.remote} -type f -printf '%s|%P\\n' 2>/dev/null")
        if r.returncode != 0:
            print("连服务器失败:", r.stderr.strip()[:200])
        else:
            for line in r.stdout.splitlines():
                if "|" not in line:
                    continue
                sz, name = line.split("|", 1)
                remote_total += 1
                if sz.strip() == "0":
                    zero.append(name)
                if name.lower().endswith(VIDEO_EXT):
                    m = ID_RE.search(name)
                    if m:
                        remote_ids.add(m.group(1))

    missing = set(should) - remote_ids if remote_ids else set()
    print(f"""
================ 核对结果 ================
需求表展开后的视频总数   {len(tasks)}
已下载(done.txt)         {len(done)}
主动跳过(skipped.txt)    {len(skipped)}
尚未下载                 {len(never)}

本地视频文件             {local_files}
服务器文件总数           {remote_total}
服务器上的视频           {len(remote_ids)}
  → 应有但缺失           {len(missing)}
      本地还有(可重传)       {len(missing & local_ids)}
      本地也没了(需重下)     {len(missing - local_ids)}
服务器上的 0 字节文件    {len(zero)}
==========================================""")

    st = core.ROOT / cfg.get("state_dir", "state")
    if missing:
        p = st / "missing_on_server.txt"
        p.write_text("".join(f"{should[i]['src_no']}\t{i}\t{should[i]['title']}\t"
                             f"{should[i]['url']}\n" for i in sorted(missing)),
                     encoding="utf-8")
        print(f"  已写出 {p}（{len(missing)} 条）→ 再跑一次 uploader.py 即可补传")
    if never:
        p = st / "never_downloaded.txt"
        p.write_text("".join(f"{t['src_no']}\t{t['raw_id']}\t{t['title']}\t{t['url']}\n"
                             for t in never), encoding="utf-8")
        print(f"  已写出 {p}（{len(never)} 条）→ 在界面里重试或 cli.py download")
    if zero:
        print("  ⚠ 0 字节文件:", zero[:5], "…" if len(zero) > 5 else "")
    if not missing and not never and not zero:
        print("\n✅ 完全一致，没有遗漏。")


if __name__ == "__main__":
    main()
