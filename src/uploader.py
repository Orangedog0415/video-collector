# -*- coding: utf-8 -*-
"""
把下好的视频分批上传到服务器，校验成功后删除本地副本。
保持来源子目录结构：videos/0007_xxx/... → <远端>/0007_xxx/...

  python src/uploader.py --dry-run          先看要传什么
  python src/uploader.py                    传，保留本地
  python src/uploader.py --delete           传+校验+删本地
  python src/uploader.py --delete --watch 300   循环监视，边下边传
服务器地址在 config.json 的 server 段。需先配好 SSH 密钥免密登录。
"""
import argparse
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core                                  # noqa: E402

SSH_OPTS = [
    "-o", "BatchMode=yes",                   # 绝不弹交互提示，否则会静默卡死
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
]
SKIP_SUFFIX = (".part", ".ytdl", ".tmp")


def sh(cmd, timeout=None):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"超时({timeout}s)，下轮重试")


def ssh_cmd(args, command, timeout=90):
    return sh(["ssh", "-p", str(args.port)] + SSH_OPTS +
              [f"{args.user}@{args.host}", command], timeout=timeout)


def human(b):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f}{u}"
        b /= 1024
    return f"{b:.1f}PB"


def pickable(local: Path):
    """递归挑出已下完的文件：跳过临时文件、跳过同视频仍在写的、跳过刚改动的"""
    now = time.time()
    fresh_parts = {p.name.split(".part")[0] for p in local.rglob("*.part*")
                   if p.is_file() and now - p.stat().st_mtime < 600}
    out = []
    for f in sorted(local.rglob("*")):
        if not f.is_file() or f.name.endswith(SKIP_SUFFIX) or ".part" in f.name:
            continue
        if any(f.name.startswith(pref[:20]) for pref in fresh_parts if pref):
            continue
        if now - f.stat().st_mtime < 120:
            continue
        out.append(f)
    return out


def one_pass(args):
    local = Path(args.local)
    if not local.exists():
        print(f"输出目录不存在: {local}")
        return 0
    record = Path(args.record)
    record.parent.mkdir(parents=True, exist_ok=True)
    uploaded = core.load_set(record)

    cands = pickable(local)
    rel = {f: f.relative_to(local).as_posix() for f in cands}

    if args.delete:                       # 补删上次「已传已记录但没删掉」的残留
        leftover = [f for f in cands if rel[f] in uploaded]
        n = 0
        for group, files in group_by_dir(leftover).items():
            got = remote_sizes(args, group, [f.name for f in files])
            for f in files:
                if got.get(f.name) == f.stat().st_size:
                    try:
                        f.unlink(); n += 1
                    except OSError:
                        pass
        if n:
            print(f"清理上次残留：已确认在服务器上的 {n} 个本地文件已删除")

    files = [f for f in cands if rel[f] not in uploaded]
    total_bytes = sum(f.stat().st_size for f in files)
    print(f"待上传 {len(files)} 个 / {human(total_bytes)}（已上传过 {len(uploaded)} 个）")
    if not files:
        return 0
    if args.dry_run:
        for f in files[:10]:
            print("  ", rel[f])
        if len(files) > 10:
            print("   ...")
        return 0

    r = ssh_cmd(args, f"mkdir -p {args.remote} && df -h $(eval echo {args.remote}) | tail -1")
    if r.returncode != 0:
        print("SSH 连接失败：" + r.stderr.strip()[:200] + "\n请确认能免密登录。")
        return 0
    print("  远端空间:", r.stdout.strip())

    # 按子目录分组，再切批
    batches = []
    for sub, group in group_by_dir(files).items():
        for i in range(0, len(group), args.batch):
            batches.append((sub, group[i:i + args.batch]))

    ok = fail = 0
    sent = [0]
    recent = deque()
    inflight = {}
    lock = threading.Lock()
    t0 = time.time()
    stop_status = threading.Event()

    def emit(msg):
        with lock:
            sys.stdout.write("\r" + " " * 110 + "\r" + msg + "\n")
            sys.stdout.flush()

    def status_loop():
        while not stop_status.is_set():
            now = time.time()
            el = max(1e-6, now - t0)
            while recent and now - recent[0][0] > 60:
                recent.popleft()
            inst = sum(b for _, b in recent) / max(1e-6, min(60.0, el))
            avg = sent[0] / el
            left = total_bytes - sent[0]
            rate = inst if inst > 1000 else avg
            eta = left / rate if rate > 1000 else 0
            eta_s = f"{eta / 3600:.1f}h" if eta > 3600 else (f"{eta / 60:.0f}分" if eta else "--")
            oldest = min((now - v for v in inflight.values()), default=0)
            sys.stdout.write(f"\r[{time.strftime('%H:%M:%S')}] {ok}/{len(files)} · "
                             f"{human(sent[0])}/{human(total_bytes)} · "
                             f"瞬时 {inst / 1048576:.1f} MB/s 平均 {avg / 1048576:.1f} · "
                             f"剩约 {eta_s} · 进行中 {len(inflight)}(最久 {oldest:.0f}s)   "[:118])
            sys.stdout.flush()
            stop_status.wait(2)

    def do_batch(bi, sub, chunk):
        nonlocal ok, fail
        size = sum(f.stat().st_size for f in chunk)
        inflight[bi] = time.time()
        try:
            target = f"{args.remote}/{sub}" if sub != "." else args.remote
            r = ssh_cmd(args, f"mkdir -p {shlex.quote(target)}")
            if r.returncode != 0:
                with lock:
                    fail += len(chunk)
                emit(f"  ✖ 批{bi} 建目录失败: {r.stderr.strip()[:110]}")
                return
            cmd = (["scp", "-P", str(args.port), "-q", "-o", "Compression=no",
                    "-c", args.cipher] + SSH_OPTS +
                   [str(f) for f in chunk] + [f"{args.user}@{args.host}:{target}/"])
            r = sh(cmd, timeout=args.timeout)
            if r.returncode != 0:
                with lock:
                    fail += len(chunk)
                emit(f"  ✖ 批{bi} 上传失败({r.returncode}): {r.stderr.strip()[:110]}")
                return
            got = remote_sizes(args, sub, [f.name for f in chunk])
            good, good_bytes = [], 0
            for f in chunk:
                if got.get(f.name) == f.stat().st_size:
                    good.append(f)
                    good_bytes += f.stat().st_size
                else:
                    with lock:
                        fail += 1
                    emit(f"  ! 校验不一致，保留本地: {rel[f][:70]}")
            with lock:
                with record.open("a", encoding="utf-8") as fh:
                    for f in good:
                        fh.write(rel[f] + "\n")
                ok += len(good)
                sent[0] += good_bytes
                recent.append((time.time(), good_bytes))
            if args.delete:
                for f in good:
                    try:
                        f.unlink()
                    except OSError as e:
                        emit(f"  ! 本地删除失败: {f.name[:50]} {e}")
        finally:
            inflight.pop(bi, None)

    print(f"开始上传：{len(batches)} 批 × 每批 {args.batch} 个，{args.jobs} 个并行通道")
    th = threading.Thread(target=status_loop, daemon=True)
    th.start()
    try:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            list(ex.map(lambda p: do_batch(p[0] + 1, p[1][0], p[1][1]), enumerate(batches)))
    finally:
        stop_status.set()
        th.join(timeout=3)
        sys.stdout.write("\r" + " " * 110 + "\r")

    el = max(1e-6, time.time() - t0)
    print(f"本轮：成功 {ok}，失败/跳过 {fail}，用时 {el / 60:.1f} 分钟，"
          f"平均 {sent[0] / 1048576 / el:.1f} MB/s")
    return ok


def group_by_dir(files):
    out = {}
    for f in files:
        out.setdefault(f.parent.name if f.parent.name else ".", []).append(f)
    return out


def remote_sizes(args, sub, names):
    if not names:
        return {}
    target = f"{args.remote}/{sub}" if sub != "." else args.remote
    quoted = " ".join(shlex.quote(n) for n in names)
    r = ssh_cmd(args, f"cd {shlex.quote(target)} 2>/dev/null && stat -c '%s|%n' {quoted} 2>/dev/null")
    out = {}
    for line in r.stdout.splitlines():
        if "|" in line:
            sz, name = line.split("|", 1)
            try:
                out[name.strip()] = int(sz)
            except ValueError:
                pass
    return out


def main():
    cfg = core.load_config()
    srv = cfg.get("server", {})
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", default=str(core.ROOT / cfg.get("output_dir", "videos")))
    ap.add_argument("--record", default=str(core.ROOT / cfg.get("state_dir", "state") / "uploaded.txt"))
    ap.add_argument("--host", default=srv.get("host", ""))
    ap.add_argument("--port", type=int, default=int(srv.get("port", 22) or 22))
    ap.add_argument("--user", default=srv.get("user", ""))
    ap.add_argument("--remote", default=srv.get("remote", "~/datasets/hakka"))
    ap.add_argument("--batch", type=int, default=6)
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--cipher", default="aes128-gcm@openssh.com")
    ap.add_argument("--delete", action="store_true", help="校验通过后删除本地")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--open-perm", action="store_true", help="上传后把数据集设为所有人可读")
    ap.add_argument("--watch", type=int, default=0, metavar="秒",
                    help="循环监视：每隔 N 秒扫一次，新下好的自动上传")
    args = ap.parse_args()
    if not args.host or not args.user:
        raise SystemExit("请先在 config.json 的 server 段填好 host / user / remote")

    def once():
        n = one_pass(args)
        if args.open_perm and n:
            ssh_cmd(args, f"chmod o+x $HOME && chmod -R o+rX $(eval echo {args.remote})")
        return n

    if not args.watch:
        once()
        return
    print(f"循环监视：每 {args.watch} 秒检查一次，Ctrl+C 退出\n")
    idle = 0
    while True:
        try:
            n = once()
        except KeyboardInterrupt:
            print("\n已退出监视。")
            return
        except Exception as e:
            print("本轮出错:", e)
            n = 0
        idle = idle + 1 if n == 0 else 0
        print(f"[{time.strftime('%H:%M:%S')}] 本轮 {n} 个"
              f"{'，连续 %d 轮无新文件' % idle if idle else ''}，{args.watch} 秒后再查 …\n")
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n已退出监视。")
            return


if __name__ == "__main__":
    main()
