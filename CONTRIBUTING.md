# 参与开发

欢迎提交针对采集、验证、恢复、文档和平台兼容性的 Issue 或 Pull Request。请在报告中注明平台、patch、Python 版本、命令、预期行为、实际错误码和可复现步骤。不要上传账号、PUUID、API Key、token、lockfile、原始比赛元数据或 `.rofl`；日志请先脱敏。

本地开发：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
```

新增平台能力时请同时更新路由、隔离规则、测试和 `ROADMAP.md` 的证据状态。模拟测试可证明代码行为；真实客户端可用性须附同平台、同 patch 的脱敏实测证据。修复恢复逻辑时保留原始失败文件和数据库记录，避免自动覆盖或删除研究资产。

提交贡献即表示你有权提交该代码，并同意该贡献按本仓库的 `AGPL-3.0-only` 许可证分发。第三方代码须说明来源和许可证；不要直接提交受限制的客户端文件或数据集。
