# Completed KR Replay 获取实测结论

## 结论

```text
CROSS_REGION_COMPLETED_REPLAY (Route A): FAIL
CURRENT_REPLAY_BACKEND (Route B): PASS
OTHER_COMPLETED_REPLAY_ROUTE (Route C): NOT_NEEDED / UNKNOWN
KR_ACCOUNT_REQUIRED: NO
```

在本次实际验证环境中，一个合法登录的非 KR（JP1）League Client 会话可以使用自身正常取得的 RSO access token 与 entitlement token，向当前 player-platform Match History / Replay 后端显式提交 KR Match ID，并下载完整的 KR completed-game Replay。

这证明当前可行路线不需要购买、借用或租用 KR 账号。它不代表可以绕过登录：仍然需要用户本人合法登录 League Client，程序不会保存或伪造认证材料。

## 验证环境

| 项目 | 实测值 |
| --- | --- |
| 验证日期 | 2026-09-03（数据库时间使用 UTC） |
| League Client 当前平台 | `JP1` |
| KR patch | `16.17` |
| 样本完整 gameVersion / build | `16.17.810.4348` |
| 样本 Match ID | `KR_8366040871` |
| Queue | Ranked Solo/Duo，`420` |
| 样本状态 | 已结束 |

本文有意不记录账号名、PUUID、RSO token、entitlement token、LCU lockfile 密码、完整请求头、个人身份或可重放凭据。

## Route A — 跨区 LCU

请求机制：

```text
POST /lol-replays/v1/rofls/{gameId}/download
body: {"componentType":"match-history"}
```

实测结果：

1. 对 KR game ID `8366040871` 发起本地 LCU POST；
2. LCU 返回 HTTP `204`，即本地命令被接受；
3. 当前客户端平台是 `JP1`；
4. 客户端随后向后端查询的是 `JP1_8366040871`，不是原比赛身份 `KR_8366040871`；
5. 该错误平台身份的 summary lookup 返回 HTTP `404`；
6. 没有由 Route A 得到 KR `.rofl`。

判定：`FAIL`。

关键解释：HTTP 204 只证明 LCU 接受了“开始下载”命令，不证明异步下载完成。客户端用自己的平台前缀重新组合裸 game ID，造成跨区身份错误；因此不能把 204 当作 cross-region Replay 成功。

## Route B — 当前 player-platform Replay 后端

使用同一个合法 JP1 League Client 会话：

1. 通过本地 LCU 取得当前会话的 RSO access token 与 entitlement token，仅在内存中使用；
2. 从当前 League Client 行为/日志识别 player-platform backend；
3. 在请求路径中显式使用完整 KR 身份 `KR_8366040871`；
4. 请求 `infoType/summary`，HTTP `200`；
5. 请求 `infoType/replay`，HTTP `200`；
6. Replay 响应为 `application/octet-stream`，attachment 文件名带正确的 KR Match ID；
7. HTTP 响应使用 `Content-Encoding: gzip`，编码后的 wire body / `Content-Length` 为 `14,327,682` bytes；
8. HTTP 客户端透明解码传输层 gzip 后得到 `14,437,706` bytes；
9. 解码后的正式 `.rofl` 以 `RIOT` 开头，是官方 ReplayV2 文件，而不是 gzip 容器；
10. 解码后文件的 SHA-256 为 `b4c4da58e051560ac38ac2073eef00c21266f2e437ecb5900db64fd1ec478e9d`；
11. 该 RIOT 文件的版本、metadata JSON、chunk/keyframe 布局均通过 collector 校验并进入 manifest。

保存路径：

```text
data/KR/16.17/builds/16.17.810.4348/rofl/KR_8366040871.rofl
```

判定：`PASS`。

这里必须区分两个层次：

```text
HTTP transport: Content-Encoding gzip, 14,327,682 wire bytes
official .rofl: decoded RIOT ReplayV2, 14,437,706 file bytes
```

早期实验曾错误地把第一层 wire bytes 直接落盘。随后通过一次性 `normalize-http-gzip` 迁移，原 gzip wrapper 被完整保存在 `data/KR/16.17/quarantine/http-gzip/`，正式路径原子替换为第二层 RIOT 文件，数据库 hash/size 与 manifest 一并更新。当前 downloader 已直接执行透明 HTTP 解码；normalizer 仅服务于这次遗留迁移，不是长期采集步骤。

身份与认证证据分别成立：

- 请求身份是显式的 `KR_8366040871`，不是由客户端平台推断出的 JP1 身份；
- 发起请求的登录会话仍是 JP1；
- 后端接受该合法 global-client session，并同时返回 summary 和完整 Replay；
- 因此在这个当前版本、当前后端和已验证样本上，KR entitlement 不是 completed Replay 下载的必要条件。

## Route C — 其他合法 completed-Replay 方法

Route B 已满足项目所需的 completed-game Replay 获取能力，因此没有理由继续寻找或实现其他路线。当前记录为：

```text
capability: UNKNOWN
not_needed: true
```

这不是 Route C 失败，也不是对所有其他 Riot 路线的否定；只是避免在已经存在合法、可验证路线后继续扩大范围。

## 证据强度与限制

已经证明：

- KR League-V4 / ASIA Match-V5 提供的真实 KR 比赛身份可用于 Replay backend；
- 非 KR League Client 的合法 RSO + entitlement 会话被 backend 接受；
- backend 可返回正确 content type、文件名以及带 gzip Content-Encoding 的完整 HTTP body；
- 透明解码后可得到以 RIOT 开头的 ReplayV2，完成 SHA-256、metadata 和 chunk 布局校验；
- collector 可以把该真实文件登记为 `VERIFIED` 并写入 manifest。

尚未由这一份样本单独证明：

- Riot 将来不会改变 endpoint、认证、保留期或 region policy；
- 所有历史 completed game 都仍有 Replay，过期或不可用比赛仍可能返回 404；
- exact-build packet 语义 decoder 的完整可移植性；现有 parser 对容器/Zstd/framing 的通过不代表语义 profile 可用；
- 最终批量 target 已经完成。Route B capability、20 文件 portability probe 与最终批次完成是三个不同结论。

因此每次新 patch 运行前仍应执行 `collector probe`。若 Route B 不再 PASS，应保留 acquisition probe 的 HTTP 状态和脱敏证据，停止下载并重新评估，而不是绕过认证或假定旧结论永久有效。

## KR parser portability — 20 文件实测

现有 ROFL parser 对 20 个 build 均为 `16.17.810.4348` 的原始 RIOT 文件进行了实际读取。证据报告：

```text
replay_file_count: 20
container opened: 20/20
Zstd / block framing: 20/20
per-file framing errors: 0 for every file
aggregate block_framing_errors: 0
exact-build semantic profile: unavailable
parser status: UNSUPPORTED_REPLAY_VERSION
KR_PARSER_PORTABILITY: PARTIAL
```

通过的层级包括 RIOT 容器、tail metadata、chunk/keyframe inventory、Zstd payload 和 block timestamp framing。没有通过的层级是依赖 build-specific profile 的 packet 语义：报告中 `decoded_packet_count` 为 0，语义事件未验证，不能声称死亡、伤害、施法、位置、视野等 decoder 对本 build 可用。

所以 `PARTIAL / UNSUPPORTED_REPLAY_VERSION` 是有意的严格结论：原始 KR 文件和底层 framing 可读，但现有 parser 尚未支持 `16.17.810.4348` 的语义 profile。报告位于：

```text
data/KR/16.17/reports/parser_portability_20/acceptance_summary.json
```

这 20 个文件用于 portability 验证；本文不由此声明任何更大的最终采集目标已经达成。

## 当前批量与恢复验收

在 Route B 和 RIOT-at-rest 修正后，采集恢复继续运行。当前 KR patch `16.17` 的持久化状态为：

```text
V1 minimum delivery target: 100
VERIFIED: 108
full manifest file audit: PASS
integrity errors: 0
total RIOT .rofl bytes: 1,509,715,320
```

108 个文件全部重新读取并通过 SHA-256、大小、RIOT magic、exact version、metadata 和 chunk-layout 检查。超过最低目标的 8 个文件按 preservation-first 保留，不会被裁剪；因此“最低验收目标 100”和“当前实际 VERIFIED 108”并不矛盾。

恢复行为也已落实到正式路径：新 run 将同 dataset 残留 `RUNNING` 标为 `INTERRUPTED`，把 `DOWNLOADING` / `DOWNLOADED` 纳入 recoverable backlog；恢复接管遇到损坏的既有 final 时保留原文件、记录错误并标成 `FAILED_PERMANENT`，随后继续其他任务。

## 实现采用的安全边界

- 只使用当前本机已登录会话公开给 League Client 自身的本地接口；
- 不自动登录、不输入密码、不修改账号 region；
- 不购买、借用、窃取账号；
- 不保存 token、lockfile 凭据或完整个人身份；
- 只记录判定所需的 Match ID、client platform、HTTP 状态、内容元数据和完整性摘要；
- Replay backend URL 默认从当前客户端日志动态发现，避免把一次环境中的主机硬编码成长期事实。
