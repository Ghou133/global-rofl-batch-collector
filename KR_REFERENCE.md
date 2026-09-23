# KR Current-Patch High-Elo ROFL Batch Collector

这是一个一次性批量采集工具：从 Riot 官方 API 发现韩国服务器（KR）当前版本的高分段单双排比赛，按比赛中的 Challenger / Grandmaster / Master 玩家数量排序，并通过已验证的 Riot Replay 后端下载、校验和保存官方 `.rofl` 文件。

它不是常驻监控服务，不需要 KR 游戏账号，也不会自动登录、重启或关闭 League Client。你只需在需要新版本样本时手动运行一次。

当前已用一个非 KR（JP1）合法登录会话实际验证：显式请求 KR 比赛身份时，Replay 后端可以返回完整 Replay。因此当前结论是 `KR_ACCOUNT_REQUIRED: NO`。验证范围和证据见 [REPLAY_ACQUISITION_FINDINGS.md](REPLAY_ACQUISITION_FINDINGS.md)。

当前恢复后的 KR `16.17` 数据集已有 `108 VERIFIED`，并已通过对全部 108 个文件的 `status --verify-files` 完整性审计。V1 最低交付目标仍是 100 场；108 是实际保留下来的结果，不会为了把数字缩回 100 而删除多出的 8 场。

## 首次准备

需要：

- Windows 电脑；
- Python 3.11 或更高版本；
- 已安装的 League of Legends Client；
- 一个可登录 League Client 的 Riot 账号，账号不必属于 KR；
- Riot Development API Key 或 Personal API Key。

在 PowerShell 中进入本项目目录，然后只做一次安装：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
notepad .env
```

在打开的 `.env` 中，把自己的 Riot API Key 填在等号后面：

```dotenv
RIOT_API_KEY=YOUR_RIOT_API_KEY
```

不要把真实 Key 发给别人，也不要放进截图、文档或 Git。项目已经通过 `.gitignore` 排除 `.env`、数据库、Replay 和运行数据。

API Key 可从 [Riot Developer Portal](https://developer.riotgames.com/) 获取。Development Key 会失效；失效时只需更新 `.env`，不需要改代码或重装项目。

## 每次正常使用

先人工打开 League Client 并完成登录。停留在客户端主界面即可，不要退出客户端。然后在本项目目录依次运行：

```powershell
.\collector.cmd probe
.\collector.cmd run --target 100
.\collector.cmd status
```

这就是正常使用所需的最少命令。

`probe` 检查 Riot API、KR 排行榜、ASIA Match-V5、当前版本、League Client、LCU、Replay 路线、数据库和存储。由于内置的跨区 LCU 路线可能失败，只要 `REPLAY_ACQUISITION: PASS` 且 Route B 为 `PASS`，Replay 获取能力就是可用的。

`run --target 100` 自动执行发现、去重、当前版本过滤、质量排序、下载、完整性校验和 manifest 更新，达到目标后退出。

批量发现和下载可能持续较长时间；运行期间请保持 League Client 已登录、终端窗口开启，并确保数据磁盘有足够空间。

`status` 只读取持久化结果并显示当前数据集状态。需要重新读取每一个 `.rofl`、计算哈希，并检查 `RIOT` ReplayV2 文件头、版本、尾部 metadata 与 chunk 布局时使用：

```powershell
.\collector.cmd status --verify-files
```

这个完整审计可能需要较长时间，但不会修改或删除 Replay。

> 命令必须从项目目录运行，因为 `.env` 和默认 `data` 路径相对于当前项目目录解析。若必须在别处运行，把 `--project-root` 放在子命令之前。

```powershell
C:\path\to\collector.cmd --project-root C:\path\to\kr-rofl-batch-collector status
```

## `--target` 的准确含义

`--target` 是“当前 KR 版本数据集中最终应有多少个已验证 Replay”，不是“本次再下载多少个”。以下只是语义示例，不代表当前数据集已经达到这些数量：

1. `run --target 100` 表示希望该 patch 最终有 100 场；
2. 以后改为 `run --target 300` 时，程序用 300 减去数据库中当时的实际 `VERIFIED` 数，只下载差额；
3. 如果实际 `VERIFIED` 已经不少于指定 target，程序不会重复下载。

计数只认数据库状态为 `VERIFIED` 的 Replay。新版本出现后，程序自动建立新版本数据集并从该版本自己的目标数开始；旧版本目录和文件保留。

## 数据保存在哪里

默认数据都在项目内的 `data` 目录：

```text
data/
  collector.sqlite3
  KR/
    <patch>/
      builds/
        <完整 gameVersion>/
          rofl/
            KR_<gameId>.rofl
      manifests/
        dataset_manifest.jsonl
      reports/
        run_<id>.json
        parser_portability_20/
      quarantine/
        http-gzip/
          KR_<gameId>.rofl.gz
```

- 数据库：`data/collector.sqlite3`
- Replay：`data/KR/<patch>/builds/<完整版本>/rofl/`
- Manifest：`data/KR/<patch>/manifests/dataset_manifest.jsonl`
- 运行报告：`data/KR/<patch>/reports/`
- 日志目录：`logs/`

路径中的 `<patch>` 是研究版本，例如 `16.17`；`<完整 gameVersion>` 会保留类似 `16.17.810.4348` 的完整 build 区别。下游程序应读取 manifest，不要靠扫描文件夹猜测版本或元数据。

正常 `.rofl` 必须是以 `RIOT` 开头的官方 ReplayV2 文件。Replay 后端虽然使用 HTTP `Content-Encoding: gzip` 传输，但 collector 会像 League Client 一样透明解码，绝不会把传输层 gzip wrapper 当成 `.rofl` 容器。`quarantine/http-gzip/` 只保存本次遗留格式迁移前的 wire bytes，供审计和回滚，不计入数据集。

## 数据保护原则

本项目默认 `PRESERVATION_FIRST`：

- 把 HTTP gzip 传输透明解码到 `.rofl.partial`，确认文件以 `RIOT` 开头且 ReplayV2 布局、metadata 和 chunk 区域有效后，才原子改名为 `.rofl`；
- 已存在的最终 `.rofl` 不会被覆盖；
- 重启时会验证并接管已有完整文件，不会重新下载；
- 新 `run` 会把同一 dataset 上次残留的 `RUNNING` 记录明确标成 `INTERRUPTED`，并把 `DOWNLOADING` / `DOWNLOADED` 纳入可恢复 backlog；
- 恢复时若未完成 job 已有同名 final 但文件损坏，程序保留原文件、记录错误、将 job 标成 `FAILED_PERMANENT`，然后继续其他任务，不会覆盖它；
- 新 patch、数据库迁移、重跑和 manifest 重建都不会主动删除旧 Replay；
- manifest 和运行报告通过临时文件原子替换；
- SQLite 使用 WAL、完整同步和事务保存状态。

如果运行中断，直接重新执行同一条 `run --target ...` 命令。不要手工删除 `.partial`、`.rofl` 或数据库。备份时最好先停止 collector，再把整个 `data` 目录复制到其他磁盘。

## 本次遗留 gzip 迁移命令

`normalize-http-gzip` 不是日常命令，只用于修复本次早期运行中误把 HTTP gzip wire bytes 直接保存为 `.rofl` 的遗留文件。当前 downloader 已经直接保存解码后的 `RIOT` 文件，新数据不需要执行它。

迁移已采用 preservation-first 方式：先把原始 wrapper 完整保存在 `data/KR/<patch>/quarantine/http-gzip/`，验证解码后的 build 和 ReplayV2 结构，再替换工作副本并重写 manifest。除非正在处理同一批遗留文件，否则不要运行：

```powershell
.\collector.cmd normalize-http-gzip
```

## KR parser portability

现有 Replay parser 已对 20 个 build `16.17.810.4348` 的原始 `RIOT` 文件完成实测：20/20 容器、Zstd 解压和 block framing 通过，每个文件都是 0 framing errors。由于 parser 没有这个 exact build 的语义 decoder profile，packet 语义没有解码，结论是：

```text
KR_PARSER_PORTABILITY: PARTIAL
reason: UNSUPPORTED_REPLAY_VERSION
```

这不是容器损坏；它表示二进制容器可读，但英雄死亡、伤害、施法、位置等语义事件仍需增加与 `16.17.810.4348` 精确匹配的 profile。本文不据此声称最终批量目标已经完成。

## 常见提示

- `API_KEY_MISSING`：在项目根目录创建 `.env` 并填写 `RIOT_API_KEY`。
- `API_KEY_INVALID` 或 `API_FORBIDDEN`：Key 被拒绝；Development Key 可能已经失效。更新 `.env` 后重试。
- `ACTION_REQUIRED: LOGIN_TO_LEAGUE_CLIENT` 或 `LEAGUE_CLIENT_CLOSED`：人工启动 League Client 并完成登录，然后重试。程序不会替你输入账号密码。
- `REPLAY_EDGE_UNDISCOVERED`：保持 League Client 已登录并在主界面活动后重试。程序通常从当前 League Client 日志识别 Replay 后端；高级排障见 [OPERATIONS.md](OPERATIONS.md)。
- `TARGET_NOT_REACHED`：本轮候选池已耗尽，但已验证资产仍然保留。这是可恢复错误；稍后用相同目标重跑即可继续发现和补齐。
- `INTERRUPTED`：用户中断是安全的。重新运行同一命令会从数据库和已有文件继续。
- `REPLAY_RATE_LIMITED`：Replay 后端返回 429；自动重试会遵守 `Retry-After`，实际等待取它与指数退避加 jitter 中较大的值。

## 更多文档

- [ARCHITECTURE.md](ARCHITECTURE.md)：组件、数据流、排序和恢复设计；
- [DATASET_SCHEMA.md](DATASET_SCHEMA.md)：SQLite 表、状态和 manifest 字段；
- [OPERATIONS.md](OPERATIONS.md)：安装、配置、故障恢复、备份和审计；
- [REPLAY_ACQUISITION_FINDINGS.md](REPLAY_ACQUISITION_FINDINGS.md)：无 KR 账号 Replay 获取的实测证据。
