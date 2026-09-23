# 数据集与数据库 Schema

> 本文描述 Riot 国际服采集数据库；dataset 按平台与 patch 隔离。腾讯国服 Replay+SUMMARY/DETAILS 使用 `data/CN/replay-paired/collector.sqlite3` 内的独立 schema。

## 总览

持久化由 SQLite 数据库、`.rofl` 文件和 JSONL manifest 组成。默认数据库是 `data/collector.sqlite3`，当前 schema 版本为 `1`。

原则：

- PUUID 是玩家稳定身份；
- Match ID 是比赛与 Replay job 的稳定身份；
- patch 是数据集边界；
- 完整 `gameVersion` 是 build 边界；
- 只有状态为 `VERIFIED` 且存在 download 记录的比赛进入 manifest；
- 数据库中的路径相对于 `data/`，便于整体移动数据目录。

数据库时间戳使用带时区的 UTC ISO 8601 字符串。`game_creation` 保留 Riot Match-V5 的整数时间戳，`game_duration` 保留秒数。

## SQLite 表

### `schema_info`

| 字段 | 含义 |
| --- | --- |
| `version` | 数据库 schema 版本，当前为 1 |

### `datasets`

每个平台的每个 patch 一条逻辑数据集。

| 字段 | 含义 |
| --- | --- |
| `id` | 内部主键 |
| `platform` | Riot 平台 ID，如 `KR`、`EUW1` |
| `patch_key` | 研究 patch，例如 `16.17` |
| `exact_realm_version` | 最近一次对应平台的官方 realm 完整版本 |
| `created_at` | 首次创建时间 |

唯一约束为 `(platform, patch_key)`。每场比赛仍在 `matches.game_version_exact` 中保存自己的完整版本，不依赖 dataset 行推断 build。

### `runs`

记录 `probe` 和 `run` 生命周期。

| 字段 | 含义 |
| --- | --- |
| `id` | run ID |
| `dataset_id` | 所属 dataset，可空 |
| `command` | `probe` 或 `run` |
| `target_total` | `run --target` 的总目标，不是增量 |
| `status` | `RUNNING`、`TARGET_REACHED`、`INCOMPLETE`、`FAILED`、`INTERRUPTED` 等 |
| `capability_status` | Replay 能力结论，如 `PASS` |
| `started_at` / `finished_at` | 生命周期时间 |
| `summary_json` | 运行摘要 |

新 `run` 开始时，同一 dataset 里遗留的 `RUNNING` 行会被更新为 `INTERRUPTED`，补写 `finished_at`，并保存 `{"reason":"PROCESS_INTERRUPTED_BEFORE_FINALIZATION"}`。这让进程崩溃与仍在运行的任务不再混淆。

### `players`

当前 dataset 的官方 apex ladder 快照。

| 字段 | 含义 |
| --- | --- |
| `dataset_id`, `puuid` | 复合主键 |
| `summoner_id` | Riot 返回时保存；不是主身份 |
| `tier` | `CHALLENGER`、`GRANDMASTER`、`MASTER` |
| `rank` | 分段内 rank 字段 |
| `league_points` | LP |
| `wins` / `losses` | 当前 ladder 统计 |
| `retrieved_at` | 刷新时间 |
| `source_run_id` | 写入该快照的 run |

同一 PUUID 在同一 dataset 中只保留一条当前记录。

### `match_discoveries`

保存“哪个玩家的 history 发现了哪场比赛”，用于审计重复发现。

| 字段 | 含义 |
| --- | --- |
| `dataset_id`, `match_id`, `puuid` | 复合主键，即唯一 discovery edge |
| `first_run_id` | 首次发现 run |
| `first_seen_at` / `last_seen_at` | 首次与最近观察时间 |
| `observations` | 同一 edge 被重复看到的次数 |

统计口径：

- `RAW_DISCOVERIES = sum(observations)`；
- `DISCOVERY_EDGES = count(rows)`；
- `UNIQUE_MATCHES = count(distinct match_id)`。

### `matches`

Match-V5 比赛明细与质量元数据。

| 字段 | 含义 |
| --- | --- |
| `match_id` | 主键，例如 `KR_<gameId>` |
| `dataset_id` | 发现它的 patch dataset |
| `platform` | Match-V5 `platformId`，必须与当前 dataset 平台一致才可 eligible |
| `game_id` | 数字 game ID，以文本保存 |
| `queue_id` | 必须为 420 才可 eligible |
| `game_version_exact` | Match-V5 完整 `gameVersion` |
| `patch_key` | 从完整版本解析的 major.minor |
| `game_creation` | Riot 比赛创建时间整数 |
| `game_duration` | 秒 |
| `metadata_json` | 完整 Match-V5 响应 JSON |
| `challenger_count` | 参与者中当前已知 Challenger 数 |
| `grandmaster_count` | 当前已知 Grandmaster 数 |
| `master_count` | 当前已知 Master 数 |
| `known_apex_count` | 上述三类总数 |
| `highest_tier` | 存在的最高 tier，未知可空 |
| `first_seen_at` / `last_seen_at` | 入库与刷新时间 |

`match_id` 全局唯一，另有 `(platform, game_id)` 唯一约束。`metadata_json` 可能含玩家标识，不应公开发布；公开数据前应另做隐私审查。

### `match_participants`

| 字段 | 含义 |
| --- | --- |
| `match_id`, `puuid` | 复合主键 |

它保留 Match 与参与者的精确关系，用于 ladder 刷新后重新计算质量。

### `replay_jobs`

每场比赛一个 durable Replay 状态机。

| 字段 | 含义 |
| --- | --- |
| `match_id` | 主键，并引用 `matches` |
| `state` | 当前状态 |
| `resume_state` | 可重试任务应恢复到的状态 |
| `provider` | 当前下载 provider |
| `attempts` | 进入下载的次数 |
| `next_attempt_at` | 预留的下次尝试时间 |
| `lease_owner` / `lease_expires_at` | 预留租约字段；当前单进程由文件锁保护 |
| `last_error_id` | 最近错误 |
| `eligibility_reason` | `CURRENT_PATCH_RANKED_SOLO_COMPLETED` 或 `FILTERED` |
| `created_at` / `updated_at` | 状态时间 |

状态集合：

| 状态 | 含义 |
| --- | --- |
| `DISCOVERED` | 已发现，尚未完成资格判断 |
| `ELIGIBLE` | 当前 patch、目标平台、queue 420、已结束 |
| `INELIGIBLE` | 被版本、平台、队列或完成条件过滤 |
| `QUEUED` | 已排队 |
| `DOWNLOADING` | 正在写 `.partial` |
| `DOWNLOADED` | 下载和容器验证完成，等待最终登记 |
| `VERIFIED` | 文件与 download 元数据已持久化，可进 manifest |
| `UNAVAILABLE` | 后端明确返回没有 Replay，例如 404 |
| `FAILED_RETRYABLE` | 网络、截断或服务端错误；下次 run 可重试 |
| `FAILED_PERMANENT` | 非重试错误 |

用于计算是否还需扩大发现池的 recoverable backlog 包含 `ELIGIBLE`、`QUEUED`、`DOWNLOADING`、`DOWNLOADED` 和 `FAILED_RETRYABLE`。因此进程中断留下的两个中间状态会进入恢复规划，而不会被当成不存在。

恢复接管未完成 job 时，若同名 final 已存在但损坏，程序不会覆盖或删除文件；它在 `errors` 中记录 `stage=replay_reconcile` 与 `details_json.preserved_path`，并把 job 转成 `FAILED_PERMANENT`，然后继续扫描其他 job。

### `downloads`

只有已验证资产才有记录。

| 字段 | 含义 |
| --- | --- |
| `match_id` | 主键 |
| `file_path` | 相对 `data/` 的唯一路径 |
| `file_size` | HTTP 透明解码后、以 `RIOT` 开头的正式 `.rofl` 字节数，必须大于零 |
| `sha256` | 64 位十六进制 SHA-256 |
| `provider` | 当前为 `riot-player-platform-replay-v2` |
| `downloaded_at` / `verified_at` | 下载与验证时间 |
| `verification_json` | 容器校验详情 |

`verification_json` 的当前字段包括：

- `container: "riot-replay-v2"`；
- `file_size`、`sha256`、`inner_prefix_hex`；
- `game_version`；
- `chunk_count`、`game_chunks`、`keyframes`、`start_keyframes`；
- `container_layout: "PASS"`、`metadata_json: "PASS"`；
- `uncompressed_size`：为兼容既有 manifest 保留的字段名；对原始 RIOT 文件等于 `file_size`，不表示 `.rofl` 自身是 gzip 容器；
- `adopted_existing: true`：重启时接管已有文件时出现；
- `http_content_encoding_normalized` 与 `preserved_transport_backup`：仅本次遗留 HTTP gzip 迁移的审计字段。

### `acquisition_probes`

保存 Replay feasibility 证据，但不保存 token。

| 字段 | 含义 |
| --- | --- |
| `route` | Route A / B / C |
| `capability` | `PASS`、`FAIL`、`UNKNOWN`、`ACTION_REQUIRED` |
| `sample_match_id` | 被验证的样本 |
| `mechanism` | 请求机制说明 |
| `http_status` | 响应码，可空 |
| `auth_result` | 脱敏后的认证结论 |
| `region_evidence` | 平台路由结论 |
| `client_log_excerpt` | 必要且裁剪后的客户端证据，可空 |
| `patch_key` / `attempted_at` | 环境与时间 |
| `evidence_json` | content type、wire `Content-Length`、HTTP `Content-Encoding`、解码后 `RIOT` payload magic 等非秘密证据 |

### `errors`

| 字段 | 含义 |
| --- | --- |
| `run_id` / `match_id` | 错误上下文 |
| `stage` | 发生阶段 |
| `code` / `message` | 稳定错误码与脱敏说明 |
| `retryable` | 是否可重试 |
| `http_status` | HTTP 状态，可空 |
| `occurred_at` | UTC 时间 |
| `details_json` | 附加结构化信息 |

## Manifest

路径：

```text
data/<platform>/<patch>/manifests/dataset_manifest.jsonl
```

每行一个独立 JSON 对象，只包含 `VERIFIED` Replay，按 `match_id` 稳定排序。字段如下：

| 字段 | 含义 |
| --- | --- |
| `match_id`, `game_id`, `region` | 稳定比赛身份 |
| `queue_id` | 队列 ID，目标为 420 |
| `game_version`, `patch` | 完整 build 与 patch |
| `game_creation`, `game_duration` | 比赛时间和时长 |
| `challenger_count`, `grandmaster_count`, `master_count` | 精确 apex 密度 |
| `known_apex_count`, `highest_tier` | 汇总质量字段 |
| `source_quality` | 离散质量标签 |
| `file` | 相对 `data/` 的 Replay 路径 |
| `size`, `sha256` | 文件完整性身份 |
| `downloaded_at`, `verified_at` | 处理时间 |
| `verification` | 原始 RIOT ReplayV2 容器验证对象 |
| `provider` | Replay 来源实现 |

`source_quality` 的规则依次为：Challenger 至少 5 人是 `CHALLENGER_HEAVY`；否则有 Challenger 是 `CHALLENGER_PRESENT`；否则 Grandmaster 至少 5 人是 `GRANDMASTER_HEAVY`；其余为 `MASTER_HEAVY`。

示例使用占位身份，不是实际账号数据：

```json
{"match_id":"KR_<gameId>","game_id":"<gameId>","region":"KR","queue_id":420,"game_version":"16.17.810.4348","patch":"16.17","challenger_count":10,"grandmaster_count":0,"master_count":0,"known_apex_count":10,"highest_tier":"CHALLENGER","source_quality":"CHALLENGER_HEAVY","file":"KR/16.17/builds/16.17.810.4348/rofl/KR_<gameId>.rofl","size":14437706,"sha256":"<64 hex characters>","verification":{"container":"riot-replay-v2","container_layout":"PASS","metadata_json":"PASS","game_version":"16.17.810.4348","chunk_count":82},"provider":"riot-player-platform-replay-v2"}
```

Manifest 写入时先生成 `.tmp`，验证 Match ID 无重复、文件存在且大小一致，再原子替换正式文件。下游应把 manifest 当作资产清单，把 SQLite 当作完整的采集与审计状态。

当前 KR `16.17` dataset 的实测状态为 108 个 `VERIFIED` manifest rows；全部文件重新读取后的 integrity audit 为 `PASS`，错误列表为空。V1 最低目标定义仍是 100，schema 不会因为实际数量超过 target 而删除多余记录或资产。

## HTTP 传输与文件格式

这两个层次不能混为一谈：

| 层次 | 实际格式 | 长度含义 |
| --- | --- | --- |
| Replay backend wire response | HTTP `Content-Encoding: gzip` | `Content-Length` 是 gzip 编码后的 wire bytes |
| 正式 `.rofl` 文件 | 以 `RIOT` 开头的 ReplayV2 | `downloads.file_size` / manifest `size` 是透明解码后的文件 bytes |

本次早期采集产生过把第一层直接落盘的遗留文件。一次性 `normalize-http-gzip` 命令将其解码为第二层，同时把原 wrapper 移至：

```text
data/KR/<patch>/quarantine/http-gzip/<match_id>.rofl.gz
```

Quarantine 文件不属于 `downloads.file_path`，不计为 `VERIFIED` Replay，也不写入 manifest；它只由 `verification.preserved_transport_backup` 引用，满足 preservation-first 审计要求。新下载直接得到 RIOT 文件，不再需要迁移。
