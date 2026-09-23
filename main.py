# -*- coding: utf-8 -*-
"""AstrBot 插件：郊狼（DG-LAB）硬件电击控制。

插件内嵌 V3 + V4 WebSocket Server（不再依赖外部 dglab-websocket-server），
APP 扫码直连插件，机器人指令直接下发到设备，实现群内一键开火、
A/B 通道单独开关、查询已连接 APP 以及发送 V3/V4 配对二维码图片。

权限：`/郊狼二维码`、`/郊狼查询` 所有人可用；
其余控制类指令（开火 / A、B 通道开关）仅机器人管理员可用
（可通过配置项 admin_only 关闭该限制）。

参考：
- https://github.com/dungeonlab-open/dglab-websocket-server
- e:/Traecode/astrbot_plugin_course_schedule/main.py（AstrBot 插件模板）
"""

import json
import os
import secrets
import shutil
import socket
import urllib.parse
import uuid
from typing import List, Optional, Tuple

import astrbot.api.star as star
from astrbot.api.star import Context, StarTools
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import AstrBotConfig, logger

try:  # AstrBot 包方式加载
    from .dglab_server import DglabError, DglabV3Server, DglabV4Server
    from .qrcode_util import make_qrcode_image
except ImportError:  # 脚本方式调试
    from dglab_server import DglabError, DglabV3Server, DglabV4Server
    from qrcode_util import make_qrcode_image

# DG-LAB APP 跳转二维码 URL 模板
_V3_QRCODE_TEMPLATE = (
    "https://www.dungeon-lab.com/app-download.php"
    "#DGLAB-SOCKET#{ws_url}"
)
_V4_QRCODE_TEMPLATE = (
    "https://dungeon-lab.cn/s/?v=1&action=socket&url={ws_url_encoded}"
)


class DglabControlPlugin(star.Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}

        # 二维码 PNG 属临时产物，留在插件目录下的 cache/
        self._cache_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "cache"
        )
        os.makedirs(self._cache_dir, exist_ok=True)
        self._v3_qr_path = os.path.join(self._cache_dir, "v3_qrcode.png")
        self._v4_qr_path = os.path.join(self._cache_dir, "v4_qrcode.png")

        # 持久化数据（target_id.json）必须落在 data/plugin_data/<插件名>/ 下
        self._data_dir = str(StarTools.get_data_dir())

        self._v3: Optional[DglabV3Server] = None
        self._v4: Optional[DglabV4Server] = None

    # ------------------------------------------------------------ 生命周期

    async def initialize(self):
        """启动 V3 / V4 WebSocket Server，监听端口等待 APP 扫码接入。"""
        v3_id, v4_id = self._load_or_create_target_ids()
        self._v3 = DglabV3Server(
            int(self.config.get("v3_port", 9999)), v3_id, logger
        )
        self._v4 = DglabV4Server(
            int(self.config.get("v4_port", 9998)), v4_id, logger
        )
        await self._v3.start()
        await self._v4.start()
        host = self._ws_host()
        logger.info(
            f"[郊狼] 已启动内置 Server：V3=ws://{host}:{self._v3.port}/{v3_id}，"
            f"V4=ws://{host}:{self._v4.port}?tid={v4_id}，等待 APP 扫码接入。"
        )

    def _load_or_create_target_ids(self) -> Tuple[str, str]:
        """加载或生成持久化的 targetId（v3 UUID + v4 8hex）。

        targetId 一旦变化，之前发的二维码就会失效，所以必须落盘并在日志里
        区分「从磁盘加载」还是「新生成」，方便排查持久化是否生效。
        """
        path = os.path.join(self._data_dir, "target_id.json")
        # 兼容旧版本：target_id.json 原先存在插件目录 cache/ 下，
        # 若旧文件存在而新位置尚无，则自动迁移到 data/plugin_data 下，
        # 避免用户升级后 targetId 变化、已发二维码失效。
        legacy_path = os.path.join(self._cache_dir, "target_id.json")
        if os.path.exists(legacy_path) and not os.path.exists(path):
            try:
                shutil.move(legacy_path, path)
                logger.info(f"[郊狼] 已迁移旧版 targetId：{legacy_path} -> {path}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[郊狼] 迁移旧版 targetId 失败（{legacy_path}）：{e}")
        v3_id = ""
        v4_id = ""
        loaded = False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            v3_id = str(data.get("v3") or "").strip()
            v4_id = str(data.get("v4") or "").strip()
            loaded = bool(v3_id and v4_id)
        except FileNotFoundError:
            logger.info(f"[郊狼] 未找到 targetId 文件，将新建：{path}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[郊狼] 读取 targetId 文件失败（{path}）：{e}")

        if not v3_id:
            v3_id = str(uuid.uuid4())
        if not v4_id:
            v4_id = secrets.token_hex(4)

        if loaded:
            logger.info(f"[郊狼] 已从磁盘加载 targetId：{path}")
        else:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"v3": v3_id, "v4": v4_id}, f, ensure_ascii=False)
                logger.info(f"[郊狼] 已生成并写入 targetId：{path}")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"[郊狼] targetId 持久化失败（{path}）：{e}；"
                    f"重启后二维码会变化，请检查该目录是否可写"
                )
        return v3_id, v4_id

    async def terminate(self):
        if self._v3 is not None:
            await self._v3.stop()
        if self._v4 is not None:
            await self._v4.stop()

    # ------------------------------------------------------------ 配置

    def _ws_host(self) -> str:
        host = (self.config.get("ws_host") or "").strip()
        if host:
            return host
        # 留空时自动取本机 IPv4
        return _local_ipv4()

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        if not bool(self.config.get("admin_only", True)):
            return True
        return event.role in ("admin", "owner")

    def _check_admin(self, event: AstrMessageEvent) -> Optional[str]:
        """控制类指令的管理员校验：返回错误提示字符串；为 None 表示通过。

        仅用于 `/郊狼开火`、`/郊狼A开`、`/郊狼A关`、`/郊狼B开`、`/郊狼B关`；
        `/郊狼二维码` 与 `/郊狼查询` 不做校验，所有人可用。
        """
        if not self._is_admin(event):
            return "只有机器人管理员可以执行此指令。"
        return None

    # ------------------------------------------------------------ 二维码

    def _v3_qrcode_url(self) -> str:
        """V3 二维码内编码的 DG-LAB 跳转 URL。"""
        if not self._v3 or not self._v3.target_id:
            return ""
        host = self._ws_host()
        ws_url = f"ws://{host}:{self._v3.port}/{self._v3.target_id}"
        return _V3_QRCODE_TEMPLATE.format(ws_url=ws_url)

    def _v4_qrcode_url(self) -> str:
        """V4 二维码内编码的 DG-LAB 跳转 URL。"""
        if not self._v4 or not self._v4.target_id:
            return ""
        host = self._ws_host()
        ws_url = f"ws://{host}:{self._v4.port}?tid={self._v4.target_id}"
        return _V4_QRCODE_TEMPLATE.format(ws_url_encoded=urllib.parse.quote(ws_url, safe=""))

    def _render_qrcodes(self) -> Tuple[Optional[str], Optional[str], str, str]:
        """生成 V3/V4 二维码图片。返回 (v3_path, v4_path, v3_url, v4_url)。

        如 targetId 还没准备好（不会发生，因为持久化），对应路径返回 None。
        """
        v3_url = self._v3_qrcode_url()
        v4_url = self._v4_qrcode_url()
        v3_path = make_qrcode_image(v3_url, self._v3_qr_path) if v3_url else None
        v4_path = make_qrcode_image(v4_url, self._v4_qr_path) if v4_url else None
        return v3_path, v4_path, v3_url, v4_url

    # ------------------------------------------------------------ 通道控制

    async def _fire_v4(self, channel: str, strength: int, duration_ms: int) -> str:
        """对 V4 所有已接入 APP 的所有 slot 发起 SetTempIntensity。

        返回汇总字符串，如 "V4: 2 个 APP 共 3 个设备已开火 A=15"。
        """
        if self._v4 is None:
            return "V4: Server 未启动"
        if not self._v4.app_clients:
            return "V4: 暂无 APP 接入"

        app_count = 0
        device_count = 0
        errors: List[str] = []
        for cid, info in list(self._v4.app_clients.items()):
            devices = info.get("devices") or []
            if not devices:
                # APP 已接入但还没上报设备列表，主动请求一次
                try:
                    await self._v4.request_devices(cid)
                    devices = info.get("devices") or []
                except DglabError as e:
                    errors.append(f"APP {cid[:8]}.. 设备查询失败：{e}")
                    continue
                if not devices:
                    errors.append(f"APP {cid[:8]}.. 没有暴露设备")
                    continue
            app_count += 1
            for d in devices:
                slot_id = d.get("slotId")
                if not slot_id:
                    continue
                try:
                    await self._v4.set_temp_intensity(
                        cid, slot_id, channel, strength, duration_ms
                    )
                    device_count += 1
                except DglabError as e:
                    errors.append(f"{d.get('name') or slot_id[:8]}: {e}")
        head = f"V4: {app_count} 个 APP 共 {device_count} 个设备已开火 {channel}={strength}"
        if errors:
            head += "（部分失败：" + "；".join(errors[:3]) + "）"
        return head

    async def _fire_v3(self, channel: str, strength: int, duration_ms: int) -> str:
        if self._v3 is None:
            return "V3: Server 未启动"
        if not self._v3.app_ws:
            return "V3: 暂无 APP 接入"
        try:
            await self._v3.set_temp_intensity(channel, strength, duration_ms)
            return f"V3: APP {self._v3.app_client_id[:8]}.. 已开火 {channel}={strength}"
        except DglabError as e:
            return f"V3: 开火失败：{e}"

    async def _stop_v4(self, channel: str) -> str:
        if self._v4 is None:
            return "V4: Server 未启动"
        if not self._v4.app_clients:
            return "V4: 暂无 APP 接入"
        stopped = 0
        errors: List[str] = []
        for cid, info in list(self._v4.app_clients.items()):
            for d in info.get("devices") or []:
                slot_id = d.get("slotId")
                if not slot_id:
                    continue
                try:
                    await self._v4.clear_operate(cid, slot_id, channel)
                    stopped += 1
                except DglabError as e:
                    errors.append(f"{d.get('name') or slot_id[:8]}: {e}")
        head = f"V4: 已关闭 {channel} 通道 {stopped} 个设备"
        if errors:
            head += "（部分失败：" + "；".join(errors[:3]) + "）"
        return head

    async def _stop_v3(self, channel: str) -> str:
        if self._v3 is None:
            return "V3: Server 未启动"
        if not self._v3.app_ws:
            return "V3: 暂无 APP 接入"
        try:
            await self._v3.clear(channel)
            await self._v3.set_strength(channel, 0)
            return f"V3: APP {self._v3.app_client_id[:8]}.. 已关闭 {channel} 通道"
        except DglabError as e:
            return f"V3: 关闭失败：{e}"

    # ------------------------------------------------------------ 指令

    @filter.command("郊狼二维码")
    async def cmd_qrcode(self, event: AstrMessageEvent):
        # 所有人可用，不做管理员校验
        v3_path, v4_path, v3_url, v4_url = self._render_qrcodes()
        result = event.make_result()
        host = self._ws_host()
        if v3_path:
            result.file_image(v3_path)
            result.message(
                f"V3 协议二维码（旧版 DG-LAB APP）\n"
                f"跳转 URL：{v3_url}\n"
                f"wsUrl：ws://{host}:{self._v3.port}/{self._v3.target_id}"
            )
        else:
            result.message("V3: Server 未启动，请稍候重试")
        if v4_path:
            result.file_image(v4_path)
            result.message(
                f"V4 协议二维码（DG-LAB 4 新版 APP）\n"
                f"跳转 URL：{v4_url}\n"
                f"wsUrl：ws://{host}:{self._v4.port}?tid={self._v4.target_id}"
            )
        else:
            result.message("V4: Server 未启动，请稍候重试")
        yield result

    @filter.command("郊狼开火")
    async def cmd_fire(self, event: AstrMessageEvent, a: int = 0, b: int = 0):
        err = self._check_admin(event)
        if err:
            yield event.plain_result(err)
            return

        default = int(self.config.get("default_intensity", 15) or 15)
        a = a if a > 0 else default
        b = b if b > 0 else default
        duration_ms = int(self.config.get("fire_duration_ms", 3000) or 3000)

        a_msg = await self._fire_v4("A", a, duration_ms)
        a_v3 = await self._fire_v3("A", a, duration_ms)
        b_msg = await self._fire_v4("B", b, duration_ms)
        b_v3 = await self._fire_v3("B", b, duration_ms)

        report = (
            f"⚡ 一键开火！A={a} / B={b}，持续 {duration_ms}ms\n\n"
            f"【A 通道】\n{a_msg}\n{a_v3}\n\n"
            f"【B 通道】\n{b_msg}\n{b_v3}"
        )
        yield event.plain_result(report)

    @filter.command("郊狼A开")
    async def cmd_a_on(self, event: AstrMessageEvent, strength: int = 0):
        err = self._check_admin(event)
        if err:
            yield event.plain_result(err)
            return

        default = int(self.config.get("default_intensity", 15) or 15)
        strength = strength if strength > 0 else default
        duration_ms = int(self.config.get("fire_duration_ms", 3000) or 3000)

        v4 = await self._fire_v4("A", strength, duration_ms)
        v3 = await self._fire_v3("A", strength, duration_ms)
        yield event.plain_result(f"⚡ A 通道开火，强度 {strength}，持续 {duration_ms}ms\n{v4}\n{v3}")

    @filter.command("郊狼A关")
    async def cmd_a_off(self, event: AstrMessageEvent):
        err = self._check_admin(event)
        if err:
            yield event.plain_result(err)
            return
        v4 = await self._stop_v4("A")
        v3 = await self._stop_v3("A")
        yield event.plain_result(f"🛑 A 通道关闭\n{v4}\n{v3}")

    @filter.command("郊狼B开")
    async def cmd_b_on(self, event: AstrMessageEvent, strength: int = 0):
        err = self._check_admin(event)
        if err:
            yield event.plain_result(err)
            return

        default = int(self.config.get("default_intensity", 15) or 15)
        strength = strength if strength > 0 else default
        duration_ms = int(self.config.get("fire_duration_ms", 3000) or 3000)

        v4 = await self._fire_v4("B", strength, duration_ms)
        v3 = await self._fire_v3("B", strength, duration_ms)
        yield event.plain_result(f"⚡ B 通道开火，强度 {strength}，持续 {duration_ms}ms\n{v4}\n{v3}")

    @filter.command("郊狼B关")
    async def cmd_b_off(self, event: AstrMessageEvent):
        err = self._check_admin(event)
        if err:
            yield event.plain_result(err)
            return
        v4 = await self._stop_v4("B")
        v3 = await self._stop_v3("B")
        yield event.plain_result(f"🛑 B 通道关闭\n{v4}\n{v3}")

    @filter.command("郊狼查询")
    async def cmd_query(self, event: AstrMessageEvent):
        # 所有人可用，不做管理员校验
        lines = ["📡 郊狼已连接 APP 状态"]
        host = self._ws_host()  # 填了 ws_host 就用填的，留空则自动取本机 IP

        # V3
        if self._v3 is None:
            lines.append("\n[V3] Server 未启动")
        else:
            s = self._v3.status()
            lines.append(
                f"\n[V3] 监听: {host}:{s['port']}\n"
                f"targetId: {s['target_id']}\n"
                f"服务: {'运行中' if s['is_listening'] else '未启动'}, "
                f"APP: {'已接入' if s['is_paired'] else '未接入'}"
            )
            if s["is_paired"]:
                lines.append(f"已接入 APP: {s['app_client_id']}")

        # V4
        if self._v4 is None:
            lines.append("\n[V4] Server 未启动")
        else:
            s = self._v4.status()
            lines.append(
                f"\n[V4] 监听: {host}:{s['port']}\n"
                f"targetId: {s['target_id']}\n"
                f"服务: {'运行中' if s['is_listening'] else '未启动'}, "
                f"APP 数: {s['app_count']}"
            )
            for app in s["apps"]:
                short = app["client_id"][:12] + ".."
                lines.append(f"  - APP {short}: {app['device_count']} 个设备")
                for d in app["devices"]:
                    lines.append(
                        f"      • {d.get('name') or '(无名)'} | "
                        f"slot={d.get('slot_id', '')[:12]}.. | "
                        f"type={d.get('type', '')}"
                    )

        yield event.plain_result("\n".join(lines))


# ------------------------------------------------------------ 工具函数

def _local_ipv4() -> str:
    """获取本机出网 IPv4（局域网 IP），失败时回退 127.0.0.1。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:  # noqa: BLE001
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip
