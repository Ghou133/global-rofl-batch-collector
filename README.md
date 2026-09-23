# Global ROFL Batch Collector

[![CI](https://github.com/Ghou133/global-rofl-batch-collector/actions/workflows/ci.yml/badge.svg)](https://github.com/Ghou133/global-rofl-batch-collector/actions/workflows/ci.yml)

面向《英雄联盟》国际服与腾讯国服的本地 Replay 采集器。项目的目的是建立可恢复、可核验、保留来源信息的 `.rofl` 数据集，供后续研究或解析使用。它不提供 Replay 语义解码，也不随仓库发布比赛数据或账号凭据。

项目由两条独立采集链组成：

| 采集链 | 范围 | 输入与产出 | 本地存储 |
| --- | --- | --- | --- |
| Riot 国际服 | 17 个可选平台，默认 `KR` | Riot 官方 API 发现当前版本高分段单双排比赛；通过已登录的 League Client 获取并验证 Replay | `data/collector.sqlite3` 和 `data/<platform>/<patch>/` |
| 腾讯国服 | 当前支持 `HN1` 会话 | 按 game ID 或本机 Replay 文件采集，并配对同局的新鲜 SUMMARY、DETAILS | `data/CN/replay-paired/` |

两条链的比赛身份、认证、SQLite schema 与文件路径彼此独立。`data/`、日志和 `.env` 都不进入 Git。项目与 Riot Games、腾讯无隶属关系；使用者需遵守相关服务条款及数据使用规则。

## 当前进度

- **已实现并通过自动化测试：**统一 `collector` 入口、17 个国际服平台的路由与数据分区、Riot API 发现和回放容器校验、腾讯 `HN1` Replay 与 SUMMARY/DETAILS 配对、持久化与中断恢复。根项目本地测试 94 项通过，迁入国服源码的原测试 62 项通过，Ruff 检查通过；首次 GitHub Windows CI 的安装、Ruff 和测试也通过。
- **历史真实采集证据：**原 KR 链在 patch `16.17` 保存 108 个 `VERIFIED` Replay，并对这 108 个文件通过完整性审计。这是该次 KR 数据集的历史结果，不代表当前 patch 或其他平台已通过真实客户端采集。
- **尚未完成：**17 个国际平台的逐区真实客户端验收、腾讯国服 `HN1` 以外平台的端点与实测、Replay exact-build 语义解码、跨平台统一数据模型和公开数据集发布。详见 [开发路线与进度](ROADMAP.md)。

## 安装

目前以 **Windows、Python 3.11+、已安装并登录的 League Client** 为运行环境。打开 PowerShell，在项目根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

国际服需要把自己的 `RIOT_API_KEY` 写入 `.env`；可从 [Riot Developer Portal](https://developer.riotgames.com/) 获取。也可通过同名环境变量提供。国服 `cn replay` 不使用此 Key，但要求本人已登录的腾讯 League Client 会话。不要提交 `.env`、客户端 lockfile、token、数据库或 Replay。

```powershell
.\collector.cmd --help
.\collector.cmd cn --help
.\collector.cmd cn replay --help
```

安装后也可在已激活的虚拟环境中直接运行 `collector`。`collector.cmd` 使用项目的 `.venv`。

## Riot 国际服用法

先启动并登录 League Client，再检查当前平台的 API、版本与 Replay 路由。`KR` 是兼容原项目的默认值：

```powershell
.\collector.cmd probe
.\collector.cmd run --target 100
.\collector.cmd status --verify-files
```

选择其他平台时，**在子命令前**传 `--platform`：

```powershell
.\collector.cmd --platform EUW1 probe
.\collector.cmd --platform EUW1 run --target 100
.\collector.cmd --platform EUW1 status --verify-files
```

可选平台：`KR`、`JP1`、`NA1`、`BR1`、`LA1`、`LA2`、`EUW1`、`EUN1`、`TR1`、`RU`、`ME1`、`OC1`、`PH2`、`SG2`、`TH2`、`TW2`、`VN2`。平台自动映射到对应 Match-V5 区域路由。可在 `.env` 设置 `COLLECTOR_PLATFORM` 作为默认平台；命令行参数优先。

`--target 100` 指该平台当前 patch 的**累计已验证数量**，不是本次新增 100 场。只有 `probe` 的回放能力检查通过，`run` 才会批量下载。`VERIFIED` 表示文件通过本项目的 ReplayV2 容器布局、版本、metadata、chunk 边界及 SHA-256 检查，不能推断 packet 语义正确或 Replay 一定能在每个客户端版本播放。`--json` 可放在子命令前输出机器可读结果。

客户端会话和 Riot 后端会变化。跨平台下载可用性需在目标平台、目标 patch 的当前环境用 `probe` 和实际采集确认。

## 腾讯国服用法

目前国服采集链针对腾讯 `HN1` 客户端会话。直接下载指定比赛：

```powershell
.\collector.cmd cn replay --game-id <game-id>
.\collector.cmd cn replay --status
```

也可以监视 League Client 已下载的本机 Replay；首次运行默认先建立现有文件基线，之后只处理新文件：

```powershell
.\collector.cmd cn replay
.\collector.cmd cn replay --watch
```

`--game-id` 和 `--source-dir` 可重复指定。`--limit 10` 限制单次处理数；`--include-existing` 只适合在**全新数据根**受控导入旧文件。无 `--game-id` 时，命令会扫描文件并重试已排队的远程/本机候选，**不会自动从排行榜发现新比赛**。完整国服采集器还提供 `collector cn probe`、`collect`、`run`、`status` 和 `audit`；其默认一般采集根为 `data/CN/`，与 `cn replay` 的配对归档根不同。

国服 Replay 只有在文件、game ID、patch、同局 SUMMARY/DETAILS 及 hash 均满足校验后才记为 `VALIDATED`。失败、不可用、排队和中断状态会保留以供复查；`--status` 仅查询，不下载。

## 数据、安全与维护

采集记录和 Replay 保存在本机 `data/`，项目不会自动清理旧 patch，也不会覆盖已验证的不同 Replay。备份前停止 collector，然后复制整个 `data/`，包括 SQLite 数据库及可能存在的 WAL/SHM 文件。公开数据前应另外审查比赛与玩家标识；源码仓库不包含数据集。

维护、配置和故障恢复见 [操作手册](OPERATIONS.md)，数据库字段见 [数据集 Schema](DATASET_SCHEMA.md)，模块关系见 [代码结构与架构](ARCHITECTURE.md)。[KR 历史说明](KR_REFERENCE.md) 与 [回放获取实测记录](REPLAY_ACQUISITION_FINDINGS.md) 保留原 KR 证据，其时间和平台范围不能外推。迁入边界见 [集成决策](INTEGRATION_DECISION.md)。

## 开发与许可

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

贡献说明见 [CONTRIBUTING.md](CONTRIBUTING.md)，漏洞报告方式见 [SECURITY.md](SECURITY.md)。源码按 [GNU AGPLv3](LICENSE)（`AGPL-3.0-only`）发布。
