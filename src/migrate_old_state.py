# -*- coding: utf-8 -*-
"""
把旧版（单播放列表）脚本的进度迁移过来，避免已下好的 2794 个客家新闻重下。
旧 done.txt 里是裸的 YouTube 视频 ID，新格式是 "youtube:<id>"。

  python src/migrate_old_state.py "C:\\...\\闽语数据收集\\videos\\done.txt"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core  # noqa: E402


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    old = Path(sys.argv[1])
    site = sys.argv[2] if len(sys.argv) > 2 else "youtube"
    if not old.exists():
        raise SystemExit(f"找不到 {old}")
    cfg = core.load_config()
    new = core.ROOT / cfg.get("state_dir", "state") / "done.txt"
    cur = core.load_set(new)
    added = 0
    with new.open("a", encoding="utf-8") as f:
        for line in old.read_text(encoding="utf-8", errors="replace").splitlines():
            vid = line.strip()
            if not vid or ":" in vid:
                continue
            key = f"{site}:{vid}"
            if key not in cur:
                f.write(key + "\n")
                cur.add(key)
                added += 1
    print(f"已迁移 {added} 条到 {new}（原有 {len(cur) - added} 条）")
    print("这些视频不会被重新下载。")


if __name__ == "__main__":
    main()
