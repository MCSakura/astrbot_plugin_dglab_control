# 更新日志

## v1.0.1（2026-09-23）

### 修复

- **日志规范**：移除 Python 内置 `logging` 模块（`main.py`、`dglab_server.py`），统一改为从 `astrbot.api` 导入 `logger`，日志正确路由到插件专属 logger（`astrbot.plugin.astrbot_plugin_dglab_control`），支持独立日志级别调节。
- **数据持久化**：`target_id.json` 的保存路径从插件目录下的 `cache/` 迁移至 `data/plugin_data/astrbot_plugin_dglab_control/`，符合 AstrBot 插件数据规范，便于用户数据迁移与审计；二维码 PNG 等临时产物仍保留在 `cache/`。
- **旧数据迁移**：升级后若检测到旧版 `cache/target_id.json` 存在而新位置尚无文件，会自动迁移，避免升级导致 targetId 变化、已发出的二维码失效。

## v1.0.0

- 首个正式版本。
- 插件内嵌 V3 / V4 WebSocket Server，APP 扫码直连，无需外部 `dglab-websocket-server`。
- 支持一键开火、A/B 通道独立开关、连接状态查询、V3/V4 双协议二维码。
- 控制类指令支持仅机器人管理员可用（`admin_only`）。
- targetId 持久化，重启后二维码不失效。
