# 多站点方言语料视频采集

按需求表（Excel）批量下载 YouTube / 哔哩哔哩 / 抖音 上的方言视频，
支持断点续传、被限流时轮循重试、按来源分目录归档，以及传到实验室服务器。

## 快速开始

```bash
git clone <仓库地址>
cd video-collector
pip install -U -r requirements.txt
cp config.example.json config.json      # Windows: copy config.example.json config.json
```

再装两个外部程序（Windows 用 winget，其他系统用各自的包管理器）：

```powershell
winget install DenoLand.Deno    # YouTube 必需：yt-dlp 要靠它解 n 参数
winget install Gyan.FFmpeg      # 合并高画质音视频、转音频格式
```

装完**新开一个终端**（要让 PATH 生效），然后双击 `下载-界面.bat`，或者：

```bash
python src/cli.py list        # 先展开需求表，看看每条有多少个视频
python src/cli.py download    # 开始下载
```

## 配置 config.json

`sites` 按站点分别配置，`default` 是兜底，各站点的同名项覆盖它。

| 项 | 说明 |
|---|---|
| `proxy` | 代理地址。**YouTube 要填，B站和抖音必须留空**，国内站走境外代理又慢又容易被风控 |
| `cookies_from_browser` | `firefox` / `chrome` / `edge`，借用浏览器登录态。新版 Chrome/Edge 的 Cookie 加密常读不出来，优先用 firefox |
| `cookies_file` | cookies.txt 路径，比上一项可靠。B站要 1080P+ 需要登录 |
| `workers` | 并行下载数。**注意 yt-dlp 每个视频内部还会开 `frags` 条分片连接**，实际并发 = workers × frags |
| `sleep` | 每个视频之间的间隔秒数，防限流 |
| `rotate_clients` | 仅 YouTube：报错时轮换播放器客户端（默认→tv→web_safari） |
| `throttle_kb` | 速度低于这个值视为卡死并重连，0 关闭 |

`server` 段填服务器地址，给 `uploader.py` 和 `verify.py` 用。

### 并发怎么定

实测数据（YouTube，家用宽带 + 代理）：

| 设置 | 实际完成 | 结果 |
|---|---|---|
| 8 并行 × 4 分片 = 32 连接 | 约 260 个/小时 | 约 450 个后触发机器人验证 |
| 3 并行 × 4 分片 = 12 连接 | 约 400 个/小时 | 连跑 5 小时无验证 |

并行开大反而更慢（带宽被切碎），而且触发风控的是**并发连接数**不是请求频率。
**12 条总连接是验证过的安全线**，想开 4 并行就把分片降到 3。配好 Cookie 后可以更激进。

## 需求表格式

`sources/*.xlsx` 第一列标题、第二列链接（顺序无所谓，脚本自动找带 http 的那列）。
支持播放列表、频道页、单个视频；抖音分享链接里的文案残留会自动清掉。

## 日常用法

```bash
python src/cli.py download --site bilibili      # 只下某个站点
python src/cli.py download --only 1,5,12        # 只下需求表第 1/5/12 行
python src/cli.py download --round-gap 20       # 被拒后每轮等 20 分钟
python src/cli.py status                        # 看进度
python src/uploader.py --delete --watch 300     # 边下边传，传完删本地
python src/verify.py                            # 核对服务器有没有遗漏
```

## 断点续传

- **视频级**：完成的记在 `state/done.txt`，重复运行自动跳过
- **文件级**：中断留下 `.part`，下次从断点字节继续
- **永久失效**：私有/已删/会员专享记在 `state/skipped.txt`，不再浪费时间重试
- **被限流**：不等待，标记后先下别的，整轮结束再回头重试（可设轮次间隔）

从旧版单播放列表脚本迁移进度：

```bash
python src/migrate_old_state.py "旧目录/videos/done.txt"
```

## 各站点已知问题

- **YouTube**：没装 deno 会报 `n challenge solving failed`，然后连锁出现
  `The page needs to be reloaded`、`Requested format is not available`——根因都是缺 deno
- **B站**：多 P 视频会展开成多条；1080P+ 需要登录 Cookie
- **抖音**：yt-dlp 支持随风控变动，可能失败，建议配 Cookie；只有几条的话手动存更快

## 目录结构

```
video-collector/
├── src/
│   ├── core.py       站点识别、按站点配置、列表展开、错误分类
│   ├── engine.py     下载引擎（命令行与界面共用）
│   ├── sources.py    需求表读取
│   ├── cli.py        命令行入口
│   ├── gui.py        图形界面
│   ├── uploader.py   分批上传 + 校验 + 删本地
│   ├── verify.py     三方核对，找遗漏
│   └── migrate_old_state.py
├── sources/          需求表
├── state/            进度账本（gitignore，需要时手动备份）
├── videos/           下载结果（gitignore）
└── config.json       本机配置（gitignore，从 config.example.json 复制）
```
