# -*- coding: utf-8 -*-
"""DG-LAB WebSocket Server（V3 + V4 协议）内嵌实现。

把官方 dungeonlab-open/dglab-websocket-server（Bun/TS 版）的协议逻辑用
Python + websockets 重写到插件里。插件本身就是控制方，APP 扫码直连插件。

V3 协议（参考 v3-server.ts）：
  - 监听 0.0.0.0:9999，APP 通过 ws://host:9999/{targetId} 路径段接入
  - APP 接入后服务端发 bind 帧（携带 APP 自己的 clientId + 配对成功 message=200）
  - 强度控制：服务端把控制方"逻辑指令"转成 APP 实际收到的 msg 帧
    例如 set_strength(A, 15) → APP 收到 {"type":"msg", "message":"strength-1+2+15"}
  - clear(A) → APP 收到 {"type":"msg", "message":"clear-1"}

V4 协议（参考 v4-server.ts）：
  - 监听 0.0.0.0:9998，APP 通过 ws://host:9998?tid={targetId} 查询参数接入
  - APP 接入后服务端发 hello 帧（携带 APP 自己的 8hex clientId）+
    controller_attached 帧（携带控制方 targetId）
  - 控制方通过 message 帧 + device.op RPC 下发指令，APP 通过 resp/ev 上报
  - 服务端只做透传 + reqId 匹配，不解析 device.op 内容
"""

import asyncio
import json
import logging
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import websockets


class DglabError(Exception):
    """DG-Lab Server 通用异常。"""


def _extract_url_path(ws, path_arg) -> str:
    """兼容 websockets 12~15 的路径提取。

    - websockets < 13：handler 第二参数 path 直接是 "路径?query" 字符串
    - websockets >= 13：handler 只传 connection，路径在 connection.request.path
      （同样是 "路径?query"）；connection.path 属性已被移除
    """
    if path_arg:
        return path_arg
    req = getattr(ws, "request", None)
    if req is not None:
        p = getattr(req, "path", None)
        if p:
            return p
    return getattr(ws, "path", "") or ""


def _describe_conn(ws, url_path: str) -> str:
    """握手诊断信息：远端地址 / 路径 / User-Agent / Origin。

    用于定位"到底是谁在连"，尤其是不带 targetId 的探测型连接。
    """
    remote = getattr(ws, "remote_address", None)
    if isinstance(remote, tuple) and len(remote) >= 2:
        remote_s = f"{remote[0]}:{remote[1]}"
    else:
        remote_s = str(remote)

    ua = ""
    origin = ""
    req = getattr(ws, "request", None)
    headers = getattr(req, "headers", None) if req is not None else None
    if headers is not None:
        try:
            ua = headers.get("User-Agent", "") or ""
            origin = headers.get("Origin", "") or ""
        except Exception:  # noqa: BLE001
            pass

    parts = [f"remote={remote_s}", f"path={url_path!r}"]
    if ua:
        parts.append(f"ua={ua!r}")
    if origin:
        parts.append(f"origin={origin!r}")
    return " ".join(parts)


# 未携带 targetId 的连接最长保留时间（仅用于观察对端行为，之后关闭避免泄漏）
_UNPAIRED_KEEPALIVE_SEC = 120
# 未携带 targetId 的连接最多记录多少条上行消息
_UNPAIRED_LOG_LIMIT = 5


async def _observe_unpaired(ws, tag: str, url_path: str, logger, greet: Optional[Dict[str, Any]] = None):
    """接受「未携带配对 ID」的连接，采集诊断信息后再关闭。

    官方 server 并不会拒绝这类连接（V3 会回 bind 告知分配到的 ID，
    V4 直接视为新的控制方）。之前一律拒绝会导致对端不断重连刷屏，
    这里改为接受 + 记录握手信息与首批上行消息，便于定位是谁在连。
    """
    logger.info(f"[{tag}] 未携带配对 ID 的连接已接受：{_describe_conn(ws, url_path)}")
    count = 0

    async def _close_later():
        await asyncio.sleep(_UNPAIRED_KEEPALIVE_SEC)
        try:
            await ws.close(code=4001, reason="controller_not_found")
        except Exception:  # noqa: BLE001
            pass

    timer = asyncio.create_task(_close_later())
    try:
        if greet:
            await ws.send(json.dumps(greet))
        async for raw in ws:
            if count < _UNPAIRED_LOG_LIMIT:
                logger.info(f"[{tag}] 未配对连接上行消息：{raw!r}")
            count += 1
        logger.info(f"[{tag}] 未配对连接对端主动关闭，共收到 {count} 条消息")
    except websockets.exceptions.ConnectionClosed:
        logger.info(f"[{tag}] 未配对连接对端断开，共收到 {count} 条消息")
    except Exception as e:  # noqa: BLE001
        logger.info(f"[{tag}] 未配对连接异常：{e}（累计 {count} 条消息）")
    finally:
        timer.cancel()


# ---------------------------------------------------------------- V3 Server

class DglabV3Server:
    """V3 协议 WebSocket Server（1 APP 单连接）。

    监听 ws://0.0.0.0:{port}，APP 扫码连进来后，server 直接给 APP
    下发强度 / clear 指令，做 type=4 → msg 的格式转换。
    """

    def __init__(self, port: int, target_id: str, logger: logging.Logger):
        self.port = port
        self.target_id = target_id  # 控制方 targetId，UUID 持久化
        self.logger = logger or logging.getLogger("dglab-v3")

        self._server: Optional[websockets.WebSocketServer] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        # APP 连接状态
        self.app_ws = None           # APP WebSocket
        self.app_client_id: str = ""  # APP 端 clientId（每次接入重新生成）

    # ------------------------------------------------------ 生命周期

    async def start(self):
        self._server = await websockets.serve(
            self._handler,
            "0.0.0.0",
            self.port,
            ping_interval=20,
            ping_timeout=10,
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="dglab-v3-heartbeat"
        )
        self.logger.info(
            f"[dglab-v3] Server 已监听 0.0.0.0:{self.port}，targetId={self.target_id}"
        )

    async def stop(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await asyncio.wait_for(self._heartbeat_task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        if self.app_ws is not None:
            try:
                await self.app_ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.app_ws = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # ------------------------------------------------------ 连接处理

    async def _handler(self, ws, path=None):
        url_path = _extract_url_path(ws, path)
        # 解析路径段：必须形如 /<targetId>
        target = (url_path.lstrip("/").split("/", 1)[0]).strip()
        if target != self.target_id:
            # 未携带（或携带错误）targetId：官方 v3-server 不会拒绝这类连接，
            # 而是回一帧 bind 告知分配到的 clientId。这里保持兼容并采集诊断信息。
            await _observe_unpaired(
                ws, "dglab-v3", url_path, self.logger,
                greet={
                    "type": "bind",
                    "clientId": str(uuid.uuid4()),
                    "targetId": "",
                    "message": "targetId",
                },
            )
            return

        # 同一时刻只允许一个 APP 连接：旧连接先关掉
        if self.app_ws is not None:
            try:
                await self.app_ws.close(code=1000, reason="replaced")
            except Exception:  # noqa: BLE001
                pass
            self.app_ws = None
            self.app_client_id = ""

        self.app_client_id = str(uuid.uuid4())
        self.app_ws = ws

        # 给 APP 发首帧 bind（携带 APP 自己的 clientId，告诉 APP 已注册）
        await self._send_frame(ws, {
            "type": "bind",
            "clientId": self.app_client_id,
            "targetId": "",
            "message": "targetId",
        })
        # 通知配对成功
        await self._send_frame(ws, {
            "type": "bind",
            "clientId": self.target_id,
            "targetId": self.app_client_id,
            "message": "200",
        })
        self.logger.info(
            f"[dglab-v3] APP 已接入：{self.app_client_id}（{_describe_conn(ws, url_path)}）"
        )

        try:
            async for raw in ws:
                await self._on_app_message(raw)
        except websockets.exceptions.ConnectionClosed as e:
            self.logger.info(
                f"[dglab-v3] APP 连接关闭："
                f"code={getattr(e, 'code', None)} reason={getattr(e, 'reason', '')!r}"
            )
        finally:
            if self.app_ws is ws:
                self.app_ws = None
                self.app_client_id = ""
            self.logger.info("[dglab-v3] APP 已断开")

    async def _send_frame(self, ws, frame: Dict[str, Any]):
        if ws is None:
            return
        await ws.send(json.dumps(frame))

    async def _on_app_message(self, raw):
        """V3 APP 上行：冗余 bind（幂等回 200）、feedback/strength 上报等。"""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(data, dict):
            return
        ftype = data.get("type")
        message = str(data.get("message") or "")

        # APP 主动回发的 bind：配对已在连接时完成，这里幂等重发 200
        if ftype == "bind":
            if self.app_ws is not None:
                await self._send_frame(self.app_ws, {
                    "type": "bind",
                    "clientId": self.target_id,
                    "targetId": self.app_client_id,
                    "message": "200",
                })
            return
        if ftype == "heartbeat":
            return
        if ftype == "msg" and (
            message.startswith("feedback") or message.startswith("strength")
        ):
            self.logger.info(f"[dglab-v3] APP 上报：{message}")
            return
        self.logger.info(f"[dglab-v3] APP 消息：type={ftype} message={message}")

    # ------------------------------------------------------ 控制 API

    async def set_strength(self, channel: str, value: int) -> bool:
        """设置指定通道绝对强度。

        server 端做格式转换：控制方逻辑指令 → APP 收到的 msg 帧
        例如 channel="A" value=15 → APP 收到 message="strength-1+2+15"
        """
        if not self.app_ws:
            raise DglabError("V3 暂无 APP 接入")
        ch_num = self._channel_num(channel)
        await self._send_frame(self.app_ws, {
            "type": "msg",
            "clientId": self.target_id,
            "targetId": self.app_client_id,
            "message": f"strength-{ch_num}+2+{int(value)}",
        })
        return True

    async def clear(self, channel: str) -> bool:
        """清除指定通道波形。"""
        if not self.app_ws:
            raise DglabError("V3 暂无 APP 接入")
        ch_num = self._channel_num(channel)
        await self._send_frame(self.app_ws, {
            "type": "msg",
            "clientId": self.target_id,
            "targetId": self.app_client_id,
            "message": f"clear-{ch_num}",
        })
        return True

    async def set_temp_intensity(
        self, channel: str, value: int, duration_ms: int
    ) -> bool:
        """V3 协议没有 SetTempIntensity 原语，用 set_strength + 定时
        clear + set_strength(0) 模拟。"""
        await self.set_strength(channel, value)
        asyncio.create_task(
            self._auto_reset(channel, duration_ms / 1000),
            name=f"v3-auto-reset-{channel}",
        )
        return True

    async def _auto_reset(self, channel: str, seconds: float):
        try:
            await asyncio.sleep(seconds)
            try:
                await self.clear(channel)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"[dglab-v3] 自动 clear 失败：{e}")
            try:
                await self.set_strength(channel, 0)
            except Exception as e:  # noqa: BLE001
                self.logger.warning(f"[dglab-v3] 自动归零失败：{e}")
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _channel_num(channel: str) -> int:
        ch = channel.upper()
        if ch == "A":
            return 1
        if ch == "B":
            return 2
        raise DglabError(f"通道参数错误：{channel}")

    # ------------------------------------------------------ 心跳

    async def _heartbeat_loop(self):
        """V3 协议要求服务端定期给 APP 发心跳。

        帧格式跟官方 v3-server 一致：clientId 是接收方（APP）自己的 id，
        targetId 是其配对端（控制方）的 id。
        """
        while True:
            try:
                if self.app_ws is not None:
                    await self._send_frame(self.app_ws, {
                        "type": "heartbeat",
                        "clientId": self.app_client_id,
                        "targetId": self.target_id,
                        "message": "200",
                    })
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(30)

    # ------------------------------------------------------ 查询

    def status(self) -> Dict[str, Any]:
        return {
            "protocol": "V3",
            "port": self.port,
            "target_id": self.target_id,
            "is_listening": self._server is not None,
            "is_paired": self.app_ws is not None,
            "app_client_id": self.app_client_id,
        }


# ---------------------------------------------------------------- V4 Server

class DglabV4Server:
    """V4 协议 WebSocket Server（1 控制方 : N APP 被控方）。

    监听 ws://0.0.0.0:{port}，APP 扫码连进来后，server 直接给 APP
    下发 message 帧（内含 device.op RPC），并匹配 reqId 等待 APP 响应。
    """

    def __init__(self, port: int, target_id: str, logger: logging.Logger):
        self.port = port
        self.target_id = target_id  # 控制方 targetId，8hex 持久化
        self.logger = logger or logging.getLogger("dglab-v4")

        self._server: Optional[websockets.WebSocketServer] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        # 已接入 APP：{clientId: {"ws":..., "devices": [...]}}
        self.app_clients: Dict[str, Dict[str, Any]] = {}

        self._seq = 0
        self._pending: Dict[str, asyncio.Future] = {}

    # ------------------------------------------------------ 生命周期

    async def start(self):
        self._server = await websockets.serve(
            self._handler,
            "0.0.0.0",
            self.port,
            ping_interval=20,
            ping_timeout=10,
        )
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="dglab-v4-heartbeat"
        )
        self.logger.info(
            f"[dglab-v4] Server 已监听 0.0.0.0:{self.port}，targetId={self.target_id}"
        )

    async def stop(self):
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await asyncio.wait_for(self._heartbeat_task, timeout=2)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        # 取消所有未完成 RPC
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(DglabError("V4 server 关闭"))
        self._pending.clear()
        # 关闭所有 APP 连接
        for client in list(self.app_clients.values()):
            ws = client.get("ws")
            if ws is not None:
                try:
                    await ws.close(code=1000, reason="server_shutdown")
                except Exception:  # noqa: BLE001
                    pass
        self.app_clients.clear()
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # ------------------------------------------------------ 连接处理

    async def _handler(self, ws, path=None):
        url_path = _extract_url_path(ws, path)
        parsed = urlparse(url_path)
        tid_list = parse_qs(parsed.query).get("tid") or [""]
        tid = (tid_list[0] or "").strip()
        if tid != self.target_id:
            # 未携带（或携带错误）tid：官方 v4-server 视为新的控制方并下发 hello。
            # 这里保持兼容 + 采集诊断信息，不再直接拒绝（否则对端会不断重连刷屏）。
            await _observe_unpaired(
                ws, "dglab-v4", url_path, self.logger,
                greet={"type": "hello", "clientId": self._gen_client_id()},
            )
            return

        # 注册新 APP
        app_client_id = self._gen_client_id()
        self.app_clients[app_client_id] = {"ws": ws, "devices": []}

        # 发握手帧：hello + controller_attached
        await self._send_frame(ws, {"type": "hello", "clientId": app_client_id})
        await self._send_frame(ws, {
            "type": "controller_attached",
            "clientId": self.target_id,
        })
        self.logger.info(
            f"[dglab-v4] APP 接入：{app_client_id}（{_describe_conn(ws, url_path)}）"
        )

        try:
            async for raw in ws:
                await self._on_app_message(app_client_id, raw)
        except websockets.exceptions.ConnectionClosed as e:
            self.logger.info(
                f"[dglab-v4] APP {app_client_id} 连接关闭："
                f"code={getattr(e, 'code', None)} reason={getattr(e, 'reason', '')!r}"
            )
        finally:
            self.app_clients.pop(app_client_id, None)
            self.logger.info(f"[dglab-v4] APP 断开：{app_client_id}")

    async def _send_frame(self, ws, frame: Dict[str, Any]):
        if ws is None:
            return
        await ws.send(json.dumps(frame))

    async def _send_message_data(self, app_client_id: str, payload: Dict[str, Any]):
        """发送 V4 应用层消息帧：{"type":"message","data":payload}。

        外层不带 clientId，跟官方 v4-server 转发格式一致。
        """
        client = self.app_clients.get(app_client_id)
        if not client:
            return
        await self._send_frame(client["ws"], {"type": "message", "data": payload})

    async def _on_app_message(self, app_client_id: str, raw):
        """APP 上行帧处理。

        APP 帧是两层结构：{"type":"message","data":{...}}，
        data.t ∈ {req, resp, ev}，必须解开内层 data 才能读到。
        """
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            self.logger.debug(f"[dglab-v4] 非 JSON 消息：{raw!r}")
            return
        if not isinstance(frame, dict):
            return

        ftype = frame.get("type")

        # 消息级 ping → 回 pong（官方 v4-server handlePingMessage）
        if ftype == "ping":
            client = self.app_clients.get(app_client_id)
            if client:
                await self._send_frame(
                    client["ws"], {"type": "pong", "ts": int(time.time() * 1000)}
                )
            return
        if ftype in ("pong", "heartbeat"):
            return
        if ftype != "message":
            self.logger.info(
                f"[dglab-v4] APP {app_client_id} 未知帧 type={ftype!r}：{raw!r}"
            )
            return

        data = frame.get("data")
        if not isinstance(data, dict):
            self.logger.info(f"[dglab-v4] APP {app_client_id} message 无 data：{raw!r}")
            return

        t = data.get("t")

        if t == "resp":
            req_id = data.get("reqId", "")
            fut = self._pending.pop(req_id, None)
            if fut is None or fut.done():
                return
            err = data.get("error")
            if err:
                fut.set_exception(DglabError(f"V4 RPC 错误：{err}"))
            else:
                result = data.get("result")
                # devices.get 的响应同样携带设备列表，同步进本地缓存
                if isinstance(result, dict) and isinstance(result.get("devices"), list):
                    client = self.app_clients.get(app_client_id)
                    if client is not None:
                        client["devices"] = list(result["devices"])
                        self.logger.info(
                            f"[dglab-v4] APP {app_client_id} 返回 "
                            f"{len(client['devices'])} 个设备"
                        )
                fut.set_result(result)
            return

        if t == "req":
            # APP 主动发起的 RPC 必须回复，否则 APP 判定控制方失联并断开连接
            await self._on_app_request(app_client_id, data)
            return

        if t == "ev":
            client = self.app_clients.get(app_client_id)
            if not client:
                return
            ev = data.get("ev")
            if ev == "devices.snapshot":
                client["devices"] = list(data.get("devices") or [])
                self.logger.info(
                    f"[dglab-v4] APP {app_client_id} 上报 "
                    f"{len(client['devices'])} 个设备"
                )
            elif ev == "devices.patch":
                added = list(data.get("added") or [])
                removed = set(data.get("removed") or [])
                client["devices"] = [
                    d for d in client["devices"]
                    if d.get("slotId") not in removed
                ] + added
            elif ev == "custom.action":
                self.logger.info(
                    f"[dglab-v4] APP {app_client_id} 自定义动作：{data.get('action')}"
                )
            else:
                self.logger.info(f"[dglab-v4] APP {app_client_id} 事件：ev={ev}")
            return

        self.logger.info(f"[dglab-v4] APP {app_client_id} 未识别 data：{data}")

    async def _on_app_request(self, app_client_id: str, data: Dict[str, Any]):
        """回复 APP 主动发起的 RPC：ping 回时间戳，其余回 unimplemented。"""
        req_id = data.get("reqId", "")
        method = data.get("m", "")
        self.logger.info(
            f"[dglab-v4] APP {app_client_id} 发起 RPC：m={method} reqId={req_id}"
        )
        payload: Dict[str, Any] = {"t": "resp"}
        if req_id:
            payload["reqId"] = req_id
        if method == "ping":
            payload["result"] = int(time.time() * 1000)
        else:
            payload["error"] = "unimplemented"
        await self._send_message_data(app_client_id, payload)

    # ------------------------------------------------------ RPC

    async def _send_req(
        self,
        app_client_id: str,
        method: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: float = 8.0,
    ):
        client = self.app_clients.get(app_client_id)
        if not client:
            raise DglabError(f"APP {app_client_id} 未接入或已断开")
        ws = client["ws"]
        if ws is None:
            raise DglabError(f"APP {app_client_id} WebSocket 已关闭")

        self._seq += 1
        req_id = str(self._seq)  # 官方 kit 用自增数字字符串
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[req_id] = fut

        await self._send_message_data(app_client_id, {
            "t": "req",
            "reqId": req_id,
            "m": method,
            "data": data or {},
        })
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise DglabError(f"V4 RPC 超时（{timeout}s）：{method}")

    async def request_devices(self, app_client_id: str):
        """主动请求 APP 当前设备列表。"""
        return await self._send_req(app_client_id, "devices.get")

    # ------------------------------------------------------ 控制 API

    @staticmethod
    def _channel_code(channel: str) -> int:
        ch = channel.upper()
        if ch == "A":
            return 0
        if ch == "B":
            return 1
        raise DglabError(f"通道参数错误：{channel}")

    async def set_temp_intensity(
        self, app_client_id: str, slot_id: str, channel: str,
        value: int, duration_ms: int, timeout: float = 8.0,
    ):
        """SetTempIntensity (t=4)：设置临时强度，到时自动归零。"""
        c = self._channel_code(channel)
        return await self._send_req(
            app_client_id, "device.op",
            {
                "s": slot_id, "t": 4, "c": c,
                "d": int(duration_ms), "v": int(value), "im": True,
            },
            timeout=timeout,
        )

    async def clear_operate(
        self, app_client_id: str, slot_id: str, channel: str,
        timeout: float = 8.0,
    ):
        """清理指定设备指定通道的全部任务。"""
        c = self._channel_code(channel)
        return await self._send_req(
            app_client_id, "device.op.clear",
            {"s": slot_id, "c": c},
            timeout=timeout,
        )

    # ------------------------------------------------------ 心跳

    async def _heartbeat_loop(self):
        """V4 协议要求服务端每 30s 给所有 APP 发心跳。"""
        while True:
            try:
                for client in list(self.app_clients.values()):
                    ws = client.get("ws")
                    if ws is None:
                        continue
                    await self._send_frame(ws, {"type": "heartbeat"})
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(30)

    # ------------------------------------------------------ 查询

    def list_clients(self) -> List[Dict[str, Any]]:
        result = []
        for cid, info in self.app_clients.items():
            devices = info.get("devices") or []
            result.append({
                "client_id": cid,
                "devices": [
                    {
                        "slot_id": d.get("slotId", ""),
                        "name": d.get("name", ""),
                        "type": d.get("type", ""),
                    }
                    for d in devices
                ],
                "device_count": len(devices),
            })
        return result

    def status(self) -> Dict[str, Any]:
        return {
            "protocol": "V4",
            "port": self.port,
            "target_id": self.target_id,
            "is_listening": self._server is not None,
            "app_count": len(self.app_clients),
            "apps": self.list_clients(),
        }

    # ------------------------------------------------------ 工具

    def _gen_client_id(self) -> str:
        """生成 8 位 hex clientId，跟当前已存在的不重复。"""
        while True:
            cid = secrets.token_hex(4)
            if cid not in self.app_clients:
                return cid
