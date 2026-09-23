# 操作与故障恢复

> 本文保留原 KR 采集链的操作细节。当前国际平台选择与内置国服命令以 [README.md](README.md) 为准。

## 一次性安装

在 Windows PowerShell 中进入项目目录：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
notepad .env
```

只在 `.env` 中填写自己的值。至少需要：

```dotenv
RIOT_API_KEY=YOUR_RIOT_API_KEY
```

不要在命令行、工单、截图、日志或文档中粘贴真实 API Key。不要提交 `.env`。

## 日常运行手册

1. 如 Development Key 已失效，更新 `.env` 中的 `RIOT_API_KEY`；
2. 人工启动 League Client，用自己的合法 Riot 账号登录；账号不必是 KR；
3. 保持 League Client 运行；
4. 在项目目录运行 probe；
5. 运行目标采集；
6. 查看状态。

```powershell
.\collector.cmd probe
.\collector.cmd run --target 100
.\collector.cmd status
```

判断 probe 成功的核心行是：

```text
REPLAY_ACQUISITION: PASS
KR_ACCOUNT_REQUIRED: NO
```

Route A 显示 `FAIL` 并不妨碍正常运行；当前正式下载链是 Route B。

机器可读输出把全局 `--json` 放在子命令前：

```powershell
.\collector.cmd --json probe
.\collector.cmd --json status
```

## 配置参考

环境变量优先于 `.env`，相对路径相对于 `--project-root` 解析。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `RIOT_API_KEY` | 无 | 必填 Riot API Key |
| `LEAGUE_INSTALL_DIR` | `C:\Riot Games\League of Legends` | League 安装目录，必须能找到当前 lockfile 和 logs |
| `COLLECTOR_DATA_DIR` | `data` | Replay、manifest、reports 根目录 |
| `COLLECTOR_DB_PATH` | `data/collector.sqlite3` | SQLite 路径 |
| `COLLECTOR_LOG_DIR` | `logs` | collector 日志目录 |
| `REPLAY_EDGE_BASE_URL` | 自动发现 | 高级覆盖；通常不填写，后端会随 Riot 环境变化 |
| `COLLECTOR_REQUEST_TIMEOUT` | `30` | 常规请求超时秒数 |
| `RIOT_API_MIN_INTERVAL` | `1.25` | Riot API 请求最小间隔秒数 |
| `RIOT_API_MAX_RETRIES` | `6` | 网络、429、5xx 最大重试次数 |
| `MATCH_HISTORY_COUNT` | `20` | 每页每名玩家的 Match ID 数，限制 1–100 |

如果 League 安装在别处，可写：

```dotenv
LEAGUE_INSTALL_DIR=D:\Riot Games\League of Legends
```

不要复制 lockfile 内容到 `.env`。LCU 凭据会随客户端会话变化，程序只在运行时读取。

## 状态与完整性审计

快速状态：

```powershell
.\collector.cmd status
```

完整文件审计：

```powershell
.\collector.cmd status --verify-files
```

完整审计会读取每个 manifest Replay、重新计算 SHA-256，检查记录大小、`RIOT` magic、exact version、尾部 metadata JSON 与 chunk 布局。输出 `integrity audit: PASS` 才表示所有 manifest 资产通过当前 collector 检查。此命令不等同于 exact-build packet 语义验证。

`status` 中三个容易混淆的计数：

- `raw match discoveries`：含重复观察；
- `discovery edges`：唯一玩家 × 比赛边；
- `unique matches`：唯一 Match ID。

最终目标只看 `verified`。

当前实测基线（2026-09-03）：KR patch `16.17` 为 `108 VERIFIED`，总大小 `1,509,715,320` bytes；对全部 108 个 manifest 文件执行本节完整审计的结果为 `integrity audit: PASS`，`integrity_errors` 为空。V1 最低交付目标是 100 场，额外 8 场按 preservation-first 保留。

## 中断与恢复

正常处理中按 Ctrl+C 后会输出 `INTERRUPTED`。直接重跑相同总目标：

```powershell
.\collector.cmd run --target 100
```

恢复逻辑会复用数据库和最终文件。新 `run` 先把同一 dataset 遗留的 `RUNNING` run 标成 `INTERRUPTED`；容量规划把 `DOWNLOADING` 和 `DOWNLOADED` 与其他可恢复状态一起计入 backlog。Reconcile 随后把遗留 `DOWNLOADING` 变成可重试，并接管已经完整的 `.partial` 或 `.rofl`。它不会从零开始，也不会把 target 当成本次增量。

若恢复过程中发现未完成 job 已有同名 final、但该文件未通过 RIOT ReplayV2 校验，程序不会覆盖或删除它。程序会记录 `replay_reconcile` 错误及保留路径，把 job 标为 `FAILED_PERMANENT`，继续处理其他任务。之后应先备份该文件，再根据 `collector status` 的 latest errors 人工调查。

不要为“清理状态”删除以下内容：

- `data/collector.sqlite3`、`-wal`、`-shm`；
- 任何 `.rofl`；
- 任何 `.rofl.partial`；
- 旧 patch 目录。

数据库丢失后，磁盘 Replay 不能仅凭文件名完整恢复全部研究元数据；应从备份恢复整个 `data` 目录。

## 常见故障矩阵

| 输出/现象 | 原因 | 操作 |
| --- | --- | --- |
| `API_KEY_MISSING` | `.env` 不存在或 Key 为空 | 复制 `.env.example` 为 `.env` 并填写 Key |
| `API_KEY_INVALID` | HTTP 401 | 换用当前有效 Key，再重跑 |
| `API_FORBIDDEN` | HTTP 403，Key 被禁止或 Development Key 可能失效 | 在 Developer Portal 刷新 Key并更新 `.env` |
| `RATE_LIMITED` | HTTP 429 且重试预算耗尽 | 保留数据，稍后重跑；不要并发启动第二个 collector |
| `PATCH_RESOLUTION_FAILED` | 无法读取 KR realm | 检查网络，稍后重跑 |
| `LEAGUE_CLIENT_CLOSED` | 没找到 lockfile | 启动并登录 League Client |
| `LCU_LOGIN_REQUIRED` | 客户端尚未取得完整会话 | 完成登录，停留在主界面后重跑 |
| `LCU_UNREACHABLE` | 客户端退出、重启中或本地连接中断 | 等客户端稳定后重跑 |
| `REPLAY_EDGE_UNDISCOVERED` | 当前日志中没有后端地址 | 登录后让客户端完成初始化，再 probe；必要时才使用高级 URL 覆盖 |
| `REPLAY_AUTH_FAILED` | 当前 LCU session 未被后端接受 | 重新登录客户端，再 probe；不要尝试绕过认证 |
| `REPLAY_UNAVAILABLE` | 某场 Replay 已不存在，HTTP 404 | 该 job 记为 `UNAVAILABLE`；程序继续其他候选 |
| `REPLAY_NETWORK_ERROR` / `REPLAY_SERVER_ERROR` / `REPLAY_RATE_LIMITED` | 网络、5xx 或 Replay HTTP 429 | 同次下载最多自动尝试 4 次；429 等待取 `Retry-After` 与指数退避+jitter 的较大值；仍失败才记为 `FAILED_RETRYABLE` |
| `REPLAY_TRUNCATED` / `ROFL_CHUNK_TRUNCATED` | 下载中断或容器不完整 | 保留 partial，重跑同一目标 |
| `ROFL_HTTP_GZIP_WRAPPER` | `.rofl` 仍是早期误存的 HTTP gzip wire bytes | 仅对本次遗留 dataset 使用下述一次性 normalizer |
| `ROFL_MALFORMED` / `ROFL_METADATA_INVALID` | 不是合法 RIOT ReplayV2 或 metadata 损坏 | 保留原文件，先备份并调查，不要覆盖 |
| 既有 final 在恢复接管时损坏 | 未完成 job 的目标路径已有无效文件 | 文件原地保留、错误入库、job 记为 `FAILED_PERMANENT`；其他任务继续 |
| `TARGET_NOT_REACHED` | 当前候选池不足 | 已有 Replay 安全；稍后同目标重跑以扩大/刷新候选 |
| `COLLECTOR_ALREADY_RUNNING: Another collector process is already running` | 同一数据目录已有 run；这是正常的 `ConfigurationError`，不会输出 traceback | 等另一个进程结束，不要删除锁文件来强开并发 |

## Replay 路线排障

正常情况下不要填写 `REPLAY_EDGE_BASE_URL`。程序会从当前 League Client logs 中发现 player-platform backend；这是比长期硬编码主机更稳妥的方式。

若自动发现持续失败：

1. 确认 `LEAGUE_INSTALL_DIR` 指向真实 League 安装根目录；
2. 确认其下存在 `Logs\LeagueClient Logs`；
3. 重新登录并运行 `probe`；
4. 仅在已经合法、独立确认当前 backend 时，才把 base URL 写入本机 `.env`；
5. 不要把这个 URL 连同 token、请求头或 lockfile 上传。

Route A 的 LCU POST 返回 204 只代表本地命令被接受，不证明跨区 Replay 下载成功。以 `REPLAY_ACQUISITION` 和 Route B 的内容校验为准。

## 一次性 legacy HTTP gzip normalizer

以下命令不是正常使用步骤，也不是每个 patch 都要运行：

```powershell
.\collector.cmd normalize-http-gzip
```

它只用于本次早期 collector 曾把 HTTP `Content-Encoding: gzip` 的 wire bytes 直接当作 `.rofl` 保存的遗留数据。当前下载器已透明解码 HTTP body，并直接落盘以 `RIOT` 开头的 ReplayV2；对新数据不要运行 normalizer。

命令会锁定数据目录并处理最新 KR dataset 的 manifest：

- `RIOT` 文件只验证并计入 `ALREADY_RAW`；
- gzip wrapper 先完整解码到临时文件；
- 解码文件必须通过 ReplayV2 布局检查且内嵌 build 与数据库相同；
- 原 wrapper 在替换前复制到 `data/KR/<patch>/quarantine/http-gzip/<match>.rofl.gz`，并核对 hash；
- 解码后的 RIOT 文件原子替换工作副本，再更新数据库和 manifest；
- 若 backup 已存在但与源文件不同，命令拒绝继续，不覆盖 backup。

正常输出示例字段是 `NORMALIZED`、`ALREADY_RAW` 和 `MANIFEST`。Quarantine backup 应与整个 `data` 一起备份，不要清理；它不是可直接交给 Replay parser 的正式 `.rofl`。

## Parser portability 结果

现有 parser 已实际读取 `data/KR/16.17/builds/16.17.810.4348/rofl/` 中的 20 个原始 RIOT 文件。20/20 的容器、Zstd 和 block framing 通过，每场 `block_error_count` 均为 0，汇总 `block_framing_errors` 也是 0。

但 exact build `16.17.810.4348` 没有对应语义 decoder profile；报告状态为 `UNSUPPORTED_REPLAY_VERSION`，英雄死亡、伤害、施法、位置等 packet 语义不能宣称已验证。因此运维结论是：

```text
KR_PARSER_PORTABILITY: PARTIAL
reason: UNSUPPORTED_REPLAY_VERSION
```

报告路径：`data/KR/16.17/reports/parser_portability_20/acceptance_summary.json`。不要把 `PARTIAL` 写成 `PASS`，也不要从这 20 个 parser 输入推导或声称最终采集 target 已完成。

## 备份与迁移

推荐在 collector 未运行时备份整个 `data` 目录，而不是只复制 `.rofl`：

```text
data/
  collector.sqlite3
  collector.sqlite3-wal   （存在时一并保留）
  collector.sqlite3-shm   （存在时一并保留）
  KR/
```

最简单的安全做法是关闭 collector 后，通过文件资源管理器复制整个 `data` 目录。迁移到另一磁盘时可把完整目录复制过去，再用 `COLLECTOR_DATA_DIR` 和 `COLLECTOR_DB_PATH` 指向新位置。

不要让 `COLLECTOR_DATA_DIR` 与 `COLLECTOR_DB_PATH` 指向彼此不对应的旧/新副本。数据库中的 Replay 路径相对于 data root；保持 `KR/...` 内部结构不变。

## Preservation-first 规则

- 没有自动 cleanup 命令；
- 新 patch 不删除旧 patch；
- target 变小不删除多出的 Replay；
- 重建 manifest 不删除资产；
- schema 初始化使用 `CREATE TABLE IF NOT EXISTS`，不会清表；
- 最终文件冲突时拒绝覆盖；
- 对损坏或来源不明文件应先隔离并备份，再人工调查，不要直接删除。

任何未来 migration、repair 或清理操作都应先做完整备份，并以保留 `.rofl` 为最高优先级。

## 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | 命令成功；probe 的 Replay acquisition 已 PASS |
| `1` | 一般 collector/API 错误 |
| `2` | 缺少 API Key、需要客户端登录或客户端关闭 |
| `3` | Replay probe 未 PASS，或 Replay 类错误 |
| `130` | 用户中断；状态和已完成资产已保留 |
