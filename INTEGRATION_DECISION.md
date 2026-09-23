# Collector integration decision — 2026-09-23

`PROJECT_CONTEXT_LOADED = YES`

## Architecture gate

1. **Authority and value.** The user explicitly requested that the Tencent CN Replay downloader in `lol data` be fully integrated into this project, that this project be renamed for global regions, and that all selectable Riot global regions be supported. This authorizes consolidating the two offline collector implementations in this repository.
2. **Context.** The `lol data` charter and contract, and the relevant V2 North Star, Project Map, Decision Log, Capability Registry, and Evidence Source Registry were reviewed. The existing V2 Project Map names `lol data` as the Research Collector. This local decision records the user-directed migration of its CN implementation into the renamed global-region collector; the old map pointer is historical until the shared governance is updated.
3. **Ownership.** The resulting project remains an offline corpus collector. It acquires and preserves Replay bytes and source metadata; it does not become the Inference Lab, Akari runtime, Replay semantic decoder, or map-truth owner. The CN and Riot global backends keep separate authentication, region identity, databases, and on-disk data roots.
4. **Evidence and acceptance.** The existing KR dataset, source `lol data` repository, raw artifacts, failures, and historical evidence remain in place. Neither source data nor credentials are copied. Source code is migrated with test coverage. The CN backend retains Replay plus fresh SUMMARY/DETAILS pairing and provenance. Global backends retain exact platform Match IDs, ROFL integrity validation, and per-platform manifests. Client-backed live acquisition is reported only when actually exercised, rather than inferred from tests.
5. **Preservation.** No deletion or rewriting of either existing dataset is authorized by this change. New paths are additive, with KR default paths remaining compatible. A shared V2 map/registry pointer update is a separate writable-scope change; until then, this decision and the source paths identify the code migration precisely.

Decision: `CONSOLIDATE_OFFLINE_COLLECTOR_WITH_SEPARATE_GLOBAL_AND_CN_BACKENDS`.

`ARCHITECTURE_GATE = PASS`
