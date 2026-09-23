# 郊狼 DG-LAB 控制插件（AstrBot）

通过 QQ 机器人控制郊狼（DG-LAB）硬件，支持 V3 / V4 双协议二维码接入、群内一键开火、
A/B 通道独立开关、查询已连接 APP。

**插件内嵌 WebSocket Server**，不依赖外部 `dglab-websocket-server`：插件自身扮演
官方协议里的「控制方」，APP 扫码直连插件，机器人指令直接下发到设备。

---

## 一、特性

| 能力 | 说明 |
|---|---|
| 双协议二维码 | 一次生成 V3（旧版 APP）+ V4（DG-LAB 4）两张二维码图片 |
| 一键开火 | `/郊狼开火 [A] [B]`，未传强度时用配置的默认值（默认 15） |
| 通道独立控制 | A/B 通道可分别开启（指定强度）与关闭 |
| 连接查询 | `/郊狼查询` 列出已接入 APP、设备槽位、监听状态 |
| targetId 持久化 | 存于 `data/plugin_data/astrbot_plugin_dglab_control/target_id.json`，重启后二维码不失效 |
| 自动探测本机 IP | `ws_host` 留空时自动取本机出网 IPv4 |
| 分级权限 | `/郊狼二维码`、`/郊狼查询` 所有人可用；控制类指令仅机器人管理员 |

---

## 二、架构

```
┌──────────────┐   扫码   ┌─────────────────────────────────┐
│  DG-LAB APP  │ ───────► │  AstrBot 插件（本插件）          │
│  (V3 或 V4)  │ ◄─────── │                                 │
└──────────────┘  WS 长连  │  DglabV3Server :9999  ┐         │
                          │  DglabV4Server :9998  ┘ 内嵌    │
                          │                                 │
      QQ 群 ◄──── 机器人 ──│  main.py（指令 → Server API）   │
                          └─────────────────────────────────┘
```

插件即「控制方」，因此**不需要**再跑官方 TS 版 server，也不需要额外的控制端网页。

---

## 三、安装

1. 把整个 `astrbot_plugin_dglab_control` 目录放入 AstrBot 的 `data/plugins/` 下。
2. 重启 AstrBot（或在 WebUI 重载插件），AstrBot 会按 `requirements.txt` 自动安装：
   - `websockets>=12.0`
   - `qrcode[pil]>=7.4`
3. 在 WebUI 中启用插件，日志出现下面这行即为启动成功：

```
[郊狼] 已启动内置 Server：V3=ws://<host>:9999/<v3_id>，V4=ws://<host>:9998?tid=<v4_id>，等待 APP 扫码接入。
```

> **Docker 部署注意**：需确保 AstrBot 的 `data/` 目录（含 `data/plugin_data/astrbot_plugin_dglab_control/target_id.json`）
> 挂载为持久卷，否则容器重建后 targetId 丢失，之前发出去的二维码全部失效。

---

## 四、配置项

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ws_host` | string | 空 | 二维码里写入的地址。可填 IP 或域名；留空自动取本机出网 IPv4。插件监听 `0.0.0.0`，APP 能用任意本机可达 IP 连入。 |
| `v3_port` | int | 9999 | V3 Server 监听端口（沿用官方默认）。 |
| `v4_port` | int | 9998 | V4 Server 监听端口（沿用官方默认）。 |
| `default_intensity` | int | 15 | 未显式传强度时的默认值，建议 5–30。 |
| `fire_duration_ms` | int | 3000 | 开火后强度保持时长，结束后自动归零。 |
| `admin_only` | bool | true | 是否让**控制类指令**（开火 / 通道开关）仅限机器人管理员。`/郊狼二维码`、`/郊狼查询` 始终所有人可用。 |

---

## 五、指令

| 指令 | 参数 | 权限 | 说明 |
|---|---|---|---|
| `/郊狼二维码` | — | 所有人 | 发送 V3 + V4 两张二维码图片及对应 wsUrl 文本 |
| `/郊狼查询` | — | 所有人 | 查看监听状态、targetId、已接入 APP 与设备列表 |
| `/郊狼开火` | `[A强度] [B强度]` | 管理员 | 双通道同时开火，缺省用 `default_intensity` |
| `/郊狼A开` | `[强度]` | 管理员 | 仅 A 通道开火 |
| `/郊狼A关` | — | 管理员 | 关闭 A 通道（清波形 + 归零） |
| `/郊狼B开` | `[强度]` | 管理员 | 仅 B 通道开火 |
| `/郊狼B关` | — | 管理员 | 关闭 B 通道（清波形 + 归零） |

> 管理员指机器人配置中的 admin / owner 角色。把 `admin_only` 设为 `false`
> 可让控制类指令也对所有人开放。

---

## 六、协议实现说明

### V3（`ws://host:9999/{targetId}`）

- APP 以**路径段**携带 targetId 接入；targetId 为 36 位 UUID。
- 接入后服务端连发两帧 `bind`：
  - `{type:"bind", clientId:<appId>, targetId:"", message:"targetId"}`
  - `{type:"bind", clientId:<控制方ID>, targetId:<appId>, message:"200"}`（配对确认）
- 强度：`{type:"msg", message:"strength-{通道号}+2+{强度}"}`，通道号 `1=A / 2=B`。
- 停止：`{type:"msg", message:"clear-{通道号}"}`。
- 心跳：每 30s 下发 `{type:"heartbeat", clientId:<appId>, targetId:<控制方ID>, message:"200"}`。
- V3 无原生「临时强度」，开火 = `set_strength` + 定时 `clear` + 归零 模拟。

### V4（`ws://host:9998?tid={targetId}`）

- APP 以**查询参数**携带 tid 接入；tid 为 8 位 hex。
- 接入后服务端连发两帧：`{type:"hello", clientId:<appId>}`、`{type:"controller_attached", clientId:<控制方ID>}`。
- 上行帧是**两层嵌套**结构，必须解开内层 `data`：
  ```json
  {"type": "message", "data": {"t": "req|resp|ev", ...}}
  ```
  - `t:"ev"`：`devices.snapshot` / `devices.patch` / `custom.action` 等设备事件
  - `t:"resp"`：对服务端 RPC 的响应（按 `reqId` 匹配）
  - `t:"req"`：**APP 主动发起的 RPC（含 ping），必须回复**，否则 APP 判定控制方失联并断开
- 下行 RPC：`{"type":"message","data":{"t":"req","reqId":"<自增数字>","m":"device.op","data":{...}}}`
  - 设定临时强度：`{"s":<slotId>,"c":0|1,"t":4,"v":<强度>,"d":<毫秒>,"im":true}`
  - 清理任务：`m:"device.op.clear"`，`data:{"s":<slotId>,"c":0|1}`
  - 通道：`c` 为 `0=A / 1=B`（注意与 V3 的 `1/2` 不同）
- 消息级心跳：收到 `{"type":"ping"}` 回 `{"type":"pong","ts":<毫秒>}`。

### 二维码跳转 URL 模板

```
V3: https://www.dungeon-lab.com/app-download.php#DGLAB-SOCKET#{ws_url}
V4: https://dungeon-lab.cn/s/?v=1&action=socket&url={urlencode(ws_url)}
```

---

## 七、常见问题排查

### 1. 日志刷屏：`拒绝连接：targetId 不匹配，收到=''，path='/'`

**原因**：有客户端连接根路径 `/`（不带 targetId/tid）。旧版逻辑会直接拒绝并关闭，
对方随即重连，形成刷屏。

**现状**：已按官方行为改为「接受 + 采集诊断」，日志会打印
`remote=` / `path=` / `ua=` 以及该连接的前 5 条上行消息，便于判断来源。
若这些连接来自 APP，可观察其上行消息中是否携带 targetId/tid。

### 2. V4 连上约 7 秒后自动断开

**原因**：APP 会周期性发起 `t:"req", m:"ping"` 的 RPC，服务端必须回
`{"t":"resp","reqId":...,"result":<时间戳>}`。未回复时 APP 判定控制方无响应并断连。

**排查**：日志中应有 `[dglab-v4] APP xx 发起 RPC：m=ping reqId=...`。

### 3. 报错 `TypeError: object of type 'MessageChain' has no len()`

**原因**：AstrBot 管道会执行 `len(result.chain)`，`result.chain` 必须是组件**列表**。
若把 `MessageChain` 对象整体传给 `event.chain_result()`，`chain` 就变成了对象而非列表。

**正确写法**：用 `event.make_result()`，或直接 `event.plain_result(文本)`。

### 4. 报错 `ModuleNotFoundError: No module named 'websockets'`

属运行时依赖，AstrBot 启用插件时会按 `requirements.txt` 自动安装。
本地手工验证时可用 AstrBot 自带 Python 执行：
`<AstrBot>/backend/python/python.exe -m py_compile dglab_server.py`。

### 5. 二维码里的地址不对 / APP 连不上

- `ws_host` 不要填 `127.0.0.1`——手机无法访问；应填本机局域网 IP 或公网域名。
- 检查防火墙 / 路由器是否放行 `v3_port`、`v4_port` 两个端口。
- 公网场景需要端口映射或内网穿透。

### 6. 每次重启插件 targetId 都变

**原因**：`data/plugin_data/astrbot_plugin_dglab_control/target_id.json` 未成功落盘
（常见于 Docker 未挂载持久卷、或插件数据目录不可写）。

**排查**：启动日志会明确区分：
- `[郊狼] 已从磁盘加载 targetId：<路径>`
- `[郊狼] 未找到 targetId 文件，将新建：<路径>`
- `[郊狼] targetId 持久化失败（<路径>）：<原因>；重启后二维码会变化，请检查该目录是否可写`

---

## 八、已知限制

- **未实现波形帧**：当前开火只下发 `SetTempIntensity(t=4)` 设定强度等级。
  按 DG-LAB 协议，设备通常需要持续收到波形帧（`device.op t:0` AppendPulseData）
  才会产生实际刺激，`t=4` 仅改变幅度等级。若出现「查得到设备但开火无感觉」，
  需要补充波形帧下发逻辑。
- **不支持 TLS**：仅支持 `ws://`，未实现 `wss://`。
- **不支持路径前缀**：V4 固定监听根路径，未实现 `PREFIX` 配置。
- **V3 单连接**：同一时刻只允许一个 APP 接入（新连接会替换旧连接）；
  V4 支持多 APP 多设备。

---

## 九、文件结构

```
astrbot_plugin_dglab_control/
├── main.py             # 插件入口：7 个指令、生命周期、二维码生成、targetId 持久化
├── dglab_server.py     # 内嵌 V3 / V4 WebSocket Server（协议实现）
├── qrcode_util.py      # 二维码 PNG 生成
├── _conf_schema.json   # WebUI 配置项定义
├── metadata.yaml       # 插件元数据
├── requirements.txt    # 运行时依赖
└── cache/              # 运行时临时产物：v3_qrcode.png、v4_qrcode.png

# 持久化数据（不在插件目录内，由 AstrBot 管理）：
# data/plugin_data/astrbot_plugin_dglab_control/target_id.json
```

---

## 十、参考

- 协议参考实现：[dungeonlab-open/dglab-websocket-server](https://github.com/dungeonlab-open/dglab-websocket-server)（`v3-server.ts` / `v4-server.ts`）
