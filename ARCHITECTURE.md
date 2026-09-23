# 架构说明

## 当前代码结构

```text
collector.cmd                         Windows 启动脚本
pyproject.toml                        包元数据、依赖和测试/静态检查配置
src/
  global_rofl_collector/
    entrypoint.py                     统一命令分发
    cli.py, config.py, platforms.py   国际服命令、配置和平台路由
    riot.py, discovery.py, service.py 官方 API、比赛发现与运行编排
    replay.py, maintenance.py         Replay 获取、验证、恢复与旧数据迁移
    db.py, models.py, manifest.py     状态、数据模型和下游清单
    locking.py, errors.py             单进程锁和稳定错误码
  lol_collector/
    cli.py, collection_*.py           国服一般采集入口与流水线
    adapters.py, transport.py         本机客户端和腾讯 SGP 通信
    replay_capture.py, replay_store.py,
    replay_archive.py                 国服 Replay 与 SUMMARY/DETAILS 配对
    repository.py, schema.py,
    schema.sql, *_store.py            国服独立 SQLite 数据模型
    probe.py, security.py, audit.py   能力检查、凭据扫描与审计
  kr_rofl_collector/                  旧 Python 模块名兼容入口
tests/                                统一项目离线测试
.github/workflows/ci.yml             GitHub 离线检查
data/                                 运行产物；被 Git 忽略
```

统一入口按首个子命令分发：`collector cn ...` 进入 `lol_collector`，其他命令进入 `global_rofl_collector`。国际服由 `--platform` 选择明确的 League-V4、Match-V5 和 Data Dragon realm 路由；相同的 `data/collector.sqlite3` 中按 `(platform, patch)` 分 dataset，Replay 位于 `data/<platform>/<patch>/`。国服单独使用 `data/CN/` 和 `data/CN/replay-paired/`，不会把腾讯 game ID 当成 Riot Match-V5 ID，也不会读取国际服数据库。

边界与接口以 [README.md](README.md)、[DATASET_SCHEMA.md](DATASET_SCHEMA.md) 和实际源码为准。[ROADMAP.md](ROADMAP.md) 把代码能力与真实客户端验收分别列出。

## 原 KR 链历史设计细节

以下章节是迁移前 KR 链的设计及 patch `16.17` 证据快照。章节中的固定 `KR` / `ASIA` URL、样本数与 Replay 路由结论仅适用于当时的 KR 环境，不表示其他国际服平台已经真实采集成功。当前平台选择见 `src/global_rofl_collector/platforms.py`。

## 目标与边界

本项目是本地、按需运行的单进程批处理程序。目标是把 KR 当前版本、Ranked Solo/Duo（queue ID `420`）的高端局变成可恢复、可追溯的 `.rofl` 数据集。

它不提供 Web 服务，不做常驻监控、实时 spectator 录制、玩家画像、明星选手权重或机器学习排序。正式发现链只依赖 Riot 官方 API；OP.GG 不是依赖项。

## 组件

```text
collector CLI
  -> Config / .env
  -> CollectorService
       -> PatchResolver --------> KR Data Dragon realm
       -> RiotApi --------------> KR League-V4 + ASIA Match-V5
       -> DiscoveryService -----> 去重、过滤、质量计算
       -> Database -------------> SQLite durable state
       -> ReplayBackendAcquirer -> 本地 LCU + Riot player-platform backend
            -> DownloadManager ------> partial、验证、原子发布、恢复
            -> Manifest writer ------> JSONL 下游索引
            -> Legacy normalizer ----> 一次性 HTTP gzip 迁移与 quarantine
```

### CLI 与配置

`global_rofl_collector.cli` 提供 `probe`、`run --target N` 和 `status`。`Config` 从项目根目录的 `.env` 读取配置，并允许同名进程环境变量覆盖文件值。配置加载时不会打印 API Key 或 LCU 凭据。

### 当前版本解析

`PatchResolver` 读取官方 KR realm。它同时保存：

- `patch_key`：主版本与次版本，例如 `16.17`；
- `exact_realm_version`：realm 返回的完整版本；
- 每场比赛自己的 `game_version_exact`：来自 Match-V5，用于精确 build 路径。

因此 patch 用于数据集分区，完整 `gameVersion` 用于保留同 patch 内的 build 差异。

### Riot API 层

平台和 regional routing 固定为当前目标：

- League-V4：`https://kr.api.riotgames.com`；
- Match-V5：`https://asia.api.riotgames.com`；
- 排位队列：`RANKED_SOLO_5x5` / queue ID `420`。

请求调度包含：

- 默认每次请求至少间隔 1.25 秒；
- 观察 `X-App-Rate-Limit`、`X-Method-Rate-Limit` 及对应 count；
- HTTP 429 尊重 `Retry-After`；
- 网络错误和 HTTP 5xx 使用指数退避与 jitter；
- 401、403、404、schema 异常分别映射为稳定错误码。

密钥只放在 `X-Riot-Token` 请求头中，不进入数据库或报告。

## 发现流程

一次 `run` 的主要流程如下：

1. 解析当前 KR patch，取得或创建该 patch 的 dataset；
2. 若该 dataset 已有的 `VERIFIED` 数量达到 target，重建 manifest 后立即退出；
3. 刷新 Challenger、Grandmaster、Master 官方排行榜，以 PUUID 为稳定身份；
4. 按 Challenger → Grandmaster → Master 顺序查询近期 queue 420 Match ID；
5. 以 `(dataset, match_id, puuid)` 保存 discovery edge，以 `match_id` 得到 unique match；
6. 获取 Match-V5 明细并只把满足 KR、queue 420、当前 patch、已结束且时长大于零的比赛标为 `ELIGIBLE`；
7. 重新计算每场已知 apex 玩家密度；
8. 对候选排序，先探测 Replay Route B，再下载直到当前 patch 的总 `VERIFIED` 数达到 target；
9. 原子写出 manifest 和 run report。

默认每名玩家取最近 20 场；候选不足时再取下一页。程序以目标缺口加缓冲量构建候选池，缓冲至少 20 场、否则约为缺口的 25%，并在已有候选足够时停止扩大 history 查询。

## 去重模型

发现次数与比赛数有意分开：

- `raw_match_discoveries`：所有重复观察的总次数；
- `discovery_edges`：唯一的“玩家发现某场比赛”边数量；
- `unique_matches`：dataset 内唯一 Match ID 数量；
- `matches.match_id`：全局比赛主键；
- `(platform, game_id)`：第二唯一约束。

同一场比赛即使被十名高端玩家发现，也只产生一条 `matches` 记录和一个 Replay job。

## 质量排序

质量完全来自当前排行榜 PUUID 与比赛参与者 PUUID 的精确交集，不使用姓名、知名度、模糊推断或 ML。每场保存：

- `challenger_count`；
- `grandmaster_count`；
- `master_count`；
- `known_apex_count`；
- `highest_tier`。

候选 SQL 排序键依次为：

1. Challenger 数量降序；
2. 是否至少包含一名 Challenger；
3. Grandmaster 数量降序；
4. 已知 apex 总数降序；
5. Master 数量降序；
6. 比赛创建时间降序；
7. Match ID 升序，作为稳定决胜项。

## Replay 获取

### Route A：跨区 LCU

调用本地 `POST /lol-replays/v1/rofls/{gameId}/download`。实测非 KR 客户端会把裸 game ID 组合成客户端自己的平台身份，因此 Route A 对跨区 KR 样本失败。它仍保留为 probe 证据，不被误判为主下载路线。

### Route B：当前 Replay 后端

程序从 League Client 当前日志识别 player-platform backend，或使用明确配置的 `REPLAY_EDGE_BASE_URL`。随后只在内存中读取当前合法 LCU 会话的 RSO access token 与 entitlement token，并请求：

```text
/match-history-query/v3/product/lol/matchId/<KR_match_id>/infoType/replay
```

显式的 `KR_<gameId>` 保留了比赛平台身份。Route B 已在非 KR 登录会话中实测通过，详情见 acquisition findings。

后端响应带 HTTP `Content-Encoding: gzip`。这是传输编码，不是 ROFL 文件容器：HTTP 客户端通过 `iter_bytes()` 透明解码，probe 检查解码后前缀为 `RIOT`，downloader 也把解码后的官方 ReplayV2 bytes 写入 `.partial`。当存在 Content-Encoding 时，HTTP `Content-Length` 是编码后 wire body 的长度，与落盘 RIOT 文件大小不同，不能直接相等比较。

### Route C

Route B 已通过，因此没有必要把 Route C 作为正式实现。probe 会把它记录为 `UNKNOWN`，并注明 `not_needed=true`。

## 下载、验证与发布

每个候选的状态变化为：

```text
ELIGIBLE -> QUEUED -> DOWNLOADING -> DOWNLOADED -> VERIFIED
```

错误分支为：

```text
DOWNLOADING/DOWNLOADED -> UNAVAILABLE
                       -> FAILED_RETRYABLE
                       -> FAILED_PERMANENT
```

下载目标先写为 `<match>.rofl.partial`，写完后 `flush + fsync`。验证包括：

- 非零；仅在没有 HTTP Content-Encoding 时才要求落盘大小与 `Content-Length` 一致；
- 拒绝 HTML / JSON 错误体；
- 拒绝遗留 gzip wrapper，并要求以 `RIOT` 开头；
- 读取并校验内嵌的 exact game version；
- 校验尾部 metadata 长度、JSON、签名边界；
- 顺序校验 17-byte chunk record、压缩/未压缩 body 长度、stream tag 和 chunk region 边界；
- SHA-256；
- 记录文件大小、前 32 字节摘要、game/keyframe/start-keyframe 计数。

Replay 下载遇到网络、HTTP 429 或 5xx 时，会在同次下载内最多尝试 4 次。普通重试使用指数退避与 jitter；429 还会解析 `Retry-After`（秒数或 HTTP date），实际等待时间取 `Retry-After` 与退避加 jitter 的较大值。仍失败才写入 `FAILED_RETRYABLE`，供下一次 `run` 继续。

通过后先把 job 标成 `DOWNLOADED`，再以同文件系统原子 rename 发布 `.rofl`，最后在一个数据库事务中记录 download 并标成 `VERIFIED`。Collector 自身检查 ReplayV2 容器布局，但不把 chunk 内 Zstd/block/packet 的解析冒充语义验证。

## 遗留 HTTP gzip 迁移

`normalize-http-gzip` 是仅为本次早期遗留资产提供的一次性、preservation-first maintenance 命令，不属于正常采集流程。它只处理最新 KR dataset 的 manifest 文件：

1. 已经以 `RIOT` 开头的文件只重新验证；
2. 以 gzip magic 开头的旧文件先完整解码到临时文件；
3. 验证解码结果的 `RIOT` ReplayV2 布局和 exact build；
4. 把原 wire wrapper 按原 Match ID 保存到 `quarantine/http-gzip/*.rofl.gz`；
5. 原子发布解码后的 `.rofl`，更新 download 记录和 manifest。

当前下载器已在 HTTP 层透明解码，新下载文件不需要此迁移。Quarantine 副本是审计/恢复资产，不是正式 `.rofl`，不会进入 manifest。

## 外部 parser portability 实测

现有 Replay parser 对 20 个 `16.17.810.4348` 的原始 `RIOT` 文件进行了实测。容器、metadata、Zstd payload 和 block timestamp framing 均可读取，20/20 每场的 framing error 都为 0；parser 汇总也报告 `block_framing_errors: 0`。

但 parser 没有 exact build `16.17.810.4348` 的语义 decoder profile，所有文件的 decoder status 都是 `UNSUPPORTED_REPLAY_VERSION`，语义事件未验证。因此结论必须分层：

```text
container / Zstd / block framing: PASS
exact-build semantic decoding: UNSUPPORTED_REPLAY_VERSION
KR_PARSER_PORTABILITY: PARTIAL
```

`PARTIAL` 不能提升为 `PASS`，也不表示原始 Replay 损坏。详细报告位于 `data/KR/16.17/reports/parser_portability_20/acceptance_summary.json`。

## 恢复与并发

`run` 持有 `data/.collector.lock`，防止两个进程同时修改同一数据集。SQLite 使用 foreign keys、WAL、`synchronous=FULL` 和显式事务。

取得当前 dataset 后，新 `run` 会先把同一 dataset 中遗留的所有 `RUNNING` run 更新为 `INTERRUPTED`，写入结束时间及 `PROCESS_INTERRUPTED_BEFORE_FINALIZATION` 原因，再创建自己的 `RUNNING` 记录。候选容量规划的 recoverable backlog 包括 `ELIGIBLE`、`QUEUED`、`DOWNLOADING`、`DOWNLOADED` 和 `FAILED_RETRYABLE`，避免把崩溃前已经取得的工作误当作全新缺口。

启动下载前的 reconcile 会：

- 验证并接管数据库已知但状态尚未完成的最终 `.rofl`；
- 把遗留的 `DOWNLOADING` 变成可重试；
- 若 `.partial` 已经是完整有效的原始 `RIOT` ReplayV2，则原子发布并接管；
- 保留无效 partial，后续下载可重新写入；
- 从不覆盖已存在的最终资产；
- 若未完成 job 对应的现有 final 无法通过完整性验证，则原文件保留在原路径，错误以 `replay_reconcile` stage 入库，job 转为 `FAILED_PERMANENT`；reconcile 继续处理其余任务。

Manifest 和 run report 都先写临时文件、`fsync`，再原子替换。Manifest 在发布前还会检查 Match ID 唯一、文件存在及大小一致。

## 当前实测数据集

恢复后的 KR patch `16.17` 当前有 108 个 `VERIFIED` Replay，总大小 `1,509,715,320` bytes。对全部 108 个 manifest 文件执行 `status --verify-files` 的结果为 `integrity_audit: PASS`、`integrity_errors: []`。V1 的最低交付目标是 100 场；保留 108 场是 target-total 与 preservation-first 共同作用的预期结果。

## 安全边界

- 不自动输入账号密码、切换 region、关闭或重启 League Client；
- LCU lockfile 密码、RSO token、entitlement token 只用于本机当前会话，不持久化；
- acquisition 证据只记录路由、HTTP 状态、平台结论和经过裁剪的非秘密信息；
- `.env`、`data/`、`logs/`、SQLite、`.rofl` 和 `.partial` 均被 Git 忽略；
- 不绕过认证：Route B 使用用户本人已登录的合法 Riot 会话。
